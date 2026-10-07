"""生死事件（阶段 3 收尾，EVENT_ENGINE_SPEC §四：单独记账、可产生死讯说法）。"""

from __future__ import annotations

import time

from isekai_core.runtime import events
from isekai_core.runtime.calendar import calendar_from_package
from isekai_core.runtime.service import RuntimeService
from isekai_core.world.instances import create_instance
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _dying_card(package, *, died: int) -> dict:
    """卡片固化的死亡（§12：已固化的出生与死亡不因分叉 / 回滚改变）。"""
    card = sample_card(package)
    card["identity"]["died"] = died
    return card


def test_death_moment_from_lifespan_or_fixed() -> None:
    package = sample_package()
    calendar = calendar_from_package(package)
    card = sample_card(package)
    born = int(card["identity"]["born"])
    assert events.death_moment(card, package, calendar) == born + 80 * calendar.year_seconds, (
        "按种族寿命上限与出生时刻推出"
    )
    fixed = _dying_card(package, died=DAY * 1502)
    assert events.death_moment(fixed, package, calendar) == DAY * 1502, "卡片固化优先"
    unbounded = sample_card(package)
    unbounded["identity"]["race_id"] = "rc-none"
    assert events.death_moment(unbounded, package, calendar) is None, "没有寿命依据就不替设定发明死亡"


def test_lifespan_form_and_overrides() -> None:
    """寿命 schema（§十 残余）：两种形态同一套校验；个体覆盖优先于种族带，固死优先于覆盖。"""
    from isekai_core.world.cards import effective_lifespan, validate_card
    from isekai_core.world.validate import lifespan_errors, validate_package

    # 形态：mode 与 min/max 不能混写；未知 mode 报错
    assert lifespan_errors({"mode": "long"}, "x") == []
    assert "不能同时声明" in " ".join(lifespan_errors({"mode": "long", "max_years": 3}, "x"))
    assert "long/unbounded" in " ".join(lifespan_errors({"mode": "ageless"}, "x"))

    package = sample_package()
    calendar = calendar_from_package(package)
    born = int(sample_card(package)["identity"]["born"])

    # 种族 band 与 mode 混写：整包校验拒绝
    bad = sample_package()
    bad["races"][0]["lifespan"] = {"mode": "long", "min_years": 1, "max_years": 2}
    assert any("混写" in item for item in validate_package(bad)), validate_package(bad)

    # 卡级覆盖：种族声明 long（不推寿终）也照样按卡片自己的年限推
    long_package = sample_package()
    long_package["races"][0]["lifespan"] = {"mode": "long"}
    card = sample_card(long_package)
    assert events.death_moment(card, long_package, calendar) is None, "种族 long 时不推"
    card["identity"]["lifespan"] = {"min_years": 3, "max_years": 5}
    assert effective_lifespan(card["identity"], long_package["races"][0]) == {"min_years": 3, "max_years": 5}
    assert events.death_moment(card, long_package, calendar) == born + 5 * calendar.year_seconds

    # 固死优先于卡级覆盖
    card["identity"]["died"] = born + 10
    assert events.death_moment(card, long_package, calendar) == born + 10

    # 冲突：固死早于出生 / 卡级形态非法 → 校验拒绝
    card["identity"]["died"] = born - 1
    assert any("不能早于出生" in item for item in validate_card(card, long_package, moment=DAY * 1500))
    card["identity"].pop("died")
    card["identity"]["lifespan"] = {"mode": "long", "min_years": 1}
    assert any("混写" in item for item in validate_card(card, long_package, moment=DAY * 1500))
    card["identity"]["lifespan"] = {"min_years": 5, "max_years": 3}
    assert any("正整数年" in item for item in validate_card(card, long_package, moment=DAY * 1500))

    # 相容性检查用生效形态：种族带太短但卡级覆盖够长 → 不再报「不相容」
    short = sample_package()
    short["races"][0]["lifespan"] = {"min_years": 1, "max_years": 2}
    short_card = sample_card(short)
    assert any("不相容" in item for item in validate_card(short_card, short, moment=DAY * 1500))
    short_card["identity"]["lifespan"] = {"min_years": 100, "max_years": 200}
    assert not any("不相容" in item for item in validate_card(short_card, short, moment=DAY * 1500))


def test_death_event_is_registered_separately_and_spreads(store) -> None:
    world_service = RuntimeService(store)
    package = sample_package()
    moment = DAY * 1500
    card = _dying_card(package, died=moment + 3 * DAY)
    other = sample_card(package)
    other["meta"]["card_id"] = "cc-other"
    other["meta"]["confirmed"] = True
    other["identity"]["name"] = "另一个角色"

    info = create_instance(store, package, [card, other])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world_service.ensure_instance(info["id"], now_real=1.7e9)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 1 * DAY)
    assert not [item for item in store.event_window(info["id"], timeline_id, until=10**12, limit=200)
                if str(item["template"]).startswith("death:")], "还没到寿终时刻"

    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 4 * DAY)
    deaths = [
        item
        for item in store.event_window(info["id"], timeline_id, until=10**12, limit=300)
        if str(item["template"]).startswith("death:")
    ]
    assert len(deaths) == 1, "寿终登记为事件且只登记一次"
    death = deaths[0]
    assert death["kind"] == "character" and death["source"] == "engine"
    assert "身故" in str(death["summary"])
    assert int(death["world_seconds"]) == moment + 3 * DAY, "发生在寿命推出的时刻"

    # 死讯走普通说法路径：有渠道的角色能听到，死者自己不需要自己的死讯
    claims = store.claim_list(info["id"], timeline_id, event_id=str(death["id"]))
    assert claims, "身故产生死讯说法"
    claim_ids = [str(item["id"]) for item in claims]
    knowers = {
        str(row["character_id"])
        for row in store._conn.execute(
            f"SELECT character_id FROM knowledge WHERE timeline_id=? AND target IN ({','.join('?' * len(claim_ids))})",
            (timeline_id, *claim_ids),
        ).fetchall()
    }
    assert "cc-other" in knowers, "有相应渠道的角色获知死讯"
    assert str(card["meta"]["card_id"]) not in knowers, "死者不需要自己的死讯"

    # 身故后不再产生计划与经历
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 6 * DAY)
    latest = store.plan_latest(info["id"], timeline_id, str(card["meta"]["card_id"]))
    assert int(latest["day_index"]) <= (moment + 3 * DAY) // DAY, "身故后不再排新计划"
    fresh = [
        item
        for item in store.experience_window(
            info["id"], timeline_id, str(card["meta"]["card_id"]), until=10**12, limit=300
        )
        if int(item["world_seconds"]) > moment + 3 * DAY
    ]
    assert not fresh, "身故后不再产生经历"


def test_death_abandons_standing_intents(store) -> None:
    world_service = RuntimeService(store)
    package = sample_package()
    moment = DAY * 1500
    card = _dying_card(package, died=moment + 2 * DAY)
    # 把打算的目标时间窗挪到身故之后：它只能被收束为放弃，不能假装执行
    card["intents"] = [
        {
            "id": "in-late",
            "object": "等汛后去把堤志补完",
            "basis": "她自己的差事",
            "strength": 0.6,
            "window": {"from": moment + 8 * DAY, "to": moment + 12 * DAY},
            "preconditions": ["cf-1"],
            "effect": {"kind": "public_notice", "target": "src-1", "expiry": "with_cause"},
        }
    ]
    info = create_instance(store, package, [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world_service.ensure_instance(info["id"], now_real=1.7e9)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 4 * DAY)
    rows = store.intent_list(info["id"], timeline_id, str(card["meta"]["card_id"]))
    assert rows and all(str(row["stage"]) in ("abandoned", "done") for row in rows), (
        "身故后未竟之事收束为放弃（不静默消失）"
    )
    assert any("她已不在了" in str(row["note"]) for row in rows if row["stage"] == "abandoned")
    _ = time


def test_archive_state_outlives_the_event_window(store) -> None:
    """§六 / P1-3：归档态是**版本化状态位**，不靠「最近 400 条事件」反推。

    构造：角色寿终 → 状态位与死亡事件同一批落盘；随后灌入 500 条更晚的事件，把死亡事件
    挤出有界窗口 → 她仍然算归档（不再排新计划、不再产生经历），不会出现「复生」。
    """
    world_service = RuntimeService(store)
    package = sample_package()
    moment = DAY * 1500
    card = _dying_card(package, died=moment + DAY)
    info = create_instance(store, package, [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world_service.ensure_instance(info["id"], now_real=1.7e9)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)

    character_id = str(card["meta"]["card_id"])
    state = store.character_state_get(info["id"], timeline_id, character_id)
    assert state is not None and int(state["archived"]) == 1, "寿终与归档态同一批落盘（§六）"
    assert str(state["basis"]).startswith("ev-") or state["basis"], "归档要留依据引用"
    assert store.death_exists(info["id"], timeline_id, character_id), "归档判定读状态位"
    assert character_id in world_service._archived_ids(info["id"], timeline_id)

    # 把死亡事件挤出 400 条窗口：灌 500 条更晚的事件（同刻顺序按 seq）
    with store._lock, store._conn:
        for index in range(500):
            store._conn.execute(
                """INSERT OR IGNORE INTO event(instance_id, timeline_id, id, world_seconds, seq, kind,
                       family, template, source, summary, detail, text_source, effects, share_value,
                       importance, created_real)
                   VALUES(?,?,?,?,?,'world','','filler','engine','填充事件','','template','[]',0,0.1,0)""",
                (info["id"], timeline_id, f"fill-{index}", moment + 10 * DAY + index, index),
            )
    assert not events.is_dead(
        info["id"], timeline_id, character_id,
        store.event_window(info["id"], timeline_id, until=10**12, limit=400),
    ), "前提：这时窗口判法已经看不见那次寿终"

    plan_before = store.plan_latest(info["id"], timeline_id, character_id)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 12 * DAY)
    plan_after = store.plan_latest(info["id"], timeline_id, character_id)
    assert int(plan_after["day_index"]) == int(plan_before["day_index"]), (
        "窗口看不见寿终也不能让她复生：不再排新计划"
    )
    fresh = [
        item
        for item in store.experience_window(info["id"], timeline_id, character_id, until=10**12, limit=600)
        if int(item["world_seconds"]) > moment + DAY
    ]
    assert not fresh, "已归档角色不再产生经历"
    assert store.death_exists(info["id"], timeline_id, character_id), "窗口失效后状态位仍然作数"
