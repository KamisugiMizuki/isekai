"""python -m isekai_core：拉起核心进程并输出就绪握手。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Sequence

from .app import OwnershipError, run_core
from .config import load_config
from .log import setup_logging


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="isekai_core", description="isekai 核心进程（阶段 0）")
    parser.add_argument("--root", default=None, help="数据根目录（默认仓库根或 ISEKAI_ROOT）")
    parser.add_argument("--no-print-ready", action="store_true", help="不向 stdout 输出就绪握手")
    parser.add_argument("--parent-pid", type=int, default=None, help="父进程（壳）pid：它退出时核心随之停止")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    cfg = load_config(args.root)
    setup_logging(cfg.paths.logs, level=getattr(logging, args.log_level.upper(), logging.INFO))
    try:
        asyncio.run(run_core(cfg, print_ready=not args.no_print_ready, parent_pid=args.parent_pid))
    except OwnershipError as exc:
        print(json.dumps({"event": "failed", "code": "already_running", "message": str(exc)}, ensure_ascii=False), flush=True)
        return 3
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
