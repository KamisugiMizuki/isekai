"""世界设定层的管理面操作：世界包 / 角色卡 / 实例 / 导入导出。

桌面端与安卓端共用同一套操作（§2.5）：两端只做表单与展示，schema、校验、
生成与修订流程都在这里。所有校验失败以 `invalid_input` 错误返回，附带逐条原因。
"""

from __future__ import annotations

import json
import re
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
    get_setting,
    list_instances,
    load_cards,
    public_info,
    rename_instance,
    save_card,
)
from .package import PackageError, load_package, save_package, template_package
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
    }
)
ASYNC_OPS = frozenset(
    {
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


def _card_arg(args: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """角色卡来源：内联 `card` 优先，否则读 `card_path`。"""
    inline = args.get("card")
    if isinstance(inline, dict):
        return inline
    path = resolve_path(cfg, args.get("card_path"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UmpError(Err.NOT_FOUND, f"角色卡文件不存在：{path}", retryable=False) from exc
    except json.JSONDecodeError as exc:
        raise UmpError(Err.INVALID, f"角色卡不是合法 JSON：{exc}", retryable=False) from exc
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
                card = json.loads(Path(resolve_path(cfg, args.get("card_path"))).read_text(encoding="utf-8"))
            return {
                "join": runtime.add_character(
                    instance_id,
                    timeline_id,
                    card,
                    now_real=now,
                    joined_world=int(args["joined_world"]) if args.get("joined_world") is not None else None,
                    note=str(args.get("note") or ""),
                    acquainted=bool(args.get("acquainted")),
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
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise UmpError(Err.NOT_FOUND, f"草稿不存在：{target.name}", retryable=False) from exc
            except json.JSONDecodeError as exc:
                raise UmpError(Err.INVALID, f"草稿不是合法 JSON：{exc}", retryable=False) from exc
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
            cards = setting.get("cards") or []
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
        if op == "instance.setting":
            return {"setting": get_setting(store, str(args.get("id") or ""))}
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


async def dispatch_async(cfg: Config, llm: Any, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """异步操作：涉及模型调用（生成 / 修订 / 补全）。候选一律不落盘，并带回调用用量。"""
    limit = args.get("max_calls")
    max_calls = int(limit) if isinstance(limit, int) and limit > 0 else None
    kwargs = {"max_calls": max_calls} if max_calls else {}
    try:
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
