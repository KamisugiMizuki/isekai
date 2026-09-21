"""TRPG 战役运行时（TRPG_CAMPAIGN_RUNTIME_SPEC）：真 WS + 真 SQLite + 真插件子进程。

覆盖设计里的硬约束：未确认不裁定、规则状态与世界后果同批成功或同批失败、
幂等重放返回原结果、版本冲突不落半条、回滚恢复规则状态、插件崩溃不猜结果、
待选择不进世界事实。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from conftest import open_mgmt, running_core
from isekai_core.ump import UmpError
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401

PLUGIN_SOURCE = '''
import json, sys

request = json.loads(sys.stdin.readline())
mode = str(request.get("intent") or "ok")
state = request.get("rule_state") or {}
base = int(state.get("state_revision") or 0)
if mode == "crash":
    sys.exit(1)
patch = {
    "ruleset_id": state.get("ruleset_id"),
    "base_state_revision": base,
    # 首次落值用 add，之后用 increase：数值增减要求目标已存在（避免路径写错被静默当 0）
    "operations": [{
        "path": "/actors/pc-1/hp",
        "op": "add" if base == 0 else "increase",
        "value": 2,
    }],
}
if mode == "bad_base":
    patch["base_state_revision"] = base + 5
consequences = [{
    "kind": "institution_state", "target": "off-1", "value": "vacant",
    "expiry": "until_cleared", "certainty": "confirmed",
}]
if mode == "bad_world":
    consequences = [{
        "kind": "institution_state", "target": "off-999", "value": "vacant",
        "expiry": "until_cleared", "certainty": "confirmed",
    }]
transition = {"status": "advanced"}
if mode == "time":
    transition = {
        "status": "advanced",
        "world_time_request": {"seconds": 3600, "cause": "潜入夜行"},
    }
if mode == "bad_time":
    transition = {"status": "advanced", "world_time_request": {"seconds": -5, "cause": "倒流"}}
if mode == "waiting":
    transition = {
        "status": "waiting_choice",
        "available_choices": [{"choice_id": "ch-1", "options": ["追上去", "先撤退"]}],
    }
print(json.dumps({
    "resolution": {"system": "fake-rules", "outcome": "success", "mode": mode},
    "rule_state_patch": patch,
    "consequences": consequences,
    "scene_transition": transition,
    "claims": [{"text": "职位出现变动", "source_id": "src-1", "audience": "public"}],
    "participants": ["pc-1"],
}, ensure_ascii=False))
'''


CONVERT_SOURCE = '''
import json, sys

req = json.loads(sys.stdin.readline())
converter_id = str(req.get("converter_id") or "")
state = req.get("opaque_state") or {}
if converter_id == "boom":
    sys.exit(3)
if converter_id == "bad_shape":
    print(json.dumps({"opaque_state": "不是对象"}))
    raise SystemExit(0)
actors = {
    key: {"hp": int((value or {}).get("hp") or 0) * 10}
    for key, value in (state.get("actors") or {}).items()
}
losses = ["旧字段 blood_pool 没有对应项"] if converter_id == "lossy" else []
print(json.dumps({
    "opaque_state": {"actors": actors, "converted_from": req.get("from_version")},
    "losses": losses,
    "notes": f"{req.get('from_version')} -> {req.get('to_version')}",
}, ensure_ascii=False))
'''


RESIDENT_SOURCE = '''
import json, os, sys

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "spawns.txt"), "a", encoding="utf-8") as handle:
    handle.write("spawn\\n")
with open(os.path.join(here, "pid.txt"), "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))

for line in sys.stdin:                     # 读到 EOF 就退出（核心被杀时没人来回收它）
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    if request.get("type") == "ping":
        print(json.dumps({"type": "pong"}), flush=True)
        continue
    state = request.get("rule_state") or {}
    base = int(state.get("state_revision") or 0)
    print(json.dumps({
        "resolution": {"system": "resident", "outcome": "success", "base": base},
        "rule_state_patch": {
            "ruleset_id": state.get("ruleset_id"),
            "base_state_revision": base,
            "operations": [{
                "path": "/actors/pc-1/hp",
                "op": "add" if base == 0 else "increase",
                "value": 2,
            }],
        },
        "consequences": [{
            "kind": "institution_state", "target": "off-1", "value": "vacant",
            "expiry": "until_cleared", "certainty": "confirmed",
        }],
        "scene_transition": {"status": "advanced"},
        "claims": [],
        "participants": ["pc-1"],
    }, ensure_ascii=False), flush=True)
'''


def make_resident_plugin(tmp_path) -> str:
    """清单声明 resident: true 的插件：一个进程活过多次裁定，自己数被拉起了几次。"""
    folder = tmp_path / "resident_rules"
    folder.mkdir(exist_ok=True)
    (folder / "main.py").write_text(RESIDENT_SOURCE, encoding="utf-8")
    manifest = folder / "manifest.json"
    manifest.write_text(
        json.dumps({
            "id": "fake-rules", "name": "fake resident", "version": "1.0", "resident": True,
            "protocol": "isekai.trpg.rules/1", "entry": [sys.executable, "main.py"],
        }),
        encoding="utf-8",
    )
    return str(manifest)


def make_plugin(tmp_path) -> str:
    folder = tmp_path / "rules"
    folder.mkdir(exist_ok=True)
    (folder / "main.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    manifest = folder / "manifest.json"
    (folder / "convert.py").write_text(CONVERT_SOURCE, encoding="utf-8")
    manifest.write_text(
        json.dumps({
            "id": "fake-rules", "name": "fake", "version": "1.0",
            "protocol": "isekai.trpg.rules/1", "entry": [sys.executable, "main.py"],
            "converters": [
                {"converter_id": "up1-2", "from_version": "1.0", "to_version": "2.0",
                 "converter_version": "c-1", "entry": [sys.executable, "convert.py"]},
                {"converter_id": "lossy", "from_version": "2.0", "to_version": "3.0",
                 "entry": [sys.executable, "convert.py"]},
                {"converter_id": "boom", "from_version": "3.0", "to_version": "4.0",
                 "entry": [sys.executable, "convert.py"]},
                {"converter_id": "bad_shape", "from_version": "3.0", "to_version": "5.0",
                 "entry": [sys.executable, "convert.py"]},
            ],
        }),
        encoding="utf-8",
    )
    return str(manifest)


async def _campaign(mgmt, info, timeline_id, plugin, **overrides) -> str:
    created = await mgmt.call(
        "trpg.campaign.create",
        instance_id=info["id"], timeline_id=timeline_id,
        ruleset_id="fake-rules", ruleset_version="1.0", plugin_manifest=plugin,
        participants=["card-1"],
        scene={"kind": "conflict", "location_refs": ["rl-1"], "participants": ["card-1"]},
        **overrides,
    )
    assert created["status"] == "active"
    assert created["scene_id"], "开局面场景要落在战役上"
    return str(created["campaign_id"])


async def _run_action(mgmt, info, timeline_id, campaign_id, *, plugin, intent="ok", auto=True):
    declared = await mgmt.call(
        "trpg.action.declare",
        instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
        actor_id="card-1", intent=intent, raw_text=intent, auto_confirm=auto,
        target_refs=["off-1"],
    )
    action_id = str(declared["action_id"])
    if not auto:
        confirmed = await mgmt.call(
            "trpg.action.confirm",
            instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
            action_id=action_id, action_revision=declared["action_revision"],
        )
        assert confirmed["status"] == "confirmed"
    resolved = await mgmt.call(
        "trpg.action.resolve", timeout=60.0,
        instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
        plugin_manifest=plugin, action_id=action_id, actor_id="card-1", intent=intent,
    )
    return action_id, resolved


@pytest.mark.asyncio
async def test_commits_rule_state_and_world_consequence_together(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, resolved = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="ok")
            assert resolved["status"] == "reviewing", "裁定结果先停在待提交，不写世界"
            assert resolved["rule_state_patch"]["base_state_revision"] == 0

            # 裁定阶段不落世界、不落规则状态：提交前世界事件为零
            assert [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                    if row["source"] == "trpg_action"] == []
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id))["state_revision"] == 0

            committed = await mgmt.call(
                "trpg.commit",
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="k-1",
            )
            assert committed["status"] == "committed", committed
            assert committed["state_revisions"] == {"fake-rules": 1}

            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["opaque_state"] == {"actors": {"pc-1": {"hp": 2}}}, "规则状态按 patch 落盘"
            events = [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                      if row["source"] == "trpg_action"]
            assert len(events) == 1 and json.loads(events[0]["effects"])[0]["target"] == "off-1"
            assert harness.store.institution_list(info["id"], timeline_id), "制度效果落成真状态"

            view = await mgmt.call("trpg.scene.view", instance_id=info["id"],
                                   timeline_id=timeline_id, campaign_id=campaign_id)
            assert view["actions"] == [], "已提交的行动不再挂在可行动局面上"
            assert [item["status"] for item in view["recent"]] == ["transitioned"]
            assert view["rule_state"]["state_revision"] == 1
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_unconfirmed_action_never_reaches_resolver(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            declared = await mgmt.call(
                "trpg.action.declare",
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                actor_id="card-1", intent="ok", raw_text="ok",
            )
            assert declared["status"] == "awaiting_confirmation"
            with pytest.raises(UmpError):
                await mgmt.call(
                    "trpg.action.resolve", timeout=60.0,
                    instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                    plugin_manifest=plugin, action_id=declared["action_id"],
                    actor_id="card-1", intent="ok",
                )
            assert [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                    if row["source"] == "trpg_action"] == []
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_invalid_world_consequence_writes_nothing(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _resolved = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="bad_world")
            result = await mgmt.call(
                "trpg.commit",
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="k-bad",
            )
            assert result["status"] == "needs_review"
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 0, "世界后果非法则规则状态也不落（同批）"
            assert [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                    if row["source"] == "trpg_action"] == []
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_stale_base_revision_conflicts_without_writes(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            first, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="ok")
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=first, idempotency_key="k-a")
            second, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="bad_base")
            result = await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                                     campaign_id=campaign_id, action_id=second, idempotency_key="k-b")
            assert result["status"] == "conflict"
            # 插件在「当前 revision + 5」上做版本，与真实 revision 不同 → 冲突
            assert result["rule_state_revision"] == 1 and result["requested_base"] == 6
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 1, "冲突不推进规则状态"
            events = [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                      if row["source"] == "trpg_action"]
            assert len(events) == 1, "冲突不落第二条世界事件"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_replays_original(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="ok")
            first = await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id, action_id=action_id, idempotency_key="k-dup")
            again = await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id, action_id=action_id, idempotency_key="k-dup")
            assert again["status"] == "duplicate"
            assert again["joint_commit_id"] == first["joint_commit_id"]
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 1, "重放不重复扣资源"
            events = [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                      if row["source"] == "trpg_action"]
            assert len(events) == 1
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_pending_choice_is_not_a_world_fact(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="waiting")
            committed = await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                                        campaign_id=campaign_id, action_id=action_id, idempotency_key="k-w")
            assert committed["open_choices"] == ["ch-1"]
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"],
                                   timeline_id=timeline_id, campaign_id=campaign_id)
            assert len(view["pending_choices"]) == 1
            assert view["scene"]["status"] == "waiting_choice"
            # 未选择的分支不能出现在世界事实里（只落了一条已提交的行动事件）
            assert not any("追上去" in str(row["detail"]) for row in
                           harness.store.event_window(info["id"], timeline_id, until=10**15))
            picked = await mgmt.call("trpg.choice.select", instance_id=info["id"], timeline_id=timeline_id,
                                     campaign_id=campaign_id, choice_id="ch-1", selection="追上去")
            assert picked["status"] == "selected" and picked["duplicate"] is False
            again = await mgmt.call("trpg.choice.select", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id, choice_id="ch-1", selection="追上去")
            assert again["duplicate"] is True, "已选择的待选择重放原结果"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_plugin_crash_marks_failed_and_guesses_nothing(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            declared = await mgmt.call(
                "trpg.action.declare",
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                actor_id="card-1", intent="crash", raw_text="crash", auto_confirm=True,
            )
            with pytest.raises(UmpError):
                await mgmt.call(
                    "trpg.action.resolve", timeout=60.0,
                    instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                    plugin_manifest=plugin, action_id=declared["action_id"],
                    actor_id="card-1", intent="crash",
                )
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"],
                                   timeline_id=timeline_id, campaign_id=campaign_id)
            assert view["actions"][0]["status"] == "plugin_failed"
            assert view["rule_state"]["state_revision"] == 0
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_rollback_restores_rule_state(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            first, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="ok")
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=first, idempotency_key="k-1")
            mark = await mgmt.call("runtime.commit", instance_id=info["id"], timeline_id=timeline_id)
            second, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="ok")
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=second, idempotency_key="k-2")
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 2 and state["opaque_state"]["actors"]["pc-1"]["hp"] == 4

            await mgmt.call("runtime.rollback", instance_id=info["id"], timeline_id=timeline_id,
                            commit_id=str(mark["commit"]["id"]), confirm=True)
            rolled = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                     timeline_id=timeline_id, campaign_id=campaign_id)
            assert rolled["state_revision"] == 1, "回滚把规则状态退回提交那一刻"
            assert rolled["opaque_state"]["actors"]["pc-1"]["hp"] == 2
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"],
                                   timeline_id=timeline_id, campaign_id=campaign_id)
            assert [item["action_id"] for item in view["recent"]] == [first], "被截去的行动退出当前线"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_recover_reports_inflight_without_guessing(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            declared = await mgmt.call(
                "trpg.action.declare",
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                actor_id="card-1", intent="ok", raw_text="ok", auto_confirm=True,
            )
            # 模拟「进程在裁定中途被杀」：库里留下 resolving 状态，重启后必须被归位
            harness.store.trpg_upserts({"action": [{
                "instance_id": info["id"], "timeline_id": timeline_id, "campaign_id": campaign_id,
                "action_id": declared["action_id"], "status": "resolving",
            }]})
            result = await mgmt.call("trpg.recover", instance_id=info["id"], timeline_id=timeline_id)
            assert result["interrupted"] == 1
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"],
                                   timeline_id=timeline_id, campaign_id=campaign_id)
            assert view["actions"][0]["status"] == "interrupted"
            assert view["rule_state"]["state_revision"] == 0, "中断不猜结果、不扣资源"
        finally:
            await mgmt.close()

@pytest.mark.asyncio
async def test_ruleset_version_change_blocks_then_manual_accept_unblocks(tmp_path) -> None:
    """§十六：插件声明的规则版本变了 → 战役 blocked；人工确认接受是唯一出口。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _resolved = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            committed = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="ver-1",
            )
            assert committed["status"] == "committed", committed
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_ruleset_version"] == "1.0", "状态要记下写它时插件声明的规则版本"

            # 插件升级：清单里声明的规则版本变了（只有 opaque_state 格式变化才该这么干）
            manifest_path = Path(plugin)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["ruleset_version"] = "2.0"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            # 闸门在裁定入口就生效：拿旧状态去问新插件 = 最危险的情形（§十六）
            with pytest.raises(UmpError) as err:
                await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            assert "规则版本不兼容" in str(err.value)

            row = await mgmt.call("trpg.campaign.info", instance_id=info["id"],
                                  timeline_id=timeline_id, campaign_id=campaign_id)
            assert row["status"] == "blocked", "版本不兼容要阻断，不静默替换状态"
            assert "规则版本不兼容" in row["note"]

            # 出口：人工确认接受新版本（同一批把状态版本重铸，并留下记录）
            accepted = await mgmt.call(
                "trpg.campaign.status", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id, status="active", accept_ruleset_version="2.0",
            )
            assert accepted["status"] == "active"
            assert "人工确认" in accepted["note"]
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_ruleset_version"] == "2.0"
            assert state["state_revision"] == 1, "接受版本不改写状态本身"
            assert state["opaque_state"] == {"actors": {"pc-1": {"hp": 2}}}

            action_id2, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            committed2 = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id2, idempotency_key="ver-2",
            )
            assert committed2["status"] == "committed", committed2
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 2 and state["opaque_state"]["actors"]["pc-1"]["hp"] == 4
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_waiting_campaign_takes_only_the_choice(tmp_path) -> None:
    """§11.1：有待选择时战役进 waiting，只接受对应的输入；选完就能继续声明行动。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="waiting")
            committed = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="wait-1",
            )
            assert committed["status"] == "committed", committed
            choice_id = committed["open_choices"][0]
            row = await mgmt.call("trpg.campaign.info", instance_id=info["id"],
                                  timeline_id=timeline_id, campaign_id=campaign_id)
            assert row["status"] == "waiting"

            with pytest.raises(UmpError) as err:
                await mgmt.call(
                    "trpg.action.declare", instance_id=info["id"], timeline_id=timeline_id,
                    campaign_id=campaign_id, actor_id="card-1", intent="硬来", raw_text="硬来",
                )
            assert "等待选择" in str(err.value)

            picked = await mgmt.call(
                "trpg.choice.select", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id, choice_id=choice_id, selection="追上去",
            )
            assert picked["status"] == "selected" and picked["duplicate"] is False
            row = await mgmt.call("trpg.campaign.info", instance_id=info["id"],
                                  timeline_id=timeline_id, campaign_id=campaign_id)
            assert row["status"] == "active"

            # 继续本来就靠「再声明一个行动」完成，不需要额外的自动续接机制
            action_id2, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            after = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id2, idempotency_key="wait-2",
            )
            assert after["status"] == "committed", after
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_gm_declaration_source_is_distinguishable(tmp_path) -> None:
    """§十五：GM 直接裁定的后果与角色行动的后果不能混成同一个来源。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            with pytest.raises(UmpError) as err:
                await mgmt.call(
                    "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                    action_id=action_id, idempotency_key="gm-bad", source_mode="whatever",
                )
            assert "来源" in str(err.value)

            committed = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="gm-ok", source_mode="gm_declaration",
            )
            assert committed["status"] == "committed", committed
            events = harness.store.event_window(info["id"], timeline_id, until=10**15)
            sources = {str(row["source"]) for row in events}
            assert "gm_declaration" in sources, "GM 直接裁定要落成自己的来源"
            assert "trpg_action" not in sources
        finally:
            await mgmt.close()

@pytest.mark.asyncio
async def test_scene_time_request_moves_world_clock_in_the_same_commit(tmp_path) -> None:
    """§十四：场景内时间消耗与规则状态、世界后果同批落地，随后按正常批次结算。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            await mgmt.call("runtime.activate", instance_id=info["id"], timeline_id=timeline_id)
            clock = (await mgmt.call("runtime.clock", instance_id=info["id"], timeline_id=timeline_id))["clock"]
            before = int(clock["processed_world"])

            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="time")
            committed = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="time-1",
            )
            assert committed["status"] == "committed", committed
            assert committed["world_time_applied"] is True
            assert committed["world_time_request"] == {"seconds": 3600, "cause": "潜入夜行"}
            settled = committed["world_time_settled"]
            assert settled["processed_world"] == before + 3600, f"跳过去的时间要当场结算：{settled}"
            assert settled["cause"] == "潜入夜行"
            assert committed["world_time_source"] == "player_action", "玩家行动与世界过程分开记账"

            # 同一批里的另外两样也得真在
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id))["state_revision"] == 1
            events = [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                      if row["source"] == "trpg_action"]
            assert len(events) == 1, "世界后果与时间消耗同批"

            # 重放：账本只增不改，拿到原结果（applied 与请求都在），结算读数看时钟
            again = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="time-1",
            )
            assert again["status"] == "duplicate" and again["world_time_applied"] is True
            clock = (await mgmt.call("runtime.clock", instance_id=info["id"], timeline_id=timeline_id))["clock"]
            assert int(clock["processed_world"]) == before + 3600, "重放不再推时间"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_bad_time_request_is_reviewed_and_moves_nothing(tmp_path) -> None:
    """非法时间请求进待审：不静默忽略，也不改时钟。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            await mgmt.call("runtime.activate", instance_id=info["id"], timeline_id=timeline_id)
            before = int((await mgmt.call(
                "runtime.clock", instance_id=info["id"], timeline_id=timeline_id))["clock"]["processed_world"])

            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="bad_time")
            reviewed = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="time-bad",
            )
            assert reviewed["status"] == "needs_review", reviewed
            assert any("seconds" in item for item in reviewed["errors"]), reviewed["errors"]

            after = int((await mgmt.call(
                "runtime.clock", instance_id=info["id"], timeline_id=timeline_id))["clock"]["processed_world"])
            assert after == before, "被拒的时间请求不许改时钟"
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id))["state_revision"] == 0
            assert [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                    if row["source"] == "trpg_action"] == []
        finally:
            await mgmt.close()

@pytest.mark.asyncio
async def test_gm_change_commits_without_action_or_plugin(tmp_path) -> None:
    """§十五：GM 直接变化不过行动、不过插件，但走同一条联合提交管线。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            changes = {
                "consequences": [{
                    "kind": "institution_state", "target": "off-1", "value": "vacant",
                    "expiry": "until_cleared", "certainty": "confirmed",
                }],
                "claims": [{"text": "职位出现变动", "source_id": "src-1", "audience": "public"}],
                "rule_state_patch": {
                    "ruleset_id": "fake-rules",
                    "base_state_revision": 0,
                    "operations": [{"path": "/actors/pc-1/hp", "op": "add", "value": 7}],
                },
            }
            committed = await mgmt.call(
                "trpg.gm.change", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                changes=changes, idempotency_key="gm-1",
            )
            assert committed["status"] == "committed", committed
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 1
            assert state["opaque_state"] == {"actors": {"pc-1": {"hp": 7}}}, "GM 的 patch 与行动路径同一套校验"
            events = [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                      if row["source"] == "gm_declaration"]
            assert len(events) == 1, "GM 直接变化要落自己的来源"
            assert harness.store.trpg_list("action", instance_id=info["id"], timeline_id=timeline_id) == [], \
                "GM 直接变化不制造行动行"

            dup = await mgmt.call(
                "trpg.gm.change", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                changes=changes, idempotency_key="gm-1",
            )
            assert dup["status"] == "duplicate"
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id))["state_revision"] == 1, "重放不改规则状态"

            stale_patch = dict(changes)
            stale_patch["rule_state_patch"] = {**changes["rule_state_patch"], "base_state_revision": 9}
            conflicted = await mgmt.call(
                "trpg.gm.change", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                changes=stale_patch, idempotency_key="gm-2",
            )
            assert conflicted["status"] == "conflict" and conflicted["action_id"] == ""
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id))["state_revision"] == 1, "冲突不落半条"
        finally:
            await mgmt.close()

async def _bump_manifest_version(plugin, version):
    path = Path(plugin)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["ruleset_version"] = version
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.asyncio
async def test_ruleset_converter_migrates_state_and_clears_the_gate(tmp_path) -> None:
    """§十六：插件声明转换器 → 转换由插件执行、核心记账；转完版本闸自动放行。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=action_id, idempotency_key="mig-0")
            await _bump_manifest_version(plugin, "2.0")

            migrated = await mgmt.call(
                "trpg.campaign.migrate", timeout=60.0,
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
            )
            assert migrated["status"] == "converted", migrated
            assert migrated["converter_id"] == "up1-2"
            assert migrated["old_ruleset_version"] == "1.0" and migrated["new_ruleset_version"] == "2.0"
            assert migrated["old_state_revision"] == 1 and migrated["new_state_revision"] == 2
            assert migrated["losses"] == []

            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 2
            assert state["state_ruleset_version"] == "2.0"
            assert state["opaque_state"]["actors"]["pc-1"]["hp"] == 20, "转换器说了算，核心不猜"
            row = await mgmt.call("trpg.campaign.info", instance_id=info["id"],
                                  timeline_id=timeline_id, campaign_id=campaign_id)
            assert row["ruleset_version"] == "2.0" and "规则版本转换" in row["note"]

            # 幂等：同一转换重放返回原记录，状态不再动
            replay = await mgmt.call(
                "trpg.campaign.migrate", timeout=60.0,
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
            )
            assert replay["status"] == "duplicate"
            assert replay["joint_commit_id"] == migrated["joint_commit_id"]
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id))["state_revision"] == 2

            # 版本闸放行：现在能拿新状态继续裁定
            action_id2, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            committed = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id2, idempotency_key="mig-1",
            )
            assert committed["status"] == "committed", committed
            assert committed["state_revisions"] == {"fake-rules": 3}
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id))["opaque_state"]["actors"]["pc-1"]["hp"] == 22
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_converter_failures_keep_original_state(tmp_path) -> None:
    """§十六：有信息损失要显式接受；转换失败 / 输出非法 → 停在待审，原状态可恢复。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=action_id, idempotency_key="cf-0")
            await _bump_manifest_version(plugin, "2.0")
            await mgmt.call("trpg.campaign.migrate", timeout=60.0,
                            instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id)

            def rule_state_snapshot(call):
                return call

            # 有损失：不显式接受就停在待审，原状态不动
            await _bump_manifest_version(plugin, "3.0")
            lossy = await mgmt.call(
                "trpg.campaign.migrate", timeout=60.0,
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
            )
            assert lossy["status"] == "needs_review", lossy
            assert lossy["losses"] == ["旧字段 blood_pool 没有对应项"]
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 2 and state["state_ruleset_version"] == "2.0", "待审不改状态"

            accepted = await mgmt.call(
                "trpg.campaign.migrate", timeout=60.0,
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                accept_losses=True,
            )
            assert accepted["status"] == "converted" and accepted["losses"]
            assert "已人工接受" in (await mgmt.call(
                "trpg.campaign.info", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id))["note"]

            # 转换器崩了：待审、原状态可恢复
            await _bump_manifest_version(plugin, "4.0")
            boom = await mgmt.call(
                "trpg.campaign.migrate", timeout=60.0,
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
            )
            assert boom["status"] == "needs_review" and boom["errors"], boom
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id))["state_revision"] == 3

            # 转换器输出非法：同样不许落盘
            await _bump_manifest_version(plugin, "5.0")
            bad = await mgmt.call(
                "trpg.campaign.migrate", timeout=60.0,
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
            )
            assert bad["status"] == "needs_review" and "opaque_state" in bad["errors"][0], bad
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id))["state_revision"] == 3

            # 没有可用的转换器 / 版本已经一致：说清楚，不瞎猜
            with pytest.raises(UmpError) as err:
                await mgmt.call(
                    "trpg.campaign.migrate", timeout=60.0,
                    instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                    converter_id="不存在",
                )
            assert "没有声明可用的转换器" in str(err.value)
        finally:
            await mgmt.close()

@pytest.mark.asyncio
async def test_audience_keeps_private_material_apart(tmp_path) -> None:
    """§十五：材料按受众呈现；GM 全见，其余只认精确匹配，插件原始裁定不进玩家面。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            created = await mgmt.call(
                "trpg.campaign.create",
                instance_id=info["id"], timeline_id=timeline_id,
                ruleset_id="fake-rules", ruleset_version="1.0", plugin_manifest=plugin,
                participants=["card-1"],
                scene={
                    "kind": "conflict", "location_refs": ["rl-1"], "participants": ["card-1"],
                    "public_facts": ["门外有脚印", {"text": "他袖口有血", "audience": "character:pc-1"}],
                    "private_views": {
                        "character:pc-1": {"note": "你认得这脚印"},
                        "character:pc-2": {"note": "这与你无关"},
                    },
                },
            )
            campaign_id = str(created["campaign_id"])
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            committed = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="aud-1", audience="character:pc-1",
            )
            assert committed["status"] == "committed" and committed["audience"] == "character:pc-1"

            async def view_as(audience):
                return await mgmt.call(
                    "trpg.scene.view", instance_id=info["id"], timeline_id=timeline_id,
                    campaign_id=campaign_id, audience=audience,
                )

            gm = await view_as("gm_only")
            assert [row["action_id"] for row in gm["recent"]] == [action_id]
            assert "resolution" in gm["recent"][0], "GM 面能看到插件原始裁定"
            assert set(gm["scene"]["private_views"]) == {"character:pc-1", "character:pc-2"}

            mine = await view_as("character:pc-1")
            assert [row["action_id"] for row in mine["recent"]] == [action_id]
            assert "resolution" not in mine["recent"][0], "插件私有裁定不进玩家面"
            assert mine["scene"]["private_views"] == {"character:pc-1": {"note": "你认得这脚印"}}
            assert "门外有脚印" in mine["scene"]["public_facts"]
            assert {"text": "他袖口有血", "audience": "character:pc-1"} in mine["scene"]["public_facts"]

            other = await view_as("character:pc-2")
            assert other["recent"] == [] and other["actions"] == [], "别人的行动不进你的面"
            assert other["scene"]["private_views"] == {"character:pc-2": {"note": "这与你无关"}}
            assert other["scene"]["public_facts"] == ["门外有脚印"], "按受众裁剪的公共事实不外泄"

            party = await view_as("public_party")
            assert party["recent"] == [], "character:pc-1 受众的行动不进公开面"
            assert party["scene"]["private_views"] == {}

            with pytest.raises(UmpError) as err:
                await view_as("围观群众")
            assert "未知受众" in str(err.value)
            with pytest.raises(UmpError):
                await mgmt.call(
                    "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                    action_id=action_id, idempotency_key="aud-2", audience="围观群众",
                )
        finally:
            await mgmt.close()

@pytest.mark.asyncio
async def test_resident_plugin_spawns_once_for_many_actions(tmp_path) -> None:
    """常驻形态（§五）：一个插件进程服务多次裁定；状态仍旧经快照进出，不靠进程内存。"""
    plugin = make_resident_plugin(tmp_path)
    spawns_file = Path(plugin).parent / "spawns.txt"
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)

            action_id, first = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            assert first["resolution"]["base"] == 0
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=action_id, idempotency_key="res-1")
            action_id2, second = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            assert second["resolution"]["base"] == 1, "第二次裁定拿到的是快照里的新版本"
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=action_id2, idempotency_key="res-2")

            assert spawns_file.read_text(encoding="utf-8").count("spawn") == 1, "常驻插件只该被拉起一次"
            assert len(harness.runtime.service.runtime.campaign.rule_sessions) == 1
            assert await harness.runtime.service.runtime.campaign.close_rule_sessions() == 1, "退出时能收掉"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_resident_plugin_recovers_after_crash(tmp_path) -> None:
    """崩溃恢复（§五）：进程死在「还没发请求」时重开一个；死在半路不许重跑裁定。"""
    plugin = make_resident_plugin(tmp_path)
    folder = Path(plugin).parent
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=action_id, idempotency_key="crash-1")

            pid = int((folder / "pid.txt").read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
            session = next(iter(harness.runtime.service.runtime.campaign.rule_sessions.values()))
            for _ in range(100):  # 等会话自己观察到进程没了（就用被测的那套 liveness）
                if not session.alive():
                    break
                await asyncio.sleep(0.05)
            assert not session.alive(), "插件进程没有被杀掉"

            action_id2, again = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            assert again["resolution"]["base"] == 1, "重开之后照样按快照裁定"
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=action_id2, idempotency_key="crash-2")
            assert (folder / "spawns.txt").read_text(encoding="utf-8").count("spawn") == 2, "死了要重开"
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id))["state_revision"] == 2
            # 常驻进程是核心的子进程：测试自己收了它，别留给已经关掉的循环（否则 Event loop is closed）
            assert await harness.runtime.service.runtime.campaign.close_rule_sessions() == 1
        finally:
            await mgmt.close()
