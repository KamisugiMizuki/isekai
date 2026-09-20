"""短期反应状态域（WORLD_RUNTIME_SPEC §11.1 / 附录 B #19、#26）。

判据：有来源才有记录；同一来源不叠加；解除后果后不再被引用；回滚撤销派生状态；分批与连续等价。
"""

from __future__ import annotations

from isekai_core.config import load_config
from isekai_core.runtime import reaction
from isekai_core.world import ops as world_ops
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401  夹具定义在那边


def _effect(instance_id: str, timeline_id: str, *, ident: str, kind: str, at: int, target: str):
    return {
        "id": ident,
        "instance_id": instance_id,
        "timeline_id": timeline_id,
        "event_id": "ev-x",
        "target": target,
        "kind": kind,
        "family": "",
        "from_world": at,
        "expiry": "until_cleared",
        "recovery": "",
        "active": 1,
        "cleared_at": None,
    }


def test_id_is_derived_from_source() -> None:
    same = reaction.reaction_id("event_effect", "fx-1")
    assert same == reaction.reaction_id("event_effect", "fx-1")
    assert same != reaction.reaction_id("experience", "fx-1")
    assert same != reaction.reaction_id("event_effect", "fx-2")


def test_merge_does_not_stack_and_opposite_evidence_lowers() -> None:
    row = reaction.from_effect(
        _effect("in-1", "tl-1", ident="fx-1", kind="route_blocked", at=100, target="rl-1"),
        character_id="ch-1",
    )
    assert row["intensity"] == "high" and row["stage"] == "candidate"

    repeat = reaction.merge(row, dict(row))
    assert repeat["id"] == row["id"] and repeat["intensity"] == "high", "重复素材不叠强度"

    opposite = reaction.merge(row, {**row, "direction": -1, "tendency": "她不再这么想了"})
    assert opposite["intensity"] == "mid" and opposite["stage"] == "fading", "相反依据降低一档"
    again = reaction.merge(opposite, {**row, "direction": -1})
    assert again["intensity"] == "mid" and again["stage"] == "fading", "同一份相反素材再来一次不继续加码"
    # 终止走上层路径：依据被解除 → 减弱 → 失效（advance 管收尾，merge 只管同源更新）
    assert reaction.advance(again, watermark=600, cleared={"fx-1"})["stage"] == "expired", "减弱后依据被解除 → 失效"


def test_advance_lifecycle_is_deterministic() -> None:
    row = reaction.from_experience(
        {"id": "ex-1", "instance_id": "in-1", "timeline_id": "tl-1", "kind": "life",
         "summary": "昨天夜里没睡好", "world_seconds": 500, "confidence": 1.0},
        character_id="ch-1",
    )
    before_start = reaction.advance(row, watermark=400)
    assert before_start["stage"] == "candidate", "还没到开始水位就只是候选"
    adopted = reaction.advance(row, watermark=600)
    assert adopted["stage"] == "adopted"
    active = reaction.advance(adopted, watermark=600)
    assert active["stage"] == "active"
    steady = reaction.advance(active, watermark=600)
    assert steady == active, "没有新依据就不变（幂等）"
    fading = reaction.advance(active, watermark=700, cleared={"ex-1"})
    assert fading["stage"] == "fading"
    expired = reaction.advance(fading, watermark=700, cleared={"ex-1"})
    assert expired["stage"] == "expired"
    assert reaction.advance(expired, watermark=900, cleared={"ex-1"})["stage"] == "expired", "失效后不再变化"


def test_tendency_block_only_live_and_capped() -> None:
    def make(index: int, stage: str) -> dict:
        row = reaction.from_experience(
            {"id": f"ex-{index}", "instance_id": "in", "timeline_id": "tl", "kind": "life",
             "summary": f"第 {index} 件事", "world_seconds": 100, "confidence": 1.0},
            character_id="ch",
        )
        return {**row, "stage": stage, "tendency": f"倾向 {index}"}

    rows = [make(1, "active"), make(2, "expired"), make(3, "fading"), make(4, "paused"), make(5, "active")]
    block = reaction.tendency_block(rows, watermark=200)
    assert "倾向 2" not in block, "失效的不再被引用"
    assert block.count("- ") <= 3, "最多三条，且顺序稳定"
    assert reaction.tendency_block(rows, watermark=200) == block


def test_reaction_reaches_snapshot_and_prompt(store, world, tmp_path) -> None:
    cfg = load_config(tmp_path)
    info, timeline_id, character_id = make_instance(store, world)
    card = world.card_of(
        store.instance_get(info["id"]), character_id, timeline_id=timeline_id, world_seconds=0
    )
    role_id = str(card.get("role_id") or "")
    at = int(store.clock_get(timeline_id)["processed_world"])
    effect = _effect(info["id"], timeline_id, ident="fx-prompt", kind="route_blocked", at=at, target=role_id)
    cards = world.cards(store.instance_get(info["id"]), timeline_id=timeline_id, world_seconds=at)
    from isekai_core.runtime.service import _reaction_rows

    rows = _reaction_rows(cards, effects=[effect], experiences=[])
    assert rows and rows[0]["source_kind"] == "event_effect" and rows[0]["character_id"] == character_id
    ok = store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(store.clock_get(timeline_id)["generation"]),
        processed_world=at,
        catching_up=False,
        effects=[effect],
        reactions=rows,
    )
    assert ok is True

    # 按同一水位读：反应已转活跃，进快照与生成上下文；只给倾向，不暴露强度 / 来源字段
    snapshot = world.character_snapshot(info["id"], timeline_id, character_id, world_seconds=at + 60)
    assert snapshot["reactions"], snapshot
    assert snapshot["reaction_tendency"].startswith("- ")
    assert "intensity" not in snapshot["reaction_tendency"]

    context = world.turn_context(
        {
            "id": "s-probe",
            "instance_id": info["id"],
            "timeline_id": timeline_id,
            "character_id": character_id,
            "channel_id": "builtin",
            "thread_id": "th-probe",
        },
        world_seconds=at + 60,
    )
    assert "她眼下的处境" in context["prompt"], context["prompt"][:200]
    assert "rx-" not in context["prompt"], "内部标识不进提示词"


def test_dialog_note_roundtrip(store, world, tmp_path) -> None:
    cfg = load_config(tmp_path)
    info, timeline_id, character_id = make_instance(store, world)
    at = int(store.clock_get(timeline_id)["processed_world"])
    out = world_ops.dispatch(
        cfg,
        store,
        "reaction.note",
        {
            "instance_id": info["id"],
            "timeline_id": timeline_id,
            "character_id": character_id,
            "message_id": "m-1",
            "world_seconds": at,
            "direction": 1,
            "tendency": "刚才那句话说出口之后她心里松了一点",
            "basis": "对话里定下的事",
        },
    )
    assert out["noted"]
    rows = store.reaction_list(info["id"], timeline_id, character_id=character_id)
    assert any(str(row["id"]) == str(out["reaction"]) for row in rows)
    again = world_ops.dispatch(
        cfg,
        store,
        "reaction.note",
        {
            "instance_id": info["id"],
            "timeline_id": timeline_id,
            "character_id": character_id,
            "message_id": "m-1",
            "world_seconds": at,
            "direction": 1,
        },
    )
    assert again["reaction"] == out["reaction"], "同一来源只建一条"
    assert len(store.reaction_list(info["id"], timeline_id, character_id=character_id)) == 1


def test_rollback_drops_reactions(store, world) -> None:
    info, timeline_id, character_id = make_instance(store, world)
    commit = world.commit(info["id"], timeline_id, kind="manual", note="反应之前")["id"]
    at = int(store.clock_get(timeline_id)["processed_world"])
    row = reaction.from_dialog(
        instance_id=info["id"],
        timeline_id=timeline_id,
        character_id=character_id,
        message_id="m-rollback",
        world_seconds=at,
        tendency="先把这件事记在心里",
    )
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(store.clock_get(timeline_id)["generation"]),
        processed_world=at,
        catching_up=False,
        reactions=[row],
    )
    assert store.reaction_list(info["id"], timeline_id)
    world.rollback(info["id"], timeline_id, commit_id=commit)
    assert not store.reaction_list(info["id"], timeline_id), "回滚撤销派生状态"
