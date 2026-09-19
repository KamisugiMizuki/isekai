"""日志。

壳与核心日志分离；默认不记录消息正文、prompt、凭据（CHANNEL_PLUGIN_SPEC §六）。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> logging.Logger:
    global _configured
    logger = logging.getLogger("isekai")
    if _configured:
        return logger
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    logs_dir.mkdir(parents=True, exist_ok=True)
    # 容量上限：轮转而不是无限增长（CHANNEL_PLUGIN_SPEC §3.2）
    file_handler = RotatingFileHandler(
        logs_dir / "core.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    _configured = True
    return logger


def get_logger(name: str = "isekai") -> logging.Logger:
    return logging.getLogger(name)
