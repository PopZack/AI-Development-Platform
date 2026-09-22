"""工作流接口测试：创建、幂等键、权限。

幂等键是这个提交的核心。「相同幂等键不会重复创建工作流」是设计文档 §11
Stage 2 的四条验收之一。
"""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient

from tests.conftest import API


def _with_key(headers: dict[str, str], key: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key}


# ------------------------------------------------------------------ 创建


async def test_create_run_stops_at_created_pending(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """只创建不执行：状态停在 CREATED，current_step 是 PENDING，started_at 仍为 null。"""
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])

    response = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(actor["headers"], "requirement-001-v1"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["requirement_id"] == requirement["id"]
    assert body["status"] == "CREATED"
    assert body["current_step"] == "PENDING"
    assert body["idempotency_key"] == "requirement-001-v1"
    assert body["started_at"] is None
    assert body["finished_at"] is None
    assert response.headers.get("Idempotent-Replay") is None


async def test_same_key_twice_returns_the_same_run(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """文档 §11 Stage 2 验收：相同幂等键不会重复创建工作流。

    第二次不是 201 而是 200，并且带 ``Idempotent-Replay`` 头 —— 什么都没创建，
    返回 201 是在骗调用方。只靠状态码区分也不够：日志里看不出发生过重放。
    """
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])
    key = "requirement-001-v1"

    first = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(actor["headers"], key),
    )
    second = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(actor["headers"], key),
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["created_at"] == first.json()["created_at"]


async def test_different_keys_create_different_runs(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """换一个键就是一次新的运行 —— 幂等键要能区分「重试」和「再跑一次」。"""
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])

    first = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(actor["headers"], "run-1"),
    )
    second = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(actor["headers"], "run-2"),
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


async def test_same_key_on_another_requirement_returns_409(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """幂等键全局唯一。跨需求复用是客户端 bug，必须报出来而不是悄悄新建。"""
    actor = await make_actor()
    project = await make_project(actor["headers"])
    first_req = await make_requirement(project["id"], actor["headers"], title="需求一")
    second_req = await make_requirement(project["id"], actor["headers"], title="需求二")
    key = "shared-key"

    await client.post(f"{API}/requirements/{first_req['id']}/runs", headers=_with_key(actor["headers"], key))

    response = await client.post(
        f"{API}/requirements/{second_req['id']}/runs", headers=_with_key(actor["headers"], key)
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert body["error"]["details"]["used_by_requirement_id"] == first_req["id"]


async def test_replay_still_succeeds_after_the_requirement_is_closed(
    client: AsyncClient,
    make_actor: object,
    make_project: object,
    make_requirement: object,
    set_requirement_status: object,
) -> None:
    """幂等检查排在状态校验之前，所以「重试一次成功过的调用」永远拿到当初的结果。

    如果顺序反了，客户端会因为「需求中途被关掉」而收到 409 —— 一次重试变得不可幂等，
    这比一开始就不幂等更难排查。
    """
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])
    key = "close-after-create"

    first = await client.post(
        f"{API}/requirements/{requirement['id']}/runs", headers=_with_key(actor["headers"], key)
    )
    assert first.status_code == 201

    await set_requirement_status(requirement["id"], "CANCELLED")

    replay = await client.post(
        f"{API}/requirements/{requirement['id']}/runs", headers=_with_key(actor["headers"], key)
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]

    # 但换一个新键就该被状态规则挡住
    fresh = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(actor["headers"], "brand-new-key"),
    )
    assert fresh.status_code == 409
    assert fresh.json()["error"]["code"] == "REQUIREMENT_NOT_RUNNABLE"


# ------------------------------------------------------------------ 请求校验


async def test_missing_idempotency_key_returns_validation_error(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """没有幂等键就不给建 —— 否则「双击启动」会安静地产生两条工作流。"""
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])

    response = await client.post(f"{API}/requirements/{requirement['id']}/runs", headers=actor["headers"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_invalid_idempotency_key_characters_are_rejected(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """幂等键会落库、进日志、出现在排查结论里，所以限制字符集。

    用 ASCII 非法字符构造 —— HTTP 头本身塞不进非 ASCII（httpx 会在客户端就拒掉），
    那测的是 httpx 而不是我们的校验。
    """
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])

    response = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(actor["headers"], "has space and $pecial!"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ------------------------------------------------------------------ 权限


async def test_create_run_requires_authentication(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    owner = await make_actor()
    project = await make_project(owner["headers"])
    requirement = await make_requirement(project["id"], owner["headers"])

    response = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers={"Idempotency-Key": "no-auth"},
    )

    assert response.status_code == 401


async def test_unknown_requirement_returns_404(client: AsyncClient, make_actor: object) -> None:
    actor = await make_actor()

    response = await client.post(
        f"{API}/requirements/{uuid4()}/runs",
        headers=_with_key(actor["headers"], "unknown-req"),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REQUIREMENT_NOT_FOUND"


async def test_non_member_cannot_create_run(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    owner = await make_actor()
    project = await make_project(owner["headers"])
    requirement = await make_requirement(project["id"], owner["headers"])
    outsider = await make_actor()

    response = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(outsider["headers"], "outsider-key"),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"


async def test_viewer_cannot_create_run(
    client: AsyncClient, make_project_with_member: object, make_requirement: object
) -> None:
    """VIEWER 只读：连工作流都不能起。"""
    owner, project, viewer = await make_project_with_member(role="VIEWER")
    requirement = await make_requirement(project["id"], owner["headers"])

    response = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(viewer["headers"], "viewer-key"),
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "PROJECT_ROLE_REQUIRED"
    assert body["error"]["details"]["actual_role"] == "VIEWER"


# ------------------------------------------------------------------ 查询


async def test_get_run_by_member(
    client: AsyncClient, make_project_with_member: object, make_requirement: object
) -> None:
    owner, project, developer = await make_project_with_member(role="DEVELOPER")
    requirement = await make_requirement(project["id"], owner["headers"])
    created = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(developer["headers"], "dev-key"),
    )

    response = await client.get(f"{API}/runs/{created.json()['id']}", headers=developer["headers"])

    assert response.status_code == 200
    assert response.json()["id"] == created.json()["id"]


async def test_get_run_hides_it_from_non_members(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    owner = await make_actor()
    project = await make_project(owner["headers"])
    requirement = await make_requirement(project["id"], owner["headers"])
    created = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(owner["headers"], "owner-key"),
    )

    outsider = await make_actor()
    response = await client.get(f"{API}/runs/{created.json()['id']}", headers=outsider["headers"])

    assert response.status_code == 403


async def test_unknown_run_returns_404(client: AsyncClient, make_actor: object) -> None:
    actor = await make_actor()

    response = await client.get(f"{API}/runs/{uuid4()}", headers=actor["headers"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WORKFLOW_RUN_NOT_FOUND"


async def test_get_run_requires_authentication(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    owner = await make_actor()
    project = await make_project(owner["headers"])
    requirement = await make_requirement(project["id"], owner["headers"])
    created = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(owner["headers"], "auth-key"),
    )

    assert (await client.get(f"{API}/runs/{created.json()['id']}")).status_code == 401
