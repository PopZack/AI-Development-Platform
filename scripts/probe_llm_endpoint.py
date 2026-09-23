"""LLM 端点行为探针。

**用途**：当 LLM 调用超时 / 变慢 / 输出异常时，先绕开应用直接量端点行为，
把「病因在 provider 侧还是在应用侧」分开。推断在这里特别不可靠 ——
尤其 httpx 的 ``timeout`` 是**读超时**（两次数据到达之间的最大间隔），
非流式下它等价于总时长上限，流式下几乎不会触发。同一个数字，两套含义。

它回答四个问题：

1. **端点支持流式吗？** 首字节与块间最大间隔各是多少 ——
   块间隔亚秒级的话，「长静默期导致读超时」用流式就能根治，不用放宽超时。
2. **``max_tokens`` 被尊重吗？** 不尊重的话，代码里任何「输出上限」都是假保证。
3. **``usage`` 怎么给的？** 注意 ``completion_tokens`` 可能包含**模型内部推理 token**，
   它**不等于**可见输出量 —— 拿它判断「输出太长」会得出反向结论。
4. **``response_format=json_object`` 与流式能共存吗？** 改流式前必须确认。

用法::

    uv run python scripts/probe_llm_endpoint.py           # 常规探针，约 1~2 分钟
    uv run python scripts/probe_llm_endpoint.py --full    # 用真实业务规模的长请求

只读、不改任何状态；不打真实业务数据，也不打印密钥。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# 直接 `python scripts/probe_llm_endpoint.py` 时 sys.path[0] 是 scripts/，
# 仓库根不在路径上，import app 会失败（与 scripts/eval_agents.py 同样的处理）
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config.settings import Settings  # noqa: E402

__all__ = ["ProbeResult", "run_probes"]

_MEDIUM_ASK = (
    "用 Python 写一个 strutils.py：提供 slugify(text)，转小写、空白与连字符归一为单个连字符、"
    "去首尾连字符；再写一个 pytest 用例文件覆盖边界情况。"
)

# 真实业务规模：Developer 角色就是这种量级的「给出整份文件内容」请求
_FULL_ASK = (
    "在工作区实现 strutils.py，提供 slugify(text)：转小写、空白与连字符归一为单个连字符、"
    "去首尾连字符。再给 test_strutils.py 用 pytest 覆盖边界用例。"
    "只输出一个 JSON 对象，字段 changes 是数组，每项含 path 与 new_content（文件的完整内容）。"
)


@dataclass
class ProbeResult:
    name: str
    http_status: int = 0
    first_byte_s: float = 0.0
    first_content_s: float | None = None
    chunks: int = 0
    chars: int = 0
    max_gap_s: float = 0.0
    total_s: float = 0.0
    usage: dict[str, Any] | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


async def _probe(
    name: str,
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    budget: float = 600.0,
) -> ProbeResult:
    """发一次流式请求，逐块量时间间隔。"""
    result = ProbeResult(name=name)
    started = time.time()
    gaps: list[float] = []

    async with httpx.AsyncClient(timeout=budget) as client:
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                result.http_status = response.status_code
                result.first_byte_s = time.time() - started
                if response.status_code >= 400:
                    body = await response.aread()
                    result.error = body[:200].decode("utf-8", "replace")
                    result.total_s = time.time() - started
                    return result

                last = time.time()
                async for line in response.aiter_lines():
                    now = time.time()
                    gaps.append(now - last)
                    last = now
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        body = json.loads(data)
                    except json.JSONDecodeError:
                        result.notes.append("有 chunk 不是合法 JSON")
                        continue
                    if body.get("usage"):
                        result.usage = body["usage"]
                    delta = (body.get("choices") or [{}])[0].get("delta") or {}
                    piece = delta.get("content") or ""
                    reasoning = delta.get("reasoning_content") or ""
                    if reasoning:
                        result.notes.append("端点会下发 reasoning_content")
                    if piece and result.first_content_s is None:
                        result.first_content_s = now - started
                    if piece or reasoning:
                        result.chunks += 1
                    result.chars += len(piece)
        except Exception as exc:  # noqa: BLE001 - 探针：任何异常都是结论
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            result.total_s = time.time() - started
            result.max_gap_s = max(gaps) if gaps else 0.0
            if not payload.get("stream"):
                result.notes.append("非流式：无 SSE 分帧，看首字节即可（块数/字符数恒为 0）")
    return result


def _report(results: list[ProbeResult]) -> None:
    print()
    print("探针结果")
    print("-" * 92)
    for r in results:
        head = f"{r.name:<22} HTTP {r.http_status}"
        if r.error:
            print(f"{head}  ❌ {r.error[:60]}  （耗时 {r.total_s:.1f}s）")
            continue
        first = "—" if r.first_content_s is None else f"{r.first_content_s:.1f}s"
        print(
            f"{head}  首字节 {r.first_byte_s:.1f}s | 首内容块 {first} | 块数 {r.chunks} "
            f"| 内容 {r.chars} 字符 | 块间最大 {r.max_gap_s:.1f}s | 总 {r.total_s:.1f}s"
        )
        if r.usage:
            print(
                f"{'':<22}  usage: prompt={r.usage.get('prompt_tokens')} "
                f"completion={r.usage.get('completion_tokens')} total={r.usage.get('total_tokens')}"
            )
        for note in dict.fromkeys(r.notes):
            print(f"{'':<22}  · {note}")
    print("-" * 92)


def _verdicts(results: list[ProbeResult], *, asked_max_tokens: int) -> None:
    by_name = {r.name: r for r in results}
    print()
    print("判读")
    print("-" * 92)

    stream = by_name.get("流式 · 常规")
    if stream and not stream.error:
        if stream.max_gap_s < 5:
            print(f"· 块间最大间隔 {stream.max_gap_s:.1f}s → 亚秒级：改流式可根治「静默期读超时」")
        else:
            print(f"· 块间最大间隔 {stream.max_gap_s:.1f}s → 偏大，流式也未必救得了，考虑异步化")
        if stream.first_content_s is not None:
            print(f"· 首内容块 {stream.first_content_s:.1f}s → 生成期进度提示是可行的")

    capped = by_name.get("max_tokens 上限")
    if capped and capped.usage:
        actual = capped.usage.get("completion_tokens") or 0
        if actual > asked_max_tokens:
            print(
                f"· 要了 max_tokens={asked_max_tokens}，实得 completion={actual} "
                "→ 端点**忽略 max_tokens**，代码里任何输出上限都是假保证"
            )
        else:
            print(f"· max_tokens={asked_max_tokens} 生效（实得 {actual}）")

    alt = by_name.get("max_completion_tokens 上限")
    if alt and alt.usage:
        alt_actual = alt.usage.get("completion_tokens") or 0
        if alt_actual > asked_max_tokens:
            print(f"· max_completion_tokens 也被忽略（要 {asked_max_tokens}，实得 {alt_actual}）")
        elif alt.chars == 0:
            print(
                f"· max_completion_tokens={asked_max_tokens} **生效**，但内容 0 字符："
                "额度被内部推理吃光 → 小额度上限会把回答变成空串，**不能当护栏用**"
            )
        else:
            print(f"· max_completion_tokens={asked_max_tokens} 生效（实得 {alt_actual}）")

    if stream and stream.usage and stream.chars:
        ratio = stream.chars / max(1, stream.usage.get("completion_tokens") or 1)
        print(
            f"· 可见内容 {stream.chars} 字符 : completion_tokens {stream.usage.get('completion_tokens')}"
            f"（≈{ratio:.1f} 字符/token）"
        )
        if ratio < 3:
            print("  → 比例偏低：completion_tokens 里含**内部推理 token**，别拿它当输出规模")

    json_mode = by_name.get("流式 + json_mode")
    if json_mode:
        verdict = "可用" if json_mode.http_status == 200 and not json_mode.error else "有问题"
        print(f"· response_format=json_object 与流式共存：{verdict}")

    non_stream = by_name.get("非流式 · 对照")
    if non_stream and stream and not non_stream.error and not stream.error:
        print(f"· 首字节对比：非流式 {non_stream.first_byte_s:.1f}s vs 流式 {stream.first_byte_s:.1f}s")
    print("-" * 92)


async def run_probes(*, full: bool) -> list[ProbeResult]:
    settings = Settings()
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    model = settings.llm_model

    print(f"端点 {url}")
    print(
        f"模型 {model} | 配置的读超时 {settings.llm_timeout_seconds}s | key 长度 {len(settings.llm_api_key)}"
    )
    print()

    ask = _FULL_ASK if full else _MEDIUM_ASK
    # 上限探针故意要一个很小的值：如果实得远大于它，就说明参数被忽略了
    asked = 60

    base = {"model": model, "messages": [{"role": "user", "content": ask}]}
    legacy_cap = {**base, "max_tokens": asked, "stream": True, "stream_options": {"include_usage": True}}
    alt_cap = {
        **base,
        "max_completion_tokens": asked,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    probes = [
        ("非流式 · 对照", {**base, "max_tokens": 512}),
        (
            "流式 · 常规",
            {**base, "max_tokens": 512, "stream": True, "stream_options": {"include_usage": True}},
        ),
        ("max_tokens 上限", legacy_cap),
        ("max_completion_tokens 上限", alt_cap),
        (
            "流式 + json_mode",
            {
                **base,
                "max_tokens": 1024,
                "stream": True,
                "stream_options": {"include_usage": True},
                "response_format": {"type": "json_object"},
            },
        ),
    ]

    results: list[ProbeResult] = []
    for name, payload in probes:
        print(f"  跑 {name} …")
        results.append(await _probe(name, url=url, headers=headers, payload=payload))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 端点行为探针")
    parser.add_argument("--full", action="store_true", help="用真实业务规模的长请求（更慢、更接近线上）")
    args = parser.parse_args()

    results = asyncio.run(run_probes(full=args.full))
    _report(results)
    _verdicts(results, asked_max_tokens=60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
