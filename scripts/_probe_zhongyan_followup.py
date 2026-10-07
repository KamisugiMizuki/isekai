# -*- coding: utf-8 -*-
"""终焉之地实例：补验——运行时打算（propose）/ 记忆提取（extract）/ 故事层读数 / 消息全量。

用法：cd D:/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_probe_zhongyan_followup.py
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

INSTANCE = "in-e72542c8cd3f"
TIMELINE = "tl-0f0aa9b5"
CARD = "cc-程霜"
SES = "se-159a009c"
DB = ROOT / "data" / "isekai.db"


def db_rows(sql: str, args: tuple = ()) -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in con.execute(sql, args).fetchall()]
    finally:
        con.close()


async def main() -> int:
    proc, ready = spawn_core(None)
    mgmt = MgmtClient(ready["endpoint"], ready["mgmt"])
    try:
        await mgmt.connect()
        await asyncio.sleep(4)  # 让 tick 追上

        # 1) 运行时打算
        try:
            result = await mgmt.call(
                "runtime.propose", timeout=180,
                instance_id=INSTANCE, timeline_id=TIMELINE, character_id=CARD,
            )
            print("===== runtime.propose =====")
            print(json.dumps(result, ensure_ascii=False, indent=2)[:2500])
        except Exception as exc:  # noqa: BLE001
            print("runtime.propose FAILED:", exc)

        # 2) 记忆提取
        try:
            result = await mgmt.call(
                "runtime.extract", timeout=240,
                instance_id=INSTANCE, timeline_id=TIMELINE,
            )
            print("===== runtime.extract =====")
            print(json.dumps(result, ensure_ascii=False, indent=2)[:2500])
        except Exception as exc:  # noqa: BLE001
            print("runtime.extract FAILED:", exc)

        # 3) 故事层读数（核心跑热后）
        for op in ("story.enter", "story.scene"):
            result = await mgmt.call(op, timeout=60, instance_id=INSTANCE, timeline_id=TIMELINE, character_id=CARD)
            print(f"===== {op} =====")
            print(json.dumps({k: result.get(k) for k in ("status", "product_state", "label", "can", "reason")},
                             ensure_ascii=False))

        # 4) 消息全量（对话后）
        print("===== messages =====")
        for row in db_rows(
            "SELECT seq, role, substr(COALESCE(text, parts),1,110) t, created_at FROM message "
            "WHERE session_id=? ORDER BY seq", (SES,),
        ):
            print(row)

        # 5) 知识 / 记忆增量
        print("===== knowledge count =====")
        for row in db_rows(
            "SELECT COUNT(*) n FROM knowledge WHERE instance_id=? AND timeline_id=? AND character_id=?",
            (INSTANCE, TIMELINE, CARD),
        ):
            print(row)
        print("===== memory (tail 8) =====")
        for row in db_rows(
            "SELECT id, kind, substr(text,1,90) t, recorded_world FROM memory "
            "WHERE instance_id=? AND timeline_id=? ORDER BY recorded_world DESC LIMIT 8",
            (INSTANCE, TIMELINE),
        ):
            print(row)
        print("===== intent =====")
        for row in db_rows(
            "SELECT id, character_id, object, basis, strength, stage FROM intent "
            "WHERE instance_id=? AND timeline_id=?",
            (INSTANCE, TIMELINE),
        ):
            print(row)
        return 0
    finally:
        await mgmt.close()
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
