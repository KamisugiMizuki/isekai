"""世界设定层的管理面操作：世界包 / 角色卡 / 实例 / 导入导出。

桌面端与安卓端共用同一套操作（§2.5）：两端只做表单与展示，schema、校验、
生成与修订流程都在这里。所有校验失败以 `invalid_input` 错误返回，附带逐条原因。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import Config
from ..llm import LLMError
from ..log import get_logger
from ..store import Store
from ..ump import Err, UmpError
from .cards import template_card, validate_assembly, validate_card
from .generator import fill_section, generate_card, generate_package, revise_package
from .instances import (
    InstanceError,
    create_instance,
    delete_instance,
    list_instances,
    load_cards,
    public_info,
    rename_instance,
    save_card,
    public_setting,
)
from .package import MAX_PACKAGE_BYTES, PackageError, load_package, save_package, template_package
from .portable import import_instance, read_container, write_export
from .validate import validate_package

SYNC_OPS = frozenset(
    {
        "world.package.template",
        "world.package.load",
        "world.package.save",
        "world.package.validate",
        "world.package.list",
        "world.draft.list",
        "world.draft.save",
        "world.draft.load",
        "world.draft.discard",
        "runtime.clock",
        "runtime.budget",
        "runtime.budget.set",
        "disclose.confirm",
        "disclose.list",
        "event.confirm",
        "runtime.timeline.rename",
        "runtime.timeline.archive",
        "runtime.timeline.delete",
        "runtime.commit",
        "runtime.commits",
        "runtime.fork",
        "runtime.rollback",
        "runtime.activate",
        "runtime.freeze",
        "runtime.rate",
        "runtime.advance",
        "runtime.card.add",
        "runtime.backfill",
        "world.card.template",
        "world.card.load",
        "world.card.save",
        "world.card.validate",
        "world.card.confirm",
        "world.card.list",
        "instance.list",
        "instance.create",
        "instance.info",
        "instance.rename",
        "instance.delete",
        "instance.export",
        "instance.import",
        "instance.setting",
        "app.shutdown",
        "backup.create",
        "claim.coverage",
        "world.backfill.plan",
        "instance.convert",
        "reaction.list",
        "reaction.note",
        "backup.restore",
        "backup.list",
        "proactive.list",
    }
)
ASYNC_OPS = frozenset(
    {
        "event.render",
        "event.expand",
        "runtime.proactive",
        "runtime.first_contact",
        "runtime.propose",
        "runtime.extract",
        "event.draft",
        "world.package.generate",
        "world.package.revise",
        "world.package.fill",
        "world.card.generate",
    }
)


def resolve_path(cfg: Config, value: Any) -> Path:
    """路径解析：绝对路径原样；相对路径先按根目录解释（`packages/x.json` 这种写法直接可用），
    否则落在创作目录 `<根>/packages/` 下。"""
    text = str(value or "").strip()
    if not text:
        raise UmpError(Err.INVALID, "缺少路径", retryable=False)
    path = Path(text)
    if path.is_absolute():
        return path
    from_root = cfg.paths.root / path
    if from_root.exists() or path.parts[0] in {"packages", "exports"}:
        return from_root
    return cfg.paths.packages / path


def _package_arg(args: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """世界包来源：内联 `package` 优先，其次 `package_path`（角色卡操作）或 `path`（世界包操作）。"""
    inline = args.get("package")
    if isinstance(inline, dict):
        return inline
    source = args.get("package_path") or args.get("path")
    return load_package(resolve_path(cfg, source))


def _read_user_json(path: Path, *, what: str) -> Any:
    """用户给的 JSON 文件（角色卡 / 草稿 / 导入件）：读之前先按字节限额拦（§2.3 加载限额）。"""
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise UmpError(Err.NOT_FOUND, f"{what}不存在：{path}", retryable=False) from exc
    except OSError as exc:
        raise UmpError(Err.INTERNAL, f"{what}不可读：{path}（{exc}）", retryable=False) from exc
    if size > MAX_PACKAGE_BYTES:
        raise UmpError(
            Err.INVALID, f"{what}超过加载限额：{size} 字节 > {MAX_PACKAGE_BYTES} 字节", retryable=False
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UmpError(Err.INVALID, f"{what}不是合法 JSON：{exc}", retryable=False) from exc


def _card_arg(args: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """角色卡来源：内联 `card` 优先，否则读 `card_path`。"""
    inline = args.get("card")
    if isinstance(inline, dict):
        return inline
    path = resolve_path(cfg, args.get("card_path"))
    raw = _read_user_json(path, what="角色卡文件")
    if not isinstance(raw, dict):
        raise UmpError(Err.INVALID, "角色卡顶层必须是对象", retryable=False)
    return raw


def _moment(package: dict[str, Any], args: dict[str, Any]) -> int:
    if isinstance(args.get("moment"), int):
        return int(args["moment"])
    calendar = package.get("calendar") if isinstance(package.get("calendar"), dict) else {}
    return int(calendar.get("initial_moment") or 0)


def _draft_path(cfg: Config, name: str) -> Path:
    """草稿落盘位置：`<创作目录>/<名字>.draft.json`；名字做最小脱敏，避免路径穿越。"""
    safe = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fa5.-]", "_", str(name).strip()) or "draft"
    return cfg.paths.packages / f"{safe}.draft.json"


def _list_files(cfg: Config, kind: str) -> list[dict[str, Any]]:
    """创作目录列表：给管理面下拉用（只返回摘要与校验状态，不返回全文）。"""
    folder = cfg.paths.packages
    if not folder.exists():
        return []
    items: list[dict[str, Any]] = []
    if kind == "container":
        # 实例导出件：导入下拉的候选（不算世界包创作内容）
        for path in sorted(folder.glob("*.isekai.json")):
            items.append({"file": path.name, "size": path.stat().st_size})
        return items
    if kind == "draft":
        # 草稿：允许未通过校验的候选，可显式继续或丢弃（§2.4）
        for path in sorted(folder.glob("*.draft.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            items.append(
                {
                    "file": path.name,
                    "name": payload.get("name"),
                    "kind": payload.get("kind"),
                    "updated_at": payload.get("updated_at"),
                }
            )
        return items
    for path in sorted(folder.glob("*.json")):
        if path.name.endswith((".candidate.json", ".draft.json", ".isekai.json")):
            continue  # 未通过校验的候选、草稿与实例导出件都不算创作内容
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            if kind == "package":
                items.append({"file": path.name, "name": None, "valid": False, "errors": ["不是合法 JSON"]})
            continue
        is_card = isinstance(payload.get("identity"), dict) and "calendar" not in payload
        if kind == "package" and not is_card:
            errors = validate_package(payload)
            items.append(
                {
                    "file": path.name,
                    "name": (payload.get("meta") or {}).get("original_name"),
                    "density": (payload.get("meta") or {}).get("density"),
                    "valid": not errors,
                    "errors": errors,
                }
            )
        elif kind == "card" and is_card:
            items.append(
                {
                    "file": path.name,
                    "name": (payload.get("identity") or {}).get("name"),
                    "confirmed": bool((payload.get("meta") or {}).get("confirmed")),
                    "race_id": (payload.get("identity") or {}).get("race_id"),
                }
            )
    return items


def _runtime_op(
    runtime: Any, op: str, args: dict[str, Any], *, cfg: Config, now_real: float | None = None
) -> dict[str, Any]:
    """运行层操作：时钟视图 / 激活 / 冻结 / 倍率 / 推进（§2、§3）。"""
    if runtime is None:
        raise UmpError(Err.STATE_BLOCKED, "运行层不可用", retryable=False)
    from ..runtime.service import RuntimeStateError

    now = time.time() if now_real is None else now_real
    instance_id = str(args.get("instance_id") or args.get("instance") or "")
    timeline_id = str(args.get("timeline_id") or args.get("timeline") or "")
    if not instance_id or not timeline_id:
        raise UmpError(Err.INVALID, "需要 instance_id 与 timeline_id", retryable=False)
    try:
        if op == "runtime.clock":
            return {"clock": runtime.view(instance_id, timeline_id, now_real=now)}
        if op == "runtime.activate":
            # 用 activate 返回的视图（含是否确认了倍率），别再造一个把确认信息盖掉
            view = runtime.activate(
                instance_id, timeline_id, now_real=now, rate=args.get("rate") if args.get("rate") else None
            )
            advanced = runtime.advance(instance_id, timeline_id, now_real=now)
            return {"clock": view, "advance": advanced}
        if op == "runtime.freeze":
            return {"clock": runtime.freeze(instance_id, timeline_id, now_real=now)}
        if op == "runtime.rate":
            result = runtime.set_rate(instance_id, timeline_id, rate=int(args.get("rate") or 0), now_real=now)
            return {"rate": result, "clock": runtime.view(instance_id, timeline_id, now_real=now)}
        if op == "runtime.backfill":
            return {"backfill": {"rows": runtime.backfill(instance_id, timeline_id)}}
        if op == "runtime.card.add":
            card = args.get("card")
            if not isinstance(card, dict):
                card = _read_user_json(Path(resolve_path(cfg, args.get("card_path"))), what="角色卡文件")
            event = args.get("event")
            return {
                "join": runtime.add_character(
                    instance_id,
                    timeline_id,
                    card,
                    now_real=now,
                    joined_world=int(args["joined_world"]) if args.get("joined_world") is not None else None,
                    note=str(args.get("note") or ""),
                    acquainted=bool(args.get("acquainted")),
                    request_id=str(args.get("request_id") or ""),
                    event=event if isinstance(event, dict) else None,
                )
            }
        if op == "runtime.advance":
            advanced = runtime.advance(
                instance_id,
                timeline_id,
                now_real=now,
                max_batches=int(args["max_batches"]) if args.get("max_batches") else None,
            )
            return {"advance": advanced, "clock": runtime.view(instance_id, timeline_id, now_real=now)}
    except RuntimeStateError as exc:
        get_logger("isekai.world.ops").warning(
            "runtime op rejected op=%s instance_id=%s timeline_id=%s: %s",
            op,
            instance_id,
            timeline_id,
            exc,
        )
        raise UmpError(Err.INVALID, str(exc), retryable=False) from exc
    raise UmpError(Err.UNSUPPORTED_TYPE, f"未知运行层操作 {op}", retryable=False)


def _ensure_runtime(runtime: Any, instance_id: str) -> None:
    """新建 / 导入后补齐运行层状态（时钟 + 角色初始单元与首日计划）。"""
    if runtime is None:
        return
    try:
        runtime.ensure_instance(instance_id, now_real=time.time())
    except Exception:  # 运行层补齐失败不该让创建回滚（下次启动会再补）
        _log_runtime_failure(instance_id)


def _log_runtime_failure(instance_id: str) -> None:
    get_logger("isekai.world.ops").exception("ensure runtime failed instance=%s", instance_id)


def dispatch(cfg: Config, store: Store, op: str, args: dict[str, Any], runtime: Any = None) -> dict[str, Any]:
    """同步操作：只读写文件与库，不调用模型。"""
    try:
        # 预算视图 / 设置：只按实例（不要求时间线），不进 runtime.* 前缀分发
        if op == "disclose.confirm":
            return _disclose_confirm(cfg, store, args)
        if op == "disclose.list":
            return _disclose_list(cfg, store, args)
        if op == "event.confirm":
            return _confirm_user_event(cfg, store, args)
        if op == "runtime.timeline.rename":
            return _timeline_rename(cfg, store, args)
        if op == "runtime.timeline.archive":
            return _timeline_archive(cfg, store, args)
        if op == "runtime.timeline.delete":
            return _timeline_delete(cfg, store, args)
        if op == "runtime.commit":
            return _version_commit(cfg, store, args)
        if op == "runtime.commits":
            return _version_list(cfg, store, args)
        if op == "runtime.fork":
            return _version_fork(cfg, store, args)
        if op == "runtime.rollback":
            return _version_rollback(cfg, store, args)
        if op == "runtime.budget":
            return _budget_view(cfg, store, args)
        if op == "runtime.budget.set":
            return _budget_set(cfg, store, args)

        if op.startswith("runtime."):
            return _runtime_op(runtime, op, args, cfg=cfg)

        if op == "world.package.template":
            return {
                "package": template_package(
                    str(args.get("name") or "未命名世界"),
                    density=str(args.get("density") or "normal"),
                    day_seconds=int(args.get("day_seconds") or 86400),
                )
            }
        if op == "world.package.load":
            return {"package": load_package(resolve_path(cfg, args.get("path")))}
        if op == "world.package.save":
            package = _package_arg(args, cfg)
            errors = validate_package(package)
            if errors and not bool(args.get("force")):
                raise UmpError(Err.INVALID, "世界包未通过校验，未写入：" + "；".join(errors), retryable=False)
            target = resolve_path(cfg, args.get("path"))
            save_package(target, package)
            return {"path": str(target), "errors": errors}
        if op == "world.package.validate":
            return {"errors": validate_package(_package_arg(args, cfg))}
        if op == "world.package.list":
            return {"packages": _list_files(cfg, "package"), "containers": _list_files(cfg, "container")}
        if op == "world.card.list":
            return {"cards": _list_files(cfg, "card")}
        if op == "world.draft.list":
            return {"drafts": _list_files(cfg, "draft")}
        if op == "world.draft.save":
            name = str(args.get("name") or "").strip()
            if not name:
                raise UmpError(Err.INVALID, "草稿需要名称", retryable=False)
            payload = {
                "name": name,
                "kind": str(args.get("kind") or "package"),
                "payload": args.get("payload") or {},
                "progress": args.get("progress") or {},
                "errors": list(args.get("errors") or []),
                "updated_at": time.time(),
            }
            target = _draft_path(cfg, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"file": target.name}
        if op == "world.draft.load":
            target = _draft_path(cfg, str(args.get("name") or ""))
            payload = _read_user_json(target, what="草稿")
            return {"draft": payload, "file": target.name}
        if op == "world.draft.discard":
            target = _draft_path(cfg, str(args.get("name") or ""))
            if target.exists():
                target.unlink()
            return {"ok": True, "file": target.name}
        if op == "world.card.template":
            return {"card": template_card(_package_arg(args, cfg), name=str(args.get("name") or "未命名角色"))}
        if op == "world.card.load":
            return {"card": _card_arg(args, cfg)}
        if op == "world.card.validate":
            package = _package_arg(args, cfg)
            card = _card_arg(args, cfg)
            return {"errors": validate_card(card, package, moment=_moment(package, args))}
        if op == "world.card.confirm":
            package = _package_arg(args, cfg)
            card = _card_arg(args, cfg)
            errors = validate_card(card, package, moment=_moment(package, args))
            if errors:
                raise UmpError(Err.INVALID, "角色卡未通过校验，不能确认：" + "；".join(errors), retryable=False)
            card.setdefault("meta", {})["confirmed"] = True
            if args.get("card_path"):
                save_card(str(resolve_path(cfg, args.get("card_path"))), card)
            return {"card": card}
        if op == "world.card.save":
            card = _card_arg(args, cfg)
            target = resolve_path(cfg, args.get("card_path"))
            save_card(str(target), card)
            return {"path": str(target)}
        if op == "instance.list":
            return {"instances": list_instances(store)}
        if op == "instance.create":
            package = _package_arg(args, cfg)
            cards = args.get("cards")
            if not isinstance(cards, list):
                paths = args.get("card_paths") or []
                if isinstance(paths, str):
                    paths = [paths]
                if not paths:
                    raise UmpError(Err.INVALID, "创建实例需要至少一张角色卡", retryable=False)
                cards = load_cards(package, [str(resolve_path(cfg, item)) for item in paths])
            info = create_instance(
                store, package, cards, display_name=args.get("display_name") or None
            )
            _ensure_runtime(runtime, info["id"])
            return {"instance": info}
        if op == "instance.info":
            row = store.instance_get(str(args.get("id") or ""))
            if row is None:
                raise UmpError(Err.NOT_FOUND, "实例不存在", retryable=False)
            setting = json.loads(row["setting"])
            cards = list(setting.get("cards") or [])
            # 补卡的角色也要列出来（与运行层 cards() 同一口径：按该线当前水位）
            for timeline in store.timeline_list(row["id"]):
                clock = store.clock_get(timeline["id"])
                until = int(clock["processed_world"]) if clock else 0
                for joined in store.character_join_list(row["id"], timeline["id"], until=until):
                    try:
                        cards.append(json.loads(str(joined["card"])))
                    except json.JSONDecodeError:
                        continue
            return {
                "instance": public_info(row),
                "characters": [
                    {
                        "card_id": (card.get("meta") or {}).get("card_id"),
                        "name": (card.get("identity") or {}).get("name"),
                        "occupation": (card.get("identity") or {}).get("occupation"),
                    }
                    for card in cards
                ],
                "timelines": store.timeline_list(row["id"]),
                "commits": store.commit_list(row["id"]),
            }
        if op == "reaction.list":
            rows = store.reaction_list(
                str(args.get("instance_id") or ""),
                str(args.get("timeline_id") or ""),
                character_id=str(args.get("character_id") or "") or None,
            )
            return {"reactions": rows}
        if op == "reaction.note":
            from ..runtime import reaction as reaction_mod

            clock = store.clock_get(str(args.get("timeline_id") or ""))
            row = reaction_mod.from_dialog(
                instance_id=str(args.get("instance_id") or ""),
                timeline_id=str(args.get("timeline_id") or ""),
                character_id=str(args.get("character_id") or ""),
                message_id=str(args.get("message_id") or args.get("source_ref") or ""),
                world_seconds=int(args.get("world_seconds") or (clock["processed_world"] if clock else 0)),
                direction=int(args.get("direction") or 1),
                tendency=str(args.get("tendency") or ""),
                basis=str(args.get("basis") or ""),
            )
            ok = store.apply_runtime_batch(
                timeline_id=str(row["timeline_id"]),
                generation=int(clock["generation"]),
                processed_world=int(clock["processed_world"]),
                catching_up=False,
                reactions=[row],
            )
            return {"noted": bool(ok), "reaction": row["id"]}
        if op == "instance.convert":
            from .instances import convert_instance

            return {
                "convert": convert_instance(
                    store,
                    str(args.get("instance_id") or ""),
                    confirmed=bool(args.get("confirmed")),
                    exports_dir=cfg.paths.exports,
                )
            }
        if op == "world.backfill.plan":
            row = store.instance_get(str(args.get("instance_id") or ""))
            if row is None:
                raise UmpError(Err.NOT_FOUND, "实例不存在", retryable=False)
            from ..runtime import events as events_mod
            from ..runtime.calendar import calendar_from_package

            package = (json.loads(row["setting"]) or {}).get("world_package") or {}
            return {
                "plan": events_mod.backfill_plan(
                    package,
                    seed=str(row["seed"]),
                    rules_version=str(row["rules_version"] or ""),
                    calendar=calendar_from_package(package),
                )
            }
        if op == "claim.coverage":
            instance_id, timeline_id = str(args.get("instance_id") or ""), str(args.get("timeline_id") or "")
            claim_id = str(args.get("claim_id") or "")
            rows = [item for item in store.claim_list(instance_id, timeline_id) if str(item["id"]) == claim_id]
            if not rows:
                raise UmpError(Err.NOT_FOUND, f"没有该记载：{claim_id}", retryable=False)
            coverage = store.claim_coverage_get(instance_id, timeline_id, claim_id) or {
                "claim_id": claim_id,
                "state": "pending",
                "derived_id": "",
                "note": "尚未生成（不是这条记载没写下）",
            }
            return {"coverage": coverage, "claim": {"id": claim_id, "text": rows[0].get("text") or ""}}
        if op == "backup.create":
            return {"backup": backup_once(cfg, store, note=str(args.get("note") or ""))}
        if op == "app.shutdown":
            # 显式退出握手（DESKTOP_SPEC §五）：「先保存再停进程」的核心半边。
            # 保存 = 补做一次退出前备份（一致水位快照）；停进程 = 请求核心自行退出，
            # 由 app.py 的 finally 收尾（会话收尾 / 关服务 / 释放写库锁），不走 taskkill 硬杀。
            saved = backup_once(cfg, store, note="退出前补做")
            _request_exit()
            return {"saved": saved}
        if op == "backup.restore":
            folder = _backup_folder(cfg, store)
            backup_path = resolve_path(cfg, args.get("path"))
            ok, reason = store.backup_check(backup_path)  # 坏件先判清楚，别落成 internal 错误
            if not ok:
                raise UmpError(Err.INVALID, f"备份不可用：{reason}", retryable=False)
            result = store.backup_restore(
                backup_path, safety=folder / "isekai-restore-safety.db"
            )
            restored_packages = _restore_packages(cfg, Path(str(backup_path)), folder)
            if restored_packages:
                result["packages"] = restored_packages
            return {"restore": result}
        if op == "backup.list":
            folder = _backup_folder(cfg, store)
            return {
                "backups": store.backup_list(folder),
                "dir": str(folder),
                "keep": int(getattr(cfg.backup, "keep", 7)),
                "interval_hours": int(getattr(cfg.backup, "interval_hours", 0)),
            }
        if op == "proactive.list":
            return {"log": store.proactive_list(str(args.get("instance_id") or ""),
                                                str(args.get("timeline_id") or ""))}
        if op == "instance.setting":
            return {"setting": public_setting(store, str(args.get("id") or ""))}
        if op == "instance.rename":
            return {"instance": rename_instance(store, str(args.get("id") or ""), str(args.get("name") or ""))}
        if op == "instance.delete":
            delete_instance(store, str(args.get("id") or ""))
            return {"ok": True}
        if op == "instance.export":
            target = resolve_path(cfg, args.get("path"))
            manifest = write_export(store, str(args.get("id") or ""), target)
            return {"path": str(target), "manifest": manifest}
        if op == "instance.import":
            container = read_container(resolve_path(cfg, args.get("path")))
            info = import_instance(store, container, display_name=args.get("display_name") or None)
            _ensure_runtime(runtime, info["id"])
            return {"instance": info}
    except (InstanceError, PackageError) as exc:
        raise UmpError(Err.INVALID, str(exc), retryable=False) from exc
    except OSError as exc:
        raise UmpError(Err.INTERNAL, f"文件操作失败：{type(exc).__name__}", retryable=False) from exc
    raise UmpError(Err.UNSUPPORTED_TYPE, f"未知管理操作 {op}", retryable=False)
def _day_bucket(now_real: float) -> int:
    """现实日窗口（UTC）：账本按它切片，不用世界时间（§2.8 按现实时间窗口记录）。"""
    return int(now_real // 86400)


def _world_service(cfg: Config, store: Store) -> Any:
    """运行层服务：唯一构造入口在 runtime.service.from_config（配置字段自动对齐）。"""
    from ..runtime.service import from_config

    return from_config(cfg, store)


def _version_ids(args: dict[str, Any]) -> tuple[str, str]:
    instance_id = str(args.get("instance_id") or "")
    timeline_id = str(args.get("timeline_id") or "")
    if not instance_id or not timeline_id:
        raise UmpError(Err.INVALID, "缺少实例或时间线", retryable=False)
    return instance_id, timeline_id


def _disclose_confirm(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """用户明确向指定角色披露指定片段（§7.1）：独立确认事务，不提供模糊放行。"""
    instance_id, timeline_id = _version_ids(args)
    refs = args.get("refs")
    if isinstance(refs, str):
        refs = [item.strip() for item in refs.split(",") if item.strip()]
    if not refs:
        raise UmpError(Err.INVALID, "披露需要明确的片段引用（refs）", retryable=False)
    to_character = str(args.get("to_character") or args.get("card") or "")
    if not to_character:
        raise UmpError(Err.INVALID, "披露需要接收角色", retryable=False)
    return _world_service(cfg, store).disclose(
        instance_id, timeline_id,
        from_character=str(args.get("from_character") or ""),
        to_character=to_character, refs=list(refs), note=str(args.get("note") or ""),
    )


def _disclose_list(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """披露清单：只回管理元数据，不提供其他角色记忆或世界实情（DESKTOP_SPEC）。"""
    instance_id, timeline_id = _version_ids(args)
    return {
        "disclosures": _world_service(cfg, store).disclosures(
            instance_id, timeline_id, to_character=str(args.get("to_character") or "") or None
        )
    }


def _confirm_user_event(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """确认草案：原子创建新线并注入（§八 第 6–8 条）。"""
    instance_id = str(args.get("instance_id") or "")
    draft_id = str(args.get("draft_id") or "")
    if not instance_id or not draft_id:
        raise UmpError(Err.INVALID, "缺少实例或草案标识", retryable=False)
    return _world_service(cfg, store).confirm_user_event(
        instance_id, draft_id, name=str(args.get("name") or "")
    )


def _timeline_rename(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """命名 / 描述（§四）。"""
    instance_id, timeline_id = _version_ids(args)
    return {
        "timeline": _world_service(cfg, store).rename_timeline(
            instance_id, timeline_id,
            name=str(args.get("name") or ""),
            description=str(args["description"]) if args.get("description") is not None else None,
        )
    }


def _timeline_archive(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """归档先冻结、不删除数据（§四）。"""
    instance_id, timeline_id = _version_ids(args)
    return {"timeline": _world_service(cfg, store).archive_timeline(instance_id, timeline_id)}


def _timeline_delete(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """删除需确认；停止该线任务、解绑通道，其他线引用的提交保留（§四）。"""
    instance_id, timeline_id = _version_ids(args)
    if not bool(args.get("confirm")):
        raise UmpError(Err.INVALID, "删除时间线不可恢复，需要 --confirm 确认", retryable=False)
    return _world_service(cfg, store).delete_timeline(instance_id, timeline_id)


def _version_commit(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """手动提交：不受自动提交开关限制（§5.1）。"""
    instance_id, timeline_id = _version_ids(args)
    world = _world_service(cfg, store)
    return {"commit": world.commit(instance_id, timeline_id, kind="manual", note=str(args.get("note") or ""))}


def _version_list(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """提交列表只回管理元数据，不带剧情（§5.1）。"""
    instance_id, timeline_id = _version_ids(args)
    return {"commits": _world_service(cfg, store).commits(instance_id, timeline_id)}


def _version_fork(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """从提交分叉：创建不等于激活（§四）。"""
    instance_id, timeline_id = _version_ids(args)
    commit_id = str(args.get("commit_id") or "")
    if not commit_id:
        raise UmpError(Err.INVALID, "缺少 commit_id", retryable=False)
    world = _world_service(cfg, store)
    result = world.fork(
        instance_id, timeline_id, commit_id=commit_id,
        name=str(args.get("name") or ""), activate=bool(args.get("activate")),
    )
    return result


def _version_rollback(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """回滚是破坏性操作：必须显式确认；建议先分叉或导出（§七）。"""
    instance_id, timeline_id = _version_ids(args)
    commit_id = str(args.get("commit_id") or "")
    if not commit_id:
        raise UmpError(Err.INVALID, "缺少 commit_id", retryable=False)
    if not bool(args.get("confirm")):
        raise UmpError(Err.INVALID, "回滚会覆盖该线有效历史，需要 --confirm 确认", retryable=False)
    return _world_service(cfg, store).rollback(instance_id, timeline_id, commit_id=commit_id)


def _budget_view(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """非内容性的预算状态（§2.8）：用量 / 上限 / 暂停的任务，不含正文与密钥。"""
    instance_id = str(args.get("instance_id") or "")
    if not instance_id:
        raise UmpError(Err.INVALID, "缺少实例", retryable=False)
    return _world_service(cfg, store).budget_view(instance_id)


def _budget_set(cfg: Config, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """调整允许的上限或暂停低优先级任务；不能通过调预算改变已固化的世界事实。"""
    instance_id = str(args.get("instance_id") or "")
    if not instance_id:
        raise UmpError(Err.INVALID, "缺少实例", retryable=False)
    fields: dict[str, Any] = {}
    for key in ("instance_tokens_per_day", "timeline_tokens_per_day", "task_tokens_per_day"):
        value = args.get(key)
        if isinstance(value, int):
            fields[key] = value if value > 0 else None  # 0 = 清除覆盖，回到全局配置
    paused = store.budget_policy_get(instance_id).get("paused_tasks") or []
    task = str(args.get("task") or "")
    if task and args.get("pause") is not None:
        paused = sorted(set(paused) - {task}) if not args.get("pause") else sorted(set(paused) | {task})
        fields["paused_tasks"] = paused
    if not fields:
        raise UmpError(Err.INVALID, "没有要改的预算项", retryable=False)
    policy = store.budget_policy_set(
        instance_id, **{k: v for k, v in fields.items() if k != "paused_tasks"}
    )
    if "paused_tasks" in fields:
        policy = store.budget_policy_set(instance_id, paused_tasks=fields["paused_tasks"])
    return {"policy": policy, "view": _world_service(cfg, store).budget_view(instance_id)}


async def _draft_user_event(cfg: Config, llm: Any, store: Store | None, args: dict[str, Any]) -> dict[str, Any]:
    """用户引入事件的草案（§八 第 1–5 条）：只翻译与校验，不施加任何效果。"""
    if store is None:
        raise UmpError(Err.STATE_BLOCKED, "缺少存储上下文", retryable=False)
    instance_id = str(args.get("instance_id") or "")
    timeline_id = str(args.get("timeline_id") or "")
    if not instance_id or not timeline_id:
        raise UmpError(Err.INVALID, "缺少实例或时间线", retryable=False)
    payload = args.get("payload")
    if isinstance(payload, str) and payload.strip():
        payload = json.loads(payload)
    world = _world_service(cfg, store)
    use_llm = None if (isinstance(payload, dict) and payload.get("effects")) else llm
    return await world.draft_user_event(
        instance_id, timeline_id,
        intent=str(args.get("intent") or ""),
        payload=payload if isinstance(payload, dict) else None,
        source_commit=str(args.get("commit_id") or "") or None,
        llm=use_llm,
    )


async def _extract_memories(cfg: Config, llm: Any, store: Store | None, args: dict[str, Any]) -> dict[str, Any]:
    """触发一次记忆提取（写侧；不回传任何记忆内容——界面不提供浏览入口，MEMORY_SPEC §一）。"""
    if store is None:
        raise UmpError(Err.STATE_BLOCKED, "缺少存储上下文", retryable=False)
    instance_id = str(args.get("instance_id") or "")
    timeline_id = str(args.get("timeline_id") or "")
    if not instance_id or not timeline_id:
        raise UmpError(Err.INVALID, "缺少实例或时间线", retryable=False)
    world = _world_service(cfg, store)
    queued = world.queue_world_sources(instance_id, timeline_id)
    result = await world.extract_memories(
        instance_id, timeline_id, llm=llm, now_real=time.time(),
        limit=int(cfg.runtime.memory_extract_per_day),
    )
    # 顺带把缺向量的补齐（有配置才动；失败保留待嵌入状态，§5.2）
    embedded = await world.embed_memories(
        instance_id, timeline_id, now_real=time.time(), limit=32
    )
    return {**result, "queued": queued, "embedded": embedded}


async def _propose_intents(cfg: Config, llm: Any, store: Store | None, args: dict[str, Any]) -> dict[str, Any]:
    """手动触发一次角色自主提案（给 CLI / 调试用；核心 tick 自己也会跑）。"""
    if store is None:
        raise UmpError(Err.STATE_BLOCKED, "缺少存储上下文", retryable=False)
    instance_id = str(args.get("instance_id") or "")
    timeline_id = str(args.get("timeline_id") or "")
    if not instance_id or not timeline_id:
        raise UmpError(Err.INVALID, "缺少实例或时间线", retryable=False)
    from ..runtime.service import RuntimeService

    world = _world_service(cfg, store)
    return await world.propose_intents(instance_id, timeline_id, llm=llm, now_real=time.time())


async def _grounded_else_numeric(llm: Any, base: str, candidate: str) -> bool:
    """骨架里没有可核对的数字时，再花一次便宜调用问「有没有添骨架外的事实」（§3.2 / §3.4）。

    判不出来（超时 / 解析失败 / 模型抽风）按**通过**处理：数字护栏仍然生效，
    不能因为一次判断失败就把正常表述全退回模板（退回模板同样是损失）。
    """
    from ..runtime import render as render_mod

    if render_mod.has_checkable_facts(base):
        return True
    try:
        text = await llm.chat(
            render_mod.grounding_prompt(str(base), str(candidate)), temperature=0.0, timeout=20.0
        )
    except Exception:
        return True
    verdict = render_mod.parse_grounding(text)
    return True if verdict is None else verdict


async def _render_event(cfg: Config, llm: Any, store: Store | None, args: dict[str, Any]) -> dict[str, Any]:
    """把既定骨架表述成人话（§3.2）：校验不过有界重试，仍不过就退回模板，不改任何事实。"""
    from ..runtime import render as render_mod

    if store is None:
        raise UmpError(Err.STATE_BLOCKED, "缺少存储上下文", retryable=False)
    instance_id, timeline_id = str(args.get("instance_id") or ""), str(args.get("timeline_id") or "")
    event_id = str(args.get("event_id") or "")
    event = store.event_get(instance_id, timeline_id, event_id)
    if event is None:
        raise UmpError(Err.INVALID, f"没有该事件：{event_id}", retryable=False)
    if str(event.get("text_source")) == "llm":
        return {"event": event_id, "detail": event["detail"], "text_source": "llm", "calls": 0, "reused": True}
    claims = [item for item in store.claim_list(instance_id, timeline_id, event_id=event_id)
              if item.get("derived_from") is None]
    bucket = _day_bucket(time.time())
    limit = int(cfg.runtime.render_calls_per_day)
    messages = render_mod.skeleton_prompt(event, claims)
    world = _world_service(cfg, store)
    prompt_text = "\n".join(str(item.get("content") or "") for item in messages)
    reservation = world.reserve_call(instance_id, timeline_id, "event_render", prompt_text=prompt_text)
    if not reservation.get("ok"):
        used = store.call_ledger_get(instance_id, timeline_id, "event_render", bucket=bucket)
        return {
            "event": event_id,
            "detail": str(event.get("detail") or ""),
            "text_source": "template",
            "calls": 0,
            "budget": {"paused": True, "calls": used, "limit": limit, "blocked": reservation.get("blocked")},
        }
    detail, rendered_claims, calls, replies = str(event.get("detail") or ""), {}, 0, []
    for attempt in range(2):
        calls += 1
        text = await llm.chat(messages, temperature=0.4, timeout=60.0)
        replies.append(text)
        parsed = render_mod.parse_render(text, claims)
        if parsed and render_mod.facts_preserved(parsed["detail"], str(event.get("summary") or "")):
            if render_mod.has_checkable_facts(str(event.get("summary") or "")):
                detail, rendered_claims = parsed["detail"], parsed["claims"]
                break
            calls += 1  # 第二道护栏也是真调用，记进账本
            if await _grounded_else_numeric(llm, str(event.get("summary") or ""), parsed["detail"]):
                detail, rendered_claims = parsed["detail"], parsed["claims"]
                break
    if not rendered_claims:
        world.settle_call(
            reservation, prompt_text=prompt_text, reply="".join(replies), outcome="rejected", calls=calls
        )
        return {
            "event": event_id,
            "detail": str(event.get("detail") or ""),
            "text_source": "template",
            "calls": calls,
            "note": "表述未过校验，保留模板（不新增事实）",
        }
    store.event_render_save(instance_id, timeline_id, event_id, detail=detail, claims=rendered_claims)
    world.settle_call(reservation, prompt_text=prompt_text, reply="".join(replies), calls=calls)
    total = store.call_ledger_get(instance_id, timeline_id, "event_render", bucket=bucket)
    return {
        "event": event_id,
        "detail": detail,
        "claims": rendered_claims,
        "text_source": "llm",
        "calls": calls,
        "budget": {"paused": False, "calls": total, "limit": limit},
    }


async def _expand_claim(cfg: Config, llm: Any, store: Store | None, args: dict[str, Any]) -> dict[str, Any]:
    """惰性展开（§3.4）：只展开既定内容、产出派生记录、不假装读书经历。"""
    from ..runtime import render as render_mod

    if store is None:
        raise UmpError(Err.STATE_BLOCKED, "缺少存储上下文", retryable=False)
    instance_id, timeline_id = str(args.get("instance_id") or ""), str(args.get("timeline_id") or "")
    claim_id, character_id = str(args.get("claim_id") or ""), str(args.get("character_id") or "")
    question = str(args.get("question") or "这条记载还写了什么？")
    rows = [item for item in store.claim_list(instance_id, timeline_id) if str(item["id"]) == claim_id]
    if not rows:
        raise UmpError(Err.INVALID, f"没有该记载：{claim_id}", retryable=False)
    original = rows[0]
    holders = store.knowledge_holders(instance_id, timeline_id, claim_id)
    if character_id and character_id not in holders:
        raise UmpError(Err.INVALID, "该角色没有这条记载，不能凭空展开（物化不等于获知）", retryable=False)
    existing = store.claim_derived(instance_id, timeline_id, claim_id)
    if existing is not None:
        coverage = store.claim_coverage_get(instance_id, timeline_id, claim_id) or {}
        return {
            "claim": claim_id,
            "derived": existing["id"],
            "text": existing["text"],
            "state": str(coverage.get("state") or "done"),
            "calls": 0,
            "reused": True,
        }
    bucket = _day_bucket(time.time())
    used = store.call_ledger_get(instance_id, timeline_id, "claim_expand", bucket=bucket)
    limit = int(cfg.runtime.render_calls_per_day)
    if used >= limit:
        return {
            "claim": claim_id,
            "text": "",
            "state": "pending",
            "note": "尚未生成（不是这条记载没写下；别把没展开当成缺载）",
            "calls": 0,
            "budget": {"paused": True, "calls": used, "limit": limit},
        }
    text = await llm.chat(render_mod.expand_prompt(original, question=question), temperature=0.6, timeout=60.0)
    total = store.call_ledger_add(instance_id, timeline_id, "claim_expand", bucket=bucket, calls=1)
    if not render_mod.expansion_is_grounded(text, original):
        return {
            "claim": claim_id,
            "text": "",
            "calls": 1,
            "state": "pending",
            "note": "展开引入了原记载之外的事实，已丢弃（保留不知道；这次不算「已确认缺载」）",
            "budget": {"paused": False, "calls": total, "limit": limit},
        }
    if not render_mod.has_checkable_facts(str(original.get("text") or "")):
        total = store.call_ledger_add(instance_id, timeline_id, "claim_expand", bucket=bucket, calls=1)
        if not await _grounded_else_numeric(llm, str(original.get("text") or ""), text):
            return {
                "claim": claim_id,
                "text": "",
                "calls": 2,
                "state": "pending",
                "note": "展开补出了原记载之外的事实，已丢弃（保留不知道；这次不算「已确认缺载」）",
                "budget": {"paused": False, "calls": total, "limit": limit},
            }
    derived_id = f"cl-x-{str(original['id']).replace('cl-', '')}-{int(time.time())}"
    store.claim_put(
        {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "id": derived_id,
            "event_id": str(original["event_id"]),
            "source_id": str(original["source_id"]),
            "text": text.strip(),
            "audience": str(original.get("audience") or "公开"),
            "earliest_world": int(original.get("earliest_world") or 0),
            "credibility": str(original.get("credibility") or "recorded"),
            "derived_from": claim_id,
        }
    )
    # 覆盖状态（§3.4 / 附录B#10）：说清是「展开了」还是「这条记载确实没写下」——
    # 后者是留白，不是删改证据；前者也不冒充「全知」。
    state = "absent" if render_mod.declares_absence(text) else "done"
    note = (
        "这条来源没有写下这一条（缺载≠删改，历史记录不变）"
        if state == "absent"
        else "已展开为派生记录"
    )
    store.claim_coverage_put(
        {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "claim_id": claim_id,
            "state": state,
            "derived_id": derived_id,
            "note": note,
            "updated_world": 0,
        }
    )
    return {
        "claim": claim_id,
        "derived": derived_id,
        "text": text.strip(),
        "state": state,
        "note": note,
        "calls": 1,
        "budget": {"paused": False, "calls": total, "limit": limit},
    }




async def dispatch_async(
    cfg: Config,
    llm: Any,
    op: str,
    args: dict[str, Any],
    *,
    store: Store | None = None,
    runtime: Any = None,
) -> dict[str, Any]:
    """异步操作：涉及模型调用（生成 / 修订 / 补全）。候选一律不落盘，并带回调用用量。

    `runtime` = 运行层服务（或持有它的会话服务）：主动发言与初见要靠它，缺了就报错而不是
    NameError（这两个 op 是壳 / CLI 触发主动消息与开场的唯一入口）。
    """
    limit = args.get("max_calls")
    max_calls = int(limit) if isinstance(limit, int) and limit > 0 else None
    kwargs = {"max_calls": max_calls} if max_calls else {}
    try:
        if op == "event.render":
            return await _render_event(cfg, llm, store, args)
        if op == "event.expand":
            return await _expand_claim(cfg, llm, store, args)
        if op == "runtime.first_contact":
            service = getattr(runtime, "service", runtime)
            if service is None or not hasattr(service, "first_contact"):
                raise UmpError(Err.INTERNAL, "本次调用没有带上运行层服务，无法生成开场", retryable=False)
            return await service.first_contact(
                str(args.get("instance_id") or ""),
                str(args.get("timeline_id") or ""),
                str(args.get("character_id") or ""),
                channel_id=_channel_ref(store, args),
                thread_id=str(args.get("thread_id") or "main"),
                llm=llm,
                max_text_len=int(getattr(cfg, "max_text_len", 0) or 0),
                max_parts=int(getattr(cfg, "max_parts", 0) or 0),
            )
        if op == "runtime.proactive":
            if runtime is None:
                raise UmpError(Err.INTERNAL, "本次调用没有带上运行层服务，无法生成主动消息", retryable=False)
            return await _proactive_tick(cfg, llm, store, runtime, args)
        if op == "runtime.propose":
            return await _propose_intents(cfg, llm, store, args)
        if op == "runtime.extract":
            return await _extract_memories(cfg, llm, store, args)
        if op == "event.draft":
            return await _draft_user_event(cfg, llm, store, args)
        if op == "world.package.generate":
            package, errors, usage = await generate_package(
                llm, str(args.get("brief") or ""), name=str(args.get("name") or "未命名世界"), **kwargs
            )
        elif op == "world.package.revise":
            package, errors, usage = await revise_package(
                llm, _package_arg(args, cfg), str(args.get("instruction") or ""), **kwargs
            )
        elif op == "world.package.fill":
            package, errors, usage = await fill_section(
                llm, _package_arg(args, cfg), str(args.get("section") or ""), **kwargs
            )
        elif op == "world.card.generate":
            package, errors, usage = await generate_card(
                llm, _package_arg(args, cfg), str(args.get("brief") or ""), **kwargs
            )
        else:
            raise UmpError(Err.UNSUPPORTED_TYPE, f"未知管理操作 {op}", retryable=False)
    except LLMError as exc:
        code = Err.LLM_NOT_CONFIGURED if exc.code == "llm_not_configured" else Err.GENERATION_FAILED
        raise UmpError(code, f"生成失败：{exc.code}", retryable=exc.retryable) from exc
    except (InstanceError, PackageError) as exc:
        raise UmpError(Err.INVALID, str(exc), retryable=False) from exc
    return {"candidate": package, "errors": errors, "valid": not errors, "usage": usage}


def describe_ops() -> dict[str, Any]:
    return {"sync": sorted(SYNC_OPS), "async_": sorted(ASYNC_OPS)}


def _backup_folder(cfg: Any, store: Any) -> Path:
    """备份目录：配置里给相对路径就挂在数据根下。"""
    raw = str(getattr(getattr(cfg, "backup", None), "dir", "") or "backups")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(store.path).parent / path
    return path


def backup_once(cfg: Any, store: Any, *, note: str = "") -> dict[str, Any]:
    """落一份一致水位备份（DB + 确认过的世界包 / 草稿）并按保留数轮转。

    `backup.create`、到期补做与退出前补做共用这**一条**路径——备份内容三处一致（§3.3/§五）。
    """
    folder = _backup_folder(cfg, store)
    folder.mkdir(parents=True, exist_ok=True)  # 首次到期补做时目录还不存在
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    result = store.backup_create(folder / f"isekai-{stamp}.db", note=note)
    if result["ok"]:
        # 世界包与草稿不是 DB 里的行：单独打一份配对 zip（§3.3「不是只要 DB」）
        result["packages"] = _archive_packages(cfg, folder, stamp)
        store.backup_prune(folder, keep=int(getattr(cfg.backup, "keep", 7)))
    return result


def _packages_folder(cfg: Any) -> Path | None:
    """世界包 / 草稿目录（不存在就不备份这一半）。"""
    raw = getattr(getattr(cfg, "paths", None), "packages", "")
    if not raw:
        return None
    folder = Path(raw)
    return folder if folder.is_dir() else None


def _archive_packages(cfg: Any, folder: Path, stamp: str) -> str:
    packages = _packages_folder(cfg)
    if packages is None:
        return ""
    target = folder / f"isekai-{stamp}.packages"
    try:
        shutil.make_archive(str(target), "zip", root_dir=str(packages.parent), base_dir=packages.name)
    except OSError as exc:  # 备份主体已成功，这一半失败只记录
        log.warning("packages archive failed: %s", exc)
        return ""
    return f"{target}.zip"


def _restore_packages(cfg: Any, backup_path: Path, folder: Path) -> str:
    """整库恢复时把配对的世界包快照一并放回（§十.20）：先留安全副本，再整体替换目录内容。"""
    packages = _packages_folder(cfg)
    companion = backup_path.with_name(backup_path.name.replace(".db", ".packages.zip"))
    if packages is None or not companion.is_file():
        return ""
    safety = folder / f"packages-before-restore-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    shutil.make_archive(str(safety.with_suffix("")), "zip", root_dir=str(packages.parent), base_dir=packages.name)
    # 整库恢复=回到备份时点：先清空目录内容（解包只覆盖同名文件，不会删掉备份后新增的草稿）
    for item in packages.iterdir():
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
    shutil.unpack_archive(str(companion), str(packages.parent))
    return str(companion)


def _request_exit(delay: float = 0.5) -> None:
    """请求核心自行退出：隔一拍再抛 SystemExit，让本帧回复先送出。

    核心没有别的退出通路（app.py 只在父进程消失时停），这一抛会中断事件循环，
    由 app.py 的 finally 收尾——不经过 taskkill /F，写库锁会被正常释放。
    """
    def raise_exit() -> None:
        raise SystemExit(0)

    asyncio.get_running_loop().call_later(delay, raise_exit)


def _channel_ref(store: Store | None, args: dict[str, Any]) -> str:
    """管理面按**通道名**给参数（与 channel.ensure / thread.bind 同一口径），存储要的是通道标识。"""
    raw = str(args.get("channel_id") or args.get("channel") or "builtin")
    if store is None or not raw or raw.startswith("ci-"):
        return raw
    row = store.channel_by_name(raw)
    return str((row or {}).get("id") or raw)


async def _proactive_tick(cfg: Any, llm: Any, store: Any, runtime: Any, args: dict[str, Any]) -> dict[str, Any]:
    """世界源主动发言：管理面 / CLI / 核心 tick 触发一次（补算后由调用方决定何时调）。"""
    _ = store
    service = getattr(runtime, "service", runtime)
    per_day = int(args.get("per_day") or 2)
    return await service.proactive_tick(
        str(args.get("instance_id") or ""),
        str(args.get("timeline_id") or ""),
        llm=llm,
        per_day=per_day,
        max_text_len=int(getattr(cfg, "max_text_len", 0) or 0),
        max_parts=int(getattr(cfg, "max_parts", 0) or 0),
    )
