"""L1 读取类工具：list_files / read_file / search_code。

Layer: Infrastructure。

这些是**真实实现**，不是占位 —— 文档 §11 Stage 4 要求 Tool Gateway 能实际执行。
参数只接受「工作区内的相对路径」，越界由 :class:`WorkspacePathValidator` 统一拦截。

三类输出都设了条数/体积上限。原因很实际：Agent 一次调用的输出会整体进
Prompt，返回 5000 个文件等于把上下文撑爆，还烧钱。上限到了就截断并如实告知，
**不要静默丢数据** —— 模型不知道有截断就会基于不完整的信息做判断。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.common.exceptions import NotFoundError
from app.domain.tool_levels import ToolLevel
from app.infrastructure.tools.paths import WorkspacePathValidator

__all__ = ["ToolDefinition", "ToolRequest", "READ_TOOLS"]

# 上限。够 Agent 用，又能防止一次调用把上下文撑爆
_MAX_LIST_ENTRIES = 500
_MAX_FILE_BYTES = 200 * 1024
_MAX_SEARCH_HITS = 100
_MAX_SEARCH_LINE_LEN = 240

_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache"}

_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache"}

_TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".sql",
    ".sh",
    ".env",
}


@dataclass(frozen=True)
class ToolRequest:
    params: dict[str, Any]
    workspace: WorkspacePathValidator


@dataclass(frozen=True)
class ToolDefinition:
    """一个可被 Agent 调用的工具：名字、等级、怎么执行、参数长什么样。

    ``parameter_schema`` 是给 Prompt 看的参数说明 —— Agent 不知道参数结构就只能瞎猜。
    跟着工具定义走，而不是集中放一张表：加新工具时想忘都忘不掉。
    """

    name: str
    level: ToolLevel
    description: str
    handler: Callable[[ToolRequest], Awaitable[dict[str, Any]]]
    parameter_schema: dict[str, Any] = field(default_factory=dict)


def _skip(path: Path) -> bool:
    """跳过必然无意义的目录与文件（依赖、缓存、二进制）。"""
    parts = set(path.parts)
    return bool(parts & _SKIP_DIRS) or (path.is_file() and path.suffix.lower() not in _TEXT_SUFFIXES)


async def _list_files(request: ToolRequest) -> dict[str, Any]:
    request.workspace.ensure_root_exists()
    base = request.workspace.validate(request.params.get("path") or ".")
    if not base.exists():
        raise NotFoundError.for_resource("WORKSPACE_PATH", f"No such directory: {base.name}")
    if not base.is_dir():
        raise NotFoundError.for_resource("WORKSPACE_PATH", "Not a directory")

    entries: list[dict[str, Any]] = []
    truncated = False
    for path in sorted(base.rglob("*")):
        if _skip(path):
            continue
        if len(entries) >= _MAX_LIST_ENTRIES:
            truncated = True
            break
        entries.append(
            {
                "path": request.workspace.relative_to_root(path),
                "type": "dir" if path.is_dir() else "file",
                "size": path.stat().st_size if path.is_file() else None,
            }
        )
    return {"entries": entries, "count": len(entries), "truncated": truncated}


async def _read_file(request: ToolRequest) -> dict[str, Any]:
    request.workspace.ensure_root_exists()
    path = request.workspace.validate(request.params["path"])
    if not path.exists():
        raise NotFoundError.for_resource("WORKSPACE_FILE", "File does not exist in the workspace")
    if path.is_dir():
        raise NotFoundError.for_resource("WORKSPACE_FILE", "Path is a directory, not a file")

    size = path.stat().st_size
    truncated = size > _MAX_FILE_BYTES
    raw = path.read_bytes()[:_MAX_FILE_BYTES]

    # 非 UTF-8（二进制/别的编码）时用 replace 兜底，而不是抛错 ——
    # 「读不了这个文件」对 Agent 是有效信息，但要让它知道是被替换过的
    content = raw.decode("utf-8", errors="replace")
    return {
        "path": request.workspace.relative_to_root(path),
        "size": size,
        "truncated": truncated,
        "content": content,
    }


async def _search_code(request: ToolRequest) -> dict[str, Any]:
    request.workspace.ensure_root_exists()
    query = str(request.params.get("query") or "")
    if not query.strip():
        raise NotFoundError("Search query must not be empty", code="SEARCH_QUERY_EMPTY")

    base = request.workspace.validate(request.params.get("path") or ".")
    if not base.is_dir():
        raise NotFoundError.for_resource("WORKSPACE_PATH", "Not a directory")

    matches: list[dict[str, Any]] = []
    truncated = False
    for path in sorted(base.rglob("*")):
        if truncated:
            break
        if not path.is_file() or _skip(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if query in line:
                if len(matches) >= _MAX_SEARCH_HITS:
                    truncated = True
                    break
                matches.append(
                    {
                        "path": request.workspace.relative_to_root(path),
                        "line": line_number,
                        "text": line.strip()[:_MAX_SEARCH_LINE_LEN],
                    }
                )
        if truncated:
            break

    return {"query": query, "matches": matches, "count": len(matches), "truncated": truncated}


READ_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="list_files",
        level=ToolLevel.L1,
        description="列出工作区内的文件与目录（递归，跳过依赖与缓存目录）",
        handler=_list_files,
        parameter_schema={"path": "string（可选）— 相对工作区根的目录，默认根目录"},
    ),
    ToolDefinition(
        name="read_file",
        level=ToolLevel.L1,
        description="读取工作区内一个文本文件的内容（最大 200KB，超出会截断）",
        handler=_read_file,
        parameter_schema={"path": "string（必填）— 相对工作区根的文件路径"},
    ),
    ToolDefinition(
        name="search_code",
        level=ToolLevel.L1,
        description="在工作区文本文件里按子串搜索，返回文件 / 行号 / 行内容",
        handler=_search_code,
        parameter_schema={
            "query": "string（必填）— 要搜索的子串（区分大小写）",
            "path": "string（可选）— 只在这个子目录里搜",
        },
    ),
)
