"""U4 的两条业务边界（USER_INTERFACE_DESIGN §8.5 / §8.1）：真 WS + 真 SQLite + 真插件清单。

1) 规则登记簿与通道插件分开：扫的是清单、登记是本机事实、同身份同版本换内容不许静默覆盖；
2) 被战役引用的规则版本不能移除（要能列出战役名），停用只阻止之后的调用；
3) 战役与场景的显示名跟着数据走（不拿内部标识当标题）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from conftest import open_mgmt, running_core
from isekai_core.ump import UmpError
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401
from test_trpg_campaign import make_plugin


def _tiny_plugin(tmp_path: Path, *, folder: str = "tiny_rules", protocol: str = "isekai.trpg.rules/1",
                 entry_file: str = "main.py") -> str:
    """手写一份最小规则清单（不跑它，只看登记簿怎么读）。"""
    where = tmp_path / folder
    where.mkdir(exist_ok=True)
    (where / "main.py").write_text("import json, sys\nprint('{}')\n", encoding="utf-8")
    manifest = where / "manifest.json"
    manifest.write_text(
        json.dumps({
            "id": "tiny", "name": "小微规则", "version": "0.2.0", "ruleset_version": "0.2.0",
            "protocol": protocol, "entry": [sys.executable, entry_file],
            "modes": ["stateless_resolver"], "state_schema": "tiny.state/1",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(manifest)


async def test_scan_notes_and_register_is_a_local_fact(tmp_path) -> None:
    """扫目录只是读数；登记之后才是本机事实；同身份同版本换内容必须报错（§8.5）。"""
    manifest = _tiny_plugin(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            scanned = await mgmt.call("rules.scan", dir=str(Path(manifest).parent))
            assert scanned["candidates"], scanned
            head = scanned["candidates"][0]
            assert head["ruleset_id"] == "tiny" and head["ruleset_version"] == "0.2.0", head
            assert head["status"] == "available" and head["state_schema"] == "tiny.state/1", head
            listed = await mgmt.call("rules.list")
            assert listed["plugins"] == [], "扫描不应该顺手登记"

            first = await mgmt.call("rules.register", manifest_path=manifest)
            assert first["plugin"]["enabled"] is True
            assert first["plugin"]["status"] == "available", first
            again = await mgmt.call("rules.register", manifest_path=manifest)
            assert again["updated"] is True, "同内容重复登记是更新，不是冲突"

            # 换了内容的同一个版本：不许静默覆盖（要换内容请改版本号）
            payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
            payload["state_schema"] = "tiny.state/2"
            Path(manifest).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            problem = None
            try:
                await mgmt.call("rules.register", manifest_path=manifest)
            except UmpError as exc:
                problem = str(exc)
            assert problem and "版本号" in problem, problem

            listed = await mgmt.call("rules.list")
            assert len(listed["plugins"]) == 1, listed
            assert listed["plugins"][0]["changed_since_registered"] is True, listed
        finally:
            await mgmt.close()


async def test_referenced_rules_cannot_be_removed(tmp_path, world, store) -> None:
    """被战役引用的规则版本：移除要列出战役并拒绝；停用只阻止之后的调用（§8.5）。"""
    manifest = make_plugin(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            await mgmt.call("rules.register", manifest_path=manifest)
            info, timeline_id, _card = make_instance(
                h.store, h.runtime.service.runtime, moment=DAY * 1500 + 30000
            )
            created = await mgmt.call(
                "trpg.campaign.create", instance_id=info["id"], timeline_id=timeline_id,
                name="北堤调查", ruleset_id="fake-rules", ruleset_version="1.0",
                plugin_manifest=manifest, participants=["card-1"], host_mode="autonomous",
            )
            campaign_id = str(created["campaign_id"])
            assert created["name"] == "北堤调查", created

            listed = await mgmt.call("rules.list")
            row = [item for item in listed["plugins"] if item["ruleset_id"] == "fake-rules"][0]
            assert row["referenced_count"] == 1, row
            assert row["referenced_by"][0]["name"] == "北堤调查", row

            blocked = None
            try:
                await mgmt.call("rules.remove", ruleset_id="fake-rules", ruleset_version="1.0")
            except UmpError as exc:
                blocked = str(exc)
            assert blocked and "北堤调查" in blocked, blocked

            disabled = await mgmt.call("rules.disable", ruleset_id="fake-rules", ruleset_version="1.0")
            assert disabled["plugin"]["status"] == "disabled", disabled
            assert disabled["plugin"]["status_text"] == "未启用", disabled

            # 战役仍在（停用不撤回已经算出的结果），所以仍然不许移除
            still = None
            try:
                await mgmt.call("rules.remove", ruleset_id="fake-rules", ruleset_version="1.0")
            except UmpError as exc:
                still = str(exc)
            assert still and "不能移除" in still, still
            assert str(campaign_id) in json.dumps(
                (await mgmt.call("rules.list"))["plugins"], ensure_ascii=False
            ), "登记簿要一直能指出是谁在用"
        finally:
            await mgmt.close()


async def test_missing_entry_and_wrong_protocol_are_reported(tmp_path) -> None:
    """依赖缺失 / 协议不兼容：扫描就如实说，登记直接拒（不执行未知程序）。"""
    missing = _tiny_plugin(tmp_path, folder="missing_rules", entry_file="nope.py")
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            scanned = await mgmt.call("rules.scan", dir=str(Path(missing).parent))
            head = scanned["candidates"][0]
            assert head["status"] == "missing" and "入口" in head["reason"], head
            refused = None
            try:
                await mgmt.call("rules.register", manifest_path=missing)
            except UmpError as exc:
                refused = str(exc)
            assert refused and "入口" in refused, refused

            wrong = _tiny_plugin(tmp_path, folder="wrong_rules", protocol="isekai.trpg.rules/9")
            scanned = await mgmt.call("rules.scan", dir=str(Path(wrong).parent))
            head = scanned["candidates"][0]
            assert head["status"] == "incompatible" and "协议" in head["reason"], head
        finally:
            await mgmt.close()


async def test_campaign_and_scene_keep_their_display_names(tmp_path, world, store) -> None:
    """§8.1：战役与场景的显示名进各自的元数据，读取面直接给名称（不把 id 当标题）。"""
    manifest = make_plugin(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            info, timeline_id, _card = make_instance(
                h.store, h.runtime.service.runtime, moment=DAY * 1500 + 30000
            )
            created = await mgmt.call(
                "trpg.campaign.create", instance_id=info["id"], timeline_id=timeline_id,
                name="北堤调查", ruleset_id="fake-rules", ruleset_version="1.0",
                plugin_manifest=manifest, participants=["card-1"], host_mode="assisted",
                scene={"kind": "exploration", "name": "夜里的北堤", "brief": "潮声比白天更近"},
            )
            campaign_id = str(created["campaign_id"])
            listed = await mgmt.call(
                "trpg.campaign.list", instance_id=info["id"], timeline_id=timeline_id
            )
            assert listed["campaigns"][0]["name"] == "北堤调查", listed

            view = await mgmt.call(
                "trpg.scene.view", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id, audience="public_party",
            )
            assert view["scene"]["name"] == "夜里的北堤", view["scene"]
            assert view["scene"]["brief"] == "潮声比白天更近", view["scene"]
            assert view["campaign"]["name"] == "北堤调查", view.get("campaign")
        finally:
            await mgmt.close()
