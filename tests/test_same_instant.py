"""同刻顺序（EVENT_ENGINE_SPEC §六 / 附录 B #6）：固定优先规则 + 稳定标识排序。

判据：同刻多效果 / 多事件的施加顺序只由内容决定，不随调用方的迭代顺序或消费者执行先后变。
"""

from __future__ import annotations

from isekai_core.runtime import events
from isekai_core.runtime.service import RuntimeService
from test_runtime import make_instance, store, world  # noqa: F401  夹具定义在那边


def _effect(info, timeline_id, *, ident: str, kind: str, at: int, priority: int | None = None):
    row = {
        "id": ident,
        "instance_id": info["id"],
        "timeline_id": timeline_id,
        "event_id": f"ev-{ident}",
        "target": "rl-1",
        "kind": kind,
        "family": "",
        "from_world": at,
        "expiry": "until_cleared",
        "recovery": "",
        "active": 1,
        "cleared_at": None,
    }
    if priority is not None:
        row["priority"] = priority
    return row


def _window(store, info, timeline_id, at):
    rows = store.effect_window(info["id"], timeline_id, until=at)
    return [(row["id"], row["priority"], row["seq"]) for row in rows if row["id"].startswith("fx-same")]


def _apply(store, timeline_id, at, rows):
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(store.clock_get(timeline_id)["generation"]),
        processed_world=at,
        catching_up=False,
        effects=rows,
    )


def test_same_instant_effects_ordered_by_priority(store) -> None:
    svc = RuntimeService(store)
    info, timeline_id, _character = make_instance(store, svc)
    at = int(store.clock_get(timeline_id)["processed_world"])
    _apply(
        store,
        timeline_id,
        at,
        [
            _effect(info, timeline_id, ident="fx-same-b", kind="public_notice", at=at),
            _effect(info, timeline_id, ident="fx-same-a", kind="route_blocked", at=at),
            _effect(info, timeline_id, ident="fx-same-c", kind="rumor_spread", at=at),
        ],
    )
    got = _window(store, info, timeline_id, at)
    assert [item[0] for item in got] == ["fx-same-a", "fx-same-b", "fx-same-c"], got
    assert [item[2] for item in got] == [0, 1, 2], "同刻顺序写进 seq"
    assert got[0][1] > got[-1][1], "强效果排在弱效果之前"


def test_same_instant_order_ignores_iteration_order(store) -> None:
    svc = RuntimeService(store)
    info, timeline_id, _character = make_instance(store, svc)
    at = int(store.clock_get(timeline_id)["processed_world"])
    straight = [
        _effect(info, timeline_id, ident="fx-same-a", kind="route_blocked", at=at),
        _effect(info, timeline_id, ident="fx-same-b", kind="rumor_spread", at=at),
        _effect(info, timeline_id, ident="fx-same-c", kind="environment_state", at=at),
    ]
    _apply(store, timeline_id, at, list(reversed(straight)))  # 反着喂
    got = [item[0] for item in _window(store, info, timeline_id, at)]
    assert got == ["fx-same-a", "fx-same-c", "fx-same-b"], "读回顺序只由内容定，不随喂入顺序"


def test_declared_priority_overrides_the_table(store) -> None:
    svc = RuntimeService(store)
    info, timeline_id, _character = make_instance(store, svc)
    at = int(store.clock_get(timeline_id)["processed_world"])
    _apply(
        store,
        timeline_id,
        at,
        [
            _effect(info, timeline_id, ident="fx-same-low", kind="route_blocked", at=at),
            _effect(info, timeline_id, ident="fx-same-high", kind="rumor_spread", at=at, priority=99),
        ],
    )
    got = _window(store, info, timeline_id, at)
    assert [item[0] for item in got] == ["fx-same-high", "fx-same-low"], "模板声明的优先级压过默认档位"


def test_priority_table_and_helpers() -> None:
    assert events.effect_priority({"kind": "public_notice", "priority": 99}) == 99
    assert events.effect_priority({"kind": "public_notice", "priority": True}) == events.EFFECT_PRIORITY["public_notice"]
    assert events.effect_priority({"kind": "route_blocked"}) > events.effect_priority({"kind": "rumor_spread"})
    assert events.event_priority({"effects": [{"kind": "rumor_spread"}, {"kind": "route_blocked"}]}) == (
        events.EFFECT_PRIORITY["route_blocked"]
    )
    assert events.event_priority({"effects": []}) == events.DEFAULT_EFFECT_PRIORITY
