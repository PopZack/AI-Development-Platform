"""Agent 角色构建器的单元测试。

重点不是「Prompt 长什么样」，而是**该出现的上下文必须真的出现**：
Tester 缺文件内容的教训来自真实模型验证 —— 它会输出「无法验证」的假阴性。
"""

from __future__ import annotations

from app.agent.roles import build_developer_user_prompt, build_tester_user_prompt

_PRD = {"title": "T", "acceptance_criteria": ["POST 返回 201"]}
_ARCH = {"overview": "分层"}


def test_tester_prompt_includes_written_file_contents() -> None:
    """Tester 判的是代码，Prompt 里必须有内容，只有路径会判出假阴性。"""
    prompt = build_tester_user_prompt(
        prd=_PRD,
        architecture=_ARCH,
        written_files=[{"path": "app/main.py", "content": "value = 1\n"}],
    )

    assert "app/main.py" in prompt
    assert "value = 1" in prompt
    assert "最终内容" in prompt  # 明确告诉模型这是写入后的完整内容


def test_tester_prompt_truncates_oversized_files() -> None:
    """单文件内容超上限要截断并明示 —— 防止 Prompt 被一个大文件撑爆。"""
    prompt = build_tester_user_prompt(
        prd=_PRD,
        architecture=_ARCH,
        written_files=[{"path": "big.py", "content": "x" * 20_000}],
    )

    assert prompt.count("x") < 20_000
    assert "已截断" in prompt


def test_developer_prompt_carries_prd_architecture_and_workspace() -> None:
    prompt = build_developer_user_prompt(prd=_PRD, architecture=_ARCH, workspace_files=["app/existing.py"])

    assert "T" in prompt  # PRD 标题
    assert "app/existing.py" in prompt
