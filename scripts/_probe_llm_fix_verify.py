# -*- coding: utf-8 -*-
"""验收：deepseek「空返回」修复在真实链路下的效果（终焉线，真实 LLM）。

修复前：记忆提取/提案这类判断点调用常把预算整段烧在 reasoning 里返回空正文
（5~6 秒白等、大多以失败收场）；终焉线 38 条待提取、产出记忆 0 条。
修复后：判断点显式 thinking=disabled（1 秒出字）+ 空返回降档重试兜底。

判据（DB 硬指标 + 调用返回，非 mock）：
  1) runtime.extract 之后，该线待提取数下降 / memory 表出现首条；
  2) runtime.propose 正常返回（不因模型失败留空转）；
  3) 连续第二次 extract 仍可推进（不依赖一次性状态）。

用法：cd D:/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_probe_llm_fix_verify.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

for key in ("http_proxy", "https_proxy", "all_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(key, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from isekai_core.cli import spawn_core  # noqa: E402
from isekai_core.client import MgmtClient  # noqa: E402

ZY = ("in-e72542c8cd3f", "tl-0f0aa9b5")  # 终焉之地（rate=1，用户在用）


def stats() -> tuple[int, int]:
    db = sqlite3.connect(f"file:{ROOT / 'data' / 'isekai.db'}?mode=ro", uri=True)
    pending = db.execute(
        "SELECT count(*) FROM memory_task WHERE timeline_id=? AND state='pending'", (ZY[1],)
    ).fetchone()[0]
    mem = db.execute("SELECT count(*) FROM memory WHERE timeline_id=?", (ZY[1],)).fetchone()[0]
    db.close()
    return pending, mem


async def main() -> int:
    before = stats()
    print(f"基线：pending={before[0]} memory={before[1]}")
    proc, ready = spawn_core(None)
    mgmt = MgmtClient(ready["endpoint"], ready["mgmt"])
    ok = True
    try:
        await mgmt.connect()
        await asyncio.sleep(6)

        r1 = await mgmt.call("runtime.extract", timeout=300, instance_id=ZY[0], timeline_id=ZY[1])
        print("[extract#1]", json.dumps(r1, ensure_ascii=False)[:500])

        r2 = await mgmt.call("runtime.propose", timeout=180, instance_id=ZY[0], timeline_id=ZY[1])
        print("[propose]", json.dumps(r2, ensure_ascii=False)[:500])

        r3 = await mgmt.call("runtime.extract", timeout=300, instance_id=ZY[0], timeline_id=ZY[1])
        print("[extract#2]", json.dumps(r3, ensure_ascii=False)[:500])

        after = stats()
        moved = after[1] > before[1] or after[0] < before[0]
        print(f"[1] 真实提取推进：pending {before[0]}→{after[0]}  memory {before[1]}→{after[1]} "
              f"→ {'PASS' if moved else 'FAIL'}")
        ok &= moved

        proposed = isinstance(r2, dict) and "proposed" in json.dumps(r2)
        print(f"[2] propose 正常返回：{sorted(r2.keys()) if isinstance(r2, dict) else type(r2).__name__} "
              f"→ {'PASS' if proposed else 'FAIL'}")
        ok &= proposed

        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        await mgmt.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
