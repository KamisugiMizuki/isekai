"""冻结 / 归档的时间基默认值（回归：省略 now_real 不能再炸）。

现象：`runtime.timeline.archive` 走 `archive_timeline(now_real=None)` → `freeze(now_real=None)`
→ `target_world(state, None)` 抛 `TypeError: unsupported operand type(s) for -: 'NoneType' and 'float'`。
修法：`freeze` 省略时间基时取当前现实时间（归档 / 管理面这类调用方不该被迫自己算时基）。
"""

from __future__ import annotations

from test_runtime import make_instance, store, world  # noqa: F401  夹具定义在那边


def test_freeze_and_archive_without_now_real(store, world) -> None:
    info, timeline_id, _character = make_instance(store, world)
    world.activate(info["id"], timeline_id)  # 同样省略时间基
    frozen = world.freeze(info["id"], timeline_id)
    assert frozen["state"] == "frozen", frozen
    archived = world.archive_timeline(info["id"], timeline_id)
    assert archived["state"] == "archived", archived


def test_archive_op_without_now_real(store, world, tmp_path) -> None:
    """管理面路径也走同一条默认值（壳 / 探针不传 now_real 也得活）。"""
    from isekai_core.config import load_config
    from isekai_core.world import ops as world_ops

    cfg = load_config(tmp_path)
    info, timeline_id, _character = make_instance(store, world)
    world.activate(info["id"], timeline_id)
    out = world_ops.dispatch(
        cfg, store, "runtime.timeline.archive", {"instance_id": info["id"], "timeline_id": timeline_id}
    )
    assert out["timeline"]["state"] == "archived", out
