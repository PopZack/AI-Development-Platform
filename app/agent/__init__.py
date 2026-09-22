"""Agent 层（横向层）。

设计文档 §5.1 的定位：``agent/`` 装 Agent 角色定义、Prompt 与统一运行时。
它横跨各层 —— 用 Provider（Infrastructure）调模型，用 Repository 落库，
但**不碰 HTTP**（那是 API 层的事），也**不自己判断权限**（那是 Service 的事）。

- ``runtime.py``   统一的「调模型 → 校验 → 落库」流程
- ``outputs.py``   各角色的结构化输出契约（Pydantic 模型）
- ``roles.py``     Product / Architect 的 Prompt 与输出契约绑定

设计文档 §10 把 ``prompts/`` 与具体角色分成单独文件；Stage 3 只有两个角色，
先平铺。等 Stage 4 加到 5 个角色（Developer / Tester / Reviewer）再拆目录 ——
提前建一堆每个只放一个字符串的包，只会让「去哪找」变难。
"""

from app.agent.outputs import ArchitectureComponent, ArchitectureDesign, Prd
from app.agent.roles import (
    ARCHITECT_SPEC,
    PRODUCT_SPEC,
    build_architecture_user_prompt,
    build_prd_user_prompt,
)
from app.agent.runtime import AgentContext, AgentOutcome, AgentRuntime, AgentSpec

__all__ = [
    "AgentContext",
    "AgentOutcome",
    "AgentRuntime",
    "AgentSpec",
    "ArchitectureComponent",
    "ArchitectureDesign",
    "Prd",
    "ARCHITECT_SPEC",
    "PRODUCT_SPEC",
    "build_architecture_user_prompt",
    "build_prd_user_prompt",
]
