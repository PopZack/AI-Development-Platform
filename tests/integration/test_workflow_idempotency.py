"""幂等键的集成测试：验证真正兜底的是数据库约束，而不是应用层的查重。

「先查再插」在任何并发系统里都不成立 —— 两个请求可以同时读到「不存在」，
然后同时插入。能拦住它们只有 ``idempotency_key`` 上的 UNIQUE 约束。
这个文件专门验证那条路径。
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workflow import WorkflowRun
from app.repositories.workflow_repository import WorkflowRunRepository
from tests.conftest import API


def _with_key(headers: dict[str, str], key: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key}


async def test_replay_creates_exactly_one_row(
    client: AsyncClient,
    db_session: AsyncSession,
    make_actor: object,
    make_project: object,
    make_requirement: object,
) -> None:
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])
    key = "only-once"

    for _ in range(3):
        response = await client.post(
            f"{API}/requirements/{requirement['id']}/runs",
            headers=_with_key(actor["headers"], key),
        )
        assert response.status_code in (200, 201)

    total = (await db_session.execute(select(func.count()).select_from(WorkflowRun))).scalar_one()
    assert total == 1


async def test_unique_constraint_resolves_the_race(
    client: AsyncClient,
    db_session: AsyncSession,
    make_actor: object,
    make_project: object,
    make_requirement: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟竞态：查重这一步看不见已存在的行，于是插入撞上唯一约束。

    做法是把「按幂等键查」的**第一次**调用伪装成查不到（等价于并发下读到了旧快照），
    第二次恢复真实行为。这样就能确定性地走到 ``except IntegrityError`` 那条分支 ——
    否则它在真实环境里只会在压力测试中偶发出现，等于没有覆盖。
    """
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])
    key = "race-key"

    first = await client.post(
        f"{API}/requirements/{requirement['id']}/runs", headers=_with_key(actor["headers"], key)
    )
    assert first.status_code == 201

    original = WorkflowRunRepository.get_by_idempotency_key
    calls = {"n": 0}

    async def blind_first_call(self: WorkflowRunRepository, idempotency_key: str) -> WorkflowRun | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # 假装没查到 —— 竞态下就是这个效果
        return await original(self, idempotency_key)

    monkeypatch.setattr(WorkflowRunRepository, "get_by_idempotency_key", blind_first_call)

    replay = await client.post(
        f"{API}/requirements/{requirement['id']}/runs", headers=_with_key(actor["headers"], key)
    )

    # 关键：唯一约束把这次竞态兜住了，返回的是重放而不是 500
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["id"] == first.json()["id"]
    assert calls["n"] == 2, "应该先是查不到的假象，再是真查"

    total = (await db_session.execute(select(func.count()).select_from(WorkflowRun))).scalar_one()
    assert total == 1


async def test_database_rejects_duplicate_key_behind_the_service(
    client: AsyncClient,
    db_session: AsyncSession,
    make_actor: object,
    make_project: object,
    make_requirement: object,
) -> None:
    """绕过 Service 直接插重复幂等键，数据库必须挡住。

    Service 里的查重是「友好提示」，唯一约束才是「正确性保证」。
    """
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])
    created = await client.post(
        f"{API}/requirements/{requirement['id']}/runs",
        headers=_with_key(actor["headers"], "taken-key"),
    )
    assert created.status_code == 201

    db_session.add(
        WorkflowRun(
            requirement_id=UUID(requirement["id"]),
            status="CREATED",
            current_step="PENDING",
            idempotency_key="taken-key",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
