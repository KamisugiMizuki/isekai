"""B-7 取代式退休的回归测试（文档第 565-570 行登记的第一处欠账）。

背景（`docs/worldruntime/KERNEL_OPTIMIZATION_TASKS_2026-10-10.md` 第 466-532 / 1244-1277 行）：
B-7 = 「同一 `(target, kind, family)` 上的**设值型** `until_cleared` 后果至多一条活跃」，
其余写进独立表 `effect_superseded`（**不删事实**、不改 `active`，只是离开热路径）。
受控 A/B 判定净负 ⇒ **默认停用**，由环境变量 `ISEKAI_EFFECT_RETIRE` 控制；本文件**成对**锁住两种形态。

两个历史教训直接决定了本文件的写法：

1. **「机制存在」不等于「机制在写」**（第 19 轮踩到的 bug）：`_supersede_prior_effects` 曾只挂在
   快照恢复路径（`runtime_load`），正常推进一次都不触发，而热路径白付了过滤开销。
   所以每条启用态用例都查 `effect_superseded` **真的有一行**（`_ledger`），
   而不是只断言「查询过滤生效」——一个只加过滤不写账的实现必须让这里变红。
2. 开关是**进程环境变量**，`Store.__init__`（`store.py:1224`）只在建库时读一次。
   所有用例都经 `make_store` 夹具用 `monkeypatch` 显式设置 / 还原，不在模块导入期或测试体里
   手工改 `os.environ`，避免污染同进程其他测试。

措辞精度：这里的「至多一条活跃」指**同族设值型 `until_cleared` 后果**。
`effect_window` 只过滤 `until_cleared` 前项（`_supersede_prior_effects` 的 SQL 条件），
同族里别的失效方式（`with_cause` / `natural_recovery`）按设计**不**进入退休。
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from isekai_core.store import SETTING_EFFECT_KINDS, Store

INSTANCE = "in-retire"
TIMELINE = "tl-retire"

#: 文档第 471 行的**设值型**（可取代）名单，与 `store.SETTING_EFFECT_KINDS` 逐字对应
SETTING_KINDS = ("environment_state", "custom_state", "institution_state")
#: 文档第 471 行的**累加型**（不取代、只按 expiry 解除）名单
ADDITIVE_KINDS = (
    "activity_constraint",
    "route_blocked",
    "source_delay",
    "rumor_spread",
    "public_notice",
)


# ---------- 最小夹具：真 Store + 真推进路径，不建世界包 ----------


@pytest.fixture
def make_store(tmp_path, monkeypatch):
    """按开关建库。

    环境变量**先设后用**（`Store.__init__` 只读一次）；退出时关库，环境由 `monkeypatch` 还原。
    `enabled=None` 表示「制造默认态」——显式 `delenv`，不受外部环境变量影响。
    """
    opened: list[Store] = []

    def _make(enabled: bool | None = None, *, name: str = "isekai.db") -> Store:
        if enabled is None:
            monkeypatch.delenv("ISEKAI_EFFECT_RETIRE", raising=False)
        else:
            monkeypatch.setenv("ISEKAI_EFFECT_RETIRE", "1" if enabled else "0")
        handle = Store(tmp_path / name)
        handle.ensure_schema()
        opened.append(handle)
        return handle

    yield _make
    for handle in opened:
        handle.close()


def _put_clock(store: Store, *, processed_world: int = 0, generation: int = 1) -> None:
    """`apply_runtime_batch` 只依赖 `timeline_clock` 一行，不需要实例 / 世界包。"""
    store.clock_put(
        {
            "timeline_id": TIMELINE,
            "base_real": 0.0,
            "base_world": 0,
            "rate": 1,
            "high_water_real": 0.0,
            "anchor_real": 0.0,
            "processed_world": int(processed_world),
            "generation": int(generation),
        }
    )


def _effect(
    ident: str,
    at: int,
    *,
    kind: str = "environment_state",
    target: str = "env-1",
    family: str = "ef-1",
    expiry: str = "until_cleared",
    value: str = "1",
) -> dict[str, Any]:
    return {
        "id": ident,
        "instance_id": INSTANCE,
        "timeline_id": TIMELINE,
        "event_id": f"ev-{ident}",
        "target": target,
        "kind": kind,
        "family": family,
        "value": value,
        "from_world": int(at),
        "expiry": expiry,
        "recovery": "",
        "active": 1,
        "cleared_at": None,
    }


def _write(store: Store, effects: list[dict[str, Any]], *, processed_world: int) -> bool:
    """走**推进路径**（`apply_runtime_batch`）写后果——退休钩子唯一的生产写入点。"""
    return store.apply_runtime_batch(
        timeline_id=TIMELINE,
        generation=int(store.clock_get(TIMELINE)["generation"]),
        processed_world=int(processed_world),
        catching_up=False,
        effects=effects,
    )


def _visible(store: Store, *, until: int) -> list[str]:
    """热路径可见集合（`effect_window`：管理面 / 快照 / 回退路径的完整行读取）。"""
    return [str(row["id"]) for row in store.effect_window(INSTANCE, TIMELINE, until=until)]


def _advance_path_visible(store: Store, *, until: int) -> list[str]:
    """**推进路径真正用的窄取数**（`effect_constraints` ← `service.advance`）。

    与 `_visible` 分开断言是有意的：第 59 轮发现这两个方法曾**只有一个**带 B-7 过滤，
    于是「退休」只在管理面生效、推进路径照样看得见全部历史后果——
    受控 A/B 判净负的结构性原因就在这里（被判定净负的那一侧收益从未兑现）。
    """
    return [
        str(row["id"])
        for row in store.effect_constraints(
            INSTANCE, TIMELINE, until=until, targets=("env-1",)
        )
    ]


def _ledger(store: Store) -> list[tuple[str, str, int]]:
    """`effect_superseded` 真表内容：**证明记账真的发生了**，而不是过滤恰好生效。"""
    return [
        (str(row["effect_id"]), str(row["superseded_by"]), int(row["at_world"]))
        for row in store._conn.execute(
            """SELECT effect_id, superseded_by, at_world FROM effect_superseded
               WHERE instance_id=? AND timeline_id=? ORDER BY effect_id""",
            (INSTANCE, TIMELINE),
        )
    ]


def _raw(store: Store) -> dict[str, int]:
    """库里的原始后果行（含被取代者）：证明「不删事实」。"""
    return {
        str(row["id"]): int(row["active"])
        for row in store._conn.execute(
            "SELECT id, active FROM effect_state WHERE instance_id=? AND timeline_id=?",
            (INSTANCE, TIMELINE),
        )
    }


def _indexes(store: Store, table: str) -> set[str]:
    return {str(row[1]) for row in store._conn.execute(f"PRAGMA index_list('{table}')")}


# ---------- 开关形态（成对：默认停用 / 启用，含索引按开关创建与删除） ----------


def test_default_off_does_not_create_the_b7_indexes(make_store) -> None:
    """默认态：开关未设置 ⇒ 停用，且 B-7 的两个索引不得建（白付维护成本）。"""
    store = make_store(None)
    assert store.effect_retire_enabled is False, "ISEKAI_EFFECT_RETIRE 未设置 ⇒ 默认停用"
    assert "ISEKAI_EFFECT_RETIRE" not in os.environ, "夹具用 monkeypatch.delenv 制造默认态"
    assert "ix_effect_retire" not in _indexes(store, "effect_state")
    assert "ix_effect_superseded_by" not in _indexes(store, "effect_superseded")


def test_enabled_creates_the_b7_indexes(make_store) -> None:
    """启用态：开关为 1 ⇒ 启用的索引必须真的在（否则退休自身会退化成全表扫）。"""
    store = make_store(True)
    assert store.effect_retire_enabled is True
    assert "ix_effect_retire" in _indexes(store, "effect_state")
    assert "ix_effect_superseded_by" in _indexes(store, "effect_superseded")


def test_disabling_drops_the_indexes_from_an_existing_db(make_store, monkeypatch) -> None:
    """关闭时**连既有库里的也删掉**（只「不再创建」会让旧库继续白付维护成本）。"""
    store = make_store(True)
    assert "ix_effect_retire" in _indexes(store, "effect_state")
    monkeypatch.setenv("ISEKAI_EFFECT_RETIRE", "0")
    store.ensure_schema()
    assert "ix_effect_retire" not in _indexes(store, "effect_state")
    assert "ix_effect_superseded_by" not in _indexes(store, "effect_superseded")


# ---------- 停用半边：既有行为逐字节不变 ----------


def test_disabled_keeps_both_setting_effects_visible_and_records_nothing(make_store) -> None:
    """默认停用的行为契约：不记账、热路径可见集合与 B-7 之前完全一样（两条都在）。"""
    store = make_store(None)
    _put_clock(store)
    _write(store, [_effect("fx-off-a", 100, value="1")], processed_world=100)
    _write(store, [_effect("fx-off-b", 200, value="2")], processed_world=200)
    assert _ledger(store) == [], "停用态不得写 effect_superseded"
    assert _visible(store, until=200) == ["fx-off-a", "fx-off-b"], "停用态两条都留在热路径"
    assert _raw(store) == {"fx-off-a": 1, "fx-off-b": 1}


# ---------- 启用半边：至多一条活跃 + 不删事实 + 记账真的在写 ----------


def test_enabled_supersedes_the_prior_effect_but_keeps_the_fact(make_store) -> None:
    """启用态的核心断言：**记账真的发生** + 至多一条活跃 + 被取代行仍在库里。"""
    store = make_store(True)
    _put_clock(store)

    _write(store, [_effect("fx-a", 100, value="1")], processed_world=100)
    assert _ledger(store) == [], "第一条没有可取代的前项；记账必须是第二步之后才出现的"

    _write(store, [_effect("fx-b", 200, value="2")], processed_world=200)
    assert _ledger(store) == [("fx-a", "fx-b", 200)], "推进路径必须真的写进 effect_superseded"
    assert _visible(store, until=200) == ["fx-b"], "同族设值型 until_cleared 后果至多一条活跃"
    assert _raw(store) == {"fx-a": 1, "fx-b": 1}, "退休不删事实、也不改 active：A 仍然有效"
    assert store.effect_active_exists(INSTANCE, TIMELINE, "fx-a") is True, "被取代 ≠ 被解除"
    kept = store._conn.execute(
        "SELECT value FROM effect_state WHERE instance_id=? AND timeline_id=? AND id='fx-a'",
        (INSTANCE, TIMELINE),
    ).fetchone()
    assert str(kept["value"]) == "1", "被取代行的内容原样保留（可查询、可审计）"


def test_enabled_supersedes_within_a_single_batch(make_store) -> None:
    """同一批里先后写入也必须收敛：写入点若只在批间生效，这一条会红。"""
    store = make_store(True)
    _put_clock(store)
    _write(store, [_effect("fx-s-a", 100, value="1"), _effect("fx-s-b", 200, value="2")], processed_world=200)
    assert _ledger(store) == [("fx-s-a", "fx-s-b", 200)]
    assert _visible(store, until=200) == ["fx-s-b"]
    assert _raw(store) == {"fx-s-a": 1, "fx-s-b": 1}


@pytest.mark.parametrize("kind", SETTING_KINDS)
def test_every_setting_kind_retires_its_prior_value(make_store, kind: str) -> None:
    """三个设值型 kind 逐个锁住，避免「只有 environment_state 在退休」。"""
    store = make_store(True)
    _put_clock(store)
    _write(store, [_effect(f"fx-{kind}-a", 100, kind=kind, target="tgt-1", value="1")], processed_world=100)
    _write(store, [_effect(f"fx-{kind}-b", 200, kind=kind, target="tgt-1", value="2")], processed_world=200)
    assert _ledger(store) == [(f"fx-{kind}-a", f"fx-{kind}-b", 200)]
    assert _visible(store, until=200) == [f"fx-{kind}-b"]


# ---------- 推进路径的过滤（第 59 轮补：effect_constraints） ----------


def test_disabled_advance_path_sees_both_setting_effects(make_store) -> None:
    """停用态：推进路径的窄取数与 `effect_window` 一致，两条都可见（行为逐字节不变）。"""
    store = make_store(None)
    _put_clock(store)
    _write(store, [_effect("fx-ap-off-a", 100, value="1")], processed_world=100)
    _write(store, [_effect("fx-ap-off-b", 200, value="2")], processed_world=200)
    assert _ledger(store) == []
    assert _advance_path_visible(store, until=200) == ["fx-ap-off-a", "fx-ap-off-b"]


def test_enabled_advance_path_also_hides_superseded_effects(make_store) -> None:
    """启用态：**推进路径**也必须只见最近一条——否则「退休」在推进路径上等于没做。

    这条是第 59 轮新增的守卫：`effect_constraints` 原先没有 B-7 过滤，
    只修 `effect_window` 会让本条变红，而受控 A/B 会因此测不出任何收益。
    """
    store = make_store(True)
    _put_clock(store)
    _write(store, [_effect("fx-ap-on-a", 100, value="1")], processed_world=100)
    _write(store, [_effect("fx-ap-on-b", 200, value="2")], processed_world=200)
    assert _ledger(store) == [("fx-ap-on-a", "fx-ap-on-b", 200)], "前置：记账真的在写"
    assert _advance_path_visible(store, until=200) == ["fx-ap-on-b"], "推进路径也必须只见最近一条"
    assert _visible(store, until=200) == ["fx-ap-on-b"], "两条取数口径必须一致"
    assert _raw(store) == {"fx-ap-on-a": 1, "fx-ap-on-b": 1}, "仍然不删事实"


def test_enabled_advance_path_keeps_additive_kinds(make_store) -> None:
    """累加型在推进路径上同样不得被取代（与 `effect_window` 口径一致）。"""
    store = make_store(True)
    _put_clock(store)
    _write(store, [_effect("fx-ap-add-a", 100, kind="route_blocked", value="1")], processed_world=100)
    _write(store, [_effect("fx-ap-add-b", 200, kind="route_blocked", value="2")], processed_world=200)
    assert _ledger(store) == []
    assert _advance_path_visible(store, until=200) == ["fx-ap-add-a", "fx-ap-add-b"]


# ---------- 累加型守卫：不得被取代 ----------


@pytest.mark.parametrize("kind", ADDITIVE_KINDS)
def test_additive_kinds_are_never_superseded(make_store, kind: str) -> None:
    """累加型叠加有意义：启用退休也不得动它们（既有行为不变）。"""
    store = make_store(True)
    _put_clock(store)
    _write(store, [_effect(f"fx-{kind}-a", 100, kind=kind, family="ef-add", value="1")], processed_world=100)
    _write(store, [_effect(f"fx-{kind}-b", 200, kind=kind, family="ef-add", value="2")], processed_world=200)
    assert _ledger(store) == []
    assert _visible(store, until=200) == [f"fx-{kind}-a", f"fx-{kind}-b"]
    assert _raw(store) == {f"fx-{kind}-a": 1, f"fx-{kind}-b": 1}


def test_setting_kind_classification_matches_the_ruling() -> None:
    """代码里的「设值型」集合必须与裁决一致，且与累加型名单不相交。

    第 62 轮起，B-2 v2 第一步把 `relation_change` 也归入设值型（「当前关系」是设值语义，
    同一 `(持有者, 对方, 轴)` 上只应有一条生效）——所以这里的期望值**显式包含它**，
    并注明它来自哪一次裁决，避免以后有人看到「多了一项」就去删。
    """
    assert set(SETTING_EFFECT_KINDS) == set(SETTING_KINDS) | {"relation_change"}
    assert set(ADDITIVE_KINDS) & set(SETTING_EFFECT_KINDS) == set()


def test_unclassified_kind_is_treated_as_additive(make_store) -> None:
    """无法归类的 kind 一律按累加型保守处理（文档第 471 行末句）。"""
    store = make_store(True)
    _put_clock(store)
    _write(store, [_effect("fx-un-a", 100, kind="never_seen_kind", family="ef-u")], processed_world=100)
    _write(store, [_effect("fx-un-b", 200, kind="never_seen_kind", family="ef-u")], processed_world=200)
    assert _ledger(store) == []
    assert _visible(store, until=200) == ["fx-un-a", "fx-un-b"]


def test_only_until_cleared_setting_pairs_are_retired(make_store) -> None:
    """取代只发生在「设值型 + `until_cleared`」这一对上：两个方向都锁。"""
    store = make_store(True)
    _put_clock(store)
    # 方向 ①：新写入的行不是 until_cleared ⇒ 不成为取代触发者
    _write(store, [_effect("fx-nc-a", 100, target="tgt-a", value="1")], processed_world=100)
    _write(
        store,
        [_effect("fx-nc-b", 200, target="tgt-a", expiry="with_cause", value="2")],
        processed_world=200,
    )
    assert _ledger(store) == [], "非 until_cleared 的新行不是取代触发者"
    # 方向 ②：被取代的前项不是 until_cleared ⇒ 不进入退休
    _write(
        store,
        [_effect("fx-nc-c", 300, target="tgt-b", expiry="with_cause", value="3")],
        processed_world=300,
    )
    _write(store, [_effect("fx-nc-d", 400, target="tgt-b", value="4")], processed_world=400)
    assert _ledger(store) == [], "with_cause 的前项不进入取代式退休"
    assert _visible(store, until=400) == ["fx-nc-a", "fx-nc-b", "fx-nc-c", "fx-nc-d"]


# ---------- 欠账特征化：回滚后记账丢失（结论见 tools/notes/effect_superseded_snapshot.md） ----------


def test_gap_rollback_reloads_effects_but_not_the_supersede_ledger(make_store) -> None:
    """**当前行为特征化**（欠账本体，不是期望行为）。

    `effect_superseded` 已登记为 `CLEARED_ON_ROLLBACK`（回滚必须清），但没有进 `runtime_dump`
    分节，`runtime_load` 也不重放记账 ⇒ 回滚（clear + load）把同族后果行整体写回后，
    「至多一条活跃」不变量**要等到该族下一次写入才自愈**。

    若将来补上恢复侧（新增快照分节，或在装载时按 `from_world, seq, id` 重放
    `_supersede_prior_effects`），**本用例必须同步改成断言只有 1 条可见**。
    """
    store = make_store(True)
    _put_clock(store)
    _write(store, [_effect("fx-r-a", 100, value="1")], processed_world=100)
    _write(store, [_effect("fx-r-b", 200, value="2")], processed_world=200)
    assert _ledger(store) == [("fx-r-a", "fx-r-b", 200)], "前置：退休已记账"

    dump = store.runtime_dump(INSTANCE, TIMELINE, watermark=200)
    assert "effect_superseded" not in dump, "当前快照分节不含该表（欠账本体）"

    store.timeline_clear_state(TIMELINE)
    assert _ledger(store) == [], "回滚清单里的表必须被清空"
    store.runtime_load(INSTANCE, TIMELINE, dump)

    assert sorted(_raw(store)) == ["fx-r-a", "fx-r-b"], "回滚把后果行整体写回"
    assert _ledger(store) == [], "恢复侧不重建记账"
    assert _visible(store, until=200) == ["fx-r-a", "fx-r-b"], "⇒ 不变量在回滚后未恢复（当前行为）"


# ---------- 环境变量不得泄漏到同进程其他测试 ----------


def test_switch_env_is_read_per_store_and_is_restored(tmp_path) -> None:
    """开关只在建库时读一次；用 `MonkeyPatch.context()` 显式验证「用完还原」。"""
    before = os.environ.get("ISEKAI_EFFECT_RETIRE")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ISEKAI_EFFECT_RETIRE", "1")
        enabled = Store(tmp_path / "on.db")
        enabled.ensure_schema()
        assert enabled.effect_retire_enabled is True
        patch.setenv("ISEKAI_EFFECT_RETIRE", "0")
        assert enabled.effect_retire_enabled is True, "已建实例不回读环境"
        disabled = Store(tmp_path / "off.db")
        disabled.ensure_schema()
        assert disabled.effect_retire_enabled is False
        disabled.close()
        enabled.close()
    assert os.environ.get("ISEKAI_EFFECT_RETIRE") == before, "上下文退出后环境必须还原"
    expected = str(os.environ.get("ISEKAI_EFFECT_RETIRE", "0")).strip() == "1"
    fresh = Store(tmp_path / "fresh.db")
    fresh.ensure_schema()
    assert fresh.effect_retire_enabled is expected, "新实例按当前环境判定，无进程级缓存"
    fresh.close()
