"""审批接口（资源树 `/approvals/*`）。

Layer: Presentation（Router）。

审批记录由 **Tool Gateway 在 L4 工具被拦下时自动创建**，
没有「手动发起审批」的接口 —— 系统知道有个工具被拦了，人不需要替系统记这件事。

谁能批：**只有项目 OWNER**。Developer 可以触发 Agent（也就可能触发 L4 工具），
但不能批自己触发的请求 —— 职责分离，否则审批形同虚设。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.common.dependencies import ApprovalServiceDep, CurrentUserDep
from app.common.openapi import AUTH_ERROR_RESPONSES
from app.domain.enums import ApprovalStatus
from app.schemas.approval import ApprovalDecision, ApprovalList, ApprovalRead

router = APIRouter(tags=["approvals"])


def _to_read(approval) -> ApprovalRead:
    return ApprovalRead(
        id=approval.id,
        requirement_id=approval.requirement_id,
        tool_call_id=approval.tool_call_id,
        tool_name=approval.tool_name,
        status=ApprovalStatus(approval.status),
        requested_by=approval.requested_by,
        reviewed_by=approval.reviewed_by,
        reason=approval.reason,
        review_note=approval.review_note,
        expires_at=approval.expires_at,
        created_at=approval.created_at,
        updated_at=approval.updated_at,
    )


@router.get(
    "/requirements/{requirement_id}/approvals",
    response_model=ApprovalList,
    summary="列出某需求的审批记录",
    description="按创建时间倒序。`status` 可选：`PENDING` / `APPROVED` / `REJECTED` / `EXPIRED`。",
    responses=AUTH_ERROR_RESPONSES,
)
async def list_approvals(
    requirement_id: UUID,
    service: ApprovalServiceDep,
    current_user: CurrentUserDep,
    status: Annotated[ApprovalStatus | None, Query(description="按状态过滤")] = None,
) -> ApprovalList:
    items, total = await service.list_for_requirement(requirement_id, actor=current_user, status=status)
    return ApprovalList(items=[_to_read(a) for a in items], total=total)


@router.get(
    "/approvals/{approval_id}",
    response_model=ApprovalRead,
    summary="查询审批详情",
    responses=AUTH_ERROR_RESPONSES,
)
async def get_approval(
    approval_id: UUID, service: ApprovalServiceDep, current_user: CurrentUserDep
) -> ApprovalRead:
    approval = await service.get(approval_id, actor=current_user)
    return _to_read(approval)


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=ApprovalRead,
    summary="批准（仅项目 OWNER）",
    description=(
        "只有 OWNER 能批 —— Developer 不能批自己触发的请求。\n\n"
        "已过期（`EXPIRED`）或已决定的审批会返回 `409 APPROVAL_NOT_PENDING`。"
    ),
    responses=AUTH_ERROR_RESPONSES,
)
async def approve(
    approval_id: UUID,
    body: ApprovalDecision,
    service: ApprovalServiceDep,
    current_user: CurrentUserDep,
) -> ApprovalRead:
    approval = await service.approve(approval_id, actor=current_user, note=body.note)
    return _to_read(approval)


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=ApprovalRead,
    summary="驳回（仅项目 OWNER）",
    description="驳回后 Agent 可以调整方案重新发起；记录会原样保留，不做物理删除。",
    responses=AUTH_ERROR_RESPONSES,
)
async def reject(
    approval_id: UUID,
    body: ApprovalDecision,
    service: ApprovalServiceDep,
    current_user: CurrentUserDep,
) -> ApprovalRead:
    approval = await service.reject(approval_id, actor=current_user, note=body.note)
    return _to_read(approval)
