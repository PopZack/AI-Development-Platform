"""Tool Gateway：Agent 调工具的唯一入口。

Layer: Infrastructure。

**为什么必须是唯一入口**：文档 §14.1 的权限分级、§14.1 的路径校验、
「L1 允许并记录」—— 这三条只有在所有调用都过同一道门时才成立。
任何一个 Agent 自己直接 ``open()`` 文件，这三条就同时失效，
而且从代码上完全看不出来（这正是最危险的地方）。

流程：

    查工具 → 权限等级判断（L4 要审批 / L5 拒绝）→ 执行 → 落 tool_calls 审计
                                        ↑
                        成功、失败、被拒、要审批 —— 四种结局都记录

审计记录在 Gateway 自己的事务里提交，不跟外层业务事务走：
外层回滚时，审计要留下来 —— 出问题的时候你恰恰需要知道它做过什么。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import AppError, ApprovalRequiredError, NotFoundError, ToolDeniedError
from app.domain.approval import effective_status
from app.domain.enums import ApprovalStatus, ToolCallStatus
from app.domain.tool_levels import ToolAccessDecision, ToolLevel, decision_for
from app.infrastructure.events import Event, get_event_bus
from app.infrastructure.tools.exec_tools import EXEC_TOOLS
from app.infrastructure.tools.patch_tools import PATCH_TOOLS
from app.infrastructure.tools.paths import WorkspacePathValidator
from app.infrastructure.tools.read_tools import READ_TOOLS, ToolDefinition, ToolRequest
from app.models.approval import Approval
from app.models.tool import ToolCall
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.tool_repository import ToolCallRepository

__all__ = ["ToolContext", "ToolGateway"]

logger = logging.getLogger(__name__)

_RESULT_SUMMARY_LIMIT = 500


@dataclass(frozen=True)
class ToolContext:
    """这次调用挂在哪个业务对象上，用于审计关联。允许全部为空（手工调试）。"""

    requirement_id: UUID | None = None
    workflow_run_id: UUID | None = None
    agent_run_id: UUID | None = None


class ToolGateway:
    def __init__(
        self,
        session: AsyncSession,
        *,
        workspace_root: str | Path = "./workspace",
        requested_by: UUID | None = None,
    ) -> None:
        self._session = session
        self._calls = ToolCallRepository(session)
        self._approvals = ApprovalRepository(session)
        self._workspace = WorkspacePathValidator(workspace_root)
        # 触发工具的用户：L4 工具被拦下时，审批记录要能追溯到「是谁要做的」
        self._requested_by = requested_by
        self._tools: dict[str, ToolDefinition] = {t.name: t for t in (*READ_TOOLS, *PATCH_TOOLS, *EXEC_TOOLS)}

    # ------------------------------------------------------------------ 注册

    def register(self, definition: ToolDefinition) -> None:
        """注册一个新工具。

        同名覆盖是允许的（测试里换实现用），但会打日志 ——
        生产上同名覆盖几乎总是失误。
        """
        if definition.name in self._tools:
            logger.warning("tool re-registered, overwriting existing definition | tool=%s", definition.name)
        self._tools[definition.name] = definition

    @property
    def workspace(self) -> WorkspacePathValidator:
        return self._workspace

    def describe_tools(self) -> list[dict[str, Any]]:
        """工具清单 + 参数说明，给 Prompt 拼「你能用哪些工具」用。"""
        return [
            {
                "name": t.name,
                "level": t.level.name,
                "description": t.description,
                "parameters": t.parameter_schema,
            }
            for t in sorted(self._tools.values(), key=lambda x: (x.level, x.name))
        ]

    # ------------------------------------------------------------------ 执行

    async def execute(
        self, tool_name: str, params: dict[str, Any] | None, *, context: ToolContext | None = None
    ) -> dict[str, Any]:
        """执行工具并返回输出（可直接进 Prompt）。

        任何失败都**先落审计再抛出**，且抛出的类型不变 ——
        上层拿到的异常语义和直接调用时一致，只是多了一条审计记录。
        """
        ctx = context or ToolContext()
        params = params or {}
        started = time.perf_counter()

        definition = self._tools.get(tool_name)
        if definition is None:
            await self._record(
                tool_name=tool_name,
                level=None,
                status=ToolCallStatus.DENIED,
                params=params,
                ctx=ctx,
                error_code="TOOL_UNKNOWN",
                error_message=f"Unknown tool: {tool_name}",
                latency_ms=_ms(started),
            )
            raise NotFoundError(f"Unknown tool: {tool_name}", code="TOOL_UNKNOWN")

        # ── 权限判断 ──────────────────────────────────────────────
        # L4 的协议是「两段式」：
        #   第一次调用不带 approval_id → 自动发起审批，抛 APPROVAL_REQUIRED
        #   OWNER 批准后再带上 approval_id 重试 → 校验通过，真正执行
        # 这样「人点头」就不是一句空话：没有批准过的调用永远到不了 handler。
        if decision_for(definition.level) is ToolAccessDecision.APPROVAL_REQUIRED:
            if ctx.requirement_id is None:
                # 审批必须挂在某个需求上（approvals.requirement_id 非空），
                # 更重要的是：脱离需求的「批准」没有意义 —— 批的是
                # 「改这份需求的工作区」，不是「随便改点什么」。
                # 与其让审批变成一条没有归属的空记录，不如在这里明确拒绝。
                await self._record(
                    tool_name=tool_name,
                    level=definition.level,
                    status=ToolCallStatus.DENIED,
                    params=params,
                    ctx=ctx,
                    error_code="APPROVAL_CONTEXT_REQUIRED",
                    error_message="L4 tools must be executed within a requirement context",
                    latency_ms=_ms(started),
                )
                raise ToolDeniedError(
                    "L4 tools must be executed within a requirement context",
                    code="APPROVAL_CONTEXT_REQUIRED",
                    details={"tool_name": tool_name, "level": definition.level.name},
                )

            approval_id = self._extract_approval_id(params)
            if approval_id is None:
                # 第一次调用：发起审批
                approval = await self._request_approval(definition, params, ctx)
                await self._record(
                    tool_name=tool_name,
                    level=definition.level,
                    status=ToolCallStatus.APPROVAL_REQUIRED,
                    params=params,
                    ctx=ctx,
                    error_code="APPROVAL_REQUIRED",
                    error_message=f"Tool {tool_name} requires human approval",
                    latency_ms=_ms(started),
                )
                get_event_bus().emit(
                    Event(
                        type="approval.created",
                        requirement_id=ctx.requirement_id,
                        payload={
                            "approval_id": str(approval.id),
                            "tool_name": tool_name,
                            "level": definition.level.name,
                            "run_id": str(ctx.workflow_run_id) if ctx.workflow_run_id else None,
                        },
                    )
                )
                raise ApprovalRequiredError(
                    f"Tool {tool_name} requires human approval",
                    details={
                        "approval_id": str(approval.id),
                        "tool_name": tool_name,
                        "level": definition.level.name,
                    },
                )

            # 第二次调用：校验审批
            approval = await self._ensure_approval_usable(approval_id, definition=definition, ctx=ctx)

        if decision_for(definition.level) is ToolAccessDecision.DENIED:
            await self._record(
                tool_name=tool_name,
                level=definition.level,
                status=ToolCallStatus.DENIED,
                params=params,
                ctx=ctx,
                error_code="TOOL_LEVEL_FORBIDDEN",
                error_message=f"Tool level {definition.level.name} is not allowed in this deployment",
                latency_ms=_ms(started),
            )
            raise ToolDeniedError(
                f"Tool level {definition.level.name} is not allowed in this deployment",
                details={"level": definition.level.name},
            )

        try:
            data = await definition.handler(ToolRequest(params=params, workspace=self._workspace))
        except AppError as exc:
            await self._record(
                tool_name=tool_name,
                level=definition.level,
                status=ToolCallStatus.FAILED,
                params=params,
                ctx=ctx,
                approval_id=approval_id
                if decision_for(definition.level) is ToolAccessDecision.APPROVAL_REQUIRED
                else None,
                error_code=exc.code,
                error_message=exc.message,
                latency_ms=_ms(started),
            )
            raise

        latency = _ms(started)
        await self._record(
            tool_name=tool_name,
            level=definition.level,
            status=ToolCallStatus.SUCCEEDED,
            params=params,
            ctx=ctx,
            approval_id=approval_id
            if decision_for(definition.level) is ToolAccessDecision.APPROVAL_REQUIRED
            else None,
            result=data,
            latency_ms=latency,
        )
        logger.info(
            "tool executed | tool=%s level=%s latency=%sms", tool_name, definition.level.name, latency
        )
        return data

    # ------------------------------------------------------------------ 审批

    @staticmethod
    def _extract_approval_id(params: dict[str, Any]) -> UUID | None:
        """从参数里取审批 id。带了但格式不对就拒绝 —— 别让它悄悄变成 None。"""
        raw = params.get("approval_id")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        try:
            return UUID(str(raw))
        except ValueError as exc:
            raise ToolDeniedError(
                "approval_id is not a valid UUID",
                code="APPROVAL_ID_INVALID",
            ) from exc

    async def _ensure_approval_usable(
        self, approval_id: UUID, *, definition: ToolDefinition, ctx: ToolContext
    ) -> Any:
        """审批要能用，必须同时满足四件事。

        缺任何一件都拒绝，且错误码分开 —— 调用方要能分辨
        「没批」「批的是别的工具」「批的是别的需求」「已经用过」。
        """
        approval = await self._approvals.get(approval_id)
        if approval is None:
            raise NotFoundError(f"Approval {approval_id} does not exist", code="APPROVAL_NOT_FOUND")

        if approval.tool_name != definition.name:
            raise ToolDeniedError(
                "This approval was issued for a different tool",
                code="APPROVAL_TOOL_MISMATCH",
                details={"approval_tool": approval.tool_name, "requested_tool": definition.name},
            )

        if approval.requirement_id != ctx.requirement_id:
            raise ToolDeniedError(
                "This approval belongs to a different requirement",
                code="APPROVAL_REQUIREMENT_MISMATCH",
            )

        # 惰性过期：先把真实状态落库，再判断能不能用
        actual = effective_status(ApprovalStatus(approval.status), approval.expires_at)
        if actual is not ApprovalStatus(approval.status):
            approval.status = actual.value
            await self._session.commit()
        if actual is not ApprovalStatus.APPROVED:
            raise ApprovalRequiredError(
                f"Approval {approval_id} has not been approved yet (status: {actual})",
                code="APPROVAL_NOT_APPROVED",
                details={"approval_id": str(approval_id), "current_status": str(actual)},
            )

        # 重放检查：一条审批只换一次成功执行。没有这条，批准一次就能反复写盘
        if await self._calls.has_succeeded_with_approval(approval_id):
            raise ToolDeniedError(
                "This approval has already been used by a successful execution",
                code="APPROVAL_ALREADY_USED",
                details={"approval_id": str(approval_id)},
            )

        return approval

    # ------------------------------------------------------------------ 审批

    async def _request_approval(
        self, definition: ToolDefinition, params: dict[str, Any], ctx: ToolContext
    ) -> Approval:
        """L4 工具被拦下时自动发起一条审批。

        让 Gateway 而不是调用方来建这条记录，理由很直接：
        **系统知道有个 L4 工具被拦了**，人不需要替系统记这件事。
        如果要靠谁手动 POST /approvals，那这一步早晚会忘。

        ``reason`` 刻意写清「是什么工具、要改什么」，审批人点开列表
        第一眼就能判断该不该批。
        """
        from app.domain.approval import DEFAULT_APPROVAL_TTL_HOURS

        expires_at = datetime.now(UTC) + timedelta(hours=DEFAULT_APPROVAL_TTL_HOURS)
        approval = Approval(
            requirement_id=ctx.requirement_id,
            workflow_run_id=ctx.workflow_run_id,
            tool_name=definition.name,
            status="PENDING",
            requested_by=self._requested_by,
            reason=(
                f"Agent 请求执行 L4 工具「{definition.name}」：{definition.description}。"
                f"参数：{json.dumps(params, ensure_ascii=False, default=str)[:300]}"
            ),
            expires_at=expires_at,
        )
        await self._approvals.add(approval)
        await self._session.commit()
        logger.info(
            "approval requested | tool=%s approval=%s requirement=%s",
            definition.name,
            approval.id,
            ctx.requirement_id,
        )
        return approval

    # ------------------------------------------------------------------ 审计

    async def _record(
        self,
        *,
        tool_name: str,
        level: ToolLevel | None,
        status: ToolCallStatus,
        params: dict[str, Any],
        ctx: ToolContext,
        approval_id: UUID | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        result: dict[str, Any] | None = None,
        latency_ms: int = 0,
    ) -> None:
        call = ToolCall(
            requirement_id=ctx.requirement_id,
            workflow_run_id=ctx.workflow_run_id,
            agent_run_id=ctx.agent_run_id,
            tool_name=tool_name,
            level=level.name if level else "UNKNOWN",
            status=status.value,
            approval_id=approval_id,
            params_json=json.dumps(params, ensure_ascii=False, default=str),
            result_summary=_summarize(result),
            error_code=error_code,
            error_message=error_message,
            latency_ms=latency_ms,
        )
        await self._calls.add(call)
        # 审计记录独立提交：外层业务回滚时，这条记录必须留下来
        await self._session.commit()


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _summarize(result: dict[str, Any] | None) -> str | None:
    """审计里只留结果摘要。完整输出可能上千行，塞数据库既贵又没用。"""
    if result is None:
        return None
    text = json.dumps(result, ensure_ascii=False, default=str)
    return text[:_RESULT_SUMMARY_LIMIT]
