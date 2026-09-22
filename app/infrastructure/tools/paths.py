"""工作区路径校验。

Layer: Infrastructure。

设计文档 §14.1 的硬性要求（原文）：

    路径校验必须解析为规范化路径后判断是否在授权工作区根内，
    **不能用字符串前缀判断**。

为什么「字符串前缀」是错的，值得把例子写下来：

- ``/workspace`` 前缀匹配 ``/workspace-evil/x`` —— 两个完全不同的目录
- ``/workspace/../etc/passwd`` —— 前缀匹配，但真实位置在 /etc
- 符号链接 ``/workspace/link`` 指向 ``/etc`` —— 字符串看着在里面

``Path.resolve()`` 会把 ``..``、符号链接、相对路径全部展开成真实绝对路径，
之后 ``is_relative_to`` 才是可靠的判断。

所有文件类工具（list_files / read_file / search_code / …）都必须先过这一层，
不允许自己另写一套「看起来也行」的路径检查。
"""

from __future__ import annotations

from pathlib import Path

from app.common.exceptions import ToolDeniedError

__all__ = ["WorkspacePathValidator"]


class WorkspacePathValidator:
    """把「随便什么字符串」变成「确定落在授权根内的绝对路径」，否则拒绝。"""

    def __init__(self, workspace_root: str | Path) -> None:
        # 根目录自己也 resolve 一次：调用方传 "./workspace" 时，
        # 不 resolve 的话 is_relative_to 会因为前缀形态不同而误判
        self._root = Path(workspace_root).expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root_exists(self) -> Path:
        """确保授权根存在并返回它。

        放在工具执行前而不是构造时：构造发生在应用启动，
        那时候工作区可能还没被创建，不该因为这个把启动搞挂。
        """
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    def validate(self, raw: str | Path) -> Path:
        """校验并返回规范化后的绝对路径。越界抛 ``ToolDeniedError``。

        相对路径一律相对**授权根**解析 —— 而不是相对进程当前目录，
        否则「同一份代码换个启动目录行为就变了」。
        """
        if not isinstance(raw, (str, Path)):
            raise ToolDeniedError("Path must be a string", details={"received_type": type(raw).__name__})

        text = str(raw).strip()
        if not text:
            raise ToolDeniedError("Path must not be empty")

        # 反斜杠/盘符这类 Windows 写法交给 resolve 处理；
        # 这里只拦掉明显恶意控制字符，避免它们混进日志
        if any(ch in text for ch in ("\x00", "\n", "\r")):
            raise ToolDeniedError("Path contains control characters")

        resolved = (self._root / text).resolve()

        if not resolved.is_relative_to(self._root):
            raise ToolDeniedError(
                "Path is outside the authorized workspace root",
                details={"workspace_root": str(self._root)},
            )
        return resolved

    def relative_to_root(self, resolved: Path) -> str:
        """把已校验的路径转回相对形式（给响应/日志用，不暴露机器绝对路径）。"""
        return resolved.relative_to(self._root).as_posix()
