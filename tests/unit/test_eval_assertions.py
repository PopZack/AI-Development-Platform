"""评估集自身的测试。

评估器如果坏了（断言路径写错、用例失去区分度），real 模式给出的低分会被误读成
「模型不行」。所以评估集必须和别的代码一样被测 —— 这些用例跑在 CI 里，
不调模型、毫秒级。

对应关系：``scripts/eval_agents.py --provider mock`` 是手动跑的版本，
这里把它固化成了 pytest 用例。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.assertions import Assertion, evaluate
from evals.runner import load_cases, run_mock

CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "agent_cases.json"


# ---------------------------------------------------------------- 评估集完整性


def test_every_case_has_discrimination() -> None:
    """好输出必须全过、坏输出必须挂至少一条 —— 否则这个用例量不出任何东西。"""
    cases = load_cases(CASES_PATH)
    assert cases, "评估集是空的"

    _results, problems = run_mock(cases)
    assert not problems, "评估集自身有问题：\n" + "\n".join(problems)


def test_cases_cover_all_three_agents() -> None:
    """三个 Agent 都要有用例 —— 只测 Reviewer 的话，Product / Tester 出了问题看不见。"""
    agents = {case.agent for case in load_cases(CASES_PATH)}
    assert agents == {"product", "reviewer", "tester"}


def test_defect_cases_exist() -> None:
    """必须有用例专门验证「缺陷检出」——只测好样本等于奖励「永远说没问题」。"""
    kinds = [case.kind for case in load_cases(CASES_PATH)]
    assert kinds.count("defect_detection") >= 2


def test_mock_outputs_match_the_output_schema() -> None:
    """用例里预置的输出必须符合 Pydantic 契约。

    否则用例定义与 Schema 漂移，real 模式的失败会被误读成「模型不行」，
    而真正的原因是评估集写错了。
    """
    for case in load_cases(CASES_PATH):
        case.output_model.model_validate(case.mock_output)
        case.output_model.model_validate(case.mock_bad_output)


# ---------------------------------------------------------------- 断言引擎


def test_min_items_counts_correctly() -> None:
    assertion = Assertion(type="min_items", path="items", value=2)

    assert evaluate({"items": ["a", "b"]}, [assertion])[0].ok
    assert not evaluate({"items": ["a"]}, [assertion])[0].ok
    assert not evaluate({}, [assertion])[0].ok  # 路径取不到也算不通过


def test_items_match_none_catches_empty_talk() -> None:
    """反例断言必须能抓住「要保证数据一致性」这类无法验证的表述。"""
    assertion = Assertion(type="items_match_none", path="criteria", none=["要保证.{0,6}一致性"])

    assert evaluate({"criteria": ["POST /api/x 返回 201"]}, [assertion])[0].ok
    assert not evaluate({"criteria": ["要保证数据一致性"]}, [assertion])[0].ok


def test_findings_substantiated_requires_file_and_detail() -> None:
    """对照组的核心断言：打回可以，但必须说清是哪个文件的什么问题。"""
    assertion = Assertion(type="findings_substantiated", path="findings")

    good = {
        "findings": [
            {"severity": "blocker", "file": "app/x.py", "comment": "缺少唯一性校验，重复标题会写进去"}
        ]
    }
    assert evaluate(good, [assertion])[0].ok

    # 无文件、意见只有两个字 —— 典型的「无依据打回」
    vague = {"findings": [{"severity": "blocker", "file": "", "comment": "不够好"}]}
    assert not evaluate(vague, [assertion])[0].ok

    # minor 不受约束：小问题意见简短可以接受，不该因此判失败
    minor_only = {"findings": [{"severity": "minor", "file": "", "comment": "建议补注释"}]}
    assert evaluate(minor_only, [assertion])[0].ok


def test_expected_value_reports_both_sides() -> None:
    assertion = Assertion(type="expected_value", path="verdict", value="approved")
    result = evaluate({"verdict": "needs_revision"}, [assertion])[0]

    assert not result.ok
    # 失败信息要能自解释：不能只说「不通过」，要写出实际值
    assert "needs_revision" in result.detail and "approved" in result.detail


def test_unknown_assertion_type_is_rejected() -> None:
    """拼错断言类型必须当场报错，而不是静默跳过（静默跳过=用例白写）。"""
    with pytest.raises(ValueError, match="不支持的断言类型"):
        Assertion.from_dict({"type": "min_itemz", "path": "a"})


def test_unknown_assertion_field_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知字段"):
        Assertion.from_dict({"type": "not_empty", "path": "a", "valeu": 3})
