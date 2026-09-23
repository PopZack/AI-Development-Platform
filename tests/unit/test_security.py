"""安全默认值的守卫。

目前只有一条规则：**日志里不许出现未脱敏的连接串**。

它的由来很具体：`app/main.py` 的启动日志原本直接打 ``settings.database_url``，
而连接串是 ``mysql+aiomysql://user:password@host/db``。多 worker 部署时
每个 worker 都会把库密码明文写进日志 —— 本地看没什么，一旦接上日志采集
（容器日志、云日志、ELK）就等于把数据库密码公布了。
排障需要的是「连的是哪个库、哪台机器」，不是用户名密码。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.common.security import mask_url

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"

# 连接串里的敏感字段名：出现在 logger 调用里就必须先脱敏
_SENSITIVE_FIELDS = ("database_url", "redis_url", "dsn", "connection_string")


def test_mask_url_hides_credentials_and_keeps_the_useful_parts() -> None:
    masked = mask_url("mysql+aiomysql://aidev:sup3rsecret@db:3306/aidev?charset=utf8mb4")

    assert "sup3rsecret" not in masked
    assert "aidev:sup3rsecret" not in masked
    # 排障要用的部分必须留着
    assert masked.startswith("mysql+aiomysql://***@")
    assert "db:3306" in masked
    assert "charset=utf8mb4" in masked


@pytest.mark.parametrize(
    "url",
    [
        "redis://127.0.0.1:6379/0",  # 没有凭据
        "sqlite+aiosqlite:///./data/app.db",
        "not-a-url",
        "",
    ],
)
def test_mask_url_leaves_credential_free_urls_untouched(url: str) -> None:
    """没有凭据就原样返回 —— 别为了「统一格式」把可用信息也改掉。"""
    assert mask_url(url) == url


# 匹配一个 logger 调用（含括号内可能出现的嵌套括号），用于静态检查
_LOGGER_CALL = re.compile(r"logger\.[a-z]+\((?:[^()]|\([^()]*\))*\)", re.S)


def test_no_logger_call_prints_an_unmasked_connection_string() -> None:
    """任何打印连接串的 logger 调用都必须套 ``mask_url()``。"""
    offenders: list[str] = []

    for path in sorted(APP_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for call in _LOGGER_CALL.finditer(text):
            body = call.group(0)
            if not any(field in body for field in _SENSITIVE_FIELDS):
                continue
            if "mask_url" in body:
                continue
            line_no = text[: call.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line_no}  {body[:70]}")

    assert not offenders, (
        "发现直接打印连接串的日志（会把密码写进日志，多 worker 下重复 N 遍）：\n"
        + "\n".join(offenders)
        + "\n用 app.common.security.mask_url() 包一层。"
    )
