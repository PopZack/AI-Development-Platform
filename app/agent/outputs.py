"""Agent 的结构化输出契约。

Layer: Agent。

这些 Pydantic 模型同时扮演三个角色，这也是它们值得单独放一个文件的原因：

1. **是给模型的接口说明** —— ``model_json_schema()`` 会被拼进 system prompt，
   模型照着它输出。字段的 ``description`` 因此不是文档，是 Prompt 的一部分。
2. **是校验规则** —— Runtime 拿它 ``model_validate``，不通过就不落库（文档 §14.3）。
3. **是交付物的结构定义** —— 通过校验的内容直接进 ``artifacts.content_json``。

三条约束尺度上的取舍：

- **必填就必填**：``acceptance_criteria`` 要求至少一条。没有验收标准的 PRD 对
  Stage 4 的 Tester Agent 毫无用处 —— 与其让它通过校验后一路空转到下游，
  不如当场判为不合格、把原因反馈给模型重试。
- **用嵌套模型而不是 ``list[str]``**：``ArchitectureDesign.components`` 是有结构的
  对象列表。扁平字符串看起来更宽容，但下游要用它生成代码/文件时就抓不到字段了。
- **``min_length`` 与业务含义对齐**：``summary`` 至少要有一个字符，
  空的「概述」等于没写。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "ArchitectureComponent",
    "ArchitectureDesign",
    "DeveloperPatch",
    "FileChange",
    "Prd",
    "ReviewFindings",
    "TestCaseResult",
    "TestReport",
]


class Prd(BaseModel):
    """Product Agent 的产出（文档 §3.2 流程 B 第一步）。"""

    title: str = Field(min_length=1, description="需求标题，与原始需求保持一致或更精确")
    summary: str = Field(min_length=1, description="一段话概述这份需求要解决什么问题")
    goals: list[str] = Field(default_factory=list, description="本次要达成的目标，每条一句话，可判定")
    user_stories: list[str] = Field(
        default_factory=list,
        description="用户故事，统一用「作为<角色>，我希望<目标>，以便<价值>」句式",
    )
    acceptance_criteria: list[str] = Field(
        min_length=1,
        description=(
            "验收标准。每条必须可测试、可判定，包含具体的接口/参数/状态码/边界条件，"
            "禁止「要保证数据一致性」这类无法验证的表述"
        ),
    )
    out_of_scope: list[str] = Field(default_factory=list, description="明确写出本期不做的事情，避免范围蔓延")
    open_questions: list[str] = Field(
        default_factory=list, description="需要向需求方确认的疑问；没有就留空数组"
    )


class ArchitectureComponent(BaseModel):
    """架构里的一个模块。"""

    name: str = Field(min_length=1, description="模块名，例如 auth_service")
    responsibility: str = Field(min_length=1, description="这个模块负责什么，一句话")
    depends_on: list[str] = Field(
        default_factory=list, description="依赖的其他模块名，必须是 components 里出现过的名字"
    )


class ArchitectureDesign(BaseModel):
    """Architect Agent 的产出（文档 §3.2 流程 B 第二步）。"""

    overview: str = Field(min_length=1, description="技术方案概述，说明整体思路与选型")
    components: list[ArchitectureComponent] = Field(
        min_length=1, description="模块划分。每个模块单一职责，模块之间通过明确接口交互"
    )
    data_model: list[str] = Field(
        default_factory=list,
        description="数据模型改动，每条说明表名与关键字段/约束（新增或修改都要写）",
    )
    api_endpoints: list[str] = Field(
        default_factory=list,
        description="接口清单，格式为「METHOD /path —— 用途」，不要写实现细节",
    )
    key_decisions: list[str] = Field(
        default_factory=list,
        description="关键技术决策，每条要写出「选了什么 + 为什么 + 代价是什么」",
    )
    risks: list[str] = Field(default_factory=list, description="风险与不确定点，以及打算怎么应对")
    test_strategy: list[str] = Field(
        min_length=1, description="测试策略：需要覆盖哪些场景，哪些用单测、哪些用集成测试"
    )


class FileChange(BaseModel):
    """一个文件的目标内容。

    用「最终长什么样」而不是 diff：Agent 生成目标状态比生成变更过程更不容易出错，
    应用时也不用解析 diff 格式（解析 diff 的边界情况多得离谱）。
    """

    path: str = Field(min_length=1, description="相对工作区根的文件路径，不要以 / 开头")
    new_content: str = Field(description="这个文件的完整目标内容（不是增量 diff）")
    reason: str = Field(min_length=1, description="为什么改这个文件，一句话")


class DeveloperPatch(BaseModel):
    """Developer Agent 的产出（文档 §3.2 流程 B 第三步）。"""

    summary: str = Field(min_length=1, description="这次变更做了什么，一段话")
    changes: list[FileChange] = Field(
        min_length=1,
        max_length=20,
        description="文件变更清单。每个文件一条；新增文件也要给出完整内容",
    )
    follows_architecture: bool = Field(description="变更是否严格遵循架构设计的模块划分")
    notes: list[str] = Field(default_factory=list, description="需要评审者特别注意的点")


class TestCaseResult(BaseModel):
    """一条测试用例的执行/推演结论。"""

    name: str = Field(min_length=1, description="用例名，例如「重复标题返回 409」")
    expectation: str = Field(min_length=1, description="期望行为（来自验收标准）")
    passed: bool = Field(description="该用例是否通过")
    detail: str = Field(default="", description="失败时的具体观察；通过时可留空")


class TestReport(BaseModel):
    """Tester Agent 的产出（文档 §12.6）。"""

    verdict: str = Field(pattern="^(pass|fail)$", description="总体结论：pass 或 fail")
    summary: str = Field(min_length=1, description="一段话总结测试结论")
    cases: list[TestCaseResult] = Field(
        min_length=1, description="逐条验收标准对应的用例结果，必须覆盖全部验收标准"
    )
    risks: list[str] = Field(default_factory=list, description="未覆盖/无法验证的点")


class ReviewFinding(BaseModel):
    """Reviewer 发现的一个问题（或一条肯定）。"""

    severity: str = Field(pattern="^(blocker|major|minor|praise)$", description="问题严重级别")
    file: str = Field(default="", description="相关文件；不针对具体文件可留空")
    comment: str = Field(min_length=1, description="问题描述 / 肯定理由，写清依据")


class ReviewFindings(BaseModel):
    """Reviewer Agent 的产出（文档 §12.7）。"""

    verdict: str = Field(
        pattern="^(approved|needs_revision)$",
        description="approved=可以进入人工审批；needs_revision=必须回到实现阶段修改",
    )
    summary: str = Field(min_length=1, description="一段话总结审查结论")
    findings: list[ReviewFinding] = Field(
        min_length=1, description="逐条审查意见；全都没问题也至少给一条 praise"
    )
