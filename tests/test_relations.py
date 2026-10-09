"""B-2 第一步：关系作为**事实状态**（可声明、可查询、随回滚清空；**不接认知投影**）。

锁住四件事：① 轴与档位是闭集、强度是千分比整数；② 关系必须有**依据**（无来源的漂移不允许）；
③ 关系是「只补不覆盖」的幂等写入（单一事实源）；④ 回滚必须清空（A-7 纪律）。
另有一条**边界断言**：v1 明确不把关系接进角色认知投影。
"""

from __future__ import annotations

import io

from isekai_core.store import Store
from isekai_core.world.validate import RELATION_AXES, RELATION_GRADES, validate_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边
from samples import sample_package  # noqa: F401  仓库既有夹具


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
