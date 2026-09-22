"""工具权限等级（设计文档 §14.1）。

Layer: Domain —— 只回答「这个等级的工具允不允许用」，不碰文件系统。

分级（文档原文语义，一字不改地映射）：

===== ========================================================================
等级  含义
===== ========================================================================
L0    读已授权上下文（需求、PRD、技术设计）—— 允许
L1    读代码 · 搜索 —— 允许，**并记录**
L2    生成 Patch —— 允许，但要过路径与规则检查
L3    运行测试和有限命令 —— 允许，**白名单**
L4    应用变更 —— **需人工审批**（对应 ``ApprovalRequiredError``，
      工作流应进入 WAITING_APPROVAL，而不是当失败处理）
L5    部署 · 删数据 · 改权限 —— 首期**禁止**
===== ========================================================================

关键判断：**L4 不是「被拒绝」，而是「需要人点头」。**
把这两者混成一个 DENIED，工作流就没法在「等审批」这个状态停下来 ——
而「人工审批」恰恰是文档流程 B 里不可省略的一步。
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

from app.common.exceptions import ApprovalRequiredError, ToolDeniedError

__all__ = [
    "ToolLevel",
    "ToolAccessDecision",
    "LEVEL_DECISIONS",
    "decision_for",
    "ensure_tool_allowed",
    "FORBIDDEN_LEVELS",
]


class ToolLevel(IntEnum):
    """权限等级。用 ``IntEnum`` 是为了让 L2 > L1 这种比较有意义。"""

    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4
    L5 = 5


class ToolAccessDecision(StrEnum):
    ALLOWED = "ALLOWED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DENIED = "DENIED"


LEVEL_DECISIONS: dict[ToolLevel, ToolAccessDecision] = {
    ToolLevel.L0: ToolAccessDecision.ALLOWED,
    ToolLevel.L1: ToolAccessDecision.ALLOWED,
    ToolLevel.L2: ToolAccessDecision.ALLOWED,
    ToolLevel.L3: ToolAccessDecision.ALLOWED,
    ToolLevel.L4: ToolAccessDecision.APPROVAL_REQUIRED,
    ToolLevel.L5: ToolAccessDecision.DENIED,
}

#: 首期**完全不允许**的等级。不是「要审批」，是根本不提供。
FORBIDDEN_LEVELS: frozenset[ToolLevel] = frozenset({ToolLevel.L5})


def decision_for(level: ToolLevel) -> ToolAccessDecision:
    return LEVEL_DECISIONS[level]


def ensure_tool_allowed(level: ToolLevel) -> ToolAccessDecision:
    """等级不允许就直接抛错。

    L4 抛 ``ApprovalRequiredError``（409），L5 抛 ``ToolDeniedError``（403）——
    两个状态码不同，因为对调用方的含义不同：前者是「去走审批流程」，
    后者是「这条路根本没有」。
    """
    decision = decision_for(level)
    if decision is ToolAccessDecision.APPROVAL_REQUIRED:
        raise ApprovalRequiredError(
            f"Tool level {level.name} requires human approval before it can run",
            details={"level": level.name},
        )
    if decision is ToolAccessDecision.DENIED:
        raise ToolDeniedError(
            f"Tool level {level.name} is not allowed in this deployment",
            details={"level": level.name, "forbidden_levels": [lv.name for lv in FORBIDDEN_LEVELS]},
        )
    return decision
