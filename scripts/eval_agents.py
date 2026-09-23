"""Agent 评估集入口。

用法：

    # 1) 评估器自测（毫秒级、确定性，可进 CI）
    uv run python scripts/eval_agents.py --provider mock

    # 2) 用真实模型评估（花钱、几十秒到几分钟）
    uv run python scripts/eval_agents.py --provider real --min-pass-rate 0.7

    # 3) 留档（可选；默认只打 stdout，不往仓库里落过程产物）
    uv run python scripts/eval_agents.py --provider real --json-out /tmp/eval.json

退出码：0 = 通过率达标；1 = 未达标或评估器自检失败（可当门禁用）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.runner import (  # noqa: E402
    CaseResult,
    load_cases,
    prepare_real_environment,
    run_mock,
    run_real,
    seed_requirement,
)

DEFAULT_CASES = ROOT / "evals" / "agent_cases.json"


def _print_case(result: CaseResult) -> None:
    mark = "✅" if result.passed else "❌"
    extra = f"  {result.latency_ms / 1000:.1f}s  {result.tokens} tokens" if result.tokens else ""
    print(f"  {mark} {result.case_id}  [{result.agent}]  {result.title}{extra}")
    if result.error:
        print(f"      错误：{result.error}")
    for failure in result.failures:
        print(f"      · 未通过：{failure}")
    for note in result.notes:
        print(f"      · {note}")
    if not result.passed and result.payload:
        # 诊断信息：只看「挂了」没用，要知道模型到底怎么想的，
        # 才能判断是模型的问题还是用例的问题
        verdict = result.payload.get("verdict") or result.payload.get("title") or ""
        summary = str(result.payload.get("summary", ""))[:220]
        print(f"      模型结论：{verdict} —— {summary}")
        for finding in (result.payload.get("findings") or [])[:3]:
            if isinstance(finding, dict):
                print(f"      ↳ [{finding.get('severity')}] {str(finding.get('comment', ''))[:160]}")


def _summary(results: list[CaseResult]) -> tuple[int, float, int]:
    passed = sum(1 for r in results if r.passed)
    rate = passed / len(results) if results else 0.0
    tokens = sum(r.tokens for r in results)
    return passed, rate, tokens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent 评估集")
    parser.add_argument("--provider", choices=["mock", "real"], default="mock")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=1.0,
        help="通过率下限；低于它返回非 0（mock 模式默认要求全过）",
    )
    parser.add_argument("--json-out", type=Path, default=None, help="可选：把结果写到指定文件")
    parser.add_argument("--only", default=None, help="只跑某个用例 id（调模型花钱，定位问题用）")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每个用例跑几次。判断类任务有方差（实测同一输入两次结论不同），"
        "单跑一次的结果不能当结论 —— 报告里给出「n 次中通过几次」",
    )
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.only:
        cases = [c for c in cases if c.id == args.only or args.only in c.id]
        if not cases:
            print(f"没有匹配的用例：{args.only}")
            return 2
    print(f"评估集：{args.cases.name}  用例 {len(cases)} 条  模式 {args.provider}\n")

    if args.provider == "mock":
        results, problems = run_mock(cases)
        print("【评估器自测】预置好输出必须全过、坏输出必须挂至少一条")
        for result in results:
            _print_case(result)
        if problems:
            print("\n⚠️ 评估器自身的问题（先修这里，否则 real 模式的分数不可信）：")
            for problem in problems:
                print(f"  · {problem}")
        passed, rate, _ = _summary(results)
        print(f"\n评估器自测：{passed}/{len(results)} 通过")
        ok = not problems and rate >= args.min_pass_rate
        print("✅ 评估器可用" if ok else "❌ 评估器有问题")
        return 0 if ok else 1

    # ---------------- 真实模型 ----------------
    from app.config.settings import Settings
    from app.infrastructure.llm import create_llm_provider

    async def run() -> int:
        with tempfile.TemporaryDirectory(prefix="agent-eval-") as tmp:
            settings = Settings(
                app_env="local",
                database_url=f"sqlite+aiosqlite:///{(Path(tmp) / 'eval.db').as_posix()}",
                workspace_root=str(Path(tmp) / "workspace"),
            )
            print(f"模型：{settings.llm_model} @ {settings.llm_base_url}\n")
            engine = await prepare_real_environment(settings)
            provider = create_llm_provider(settings)
            try:
                requirement_id = await seed_requirement(settings)
                from app.infrastructure.db.session import get_session_factory

                async with get_session_factory()() as session:
                    results = []
                    for round_index in range(max(1, args.repeat)):
                        if args.repeat > 1:
                            print(f"--- 第 {round_index + 1}/{args.repeat} 轮 ---")
                        round_results = await run_real(
                            cases, provider=provider, session=session, requirement_id=requirement_id
                        )
                        for result in round_results:
                            # case_id 带上轮次：重复运行时要把每一轮都看成独立样本
                            if args.repeat > 1:
                                result.notes.append(f"第 {round_index + 1} 轮")
                            results.append(result)
            finally:
                await provider.aclose()
                await engine.dispose()

        for result in results:
            _print_case(result)
        passed, rate, tokens = _summary(results)
        print(f"\n真实模型：{passed}/{len(results)} 通过（通过率 {rate:.0%}）  合计 {tokens} tokens")

        if args.repeat > 1:
            # 逐用例看稳定性：某个用例偶尔挂 vs 稳定挂，含义完全不同
            print(f"\n逐用例稳定性（每例 {args.repeat} 次）：")
            by_case: dict[str, list[bool]] = {}
            for result in results:
                by_case.setdefault(result.case_id, []).append(result.passed)
            for case_id, outcomes in by_case.items():
                ok = sum(outcomes)
                flag = "稳定通过" if ok == len(outcomes) else ("稳定失败" if ok == 0 else "有波动")
                print(f"  {case_id}: {ok}/{len(outcomes)}  {flag}")

        if args.json_out:
            args.json_out.write_text(
                json.dumps(
                    [
                        {
                            "id": r.case_id,
                            "agent": r.agent,
                            "passed": r.passed,
                            "failures": r.failures,
                            "latency_ms": r.latency_ms,
                            "tokens": r.tokens,
                            "error": r.error,
                        }
                        for r in results
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"结果已写入 {args.json_out}")

        ok = rate >= args.min_pass_rate
        print(f"{'✅ 通过率达标' if ok else '❌ 通过率低于阈值'}（阈值 {args.min_pass_rate:.0%}）")
        return 0 if ok else 1

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
