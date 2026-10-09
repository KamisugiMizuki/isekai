"""B-2 第一步：关系作为**事实状态**（可声明、可查询、随回滚清空；**不接认知投影**）。

锁住四件事：① 轴与档位是闭集、强度是千分比整数；② 关系必须有**依据**（无来源的漂移不允许）；
③ 关系是「只补不覆盖」的幂等写入（单一事实源）；④ 回滚必须清空（A-7 纪律）。
另有一条**边界断言**：v1 明确不把关系接进角色认知投影。
"""

from __future__ import annotations

import io
import json

from isekai_core.store import Store
from isekai_core.world.validate import RELATION_AXES, RELATION_GRADES, validate_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边
from samples import DAY, sample_package  # noqa: F401  仓库既有夹具


def _relations_package(initial: list[dict] | None = None, axes: list[str] | None = None) -> dict:
    package = sample_package()
    package["relations"] = {
        "axes": axes if axes is not None else ["宿怨", "恩情"],
        "initial": initial if initial is not None else [
            {"from": "cc-堤禾", "to": "cc-潮生", "axis": "宿怨", "档位": "中"},
        ],
    }
    return package


def test_axes_and_grades_are_closed_sets() -> None:
    assert RELATION_AXES == ("亲属", "同僚", "恩情", "债务", "宿怨", "隶属")
    assert Store.RELATION_AXES == RELATION_AXES, "包校验与运行期必须共用同一份轴声明"
    assert Store.RELATION_GRADES == RELATION_GRADES, "档位映射也必须同一份"
    assert all(0 <= value <= 1000 for value in RELATION_GRADES.values())


def test_valid_relations_declaration_passes() -> None:
    errors = validate_package(_relations_package())
    assert not [item for item in errors if "relations" in item], errors


def test_unknown_axis_is_rejected() -> None:
    errors = validate_package(_relations_package(
        initial=[{"from": "a", "to": "b", "axis": "盟约", "档位": "中"}], axes=["宿怨"],
    ))
    assert any("未知关系轴" in item or "未在 relations.axes 里启用" in item for item in errors), errors


def test_free_numeric_strength_is_rejected() -> None:
    """**核心守卫**：强度只走档位——不接受自由数值（否则「只沿依据变化」无从验收）。"""
    errors = validate_package(_relations_package(
        initial=[{"from": "a", "to": "b", "axis": "宿怨", "档位": "中", "strength": 777}],
    ))
    assert any("只允许" in item for item in errors), errors


def test_bad_grade_is_rejected() -> None:
    errors = validate_package(_relations_package(
        initial=[{"from": "a", "to": "b", "axis": "宿怨", "档位": "非常深"}],
    ))
    assert any("必须是" in item and "档位" in item for item in errors), errors


def test_relation_requires_a_basis(store, world) -> None:  # noqa: ANN001, F811
    """无依据的关系变化不允许发生（与性格单元同一纪律）。"""
    info, timeline_id, _character_id = make_instance(store, world)
    try:
        store.relation_set(
            {"instance_id": info["id"], "timeline_id": timeline_id,
             "from_id": "a", "to_id": "b", "axis": "宿怨", "strength": 500},
            basis="   ", world_seconds=0,
        )
    except ValueError:
        return
    raise AssertionError("没有依据的关系变化必须被拒绝")


def test_relation_init_is_idempotent_and_never_overwrites(store, world) -> None:  # noqa: ANN001, F811
    info, timeline_id, _character_id = make_instance(store, world)
    declared = [{"from": "cc-a", "to": "cc-b", "axis": "恩情", "档位": "深"}]
    assert store.relation_init(info["id"], timeline_id, declared, world_seconds=0) == 1
    assert store.relation_init(info["id"], timeline_id, declared, world_seconds=99) == 0, "幂等"
    assert store.relation_list(info["id"], timeline_id)[0]["strength"] == 800
    # 改写后再次 init：不得被声明值覆盖（单一事实源）
    store.relation_set(
        {"instance_id": info["id"], "timeline_id": timeline_id, "from_id": "cc-a",
         "to_id": "cc-b", "axis": "恩情", "strength": 350},
        basis="ev-1", world_seconds=10,
    )
    assert store.relation_init(info["id"], timeline_id, declared, world_seconds=999) == 0
    assert store.relation_list(info["id"], timeline_id)[0]["strength"] == 350


def test_relation_is_cleared_on_rollback(store, world) -> None:  # noqa: ANN001, F811
    from isekai_core import store_state_domains as domains

    assert "relation_state" in domains.cleared_tables(), "A-7：关系必须随回滚清空"
    info, timeline_id, _character_id = make_instance(store, world)
    store.relation_init(
        info["id"], timeline_id,
        [{"from": "cc-a", "to": "cc-b", "axis": "宿怨", "档位": "中"}], world_seconds=0,
    )
    assert store.relation_list(info["id"], timeline_id)
    store.timeline_clear_state(timeline_id)
    assert not store.relation_list(info["id"], timeline_id), "回滚必须清空关系"


def test_relations_are_not_exposed_through_cognition_in_v1() -> None:
    """边界断言：v1 明确**不接认知投影**——认知取数路径不得出现 `relation_list`。

    第二步（按「是否获知」过滤后再进认知）单独验收：串材料的风险集中在那一处，不能混进同一次发布。
    """
    src = io.open('isekai_core/runtime/service.py', encoding='utf-8').read()
    start = src.find('def cognition_project')
    assert start > 0, '未找到 cognition_project'
    end = src.find('\n    def ', start + 10)
    segment = src[start:end if end > 0 else len(src)]
    assert 'relation_list' not in segment, "v1 不接认知投影：关系不得出现在认知取数路径里"


# ---------- B-2 v2 第一步：由事件效果改变关系（**仍不接认知**） ----------
#
# 裁决书：`docs/worldruntime/B2_V2_STEP1_RULING_RELATION_EFFECT.md`
# value 是 `{持有者, 轴, 档位}` 结构体——三件事各占一个命名键，不挤进一个字符串。


def _effect_package(value) -> dict:
    package = _relations_package()
    package["events"]["families"][0]["templates"][0]["effects"] = [
        {"kind": "relation_change", "target": "cc-潮生", "value": value},
    ]
    return package


def test_relation_change_declaration_is_accepted() -> None:
    errors = validate_package(_effect_package(
        {"持有者": "cc-堤禾", "轴": "恩情", "档位": "深"}
    ))
    assert not [item for item in errors if "relation_change" in item], errors


def test_relation_change_requires_an_explicit_holder() -> None:
    """**核心守卫**：持有者必须显式声明——从 target 反推会静默建立一段**反过来的**关系。"""
    errors = validate_package(_effect_package({"轴": "恩情", "档位": "深"}))
    assert any("持有者" in item for item in errors), errors


def test_relation_change_rejects_extra_keys_and_free_values() -> None:
    """只允许三个键；自由数值（`强度: 700`）必须被拒——档位 → 千分比是固定映射。"""
    extra = validate_package(_effect_package(
        {"持有者": "cc-堤禾", "轴": "恩情", "档位": "深", "强度": 700}
    ))
    assert any("只允许 持有者 / 轴 / 档位" in item for item in extra), extra

    bad_axis = validate_package(_effect_package({"持有者": "cc-堤禾", "轴": "盟约", "档位": "深"}))
    assert any("value.轴" in item for item in bad_axis), bad_axis

    bad_grade = validate_package(_effect_package({"持有者": "cc-堤禾", "轴": "恩情", "档位": "700"}))
    assert any("value.档位" in item for item in bad_grade), bad_grade

    not_a_dict = validate_package(_effect_package("cc-堤禾:恩情:深"))
    assert any("结构体" in item for item in not_a_dict), not_a_dict


def test_relation_change_is_in_the_supported_closure() -> None:
    """声明必须与消费同批落地：进闭集 + 有优先级（否则是「包能声明、没人消费」的空效果）。"""
    from isekai_core.runtime import events
    from isekai_core.world.validate import SUPPORTED_EFFECTS

    assert "relation_change" in SUPPORTED_EFFECTS
    assert events.EFFECT_PRIORITY.get("relation_change", 0) > 0


def test_relation_change_effect_writes_relation_state_end_to_end(store, world) -> None:  # noqa: F811
    """**端到端**：注入一条 `relation_change` 后果后，真实 `advance` 必须写出那一行关系。"""
    from test_memory import _service

    info, timeline_id, character_id = make_instance(store, world)
    service = _service(store)
    service.activate(info["id"], timeline_id, now_real=1.7e9)
    service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY, max_batches=1)

    clock = store.clock_get(timeline_id)
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(clock["generation"]),
        processed_world=int(clock["processed_world"]),
        catching_up=False,
        effects=[{
            "id": "fx-rel-1", "instance_id": info["id"], "timeline_id": timeline_id,
            "event_id": "ev-rel-1", "target": "cc-堤禾", "kind": "relation_change",
            "family": "ef-rel", "value": json.dumps(
                {"持有者": "cc-潮生", "轴": "恩情", "档位": "深"}, ensure_ascii=False
            ),
            "from_world": 0, "expiry": "until_cleared", "recovery": "", "active": 1, "cleared_at": None,
        }],
    )

    service.advance(info["id"], timeline_id, now_real=1.7e9 + 4 * DAY, max_batches=1)
    rows = store.relation_list(info["id"], timeline_id)
    target = [row for row in rows if str(row["from_id"]) == "cc-潮生" and str(row["to_id"]) == "cc-堤禾"]
    assert target, f"真实 advance 必须把关系效果落成关系行（实际 {rows}）"
    assert str(target[0]["axis"]) == "恩情"
    assert int(target[0]["strength"]) == RELATION_GRADES["深"], "档位必须映射成固定千分比"
    assert str(target[0]["basis"]) == "ev-rel-1", "依据必须指向事件标识（不允许空依据）"