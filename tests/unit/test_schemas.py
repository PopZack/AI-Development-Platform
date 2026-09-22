"""Schema 层约定测试。

重点验证「时间戳出口」这一条约定：库里存 naive UTC，API 必须输出带 Z 的
ISO-8601。这条约定一旦破了，前端会把 UTC 当本地时间渲染，在 UTC+8 下
每个时间都会差 8 小时，而且很难在小数据量下被发现。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ValidationError

from app.common.utils import slugify, validate_slug
from app.schemas.common import UtcDateTime


class _Stamp(BaseModel):
    at: UtcDateTime


def test_naive_datetime_is_serialized_as_utc_with_z() -> None:
    stamp = _Stamp(at=datetime(2026, 9, 22, 1, 2, 3))
    assert stamp.model_dump_json() == '{"at":"2026-09-22T01:02:03Z"}'


def test_aware_datetime_is_converted_to_utc() -> None:
    """东八区的 09:02 就是 UTC 的 01:02。"""
    beijing = timezone(timedelta(hours=8))
    stamp = _Stamp(at=datetime(2026, 9, 22, 9, 2, 3, tzinfo=beijing))
    assert stamp.model_dump_json() == '{"at":"2026-09-22T01:02:03Z"}'


def test_utc_datetime_keeps_python_type_for_validation() -> None:
    stamp = _Stamp(at="2026-09-22T01:02:03Z")
    assert stamp.at == datetime(2026, 9, 22, 1, 2, 3, tzinfo=UTC)


@pytest.mark.parametrize("value", ["todo-api-demo", "a", "a1-b2", "todo-api-2"])
def test_valid_slugs(value: str) -> None:
    assert validate_slug(value)


@pytest.mark.parametrize("value", ["Todo", "todo_api", "-todo", "todo-", "todo--api", "项目"])
def test_invalid_slugs(value: str) -> None:
    assert not validate_slug(value)


def test_slugify_normalizes_punctuation_and_case() -> None:
    assert slugify("Payment Gateway: Refactor (v2)") == "payment-gateway-refactor-v2"


def test_slugify_drops_non_ascii() -> None:
    """中文名会得到空串，调用方必须自己决定回退策略（补随机后缀）。"""
    assert slugify("智全的线索平台") == ""


def test_datetime_field_rejects_garbage() -> None:
    with pytest.raises(ValidationError):
        _Stamp(at="not-a-date")
