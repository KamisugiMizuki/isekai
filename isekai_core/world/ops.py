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
    }
)
ASYNC_OPS = frozenset(
    {
        "event.render",
        "event.expand",
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
        return {"claim": claim_id, "derived": existing["id"], "text": existing["text"], "calls": 0, "reused": True}
    bucket = _day_bucket(time.time())
    used = store.call_ledger_get(instance_id, timeline_id, "claim_expand", bucket=bucket)
    limit = int(cfg.runtime.render_calls_per_day)
    if used >= limit:
        return {"claim": claim_id, "text": "", "calls": 0, "budget": {"paused": True, "calls": used, "limit": limit}}
    text = await llm.chat(render_mod.expand_prompt(original, question=question), temperature=0.6, timeout=60.0)
    total = store.call_ledger_add(instance_id, timeline_id, "claim_expand", bucket=bucket, calls=1)
    if not render_mod.expansion_is_grounded(text, original):
        return {
            "claim": claim_id,
            "text": "",
            "calls": 1,
            "note": "展开引入了原记载之外的事实，已丢弃（保留不知道）",
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
    return {
        "claim": claim_id,
        "derived": derived_id,
        "text": text.strip(),
        "calls": 1,
        "budget": {"paused": False, "calls": total, "limit": limit},
    }




async def dispatch_async(
    cfg: Config, llm: Any, op: str, args: dict[str, Any], *, store: Store | None = None
) -> dict[str, Any]:
    """异步操作：涉及模型调用（生成 / 修订 / 补全）。候选一律不落盘，并带回调用用量。"""
    limit = args.get("max_calls")
    max_calls = int(limit) if isinstance(limit, int) and limit > 0 else None
    kwargs = {"max_calls": max_calls} if max_calls else {}
    try:
        if op == "event.render":
            return await _render_event(cfg, llm, store, args)
        if op == "event.expand":
            return await _expand_claim(cfg, llm, store, args)
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
