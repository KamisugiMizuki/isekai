"""快照等价性（A-4 的**不变量护栏**）：无论走全量、差量还是压缩，快照内容必须**逐字段相同**。

为什么先写这组测试（而不是先改 `snapshot_of`）：
`snapshot_of` 的输出要被**存储层的差量机制**（`SNAPSHOT_DELTA` / `MAX_DELTA_CHAIN`）与
**压缩路径**（`commit_snapshot_compress` → `SNAPSHOT_FULL`）消费。这里出错的后果不是性能，
而是「**回滚点不可用 / 祖先快照读不出来**」。所以先把不变量钉住，再谈优化。
"""

from __future__ import annotations

import json

from isekai_core.runtime import versioning
from test_memory import _ready, _service  # noqa: F401  复用既有夹具
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边
from samples import DAY  # noqa: F401  仓库既有夹具


def test_snapshot_of_carries_the_full_runtime_dump(store, world) -> None:  # noqa: ANN001, F811
    """① 快照里的 `runtime` 必须与 `runtime_dump` **逐字段相同**（不丢分节、不省行）。"""
    info, timeline_id, _character_id = make_instance(store, world)
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    snap = versioning.snapshot_of(store, info["id"], timeline_id, note="等价性")
    dump = store.runtime_dump(info["id"], timeline_id, watermark=watermark)
    assert snap["runtime"] == dump, "快照不得省略或改写 `runtime_dump` 的任何分节"
    for key in ("note", "world", "rate", "rules_version", "data_format", "seed", "sessions", "dialog"):
        assert key in snap, f"快照缺少语义字段 {key}"


def test_commit_round_trip_is_byte_identical(store, world) -> None:  # noqa: ANN001, F811
    """② 经**差量存储**读回的快照必须与提交时的内容逐字段相同（回滚点可用性）。"""
    world_service = _service(store)
    info, timeline_id, _character_id = _ready(store, world_service)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)

    commit = world_service.commit(info["id"], timeline_id, kind="manual", note="往返")
    commit_id = str(commit.get("id") or commit.get("commit_id") or "")
    assert commit_id, commit
    snap_before = versioning.snapshot_of(store, info["id"], timeline_id, note="往返")
    restored = store.commit_snapshot_get(commit_id)
    assert restored is not None, "读不回该提交的快照"
    # 逐字段比较：先做一次 JSON 规范化，避免「同值不同元组/列表」造成的假差异
    assert json.dumps(restored, sort_keys=True, ensure_ascii=False) == json.dumps(
        snap_before, sort_keys=True, ensure_ascii=False
    ), "差量存储往返后内容必须逐字段相同"


def test_compression_keeps_content_identical(store, world) -> None:  # noqa: ANN001, F811
    """③ 压缩（差量 → 全量）**不改变可观察状态**：压缩前后读回内容相同（§8 明文承诺）。"""
    world_service = _service(store)
    info, timeline_id, _character_id = _ready(store, world_service)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    commit = world_service.commit(info["id"], timeline_id, kind="manual", note="压缩")
    commit_id = str(commit.get("id") or commit.get("commit_id") or "")
    before = store.commit_snapshot_get(commit_id)
    status = store.commit_snapshot_compress(commit_id)
    after = store.commit_snapshot_get(commit_id)
    assert json.dumps(before, sort_keys=True, ensure_ascii=False) == json.dumps(
        after, sort_keys=True, ensure_ascii=False
    ), f"压缩改变了内容（status={status}）"


def test_ancestor_stays_readable_after_a_new_commit(store, world) -> None:  # noqa: ANN001, F811
    """④ 新提交**不得**让祖先快照读不出来（§8：别的线引用的祖先照旧可读）。"""
    world_service = _service(store)
    info, timeline_id, _character_id = _ready(store, world_service)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    first = world_service.commit(info["id"], timeline_id, kind="manual", note="祖先")
    first_id = str(first.get("id") or first.get("commit_id") or "")
    snapshot_before = store.commit_snapshot_get(first_id)

    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 3 * DAY)
    world_service.commit(info["id"], timeline_id, kind="manual", note="后代")

    assert store.commit_snapshot_get(first_id) is not None, "新提交后祖先快照必须仍可读"
    assert json.dumps(store.commit_snapshot_get(first_id), sort_keys=True, ensure_ascii=False) == json.dumps(
        snapshot_before, sort_keys=True, ensure_ascii=False
    ), "祖先快照内容不得被后续提交改写"
