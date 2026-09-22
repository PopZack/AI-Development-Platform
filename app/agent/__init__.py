"""Agent 层（横向层）。

设计文档 §5.1 的定位：``agent/`` 装 Agent 角色定义、Prompt 与统一运行时。
它横跨各层 —— 用 Provider（Infrastructure）调模型，用 Repository 落库，
但**不碰 HTTP**（那是 API 层的事），也**不自己判断权限**（那是 Service 的事）。

目录规划：

- ``runtime.py``  已实现：统一的「调模型 → 校验 → 落库」流程
- ``prompts/``    Stage 3 后续：每个角色的 Prompt 模板
- ``agents/``     Stage 3 后续：Product / Architect 等具体角色

现在只有 runtime 一个文件，所以先平铺；角色多起来再分目录，
不要为了「看起来完整」提前建一堆空包。
"""

from app.agent.runtime import AgentContext, AgentOutcome, AgentRuntime, AgentSpec

__all__ = ["AgentContext", "AgentOutcome", "AgentRuntime", "AgentSpec"]
