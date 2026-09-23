"""安全相关的零散工具。

Layer: Common。

目前只有一个函数，但它是必须的：**日志会被采集、转发、长期保存**，
明文凭据出现在日志里等同于泄露一次 —— 而且是最难察觉的那种泄露，
因为日志通常不会有人逐行审查。
"""

from __future__ import annotations

__all__ = ["mask_url"]


def mask_url(url: str) -> str:
    """把 URL 里的凭据部分换成 ``***``。

    刻意保留 scheme / host / port / path：排障时最需要知道的是
    「连的是哪个库、哪台机器、哪个 db」，而**用户名密码一个都不需要**。

    >>> mask_url("mysql+aiomysql://aidev:secret@db:3306/aidev?charset=utf8mb4")
    'mysql+aiomysql://***@db:3306/aidev?charset=utf8mb4'
    >>> mask_url("redis://127.0.0.1:6379/0")
    'redis://127.0.0.1:6379/0'
    """
    scheme, sep, rest = url.partition("://")
    if not sep or "@" not in rest:
        return url
    return f"{scheme}://***@{rest.rpartition('@')[2]}"
