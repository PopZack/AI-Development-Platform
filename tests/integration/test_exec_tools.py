"""L3 工具（真实执行 pytest）的测试。

这一层是唯一会**执行模型写出来的代码**的地方，所以测试的重点不是
「pytest 能不能跑」，而是三条边界：

1. 参数白名单真的挡得住（不经过 shell、不接受任意命令、不允许越界路径）
2. **子进程拿不到宿主环境变量** —— 否则工作区里的测试代码能读到
   LLM_API_KEY 并打进输出，输出还会顺着 Prompt / artifact / 日志扩散
3. 超时与输出上限真的生效（卡死的测试不能拖住工作流）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from app.common.exceptions import ToolDeniedError
from app.infrastructure.tools.exec_tools import _child_env, sanitize_args
from app.infrastructure.tools.paths import WorkspacePathValidator
from app.infrastructure.tools.read_tools import ToolRequest

# ---------------------------------------------------------------- 参数白名单


@pytest.mark.parametrize(
    "raw",
    [
        ["-q"],
        ["-q", "-x"],
        ["-k", "slugify"],
        ["--maxfail", "1"],
        ["tests/test_ok.py"],
        ["-q", "tests/"],
    ],
)
def test_allowed_args_pass(raw: list[str]) -> None:
    assert sanitize_args(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        ["-p", "no:cacheprovider"],  # 能改插件加载，等于绕开「固定可执行文件」
        ["--rootdir", "/etc"],  # 能改搜索根
        ["-c", "evil.ini"],
        ["../../etc/passwd"],
        ["/etc/passwd"],  # 绝对路径：不含 .. 但照样越界
        ["C:////Windows////system32"],
        ["$(whoami)"],
        ["tests; rm -rf /"],
    ],
)
def test_disallowed_args_are_rejected(raw: list[str]) -> None:
    with pytest.raises(ToolDeniedError) as excinfo:
        sanitize_args(raw)
    assert excinfo.value.code in {"PYTEST_ARGS_INVALID", "TOOL_DENIED"}


def test_value_flag_without_value_is_rejected() -> None:
    """`-k` 少了值必须报错而不是被当成布尔开关 —— 否则过滤器静默失效。"""
    with pytest.raises(ToolDeniedError):
        sanitize_args(["-k"])


def test_string_args_are_split() -> None:
    assert sanitize_args("-q -x") == ["-q", "-x"]


def test_none_returns_empty_list() -> None:
    assert sanitize_args(None) == []


# ---------------------------------------------------------------- 环境隔离


def test_child_env_strips_secrets_but_keeps_system_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """剥离密钥，但保留系统变量 —— 后者少了子进程根本起不来。

    这条边界踩过：一开始只给「PATH + PYTHONPATH」的白名单，结果在 Windows 上
    因为缺 SystemRoot 导致 Winsock 初始化失败（WinError 10106），
    pytest 自己都拉不起来。所以策略是「继承 + 按名字剔除」，而不是白名单。
    """
    monkeypatch.setenv("LLM_API_KEY", "super-secret-key")
    monkeypatch.setenv("JWT_SECRET_KEY", "another-secret")
    monkeypatch.setenv("DATABASE_URL", "mysql://user:pass@host/db")
    monkeypatch.setenv("SOME_NORMAL_VAR", "keep-me")

    env = _child_env("/tmp/workspace")

    assert "LLM_API_KEY" not in env
    assert "JWT_SECRET_KEY" not in env
    assert "DATABASE_URL" not in env  # 连接串里可能带密码
    assert env["SOME_NORMAL_VAR"] == "keep-me"
    assert env["PYTHONPATH"] == "/tmp/workspace"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


# ---------------------------------------------------------------- 真实执行


def _request(workspace: Path, params: dict[str, Any] | None = None) -> ToolRequest:
    return ToolRequest(params=params or {}, workspace=WorkspacePathValidator(workspace))


def _write(workspace: Path, relative: str, content: str) -> None:
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def test_passing_suite_is_reported_as_passed(tmp_path: Path) -> None:
    from app.infrastructure.tools.exec_tools import _run_pytest

    _write(tmp_path, "tests/test_ok.py", "def test_one():\n    assert 1 + 1 == 2\n")

    result = await _run_pytest(_request(tmp_path, {"args": ["-q"]}))

    assert result["passed"] is True
    assert result["exit_code"] == 0
    assert "1 passed" in result["output"]


async def test_failing_suite_is_reported_as_failed(tmp_path: Path) -> None:
    from app.infrastructure.tools.exec_tools import _run_pytest

    _write(tmp_path, "tests/test_bad.py", "def test_one():\n    assert 1 == 2\n")

    result = await _run_pytest(_request(tmp_path, {"args": ["-q"]}))

    assert result["passed"] is False
    assert result["exit_code"] == 1


async def test_missing_tests_are_reported_distinctly(tmp_path: Path) -> None:
    """「没有测试」与「测试失败」必须分得开 —— pytest 用 exit code 5 表达前者。"""
    from app.infrastructure.tools.exec_tools import _run_pytest

    result = await _run_pytest(_request(tmp_path, {"args": ["-q"]}))

    assert result["no_tests_collected"] is True
    assert result["passed"] is False


async def test_timeout_kills_a_hanging_suite(tmp_path: Path) -> None:
    from app.infrastructure.tools.exec_tools import _run_pytest

    _write(
        tmp_path,
        "tests/test_slow.py",
        "import time\n\ndef test_slow():\n    time.sleep(30)\n",
    )

    result = await _run_pytest(_request(tmp_path, {"args": ["-q"], "timeout": 2}))

    assert result["timed_out"] is True
    assert result["passed"] is False


async def test_runs_inside_the_workspace_not_the_host_cwd(tmp_path: Path) -> None:
    """工作目录必须是工作区根 —— 否则测试会在仓库目录里乱写文件。"""
    from app.infrastructure.tools.exec_tools import _run_pytest

    _write(
        tmp_path,
        "tests/test_cwd.py",
        "from pathlib import Path\n\ndef test_cwd():\n    assert Path('tests/test_cwd.py').exists()\n",
    )

    result = await _run_pytest(_request(tmp_path, {"args": ["-q"]}))

    assert result["passed"] is True, result["output"]


async def test_output_is_truncated_but_keeps_both_ends(tmp_path: Path) -> None:
    """输出上限：头尾都要留 —— 尾部是汇总，头部是收集错误。"""
    from app.infrastructure.tools.exec_tools import _run_pytest

    # 造一个打印大量输出的测试
    body = "\n".join(f"    print('line {i:05d}')" for i in range(4000))
    _write(tmp_path, "tests/test_noisy.py", f"def test_noisy():\n{body}\n")

    result = await _run_pytest(_request(tmp_path, {"args": ["-q", "-s"]}))

    assert result["truncated"] is True
    assert len(result["output"]) < 20_000
    assert "已省略" in result["output"]


async def test_child_process_cannot_see_host_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端确认环境隔离：工作区里的测试把环境变量写进文件，我们再读它。"""
    from app.infrastructure.tools.exec_tools import _run_pytest

    monkeypatch.setenv("LLM_API_KEY", "should-not-leak")
    monkeypatch.setenv("DATABASE_URL", "mysql://user:pass@host/db")

    _write(
        tmp_path,
        "tests/test_env.py",
        "import os\nfrom pathlib import Path\n\ndef test_env():\n"
        "    Path('env_dump.txt').write_text('|'.join(sorted(os.environ)), encoding='utf-8')\n",
    )

    result = await _run_pytest(_request(tmp_path, {"args": ["-q"]}))
    assert result["passed"] is True, result["output"]

    dumped = (tmp_path / "env_dump.txt").read_text(encoding="utf-8")
    assert "LLM_API_KEY" not in dumped
    assert "JWT_SECRET_KEY" not in dumped
    assert "DATABASE_URL" not in dumped


def test_tool_is_declared_as_l3() -> None:
    """等级必须与文档 §14.1 一致：运行测试 = L3（白名单，允许并记录）。"""
    from app.domain.tool_levels import ToolLevel
    from app.infrastructure.tools.exec_tools import EXEC_TOOLS

    assert [t.name for t in EXEC_TOOLS] == ["run_pytest"]
    assert EXEC_TOOLS[0].level is ToolLevel.L3


def test_python_used_is_the_current_interpreter() -> None:
    """用 sys.executable 而不是字面量 "python"：镜像里可能只有 venv 的解释器。"""
    assert Path(sys.executable).name.lower().startswith("python")
