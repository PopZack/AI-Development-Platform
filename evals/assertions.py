"""评估断言引擎。

Layer: 评估工具（不进生产镜像，见 .dockerignore）。

### 为什么评估集要单独做一套断言

「模型输出得好不好」很容易变成主观打分，而主观打分**无法回归**：
改了 Prompt 之后你没法说清是变好还是变坏。所以这里的断言都是**可判定**的：

- 结构类：验收标准至少 3 条、`out_of_scope` 非空
- 内容类：每条验收标准必须含可验证要素（HTTP 状态码 / 路径 / 函数名 / "返回"）
- 反例类：禁止「要保证数据一致性」这类无法验证的空话
- **缺陷检出类**：给一段有缺陷的实现，审查必须判 needs_revision 且提到缺陷关键词

### 最容易做错的地方：只有好样本

如果用例里全是「好需求 → 好输出」，那么一个永远说"看起来没问题"的模型也能拿满分。
所以评估集必须**成对出现**：同一个场景给「正确实现」与「有缺陷实现」，
期望前者通过、后者被打回。**这才是评估集真正的信息量所在。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Assertion", "AssertionResult", "evaluate", "SUPPORTED_TYPES"]

# 反例：这些说法无法验证，「验收标准」里出现就说明这条标准是废话
# 严重意见的最短长度：低于它就说不清问题（中文按字计，见断言实现里的说明）
MIN_FINDING_CHARS = 10

UNVERIFIABLE_PATTERNS = [
    r"要保证.{0,6}(一致性|正确性|稳定性|质量)",
    r"用户体验(良好|流畅|友好)",
    r"性能(良好|优秀|足够)",
    r"尽量",
    r"等等$",
]
# 正例：可验证要素。验收标准至少要命中一个，否则它没法判
VERIFIABLE_HINTS = ["返回", "HTTP", "状态码", "接口", "/", "函数", "字段", "报错", "抛", "`"]

SUPPORTED_TYPES = frozenset(
    {
        "min_items",
        "not_empty",
        "items_contain_any",
        "items_match_none",
        "expected_value",
        "findings_contain_any",
        "findings_substantiated",
        "list_items_match_none",
    }
)


@dataclass(frozen=True)
class Assertion:
    type: str
    path: str = ""
    value: Any = None
    any: list[str] = field(default_factory=list)
    none: list[str] = field(default_factory=list)
    message: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Assertion:
        unknown = set(raw) - {"type", "path", "value", "any", "none", "message"}
        if unknown:
            raise ValueError(f"断言里有未知字段（拼错了吗）: {sorted(unknown)}")
        if raw.get("type") not in SUPPORTED_TYPES:
            raise ValueError(f"不支持的断言类型: {raw.get('type')}")
        return cls(
            type=raw["type"],
            path=raw.get("path", ""),
            value=raw.get("value"),
            any=list(raw.get("any", [])),
            none=list(raw.get("none", [])),
            message=raw.get("message", ""),
        )


@dataclass(frozen=True)
class AssertionResult:
    ok: bool
    detail: str


def _get(payload: Any, path: str) -> Any:
    """按 ``a.b`` 取值；取不到返回 None（断言自己会判它不合规）。"""
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def evaluate(payload: Any, assertions: list[Assertion]) -> list[AssertionResult]:
    """对一份 Agent 输出跑一组断言。"""
    results: list[AssertionResult] = []

    for assertion in assertions:
        target = _get(payload, assertion.path) if assertion.path else payload
        prefix = f"{assertion.message or assertion.type}（{assertion.path}）"

        if assertion.type == "min_items":
            count = len(target) if isinstance(target, list) else 0
            results.append(
                AssertionResult(
                    count >= int(assertion.value),
                    f"{prefix}: {count} 条，要求 ≥ {assertion.value}",
                )
            )

        elif assertion.type == "not_empty":
            ok = bool(target) and (not isinstance(target, (list, str)) or len(target) > 0)
            results.append(AssertionResult(ok, f"{prefix}: {'非空' if ok else '为空'}"))

        elif assertion.type == "items_contain_any":
            if not isinstance(target, list) or not target:
                results.append(AssertionResult(False, f"{prefix}: 不是非空列表"))
                continue
            hits, misses = [], []
            for index, item in enumerate(target):
                (hits if _contains_any(str(item), assertion.any) else misses).append(index)
            results.append(
                AssertionResult(
                    not misses,
                    f"{prefix}: {len(hits)}/{len(target)} 条含可验证要素"
                    + (
                        f"，第 {[i + 1 for i in misses]} 条不含（例：{target[misses[0]][:60]}）"
                        if misses
                        else ""
                    ),
                )
            )

        elif assertion.type == "items_match_none":
            # 反例断言：列表里不能出现匹配这些正则的项（如不可验证的验收标准）
            if not isinstance(target, list) or not target:
                results.append(AssertionResult(False, f"{prefix}: 不是非空列表"))
                continue
            offenders = [
                (index, str(item))
                for index, item in enumerate(target)
                if any(re.search(pattern, str(item)) for pattern in assertion.none)
            ]
            results.append(
                AssertionResult(
                    not offenders,
                    f"{prefix}: "
                    + ("无不可验证表述" if not offenders else f"出现空话：{offenders[0][1][:60]}"),
                )
            )

        elif assertion.type == "list_items_match_none":
            text = " ".join(str(item) for item in (target or []))
            results.append(AssertionResult(bool(text.strip()), f"{prefix}: 有内容可校验"))

        elif assertion.type == "expected_value":
            ok = target == assertion.value
            results.append(AssertionResult(ok, f"{prefix}: 实际 {target!r}，期望 {assertion.value!r}"))

        elif assertion.type == "findings_substantiated":
            # 「审查意见必须有依据」—— 这是对照组真正该测的东西。
            # 刻意**不**断言 verdict == approved：严格的审查者总能找出合理的改进点
            # （实测三轮，每轮意见都成立），断言 approved 只会奖励宽松、不奖励严谨。
            # 这里要求：只要打了回，每条 blocker/major 都要指出具体文件与具体问题。
            findings = target if isinstance(target, list) else []
            vague: list[str] = []
            for item in findings:
                if not isinstance(item, dict):
                    continue
                if item.get("severity") not in {"blocker", "major"}:
                    continue
                comment = str(item.get("comment") or "").strip()
                # 阈值 10 个字（不是 20）：中文信息密度高，「缺少唯一性校验，重复标题会写进去」
                # 只有 18 个字但意思完整。按英文的字符数定阈值会把正常的意见判成空话。
                if len(comment) < MIN_FINDING_CHARS or not str(item.get("file") or "").strip():
                    vague.append(f"[{item.get('severity')}] {comment[:60] or '（空意见）'}")
            results.append(
                AssertionResult(
                    not vague,
                    f"{prefix}: "
                    + ("每条严重意见都有文件与具体说明" if not vague else f"存在无依据的意见：{vague[0]}"),
                )
            )

        elif assertion.type == "findings_contain_any":
            findings = target if isinstance(target, list) else []
            text = " ".join(
                str(item.get("comment", item)) if isinstance(item, dict) else str(item) for item in findings
            )
            ok = _contains_any(text, assertion.any)
            results.append(
                AssertionResult(
                    ok,
                    f"{prefix}: "
                    + ("提到了缺陷关键词" if ok else f"未提到 {assertion.any}；实际意见：{text[:120]}"),
                )
            )

    return results
