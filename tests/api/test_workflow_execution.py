"""工作流执行编排的接口测试。

用 MockLLMProvider 驱动完整闭环（分析 → 设计 → 实现 → 审批暂停 → 恢复 →
测试 → 审查 → 最终审批 → 完成），以及修订回路、取消与各类守卫。
真实模型的端到端单独验证 —— 这里的目标是把每条分支都确定性走到。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

from app.infrastructure.llm import MockLLMProvider
from tests.conftest import API

# ---------------------------------------------------------------- 脚本素材

GOOD_PRD = {
    "title": "实现 Todo API",
    "summary": "提供待办事项的增删改查。",
    "goals": ["支持创建与查询待办"],
    "user_stories": ["作为用户，我希望创建待办，以便记录要做的事"],
    "acceptance_criteria": ["POST /api/todos 成功返回 201"],
    "out_of_scope": [],
    "open_questions": [],
}

GOOD_ARCH = {
    "overview": "分层单体：api 层调 service，service 调 repository。",
    "components": [
        {"name": "todo_service", "responsibility": "业务规则与校验", "depends_on": ["todo_repository"]},
        {"name": "todo_repository", "responsibility": "数据读写", "depends_on": []},
    ],
    "data_model": ["todos: id, title, completed"],
    "api_endpoints": ["POST /api/todos —— 创建待办"],
    "key_decisions": ["用 SQLAlchemy 2.x —— 异步友好；代价是要注意 session 生命周期"],
    "risks": [],
    "test_strategy": ["唯一性约束用集成测试验证"],
}

DEVELOPER_PATCH_1 = {
    "summary": "新增待办接口的基础实现。",
    "changes": [
        {"path": "app/main.py", "new_content": "value = 1\n", "reason": "初始化入口"},
    ],
    "follows_architecture": True,
    "notes": [],
}

DEVELOPER_PATCH_2 = {
    "summary": "按审查意见修正实现。",
    "changes": [
        {"path": "app/main.py", "new_content": "value = 2\n", "reason": "修正取值"},
    ],
    "follows_architecture": True,
    "notes": [],
}

TEST_REPORT_PASS = {
    "verdict": "pass",
    "summary": "验收标准全部覆盖。",
    "cases": [{"name": "创建待办返回 201", "expectation": "POST 返回 201", "passed": True, "detail": ""}],
    "risks": [],
}

REVIEW_APPROVED = {
    "verdict": "approved",
    "summary": "实现与架构一致。",
    "findings": [{"severity": "praise", "file": "", "comment": "模块划分清晰"}],
}

REVIEW_NEEDS_REVISION = {
    "verdict": "needs_revision",
    "summary": "存在必须修复的问题。",
    "findings": [{"severity": "blocker", "file": "app/main.py", "comment": "与架构设计的模块划分不一致"}],
}


def _install(app: FastAPI, script: list[Any]) -> MockLLMProvider:
    """把 app 上装配好的 provider 换成按脚本响应的 Mock，并返回它以便 enqueue。"""
    provider = MockLLMProvider(script=script)
    app.state.llm_provider = provider
    return provider


async def _make_run(
    client: AsyncClient,
    owner: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    proj = (await client.post(f"{API}/projects", json={"name": "编排演示"}, headers=owner["headers"])).json()
    req = (
        await client.post(
            f"{API}/projects/{proj['id']}/requirements",
            json={"title": "实现 Todo API", "description": "增删改查"},
            headers=owner["headers"],
        )
    ).json()
    run = (
        await client.post(
            f"{API}/requirements/{req['id']}/runs",
            headers={**owner["headers"], "Idempotency-Key": "run-1"},
        )
    ).json()
    return req, run


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------- 用例


async def test_full_loop_pauses_for_patch_approval_then_completes(
    client: AsyncClient, app: FastAPI, make_actor, test_settings
) -> None:
    """完整闭环：启动 → 补丁审批暂停 → 批准 → 恢复 → 最终审批 → 完成。"""
    owner = await make_actor()
    req, run = await _make_run(client, owner)

    # 启动会消耗 3 次模型调用：PRD、架构、补丁
    provider = _install(app, [_json(GOOD_PRD), _json(GOOD_ARCH), _json(DEVELOPER_PATCH_1)])

    started = await client.post(f"{API}/runs/{run['id']}/start", headers=owner["headers"])
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["paused"] is True
    assert body["pause_reason"] == "tool_approval"
    approval_id = body["approval_id"]
    assert approval_id

    assert body["run"]["status"] == "IMPLEMENTING"
    assert body["run"]["current_step"] == "TOOL_GATEWAY"

    # 需求走完了前两步
    req_detail = (await client.get(f"{API}/requirements/{req['id']}", headers=owner["headers"])).json()
    assert req_detail["status"] == "DESIGNED"

    # PATCH 交付物已落库（PRD / ARCHITECTURE / PATCH）
    arts = (await client.get(f"{API}/runs/{run['id']}/artifacts", headers=owner["headers"])).json()
    assert arts["total"] == 3
    assert [a["type"] for a in arts["items"]] == ["PRD", "ARCHITECTURE", "PATCH"]

    # 补丁审批由 Gateway 自动创建，挂在需求上
    approvals = (
        await client.get(f"{API}/requirements/{req['id']}/approvals?status=PENDING", headers=owner["headers"])
    ).json()
    assert approvals["total"] == 1

    # OWNER 批准 → 恢复（消耗 tester + reviewer 两次调用）→ 停在最终审批
    approved = await client.post(
        f"{API}/approvals/{approval_id}/approve", json={"note": "改动可控"}, headers=owner["headers"]
    )
    assert approved.status_code == 200, approved.text

    provider.enqueue(_json(TEST_REPORT_PASS))
    provider.enqueue(_json(REVIEW_APPROVED))

    resumed = await client.post(
        f"{API}/runs/{run['id']}/resume",
        json={"approval_id": approval_id},
        headers=owner["headers"],
    )
    assert resumed.status_code == 200, resumed.text
    final = resumed.json()
    assert final["paused"] is True
    assert final["pause_reason"] == "final_approval"
    assert final["approval_id"] is None
    assert final["run"]["status"] == "WAITING_APPROVAL"
    assert final["run"]["current_step"] == "APPROVAL"

    # 写盘真的发生了
    workspace_file = Path(test_settings.workspace_root) / f"req-{req['id']}" / "app/main.py"
    assert workspace_file.read_text(encoding="utf-8") == "value = 1\n"

    # 交付物齐了：PRD / ARCHITECTURE / PATCH / TEST_REPORT / REVIEW
    arts = (await client.get(f"{API}/runs/{run['id']}/artifacts", headers=owner["headers"])).json()
    assert arts["total"] == 5

    # 测试交付物里必须带**真实执行结果**（而不只是模型的结论）：
    # 工作区里没有测试文件，所以真实结论是「没有收集到测试」——
    # 这正是要留痕的东西：模型说通过 ≠ pytest 真的跑过
    report = next(a for a in arts["items"] if a["type"] == "TEST_REPORT")
    execution = report["content"].get("execution")
    assert execution is not None, "TEST_REPORT 交付物缺少真实执行结果"
    assert execution["no_tests_collected"] is True
    assert execution["exit_code"] == 5

    # ⚠️ 回归守卫：执行结果必须**进 Tester 的 Prompt**，不只是落进交付物。
    # 这里曾断过一根线：编排器调 build_tester_user_prompt 时漏传 execution=，
    # 于是 pytest 明明跑了、Prompt 里却是「本次没有真实执行测试」——
    # Tester 如实照做判 fail，凭空多出一轮返工（真实容器环境发生过）。
    # 交付物断言（上面两行）与 Prompt 断言必须同时存在，二者盖的是不同的线。
    tester_call = provider.calls[3]  # 0=PRD 1=架构 2=Developer 3=Tester 4=Reviewer
    tester_prompt = tester_call.messages[-1].content
    assert "没有收集到任何测试" in tester_prompt, (
        "Tester 的 Prompt 里没有真实执行结果 —— 编排器漏传 execution= 了"
    )
    assert "本次没有真实执行测试" not in tester_prompt

    # 最终批准 → COMPLETED
    done = await client.post(f"{API}/runs/{run['id']}/approve", headers=owner["headers"])
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "COMPLETED"
    assert done.json()["current_step"] == "DONE"

    req_detail = (await client.get(f"{API}/requirements/{req['id']}", headers=owner["headers"])).json()
    assert req_detail["status"] == "COMPLETED"

    # 终态之后再 start / approve 都该被拒
    again = await client.post(f"{API}/runs/{run['id']}/start", headers=owner["headers"])
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "WORKFLOW_ALREADY_STARTED"


async def test_reviewer_revision_loop_reruns_developer(client: AsyncClient, app: FastAPI, make_actor) -> None:
    """审查要求返工：REVIEWING → REVISION_REQUIRED → 重新实现 → 新审批。"""
    owner = await make_actor()
    req, run = await _make_run(client, owner)

    provider = _install(
        app,
        [
            _json(GOOD_PRD),
            _json(GOOD_ARCH),
            _json(DEVELOPER_PATCH_1),
            # 第一次 resume：tester 通过、reviewer 打回后，主循环**同一请求内**
            # 就回到实现阶段 —— developer 的第二次输出也在这次 resume 里消耗
            _json(TEST_REPORT_PASS),
            _json(REVIEW_NEEDS_REVISION),
            _json(DEVELOPER_PATCH_2),
        ],
    )

    started = await client.post(f"{API}/runs/{run['id']}/start", headers=owner["headers"])
    approval_1 = started.json()["approval_id"]
    await client.post(f"{API}/approvals/{approval_1}/approve", json={}, headers=owner["headers"])
    resumed = await client.post(
        f"{API}/runs/{run['id']}/resume",
        json={"approval_id": approval_1},
        headers=owner["headers"],
    )
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["run"]["status"] == "IMPLEMENTING"
    assert body["run"]["current_step"] == "TOOL_GATEWAY"
    assert body["paused"] is True
    approval_2 = body["approval_id"]
    assert approval_2 and approval_2 != approval_1  # 返工产生的是**新**审批

    # PATCH 交付物到 v2 了 —— 旧版本不覆盖
    arts = (await client.get(f"{API}/runs/{run['id']}/artifacts", headers=owner["headers"])).json()
    patch_versions = sorted(a["version"] for a in arts["items"] if a["type"] == "PATCH")
    assert patch_versions == [1, 2]

    # 批准新审批 → 恢复 → 通过 → 最终批准
    await client.post(f"{API}/approvals/{approval_2}/approve", json={}, headers=owner["headers"])
    provider.enqueue(_json(TEST_REPORT_PASS))
    provider.enqueue(_json(REVIEW_APPROVED))
    resumed2 = await client.post(
        f"{API}/runs/{run['id']}/resume",
        json={"approval_id": approval_2},
        headers=owner["headers"],
    )
    assert resumed2.json()["pause_reason"] == "final_approval"

    done = await client.post(f"{API}/runs/{run['id']}/approve", headers=owner["headers"])
    assert done.json()["status"] == "COMPLETED"


async def test_resume_without_approval_id_is_rejected(client: AsyncClient, app: FastAPI, make_actor) -> None:
    """补丁审批暂停点上，不带 approval_id 的 resume 必须被明确拒绝。"""
    owner = await make_actor()
    _req, run = await _make_run(client, owner)
    _install(app, [_json(GOOD_PRD), _json(GOOD_ARCH), _json(DEVELOPER_PATCH_1)])

    started = await client.post(f"{API}/runs/{run['id']}/start", headers=owner["headers"])
    assert started.json()["paused"] is True

    resumed = await client.post(f"{API}/runs/{run['id']}/resume", json={}, headers=owner["headers"])
    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "APPROVAL_ID_REQUIRED"


async def test_final_approval_requires_owner(
    client: AsyncClient, app: FastAPI, make_actor, make_project_with_member
) -> None:
    """Developer 可以启动工作流，但不能做最终批准 —— 不能自己批自己。"""
    owner, proj, developer = await make_project_with_member(role="DEVELOPER")
    req = (
        await client.post(
            f"{API}/projects/{proj['id']}/requirements",
            json={"title": "T", "description": "D"},
            headers=owner["headers"],
        )
    ).json()
    run = (
        await client.post(
            f"{API}/requirements/{req['id']}/runs",
            headers={**owner["headers"], "Idempotency-Key": "run-owner-only"},
        )
    ).json()

    _install(app, [_json(GOOD_PRD), _json(GOOD_ARCH), _json(DEVELOPER_PATCH_1)])
    started = await client.post(f"{API}/runs/{run['id']}/start", headers=developer["headers"])
    assert started.status_code == 200
    approval_id = started.json()["approval_id"]

    await client.post(f"{API}/approvals/{approval_id}/approve", json={}, headers=owner["headers"])
    # reviewer/tester 的调用由 developer 触发也行 —— 但为省脚本，直接由 owner resume
    app.state.llm_provider.enqueue(_json(TEST_REPORT_PASS))
    app.state.llm_provider.enqueue(_json(REVIEW_APPROVED))
    resumed = await client.post(
        f"{API}/runs/{run['id']}/resume",
        json={"approval_id": approval_id},
        headers=developer["headers"],
    )
    assert resumed.json()["paused"] is True

    denied = await client.post(f"{API}/runs/{run['id']}/approve", headers=developer["headers"])
    assert denied.status_code == 403

    done = await client.post(f"{API}/runs/{run['id']}/approve", headers=owner["headers"])
    assert done.status_code == 200
    assert done.json()["status"] == "COMPLETED"


async def test_cancel_from_paused_state(client: AsyncClient, app: FastAPI, make_actor) -> None:
    owner = await make_actor()
    req, run = await _make_run(client, owner)
    _install(app, [_json(GOOD_PRD), _json(GOOD_ARCH), _json(DEVELOPER_PATCH_1)])

    started = await client.post(f"{API}/runs/{run['id']}/start", headers=owner["headers"])
    assert started.json()["paused"] is True

    cancelled = await client.post(f"{API}/runs/{run['id']}/cancel", headers=owner["headers"])
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["finished_at"] is not None

    # 取消后不能再 resume
    resumed = await client.post(f"{API}/runs/{run['id']}/resume", json={}, headers=owner["headers"])
    assert resumed.status_code == 409


async def test_deliverables_summary_aggregates_latest_versions(
    client: AsyncClient, app: FastAPI, make_actor
) -> None:
    """Stage 4 验收项「用户可以查看完整交付物」：每种类型取最新版 + 工作区文件。"""
    owner = await make_actor()
    req, run = await _make_run(client, owner)

    provider = _install(
        app,
        [
            _json(GOOD_PRD),
            _json(GOOD_ARCH),
            _json(DEVELOPER_PATCH_1),
            _json(TEST_REPORT_PASS),
            _json(REVIEW_APPROVED),
        ],
    )

    started = await client.post(f"{API}/runs/{run['id']}/start", headers=owner["headers"])
    approval_id = started.json()["approval_id"]
    await client.post(f"{API}/approvals/{approval_id}/approve", json={}, headers=owner["headers"])
    provider.enqueue(_json(TEST_REPORT_PASS))
    provider.enqueue(_json(REVIEW_APPROVED))
    await client.post(
        f"{API}/runs/{run['id']}/resume",
        json={"approval_id": approval_id},
        headers=owner["headers"],
    )
    await client.post(f"{API}/runs/{run['id']}/approve", headers=owner["headers"])

    summary = await client.get(f"{API}/requirements/{req['id']}/deliverables", headers=owner["headers"])
    assert summary.status_code == 200, summary.text
    body = summary.json()

    assert body["requirement"]["status"] == "COMPLETED"
    assert body["latest_run"]["status"] == "COMPLETED"
    # 每种类型只有最新一条：5 类型 = 5 条
    assert [d["type"] for d in body["deliverables"]] == [
        "PRD",
        "ARCHITECTURE",
        "PATCH",
        "TEST_REPORT",
        "REVIEW",
    ]
    # 工作区文件清单反映的是实际写入
    assert body["workspace_files"] == ["app/main.py"]


async def test_deliverables_summary_without_any_run(
    client: AsyncClient, app: FastAPI, make_actor, make_project, make_requirement
) -> None:
    """刚建的需求也有汇总：空列表 + latest_run 为 null，而不是 404。"""
    owner = await make_actor()
    proj = await make_project(owner["headers"])
    req = await make_requirement(proj["id"], owner["headers"])

    summary = await client.get(f"{API}/requirements/{req['id']}/deliverables", headers=owner["headers"])
    assert summary.status_code == 200
    body = summary.json()
    assert body["deliverables"] == []
    assert body["latest_run"] is None
    assert body["workspace_files"] == []
