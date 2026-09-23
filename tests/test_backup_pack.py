"""单文件全量备份与恢复（ONBOARDING §9.1/§9.2）的行为级判据。

真 SQLite + 真文件，不碰本机数据。判据全部落在可观察结果上：

1. 一份包 = 数据库 + 受管素材 + 非敏感偏好 + 清单；密钥不入包；
2. 坏件在预检/暂存就被拦下，现行数据不动；
3. 恢复是「暂存 → 留恢复前副本 → 换单元」；换完条数对得上，全部线暂停；
4. 中断的切换在下次启动时回退，不留「库是新的、素材还是旧的」的正常态；
5. 自动备份按保留数轮换，手动件不动。
"""

from __future__ import annotations

import json
import sqlite3
import time
import zipfile
from pathlib import Path

from isekai_core import backup_pack
from isekai_core.config import load_config
from isekai_core.runtime.service import from_config
from isekai_core.store import Store
from isekai_core.world.example import example_card, example_package
from isekai_core.world.instances import create_instance


def _world(tmp_path: Path, name: str) -> dict:
    cfg = load_config(tmp_path)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    runtime = from_config(cfg, store)
    package = example_package(name)
    cards = [example_card(package, name="堤禾"), example_card(package, name="潮生")]
    info = create_instance(store, package, cards)
    runtime.ensure_instance(info["id"], now_real=time.time())
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    runtime.activate(info["id"], timeline_id, now_real=time.time())
    return {"cfg": cfg, "store": store, "runtime": runtime, "instance_id": str(info["id"])}


def _instance_names(store: Store) -> list[str]:
    return sorted(str(item["name"]) for item in store.instance_list())


def test_pack_holds_db_assets_and_clean_config(tmp_path) -> None:
    world = _world(tmp_path, "备份世界")
    cfg, store = world["cfg"], world["store"]
    assets = Path(cfg.paths.packages) / "sample_world"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "world.json").write_text("{}", encoding="utf-8")
    cfg.paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_file.write_text("llm:\n  api_key: sk-should-not-travel\n  model: deepseek\n", encoding="utf-8")

    record = backup_pack.write_pack(cfg, store, kind="manual", note="手测")
    report = backup_pack.verify_pack(Path(record["path"]))
    with zipfile.ZipFile(record["path"]) as zf:
        names = zf.namelist()
        config_text = zf.read(backup_pack.CONFIG_PART).decode("utf-8")

    assert record["complete"] and report["complete"], report["problems"]
    assert backup_pack.DB_PART in names and backup_pack.MANIFEST in names
    assert any(name.startswith("assets/packages/sample_world/") for name in names)
    assert "sk-should-not-travel" not in config_text and "model: deepseek" in config_text
    assert report["counts"]["instances"] == 1
    assert report["manifest"]["excluded"] and report["manifest"]["format"] == backup_pack.FORMAT


def test_bad_part_is_refused_before_touching_current_data(tmp_path) -> None:
    world = _world(tmp_path, "坏件世界")
    cfg, store = world["cfg"], world["store"]
    record = backup_pack.write_pack(cfg, store, kind="manual")

    # 把清单里的一个摘要改掉：校验必须报出来，暂存必须拒绝
    path = Path(record["path"])
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read(backup_pack.MANIFEST).decode("utf-8"))
        payload = {name: zf.read(name) for name in zf.namelist() if name != backup_pack.MANIFEST}
    manifest["parts"][0]["sha256"] = "0" * 64
    payload[backup_pack.MANIFEST] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in payload.items():
            zf.writestr(name, data)

    report = backup_pack.verify_pack(path)
    staged = backup_pack.stage_pack(cfg, path)
    assert not report["complete"] and any("摘要不符" in item for item in report["problems"])
    assert staged["ok"] is False and staged["staged"] == ""
    assert not backup_pack._staging_root(cfg).exists() or not list(backup_pack._staging_root(cfg).iterdir())
    assert len(store.instance_list()) == 1, "拒绝坏件时现行数据不该被动过"


def test_restore_swaps_unit_and_pauses_every_timeline(tmp_path) -> None:
    world = _world(tmp_path, "原始世界")
    cfg, store = world["cfg"], world["store"]
    asset_file = Path(cfg.paths.packages) / "draft.md"
    asset_file.parent.mkdir(parents=True, exist_ok=True)
    asset_file.write_text("包里的草稿", encoding="utf-8")
    record = backup_pack.write_pack(cfg, store, kind="manual")
    before_names = _instance_names(store)

    # 备份之后又加了一个世界、改了草稿：恢复要把这些都换回去
    later = _world(tmp_path, "后加的世界")
    asset_file.write_text("后来改的", encoding="utf-8")
    assert len(store.instance_list()) == 2

    staged = backup_pack.stage_pack(cfg, Path(record["path"]))
    assert staged["ok"], staged["problems"]
    applied = backup_pack.apply_staged(cfg, store, staged["staged"], note="回到备份时点")

    assert applied["ok"] and applied["state"] == "done", applied["problems"]
    assert Path(applied["before"]).is_file(), "恢复前副本必须留下"
    assert _instance_names(store) == before_names
    states = {str(row["state"]) for row in store.timeline_list(later["instance_id"])}
    assert states in ({"frozen"}, set()), f"恢复后所有线暂停：{states}"
    assert asset_file.read_text(encoding="utf-8") == "包里的草稿"
    assert not list(backup_pack._staging_root(cfg).iterdir()), "切换成功后暂存要清掉"
    assert backup_pack.read_record(cfg)["state"] == "done"


def test_interrupted_switch_is_rolled_back_on_next_start(tmp_path) -> None:
    world = _world(tmp_path, "中断世界")
    cfg, store = world["cfg"], world["store"]
    record = backup_pack.write_pack(cfg, store, kind="manual")
    names_before = _instance_names(store)

    # 造一个「切到一半断电」的现场：记录是 switching，现行数据被换成了另一份
    other = _world(tmp_path, "另一份数据")
    assert len(store.instance_list()) == 2
    backup_pack._write_record(cfg, {"state": "switching", "at": time.time(), "before": record["path"], "note": "模拟中断"})
    resumed = backup_pack.resume_pending(cfg, store)

    assert resumed["state"] == "rolled_back", resumed
    assert _instance_names(store) == names_before, "回退后应回到恢复前单元"
    assert backup_pack.read_record(cfg)["state"] == "rolled_back"
    assert other["instance_id"]


def test_rotation_only_touches_auto_packs(tmp_path) -> None:
    world = _world(tmp_path, "轮换世界")
    cfg, store = world["cfg"], world["store"]
    manual = backup_pack.write_pack(cfg, store, kind="manual", note="别删我")
    autos = []
    for index in range(4):
        pack = backup_pack.write_pack(cfg, store, kind="auto", note=f"自动 {index}")
        autos.append(pack["path"])
        time.sleep(1.05)  # 名称带秒级时间戳：拉开以免重名

    removed = backup_pack.rotate(cfg, keep=2)
    listed = {item["name"] for item in backup_pack.list_packs(cfg)}

    assert len(removed) == 2 and all(name.startswith("isekai-auto-") for name in removed)
    assert Path(manual["path"]).name in listed
    assert len([name for name in listed if name.startswith("isekai-auto-")]) == 2


def test_list_reports_status_and_reuses_verification(tmp_path) -> None:
    world = _world(tmp_path, "列表世界")
    cfg, store = world["cfg"], world["store"]
    good = backup_pack.write_pack(cfg, store, kind="manual")
    broken = Path(backup_pack.packs_dir(cfg)) / "isekai-manual-19700101-000000.zip"
    broken.write_bytes(b"not a zip at all")

    listed = {item["name"]: item for item in backup_pack.list_packs(cfg)}
    first = listed[Path(good["path"]).name]
    cached = backup_pack._load_cache(cfg)[Path(good["path"]).name]["checked_at"]
    again = {item["name"]: item for item in backup_pack.list_packs(cfg)}[Path(good["path"]).name]

    assert first["status"] == "ok" and first["counts"]["instances"] == 1
    assert listed["isekai-manual-19700101-000000.zip"]["status"] == "broken"
    assert again["checked_at"] == cached, "同一份文件第二次列表不该重算摘要"
    assert sqlite3.sqlite_version  # 真 SQLite 参与（连接可用）


def test_shutdown_saved_flag_matches_a_real_pack(tmp_path, monkeypatch) -> None:
    """退出握手的判据是 `saved.ok`（界面读它决定显示成功还是「未通过完整性校验」）。

    失败时界面要能显示真实原因，成功时不能报成失败：ok 必须与包的真实完整性一致。
    """
    from isekai_core.world import ops as world_ops

    world = _world(tmp_path, "退出世界")
    monkeypatch.setattr(world_ops, "_request_exit", lambda *args, **kwargs: None)
    result = world_ops.dispatch(world["cfg"], world["store"], "app.shutdown", {})

    saved = result["saved"]
    assert saved["ok"] is True
    assert backup_pack.verify_pack(Path(saved["path"]))["complete"], "报成功就得是真完整的包"
