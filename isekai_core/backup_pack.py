"""单文件全量备份与恢复（ONBOARDING_AND_RECOVERY §9.1 / §9.2）。

用户的搬运单位是**一份 ZIP**：一致读的数据库快照 + 受管素材 + 非敏感偏好 + 完整性清单。
旧格式（数据库 + 配对素材 zip）不由这里产出，只在需要时按「导入旧备份」识别。

一致性怎么来的：
  * 数据库走 SQLite 在线备份 API（`Store.backup_create`），核心在跑也拿到一致快照；
  * 打包期间用 `quiet()` 挂起世界推进（`app._clock_tick` 看 `PAUSE` 这个旗标），
    素材与库因此来自同一时点；
  * 残留窗口：打包的几秒内用户正好经管理面写东西，仍按受管单写者模型处理——
    库快照始终一致，素材复制是「同一进程内、几乎同时」，不做额外锁。

切换（§9.2）不是解包覆盖：先暂存并逐件校验，再留恢复前完整副本，然后换单元；
换完打开失败就回退到恢复前单元并留记录。启动时 `resume_pending` 处理中断的切换。
坏件不落进受管目录：暂存区在备份目录下，校验不过不碰现行数据。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Iterator

from .log import get_logger
from .store import Store

log = get_logger("backup_pack")

FORMAT = "isekai-pack/1"
MANIFEST = "manifest.json"
DB_PART = "db/isekai.db"
CONFIG_PART = "config/config.yaml"
STAGING_DIR = ".staging"
RECORD = "restore.json"
STATE_FILE = ".verify-cache.json"
EXPAND_LIMIT = 8 * 1024**3  # 展开总量上限：坏件不能把磁盘撑爆
SENSITIVE = ("key", "token", "secret", "password", "credential", "apikey")

# 世界推进的挂起旗标：app 的时钟 tick 每轮看一次（打包/切换期间世界不动）
PAUSE = threading.Event()
_depth = 0
_depth_lock = threading.Lock()


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """挂起世界推进（可重入：打包与切换各自套一层也不会互相提前放行）。"""
    global _depth
    with _depth_lock:
        _depth += 1
        PAUSE.set()
    try:
        yield
    finally:
        with _depth_lock:
            _depth -= 1
            if _depth <= 0:
                PAUSE.clear()


class PackError(RuntimeError):
    """打包/恢复的失败，带人话原因（供界面直接显示）。"""


# ------------------------------------------------------------------ 位置

def packs_dir(cfg: Any) -> Path:
    raw = str(getattr(getattr(cfg, "backup", None), "dir", "") or "backups")
    path = Path(raw)
    return path if path.is_absolute() else Path(cfg.paths.root) / path


def _staging_root(cfg: Any) -> Path:
    return packs_dir(cfg) / STAGING_DIR


def _asset_roots(cfg: Any) -> list[tuple[str, Path]]:
    """受管素材：创作目录与导出件。日志/缓存/备份目录本身不进包。"""
    out: list[tuple[str, Path]] = []
    for name, raw in (("packages", getattr(cfg.paths, "packages", "")), ("exports", getattr(cfg.paths, "exports", ""))):
        if raw:
            out.append((name, Path(raw)))
    return out


def _skip(path: Path) -> bool:
    name = path.name
    return name.endswith((".part", ".tmp", ".lock")) or name.startswith(".")


def _asset_files(cfg: Any) -> list[tuple[Path, str]]:
    items: list[tuple[Path, str]] = []
    for name, root in _asset_roots(cfg):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and not _skip(path):
                items.append((path, f"assets/{name}/{path.relative_to(root).as_posix()}"))
    return items


# ------------------------------------------------------------------ 打包

def _sha256_chunks(*chunks: bytes) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def _add_bytes(zf: zipfile.ZipFile, rel: str, data: bytes, parts: list[dict[str, Any]]) -> None:
    zf.writestr(rel, data)
    parts.append({"path": rel, "bytes": len(data), "sha256": _sha256_chunks(data)})


def _add_file(zf: zipfile.ZipFile, src: Path, rel: str, parts: list[dict[str, Any]]) -> None:
    digest = hashlib.sha256()
    size = 0
    with open(src, "rb") as handle, zf.open(rel, "w") as out:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    parts.append({"path": rel, "bytes": size, "sha256": digest.hexdigest()})


def _clean_config(cfg: Any) -> str:
    """非敏感偏好：密钥 / 令牌整条剔除（凭据不随备份传播）。"""
    path = Path(cfg.paths.config_file)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    try:
        import yaml

        data = yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001 —— 配置坏了不该让整份备份失败
        return text

    def prune(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                str(k): prune(v)
                for k, v in node.items()
                if not any(word in str(k).lower() for word in SENSITIVE)
            }
        if isinstance(node, list):
            return [prune(item) for item in node]
        return node

    try:
        import yaml

        return yaml.safe_dump(prune(data), allow_unicode=True, sort_keys=False)
    except Exception:  # noqa: BLE001
        return text


def _snapshot_counts(db_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for label, table in (("instances", "instance"), ("timelines", "timeline"), ("threads", "thread")):
            try:
                counts[label] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return counts


def write_pack(cfg: Any, store: Store, *, kind: str = "manual", note: str = "") -> dict[str, Any]:
    """落一份单文件全量备份。失败抛 PackError，且不留下半份文件。"""
    folder = packs_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    name = f"isekai-{kind}-{stamp}.zip"
    if (folder / name).exists():
        name = f"isekai-{kind}-{stamp}-{os.getpid()}.zip"
    target = folder / name
    tmp = folder / f".{name}.part"
    parts: list[dict[str, Any]] = []
    with quiet():
        snap = folder / f".{name}.db"
        try:
            snapshot = store.backup_create(snap, note=note or kind)
            if not snapshot.get("ok"):
                raise PackError("数据库快照没通过完整性检查，这一份没有生成")
            counts = _snapshot_counts(snap)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                _add_file(zf, snap, DB_PART, parts)
                for src, rel in _asset_files(cfg):
                    _add_file(zf, src, rel, parts)
                _add_bytes(zf, CONFIG_PART, _clean_config(cfg).encode("utf-8"), parts)
                manifest = {
                    "format": FORMAT,
                    "kind": kind,
                    "note": note,
                    "created_at": time.time(),
                    "app_version": getattr(cfg, "app_version", "0.1.0"),
                    "schema_version": 1,
                    "counts": counts,
                    "parts": parts,
                    "excluded": ["API 密钥与通道凭据", "日志与缓存", "可执行插件", "绝对设备路径"],
                }
                zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
            tmp.unlink(missing_ok=True)
            raise PackError(f"打包失败：{exc}") from exc
        finally:
            snap.unlink(missing_ok=True)
    os.replace(tmp, target)  # 原子发布：没写完不留半份
    return {
        "name": target.name,
        "path": str(target),
        "bytes": target.stat().st_size,
        "created_at": time.time(),
        "parts": len(parts),
        "counts": counts,
        "kind": kind,
        "note": note,
        "complete": True,
    }


# ------------------------------------------------------------------ 检查

def _path_problem(rel: str) -> str:
    if rel.startswith(("/", "\\")) or ":" in rel:
        return f"路径不是相对路径：{rel}"
    if ".." in Path(rel).parts:
        return f"路径越界：{rel}"
    return ""


def read_pack(path: Path) -> tuple[dict[str, Any] | None, str]:
    """只读清单，不展开。返回 (manifest, 问题)；旧格式会给出可识别的说法。"""
    if not path.is_file():
        return None, "文件不存在"
    try:
        with zipfile.ZipFile(path) as zf:
            if MANIFEST not in zf.namelist():
                return None, "不像本项目的单文件备份（没有完整性清单）"
            manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"读不了这份文件：{exc}"
    if not str(manifest.get("format", "")).startswith("isekai-pack/"):
        return None, f"备份格式不认识：{manifest.get('format')}"
    return manifest, ""


def verify_pack(path: Path, *, deep: bool = True) -> dict[str, Any]:
    """完整性校验：清单、路径边界、逐件大小与摘要。不碰现行数据。"""
    problems: list[str] = []
    manifest, problem = read_pack(path)
    if manifest is None:
        return {"complete": False, "problems": [problem], "manifest": None, "bytes": path.stat().st_size if path.exists() else 0}
    if int(manifest.get("schema_version", 0)) > 1:
        problems.append(f"备份来自更新的格式（{manifest.get('schema_version')}），本端读不了")
    parts = manifest.get("parts") or []
    if not parts:
        problems.append("清单里没有任何内容")
    total = 0
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        for item in parts:
            rel = str(item.get("path", ""))
            total += int(item.get("bytes", 0))
            bad = _path_problem(rel)
            if bad:
                problems.append(bad)
                continue
            if rel not in names:
                problems.append(f"缺件：{rel}")
                continue
            if not deep:
                continue
            digest = hashlib.sha256()
            size = 0
            with zf.open(rel) as handle:
                while True:
                    chunk = handle.read(1 << 20)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
            if size != int(item.get("bytes", 0)):
                problems.append(f"大小不符：{rel}")
            elif digest.hexdigest() != str(item.get("sha256", "")):
                problems.append(f"摘要不符：{rel}")
        if total > EXPAND_LIMIT:
            problems.append("展开总量超出上限，拒绝恢复")
    return {
        "complete": not problems,
        "problems": problems,
        "manifest": manifest,
        "bytes": path.stat().st_size,
        "expanded_bytes": total,
        "counts": manifest.get("counts") or {},
    }


def _cache_path(cfg: Any) -> Path:
    return packs_dir(cfg) / STATE_FILE


def _load_cache(cfg: Any) -> dict[str, Any]:
    path = _cache_path(cfg)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cfg: Any, data: dict[str, Any]) -> None:
    path = _cache_path(cfg)
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:  # 缓存写不了不影响功能，只是下次要重算
        log.warning("verify cache write failed: %s", exc)


def list_packs(cfg: Any) -> list[dict[str, Any]]:
    """备份列表：时间、大小、完整 / 不完整 / 不兼容 + 最近校验结果（按 mtime+size 复用）。"""
    folder = packs_dir(cfg)
    if not folder.is_dir():
        return []
    cache = _load_cache(cfg)
    out: list[dict[str, Any]] = []
    changed = False
    for path in sorted(folder.glob("*.zip")):
        stat = path.stat()
        stamp = f"{int(stat.st_mtime)}:{stat.st_size}"
        known = cache.get(path.name)
        if not known or known.get("stamp") != stamp:
            report = verify_pack(path)
            known = {
                "stamp": stamp,
                "status": "ok" if report["complete"] else ("incompatible" if report["manifest"] and report["problems"] and "格式" in report["problems"][0] else "broken"),
                "checked_at": time.time(),
                "problems": report["problems"][:3],
                "manifest": report["manifest"],
                "expanded_bytes": report.get("expanded_bytes", 0),
            }
            cache[path.name] = known
            changed = True
        manifest = known.get("manifest") or {}
        out.append(
            {
                "name": path.name,
                "path": str(path),
                "bytes": stat.st_size,
                "expanded_bytes": known.get("expanded_bytes", 0),
                "created_at": float(manifest.get("created_at") or stat.st_mtime),
                "kind": str(manifest.get("kind") or ""),
                "note": str(manifest.get("note") or ""),
                "counts": manifest.get("counts") or {},
                "status": known.get("status", "broken"),
                "problems": known.get("problems") or [],
                "checked_at": known.get("checked_at", 0.0),
            }
        )
    if changed:
        _save_cache(cfg, cache)
    return out


def rotate(cfg: Any, *, keep: int) -> list[str]:
    """按保留数轮换**自动**备份；手动与恢复前副本由用户自己删。"""
    folder = packs_dir(cfg)
    autos = sorted((item for item in folder.glob("isekai-auto-*.zip")), key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for path in autos[max(0, int(keep)) :]:
        path.unlink(missing_ok=True)
        removed.append(path.name)
    return removed


# ------------------------------------------------------------------ 恢复

def stage_pack(cfg: Any, path: Path) -> dict[str, Any]:
    """预检 + 暂存：解到备份目录下的暂存区，逐件再校验一遍。现行数据不动。"""
    report = verify_pack(path)
    if not report["complete"]:
        return {"ok": False, "problems": report["problems"], "staged": "", "manifest": report["manifest"]}
    root = _staging_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    staged = root / f"{path.stem}-{os.getpid()}"
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    problems: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            for item in (report["manifest"] or {}).get("parts") or []:
                rel = str(item.get("path", ""))
                if _path_problem(rel):
                    problems.append(f"路径越界：{rel}")
                    continue
                out = staged / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(rel) as src, open(out, "wb") as dest:
                    shutil.copyfileobj(src, dest, 1 << 20)
                digest = hashlib.sha256(out.read_bytes()).hexdigest()
                if digest != str(item.get("sha256", "")):
                    problems.append(f"暂存件摘要不符：{rel}")
        db = staged / DB_PART
        if not db.is_file() or problems:
            if not db.is_file():
                problems.append("暂存里没有数据库")
        else:
            ok, reason = _store_check(db)
            if not ok:
                problems.append(f"暂存数据库不可用：{reason}")
    except (OSError, zipfile.BadZipFile) as exc:
        problems.append(f"暂存失败：{exc}")
    if problems:
        shutil.rmtree(staged, ignore_errors=True)
        return {"ok": False, "problems": problems, "staged": "", "manifest": report["manifest"]}
    manifest = report["manifest"] or {}
    # 清单也放一份进暂存：切换后要对条数、界面要展示范围，都不必再开一次包
    (staged / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return {
        "ok": True,
        "problems": [],
        "staged": str(staged),
        "manifest": manifest,
        "counts": manifest.get("counts") or {},
        "expanded_bytes": report.get("expanded_bytes", 0),
    }


def _store_check(db: Path) -> tuple[bool, str]:
    """借 Store 的暂存库校验（能开、完整、关键表在、模式版本相符）。"""
    probe = Store(str(db))
    try:
        return probe.backup_check(db)
    finally:
        with contextlib.suppress(Exception):
            probe.close()


def _write_record(cfg: Any, data: dict[str, Any]) -> None:
    path = packs_dir(cfg) / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_record(cfg: Any) -> dict[str, Any]:
    path = packs_dir(cfg) / RECORD
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _swap_assets(cfg: Any, staged: Path, *, suffix: str) -> tuple[list[tuple[Path, Path]], list[str]]:
    """把暂存素材换成现行素材：先整目录挪开（可回退），再从暂存放回来。"""
    kept: list[tuple[Path, Path]] = []
    problems: list[str] = []
    for name, root in _asset_roots(cfg):
        src = staged / "assets" / name
        old = root.with_name(root.name + f".{suffix}")
        if root.exists():
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
            try:
                root.rename(old)
            except OSError as exc:
                problems.append(f"挪开原素材失败（{name}）：{exc}")
                break
            kept.append((old, root))
        if src.is_dir():
            try:
                shutil.copytree(src, root)
            except OSError as exc:
                problems.append(f"放回素材失败（{name}）：{exc}")
                break
    return kept, problems


def apply_staged(cfg: Any, store: Store, staged: str, *, note: str = "") -> dict[str, Any]:
    """切换：恢复前完整副本 → 换数据库与素材 → 打开校验 → 失败回退。

    开始切换后没有「取消」：只有成功、或回退到恢复前单元。
    """
    staged_path = Path(staged or "")
    if not staged_path.is_dir():
        return {"ok": False, "state": "no_staging", "problems": ["暂存内容不在了，请重新预检"], "timelines": 0}
    db = staged_path / DB_PART
    if not db.is_file():
        return {"ok": False, "state": "no_staging", "problems": ["暂存里没有数据库"], "timelines": 0}

    with quiet():
        # 1) 恢复前完整副本：拿不到就不开始覆盖
        try:
            before = write_pack(cfg, store, kind="pre-restore", note=note or "恢复前自动留底")
        except PackError as exc:
            return {"ok": False, "state": "blocked", "problems": [f"恢复前副本失败，未开始覆盖：{exc}"], "timelines": 0}
        record = {"state": "switching", "at": time.time(), "before": before["path"], "note": note}
        _write_record(cfg, record)

        # 2) 换数据库（Store 侧自带安全副本、全线冻结、令牌失效）。素材先挪开再放回。
        kept, problems = _swap_assets(cfg, staged_path, suffix=f"restore-old-{int(time.time())}")
        timelines = 0
        if not problems:
            try:
                result = store.backup_restore(db, safety=packs_dir(cfg) / "isekai-restore-safety.db")
                timelines = int(result.get("timelines", 0))
            except (ValueError, sqlite3.Error, OSError) as exc:
                problems.append(f"切换数据库失败：{exc}")
        # 3) 打开校验：能读、关键表在、条数与被恢复的那一份对得上
        if not problems:
            try:
                store.ensure_schema()
                counts = _snapshot_counts(Path(store.path))
                want = json.loads((staged_path / MANIFEST).read_text(encoding="utf-8")).get("counts") or {}
                if want.get("timelines") and counts.get("timelines") != want["timelines"]:
                    problems.append(f"切换后条数对不上：时间线 {counts.get('timelines')} ≠ 该备份 {want['timelines']}")
            except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
                problems.append(f"切换后打不开：{exc}")

        if problems:
            # 回退：先把素材挪回去，再用恢复前副本换回数据库
            for old, live in kept:
                shutil.rmtree(live, ignore_errors=True)
                if old.exists():
                    try:
                        old.rename(live)
                    except OSError as exc:
                        problems.append(f"回退素材失败：{exc}")
            rolled = True
            try:
                store.backup_restore(_before_db(cfg, before), safety=None)
            except Exception as exc:  # noqa: BLE001 —— 回退本身失败要如实报
                rolled = False
                problems.append(f"回退数据库失败：{exc}")
            _write_record(cfg, {**record, "state": "rolled_back" if rolled else "needs_attention", "problems": problems, "at": time.time()})
            return {
                "ok": False,
                "state": "rolled_back" if rolled else "needs_attention",
                "problems": problems,
                "before": before["path"],
                "timelines": 0,
            }

    for old, _live in kept:
        shutil.rmtree(old, ignore_errors=True)
    _write_record(cfg, {**record, "state": "done", "problems": [], "timelines": timelines, "at": time.time()})
    shutil.rmtree(staged_path, ignore_errors=True)
    return {"ok": True, "state": "done", "problems": [], "before": before["path"], "timelines": timelines}


def _before_db(cfg: Any, before: dict[str, Any]) -> Path:
    """恢复前副本里的数据库件：从单文件里取出来（回退用）。"""
    target = packs_dir(cfg) / ".rollback.db"
    with zipfile.ZipFile(Path(before["path"])) as zf:
        with zf.open(DB_PART) as src, open(target, "wb") as dest:
            shutil.copyfileobj(src, dest, 1 << 20)
    return target


def resume_pending(cfg: Any, store: Store) -> dict[str, Any]:
    """启动时处理中断的切换（§9.2 表格最后一行）：判定或回退，不让半套数据当成正常态。"""
    record = read_record(cfg)
    state = str(record.get("state") or "")
    if state != "switching":
        return {"state": state or "none", "action": ""}
    before = Path(str(record.get("before") or ""))
    if not before.is_file():
        res = {"state": "needs_attention", "action": "恢复记录指向的恢复前副本不在了，请人工确认数据状态"}
        _write_record(cfg, {**record, "state": "needs_attention", "problems": [res["action"]], "at": time.time()})
        return res
    try:
        store.backup_restore(_before_db(cfg, {"path": str(before)}), safety=None)
        action = "上次切换中断，已回退到恢复前单元"
        res = {"state": "rolled_back", "action": action, "before": str(before)}
    except Exception as exc:  # noqa: BLE001
        action = f"上次切换中断且回退失败：{exc}"
        res = {"state": "needs_attention", "action": action}
    _write_record(cfg, {**record, "state": res["state"], "problems": [action], "at": time.time()})
    log.warning("resume_pending: %s", action)
    return res
