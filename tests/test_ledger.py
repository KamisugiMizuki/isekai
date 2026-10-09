"""B-3+B-9 v1：域外账本（`world_ledger`）+ 点名规则的结构断言。

账本必须**有写入方**（否则就是 A-9b 那一类空状态域），且**数字只能经账本变更**（单一事实源）。
点名规则在本内核当前形态下是**恒真**的：不存在匿名大众，「个体」全部来自角色卡——
所以它的 v1 形式是一条**结构断言**：核心里不得出现「批量物化匿名个体」的路径。
"""

from __future__ import annotations

import io

import pytest

from isekai_core.world.validate import validate_package
from samples import DAY, sample_package  # noqa: F401  仓库既有夹具
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _ledger_package(entry: dict | None = None) -> dict:
    package = sample_package()
    package["ledger"] = [entry or {
        "scope_kind": "region", "scope_id": "pl-1", "key": "人口", "初始值": 1200, "单位": "人",
    }]
    return package


def test_ledger_declaration_is_validated() -> None:
    errors = validate_package(_ledger_package())
    assert not [item for item in errors if item.startswith("ledger")], errors


def test_unknown_scope_kind_is_rejected() -> None:
    errors = validate_package(_ledger_package({
        "scope_kind": "planet", "scope_id": "pl-1", "key": "人口", "初始值": 1,
    }))
    assert any("scope_kind" in item for item in errors), errors


def test_derivation_fields_are_rejected_in_v1() -> None:
    """**核心守卫**：v1 不做推导式——多带一个 `expr` 就等于给「账本数字」开口子。"""
    errors = validate_package(_ledger_package({
        "scope_kind": "region", "scope_id": "pl-1", "key": "人口", "初始值": 1,
        "expr": "人口 * 税率",
    }))
    assert any("只允许" in item for item in errors), errors


def test_non_integer_initial_value_is_rejected() -> None:
    errors = validate_package(_ledger_package({
        "scope_kind": "region", "scope_id": "pl-1", "key": "人口", "初始值": "很多",
    }))
    assert any("必须是整数" in item for item in errors), errors


def test_ledger_is_written_and_is_idempotent(store, world) -> None:  # noqa: ANN001, F811
    """写入方存在：推进后账本出现声明的键；再次推进**不覆盖**已有值（只补不覆盖）。"""
    from test_memory import _ready, _service  # noqa: F401  复用既有夹具

    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    declared = [{
        "scope_kind": "region", "scope_id": "pl-1", "key": "人口", "初始值": 1200, "单位": "人",
    }]
    assert store.ledger_init(info["id"], timeline_id, declared, world_seconds=0) == 1
    assert store.ledger_init(info["id"], timeline_id, declared, world_seconds=99) == 0, "幂等：不重复写"
    rows = store.ledger_list(info["id"], timeline_id, scope_kind="region")
    assert [(row["key"], row["value"], row["unit"]) for row in rows] == [("人口", 1200, "人")]

    # 改写已有值后再次 init，不得被初始值覆盖（账本数字只能经账本变更）
    store.ledger_put({
        "instance_id": info["id"], "timeline_id": timeline_id, "scope_kind": "region",
        "scope_id": "pl-1", "key": "人口", "value": 1310, "unit": "人", "updated_world": 5,
    })
    assert store.ledger_init(info["id"], timeline_id, declared, world_seconds=999) == 0
    assert store.ledger_list(info["id"], timeline_id, scope_kind="region")[0]["value"] == 1310


def test_ledger_is_cleared_on_rollback(store, world) -> None:  # noqa: ANN001, F811
    """A-7 纪律：账本必须随回滚清空——否则回滚后会从「未来的账」继续。"""
    from isekai_core import store_state_domains as domains

    assert "world_ledger" in domains.cleared_tables()
    info, timeline_id, character_id = make_instance(store, world)
    store.ledger_put({
        "instance_id": info["id"], "timeline_id": timeline_id, "scope_kind": "org",
        "scope_id": "og-1", "key": "财力", "value": 7, "unit": "", "updated_world": 0,
    })
    assert store.ledger_list(info["id"], timeline_id)
    store.timeline_clear_state(timeline_id)
    assert not store.ledger_list(info["id"], timeline_id), "回滚必须清空账本"


def test_no_anonymous_individual_materialization_path() -> None:
    """点名规则的结构断言：核心不得出现「按人口批量物化具名个体」的路径。

    本内核没有匿名大众（个体一律来自角色卡），所以点名规则当前恒真；
    这条断言的作用是**在以后引入匿名人口时把红线钉住**——一旦有人写出批量物化，这里会红。
    """
    src = io.open('isekai_core/runtime/service.py', encoding='utf-8').read()
    forbidden = ("for _ in range(population", "range(int(row[\"value\"])", "range(ledger", "for _ in range(int(")
    hits = [token for token in forbidden if token in src]
    assert not hits, f"出现疑似「按账本量批量物化个体」的写法：{hits}"


# ---------- B-3+B-9 v2：账本推导式（声明式纯函数，**没有表达式语言**） ----------
#
# 裁决书：`docs/worldruntime/B3_B9_V2_RULING_LEDGER_DERIVED.md`
# 三条纪律：① 算子闭集只有四种；② 推导值**不落库**（单一事实源）；③ 单趟无环确定性。


def _derived_package(derived: list[dict] | None = None) -> dict:
    package = sample_package()
    package["ledger"] = [
        {"scope_kind": "region", "scope_id": "pl-1", "key": "人口", "初始值": 1200, "单位": "人"},
        {"scope_kind": "region", "scope_id": "pl-1", "key": "税率", "初始值": 30, "单位": "千分比"},
    ]
    package["derived"] = derived if derived is not None else [
        {"id": "税收", "scope_kind": "region", "scope_id": "pl-1",
         "op": "积", "a": "人口", "b": "税率", "scale": 1000, "unit": "石"},
    ]
    return package


def test_derived_ops_are_a_closed_set() -> None:
    """算子闭集恰好四种——这是「不给表达式语言」这条纪律的具体边界。"""
    from isekai_core.runtime import ledger

    assert tuple(ledger.DERIVED_OPS) == ("和", "差", "积", "比")


@pytest.mark.parametrize(
    ("op", "a", "b", "scale", "expected"),
    [
        ("和", 1200, 30, 1000, 1230),
        ("差", 1200, 30, 1000, 1170),
        ("积", 1200, 30, 1000, 36),      # 千分比整除
        ("比", 1200, 30, 1000, 40000),   # a × 1000 // b
    ],
)
def test_each_operator_matches_hand_computed_values(op, a, b, scale, expected) -> None:
    from isekai_core.runtime import ledger

    spec = {"id": "d", "op": op, "a": "a", "b": "b", "scale": scale}
    assert ledger.evaluate([spec], {"a": a, "b": b}) == {"d": expected}


def test_division_by_zero_returns_zero_instead_of_raising() -> None:
    """除零回 0（不抛错）：账本要能被稳定读取，0 是「无意义值」的保守表达。"""
    from isekai_core.runtime import ledger

    assert ledger.evaluate([{"id": "d", "op": "比", "a": "a", "b": "z"}], {"a": 10, "z": 0}) == {"d": 0}


def test_derived_is_deterministic_and_supports_chains() -> None:
    """单趟、确定性；后声明的推导键可引用先声明的（链式）。"""
    from isekai_core.runtime import ledger

    specs = [
        {"id": "d1", "op": "积", "a": "人口", "b": "税率", "scale": 1000},
        {"id": "d2", "op": "和", "a": "d1", "b": "人口"},
    ]
    stored = {"人口": 1200, "税率": 30}
    first = ledger.evaluate(specs, stored)
    assert first == {"d1": 36, "d2": 1236}
    assert ledger.evaluate(specs, stored) == first, "同输入同输出"
    assert stored == {"人口": 1200, "税率": 30}, "evaluate 不得改动入参"


def test_derived_declaration_is_validated() -> None:
    errors = validate_package(_derived_package())
    assert not [item for item in errors if item.startswith("derived")], errors


def test_unknown_operator_is_rejected() -> None:
    errors = validate_package(_derived_package([
        {"id": "税收", "scope_kind": "region", "scope_id": "pl-1",
         "op": "幂", "a": "人口", "b": "税率"},
    ]))
    assert any("未支持的算子" in item for item in errors), errors


def test_expression_keys_are_rejected() -> None:
    """**核心守卫**：多一个 `expr` 就等于给账本开一个脚本引擎入口。"""
    errors = validate_package(_derived_package([
        {"id": "税收", "scope_kind": "region", "scope_id": "pl-1",
         "op": "积", "a": "人口", "b": "税率", "expr": "a*b/1000"},
    ]))
    assert any("只允许 id / scope_kind" in item for item in errors), errors


def test_derived_must_not_shadow_a_stored_key() -> None:
    """**单一事实源守卫**：推导键不得与同作用域的存储键重名。"""
    errors = validate_package(_derived_package([
        {"id": "人口", "scope_kind": "region", "scope_id": "pl-1", "op": "和", "a": "人口", "b": "税率"},
    ]))
    assert any("重名" in item for item in errors), errors


def test_derived_must_reference_declared_keys() -> None:
    errors = validate_package(_derived_package([
        {"id": "税收", "scope_kind": "region", "scope_id": "pl-1", "op": "积", "a": "人口", "b": "不存在"},
    ]))
    assert any("未声明的账本键" in item for item in errors), errors


def test_derived_cannot_reference_a_later_declaration() -> None:
    """只允许引用**更早声明**的推导键 ⇒ 结构上无环（不需要拓扑排序或迭代收敛）。"""
    errors = validate_package(_derived_package([
        {"id": "d1", "scope_kind": "region", "scope_id": "pl-1", "op": "和", "a": "人口", "b": "d2"},
        {"id": "d2", "scope_kind": "region", "scope_id": "pl-1", "op": "和", "a": "人口", "b": "税率"},
    ]))
    assert any("未声明的账本键" in item for item in errors), errors


def test_derived_scale_must_be_a_positive_integer() -> None:
    for bad in (0, -5, 1.5, True, "1000"):
        errors = validate_package(_derived_package([
            {"id": "税收", "scope_kind": "region", "scope_id": "pl-1",
             "op": "积", "a": "人口", "b": "税率", "scale": bad},
        ]))
        assert any("scale" in item for item in errors), (bad, errors)


def test_derived_values_are_computed_and_not_persisted(store, world) -> None:  # noqa: ANN001, F811
    """**单一事实源**：推导值算得出、但**不存在于 `world_ledger` 里**，也不随回滚消失（因为它本就不落库）。"""
    info, timeline_id, character_id = make_instance(store, world)
    stored = [
        {"scope_kind": "region", "scope_id": "pl-1", "key": "人口", "初始值": 1200, "单位": "人"},
        {"scope_kind": "region", "scope_id": "pl-1", "key": "税率", "初始值": 30, "单位": "千分比"},
    ]
    assert store.ledger_init(info["id"], timeline_id, stored, world_seconds=0) == 2
    specs = [{"id": "税收", "scope_kind": "region", "scope_id": "pl-1",
              "op": "积", "a": "人口", "b": "税率", "scale": 1000, "unit": "石"}]

    derived = store.ledger_derived(info["id"], timeline_id, specs)
    assert [(row["key"], row["value"], row["unit"]) for row in derived] == [("税收", 36, "石")]

    # 关键断言：它**不在**存储表里（不是第二份事实）
    keys = {str(row["key"]) for row in store.ledger_list(info["id"], timeline_id)}
    assert keys == {"人口", "税率"}, f"推导值不得落库，实际存储键 = {keys}"

    # 未声明推导式 ⇒ 返回空（既有包零影响）
    assert store.ledger_derived(info["id"], timeline_id, None) == []
    assert store.ledger_derived(info["id"], timeline_id, []) == []

