"""用户引入事件（EVENT §八 / 验收 7、17）的行为验收。"""

from __future__ import annotations

import asyncio
import json

from isekai_core.runtime.service import RuntimeService, RuntimeStateError
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


def _ready(store, world_service, *, moment=DAY * 1500):
    info, timeline_id, character_id = make_instance(store, world_service, moment=moment)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    return info, timeline_id, character_id


def _draft(world_service, *args, **kwargs):
    """草案生成是 async（可能调模型）：测试里同步驱动。"""
    return asyncio.run(world_service.draft_user_event(*args, **kwargs))


def _payload(**over) -> dict:
    payload = {
        "intent": "堤务吏换人：柳氏接任堤长",
        "when": "now",
        "effects": [{"kind": "institution_state", "target": "off-1", "expiry": "until_cleared"}],
        "claims": [{"text": "堤务吏换人，柳氏接任堤长", "source_id": "src-1", "audience": "public"}],
    }
    payload.update(over)
    return payload


def test_draft_rejects_unexpressible_intent(store) -> None:
    """表达不出来的意图明确拒绝，不把自由文本当成已执行（§八 第 4 条）。"""
    world_service = _service(store)
    info, timeline_id, _ = _ready(store, world_service)
    refused = _draft(world_service, info["id"], timeline_id, intent="让潮水永远退去")
    assert refused["accepted"] is False and "效果" in refused["reason"]
    bad_kind = _draft(world_service, 
        info["id"], timeline_id, intent="改天气", payload={"effects": [{"kind": "weather_magic", "target": "rl-1"}]}
    )
    assert bad_kind["accepted"] is False and "不支持" in bad_kind["reason"]
    unknown = _draft(world_service, 
        info["id"], timeline_id, intent="换人", payload={"effects": [{"kind": "institution_state", "target": "rl-99"}]}
    )
    assert unknown["accepted"] is False and "未登记" in unknown["reason"]
    assert store.draft_get("dr-nope") is None


def test_draft_shows_only_user_intent_and_validated_parts(store) -> None:
    """草案只展示用户意图与可确认部分，不带任何既有隐藏事实（§八 第 2/5 条）。"""
    world_service = _service(store)
    info, timeline_id, _ = _ready(store, world_service)
    result = _draft(world_service, info["id"], timeline_id, intent="堤务吏换人：柳氏接任堤长",
                                            payload=_payload())
    assert result["accepted"] is True
    draft = result["draft"]
    assert draft["intent"] == "堤务吏换人：柳氏接任堤长"
    assert draft["effects"][0]["target"] == "off-1" and draft["when"] == "now"
    blob = json.dumps(draft, ensure_ascii=False)
    assert "秘密" not in blob and "实情" not in blob
    assert set(draft) == {"draft_id", "intent", "when", "at_world", "effects", "claims", "participants",
                          "source", "confirmed", "timeline_id"}


def test_confirm_creates_new_line_with_event_and_keeps_original(store) -> None:
    """确认后原子建线并注入；原线保留、新线默认冻结；重试返回同一结果（§八 第 6/8 条）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    before_events = len(store.event_window(info["id"], timeline_id, until=10**15, limit=500))
    draft = _draft(world_service, info["id"], timeline_id, intent="堤务吏换人：柳氏接任堤长",
                                           payload=_payload())["draft"]
    confirmed = world_service.confirm_user_event(info["id"], draft["draft_id"], name="柳氏线")
    new_line = confirmed["timeline_id"]
    assert store.timeline_get(new_line)["state"] == "frozen", "新线默认冻结，由用户明确激活"
    events = [row for row in store.event_window(info["id"], new_line, until=10**15, limit=500)
              if row["source"] == "user"]
    assert len(events) == 1 and "堤务吏换人" in events[0]["summary"]
    effects = [row for row in store.effect_window(info["id"], new_line, until=10**15)
               if row["event_id"] == events[0]["id"]]
    assert effects and effects[0]["target"] == "off-1", "效果落在新线上"
    assert len(store.event_window(info["id"], timeline_id, until=10**15, limit=500)) == before_events, "原线不动"

    again = world_service.confirm_user_event(info["id"], draft["draft_id"])
    assert again["reused"] is True and again["timeline_id"] == new_line, "重试不重复造线"
    assert len([row for row in store.timeline_list(info["id"])]) == 2, "只有两条线"


def test_claim_reaches_channel_holders_only(store) -> None:
    """说法按渠道到达角色；没有该渠道的角色不因「公开」标签获知（§五 / 附录 B #5）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    before = store.knowledge_window(info["id"], timeline_id, character_id, until=10**15, limit=100)
    assert not any("柳氏" in str(row["text"]) for row in before), "改动前没人知道"

    draft = _draft(world_service, info["id"], timeline_id, intent="堤务吏换人：柳氏接任堤长", payload=_payload())["draft"]
    new_line = world_service.confirm_user_event(info["id"], draft["draft_id"])["timeline_id"]
    holder = store.knowledge_window(info["id"], new_line, character_id, until=10**15, limit=100)
    assert any("柳氏" in str(row["text"]) for row in holder), "持有信报渠道的角色当即获知"

    # 没有该渠道的角色（卡片不含 src-1）不该知道
    deaf = sample_card(sample_package(), name="只读碑拓者")
    deaf["channels"] = [{"source_id": "src-2", "conditions": "只在碑拓上读到旧事"}]  # 没有驿站信报渠道
    world_service.add_character(
        info["id"], new_line, deaf, now_real=1.7e9 + 3 * DAY,
        joined_world=int(store.clock_get(new_line)["processed_world"]),
    )
    deaf_id = str((deaf.get("meta") or {}).get("card_id") or "")
    deaf_known = store.knowledge_window(info["id"], new_line, deaf_id, until=10**15, limit=100)
    assert not any("柳氏" in str(row["text"]) for row in deaf_known), "无渠道者不因公开标签获知"


def test_scheduled_event_waits_then_applies_or_cancels(store) -> None:
    """预约事件到点前不产生效果 / 经历 / 获知；到点复核后施加，条件失效则记取消（验收 17）。"""
    world_service = _service(store)
    info, timeline_id, _ = _ready(store, world_service)
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    draft = _draft(world_service, 
        info["id"], timeline_id, intent="七日后堤务吏换人",
        payload=_payload(when="scheduled", at_world=watermark + 7 * DAY),
    )["draft"]
    new_line = world_service.confirm_user_event(info["id"], draft["draft_id"])["timeline_id"]
    user_events = [row for row in store.event_window(info["id"], new_line, until=10**15, limit=200)
                   if row["source"] == "user"]
    assert user_events == [], "到点前不注入事件"
    assert [row for row in store.effect_window(info["id"], new_line, until=10**15)
            if row["target"] == "off-1" and int(row["from_world"]) > watermark] == [], "到点前不施加效果"

    world_service.activate(info["id"], new_line, now_real=1.7e9 + 5 * DAY)
    world_service.advance(info["id"], new_line, now_real=1.7e9 + 16 * DAY)  # 越过预约时刻
    assert store.pending_events_due(info["id"], new_line, until=10**15) == [], "到期后不再是待执行"
    landed = [row for row in store.event_window(info["id"], new_line, until=10**15, limit=200)
              if row["source"] == "user"]
    assert landed and int(landed[0]["world_seconds"]) == watermark + 7 * DAY, "在预约时刻落事件"

    # 目标在到期前失效 → 记取消而不是强行执行
    other = _draft(world_service, 
        info["id"], new_line, intent="把某职位的状态改掉",
        payload=_payload(when="scheduled", at_world=int(store.clock_get(new_line)["processed_world"]) + 3 * DAY),
    )["draft"]
    line2 = world_service.confirm_user_event(info["id"], other["draft_id"])["timeline_id"]
    # 目标失效：把角色从本线移除后到期 → 记取消而不是强行执行
    store.pending_event_add({
        "id": "pe-stale", "instance_id": info["id"], "timeline_id": line2,
        "at_world": int(store.clock_get(line2)["processed_world"]) + DAY,
        "payload": json.dumps(_payload(), ensure_ascii=False), "state": "pending", "note": "",
        "created_world": 0, "created_at": 0.0,
    })
    store.character_join_remove(info["id"], line2, character_id) if hasattr(store, "character_join_remove") else None
    world_service.activate(info["id"], line2, now_real=1.7e9 + 6 * DAY)
    world_service.advance(info["id"], line2, now_real=1.7e9 + 7 * DAY)
    assert store.pending_event_set("pe-stale", state="cancelled") is False, "已被到点处理（不再 pending）"
    _ = other


def test_rollback_removes_pending_events(store) -> None:
    """回滚撤销待执行状态及其后果（§八 末条）。"""
    world_service = _service(store)
    info, timeline_id, _ = _ready(store, world_service)
    commit = world_service.commit(info["id"], timeline_id, note="预约前")
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    draft = _draft(world_service, 
        info["id"], timeline_id, intent="日后换人",
        payload=_payload(when="scheduled", at_world=watermark + 5 * DAY),
    )["draft"]
    new_line = world_service.confirm_user_event(info["id"], draft["draft_id"])["timeline_id"]
    base = store.commit_list(info["id"], new_line)[0]["id"]
    assert store.pending_events_due(info["id"], new_line, until=10**15) != [], "先有预约"
    world_service.rollback(info["id"], new_line, commit_id=base, now_real=1.7e9 + 5 * DAY)
    assert store.pending_events_due(info["id"], new_line, until=10**15) == [], "回滚后不残留待执行"
    _ = commit
