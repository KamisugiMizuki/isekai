"""B-1 v1：压力标量 → 候选权重（写死线性形式，**不给表达式语言**）。

锁住三件事：① 压力量是**确定性纯函数**并在声明区间内夹取；② 权重只走写死的线性形式；
③ 校验**拒绝任何表达式入口**（多键、非整数系数、未声明引用）。
"""

from __future__ import annotations

import pytest

from isekai_core.runtime import events
from isekai_core.world.validate import validate_package
from samples import DAY, sample_package  # noqa: F401  仓库既有夹具
from test_runtime import world  # noqa: F401  夹具在那边（创建实例 / 初始化时钟需要它）


def _package_with_pressure(**over) -> dict:
    package = sample_package()
    pressure = {"id": "pr-tension", "name": "局势紧张", "下限": 0, "上限": 10, "初始值": 2, "drift": 1}
    pressure.update(over)
    package["pressures"] = [pressure]
    return package


def _first_template(package: dict) -> dict:
    return package["events"]["families"][0]["templates"][0]


def test_pressure_values_are_deterministic_and_clamped() -> None:
    """压力量是世界时间的纯函数：同日同值、随日推进、并在声明区间内夹取。"""
    package = _package_with_pressure()
    assert events.pressure_values(package, day_index=3)["pr-tension"] == 5       # 2 + 1×3
    assert events.pressure_values(package, day_index=3) == events.pressure_values(package, day_index=3)
    assert events.pressure_values(package, day_index=99)["pr-tension"] == 10     # 夹到上限


def test_weight_uses_the_fixed_linear_form() -> None:
    """权重 = base × (1000 + k × pressure) // 1000，且未声明压力时**逐值不变**。"""
    template = {"weight": 4, "pressure": {"id": "pr-tension", "k": 250}}
    assert events.modulated_weight(template, {"pr-tension": 4}) == 4 * 2000 // 1000
    assert events.modulated_weight(template, {"pr-tension": 0}) == 4
    assert events.modulated_weight({"weight": 4}, {"pr-tension": 9}) == 4, "未声明则不受影响"
    assert events.modulated_weight(template, {}) == 4, "压力量缺失时保守回退到基础权重"


def test_weight_never_goes_negative() -> None:
    """负权重会让「稳定选择」不可解释 ⇒ 下限为 0。"""
    template = {"weight": 3, "pressure": {"id": "pr-tension", "k": -2000}}
    assert events.modulated_weight(template, {"pr-tension": 10}) == 0


def test_pressure_shifts_the_draw() -> None:
    """机制生效：同一世界包里，声明压力后抽取结果会随压力量变化而变化（不是恒等变换）。"""
    package = _package_with_pressure()
    package["events"]["families"][0]["templates"] = [
        {"id": "et-a", "summary": "甲事件", "weight": 1, "effects": [{"kind": "public_notice", "target": ""}]},
        {"id": "et-b", "summary": "乙事件", "weight": 1, "effects": [{"kind": "public_notice", "target": ""}]},
    ]
    # 只给**一个**模板声明压力：两个模板同系数调制会等比缩放，分布不变（这是测试设计要点）
    package["events"]["families"][0]["templates"][0]["pressure"] = {"id": "pr-tension", "k": 900}
    def _series(pkg: dict) -> list[str]:
        return [
            str((events.draw_slot(pkg, seed="s", rules_version="r1", day_index=day, slot_index=0) or {}).get("template"))
            for day in range(0, 24)
        ]

    picks = _series(package)
    plain = sample_package()
    plain["events"]["families"][0]["templates"] = [
        {"id": "et-a", "summary": "甲事件", "weight": 1, "effects": [{"kind": "public_notice", "target": ""}]},
        {"id": "et-b", "summary": "乙事件", "weight": 1, "effects": [{"kind": "public_notice", "target": ""}]},
    ]
    baseline = _series(plain)
    assert picks != baseline, (
        "声明压力量后抽取**逐日序列**应发生变化（比较集合太粗：两种情况下集合都是 {et-a, et-b}）"
    )


def test_declaration_validation_accepts_a_good_pressure() -> None:
    errors = validate_package(_package_with_pressure())
    assert not [item for item in errors if "pressures" in item or ".pressure" in item], errors


def test_expression_entry_is_rejected() -> None:
    """**核心守卫**：`pressure` 只允许 id / k 两个键——多一个键就等于给表达式语言开口子。"""
    package = _package_with_pressure()
    _first_template(package)["pressure"] = {"id": "pr-tension", "k": 100, "expr": "base*(1+k*p)"}
    errors = validate_package(package)
    assert any("只允许 id / k" in item for item in errors), errors


def test_undeclared_pressure_reference_is_rejected() -> None:
    package = _package_with_pressure()
    _first_template(package)["pressure"] = {"id": "pr-不存在", "k": 100}
    errors = validate_package(package)
    assert any("未声明的压力量" in item for item in errors), errors


def test_non_integer_coefficient_is_rejected() -> None:
    package = _package_with_pressure()
    _first_template(package)["pressure"] = {"id": "pr-tension", "k": 1.5}
    errors = validate_package(package)
    assert any(".pressure.k" in item for item in errors), errors


def test_initial_value_must_be_inside_the_declared_range() -> None:
    errors = validate_package(_package_with_pressure(初始值=99))
    assert any("必须落在一开始声明的区间内" in item for item in errors), errors


# ---------- B-1 v2：由事件效果改变压力量（累积增量 Δ） ----------
#
# 裁决书：`docs/worldruntime/B1_V2_RULING_PRESSURE_STATE.md`
# 核心纪律：Δ 是**增量**（基线公式仍是 `pressure_values` 单一真源）；
# 未声明 pressures / Δ 恒为 0 的世界包，行为与 v1 **逐值相同**。

import sqlite3  # noqa: E402

from isekai_core.store import Store  # noqa: E402

INSTANCE = "in-pressure"
TIMELINE = "tl-pressure"


def _delta_of(package: dict, delta: dict[str, int], *, day_index: int) -> int:
    return int(events.pressure_values(package, day_index=day_index, delta=delta)["pr-tension"])


def test_delta_defaults_to_v1_values_exactly() -> None:
    """**回归**：`delta` 缺省 / 空映射 / 全零 ⇒ 与 v1 逐值相同。"""
    package = _package_with_pressure()
    for delta in (None, {}, {"pr-tension": 0}, {"pr-别的": 5}):
        assert _delta_of(package, delta, day_index=3) == events.pressure_values(
            package, day_index=3
        )["pr-tension"], f"delta={delta!r} 时不得改变取值"


def test_delta_shifts_the_value_and_is_clamped() -> None:
    """Δ 真的改变取值，且生效值仍被夹在声明域内（Δ 不得把世界推到域外）。"""
    package = _package_with_pressure()          # 基线 day3 = 5，域 [0,10]
    assert _delta_of(package, {"pr-tension": 3}, day_index=3) == 8
    assert _delta_of(package, {"pr-tension": 100}, day_index=3) == 10, "夹到上限"
    assert _delta_of(package, {"pr-tension": -100}, day_index=3) == 0, "夹到下限"


def test_delta_changes_the_draw_series() -> None:
    """机制生效：同一世界包，Δ 不同 ⇒ 抽取**逐日序列**发生变化。"""
    package = _package_with_pressure()
    package["events"]["families"][0]["templates"] = [
        {"id": "et-a", "summary": "甲事件", "weight": 1,
         "effects": [{"kind": "public_notice", "target": ""}], "pressure": {"id": "pr-tension", "k": 900}},
        {"id": "et-b", "summary": "乙事件", "weight": 1,
         "effects": [{"kind": "public_notice", "target": ""}]},
    ]

    def _series(delta: dict[str, int]) -> list[str]:
        return [
            str((events.draw_slot(
                package, seed="s", rules_version="r1", day_index=day, slot_index=0,
                pressure_delta=delta,
            ) or {}).get("template"))
            for day in range(0, 24)
        ]

    assert _series({}) == _series({"pr-别的": 9}), "无关的 Δ 不得影响抽取（确定性）"
    assert _series({}) != _series({"pr-tension": 10}), "Δ 改变时必须改变逐日序列"


def test_draw_slot_is_deterministic_with_a_delta() -> None:
    """给定 Δ，抽取是确定性的（同输入同输出）。"""
    package = _package_with_pressure()
    delta = {"pr-tension": 4}
    first = events.draw_slot(package, seed="s", rules_version="r1", day_index=5, slot_index=0,
                             pressure_delta=delta)
    second = events.draw_slot(package, seed="s", rules_version="r1", day_index=5, slot_index=0,
                              pressure_delta=delta)
    assert first == second


@pytest.fixture
def store(tmp_path):
    handle = Store(tmp_path / "isekai.db")
    handle.ensure_schema()
    yield handle
    handle.close()


def test_pressure_apply_writes_rows_and_is_idempotent(store) -> None:
    """**「不允许建了表没人写」**：写入方真的落行；同一 Δ 重算 ⇒ 覆盖写、不累加。"""
    store.pressure_apply(INSTANCE, TIMELINE, {"pr-tension": 3}, world_seconds=100)
    assert store.pressure_list(INSTANCE, TIMELINE) == {"pr-tension": 3}
    # 重算同一个 Δ：必须是**覆盖**而不是累加（Δ 是后果的纯函数）
    store.pressure_apply(INSTANCE, TIMELINE, {"pr-tension": 3}, world_seconds=200)
    assert store.pressure_list(INSTANCE, TIMELINE) == {"pr-tension": 3}, "不得重复累加"
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM pressure_state WHERE instance_id=? AND timeline_id=?",
        (INSTANCE, TIMELINE),
    ).fetchone()[0]
    assert rows == 1, "同一压力量只能有一行（主键）"


def test_pressure_state_clears_on_rollback(store) -> None:
    """A-7 登记生效：回滚必须清空 `pressure_state`（否则回滚后会把已撤销事件的增量继续算进去）。"""
    store.pressure_apply(INSTANCE, TIMELINE, {"pr-tension": 7}, world_seconds=100)
    assert store.pressure_list(INSTANCE, TIMELINE) == {"pr-tension": 7}
    store.timeline_clear_state(TIMELINE)
    assert store.pressure_list(INSTANCE, TIMELINE) == {}


def test_pressure_change_effect_is_in_the_supported_closure() -> None:
    """声明必须与消费同批落地：进闭集 + 有优先级（否则是「包能声明、没人消费」的空效果）。"""
    from isekai_core.world.validate import SUPPORTED_EFFECTS

    assert "pressure_change" in SUPPORTED_EFFECTS
    assert events.EFFECT_PRIORITY.get("pressure_change", 0) > 0


def _package_with_pressure_effect(**effect_over) -> dict:
    package = _package_with_pressure()
    effect = {"kind": "pressure_change", "target": "pr-tension", "value": 2}
    effect.update(effect_over)
    _first_template(package)["effects"] = [effect]
    return package


def test_pressure_change_declaration_is_accepted() -> None:
    errors = validate_package(_package_with_pressure_effect())
    assert not [item for item in errors if "pressure_change" in item], errors


def test_pressure_change_rejects_extra_keys() -> None:
    """**核心守卫**：只允许 kind / target / value——多一个键就等于给表达式语言开口子。"""
    errors = validate_package(_package_with_pressure_effect(expr="p*2"))
    assert any("只允许 kind / target / value" in item for item in errors), errors


def test_pressure_change_rejects_undeclared_target() -> None:
    errors = validate_package(_package_with_pressure_effect(target="pr-不存在"))
    assert any("未声明的压力量" in item for item in errors), errors


def test_pressure_change_rejects_non_integer_value() -> None:
    """禁止小数 / 布尔：压力量是整数域，浮点会引入不可解释的漂移。"""
    for bad in (1.5, "2", True):
        errors = validate_package(_package_with_pressure_effect(value=bad))
        assert any("压力增量必须是整数" in item for item in errors), (bad, errors)


def test_pressure_change_rejects_an_oversized_delta() -> None:
    """一条效果不得把世界掀翻：|增量| 不得超过声明域宽度（此处上限 10、下限 0 ⇒ 宽度 10）。"""
    errors = validate_package(_package_with_pressure_effect(value=99))
    assert any("超过压力量" in item for item in errors), errors


def test_pressure_delta_helper_only_counts_declared_integer_changes() -> None:
    """折算规则：只认已声明的压力量 + 整数值；同压力量的多条后果**累加**。"""
    from isekai_core.runtime.service import RuntimeService

    import inspect

    params = list(inspect.signature(RuntimeService._pressure_delta).parameters)
    assert params[0] == "self" and "active_effects" in params

    class _Stub:
        _pressure_delta = RuntimeService._pressure_delta

    stub = _Stub()
    declared = {"pr-tension"}
    effects = [
        {"kind": "pressure_change", "target": "pr-tension", "value": "3"},   # 字符串整数可折算
        {"kind": "pressure_change", "target": "pr-tension", "value": -1},    # 负增量合法
        {"kind": "pressure_change", "target": "pr-未声明", "value": 99},      # 未声明 ⇒ 忽略
        {"kind": "pressure_change", "target": "pr-tension", "value": "很多"},  # 非整数 ⇒ 忽略
        {"kind": "environment_state", "target": "pr-tension", "value": 5},   # 别的效果类 ⇒ 不算
    ]
    assert stub._pressure_delta(effects, declared=declared, until=100) == {"pr-tension": 2}


# ---------- 端到端：真实 `advance` 必须真的写出 pressure_state ----------


def _configured_instance(store, world):
    """建一个**声明了 pressures** 的实例，并由 `world.ensure_instance` 初始化时钟与派生状态。

    注入后果必须**在时钟初始化之后**（否则 `clock_get` 还没有行）。
    """
    from isekai_core.world.instances import create_instance
    from samples import sample_card

    package = _package_with_pressure()
    info = create_instance(store, package, [sample_card(package)])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world.ensure_instance(info["id"], now_real=1.7e9)
    return info, timeline_id


def test_real_advance_writes_the_pressure_state(store, world) -> None:  # noqa: F811
    """**「不允许建了表没人写」**：给实例声明 pressures 并注入一条后果后，真实 `advance` 必须落行。

    这条是端到端的：走 `service.advance`（而不是直接调 `pressure_apply`），
    因此它同时验证「声明被读到」与「折算结果被写进 pressure_state」两段接线。
    """
    from test_memory import _service

    info, timeline_id = _configured_instance(store, world)
    service = _service(store)
    service.activate(info["id"], timeline_id, now_real=1.7e9)
    service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY, max_batches=1)

    # 注入一条效果为 pressure_change 的**已固化后果**（走与生产同一条 `apply_runtime_batch`）
    clock = store.clock_get(timeline_id)
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(clock["generation"]),
        processed_world=int(clock["processed_world"]),
        catching_up=False,
        effects=[{
            "id": "fx-press-1", "instance_id": info["id"], "timeline_id": timeline_id,
            "event_id": "ev-press-1", "target": "pr-tension", "kind": "pressure_change",
            "family": "ef-press", "value": "-2", "from_world": 0,
            "expiry": "until_cleared", "recovery": "", "active": 1, "cleared_at": None,
        }],
    )

    service.advance(info["id"], timeline_id, now_real=1.7e9 + 4 * DAY, max_batches=1)
    assert store.pressure_list(info["id"], timeline_id) == {"pr-tension": -2}, (
        "真实 advance 必须把后果折算进 pressure_state（不是只有直接调 pressure_apply 才会写）"
    )
