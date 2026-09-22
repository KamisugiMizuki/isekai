"""从旧的开发目录迁移到当前数据根（ONBOARDING_AND_RECOVERY §3.2）。

用户用原生目录选择器指定旧根，不扫描整盘找项目。顺序固定：
**检查 → 提示关闭旧程序 → 确认目标还没有用户资产 → 复制到暂存区 → 完整校验 → 启用**。

- 源目录始终保留：这里只读它，不移动、不删除。
- 不迁移 API 密钥、通道凭据、进程锁、运行句柄、日志与缓存（打包时整条剔除）。
- 迁移后所有时间线先暂停（走的正是恢复的那条切换路径，自带恢复前副本与失败回退）。
- 目标已有正式数据时不做合并：让用户走「导入一个世界」或「恢复全部数据」。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import backup_pack
from .log import get_logger
from .store import SCHEMA_VERSION, Store

log = get_logger("migrate")

REQUIRED_TABLES = {"meta", "instance", "timeline"}


def _read_lock(source_root: Path) -> dict[str, Any]:
    path = source_root / "data" / "core.lock"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"unreadable": True}


def inspect(cfg: Any, source: str | Path) -> dict[str, Any]:
    """旧根体检：是不是本项目的数据目录、能不能读、有多少世界、旧程序是否还开着。"""
    root = Path(str(source)).expanduser()
    problems: list[str] = []
    counts: dict[str, int] = {}
    db_path = root / "data" / "isekai.db"
    if not root.is_dir():
        problems.append("这个位置不是一个目录")
    elif not db_path.is_file():
        problems.append("这份目录里没有 data/isekai.db：看着不像 isekai 的数据目录")
    else:
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            check = conn.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]) != "ok":
                problems.append("旧目录里的数据库没通过完整性检查")
            tables = {
                str(item[0])
                for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            missing = REQUIRED_TABLES - tables
            if missing:
                problems.append(f"旧目录的数据库缺关键表：{'、'.join(sorted(missing))}")
            else:
                counts["instances"] = int(conn.execute("SELECT COUNT(*) FROM instance").fetchone()[0])
                counts["timelines"] = int(conn.execute("SELECT COUNT(*) FROM timeline").fetchone()[0])
                row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                version = str(row[0]) if row else ""
                if version and version != str(SCHEMA_VERSION):
                    problems.append(f"数据格式版本不符（旧 {version}，本端 {SCHEMA_VERSION}）：请先升级那份数据")
        except sqlite3.Error as exc:
            problems.append(f"读不了旧目录的数据库：{exc}")
        finally:
            if conn is not None:
                conn.close()

    lock = _read_lock(root)
    running = False
    if lock:
        pid = int(lock.get("pid") or 0)
        if pid:
            from .app import pid_alive  # 局部导入：worker 与 app 之间不成环

            running = pid_alive(pid)
        if running:
            problems.append("旧目录里的程序似乎还在运行：先把它完全退出（含托盘）再来迁移")

    assets = 0
    for folder in ("packages", "exports"):
        base = root / folder
        if base.is_dir():
            assets += sum(1 for path in base.rglob("*") if path.is_file())
    return {
        "path": str(root),
        "ok": not problems,
        "problems": problems,
        "counts": counts,
        "running": running,
        "assets": assets,
        "size_bytes": _tree_size(root),
    }


def _tree_size(root: Path) -> int:
    """旧根里真正会搬走的那几部分的大小（不含日志与缓存）。"""
    total = 0
    for folder in ("data", "packages", "exports", "config"):
        base = root / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and not backup_pack._skip(path):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    return total


def target_state(cfg: Any, store: Store) -> dict[str, Any]:
    """目标根现在的样子：有正式数据就不许迁移（只能导入一个世界或恢复全部数据）。"""
    instances = store.instance_list()
    assets = 0
    for folder in (cfg.paths.packages, cfg.paths.exports):
        base = Path(folder)
        if base.is_dir():
            assets += sum(1 for path in base.rglob("*") if path.is_file())
    return {
        "instances": len(instances),
        "names": [str(item.get("name") or "") for item in instances],
        "assets": assets,
        "blocked": bool(instances) or assets > 0,
    }


def _merge_prefs(cfg: Any, text: str) -> list[str]:
    """旧根的「非敏感偏好」并进来：目标已有的值保持不变，缺的用旧值补（密钥不搬）。

    迁移搬的是数据与偏好，不是连接配置：目标上的密钥、通道凭据保持原样。
    """
    target = Path(cfg.paths.config_file)
    if not text.strip():
        return []
    try:
        import yaml

        old = yaml.safe_load(text) or {}
        current = yaml.safe_load(target.read_text(encoding="utf-8")) if target.is_file() else {}
        current = current if isinstance(current, dict) else {}
    except Exception as exc:  # noqa: BLE001 —— 配置合并失败不该让已经成功的迁移算失败
        log.warning("prefs merge skipped: %s", exc)
        return []
    filled: list[str] = []
    for key, value in (old or {}).items():
        if key not in current:
            current[key] = value
            filled.append(str(key))
        elif isinstance(current[key], dict) and isinstance(value, dict):
            for sub, sub_value in value.items():
                if sub not in current[key]:
                    current[key][sub] = sub_value
                    filled.append(f"{key}.{sub}")
    if not filled:
        return []
    try:
        import yaml

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("prefs merge write failed: %s", exc)
        return []
    return filled


def run(cfg: Any, store: Store, source: str | Path, *, note: str = "从旧开发目录迁移") -> dict[str, Any]:
    """检查 → 打包旧根 → 暂存校验 → 切换（先留恢复前副本，失败回退）。"""
    report = inspect(cfg, source)
    if not report["ok"]:
        return {"ok": False, "state": "refused", "problems": report["problems"], "inspect": report}
    target = target_state(cfg, store)
    if target["blocked"]:
        return {
            "ok": False,
            "state": "target_not_empty",
            "problems": [
                f"当前数据根已经有用户资产（{target['instances']} 个世界 / {target['assets']} 个素材文件）："
                "迁移只能搬进空的数据根；要用旧目录里的某个世界请走「导入一个世界」，要整体换成旧数据请走「恢复全部数据」"
            ],
            "inspect": report,
            "target": target,
        }
    folder = backup_pack.packs_dir(cfg)
    try:
        pack = backup_pack.write_pack_from(Path(report["path"]), folder, kind="migrate", note=note)
    except backup_pack.PackError as exc:
        return {"ok": False, "state": "pack_failed", "problems": [str(exc)], "inspect": report}
    staged = backup_pack.stage_pack(cfg, Path(pack["path"]))
    if not staged["ok"]:
        return {
            "ok": False,
            "state": "staging_failed",
            "problems": staged["problems"],
            "pack": pack,
            "inspect": report,
        }
    # 暂存目录在切换成功后会被清掉：先把旧根的偏好读到手，切换完再合并
    staged_dir = Path(str(staged["staged"]))
    prefs_file = staged_dir / "config" / "config.yaml"
    prefs_text = prefs_file.read_text(encoding="utf-8") if prefs_file.is_file() else ""
    applied = backup_pack.apply_staged(cfg, store, staged_dir, note=note)
    if not applied.get("ok"):
        return {
            "ok": False,
            "state": applied.get("state") or "switch_failed",
            "problems": applied.get("problems") or ["切换失败"],
            "pack": pack,
            "inspect": report,
        }
    prefs = _merge_prefs(cfg, prefs_text)
    log.info("migrated from %s: %s", report["path"], pack["name"])
    return {
        "ok": True,
        "state": "done",
        "pack": pack,
        "before": applied.get("before", ""),
        "counts": pack.get("counts") or {},
        "timelines": applied.get("timelines", 0),
        # 说清什么没搬、什么留下了：源目录保留，凭据与运行句柄不迁移
        "kept_source": report["path"],
        "prefs_merged": prefs,
        "excluded": ["API 密钥与通道凭据", "进程锁与运行句柄", "日志与缓存", "可执行插件"],
        "note": "全部时间线处于暂停：要接着跑就在世界里逐条启动",
    }
