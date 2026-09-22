"""Agent 角色定义：每个角色的 Prompt 与输出契约。

Layer: Agent。

一个「角色」在这里就是三样东西：**它是谁**（``AgentRole``）、**它的指令是什么**
（system prompt）、**它的输出必须符合什么结构**（``output_model``）。
三者绑成一个 ``AgentSpec``，缺一个都不完整 —— 这是刻意设计成不可分割的：
一个没有输出契约的 Prompt，等于允许模型随便回什么都往库里写。

### 关于目录

设计文档 §10 的结构里 ``agent/`` 下面分了 ``prompts/`` 与具体角色文件。
Stage 3 只有两个角色，先平铺在一个模块里；等 Stage 4 加上
Developer / Tester / Reviewer（共 5 个）再拆目录。
提前建一堆每个只放一个字符串的包，只会让「去哪找」变难，不会让结构变清晰。

### Prompt 里的 JSON 契约是自动生成的

``output_model.model_json_schema()`` 会被拼进 system prompt，而不是手写一遍字段说明。
手写的话，某天改了 Pydantic 模型却忘了改 Prompt，模型就会按旧结构输出，
而校验用的是新结构 —— 表现是「莫名其妙地一直校验失败」。
让 Prompt 从模型生成，两边不可能走偏。
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from app.agent.outputs import ArchitectureDesign, DeveloperPatch, Prd, ReviewFindings, TestReport
from app.agent.runtime import AgentSpec
from app.domain.enums import AgentRole

__all__ = [
    "ARCHITECT_SPEC",
    "DEVELOPER_SPEC",
    "PRODUCT_SPEC",
    "REVIEWER_SPEC",
    "TESTER_SPEC",
    "build_architecture_user_prompt",
    "build_developer_user_prompt",
    "build_prd_user_prompt",
    "build_reviewer_user_prompt",
    "build_tester_user_prompt",
]

# 输出体量较大（实测 PRD 约 3400 completion tokens），给足上限
_MAX_OUTPUT_TOKENS = 4096


def _json_contract(model: type[BaseModel]) -> str:
    """把 Pydantic Schema 渲染成 Prompt 里的输出契约。

    ``ensure_ascii=False`` 让中文 description 在 Prompt 里保持可读；
    ``indent=None``（紧凑）能省下不少 prompt token —— 这份 Schema 每次调用都要发一遍。
    """
    return json.dumps(model.model_json_schema(), ensure_ascii=False, separators=(",", ":"))


_COMMON_RULES = """
输出要求（违反任何一条视为失败）：
- 只输出一个 JSON 对象，不要任何解释文字，不要 Markdown 代码块围栏
- 字段名必须与下面的 JSON Schema 完全一致，不要增删字段
- 数组字段就算为空也要输出 []
- 内容用简体中文
""".strip()


PRODUCT_SPEC = AgentSpec(
    role=AgentRole.PRODUCT,
    system_prompt=(
        "你是资深产品经理，负责把一句话需求整理成可以交付给工程团队的结构化 PRD。\n\n"
        "写作要求：\n"
        "- 验收标准必须是**可测试、可判定**的，写清接口、参数、状态码、边界条件。\n"
        "  反例：「要保证数据一致性」「用户体验良好」。\n"
        "  正例：「同一 user_id 下已存在相同 title 时，POST 返回 409 且不创建新记录」。\n"
        "- 用户故事统一用「作为<角色>，我希望<目标>，以便<价值>」句式。\n"
        "- 明确写出本期不做的事情（out_of_scope），范围蔓延是需求阶段最大的风险。\n"
        "- 需求里没写清楚的地方，不要自己编一个答案 —— 放进 open_questions。\n"
        "- 不要臆造原始需求里没有的功能。\n\n"
        f"{_COMMON_RULES}\n\n"
        f"JSON Schema：\n{_json_contract(Prd)}"
    ),
    output_model=Prd,
    temperature=0.2,
    max_tokens=_MAX_OUTPUT_TOKENS,
)


ARCHITECT_SPEC = AgentSpec(
    role=AgentRole.ARCHITECT,
    system_prompt=(
        "你是资深后端架构师，负责根据 PRD 产出可落地的技术设计。\n\n"
        "设计要求：\n"
        "- 模块划分要单一职责，模块之间通过明确接口交互；depends_on 里只能出现\n"
        "  components 里定义过的模块名。\n"
        "- key_decisions 每条都要写清「选了什么 + 为什么 + 代价是什么」。\n"
        "  只写「使用 FastAPI」不给理由，等于没做决策。\n"
        "- data_model 要写出具体的表名与关键字段/约束，不要写「一张用户表」这种空话。\n"
        "- api_endpoints 只写「METHOD /path —— 用途」，不要写实现细节。\n"
        "- risks 要写真实的取舍与不确定点，不要写「可能没时间」这类套话。\n"
        "- test_strategy 要区分哪些场景用单元测试、哪些必须用集成测试，并说明为什么。\n\n"
        f"{_COMMON_RULES}\n\n"
        f"JSON Schema：\n{_json_contract(ArchitectureDesign)}"
    ),
    output_model=ArchitectureDesign,
    temperature=0.2,
    max_tokens=_MAX_OUTPUT_TOKENS,
)


def build_prd_user_prompt(
    *, title: str, description: str, priority: str, acceptance_criteria: list[str]
) -> str:
    """把需求内容整理成 Product Agent 的用户消息。

    把原始验收标准一并带上：需求方自己写的那几条往往藏着最重要的隐含约束，
    丢掉它们会让模型凭空猜。
    """
    lines = [
        f"需求标题：{title}",
        f"优先级：{priority}",
        "",
        "需求描述：",
        description,
    ]
    if acceptance_criteria:
        lines += ["", "需求方已经写明的验收标准（必须全部覆盖）："]
        lines += [f"- {item}" for item in acceptance_criteria]
    return "\n".join(lines)


def build_architecture_user_prompt(*, prd: dict, title: str) -> str:
    """把已通过校验的 PRD 喂给 Architect Agent。

    注意这里传的是**校验通过的 PRD 结构**，不是 PRD 的原始文本 ——
    Architect 拿到的是已经规范化的字段，而不是一段需要自己重新解析的散文。
    """
    return "\n".join(
        [
            f"需求标题：{title}",
            "",
            "已确认的 PRD（JSON）：",
            json.dumps(prd, ensure_ascii=False, indent=2),
            "",
            "请据此产出技术设计。",
        ]
    )


DEVELOPER_SPEC = AgentSpec(
    role=AgentRole.DEVELOPER,
    system_prompt=(
        "你是资深后端工程师，负责按照架构设计写出具体的代码变更。\n\n"
        "写作要求：\n"
        "- changes 里的 new_content 必须是**文件的完整目标内容**，不是 diff、不是片段。\n"
        "- path 相对工作区根，不要以 / 开头；新增文件与修改文件都这样给。\n"
        "- 严格遵循架构设计的模块划分与数据模型，不要引入架构里没有的依赖。\n"
        "- 验收标准里的每一条都必须能被这次变更覆盖；覆盖不了的写进 notes。\n"
        "- 代码要完整可运行，不要写「此处省略」。注释与内容用简体中文。\n\n"
        f"{_COMMON_RULES}\n\n"
        f"JSON Schema：\n{_json_contract(DeveloperPatch)}"
    ),
    output_model=DeveloperPatch,
    temperature=0.2,
    max_tokens=_MAX_OUTPUT_TOKENS,
)

TESTER_SPEC = AgentSpec(
    role=AgentRole.TESTER,
    system_prompt=(
        "你是测试工程师，负责对照验收标准逐条核对本次变更是否达标。\n\n"
        "核对要求：\n"
        "- cases 必须覆盖 PRD 的**全部**验收标准，一条不落。\n"
        "- passed 只有在你基于变更内容能明确判定时才为 true；判定不了就给 false\n"
        "  并把原因写进 detail —— 宁可误报失败让人复核，也不放过缺陷。\n"
        "- verdict=fail 当且仅当存在 passed=false 的用例。\n"
        "- risks 里写清哪些点是无法通过静态核对验证的（例如需要真实运行才能确认的）。\n\n"
        f"{_COMMON_RULES}\n\n"
        f"JSON Schema：\n{_json_contract(TestReport)}"
    ),
    output_model=TestReport,
    temperature=0.2,
    max_tokens=_MAX_OUTPUT_TOKENS,
)

REVIEWER_SPEC = AgentSpec(
    role=AgentRole.REVIEWER,
    system_prompt=(
        "你是资深代码审查者，负责在进入人工审批前把最后一道关。\n\n"
        "审查要求：\n"
        "- verdict=needs_revision 当且仅当存在 blocker 或 major 级别的 finding；\n"
        "  minor 问题不阻塞，但要写出来。\n"
        "- blocker 定义：会导致验收标准不达标 / 明显破坏架构分层 / 安全隐患。\n"
        "- 每条 finding 都要写依据（对照架构设计或验收标准的哪一条），不许只说「不好」。\n"
        "- 变更里做得好的地方也要给 praise —— 只挑毛病的审查会让实现者回避沟通。\n\n"
        f"{_COMMON_RULES}\n\n"
        f"JSON Schema：\n{_json_contract(ReviewFindings)}"
    ),
    output_model=ReviewFindings,
    temperature=0.2,
    max_tokens=_MAX_OUTPUT_TOKENS,
)


def build_developer_user_prompt(*, prd: dict, architecture: dict, workspace_files: list[str]) -> str:
    """Developer 的输入：PRD + 架构 + 工作区现状。

    工作区文件清单由 Tool Gateway 的 ``list_files`` 产出后传进来 ——
    Agent 自己不发工具调用，编排器负责把上下文喂到它面前。
    """
    files = "\n".join(f"- {f}" for f in workspace_files) or "（工作区目前为空）"
    return "\n".join(
        [
            "已确认的 PRD（JSON）：",
            json.dumps(prd, ensure_ascii=False, indent=2),
            "",
            "已确认的架构设计（JSON）：",
            json.dumps(architecture, ensure_ascii=False, indent=2),
            "",
            "工作区现有文件：",
            files,
            "",
            "请产出本次代码变更。",
        ]
    )


def build_tester_user_prompt(*, prd: dict, architecture: dict, written_files: list[dict[str, str]]) -> str:
    """Tester 的输入里必须有**文件内容**而不只是路径。

    第一轮真实模型验证暴露过这个问题：只给文件名清单时，Tester 的结论是
    「未提供文件实际内容，无法判定」—— 一个上下文缺失造成的假阴性 fail。
    Tester 判的是代码，就必须看到代码；单文件截断上限防 Prompt 失控。
    """
    max_chars = 8000
    blocks: list[str] = []
    for item in written_files:
        content = item.get("content", "")
        truncated = ""
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = f"\n…（超出 {max_chars} 字符，已截断）"
        blocks.append(f"文件 {item.get('path', '?')}：\n```\n{content}{truncated}\n```")
    written = "\n\n".join(blocks) or "（无）"
    return "\n".join(
        [
            "已确认的 PRD（JSON）：",
            json.dumps(prd, ensure_ascii=False, indent=2),
            "",
            "架构设计（JSON）：",
            json.dumps(architecture, ensure_ascii=False, indent=2),
            "",
            "本次实际写入工作区的文件内容（这是写入时的最终内容，不是 diff）：",
            written,
            "",
            "请逐条核对验收标准并给出测试报告。",
        ]
    )


def build_reviewer_user_prompt(
    *, prd: dict, architecture: dict, patch_summary: str, diff: str, test_report: dict
) -> str:
    return "\n".join(
        [
            "已确认的 PRD（JSON）：",
            json.dumps(prd, ensure_ascii=False, indent=2),
            "",
            "架构设计（JSON）：",
            json.dumps(architecture, ensure_ascii=False, indent=2),
            "",
            "本次变更概述：",
            patch_summary,
            "",
            "变更 diff（unified diff）：",
            diff or "（无 diff）",
            "",
            "测试报告（JSON）：",
            json.dumps(test_report, ensure_ascii=False, indent=2),
            "",
            "请给出审查结论。",
        ]
    )
