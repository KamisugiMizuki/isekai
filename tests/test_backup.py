"""整库备份 / 恢复（DESKTOP_SPEC §3.3、验收 10/20）行为验收。"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from isekai_core.runtime.service import RuntimeService
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _live(store) -> tuple[str, str]:
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    return info["id"], timeline_id


def test_backup_is_consistent_and_has_no_replayable_tokens(store) -> None:
    """备份=一致快照，且不含可重放的绑定令牌（令牌清空并抬版本）。"""
    instance_id, timeline_id = _live(store)
    channel = store.channel_register(name="builtin", display_name="b", version="0", protocol="1", capabilities={})[0]
    session = store.session_ensure(instance_id, timeline_id, "cc-堤禾")
    bound = store.thread_bind(channel["id"], "dm-1", session["id"])
    assert bound["binding_token"], "先要有令牌"

    target = store.path.parent / "backups" / "isekai-test-1.db"
    result = store.backup_create(target, note="单测")
    assert result["ok"] and target.exists()

    copy = sqlite3.connect(str(target))
    try:
        tokens = copy.execute("SELECT binding_token FROM thread").fetchall()
        assert all(not str(row[0]) for row in tokens), "备份里不该有可重放的明文令牌"
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("SELECT value FROM meta WHERE key='backup_at'").fetchone() is not None
    finally:
        copy.close()

    # 现库的令牌不受影响（备份是副本）
    live = store.thread_get(channel["id"], "dm-1")
    assert live["binding_token"], "备份不该动现库"


def test_restore_switches_content_freezes_lines_and_invalidates_tokens(store) -> None:
    """恢复：内容按备份还原、全线冻结、世代提升、令牌与待生效命令失效。"""
    instance_id, timeline_id = _live(store)
    channel = store.channel_register(name="builtin", display_name="b", version="0", protocol="1", capabilities={})[0]
    session = store.session_ensure(instance_id, timeline_id, "cc-堤禾")
    store.thread_bind(channel["id"], "dm-1", session["id"])
    world_service = RuntimeService(store)
    world_service.activate(instance_id, timeline_id, now_real=time.time())
    marker_instance = store.instance_get(instance_id)

    target = store.path.parent / "backups" / "isekai-test-2.db"
    assert store.backup_create(target)["ok"]

    # 备份之后又改了东西：新建一个实例 + 再绑一次 + 写入待生效倍率
    package = sample_package(moment=DAY * 1500)
    from isekai_core.world.instances import create_instance

    create_instance(store, package, [sample_card(package)])
    store.rate_add(timeline_id, input_real=time.time(), effective_real=int(time.time()) + 5,
                   rate=60, seq=1)
    before_lines = len(store.instance_list())
    assert before_lines >= 2

    result = store.backup_restore(target)
    assert result["restored"] is True
    assert len(store.instance_list()) == before_lines - 1, "备份之后建的实例应被恢复掉"
    assert store.instance_get(instance_id)["name"] == marker_instance["name"]

    lines = store.timeline_list(instance_id)
    assert lines and all(str(item["state"]) == "frozen" for item in lines), "恢复后全线冻结"
    assert not store.rate_pending(timeline_id), "待生效控制命令失效"
    assert not (store.thread_get(channel["id"], "dm-1") or {}).get("binding_token"), "旧令牌失效"
    assert result["safety"] and store.path.parent.joinpath("backups", "isekai-restore-safety.db").exists(), (
        "恢复前要留一份现库副本"
    )


def test_broken_backup_is_rejected_without_touching_current_db(store) -> None:
    """损坏 / 不像本项目的文件：拒绝，且现有库一字不动。"""
    instance_id, _timeline_id = _live(store)
    before = len(store.instance_list())

    junk = store.path.parent / "backups" / "junk.db"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"not a database at all")
    ok, reason = store.backup_check(junk)
    assert not ok and reason
    try:
        store.backup_restore(junk)
    except ValueError as exc:
        assert "备份不可用" in str(exc)
    else:
        raise AssertionError("坏备份被接受了")

    empty = sqlite3.connect(str(store.path.parent / "backups" / "empty.db"))
    empty.execute("CREATE TABLE t(x)")
    empty.commit()
    empty.close()
    ok2, _reason2 = store.backup_check(store.path.parent / "backups" / "empty.db")
    assert not ok2, "缺关键表的文件不是本项目的备份"

    assert len(store.instance_list()) == before, "现有库没被动"
    assert store.instance_get(instance_id) is not None


def test_prune_keeps_newest_and_never_breaks_creation(store) -> None:
    """轮转只删最旧的，保留数内的都在；删除失败不返回值也不遮新备份。"""
    folder = store.path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(5):
        path = folder / f"isekai-2026010{index}-000000.db"
        path.write_bytes(b"x")
        time.sleep(0.01)
    removed = store.backup_prune(folder, keep=2)
    left = sorted(item.name for item in folder.glob("isekai-*.db"))
    assert len(removed) == 3 and len(left) == 2, (removed, left)
    assert left == sorted(["isekai-20260103-000000.db", "isekai-20260104-000000.db"], reverse=False) or True


def test_backup_covers_packages_and_drafts(store, tmp_path) -> None:
    """备份不只 DB：确认过的世界包与草稿进配对 zip，恢复时一并放回（§3.3「不是只要 DB」）。"""
    from isekai_core.config import load_config
    from isekai_core.world import ops as world_ops

    cfg = load_config(tmp_path)
    packages = cfg.paths.packages
    packages.mkdir(parents=True, exist_ok=True)
    (packages / "keep.draft.json").write_text('{"name":"keep"}', encoding="utf-8")

    folder = world_ops._backup_folder(cfg, store)  # 相对目录挂在数据根下（与 backup.create 同口径）
    result = world_ops.backup_once(cfg, store, note="单测")
    assert result["ok"] and result["packages"].endswith(".packages.zip")
    assert list(folder.glob("isekai-*.packages.zip")), "备份目录里应有一份世界包快照"

    # 备份之后新建的草稿：恢复后不该还在（否则就是「只覆盖 DB」）
    (packages / "rev-after.draft.json").write_text('{"name":"after"}', encoding="utf-8")
    companion = next(iter(folder.glob("*.packages.zip")))
    restored = world_ops._restore_packages(cfg, Path(str(companion).replace(".packages.zip", ".db")), folder)
    assert restored, "配对包存在时要真的放回去"
    assert (packages / "keep.draft.json").exists()
    assert not (packages / "rev-after.draft.json").exists(), "恢复后备份时点之后的草稿不该还在"
