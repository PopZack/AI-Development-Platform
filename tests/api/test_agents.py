"""Agent 接口测试：analyze / plan / artifacts。

走完整的状态机流转，用 MockLLMProvider 驱动 ——
「模型输出不合法」这条分支只有 Mock 能稳定复现。
"""

from __future__ import annotations

import json
from typing import Any

from httpx import AsyncClient

from app.infrastructure.llm import LLMTimeoutError, MockLLMProvider
from tests.conftest import API

GOOD_PRD = {
    "title": "实现 Todo API",
    "summary": "提供待办事项的增删改查。",
    "goals": ["支持创建与查询待办"],
    "user_stories": ["作为用户，我希望创建待办，以便记录任务。"],
    "acceptance_criteria": ["POST /api/todos 成功时返回 201"],
    "out_of_scope": ["本期不做鉴权"],
    "open_questions": [],
}

GOOD_ARCHITECTURE = {
    "overview": "分层单体：Router → Service → Domain → Repository。",
    "components": [{"name": "todo_service", "responsibility": "待办业务规则", "depends_on": []}],
    "data_model": ["todos: id / user_id / title / completed / created_at"],
    "api_endpoints": ["POST /api/todos —— 创建待办"],
    "key_decisions": ["选择 SQLAlchemy 2.x async —— 与现有栈一致，代价是学习成本"],
    "risks": ["并发写同一用户时需要唯一约束兜底"],
    "test_strategy": ["Service 层用集成测试（真实 SQLite），因为关键行为在 SQL 层"],
}


def _install(client: AsyncClient, app: Any, script: list[Any]) -> MockLLMProvider:
    """把 app 上装配好的 provider 换成按脚本响应的 Mock。

    Provider 装在 ``app.state`` 上正是为了这一步：不用改环境变量、不用猴补丁。
    """
    provider = MockLLMProvider(script=script)
    app.state.llm_provider = provider
    return provider


async def _analyze(client: AsyncClient, headers: dict[str, str], requirement_id: str, content: dict) -> Any:
    return await client.post(
        f"{API}/requirements/{requirement_id}/analyze",
        json=content,
        headers=headers,
    )


async def _setup_requirement(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])
    return actor, project, requirement


# ------------------------------------------------------------------ analyze


async def test_analyze_generates_prd_and_updates_requirement(
    client: AsyncClient, app: Any, make_actor: object, make_project: object, make_requirement: object
) -> None:
    actor, _, requirement = await _setup_requirement(client, make_actor, make_project, make_requirement)
    _install(client, app, [json.dumps(GOOD_PRD, ensure_ascii=False)])

    response = await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=actor["headers"])

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["type"] == "PRD"
    assert body["version"] == 1
    assert body["content"] == GOOD_PRD
    assert body["agent_run_id"] is not None

    # 状态推进到 ANALYZING，且 prd_json 被填充（可在需求详情里读到）
    detail = (await client.get(f"{API}/requirements/{requirement['id']}", headers=actor["headers"])).json()
    assert detail["status"] == "ANALYZING"
    assert detail["prd"] == GOOD_PRD


async def test_analyze_twice_from_analyzing_is_rejected(
    client: AsyncClient, app: Any, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """ANALYZING → ANALYZING 不是合法迁移：并发重复触发的第二次应该被拒掉。"""
    actor, _, requirement = await _setup_requirement(client, make_actor, make_project, make_requirement)
    _install(client, app, [json.dumps(GOOD_PRD, ensure_ascii=False)])

    first = await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=actor["headers"])
    second = await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=actor["headers"])

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "REQUIREMENT_STATUS_CONFLICT"


# --------------------------------------------------------------------- plan


async def test_plan_requires_analyze_first(
    client: AsyncClient, app: Any, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """没做过 analyze 就不能 plan —— 这是两个接口之间的顺序约束。"""
    actor, _, requirement = await _setup_requirement(client, make_actor, make_project, make_requirement)
    _install(client, app, [json.dumps(GOOD_ARCHITECTURE, ensure_ascii=False)])

    response = await client.post(f"{API}/requirements/{requirement['id']}/plan", headers=actor["headers"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REQUIREMENT_STATUS_CONFLICT"


async def test_plan_after_analyze_produces_architecture(
    client: AsyncClient, app: Any, make_actor: object, make_project: object, make_requirement: object
) -> None:
    actor, _, requirement = await _setup_requirement(client, make_actor, make_project, make_requirement)
    _install(
        client,
        app,
        [json.dumps(GOOD_PRD, ensure_ascii=False), json.dumps(GOOD_ARCHITECTURE, ensure_ascii=False)],
    )

    await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=actor["headers"])
    response = await client.post(f"{API}/requirements/{requirement['id']}/plan", headers=actor["headers"])

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["type"] == "ARCHITECTURE"
    assert body["version"] == 1
    assert body["content"] == GOOD_ARCHITECTURE

    detail = (await client.get(f"{API}/requirements/{requirement['id']}", headers=actor["headers"])).json()
    assert detail["status"] == "DESIGNED"


async def test_reanalyze_from_designed_starts_a_new_version(
    client: AsyncClient, app: Any, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """需求改过之后要能重跑分析，并且 PRD 要变成新版本而不是覆盖旧的。"""
    actor, _, requirement = await _setup_requirement(client, make_actor, make_project, make_requirement)
    updated_prd = {**GOOD_PRD, "summary": "改过一版的概述"}
    _install(
        client,
        app,
        [
            json.dumps(GOOD_PRD, ensure_ascii=False),
            json.dumps(GOOD_ARCHITECTURE, ensure_ascii=False),
            json.dumps(updated_prd, ensure_ascii=False),
        ],
    )

    await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=actor["headers"])
    await client.post(f"{API}/requirements/{requirement['id']}/plan", headers=actor["headers"])
    again = await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=actor["headers"])

    assert again.status_code == 201
    assert again.json()["version"] == 2
    assert again.json()["content"] == updated_prd

    # 两条历史都要在
    listing = (
        await client.get(
            f"{API}/requirements/{requirement['id']}/artifacts",
            params={"type": "PRD"},
            headers=actor["headers"],
        )
    ).json()
    assert listing["total"] == 2
    assert [item["version"] for item in listing["items"]] == [1, 2]


# ------------------------------------------------------------ 失败路径与状态


async def test_invalid_model_output_fails_and_is_retryable(
    client: AsyncClient, app: Any, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """两次都输出非法内容 → 502 + 需求变 FAILED；FAILED 可以重新 analyze。"""
    actor, _, requirement = await _setup_requirement(client, make_actor, make_project, make_requirement)
    provider = _install(client, app, ["不是 JSON", "还是不是 JSON"])

    response = await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=actor["headers"])

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "AGENT_OUTPUT_INVALID"
    assert len(provider.calls) == 2

    detail = (await client.get(f"{API}/requirements/{requirement['id']}", headers=actor["headers"])).json()
    assert detail["status"] == "FAILED"

    # FAILED → ANALYZING 是合法迁移，所以失败后能重试
    provider.enqueue(json.dumps(GOOD_PRD, ensure_ascii=False))
    retry = await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=actor["headers"])
    assert retry.status_code == 201


async def test_transport_failure_marks_requirement_failed(
    client: AsyncClient, app: Any, make_actor: object, make_project: object, make_requirement: object
) -> None:
    actor, _, requirement = await _setup_requirement(client, make_actor, make_project, make_requirement)
    _install(client, app, [LLMTimeoutError("模型超时")])

    response = await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=actor["headers"])

    assert response.status_code == 504
    detail = (await client.get(f"{API}/requirements/{requirement['id']}", headers=actor["headers"])).json()
    assert detail["status"] == "FAILED"


# ------------------------------------------------------------------ 权限


async def test_analyze_requires_authentication(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    owner, _, requirement = await _setup_requirement(client, make_actor, make_project, make_requirement)

    response = await client.post(f"{API}/requirements/{requirement['id']}/analyze")

    assert response.status_code == 401
    assert owner is not None  # 只是为了让 lint 别报未使用


async def test_viewer_cannot_trigger_but_can_read_artifacts(
    client: AsyncClient, app: Any, make_project_with_member: object, make_requirement: object
) -> None:
    """VIEWER 只读：能看到交付物，但不能花 token 触发 Agent。"""
    owner, project, viewer = await make_project_with_member(role="VIEWER")
    requirement = await make_requirement(project["id"], owner["headers"])
    _install(client, app, [json.dumps(GOOD_PRD, ensure_ascii=False)])

    denied = await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=viewer["headers"])
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PROJECT_ROLE_REQUIRED"

    allowed = await client.get(f"{API}/requirements/{requirement['id']}/artifacts", headers=viewer["headers"])
    assert allowed.status_code == 200
    assert allowed.json()["total"] == 0


async def test_non_member_cannot_trigger_or_read(
    client: AsyncClient, app: Any, make_actor: object, make_project: object, make_requirement: object
) -> None:
    owner, _, requirement = await _setup_requirement(client, make_actor, make_project, make_requirement)
    outsider = await make_actor()
    _install(client, app, [json.dumps(GOOD_PRD, ensure_ascii=False)])

    assert (
        await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=outsider["headers"])
    ).status_code == 403
    assert (
        await client.get(f"{API}/requirements/{requirement['id']}/artifacts", headers=outsider["headers"])
    ).status_code == 403


async def test_unknown_requirement_returns_404(client: AsyncClient, app: Any, make_actor: object) -> None:
    from uuid import uuid4

    actor = await make_actor()
    _install(client, app, [])

    response = await client.post(f"{API}/requirements/{uuid4()}/analyze", headers=actor["headers"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REQUIREMENT_NOT_FOUND"


# ------------------------------------------------------------------ 列表接口


async def test_artifacts_can_be_filtered_by_type(
    client: AsyncClient, app: Any, make_actor: object, make_project: object, make_requirement: object
) -> None:
    actor, _, requirement = await _setup_requirement(client, make_actor, make_project, make_requirement)
    _install(
        client,
        app,
        [json.dumps(GOOD_PRD, ensure_ascii=False), json.dumps(GOOD_ARCHITECTURE, ensure_ascii=False)],
    )

    await client.post(f"{API}/requirements/{requirement['id']}/analyze", headers=actor["headers"])
    await client.post(f"{API}/requirements/{requirement['id']}/plan", headers=actor["headers"])

    all_items = (
        await client.get(f"{API}/requirements/{requirement['id']}/artifacts", headers=actor["headers"])
    ).json()
    only_arch = (
        await client.get(
            f"{API}/requirements/{requirement['id']}/artifacts",
            params={"type": "ARCHITECTURE"},
            headers=actor["headers"],
        )
    ).json()

    assert all_items["total"] == 2
    assert only_arch["total"] == 1
    assert only_arch["items"][0]["type"] == "ARCHITECTURE"
