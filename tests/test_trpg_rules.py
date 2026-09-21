"""Minimal external rule-plugin path: real SQLite, plugin subprocess, no mock bridge."""

from __future__ import annotations

import json
import sys

import pytest

from conftest import open_mgmt, running_core
from isekai_core.world import ops as world_ops
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401


@pytest.mark.asyncio
async def test_external_rule_plugin_result_is_applied_through_management_ws(tmp_path) -> None:
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _character_id = make_instance(
                harness.store, harness.runtime.service.runtime, moment=DAY * 1500
            )
            plugin = tmp_path / "rules-ws"
            plugin.mkdir()
            (plugin / "main.py").write_text(
                "import json, sys\n"
                "request = json.loads(sys.stdin.readline())\n"
                "print(json.dumps({\n"
                "  'resolution': {'system': 'ws-test', 'outcome': 'success'},\n"
                "  'effects': [{'kind': 'institution_state', 'target': 'off-1', 'value': 'vacant', 'expiry': 'until_cleared'}],\n"
                "  'claims': [{'text': '管理面测试事件', 'source_id': 'src-1', 'audience': 'public'}],\n"
                "  'participants': [request.get('actor_id', '')]\n"
                "}, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            manifest = plugin / "manifest.json"
            manifest.write_text(
                json.dumps({
                    "id": "ws-rules", "name": "ws test", "version": "1",
                    "protocol": "isekai.trpg.rules/1", "entry": [sys.executable, "main.py"],
                }),
                encoding="utf-8",
            )
            result = await mgmt.call(
                "trpg.action.resolve",
                instance_id=info["id"], timeline_id=timeline_id,
                plugin_manifest=str(manifest), action_id="ws-act-001",
                actor_id="card-1", intent="通过管理面提交行动",
            )
            assert result["accepted"] is True
            assert result["resolution"]["system"] == "ws-test"
            assert any(row["source"] == "player_action" for row in harness.store.event_window(
                info["id"], timeline_id, until=10**15, limit=500
            ))
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_external_rule_plugin_result_is_applied_idempotently(tmp_path, store, world) -> None:
    cfg = __import__("isekai_core.config", fromlist=["load_config"]).load_config(tmp_path)
    info, timeline_id, _character_id = make_instance(store, world, moment=DAY * 1500)
    world.activate(info["id"], timeline_id, now_real=1.7e9)
    world.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)

    plugin = tmp_path / "rules"
    plugin.mkdir()
    (plugin / "main.py").write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({\n"
        "  'resolution': {'system': 'test-rules', 'outcome': 'partial', 'private_roll': 17},\n"
        "  'effects': [{'kind': 'institution_state', 'target': 'off-1', 'value': 'vacant', 'expiry': 'until_cleared'}],\n"
        "  'claims': [{'text': '职位出现变动', 'source_id': 'src-1', 'audience': 'public'}],\n"
        "  'participants': [request.get('actor_id', '')]\n"
        "}, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    manifest = plugin / "manifest.json"
    manifest.write_text(
        json.dumps({
            "id": "test-rules", "name": "test", "version": "1", "protocol": "isekai.trpg.rules/1",
            "entry": [sys.executable, "main.py"],
        }),
        encoding="utf-8",
    )

    args = {
        "instance_id": info["id"], "timeline_id": timeline_id,
        "plugin_manifest": str(manifest), "action_id": "act-001",
        "actor_id": "card-1", "intent": "调查职位变动", "context": {"difficulty": 15},
    }
    first = await world_ops.dispatch_async(cfg, None, "trpg.action.resolve", args, store=store)
    second = await world_ops.dispatch_async(cfg, None, "trpg.action.resolve", args, store=store)

    assert first["accepted"] is True
    assert first["resolution"]["private_roll"] == 17
    assert second["event"] == first["event"]
    events = [row for row in store.event_window(info["id"], timeline_id, until=10**15, limit=500)
              if row["source"] == "player_action"]
    assert len(events) == 1
    assert json.loads(events[0]["effects"])[0]["target"] == "off-1"

