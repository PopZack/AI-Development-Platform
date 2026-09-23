"""L3 工具：在工作区里真实运行 pytest。

Layer: Infrastructure。

文档 §14.1 把「运行测试和有限命令」定为 L3，并要求**白名单**。
这里的白名单不是「过滤危险参数」而是**根本不接受命令字符串**：

- 可执行文件固定为当前解释器的 ``-m pytest``
- 参数只允许一小撮开关（``-q`` / ``-v`` / ``-x`` / ``--maxfail=N`` / ``-k <expr>``）
  与一个相对路径，且不允许 ``..`` 或绝对路径
- 工作目录固定为授权工作区根（由路径校验器确保它本身也在根内）

### 这会执行模型写出来的代码 —— 风险要讲清楚

这是产品本身的固有性质（Developer Agent 写代码、Tester 跑测试），
但要把爆炸半径压到最小：

- **不传宿主环境变量**。只给 ``PATH`` / ``PYTHONPATH`` / ``PYTHONIOENCODING``
  等最小集合 —— 否则工作区里的测试代码可以直接读到平台的
  ``LLM_API_KEY`` / ``JWT_SECRET_KEY`` 并打到输出里（输出还会进 Prompt、进日志）。
- **固定工作目录**：测试只能相对工作区根操作，配合路径校验无法越界。
- **超时 + 输出上限**：卡死的测试不能拖住整个工作流，输出也不能撑爆 Prompt。
- 容器/沙箱隔离（无网络、只读挂载宿主）属于部署层的事，README 里写明。

⚠️ 已知限制：超时杀的是 pytest 进程本身，它派生的子进程在某些平台上可能存活。
要彻底解决需要进程组 / 容器级隔离，那超出「本地可运行的小闭环」的范围。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Any

from app.common.exceptions import ToolDeniedError
from app.domain.tool_levels import ToolLevel
from app.infrastructure.tools.read_tools import ToolDefinition, ToolRequest

__all__ = ["EXEC_TOOLS"]

# 默认 60 秒：单元测试级别的用例通常几秒内跑完；集成测试留给调用方显式放宽。
_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 300
_MAX_OUTPUT = 16_000

# 参数白名单：布尔开关 + 带值的几个。刻意不含 -p / --rootdir / --confcutdir，
# 它们能改变 pytest 的插件加载与搜索路径，等于把「固定可执行文件」的约束绕开。
_ALLOWED_FLAGS = {"-q", "-v", "-x", "-s", "--tb=short", "--tb=line", "--disable-warnings"}
_ALLOWED_VALUE_FLAGS = {"-k", "--maxfail", "-m"}
# 路径参数：只允许相对路径。
# ⚠️ 只写「不含 ..」是不够的 —— `/etc/passwd` 既不含 .. 也只由这些字符组成，
# 但它是绝对路径。所以还要单独挡掉开头的 / 与盘符。
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./\-]+$")
_ABSOLUTE_PATH = re.compile(r"^([/\\]|[A-Za-z]:)")


def sanitize_args(raw: Any) -> list[str]:
    """把调用方给的参数过滤成安全的白名单，非法参数直接拒绝而不是静默丢弃。

    静默丢弃会让人以为「参数生效了」—— 比如 ``-k`` 被丢掉，测试全跑了却报告
    「只跑了两个用例」，比报错更难查。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split()
    if not isinstance(raw, list):
        raise ToolDeniedError("args must be a list of strings", code="PYTEST_ARGS_INVALID")

    args: list[str] = []
    index = 0
    items = [str(a) for a in raw]
    while index < len(items):
        token = items[index]
        if token in _ALLOWED_FLAGS:
            args.append(token)
        elif token in _ALLOWED_VALUE_FLAGS:
            if index + 1 >= len(items):
                raise ToolDeniedError(f"{token} requires a value", code="PYTEST_ARGS_INVALID")
            args.extend([token, items[index + 1]])
            index += 1
        elif token.startswith("-"):
            raise ToolDeniedError(
                f"pytest flag not allowed: {token}",
                code="PYTEST_ARGS_INVALID",
                details={"allowed": sorted(_ALLOWED_FLAGS | _ALLOWED_VALUE_FLAGS)},
            )
        elif _SAFE_PATH.match(token) and ".." not in token and not _ABSOLUTE_PATH.match(token):
            args.append(token)
        else:
            raise ToolDeniedError(
                f"test path not allowed: {token}",
                code="PYTEST_ARGS_INVALID",
                details={"hint": "只允许工作区内的相对路径"},
            )
        index += 1
    return args


# 疑似密钥的环境变量名。按**名字**剔除而不是只给一个白名单：
# 只给白名单看似更安全，实际在 Windows 上会砍掉 SystemRoot 之类的系统变量，
# 结果子进程连 Winsock 都初始化不了（WinError 10106）—— 连 pytest 都起不来。
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API)", re.I)
# DATABASE_URL / REDIS_URL 里可能带密码（mysql://user:pass@host），一并剔除
_SECRET_SUFFIX = re.compile(r"(_URL|_URI|_DSN)$", re.I)


def _child_env(workspace_path: str) -> dict[str, str]:
    """给子进程的环境：继承系统变量，但**剥离疑似密钥**。

    工作区里的测试代码能读环境变量，而宿主环境里有 LLM_API_KEY /
    JWT_SECRET_KEY / DATABASE_URL —— 一旦被读出来打进输出，就会顺着
    「测试输出 → Prompt → artifact → 日志」一路扩散。

    ⚠️ 这是**尽力而为**的防护，不是边界：名字起得古怪的密钥可能漏过去。
    真正的隔离要靠容器（README 的部署小节有说明）。
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if not _SECRET_NAME.search(name) and not _SECRET_SUFFIX.search(name)
    }
    env["PYTHONPATH"] = workspace_path
    env["PYTHONIOENCODING"] = "utf-8"
    # 别在工作区里留 __pycache__：它会出现在 Agent 的 list_files 输出里，白占 token
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


async def _run_pytest(request: ToolRequest) -> dict[str, Any]:
    request.workspace.ensure_root_exists()
    root = request.workspace.root
    args = sanitize_args(request.params.get("args"))

    timeout = int(request.params.get("timeout") or _DEFAULT_TIMEOUT)
    timeout = max(1, min(timeout, _MAX_TIMEOUT))

    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args]
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(root),
        env=_child_env(str(root)),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    timed_out = False
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        proc.kill()
        stdout, _ = await proc.communicate()

    output = (stdout or b"").decode("utf-8", errors="replace")
    truncated = len(output) > _MAX_OUTPUT
    if truncated:
        # 头尾都留：尾部有汇总（passed/failed），头部有收集错误
        head = output[: _MAX_OUTPUT // 2]
        tail = output[-_MAX_OUTPUT // 2 :]
        output = f"{head}\n…（输出过长，中间已省略）…\n{tail}"

    exit_code = proc.returncode if proc.returncode is not None else -1
    return {
        "command": " ".join(command[2:]),  # 不含解释器路径，避免泄漏宿主目录结构
        "exit_code": exit_code,
        "timed_out": timed_out,
        "truncated": truncated,
        # 0=全部通过；1=有用例失败；5=没有收集到测试；其余见 pytest 文档
        "passed": exit_code == 0,
        "no_tests_collected": exit_code == 5,
        "output": output,
    }


EXEC_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="run_pytest",
        level=ToolLevel.L3,
        description=(
            "在工作区里真实运行 pytest 并返回输出（不经过 shell；参数走白名单；"
            "默认 60 秒超时、输出上限 16KB）"
        ),
        handler=_run_pytest,
        parameter_schema={
            "args": "array（可选）— 白名单内的 pytest 参数，如 ['-q', '-x'] 或 ['-k', 'slug']",
            "timeout": "number（可选）— 秒，默认 60，上限 300",
        },
    ),
)
