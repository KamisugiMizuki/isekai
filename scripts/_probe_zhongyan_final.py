# -*- coding: utf-8 -*-
"""终焉之地实例：最终验收快照（核心跑热后）——实例 / 时钟 / 故事层 / 事件 / 环境 / 消息。

用法：cd D:/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_probe_zhongyan_final.py
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
        await asyncio.sleep(12)  # 等首轮追赶结算

        result = await mgmt.call("instance.info", timeout=30, id=INSTANCE)
        print("== instance ==")
        print(json.dumps({"name": result["instance"].get("name"),
                          "compatibility": result["instance"].get("compatibility"),
                          "characters": result.get("characters")}, ensure_ascii=False))

        result = await mgmt.call("runtime.clock", timeout=30, instance_id=INSTANCE, timeline_id=TIMELINE)
        clock = result.get("clock", result)
        print("== clock ==")
        print(json.dumps({k: clock.get(k) for k in ("state", "label", "rate", "world_seconds", "processed_world", "target_world")},
                         ensure_ascii=False))

        for op in ("story.enter", "story.scene", "story.turn", "story.home"):
            result = await mgmt.call(op, timeout=60, instance_id=INSTANCE, timeline_id=TIMELINE, character_id=CARD)
            keys = ("status", "product_state", "label", "world_time", "can", "reason")
            print(f"== {op} ==")
            print(json.dumps({k: result.get(k) for k in keys}, ensure_ascii=False)[:900])
            if op == "story.home":
                print("   home keys:", sorted(result.keys()))

        result = await mgmt.call("runtime.history.read", timeout=60, instance_id=INSTANCE, timeline_id=TIMELINE, limit=60)
        items = result.get("items", [])
        print(f"== history: items={len(items)} claims={len(result.get('claims', []))} ==")
        for item in items[-6:]:
            print(" ", item["world_seconds"], item["template"], "|", item["summary"][:70])

        print("== environment (final) ==")
        for row in db_rows("SELECT type_id, value, source, updated_world FROM environment_state WHERE instance_id=? AND timeline_id=? ORDER BY type_id", (INSTANCE, TIMELINE)):
            print(" ", row)
        print("== effects (active) ==")
        for row in db_rows("SELECT kind, target, value, from_world FROM effect_state WHERE instance_id=? AND timeline_id=? AND active=1", (INSTANCE, TIMELINE)):
            print(" ", row)
        print("== experience tail 4 ==")
        for row in db_rows("SELECT world_seconds, kind, substr(summary,1,80) t FROM experience WHERE instance_id=? AND timeline_id=? ORDER BY world_seconds DESC LIMIT 4", (INSTANCE, TIMELINE)):
            print(" ", row)
        print("== knowledge/memory/intent/memory_task counts ==")
        for table, where in (("knowledge", "character_id='cc-程霜'"), ("memory", "1=1"), ("intent", "1=1"), ("memory_task", "character_id='cc-程霜'")):
            n = db_rows(f"SELECT COUNT(*) n FROM {table} WHERE instance_id=? AND timeline_id=? AND {where}", (INSTANCE, TIMELINE))
            # 不是每张表都有 state 列（knowledge 就没有）：先问列存不存在，再决定要不要分组
            has_state = bool(db_rows(f"SELECT name FROM pragma_table_info('{table}') WHERE name='state'"))
            states = (
                db_rows(f"SELECT state, COUNT(*) n FROM {table} WHERE instance_id=? AND timeline_id=? AND {where} GROUP BY state", (INSTANCE, TIMELINE))
                if has_state else "（该表没有 state 列）"
            )
            print(f"  {table}: n={n[0]['n']} states={states}")
        return 0
    finally:
        await mgmt.close()
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
