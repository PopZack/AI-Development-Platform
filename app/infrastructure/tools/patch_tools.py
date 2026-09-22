"""L2 / L4 补丁工具：生成与应用代码变更。

Layer: Infrastructure。

流程（文档 §3.2 流程 B 的 Developer 环节）：

    1. generate_patch (L2)  只生成 diff，**不写盘** —— 让人先看到要改什么
    2. apply_patch  (L4)    被 Gateway 拦下 → 自动生成审批
    3. OWNER 批准
    4. 再次调用 apply_patch，带上 approval_id → 真正写盘

第 4 步之所以能通过，是因为 Gateway 会校验审批是 ``APPROVED``、
工具名与需求都对得上，并且**这个审批还没被消费过**（一条审批只换一次写盘）。

``changes`` 用「目标内容」而不是 unified diff 文本：应用补丁时直接写目标内容，
不需要解析 diff 格式（解析 diff 的边界情况多得离谱，而 Agent 生成
「这个文件最终长什么样」比生成「怎么从 A 改到 B」更不容易出错）。
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from app.common.exceptions import ToolDeniedError, ValidationError
from app.domain.tool_levels import ToolLevel
from app.infrastructure.tools.paths import WorkspacePathValidator
from app.infrastructure.tools.read_tools import ToolDefinition, ToolRequest

__all__ = ["PATCH_TOOLS"]

_MAX_CHANGES = 20
_MAX_FILE_BYTES = 512 * 1024


def _normalize_changes(params: dict[str, Any]) -> list[dict[str, str]]:
    """把 ``changes`` 参数整理成 ``[{path, new_content}]``，并做基础校验。"""
    raw = params.get("changes")
    if not isinstance(raw, list) or not raw:
        raise ValidationError(
            "changes must be a non-empty list of {path, new_content}",
            code="PATCH_CHANGES_INVALID",
        )
    if len(raw) > _MAX_CHANGES:
        raise ValidationError(
            f"Too many changes in one patch (max {_MAX_CHANGES})",
            code="PATCH_CHANGES_INVALID",
            details={"count": len(raw), "max": _MAX_CHANGES},
        )

    changes: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError(f"changes[{index}] must be an object", code="PATCH_CHANGES_INVALID")
        path = str(item.get("path") or "").strip()
        if not path:
            raise ValidationError(f"changes[{index}].path is required", code="PATCH_CHANGES_INVALID")
        if path in seen:
            raise ValidationError(f"Duplicate path in changes: {path}", code="PATCH_CHANGES_INVALID")
        seen.add(path)
        changes.append({"path": path, "new_content": str(item.get("new_content") or "")})
    return changes


def _unified_diff(workspace: WorkspacePathValidator, path: Path, current: str, new: str) -> str:
    relative = workspace.relative_to_root(path)
    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{relative}",
        tofile=f"b/{relative}",
    )
    return "".join(diff)


async def _generate_patch(request: ToolRequest) -> dict[str, Any]:
    """L2：生成 diff 但**不写盘**。这是「先看到要改什么」的那一步。"""
    request.workspace.ensure_root_exists()
    changes = _normalize_changes(request.params)

    files: list[dict[str, Any]] = []
    diffs: list[str] = []
    for change in changes:
        path = request.workspace.validate(change["path"])
        relative = request.workspace.relative_to_root(path)
        current = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        diff = _unified_diff(request.workspace, path, current, change["new_content"])
        diffs.append(diff)
        files.append(
            {
                "path": relative,
                "is_new_file": not path.exists(),
                "changed": bool(diff),
            }
        )

    return {
        "diff": "".join(diffs),
        "files": files,
        "note": "这只是预览，尚未写入工作区。需要人工批准后调用 apply_patch。",
    }


async def _apply_patch(request: ToolRequest) -> dict[str, Any]:
    """L4：真正写盘。**能不能走到这里由 Gateway 的审批校验决定**，工具本身不判权限。"""
    request.workspace.ensure_root_exists()
    changes = _normalize_changes(request.params)

    written: list[dict[str, Any]] = []
    for change in changes:
        path = request.workspace.validate(change["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        content = change["new_content"]
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            raise ToolDeniedError(
                "Patch content exceeds the per-file size limit",
                details={"path": change["path"], "max_bytes": _MAX_FILE_BYTES},
            )
        path.write_bytes(encoded)
        written.append(
            {
                "path": request.workspace.relative_to_root(path),
                "bytes": len(encoded),
            }
        )

    return {"written": written, "count": len(written)}


# apply_patch 的参数说明里写明必须带 approval_id，
# 这样「你能用哪些工具」的 Prompt 自带用法说明
_PATCH_PARAM_DOCS: dict[str, dict[str, Any]] = {
    "generate_patch": {"changes": "array（必填）— [{path, new_content}]，path 相对工作区根"},
    "apply_patch": {
        "changes": "array（必填）— [{path, new_content}]",
        "approval_id": "string（必填）— OWNER 批准后返回的审批 id",
    },
}


PATCH_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="generate_patch",
        level=ToolLevel.L2,
        description="根据目标内容生成 unified diff 预览（不写盘）",
        handler=_generate_patch,
        parameter_schema=_PATCH_PARAM_DOCS["generate_patch"],
    ),
    ToolDefinition(
        name="apply_patch",
        level=ToolLevel.L4,
        description="把变更写入工作区。需要 OWNER 批准：首次调用会自动发起审批，"
        "批准后带上返回的 approval_id 重试",
        handler=_apply_patch,
        parameter_schema=_PATCH_PARAM_DOCS["apply_patch"],
    ),
)
