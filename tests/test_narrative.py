"""叙事中介层（NARRATIVE_LAYER_SPEC）行为验收。

测的是可观察行为：候选只来自她自己知道的东西、编织只在有依据时发生、
讲出来的算消费 / 没讲出口的只降级、后验检查能拦下越界、回滚与导出导入随线走。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager

from isekai_core.runtime import life as life_mod
from isekai_core.runtime import narrative
from isekai_core.runtime.service import RuntimeService
from isekai_core.world.instances import create_instance
from conftest import bind_thread, open_mgmt, running_core
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


class ScriptedLLM:
    """记录每次调用的提示词：能分清「生成」与「后验检查」两类调用。"""

    def __init__(self, text: str = "北堤的牌子还没发下来，我路过时看了一眼。") -> None:
        self.text = text
        self.calls = 0
        self.prompts: list[str] = []

    async def chat(self, prompt, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.prompts.append(json.dumps(prompt, ensure_ascii=False))
        return self.text

    @property
    def generations(self) -> int:
        return sum(1 for item in self.prompts if "告诉联络者" in item)

    @property
    def audits(self) -> int:
        return sum(1 for item in self.prompts if "忠实度判断" in item)


class AuditLLM(ScriptedLLM):
    """正文一种应答、后验判定另一种应答。"""

    def __init__(self, text: str, verdict: dict) -> None:
        super().__init__(text)
        self.verdict = verdict

    async def chat(self, prompt, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        blob = json.dumps(prompt, ensure_ascii=False)
        self.prompts.append(blob)
        if "忠实度判断" in blob:
            return json.dumps(self.verdict, ensure_ascii=False)
        return self.text


@contextmanager
def daytime():
    """把「此刻清醒」作为前提打桩：节律本身由 test_proactive 单独验。"""
    original = life_mod.activity_label
    life_mod.activity_label = lambda window: "日间活动"  # type: ignore[assignment]
    try:
        yield
    finally:
        life_mod.activity_label = original  # type: ignore[assignment]


def _ready(store, *, activate: bool = True):
    service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, service)
    if activate:
        service.activate(info["id"], timeline_id, now_real=time.time())
    return service, info["id"], timeline_id, character_id


def _know(store, instance_id: str, timeline_id: str, character_id: str, *, at: int, ref: str, text: str,
          source: str = "src-1", stance: str = "recorded") -> None:
    store.knowledge_put(
        {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "character_id": character_id,
            "id": f"kn-{ref}",
            "world_seconds": int(at),
            "kind": "claim",
            "target": ref,
            "source": source,
            "stance": stance,
            "text": text,
        }
    )


def _live(store, instance_id: str, timeline_id: str, character_id: str, *, at: int, ref: str, text: str) -> None:
    store.experience_add(
        {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "character_id": character_id,
            "id": ref,
            "world_seconds": int(at),
            "kind": "life",
            "summary": text,
            "source_ref": "",
            "confidence": "experienced",
        }
    )


def _item(ref: str, entry: str, *, at: int, text: str = "一件小事", source: str = "src-1") -> dict:
    return {
        "ref": ref,
        "entry": entry,
        "kind": "claim" if entry == "knowledge" else "life",
        "text": text,
        "source": source,
        "stance": "只是读到过" if entry == "knowledge" else "亲身经历",
        "at_world": int(at),
    }


# ---------- 候选（§3.1 / 附录 A #1、#2） ----------


def test_materials_only_from_her_own_experience_and_knowledge(store, world) -> None:  # noqa: F811
    service, instance_id, timeline_id, character_id = _ready(store, activate=False)
    world_s = int(service.clock_row(timeline_id)["processed_world"])
    _know(store, instance_id, timeline_id, character_id, at=world_s - 60, ref="cl-now", text="北堤的通行牌这三天都停发了")
    _know(store, instance_id, timeline_id, character_id, at=world_s + 5 * DAY, ref="cl-future", text="盐滩那边立了新碑")
    _live(store, instance_id, timeline_id, character_id, at=world_s - 30, ref="xp-1", text="她清早去看过堤上的水位")
    _live(store, instance_id, timeline_id, character_id, at=world_s + 1000, ref="xp-plan", text="她明天要去集市换盐")

    items = narrative.materials(
        experiences=store.experience_window(instance_id, timeline_id, character_id, until=world_s, limit=20),
        knowledge=store.knowledge_window(instance_id, timeline_id, character_id, until=world_s, limit=20),
        world_seconds=world_s,
        day_seconds=DAY,
    )
    refs = [item["ref"] for item in items]
    assert "cl-now" in refs and "xp-1" in refs
    assert "cl-future" not in refs, "还没传播到她那儿的说法不进候选"
    assert "xp-plan" not in refs, "还没发生的安排不是经历"
    assert all(item["text"].strip() for item in items)
    assert {item["entry"] for item in items} <= {"experience", "knowledge"}


def test_rank_is_stable_and_deferred_is_demoted() -> None:
    items = [_item("cl-late", "knowledge", at=100), _item("xp-1", "experience", at=90)]
    assert [item["ref"] for item in narrative.rank(items)] == ["xp-1", "cl-late"]
    assert [item["ref"] for item in narrative.rank(list(reversed(items)))] == ["xp-1", "cl-late"]
    held = [item["ref"] for item in narrative.rank(items, deferred_refs={"xp-1"})]
    assert held == ["cl-late", "xp-1"], "没讲出口的降级但不除名"


# ---------- 编织（§4.3 / 附录 A #4） ----------


def test_weave_combines_only_related_materials() -> None:
    same_source = [
        _item("cl-1", "knowledge", at=100, text="北堤的牌子停发了", source="src-1"),
        _item("cl-2", "knowledge", at=90, text="盐滩那边也停了", source="src-1"),
    ]
    unit = narrative.weave(narrative.rank(same_source), day_seconds=DAY)
    assert unit is not None
    assert unit["refs"] == ["cl-1", "cl-2"] and unit["relation"] == "补充"

    unrelated = [
        _item("cl-1", "knowledge", at=100, text="北堤的牌子停发了", source="src-1"),
        _item("cl-2", "knowledge", at=90, text="盐滩那边也停了", source="src-2"),
    ]
    single = narrative.weave(narrative.rank(unrelated), day_seconds=DAY)
    assert single is not None
    assert single["refs"] == ["cl-1"] and single["relation"] == "并列", "没有依据就单材成单元"

    same_day = [_item("xp-1", "experience", at=100), _item("xp-2", "experience", at=100 + DAY // 3)]
    continuous = narrative.weave(narrative.rank(same_day), day_seconds=DAY)
    assert continuous is not None
    assert continuous["relation"] == "连续" and len(continuous["refs"]) == 2

    far_apart = [_item("xp-1", "experience", at=100), _item("xp-2", "experience", at=100 + 3 * DAY)]
    assert len(narrative.weave(narrative.rank(far_apart), day_seconds=DAY)["refs"]) == 1, "隔得太远不编织"


def test_constraint_lines_carry_scope_and_permission_to_withhold() -> None:
    unit = narrative.weave(
        narrative.rank([_item("cl-1", "knowledge", at=100, text="北堤的牌子停发了")]), day_seconds=DAY
    )
    lines = narrative.constraint_lines(unit, activity="在堤上巡看")
    body = "\n".join(lines)
    assert "北堤的牌子停发了" in body
    assert "可以只讲一部分" in body
    assert "没列在这里的事她不知道" in body
    assert "在堤上巡看" in body
    assert "按住不说" not in body, "没暂缓过就不提这件事"
    assert "按住不说" in "\n".join(narrative.constraint_lines(unit, deferred=True))


# ---------- 后验检查（§6.2 / 附录 A #10、#11） ----------


def test_audit_flags_numbers_outside_the_unit_and_parses_verdicts() -> None:
    unit = narrative.weave(
        narrative.rank([_item("cl-1", "knowledge", at=100, text="北堤的通行牌这三天都停发了")]), day_seconds=DAY
    )
    assert narrative.audit("北堤的牌子这几天还没发下来。", unit) == []
    flagged = narrative.audit("北堤的牌子停了 7 天。", unit)
    assert flagged and flagged[0]["kind"] == "numbers"
    assert narrative.parse_audit('{"ok": true}') == (True, "")
    assert narrative.parse_audit('前言 {"ok": false, "why": "说成亲眼所见"} 后记') == (False, "说成亲眼所见")
    assert narrative.parse_audit("说不清") is None, "判不出来不算越界"


def test_proactive_records_unit_and_consumes_every_ref(store, world) -> None:  # noqa: F811
    service, instance_id, timeline_id, character_id = _ready(store)
    world_s = int(service.clock_row(timeline_id)["processed_world"])
    _know(store, instance_id, timeline_id, character_id, at=world_s, ref="cl-a", text="北堤的通行牌这三天都停发了")
    _know(store, instance_id, timeline_id, character_id, at=world_s, ref="cl-b", text="盐滩的秤被收走了")
    llm = ScriptedLLM()

    with daytime():
        first = asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=llm, per_day=2))
    assert first["spoken"] == 1, first
    rows = store.narrative_unit_list(instance_id, timeline_id, character_id=character_id)
    assert len(rows) == 1 and rows[0]["stage"] == "spoken" and rows[0]["message_id"]
    assert json.loads(rows[0]["refs"]) == ["cl-a", "cl-b"], "同一来源的两条材料编织进同一个单元"
    assert llm.generations == 1, "生成只有一次"
    assert llm.audits == 1, "正文里没有可核对的数字：走一次语义判断"
    assert "她最近能提起的事" in next(item for item in llm.prompts if "告诉联络者" in item)

    with daytime():
        second = asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=llm, per_day=2))
    assert second["spoken"] == 0, "单元里的两条材料都算消费过"
    assert set(second["skipped"].values()) <= {"没有可用素材", "睡眠期", "今日额度用完"}


def test_rejected_text_is_recorded_as_deferred_and_never_fixed(store, world) -> None:  # noqa: F811
    service, instance_id, timeline_id, character_id = _ready(store)
    world_s = int(service.clock_row(timeline_id)["processed_world"])
    day = service.calendar(store.instance_get(instance_id)).day_index(world_s)
    _know(store, instance_id, timeline_id, character_id, at=world_s, ref="cl-a", text="北堤的通行牌这三天都停发了")
    llm = ScriptedLLM(text="北堤的牌子停了 7 天。")

    with daytime():
        result = asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=llm, per_day=2))
    assert result["spoken"] == 0
    assert llm.generations == 2, "只重试一次，且不扩大可见材料范围"
    assert store.proactive_list(instance_id, timeline_id) == [], "没有固化消息就没有消费记录"
    rows = store.narrative_unit_list(instance_id, timeline_id, character_id=character_id)
    assert len(rows) == 1 and rows[0]["stage"] == "deferred" and rows[0]["message_id"] == ""
    assert json.loads(rows[0]["audit"])[0]["kind"] == "audit"
    assert store.narrative_deferred_refs(instance_id, timeline_id, character_id, world_day=day) == {"cl-a"}


def test_semantic_audit_blocks_then_allows_the_same_material(store, world) -> None:  # noqa: F811
    service, instance_id, timeline_id, character_id = _ready(store)
    world_s = int(service.clock_row(timeline_id)["processed_world"])
    _know(store, instance_id, timeline_id, character_id, at=world_s, ref="cl-a", text="盐滩的秤被收走了")

    blocked = AuditLLM("我亲眼看见盐滩的秤被人收走了。", {"ok": False, "why": "说成亲眼所见"})
    with daytime():
        first = asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=blocked, per_day=2))
    assert first["spoken"] == 0
    assert blocked.audits == 2, "两次都问过，且第二次是重试之后"
    assert store.narrative_unit_list(instance_id, timeline_id, character_id=character_id)[0]["stage"] == "deferred"

    passed = AuditLLM("盐滩的秤被收走了，听说是这么回事。", {"ok": True})
    with daytime():
        second = asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=passed, per_day=2))
    assert second["spoken"] == 1, "暂缓不消费材料：她改主意就能讲"
    rows = store.narrative_unit_list(instance_id, timeline_id, character_id=character_id)
    assert len(rows) == 1 and rows[0]["stage"] == "spoken" and rows[0]["message_id"]


# ---------- 自然开场（§5.4 / 附录 A #16） ----------


def test_opening_block_only_for_vague_turns_and_never_invents(store, world) -> None:  # noqa: F811
    service, instance_id, timeline_id, character_id = _ready(store, activate=False)
    world_s = int(service.clock_row(timeline_id)["processed_world"])
    _know(store, instance_id, timeline_id, character_id, at=world_s - 60, ref="cl-now", text="北堤的通行牌这三天都停发了")
    _know(
        store, instance_id, timeline_id, character_id, at=world_s + 5 * DAY, ref="cl-future", text="盐滩那边立了新碑"
    )
    session = store.session_ensure(instance_id, timeline_id, character_id)

    vague = service.system_prompt(session, topic="今天怎么样")
    assert "她最近能提起的事" in vague
    assert "北堤的通行牌这三天都停发了" in vague
    assert "盐滩那边立了新碑" not in vague, "还没获知的说法不进提示"
    repeated = service.system_prompt(session, topic="今天怎么样？")
    assert repeated == vague or "北堤的通行牌这三天都停发了" in repeated, "多问一遍不会多给材料"

    topical = service.system_prompt(session, topic="你们律令里那个职位的继任规则是怎么规定的，讲给我听听")
    assert "她最近能提起的事" not in topical


def test_first_contact_speaks_from_her_own_materials(store, world) -> None:  # noqa: F811
    service, instance_id, timeline_id, character_id = _ready(store)
    world_s = int(service.clock_row(timeline_id)["processed_world"])
    _know(store, instance_id, timeline_id, character_id, at=world_s, ref="cl-a", text="盐滩的秤被收走了")
    channel = store.channel_register(
        name="builtin", display_name="内建", version="0", protocol="1", capabilities={}
    )[0]
    session = store.session_ensure(instance_id, timeline_id, character_id)
    store.thread_bind(channel["id"], "view-1", session["id"])
    llm = ScriptedLLM(text="第一次搭话，我是堤禾。")

    result = asyncio.run(
        service.first_contact(
            instance_id, timeline_id, character_id, channel_id=channel["id"], thread_id="view-1", llm=llm
        )
    )
    assert result.get("spoken") is True, result
    prompt = next(item for item in llm.prompts if "第一次主动跟联络者开口" in item)
    assert "她最近能提起的事" in prompt and "盐滩的秤被收走了" in prompt
    rows = store.narrative_unit_list(instance_id, timeline_id, character_id=character_id)
    assert rows and rows[0]["stage"] == "spoken" and rows[0]["message_id"] == result["message_id"]


# ---------- 版本与清理（§7.2） ----------


def test_narrative_records_clear_on_rollback_and_travel_with_dump(store, world) -> None:  # noqa: F811
    service, instance_id, timeline_id, character_id = _ready(store)
    world_s = int(service.clock_row(timeline_id)["processed_world"])
    _know(store, instance_id, timeline_id, character_id, at=world_s, ref="cl-a", text="北堤的通行牌这三天都停发了")
    with daytime():
        asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=ScriptedLLM(), per_day=2))
    assert store.narrative_unit_list(instance_id, timeline_id)

    payload = store.runtime_dump(instance_id, timeline_id, watermark=world_s)
    assert payload["narrative"] and payload["narrative"][0]["stage"] == "spoken"

    store.timeline_clear_state(timeline_id)
    assert store.narrative_unit_list(instance_id, timeline_id) == [], "回滚撤销派生状态"

    store.runtime_load(instance_id, timeline_id, payload)
    assert store.narrative_unit_list(instance_id, timeline_id)[0]["stage"] == "spoken", "导出导入随件走"

    store.instance_delete(instance_id)
    assert store.narrative_unit_list(instance_id, timeline_id) == []


# ---------- 戏剧性 / 三幕 / 分享欲（§9.5-2/3/5） ----------


def test_drama_act_and_share_drive_are_signals_not_gates() -> None:
    unit = narrative.weave(
        narrative.rank([_item("cl-1", "knowledge", at=100, text="北堤的牌子停发了")]), day_seconds=DAY
    )
    assert narrative.act_of(unit) == "起"
    assert narrative.act_of(unit, unresolved=True) == "承"
    assert narrative.act_of(unit, resolved=True) == "收"

    plain = narrative.drama(unit)
    worst = narrative.drama(unit, blocked=True, unresolved=True)
    assert 0.0 <= plain < worst <= 1.0
    assert worst >= narrative.DRAMA_MIN
    assert narrative.drama(unit) == narrative.drama(dict(unit)), "同一单元永远同一个分"

    assert narrative.share_drive([]) == 0.5, "没有依据时按中性，不额外抑制"
    assert narrative.share_drive([{"confidence": 0.8, "archived": 0}]) == 0.8
    assert narrative.share_drive([{"confidence": 0.1, "archived": 1}]) == 0.5, "归档单元不参与表达"

    assert narrative.willingness(0.8, drama_score=0.0)
    assert not narrative.willingness(0.1, drama_score=0.1)
    assert narrative.willingness(0.1, drama_score=narrative.DRAMA_MIN), "素材够戏剧就压得过「不想讲」"


def test_act_shapes_the_tone_not_the_facts() -> None:
    unit = narrative.weave(
        narrative.rank([_item("cl-1", "knowledge", at=100, text="北堤的牌子停发了")]), day_seconds=DAY
    )
    assert "还没了结" in "\n".join(narrative.constraint_lines(unit, act="承"))
    assert "已经有数了" in "\n".join(narrative.constraint_lines(unit, act="收"))
    plain = "\n".join(narrative.constraint_lines(unit))
    assert "还没了结" not in plain and "已经有数了" not in plain


def test_low_share_drive_holds_back_unless_material_is_dramatic(store, world) -> None:  # noqa: F811
    """分享欲低（表达倾向单元置信度压到底）→ 平素材不主动开口；换戏剧素材就讲。"""
    service, instance_id, timeline_id, character_id = _ready(store)
    world_s = int(service.clock_row(timeline_id)["processed_world"])
    _know(store, instance_id, timeline_id, character_id, at=world_s, ref="cl-a", text="北堤的通行牌这三天都停发了")
    for row in store.unit_list(instance_id, timeline_id, character_id):
        store.unit_put({**row, "confidence": 0.05})
    llm = ScriptedLLM()
    with daytime():
        first = asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=llm, per_day=2))
    assert first["spoken"] == 0 and first["skipped"].get(character_id) == "这会儿不想讲自己的事"
    assert store.proactive_list(instance_id, timeline_id) == []
    assert llm.calls == 0, "没开口就不该花掉一次生成"

    # 素材换了：她自己当天的两条亲历（有后续）+ 手上还悬着事 → 够戏剧，压得过低分享欲
    _live(store, instance_id, timeline_id, character_id, at=world_s - 60, ref="xp-1", text="她清早去看过堤上的水位")
    _live(store, instance_id, timeline_id, character_id, at=world_s - 30, ref="xp-2", text="她在盐滩边守了一阵")
    with daytime():
        second = asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=llm, per_day=2))
    assert second["spoken"] == 1, second
    prompt = next(item for item in llm.prompts if "告诉联络者" in item)
    assert "还没了结" in prompt, "三幕位置进了口气约束"


# ---------- 故事图谱（§9.5-1） ----------


def test_story_map_shows_what_she_said_and_marks_what_she_held(store, world) -> None:  # noqa: F811
    service, instance_id, timeline_id, character_id = _ready(store)
    world_s = int(service.clock_row(timeline_id)["processed_world"])
    _know(store, instance_id, timeline_id, character_id, at=world_s, ref="cl-a", text="北堤的通行牌这三天都停发了")
    _know(store, instance_id, timeline_id, character_id, at=world_s, ref="cl-b", text="盐滩的秤被收走了")
    with daytime():
        asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=ScriptedLLM(), per_day=2))
    # 第二条素材会被后验检查拦下（数字越界）→ 记成「没讲出口」
    _know(store, instance_id, timeline_id, character_id, at=world_s, ref="cl-c", text="驿站新到一份灾年编年的补页")
    with daytime():
        asyncio.run(
            service.proactive_tick(instance_id, timeline_id, llm=ScriptedLLM(text="驿站到了 7 份补页。"), per_day=2)
        )

    payload = service.narrative_map(instance_id, timeline_id)
    spoken = [node for node in payload["nodes"] if node["kind"] == "spoken"]
    held = [node for node in payload["nodes"] if node["kind"] == "deferred"]
    assert spoken and spoken[0]["message_id"] and spoken[0]["label"].strip()
    assert payload["counts"]["spoken"] == len(spoken)
    assert held and "没讲出口" in held[0]["label"]
    assert "补页" not in held[0]["label"], "没讲出口的节点不带内容"
    assert all(node["label"] != "（她讲过这件事）" for node in spoken), "讲过的节点用她真说过的那句话"


# ---------- 跨角色披露候选（§9.5-4） ----------


def _two_roles(store, *, moment: int = DAY * 1500):
    service = RuntimeService(store)
    package = sample_package(moment=moment)
    first = sample_card(package, name="堤禾")
    second = sample_card(package, name="潮生")
    info = create_instance(store, package, [first, second])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    service.ensure_instance(info["id"], now_real=time.time())
    service.activate(info["id"], timeline_id, now_real=time.time())
    return (
        service,
        info["id"],
        timeline_id,
        str(first["meta"]["card_id"]),
        str(second["meta"]["card_id"]),
    )


def test_disclosure_candidates_never_auto_grant(store, world) -> None:  # noqa: F811
    service, instance_id, timeline_id, mine, other = _two_roles(store)
    world_s = int(service.clock_row(timeline_id)["processed_world"])
    _know(store, instance_id, timeline_id, mine, at=world_s, ref="cl-a", text="盐滩的秤被收走了")
    with daytime():
        asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=ScriptedLLM(), per_day=2))
    assert store.narrative_unit_list(instance_id, timeline_id, character_id=mine)

    candidates = service.disclosure_candidates(
        instance_id, timeline_id, from_character=mine, to_character=other
    )
    assert candidates and candidates[0]["text"].strip()
    assert store.disclosure_list(instance_id, timeline_id) == [], "候选只是候选：不会自己变成授权"
    assert service.disclosure_candidates(
        instance_id, timeline_id, from_character=other, to_character=mine
    ) == [], "对方没讲过的事不成候选"

    service.disclose(
        instance_id, timeline_id, from_character=mine, to_character=other, refs=[candidates[0]["ref"]]
    )
    after = service.disclosure_candidates(
        instance_id, timeline_id, from_character=mine, to_character=other
    )
    assert all(item["ref"] != candidates[0]["ref"] for item in after), "已授权的片段不再重复推荐"


# ---------- 问答轮的后验检查（§9.5-6，真核心 + 真 WS） ----------


async def _room(h, mgmt, *, moment: int = DAY * 1500 + 30000, thread_id: str = "dm-1"):
    """真实实例 + 已激活时间线 + 绑定 thread（默认世界时刻落在她的白天，不触发睡眠等待）。"""
    package = sample_package(moment=moment)
    card = sample_card(package)
    info = create_instance(h.store, package, [card])
    timeline_id = h.store.timeline_list(info["id"])[0]["id"]
    character_id = str(card["meta"]["card_id"])
    h.runtime.world.ensure_instance(info["id"], now_real=time.time())
    h.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
    client, bound = await bind_thread(
        h,
        mgmt,
        channel_id="builtin",
        thread_id=thread_id,
        instance=info["id"],
        timeline=timeline_id,
        character=character_id,
    )
    return client, bound, info["id"], timeline_id, character_id


async def test_question_turn_runs_the_same_audit(tmp_path):
    """无主题的问答轮也过后验检查，讲过的线索记进同一本账（§6.2 落地口径 / §9.5-6）。"""
    async with running_core(tmp_path, replies=["盐滩的秤被收走了，我听说的。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, character_id = await _room(h, mgmt)
        thread = bound["thread"]
        world = int(h.runtime.world.clock_row(timeline_id)["processed_world"])
        _know(store=h.store, instance_id=instance_id, timeline_id=timeline_id, character_id=character_id,
              at=world, ref="cl-qa", text="盐滩的秤被收走了")

        await client.send_user_message(thread_id="dm-1", binding_token=thread["binding_token"], text="在吗")
        reply = await client.expect(lambda e: e.type == "reply", timeout=10)

        prompts = [json.dumps(call, ensure_ascii=False) for call in h.fake.calls]
        assert any("忠实度判断" in item for item in prompts), "问答轮同样跑了后验检查"
        rows = h.store.narrative_unit_list(instance_id, timeline_id, character_id=character_id)
        assert rows and rows[0]["stage"] == "spoken"
        assert rows[0]["message_id"] == reply.payload["message_id"], "讲过的线索挂在真正固化那条回复上"
