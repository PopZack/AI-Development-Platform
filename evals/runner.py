"""Agent 评估集执行器。

Layer: 评估工具（不进生产镜像）。

### 两种模式，解决两个不同的问题

| 模式 | 回答的问题 | 用不用真实模型 | 进 CI |
|---|---|---|---|
| ``mock`` | **评估器本身可信吗**（断言能不能区分好输出与坏输出） | 不用 | 可以（毫秒级、确定性） |
| ``real`` | **模型与 Prompt 现在表现如何** | 用 | 不进（花钱、有波动） |

mock 模式刻意不调用模型，而是拿用例里预置的 ``mock_output`` / ``mock_bad_output``
喂断言引擎：

- 好输出**必须全部通过**
- 坏输出**必须至少挂一条**

这一步是评估集的「自测」。没有它，评估器可能悄悄失效（比如断言路径写错，
取到 None 却判成通过），而 real 模式给出的低分会被误读成「模型不行」。

real 模式跑的是真实链路：临时建库 → 造需求 → ``AgentRuntime``（含 JSON 解析、
Pydantic 校验、内容层重试）→ 拿**通过校验**的输出跑断言。Agent 输出没过校验
本身就算用例失败 —— 文档 §14.3 那条硬线在评估里同样成立。

### 结果只打到 stdout

评估结果是过程产物，不该往仓库里落文件（要留档就显式传 ``--json-out``）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from app.agent.outputs import Prd, ReviewFindings, TestReport
from app.agent.roles import (
    PRODUCT_SPEC,
    REVIEWER_SPEC,
    TESTER_SPEC,
    build_prd_user_prompt,
    build_reviewer_user_prompt,
    build_tester_user_prompt,
)
from evals.assertions import Assertion, AssertionResult, evaluate

__all__ = [
    "CaseResult",
    "EvalCase",
    "SPECS",
    "load_cases",
    "prepare_real_environment",
    "run_mock",
    "run_real",
    "seed_requirement",
]

# agent 名 → (spec, 输出模型)
SPECS: dict[str, tuple[Any, type[BaseModel]]] = {
    "product": (PRODUCT_SPEC, Prd),
    "reviewer": (REVIEWER_SPEC, ReviewFindings),
    "tester": (TESTER_SPEC, TestReport),
}


@dataclass
class EvalCase:
    id: str
    agent: str
    kind: str
    title: str
    input: dict[str, Any]
    assertions: list[Assertion]
    mock_output: dict[str, Any] | None = None
    mock_bad_output: dict[str, Any] | None = None
    mock_bad_reason: str = ""
    expect: dict[str, Any] = field(default_factory=dict)

    @property
    def output_model(self) -> type[BaseModel]:
        return SPECS[self.agent][1]

    def build_user_prompt(self) -> str:
        """按 agent 类型从用例输入拼用户消息。

        ⚠️ 这里与生产路径用的是**同一组** ``build_*_user_prompt``。
        评估集如果能用一套「专门为评估优化过」的提示词，它衡量的就不是生产行为了。
        """
        data = self.input
        if self.agent == "product":
            return build_prd_user_prompt(
                title=data["title"],
                description=data["description"],
                priority=data.get("priority", "P1"),
                acceptance_criteria=list(data.get("acceptance_criteria") or []),
            )
        if self.agent == "reviewer":
            return build_reviewer_user_prompt(
                prd=data["prd"],
                architecture=data["architecture"],
                patch_summary=data.get("patch_summary", ""),
                diff=data.get("diff", ""),
                test_report=data.get("test_report", {}),
            )
        if self.agent == "tester":
            return build_tester_user_prompt(
                prd=data["prd"],
                architecture=data["architecture"],
                written_files=list(data.get("written_files") or []),
                execution=data.get("execution"),
            )
        raise ValueError(f"未知 agent: {self.agent}")


@dataclass
class CaseResult:
    case_id: str
    title: str
    agent: str
    passed: bool
    failures: list[str]
    notes: list[str] = field(default_factory=list)
    latency_ms: int = 0
    tokens: int = 0
    error: str | None = None
    payload: dict[str, Any] | None = None


def load_cases(path: Path) -> list[EvalCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases: list[EvalCase] = []
    for item in raw["cases"]:
        cases.append(
            EvalCase(
                id=item["id"],
                agent=item["agent"],
                kind=item.get("kind", "quality"),
                title=item["title"],
                input=item["input"],
                assertions=[Assertion.from_dict(a) for a in item["assertions"]],
                mock_output=item.get("mock_output"),
                mock_bad_output=item.get("mock_bad_output"),
                mock_bad_reason=item.get("mock_bad_reason", ""),
                expect=item.get("expect", {}),
            )
        )
    return cases


# ---------------------------------------------------------------- 评估器自测


def run_mock(cases: list[EvalCase]) -> tuple[list[CaseResult], list[str]]:
    """拿预置输出验证断言引擎。返回（用例结果, 环境级问题列表）。"""
    results: list[CaseResult] = []
    problems: list[str] = []

    for case in cases:
        if case.mock_output is None or case.mock_bad_output is None:
            problems.append(f"{case.id}: 缺 mock_output 或 mock_bad_output，评估器无法自测")
            continue

        # 1) 预置输出本身要符合 Schema —— 否则用例定义与 Schema 漂移，
        #    real 模式的失败会被误读成「模型不行」
        try:
            case.output_model.model_validate(case.mock_output)
        except ValidationError as exc:
            problems.append(f"{case.id}: mock_output 不符合 {case.output_model.__name__}: {exc.errors()[:1]}")
            continue

        # 2) 好输出必须全过
        good = evaluate(case.mock_output, case.assertions)
        good_failures = [r.detail for r in good if not r.ok]
        if good_failures:
            problems.append(f"{case.id}: 期望通过的输出被判失败 → {good_failures[0]}")

        # 3) 坏输出至少要挂一条（否则这个用例在 real 模式下没有区分度）
        bad = evaluate(case.mock_bad_output, case.assertions)
        bad_failures = [r.detail for r in bad if not r.ok]
        if not bad_failures:
            problems.append(
                f"{case.id}: 期望失败的输出全部通过（用例无区分度）—— {case.mock_bad_reason or '未写明原因'}"
            )

        results.append(
            CaseResult(
                case_id=case.id,
                title=case.title,
                agent=case.agent,
                passed=not good_failures and bool(bad_failures),
                failures=good_failures,
                notes=[f"好输出全过；坏输出挂 {len(bad_failures)} 条（用例有区分度）"],
            )
        )
    return results, problems


# ---------------------------------------------------------------- 真实模型


async def run_real(
    cases: list[EvalCase], *, provider: Any, session: Any, requirement_id: Any
) -> list[CaseResult]:
    from app.agent.runtime import AgentContext, AgentRuntime

    runtime = AgentRuntime(session, provider, max_output_attempts=2)
    results: list[CaseResult] = []

    for case in cases:
        spec = SPECS[case.agent][0]
        started = time.perf_counter()
        try:
            outcome = await runtime.run(
                spec,
                user_prompt=case.build_user_prompt(),
                context=AgentContext(requirement_id=requirement_id),
            )
        except Exception as exc:  # noqa: BLE001 - 评估要把失败当作结果记录下来
            results.append(
                CaseResult(
                    case_id=case.id,
                    title=case.title,
                    agent=case.agent,
                    passed=False,
                    failures=[],
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        payload = outcome.parsed
        checks: list[AssertionResult] = evaluate(payload, case.assertions)
        results.append(
            CaseResult(
                case_id=case.id,
                title=case.title,
                agent=case.agent,
                passed=all(r.ok for r in checks),
                failures=[r.detail for r in checks if not r.ok],
                latency_ms=outcome.latency_ms or int((time.perf_counter() - started) * 1000),
                tokens=outcome.usage.total_tokens,
                payload=payload,
            )
        )
    return results


async def seed_requirement(settings: Any) -> Any:
    """造一个真实需求供 agent_runs 挂载。

    ``agent_runs.requirement_id`` 是 NOT NULL 外键，评估也必须走真实写入路径 ——
    否则「评估通过」与「生产能不能落库」是两件事。
    """
    from app.infrastructure.db.session import get_session_factory
    from app.models.project import Project, ProjectMember
    from app.models.requirement import Requirement
    from app.models.user import User

    factory = get_session_factory()
    async with factory() as session:
        suffix = uuid4().hex[:8]
        user = User(
            email=f"eval-{suffix}@example.com",
            password_hash="x",
            display_name="Eval",
            status="ACTIVE",
        )
        session.add(user)
        await session.flush()
        project = Project(name=f"eval-{suffix}", slug=f"eval-{suffix}", owner_id=user.id, status="ACTIVE")
        session.add(project)
        await session.flush()
        session.add(ProjectMember(project_id=project.id, user_id=user.id, role="OWNER"))
        requirement = Requirement(
            project_id=project.id,
            title="evaluation fixture",
            description="评估集用的固定需求",
            status="DRAFT",
            priority="P1",
            created_by=user.id,
            version=1,
        )
        session.add(requirement)
        await session.commit()
        return requirement.id


async def prepare_real_environment(settings: Any) -> Any:
    from app.infrastructure.db.session import configure_database, create_all, create_engine

    engine = create_engine(settings)
    configure_database(engine)
    await create_all()
    return engine
