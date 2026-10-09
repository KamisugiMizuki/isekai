"""状态域注册表守卫（P0：新增一类状态不再需要手工改四处）。

这组测试的作用是**让「漏登记」变响**：新增一张带 `timeline_id` 的表而没写进
`isekai_core.store_state_domains` 时，测试直接失败——而不是让回滚悄悄留下脏数据。
"""

from __future__ import annotations

from isekai_core import store_state_domains as domains
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _tables_with_timeline_id(store) -> set[str]:  # noqa: ANN001 - 夹具注入
    names = [row[0] for row in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    out = set()
    for name in names:
        if name == "sqlite_sequence":
            continue
        cols = {row[1] for row in store._conn.execute(f'PRAGMA table_info("{name}")')}
        if "timeline_id" in cols:
            out.add(name)
    return out


def test_every_registered_table_exists(store) -> None:  # noqa: ANN001
    """注册表里的表名必须真实存在（防拼写漂移）。"""
    names = {row[0] for row in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    missing = sorted(domains.known_tables() - names)
    assert not missing, f"注册表里有不存在的表：{missing}"


def test_cleared_tables_are_a_subset_of_state_tables(store) -> None:  # noqa: ANN001
    """被清的表必须真的带 `timeline_id`（否则 DELETE 的 WHERE 条件没有意义）。"""
    with_tl = _tables_with_timeline_id(store)
    wrong = sorted(set(domains.cleared_tables()) - with_tl)
    assert not wrong, f"这些表被登记为「回滚要清」，但没有 timeline_id 列：{wrong}"


def test_new_state_table_must_be_registered(store) -> None:  # noqa: ANN001
    """**核心守卫**：凡带 `timeline_id` 的表都必须在注册表的三档之一里。

    失败时的正确处理是「去 `store_state_domains.py` 登记并写理由」（CLEARED / KEPT / NEEDS_DECISION），
    而不是把表名加进白名单绕过去——`NEEDS_DECISION` 本身就是待办的显式表达。
    """
    unregistered = sorted(_tables_with_timeline_id(store) - domains.known_tables())
    assert not unregistered, (
        f"这些表带 timeline_id 但没在 store_state_domains 登记：{unregistered}；"
        "请在 CLEARED_ON_ROLLBACK / KEPT_ON_ROLLBACK / NEEDS_DECISION 中选一档并写理由"
    )


def test_kept_entries_all_carry_a_reason(store) -> None:  # noqa: ANN001
    """「故意不清」必须写明依据——不接受空理由。"""
    empty = sorted(name for name, reason in domains.KEPT_ON_ROLLBACK.items() if not str(reason).strip())
    assert not empty, f"这些表登记为「回滚不清」但没写依据：{empty}"


def test_snapshot_sections_match_runtime_dump(store, world) -> None:  # noqa: ANN001, F811
    """**快照侧漂移守卫**：`runtime_dump` 的真实分节必须与注册表逐字相同。

    A-7 的目标是把「四处手工」收敛成一份声明；在把 `runtime_dump` / `runtime_load` 真正接上之前，
    先钉住当前的分节集合——此后**加了分节却忘了登记**会立刻失败，避免重构期间行为悄悄漂移。
    """
    info, timeline_id, _character_id = make_instance(store, world)
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    dump = store.runtime_dump(info["id"], timeline_id, watermark=watermark)
    actual = set(dump.keys())
    expected = set(domains.SNAPSHOT_SECTIONS)
    assert actual == expected, (
        f"快照分节与注册表不一致：多出 {sorted(actual - expected)}，缺失 {sorted(expected - actual)}；"
        "请同步 store_state_domains.SNAPSHOT_SECTIONS"
    )
    assert expected - actual == set(), "注册表不得声明运行时不存在之分节"


def test_rollback_clears_state_but_keeps_control_state(store, world) -> None:  # noqa: F401
    """行为验证：回滚清掉派生状态，但**保留**控制状态与身份记录。"""
    info, timeline_id, character_id = make_instance(store, world)
    store.knowledge_put({
        "instance_id": info["id"], "timeline_id": timeline_id, "character_id": character_id,
        "id": "kn-domain-1", "world_seconds": 0, "kind": "claim", "target": "cl-1",
        "source": "src-1", "stance": "recorded", "text": "潮位记录",
    })
    store.first_contact_mark(info["id"], timeline_id, character_id) if hasattr(
        store, "first_contact_mark"
    ) else None
    assert store.knowledge_ids(info["id"], timeline_id, character_id), "前置：先写一条获知"
    store.timeline_clear_state(timeline_id)
    assert not store.knowledge_ids(info["id"], timeline_id, character_id), "派生状态随回滚清空"
    # 身份/控制状态的表必须还在（这里只断言表可查且不报错，具体值由快照回写负责）
    assert store.timeline_get(timeline_id) is not None, "线身份保留"
    assert store.clock_get(timeline_id) is not None, "时钟保留（回滚后重新锚定）"
