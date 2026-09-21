"""对外接口的行为验收（WORLD_RUNTIME_INTERFACE_SPEC §四~§六、§十一）。

真 WebSocket + 真 SQLite + 真实例：每个接口都在管理面上真调一次，读数来自返回值。
覆盖：读接口的字段与作用域、快照的不可用态、认知投影只给观察者自己的东西、
历史的游标与过滤、世代检查的四种结果、任务失效不动历史、变化接口的预览/提交/幂等/冲突。
"""

from __future__ import annotations

import json

import pytest

from conftest import open_mgmt, running_core
from isekai_core.ump import UmpError
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401


async def _boot(mgmt, harness, *, activate: bool = True):
    """真实例 + 真激活（世界时钟走起来，快照水位才落在「已完成」上）。"""
    info, timeline_id, card = make_instance(
        harness.store, harness.runtime.service.runtime, moment=DAY * 1500
    )
    if activate:
        await mgmt.call("runtime.activate", instance_id=info["id"], timeline_id=timeline_id)
        await mgmt.call("runtime.advance", instance_id=info["id"], timeline_id=timeline_id, max_batches=4)
    return info, timeline_id, card


@pytest.mark.asyncio
async def test_scope_inspect_and_snapshot_read(tmp_path) -> None:
    """§4.1 字段族 + §4.2 快照句柄；冻结线取快照要明确不可用，不拿旧状态冒充当前。"""
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = await _boot(mgmt, harness)
            scope = await mgmt.call("runtime.scope.inspect", instance_id=info["id"], timeline_id=timeline_id)
            assert scope["status"] == "ok"
            for key in ("timeline_state", "world_time", "processed_watermark", "target_watermark",
                        "revision", "runtime_generation", "ruleset_version", "available_actions"):
                assert key in scope, f"§4.1 缺字段 {key}: {sorted(scope)}"
            assert scope["timeline_state"] == "active", scope
            assert "commit" in scope["available_actions"]
            assert scope["processed_watermark"] == scope["revision"] == scope["world_time"]

            snap = await mgmt.call(
                "runtime.snapshot.read", instance_id=info["id"], timeline_id=timeline_id,
                request={"characters": [_card], "include": ["time", "current_activity", "claims"]},
            )
            assert snap["status"] == "ok", snap
            assert snap["snapshot_id"] == f"snap-{scope['processed_watermark']}"
            assert snap["expires_at"] > 0
            payload = snap["payload"][_card]
            assert set(payload) == {"time", "current_activity", "claims"}, payload
            assert payload["time"] == snap["revision"], "快照的 time 就是这次读取的版本"

            bad = await mgmt.call(
                "runtime.snapshot.read", instance_id=info["id"], timeline_id=timeline_id,
                request={"characters": [_card], "include": ["未来计划"]},
            )
            assert bad["status"] == "rejected" and "未知的 include" in bad["reason"]

            await mgmt.call("runtime.freeze", instance_id=info["id"], timeline_id=timeline_id)
            frozen = await mgmt.call("runtime.scope.inspect", instance_id=info["id"], timeline_id=timeline_id)
            assert frozen["timeline_state"] == "frozen" and frozen["available_actions"] == ["read", "activate", "fork", "rollback"]
            refused = await mgmt.call(
                "runtime.snapshot.read", instance_id=info["id"], timeline_id=timeline_id,
                request={"characters": [_card]},
            )
            assert refused["status"] == "not_ready" and "frozen" in refused["reason"]
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_cognition_project_is_observer_scoped(tmp_path) -> None:
    """§4.3：投影只给观察者自己的经历 / 获知；没有 observer 直接拒。"""
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, card = await _boot(mgmt, harness)
            projected = await mgmt.call(
                "runtime.cognition.project", instance_id=info["id"], timeline_id=timeline_id,
                observer_id=card, query={"purpose": "dialogue"},
            )
            assert projected["status"] == "ok"
            assert projected["observer_id"] == card
            assert projected["observed_revision"] <= (await mgmt.call(
                "runtime.scope.inspect", instance_id=info["id"], timeline_id=timeline_id))["processed_watermark"]
            for key in ("observations", "claims", "known_unknowns", "source_refs"):
                assert isinstance(projected[key], list), key

            stranger = await mgmt.call(
                "runtime.cognition.project", instance_id=info["id"], timeline_id=timeline_id,
                observer_id="cc-不存在",
            )
            assert stranger["status"] == "ok" and stranger["observations"] == [] and stranger["claims"] == []

            bad = await mgmt.call(
                "runtime.cognition.project", instance_id=info["id"], timeline_id=timeline_id,
                observer_id=card, query={"purpose": "算命"},
            )
            assert bad["status"] == "rejected" and "未知 purpose" in bad["reason"]
            # 缺 observer：信封里明确 rejected（作用域是显式的，接口不猜「当前角色」）
            missing = await mgmt.call("runtime.cognition.project", instance_id=info["id"], timeline_id=timeline_id)
            assert missing["status"] == "rejected" and "observer_id" in missing["reason"], missing
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_subject_state_and_history_read(tmp_path) -> None:
    """§4.4 状态投影的受众分层 + §4.5 历史读数（游标往回翻不重复）。"""
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, card = await _boot(mgmt, harness)
            gm = await mgmt.call("runtime.subject.state.read", instance_id=info["id"],
                                 timeline_id=timeline_id, subject_id=card, audience="gm_only")
            assert gm["subject"]["subject_id"] == card and "all_units" in gm["subject"]
            public = await mgmt.call("runtime.subject.state.read", instance_id=info["id"],
                                     timeline_id=timeline_id, subject_id=card, audience="public_party")
            assert "all_units" not in public["subject"] and "knowledge" not in public["subject"]
            assert public["subject"]["state_at_revision"] == gm["subject"]["state_at_revision"]

            # 先写一条事实，历史里就该看得见
            changes = [{"id": "c1", "kind": "condition", "operation": "set", "certainty": "confirmed",
                        "target_refs": [card], "value": "封堤", "expiry": "until_cleared"}]
            preview = await mgmt.call(
                "runtime.change.preview", instance_id=info["id"], timeline_id=timeline_id, changes=changes,
            )
            assert preview["status"] == "ok", preview
            committed = await mgmt.call(
                "runtime.change.commit", instance_id=info["id"], timeline_id=timeline_id,
                changes=changes, idempotency_key="iface-hist-1", preview_id=preview["preview_id"],
            )
            assert committed["status"] == "ok", committed

            # 只有事件帧、没有事实效果的批次：预览与提交都如实说「没有能落成事实的内容」
            bare = [{"id": "c9", "kind": "world_event", "operation": "create",
                     "certainty": "confirmed", "value": "堤上换了新的守夜安排"}]
            bare_preview = await mgmt.call("runtime.change.preview", instance_id=info["id"],
                                           timeline_id=timeline_id, changes=bare)
            assert bare_preview["status"] == "rejected" and bare_preview["rejected_candidates"], bare_preview

            page = await mgmt.call("runtime.history.read", instance_id=info["id"],
                                   timeline_id=timeline_id, limit=5)
            assert page["status"] == "ok" and page["items"], page
            assert page["items"] == sorted(page["items"], key=lambda item: (item["world_seconds"], item["seq"]))
            assert any(item["ref"] == committed["commit_id"] for item in page["items"])
            older = await mgmt.call("runtime.history.read", instance_id=info["id"], timeline_id=timeline_id,
                                    limit=5, cursor=page["next_cursor"])
            assert all(item["world_seconds"] <= int(page["next_cursor"].split(":")[0]) for item in older["items"])
            filtered = await mgmt.call("runtime.history.read", instance_id=info["id"], timeline_id=timeline_id,
                                       limit=5, filters={"source": "iface-audit"})
            assert filtered["items"] == []
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_generation_check_and_task_invalidate(tmp_path) -> None:
    """§6.3 世代检查的四种结果 + §6.4 失效只提升世代、不动已固化历史。"""
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, card = await _boot(mgmt, harness)
            scope = await mgmt.call("runtime.scope.inspect", instance_id=info["id"], timeline_id=timeline_id)
            generation = scope["runtime_generation"]
            ok = await mgmt.call("runtime.generation.check", instance_id=info["id"], timeline_id=timeline_id,
                                 snapshot_id=f"snap-{scope['processed_watermark']}",
                                 runtime_generation=generation, source_refs=[card])
            assert ok["status"] == "valid", ok
            stale = await mgmt.call("runtime.generation.check", instance_id=info["id"], timeline_id=timeline_id,
                                    runtime_generation=generation - 3)
            assert stale["status"] == "stale" and "世代不一致" in stale["reason"]

            facts_before = len(harness.store.event_window(info["id"], timeline_id, until=10**15))
            bumped = await mgmt.call("runtime.task.invalidate", instance_id=info["id"], timeline_id=timeline_id,
                                     reason="审计：换一批派生任务")
            assert bumped["runtime_generation"] == generation + 1
            assert bumped["invalidated_generation"] == generation
            assert len(harness.store.event_window(info["id"], timeline_id, until=10**15)) == facts_before, \
                "任务失效不得删除事实（撤销要走回滚）"
            again = await mgmt.call("runtime.generation.check", instance_id=info["id"], timeline_id=timeline_id,
                                    runtime_generation=generation)
            assert again["status"] == "stale", "旧世代的检查必须转 stale"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_change_preview_and_commit_contract(tmp_path) -> None:
    """§5.2/§5.3：预览不改世界、提交原子且幂等、非法意图与失效预览都被挡。"""
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, card = await _boot(mgmt, harness)
            before = len(harness.store.event_window(info["id"], timeline_id, until=10**15))
            changes = [{"id": "c-1", "kind": "condition", "operation": "set", "certainty": "confirmed",
                        "target_refs": [card], "value": "封堤", "expiry": "until_cleared"}]
            preview = await mgmt.call("runtime.change.preview", instance_id=info["id"],
                                      timeline_id=timeline_id, changes=changes)
            assert preview["status"] == "ok", preview
            assert preview["accepted_candidates"] == ["c-1"] and not preview["rejected_candidates"]
            assert preview["projected_effects"], preview
            assert len(harness.store.event_window(info["id"], timeline_id, until=10**15)) == before, \
                "预览不得改变世界"

            committed = await mgmt.call("runtime.change.commit", instance_id=info["id"],
                                        timeline_id=timeline_id, changes=changes,
                                        idempotency_key="iface-1", preview_id=preview["preview_id"])
            assert committed["status"] == "ok" and committed["event_refs"] == [committed["commit_id"]]
            after = len(harness.store.event_window(info["id"], timeline_id, until=10**15))
            assert after == before + 1

            replay = await mgmt.call("runtime.change.commit", instance_id=info["id"], timeline_id=timeline_id,
                                     changes=changes, idempotency_key="iface-1")
            assert replay["status"] == "duplicate" and replay["commit_id"] == committed["commit_id"]
            assert len(harness.store.event_window(info["id"], timeline_id, until=10**15)) == after, \
                "重放不得再落一条"

            stale_preview = await mgmt.call(
                "runtime.change.commit", instance_id=info["id"], timeline_id=timeline_id,
                changes=[{"id": "c-2", "kind": "condition", "operation": "set", "certainty": "confirmed",
                          "target_refs": [card], "value": "撤封", "expiry": "until_cleared"}],
                idempotency_key="iface-2", preview_id=preview["preview_id"],
            )
            assert stale_preview["status"] == "conflict" and "预览已失效" in stale_preview["reason"]

            conflict = await mgmt.call("runtime.change.commit", instance_id=info["id"], timeline_id=timeline_id,
                                       changes=changes, idempotency_key="iface-3", expected_revision=1)
            assert conflict["status"] == "conflict" and conflict["expected"] == 1

            refused = await mgmt.call("runtime.change.commit", instance_id=info["id"], timeline_id=timeline_id,
                                      changes=[{"id": "c-3", "kind": "resource_change", "operation": "add",
                                                "certainty": "confirmed"}], idempotency_key="iface-4")
            assert refused["status"] == "rejected" and "资源量" in refused["rejected_candidates"][0]["reason"]

            pending = await mgmt.call("runtime.change.commit", instance_id=info["id"], timeline_id=timeline_id,
                                      changes=[{"id": "c-4", "kind": "condition", "operation": "set",
                                                "certainty": "candidate", "target_refs": [card], "value": "封堤"}],
                                      idempotency_key="iface-5")
            assert pending["status"] == "needs_review", pending

            # 缺幂等键：会改变状态的调用一律拒（信封里说清楚，不静默提交）
            no_key = await mgmt.call("runtime.change.commit", instance_id=info["id"], timeline_id=timeline_id,
                                     changes=changes)
            assert no_key["status"] == "rejected" and "idempotency_key" in no_key["reason"]
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_spec_names_and_aliases_reach_the_same_capability(tmp_path) -> None:
    """§十二 对照表：规范名必须真能调通，且与实现名等价。"""
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = await _boot(mgmt, harness)
            mark = await mgmt.call("runtime.commit", instance_id=info["id"], timeline_id=timeline_id, note="别名点")
            by_alias = await mgmt.call("runtime.timeline.fork", instance_id=info["id"], timeline_id=timeline_id,
                                       commit_id=mark["commit"]["id"], name="别名分支")
            by_real = await mgmt.call("runtime.fork", instance_id=info["id"], timeline_id=timeline_id,
                                      commit_id=mark["commit"]["id"], name="直连分支")
            assert by_alias["timeline"]["source_commit"] == by_real["timeline"]["source_commit"]

            advanced = await mgmt.call("runtime.time.advance", instance_id=info["id"], timeline_id=timeline_id,
                                       duration=900, reason="接口推进", source="world_process")
            assert advanced["state"] == "current" and advanced["consumed_seconds"] == 900

            rolled = await mgmt.call("runtime.timeline.rollback", instance_id=info["id"], timeline_id=timeline_id,
                                     commit_id=mark["commit"]["id"], confirm=True)
            assert rolled["world"] == mark["commit"]["moment"]

            # 规则状态读接口只在战役里成立：别名指到 trpg 命名空间，没有战役时报错但不装作空值
            with pytest.raises(UmpError):
                await mgmt.call("runtime.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                campaign_id="cp-不存在")
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_scope_is_explicit_and_envelope_is_uniform(tmp_path) -> None:
    """§3.1 拒绝隐含作用域；§3.2 每个响应都带同一套信封。"""
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, card = await _boot(mgmt, harness)
            for op, extra in (
                ("runtime.scope.inspect", {}),
                ("runtime.history.read", {"limit": 3}),
                ("runtime.subject.state.read", {"subject_id": card}),
            ):
                with pytest.raises(UmpError):
                    await mgmt.call(op, **{**extra})  # 不给 instance/timeline
            envelope = await mgmt.call("runtime.scope.inspect", instance_id=info["id"], timeline_id=timeline_id)
            for key in ("status", "instance_id", "timeline_id", "observed_revision", "world_time",
                        "processed_watermark", "runtime_generation"):
                assert key in envelope, key
            assert json.dumps(envelope)  # 信封必须可序列化回给调用方
        finally:
            await mgmt.close()
