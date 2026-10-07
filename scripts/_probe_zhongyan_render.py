# -*- coding: utf-8 -*-
"""终焉之地实例：生成故事内容——事件渲染 / 说法展开 / 故事层读数。

用法：cd D:/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_probe_zhongyan_render.py
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


def show(title: str, payload) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


async def main() -> int:
    # 先挑事件：渲染两条有代表性的（寻人纸 / 对赌合同）
    events = db_rows(
        "SELECT id, world_seconds, template, summary FROM event "
        "WHERE instance_id=? AND timeline_id=? AND source='engine' AND template NOT LIKE 'cf-%' AND template NOT LIKE 'nv-%' "
        "ORDER BY world_seconds",
        (INSTANCE, TIMELINE),
    )
    by_tpl = {e["template"]: e for e in events}
    pick_render = [e for t in ("et-6", "et-4", "et-5") if (e := by_tpl.get(t))]
    print("pick:", [(e["template"], e["id"]) for e in pick_render])

    # 程霜持有的寻人纸说法（src-2 渠道）+ 对赌合同说法
    claim = db_rows(
        "SELECT id FROM claim WHERE instance_id=? AND timeline_id=? AND source_id='src-2' AND text LIKE '%寻人纸%' "
        "ORDER BY earliest_world DESC LIMIT 1",
        (INSTANCE, TIMELINE),
    )
    claim_id = claim[0]["id"] if claim else ""
    print("claim:", claim_id)

    proc, ready = spawn_core(None)
    mgmt = MgmtClient(ready["endpoint"], ready["mgmt"])
    try:
        await mgmt.connect()
        for ev in pick_render:
            result = await mgmt.call(
                "event.render", timeout=180,
                instance_id=INSTANCE, timeline_id=TIMELINE, event_id=ev["id"],
            )
            show(f"event.render {ev['template']} ({ev['id']})",
                 {k: result.get(k) for k in ("detail", "text_source", "calls", "reused", "budget")})

        if claim_id:
            result = await mgmt.call(
                "event.expand", timeout=180,
                instance_id=INSTANCE, timeline_id=TIMELINE,
                claim_id=claim_id, character_id=CARD,
                question="这张寻人纸上还写了什么？",
            )
            show("event.expand", {k: result.get(k) for k in ("text", "derived", "state", "calls", "reused", "note", "budget")})

        result = await mgmt.call(
            "story.enter", timeout=60,
            instance_id=INSTANCE, timeline_id=TIMELINE, character_id=CARD,
        )
        show("story.enter", result)

        result = await mgmt.call(
            "story.scene", timeout=60,
            instance_id=INSTANCE, timeline_id=TIMELINE, character_id=CARD,
        )
        show("story.scene", result)

        result = await mgmt.call(
            "story.turn", timeout=60,
            instance_id=INSTANCE, timeline_id=TIMELINE, character_id=CARD,
        )
        show("story.turn", result)
    finally:
        await mgmt.close()
        proc.terminate()
        proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
