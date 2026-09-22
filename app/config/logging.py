"""日志初始化。

Layer: Common —— 只负责把 logging 配好，不做业务。
"""

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(*, debug: bool, sql_echo: bool = False) -> None:
    """配置根 logger。重复调用是安全的（会重建 handler）。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    # 第三方 logger 降噪：SQL 只在显式开启 sql_echo 时打
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if sql_echo else logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
