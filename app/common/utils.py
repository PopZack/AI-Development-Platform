"""通用工具。

Layer: Common —— 无业务含义的纯函数。
"""

from __future__ import annotations

import re
import unicodedata
from uuid import uuid4

__all__ = ["slugify"]

_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_slug(value: str) -> bool:
    return bool(_SLUG_PATTERN.match(value))


def slugify(value: str) -> str:
    """把项目名转成 URL 友好的 slug。

    中文等非 ASCII 字符在 ``ascii`` 编码后会被丢弃，此时返回空串，
    由调用方决定回退策略（补随机后缀而不是硬塞一个可能重复的默认值）。
    """
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return slug


def random_suffix(length: int = 6) -> str:
    return uuid4().hex[:length]
