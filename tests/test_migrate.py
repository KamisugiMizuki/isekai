"""从旧开发目录迁移（ONBOARDING §3.2）的行为级判据：真 SQLite + 真文件。

判据落在可观察结果上：

1. 检查是真读：不是本项目的目录、程序还在跑、数据版本不符都各自说清；
2. 源目录只读：迁移完源目录一个文件都没少、没改；
3. 目标有用户资产就拒绝（不合并数据库），并给出两条正确路径；
4. 迁移成功：世界与素材都过来，全部时间线暂停，绑定令牌不跟着来，密钥不搬；
5. 偏好按「目标优先」合并：目标已有的值不动，缺的用旧值补。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from isekai_core import backup_pack, migrate
from isekai_core.app import pid_alive  # noqa: F401  —— 供下面的「旧程序在跑」用例对照
from isekai_core.config import load_config
from isekai_core.runtime.service import from_config
from isekai_core.store import Store
from isekai_core.world.example import example_card, example_package
from isekai_core.world.instances import create_instance


def _make_root(root: Path, name: str) -> dict:
    """造一个有世界的旧数据根（等于「用户以前那台机器上的开发目录」）。"""
    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    runtime = from_config(cfg, store)
    package = example_package(name)
    cards = [example_card(package, name="堤禾"), example_card(package, name="潮生")]
    info = create_instance(store, package, cards)
    runtime.ensure_instance(info["id"], now_real=time.time())
    timeline = store.timeline_list(info["id"])[0]["id"]
    runtime.activate(info["id"], timeline, now_real=time.time())
    (cfg.paths.packages / "draft.md").parent.mkdir(parents=True, exist_ok=True)
    (cfg.paths.packages / "draft.md").write_text("旧目录里的素材", encoding="utf-8")
    return {"cfg": cfg, "store": store, "instance_id": str(info["id"]), "timeline_id": str(timeline)}


def _target(tmp_path: Path) -> dict:
    cfg = load_config(tmp_path / "target")
    store = Store(cfg.paths.db)
    store.ensure_schema()
    return {"cfg": cfg, "store": store}


VOLATILE = ("-wal", "-shm", ".lock")


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """源目录的「内容」快照：跳过 WAL / 共享内存 / 锁这类本来就会变的东西。"""
    import hashlib

    out: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.endswith(VOLATILE):
            out[str(path.relative_to(root))] = (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    return out


def test_inspect_reports_bad_dirs_and_missing_db(tmp_path) -> None:
    cfg = load_config(tmp_path / "target")
    store = Store(cfg.paths.db)
    store.ensure_schema()
    try:
        empty = tmp_path / "empty"
        empty.mkdir()
        report = migrate.inspect(cfg, empty)
        assert not report["ok"] and "data/isekai.db" in report["problems"][0]
        missing = migrate.inspect(cfg, tmp_path / "nope")
        assert not missing["ok"] and "不是一个目录" in missing["problems"][0]
        unknown = migrate.inspect(cfg, tmp_path)  # 有这个目录，但没有 data/isekai.db
        assert not unknown["ok"]
    finally:
        store.close()


def test_refuses_when_source_is_running_or_version_mismatch(tmp_path) -> None:
    old = _make_root(tmp_path / "old", "旧世界")
    target = _target(tmp_path)
    try:
        # 旧程序还开着（写一份指向自己的锁：当前进程一定活着）
        import os

        lock = old["cfg"].paths.lock
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        running = migrate.inspect(target["cfg"], old["cfg"].paths.root)
        assert not running["ok"] and running["running"] is True
        assert any("还在运行" in item for item in running["problems"])

        # 陈旧锁（pid 不存在）不算「在跑」
        lock.write_text(json.dumps({"pid": 999999}), encoding="utf-8")
        stale = migrate.inspect(target["cfg"], old["cfg"].paths.root)
        assert stale["ok"] and stale["running"] is False
        assert stale["counts"]["instances"] == 1 and stale["assets"] == 1

        # 数据格式版本不符：直接说清，不硬搬
        import sqlite3

        conn = sqlite3.connect(old["cfg"].paths.db)
        conn.execute("UPDATE meta SET value='999' WHERE key='schema'")
        conn.commit()
        conn.close()
        version = migrate.inspect(target["cfg"], old["cfg"].paths.root)
        assert not version["ok"] and any("版本不符" in item for item in version["problems"])
    finally:
        old["store"].close()
        target["store"].close()


def test_refuses_when_target_has_user_assets(tmp_path) -> None:
    old = _make_root(tmp_path / "old", "旧世界")
    target = _target(tmp_path)
    try:
        create_instance(target["store"], example_package("目标世界"), [example_card(example_package("x"), name="甲")])
        result = migrate.run(target["cfg"], target["store"], old["cfg"].paths.root)
        assert result["ok"] is False and result["state"] == "target_not_empty"
        assert "导入一个世界" in result["problems"][0] and "恢复全部数据" in result["problems"][0]
        assert len(target["store"].instance_list()) == 1, "拒绝时目标数据不该被动过"
    finally:
        old["store"].close()
        target["store"].close()


def test_migrate_brings_world_assets_and_pauses_timelines(tmp_path) -> None:
    old = _make_root(tmp_path / "old", "旧世界")
    target = _target(tmp_path)
    old_root = Path(old["cfg"].paths.root)
    before = _snapshot(old_root)
    try:
        result = migrate.run(target["cfg"], target["store"], old_root, note="迁移验收")
        assert result["ok"] is True, result.get("problems")
        names = sorted(str(item["name"]) for item in target["store"].instance_list())
        assert len(names) == 1 and names[0].lower().startswith("旧世界".lower()), names
        states = {str(row["state"]) for item in target["store"].instance_list() for row in target["store"].timeline_list(str(item["id"]))}
        assert states == {"frozen"}, f"迁移后所有线都要暂停：{states}"
        assert (Path(target["cfg"].paths.packages) / "draft.md").read_text(encoding="utf-8") == "旧目录里的素材"
        assert Path(result["kept_source"]) == old_root and Path(result["before"]).is_file(), "要有恢复前副本与源目录说明"
        assert "API 密钥与通道凭据" in result["excluded"]
        # 绑定令牌不跟着来（迁移不等于携带可重放的连接凭据）
        rows = target["store"]._conn.execute("SELECT COUNT(*) FROM thread WHERE binding_token != ''").fetchone()
        assert int(rows[0]) == 0
    finally:
        old["store"].close()
        target["store"].close()
    after = _snapshot(old_root)
    assert after.keys() == before.keys(), "源目录不该多出或少掉文件"
    # 素材要一个字节都没动（下面逐个比对）；数据库是 SQLite 自己会在关库时做检查点，所以看「还能读、世界还在」
    for key, value in before.items():
        if key.endswith("isekai.db"):
            continue
        assert after[key] == value, f"源目录被改动了：{key}"
    import sqlite3

    conn = sqlite3.connect(f"file:{old_root / 'data' / 'isekai.db'}?mode=ro", uri=True)
    try:
        assert str(conn.execute("PRAGMA integrity_check").fetchone()[0]) == "ok"
        assert int(conn.execute("SELECT COUNT(*) FROM instance").fetchone()[0]) == 1
    finally:
        conn.close()


def test_prefs_merge_keeps_target_values_and_fills_gaps(tmp_path) -> None:
    old = _make_root(tmp_path / "old", "旧世界")
    target = _target(tmp_path)
    old_cfg_path = Path(old["cfg"].paths.config_file)
    old_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    old_cfg_path.write_text(
        "llm:\n  api_key: sk-old-should-not-travel\n  model: old-model\nbackup:\n  keep: 11\n",
        encoding="utf-8",
    )
    target_cfg_path = Path(target["cfg"].paths.config_file)
    target_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    target_cfg_path.write_text("llm:\n  api_key: sk-target-key\n  model: target-model\n", encoding="utf-8")
    try:
        result = migrate.run(target["cfg"], target["store"], old["cfg"].paths.root)
        assert result["ok"] is True, result.get("problems")
        merged = target_cfg_path.read_text(encoding="utf-8")
        pack_report = backup_pack.verify_pack(Path(result["pack"]["path"]))
        with __import__("zipfile").ZipFile(result["pack"]["path"]) as zf:
            inside = zf.read(backup_pack.CONFIG_PART).decode("utf-8")
        assert "sk-target-key" in merged, "目标已有的密钥不能被搬掉"
        assert "target-model" in merged, "目标已有的值优先"
        assert "keep: 11" in merged, "旧根里目标没有的偏好要补上"
        assert "sk-old-should-not-travel" not in merged and "sk-old-should-not-travel" not in inside
        assert result["prefs_merged"] and pack_report["complete"]
    finally:
        old["store"].close()
        target["store"].close()
