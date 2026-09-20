"""多角色披露（SESSION_CORE_SPEC §七，阶段 5）行为验收：隔离 / 授权 / 撤回只有回滚。"""

from __future__ import annotations

import json

from isekai_core.runtime.service import RuntimeService, RuntimeStateError
from isekai_core.world.ops import ASYNC_OPS, SYNC_OPS
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _service(store, **over) -> RuntimeService:
    params = {
        "instance_tokens_per_day": 400_000,
        "timeline_tokens_per_day": 150_000,
        "task_tokens_per_day": 60_000,
        "autocommit_enabled": False,
    }
    params.update(over)
    return RuntimeService(store, **params)


def _two_characters(store, world_service, *, moment=DAY * 1500):
    """两个角色同线：各自独立会话与卡片（补卡装配自阶段 2 起可用）。"""
    info, timeline_id, first = make_instance(store, world_service, moment=moment)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    second_card = sample_card(sample_package(), name="堤砚")
    world_service.add_character(
        info["id"], timeline_id, second_card, now_real=1.7e9 + 2 * DAY,
        joined_world=int(store.clock_get(timeline_id)["processed_world"]), note="第二人",
    )
    second = str((second_card.get("meta") or {}).get("card_id"))
    return info, timeline_id, first, second


def _say(store, info, timeline_id, character_id, *, env: str, world_seconds: int, text: str, reply: str):
    """固化一轮对话（不经过 LLM，直接落库）：返回回复的 message_id。"""
    session = store.session_ensure(info["id"], timeline_id, character_id)
    store.inbound_put(
        session_id=session["id"], channel_id="cli-dev", thread_id=f"t-{character_id}",
        env_id=env, text=text, binding_version=1,
    )
    outbound = store.outbound_put(
        session_id=session["id"], message_id=f"m-{env}", reply_to=env, covers=[env],
        batches=[[reply]], target_channel="cli-dev", target_thread=f"t-{character_id}",
        binding_version=1, binding_token="tok",
    )
    store.session_ensure(info["id"], timeline_id, character_id)
    return outbound["message_id"], session


def test_characters_are_isolated_by_default(store) -> None:
    """角色间默认隔离：A 的对话不进 B 的召回，B 也读不到 A 的记忆（§7.1 / DESIGN 验收）。"""
    world_service = _service(store)
    info, timeline_id, first, second = _two_characters(store, world_service)
    _say(store, info, timeline_id, first, env="env-a1", world_seconds=10,
         text="你那边堤上的事怎么样", reply="堤长身故，牌停了，别外传")
    store.memory_add({
        "id": "mm-a1", "instance_id": info["id"], "timeline_id": timeline_id, "character_id": first,
        "text": "她答应过替堤长压着那份信报", "kind": "promise",
        "sources": [{"kind": "intent", "ref": "in-a"}], "happened_world": 0, "learned_world": 0,
        "recorded_world": 0, "semantic_watermark": 0, "strength": 0.9, "confidence": 0.9,
    })
    seen = world_service.recall(info["id"], timeline_id, second, topic="堤长 信报")
    assert seen["ids"] == [], "B 看不到 A 的记忆"
    assert "堤长身故" not in json.dumps(seen, ensure_ascii=False), "A 的回复内容不进 B"
    context = world_service.turn_context(
        {"instance_id": info["id"], "timeline_id": timeline_id, "character_id": second}, topic="堤长"
    )
    assert "堤长身故" not in context["prompt"], "扮演定义里也没有 A 的私聊"


def test_user_mention_is_not_a_read_grant(store) -> None:
    """用户转述不等于读取授权（§7.1）：聊天里提到 A 的话不开放 A 的历史。"""
    world_service = _service(store)
    info, timeline_id, first, second = _two_characters(store, world_service)
    _say(store, info, timeline_id, first, env="env-a2", world_seconds=10,
         text="堤上的事", reply="信报上抄到堤长身故，接任未毕")
    _say(store, info, timeline_id, second, env="env-b1", world_seconds=20,
         text="堤禾跟我说过堤长出事了对吧", reply="他跟你说的？我这边没听人提过")
    assert world_service.disclosures(info["id"], timeline_id) == [], "普通聊天不产生授权"
    seen = world_service.recall(info["id"], timeline_id, second, topic="堤长 信报")
    assert seen["ids"] == [], "没有授权就照样看不到"
    assert world_service.disclosed_fragments(info["id"], timeline_id, second) == []


def test_explicit_disclosure_opens_only_that_fragment(store) -> None:
    """明确披露后 B 才可见，而且只看到被披露的那一段（授权不是复制整库）（§7.1）。"""
    world_service = _service(store)
    info, timeline_id, first, second = _two_characters(store, world_service)
    reply_id, _ = _say(store, info, timeline_id, first, env="env-a3", world_seconds=10,
                       text="堤上的事", reply="信报上抄到堤长身故，接任未毕")
    store.memory_add({
        "id": "mm-a2", "instance_id": info["id"], "timeline_id": timeline_id, "character_id": first,
        "text": "她自己压着一份没说出去的名单", "kind": "fragment",
        "sources": [{"kind": "experience", "ref": "ex-a"}], "happened_world": 0, "learned_world": 0,
        "recorded_world": 0, "semantic_watermark": 0, "strength": 0.9, "confidence": 0.9,
    })
    grant = world_service.disclose(
        info["id"], timeline_id, from_character=first, to_character=second, refs=[reply_id], note="让堤砚知道"
    )
    assert grant["reused"] is False and grant["granted_world"] >= 0
    fragments = world_service.disclosed_fragments(info["id"], timeline_id, second)
    assert len(fragments) == 1 and "堤长身故" in fragments[0]["text"]
    seen = world_service.recall(info["id"], timeline_id, second, topic="堤长")
    assert any("堤长身故" in str(item.get("text") or "") for item in seen["entries"]), "披露后可见"
    assert not any("名单" in str(item.get("text") or "") for item in seen["entries"]), "没披露的依旧不可见"
    entry = next(item for item in seen["entries"] if "堤长身故" in str(item.get("text") or ""))
    assert "转述" in entry["text"] and json.loads(entry["sources"])[0]["source_role"] == "other_character", (
        "记成转述，不是亲历"
    )
    context = world_service.turn_context(
        {"instance_id": info["id"], "timeline_id": timeline_id, "character_id": second}, topic="堤长"
    )
    assert "联络者明确给你看过这些转述" in context["prompt"], "扮演定义带上披露块"


def test_disclosure_rejects_vague_or_foreign_refs(store) -> None:
    """范围必须明确且属于来源角色；含糊或跨角色的引用一律拒绝（§7.1）。"""
    world_service = _service(store)
    info, timeline_id, first, second = _two_characters(store, world_service)
    _say(store, info, timeline_id, first, env="env-a4", world_seconds=10, text="甲的话", reply="甲的回")
    _say(store, info, timeline_id, second, env="env-b4", world_seconds=10, text="乙的话", reply="乙的回")

    for refs, reason in (([], "范围"), (["m-不存在"], "不存在"), (["m-env-b4"], "不属于来源角色")):
        try:
            world_service.disclose(info["id"], timeline_id, from_character=first, to_character=second, refs=refs)
        except (RuntimeStateError, ValueError) as exc:
            assert reason in str(exc), (refs, str(exc))
        else:
            raise AssertionError(f"应当拒绝：{refs}")
    assert world_service.disclosures(info["id"], timeline_id) == []


def test_disclosure_is_idempotent_and_metadata_only(store) -> None:
    """重复确认同一范围返回同一条授权；清单只给管理元数据（§7.1 / DESKTOP_SPEC）。"""
    world_service = _service(store)
    info, timeline_id, first, second = _two_characters(store, world_service)
    reply_id, _ = _say(store, info, timeline_id, first, env="env-a5", world_seconds=10, text="问", reply="答")
    first_grant = world_service.disclose(
        info["id"], timeline_id, from_character=first, to_character=second, refs=[reply_id]
    )
    again = world_service.disclose(
        info["id"], timeline_id, from_character=first, to_character=second, refs=[reply_id]
    )
    assert again["reused"] is True and again["id"] == first_grant["id"]
    listed = world_service.disclosures(info["id"], timeline_id, to_character=second)
    assert len(listed) == 1 and listed[0]["count"] == 1
    assert set(listed[0]) == {"id", "from_character", "to_character", "granted_world", "note", "count"}
    assert "答" not in json.dumps(listed, ensure_ascii=False), "清单不带内容"


def test_no_revoke_entry_only_rollback(store) -> None:
    """撤回只有回滚一条路：没有单独的撤回 / 删除披露入口（§7.2）。"""
    names = set(SYNC_OPS) | set(ASYNC_OPS)
    assert "disclose.confirm" in names and "disclose.list" in names
    assert not [name for name in names if "disclosure" in name and any(k in name for k in ("revoke", "delete", "withdraw"))]


def test_rollback_revokes_disclosure_and_derived_memory(store) -> None:
    """回滚同时撤销授权与依赖它的派生（§7.2）：只从旧提交分叉不算撤回原线。"""
    world_service = _service(store)
    info, timeline_id, first, second = _two_characters(store, world_service)
    before = world_service.commit(info["id"], timeline_id, note="披露前")
    reply_id, _ = _say(store, info, timeline_id, first, env="env-a6", world_seconds=10,
                       text="堤上的事", reply="信报上抄到堤长身故")
    world_service.disclose(info["id"], timeline_id, from_character=first, to_character=second, refs=[reply_id])
    assert world_service.disclosed_fragments(info["id"], timeline_id, second), "披露已生效"

    # B 的派生记忆（转述条目被提取后的样子）
    store.memory_add({
        "id": "mm-b-derived", "instance_id": info["id"], "timeline_id": timeline_id, "character_id": second,
        "text": "联络者转述了堤禾说过的话：信报上抄到堤长身故", "kind": "fragment",
        "sources": [{"kind": "dialog", "ref": reply_id, "source_role": "other_character"}],
        "happened_world": 0, "learned_world": 0, "recorded_world": 0, "semantic_watermark": 0,
        "strength": 0.6, "confidence": 0.8,
    })

    world_service.rollback(info["id"], timeline_id, commit_id=before["id"], now_real=1.7e9 + 5 * DAY)
    assert world_service.disclosures(info["id"], timeline_id) == [], "回滚撤销授权"
    assert world_service.disclosed_fragments(info["id"], timeline_id, second) == []
    assert store.memory_get("mm-b-derived", instance_id=info["id"], timeline_id=timeline_id) is None, (
        "派生记忆一并撤销"
    )

    # 另一条路：从披露前的提交分叉不会撤销原线
    branch = world_service.fork(info["id"], timeline_id, commit_id=before["id"], name="披露前分支")
    other = branch["timeline"]["id"]
    assert world_service.disclosures(info["id"], other) == [], "分支继承的是披露前的共同过去"


def test_disclosed_fragment_becomes_a_transcript_memory_not_experience(store) -> None:
    """接收角色侧：披露内容登记为「联络者转述」来源，不成为她的亲历（§4.1 / §7.1）。"""
    world_service = _service(store)
    info, timeline_id, first, second = _two_characters(store, world_service)
    reply_id, _ = _say(store, info, timeline_id, first, env="env-a7", world_seconds=10,
                       text="堤上的事", reply="信报上抄到堤长身故")
    world_service.disclose(info["id"], timeline_id, from_character=first, to_character=second, refs=[reply_id])
    queued = world_service.queue_disclosed_sources(info["id"], timeline_id, second)
    assert queued == 1
    task = [row for row in store.memory_tasks(info["id"], timeline_id) if row["source_kind"] == "disclosed"][0]
    assert "联络者转述" in task["text"]
    material = world_service._source_material(task)
    assert material is not None
    assert material["source"] == "联络者转述"
    assert material["sources"][0]["source_role"] == "other_character", "来源标成另一个人说的"
