# -*- coding: utf-8 -*-
"""终焉之地实例：推进世界五天，读取事件 / 说法 / 经历（检验生成内容与世界观的兼容性）。

用法：cd D:/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_probe_zhongyan_play.py
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
DAYS = 5
DB = ROOT / "data" / "isekai.db"


def db_rows(sql: str, args: tuple = ()) -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in con.execute(sql, args).fetchall()]
    except sqlite3.OperationalError as exc:
        return [{"error": str(exc)}]
    finally:
        con.close()


async def main() -> int:
    proc, ready = spawn_core(None)
    mgmt = MgmtClient(ready["endpoint"], ready["mgmt"])
    try:
        await mgmt.connect()

        # 推进世界五天
        result = await mgmt.call(
            "runtime.time.consume",
            timeout=300,
            instance_id=INSTANCE,
            timeline_id=TIMELINE,
            seconds=86400 * DAYS,
            cause=f"世界照常运转了 {DAYS} 天",
            time_source="world_process",
        )
        consume = result.get("consume", {})
        clock = result.get("clock", {})
        print("===== time.consume =====")
        print(json.dumps({"consume": consume, "state": clock.get("state"),
                          "processed_world": clock.get("processed_world")},
                         ensure_ascii=False, indent=2)[:1500])

        # 事件历史
        result = await mgmt.call(
            "runtime.history.read", timeout=60,
            instance_id=INSTANCE, timeline_id=TIMELINE, limit=60,
        )
        events = result.get("events", [])
        claims = result.get("claims", [])
        print("===== events =====")
        print(f"count={len(events)}")
        for ev in events[-20:]:
            print(json.dumps({k: ev.get(k) for k in ("id", "world_seconds", "kind", "template", "summary", "effects")},
                             ensure_ascii=False))
        print("===== claims (tail 8) =====")
        for cl in claims[-8:]:
            print(json.dumps(cl, ensure_ascii=False))

        # 环境状态（只读库）
        print("===== environment =====")
        for row in db_rows(
            "SELECT type_id, value, source, from_world, updated_world FROM environment "
            "WHERE instance_id=? AND timeline_id=? ORDER BY type_id", (INSTANCE, TIMELINE),
        ):
            print(json.dumps(row, ensure_ascii=False))

        # 经历（只读库，抽样）
        print("===== experience (tail 12) =====")
        for row in db_rows(
            "SELECT character_id, kind, summary, from_world, to_world FROM experience "
            "WHERE instance_id=? AND timeline_id=? ORDER BY to_world DESC LIMIT 12", (INSTANCE, TIMELINE),
        ):
            print(json.dumps(row, ensure_ascii=False))

        # 生活线计划（只读库）
        print("===== life_plan (tail 3) =====")
        for row in db_rows(
            "SELECT character_id, day, summary FROM life_plan "
            "WHERE instance_id=? AND timeline_id=? ORDER BY day DESC LIMIT 3", (INSTANCE, TIMELINE),
        ):
            print(json.dumps(row, ensure_ascii=False))

        # 制度 / 惯例状态（只读库）
        print("===== institution / custom / event effects =====")
        for row in db_rows(
            "SELECT id, target, kind, value, active, from_world FROM effect_state "
            "WHERE instance_id=? AND timeline_id=? ORDER BY from_world DESC LIMIT 20", (INSTANCE, TIMELINE),
        ):
            print(json.dumps(row, ensure_ascii=False))
    finally:
        await mgmt.close()
        proc.terminate()
        proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
