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
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import AppError, NotFoundError
from app.domain.enums import ToolCallStatus
from app.domain.tool_levels import ToolAccessDecision, ToolLevel, ensure_tool_allowed
from app.infrastructure.tools.paths import WorkspacePathValidator
from app.infrastructure.tools.read_tools import READ_TOOLS, ToolDefinition, ToolRequest
from app.models.tool import ToolCall
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
    def __init__(self, session: AsyncSession, *, workspace_root: str | Path = "./workspace") -> None:
        self._session = session
        self._calls = ToolCallRepository(session)
        self._workspace = WorkspacePathValidator(workspace_root)
        self._tools: dict[str, ToolDefinition] = {t.name: t for t in READ_TOOLS}

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

        try:
            ensure_tool_allowed(definition.level)
        except AppError as exc:
            # L4 要审批 / L5 被禁 —— 两种都要留下痕迹
            status = (
                ToolCallStatus.APPROVAL_REQUIRED
                if decision_hint(exc) is ToolAccessDecision.APPROVAL_REQUIRED
                else ToolCallStatus.DENIED
            )
            await self._record(
                tool_name=tool_name,
                level=definition.level,
                status=status,
                params=params,
                ctx=ctx,
                error_code=exc.code,
                error_message=exc.message,
                latency_ms=_ms(started),
            )
            raise

        try:
            data = await definition.handler(ToolRequest(params=params, workspace=self._workspace))
        except AppError as exc:
            await self._record(
                tool_name=tool_name,
                level=definition.level,
                status=ToolCallStatus.FAILED,
                params=params,
                ctx=ctx,
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
            result=data,
            latency_ms=latency,
        )
        logger.info(
            "tool executed | tool=%s level=%s latency=%sms", tool_name, definition.level.name, latency
        )
        return data

    # ------------------------------------------------------------------ 审计

    async def _record(
        self,
        *,
        tool_name: str,
        level: ToolLevel | None,
        status: ToolCallStatus,
        params: dict[str, Any],
        ctx: ToolContext,
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
            params_json=json.dumps(params, ensure_ascii=False, default=str),
            result_summary=_summarize(result),
            error_code=error_code,
            error_message=error_message,
            latency_ms=latency_ms,
        )
        await self._calls.add(call)
        # 审计记录独立提交：外层业务回滚时，这条记录必须留下来
        await self._session.commit()


def decision_hint(exc: AppError) -> ToolAccessDecision:
    """从异常 code 反推权限决策，避免 Gateway 自己重复一遍等级→决策的映射。"""
    if exc.code == "APPROVAL_REQUIRED":
        return ToolAccessDecision.APPROVAL_REQUIRED
    return ToolAccessDecision.DENIED


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _summarize(result: dict[str, Any] | None) -> str | None:
    """审计里只留结果摘要。完整输出可能上千行，塞数据库既贵又没用。"""
    if result is None:
        return None
    text = json.dumps(result, ensure_ascii=False, default=str)
    return text[:_RESULT_SUMMARY_LIMIT]
