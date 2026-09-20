"""世界实例：创建、命名、多实例管理与锁定快照。

要点（WORLD_SETTING_SPEC §3、§7.4）：
- 创建 = 装配 → 联合校验 → 命名 → 一次性固化（设定快照 + 种子 + 初始提交），失败不留半个实例；
- 实例与世界包文件解耦：快照在创建时深拷贝，此后改文件不追溯实例；
- 名称全局唯一，冲突自动追加 `_2`、`_3`…，显式重命名冲突则明确拒绝（不静默改名）。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..log import get_logger
from ..store import Store
from ..version import APP_VERSION, DATA_FORMAT_VERSION, RULES_VERSION
from . import converters
from .cards import validate_assembly
from .package import (
    PackageError,
    clone_package,
    ensure_original_name,
    normalize_name,
    read_json_file,
    unique_name,
)
from .validate import validate_package

log = get_logger("isekai.world.instances")

# 名称分配 + 插入要在同一个临界区里（进程内并发）；跨进程靠 UNIQUE 冲突重试兜住
# ponytail: 进程内锁，多进程并发只靠重试，真要扛住得多进程协调（当前单机单核不必要）
_NAME_LOCK = threading.Lock()

TIMELINE_MAIN = "main"


class InstanceError(ValueError):
    """创建 / 管理操作被校验拒绝：错误列表逐条给出原因。"""

    def __init__(self, errors: list[str] | str) -> None:
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("；".join(self.errors))


def new_instance_id() -> str:
    return f"in-{secrets.token_hex(6)}"


def new_commit_id() -> str:
    return f"cm-{secrets.token_hex(6)}"


def allocate_name(base: str, taken: list[str]) -> str:
    """名称分配：`normalize_name` 等价即视为冲突，冲突则取最小可用序号（从 _2 起）。"""
    return unique_name(base, taken)


def create_instance(
    store: Store,
    package: dict[str, Any],
    cards: list[dict[str, Any]],
    *,
    display_name: str | None = None,
    imported: bool = False,
    seed: str | None = None,
    extra_setting: dict[str, Any] | None = None,
    timelines: list[dict[str, Any]] | None = None,
    commits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """从世界包与已确认角色卡创建实例；校验不通过即失败且不留半个实例。

    `timelines` / `commits` 预留给导入路径：导入要在同一次原子写入里恢复时间线与提交行。
    """
    errors = validate_package(package)
    if errors:
        raise InstanceError(errors)
    moment = int(package["calendar"]["initial_moment"])
    errors = validate_assembly(package, cards, moment=moment)
    if errors:
        raise InstanceError(errors)

    original = ensure_original_name(package)
    # 显示名创建时从**原始名称**复制一次；之后改世界包显示名不生效（§7.2）
    wanted = (display_name or original).strip()
    if not wanted:
        raise InstanceError("实例名称不能为空")
    name = allocate_name(wanted, store.instance_names())

    instance_id = new_instance_id()
    now = time.time()
    default_timeline = f"tl-{secrets.token_hex(4)}"
    default_commit = new_commit_id()
    timeline_rows = [
        {"instance_id": instance_id, **item}
        for item in (
            timelines
            if timelines is not None
            else [
                {
                    "id": default_timeline,
                    "name": "初始时间线",
                    "state": "frozen",  # 创建 / 导入不等于激活（§3.2、§7.3）
                    "source_commit": default_commit,
                    "created_at": now,
                }
            ]
        )
    ]
    commit_rows = [
        {"instance_id": instance_id, **item}
        for item in (
            commits
            if commits is not None
            else [
                {
                    "id": default_commit,
                    "timeline_id": default_timeline,
                    "kind": "import" if imported else "initial",
                    "moment": moment,
                    "note": "导入创建" if imported else "实例创建",
                    "created_at": now,
                }
            ]
        )
    ]
    setting: dict[str, Any] = {
        "world_package": clone_package(package),
        "original_name": original,
        "cards": json.loads(json.dumps(cards, ensure_ascii=False)),
    }
    if extra_setting:
        setting.update(extra_setting)
    row = {
        "id": instance_id,
        "name": name,
        "original_name": original,
        "package_id": str(package.get("meta", {}).get("package_id") or ""),
        "data_format": DATA_FORMAT_VERSION,
        "rules_version": RULES_VERSION,
        "app_version": APP_VERSION,
        "seed": seed or secrets.token_hex(16),
        "moment": moment,
        "setting": json.dumps(setting, ensure_ascii=False),
        "imported": 1 if imported else 0,
        "created_at": now,
    }
    # 并发创建 / 导入：两个进程可能选中同一个候选名，UNIQUE 冲突就按最新占用重算（§7.4 全局唯一）
    with _NAME_LOCK:
        for attempt in range(8):
            row["name"] = allocate_name(wanted, store.instance_names()) if attempt else name
            if attempt:
                row["id"] = instance_id = new_instance_id()
                timeline_rows = [{**item, "instance_id": instance_id} for item in timeline_rows]
                commit_rows = [{**item, "instance_id": instance_id} for item in commit_rows]
            try:
                store.instance_create(row, timelines=timeline_rows, commits=commit_rows)
            except sqlite3.IntegrityError:
                log.warning("实例名称并发冲突，重算候选名 attempt=%s name=%s", attempt, row["name"])
                continue
            if store.instance_get(row["id"]) is not None:
                break
            # 极偶发：并发下这次写入会丢在别人的事务里（提交了却读不回来）。名字仍是空的，
            # 换个标识重来即可——重试有界，不掩盖别的问题。
            # ponytail: 兜住共享连接隐式事务的丢写；真根因（每线程独立连接）留待 store 重构
            log.warning("实例行未落盘，重试 attempt=%s id=%s", attempt, row["id"])
            continue
        else:
            raise InstanceError("实例名称分配失败：并发冲突过多，请重试")
    # 每个提交都要自带快照（§7.1 提交闭包）：创建期的初始提交同样得能回滚 / 分叉，
    # 否则「回滚到创建点」「从创建提交分叉」在本地实例上就已经不可用
    from ..runtime import versioning  # 局部导入：versioning 在 runtime 层，顶层导入会成环

    for item in commit_rows:
        if store.commit_snapshot_get(str(item["id"])) is not None:
            continue
        store.commit_snapshot_put(
            str(item["id"]),
            instance_id,
            json.dumps(
                versioning.snapshot_of(store, instance_id, str(item["timeline_id"])), ensure_ascii=False
            ),
        )
    created = store.instance_get(instance_id)
    created = store.instance_get(instance_id)
    assert created is not None, f"实例创建后读不回来：{instance_id}"
    return public_info(created)


def compatibility(row: dict[str, Any]) -> tuple[str, str]:
    """打开实例的兼容检查（§7.6）：compatible / convertible / blocked。不改变任何状态。

    运行层按此结果决定推进、转换或在兼容性阻断下只读；阶段 1 只暴露结果与提示。
    """
    data_major = str(row.get("data_format") or "").split(".")[0]
    ours_major = DATA_FORMAT_VERSION.split(".")[0]
    if str(row.get("data_format")) == DATA_FORMAT_VERSION and str(row.get("rules_version")) == RULES_VERSION:
        return "compatible", ""
    if data_major == ours_major or converters.can_convert(str(row.get("data_format") or ""), DATA_FORMAT_VERSION):
        return (
            "convertible",
            f"数据格式 {row.get('data_format')} → {DATA_FORMAT_VERSION}（规则 {row.get('rules_version')} → {RULES_VERSION}）：需在副本上转换后使用",
        )
    return (
        "blocked",
        f"数据格式主版本不兼容（导出件 {row.get('data_format')}，本端 {DATA_FORMAT_VERSION}）：停止推进，等待兼容版本或转换",
    )


def convert_instance(
    store: Any, instance_id: str, *, confirmed: bool, exports_dir: Any = None
) -> dict[str, Any]:
    """可信转换的执行路径（§7.6）：自动发现 → 用户确认 → 副本转换 → 完整校验 → 原子发布。

    - 没有登记转换器就停在提示上（用兼容版本），不「尽量加载」；
    - 未确认只回状态，不动数据（执行转换需用户确认，§7.5）；
    - 转换前先留一份可恢复副本（原实例整体导出），校验失败时原实例一字不动；
    - 发布是**一个事务**：设定快照 + 新的数据 / 规则版本标记一起写，线先冻结由用户明确激活。
    """
    from . import converters, portable
    from .validate import validate_package

    row = store.instance_get(instance_id)
    if row is None:
        raise InstanceError(f"实例不存在：{instance_id}")
    state, reason = compatibility(row)
    source, target = str(row.get("data_format") or ""), DATA_FORMAT_VERSION
    if state == "compatible":
        return {"converted": False, "state": "compatible", "reason": reason}
    if not converters.can_convert(source, target):
        return {
            "converted": False,
            "state": "blocked",
            "reason": reason,
            "hint": "没有可信转换器：请用兼容版本打开，或等提供转换器后再试",
        }
    if not confirmed:
        return {"converted": False, "state": "convertible", "reason": reason, "needs_confirmation": True}

    safety = ""
    if exports_dir is not None:
        from pathlib import Path

        folder = Path(exports_dir)
        folder.mkdir(parents=True, exist_ok=True)
        safety_path = folder / f"{row['name']}-before-convert-{int(time.time())}.isekai.json"
        portable.write_export(store, instance_id, safety_path)
        safety = str(safety_path)

    setting = json.loads(row["setting"])
    converted = converters.convert_payload(setting, source=source, target=target)  # 副本上转换
    package = converted.get("world_package") if isinstance(converted.get("world_package"), dict) else {}
    errors = [str(item) for item in validate_package(package)]
    from .cards import validate_card

    for card in converted.get("cards") or []:
        if isinstance(card, dict):
            errors.extend(str(item) for item in validate_card(card, package, moment=int(row["moment"] or 0)))
    if errors:
        raise converters.ConverterError("转换产物未通过完整校验：" + "；".join(errors[:5]))

    store.instance_convert(
        instance_id,
        setting=json.dumps(converted, ensure_ascii=False),
        data_format=str(target),
        rules_version=RULES_VERSION,
    )
    return {
        "converted": True,
        "state": "compatible",
        "from": source,
        "to": str(target),
        "rules_version": RULES_VERSION,
        "safety": safety,
    }


def public_info(row: dict[str, Any]) -> dict[str, Any]:
    """管理面可见的实例元数据：不暴露世界内部内容（§3.5）。"""
    status, note = compatibility(row)
    return {
        "id": row["id"],
        "name": row["name"],
        "original_name": row["original_name"],
        "package_id": row["package_id"],
        "data_format": row["data_format"],
        "rules_version": row["rules_version"],
        "moment": row["moment"],
        "imported": bool(row["imported"]),
        "created_at": row["created_at"],
        "compatibility": status,
        "compatibility_note": note,
    }


def list_instances(store: Store) -> list[dict[str, Any]]:
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "original_name": row["original_name"],
            "package_id": row["package_id"],
            "moment": row["moment"],
            "imported": bool(row["imported"]),
            "created_at": row["created_at"],
            "timelines": row["timelines"],
            "sessions": row["sessions"],
        }
        for row in store.instance_list()
    ]


def get_setting(store: Store, instance_id: str) -> dict[str, Any]:
    """锁定的设定原文（**仅内部 / 测试**）：管理面一律走 `public_setting`（§3.5 黑箱）。"""
    row = store.instance_get(instance_id)
    if row is None:
        raise InstanceError(f"实例不存在：{instance_id}")
    return json.loads(row["setting"])


def public_setting(store: Store, instance_id: str) -> dict[str, Any]:
    """实例设定的**公开面**（§3.5 完全黑箱）：只说「锁了什么」，不给内部正文。

    性格单元数值、实情层 / 传说条目正文、角色卡的其余字段都不在这里——要读内容只有两条正路：
    通过角色的认知（会话）或导出件（用户自己的包）。管理面借校验错误或整份设定偷看都算泄密。
    """
    row = store.instance_get(instance_id)
    if row is None:
        raise InstanceError(f"实例不存在：{instance_id}")
    setting = json.loads(row["setting"])
    package = setting.get("world_package") if isinstance(setting.get("world_package"), dict) else {}
    calendar = package.get("calendar") if isinstance(package.get("calendar"), dict) else {}
    meta = package.get("meta") if isinstance(package.get("meta"), dict) else {}
    cards: list[dict[str, Any]] = []
    for card in setting.get("cards") or []:
        if not isinstance(card, dict):
            continue
        card_meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
        identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
        cards.append(
            {
                "card_id": str(card_meta.get("card_id") or ""),
                "name": str(identity.get("name") or ""),
                "role_id": str(card.get("role_id") or ""),
                "confirmed": bool(card_meta.get("confirmed")),
            }
        )
    return {
        "original_name": str(setting.get("original_name") or ""),
        "world_package": {
            "package_id": str(meta.get("package_id") or ""),
            "original_name": str(meta.get("original_name") or ""),
            "era": str(calendar.get("era") or ""),
            "day_seconds": calendar.get("day_seconds"),
            "initial_moment": calendar.get("initial_moment"),
            "density": str(meta.get("density") or ""),
        },
        "cards": cards,
    }


def rename_instance(store: Store, instance_id: str, name: str) -> dict[str, Any]:
    row = store.instance_get(instance_id)
    if row is None:
        raise InstanceError(f"实例不存在：{instance_id}")
    wanted = name.strip()
    if not wanted:
        raise InstanceError("实例名称不能为空")
    others = [n for n in store.instance_names() if normalize_name(n) != normalize_name(row["name"])]
    if normalize_name(wanted) in {normalize_name(n) for n in others}:
        raise InstanceError(f"名称已被占用：{wanted}（请换一个）")
    store.instance_rename(instance_id, wanted)
    updated = store.instance_get(instance_id)
    assert updated is not None
    return public_info(updated)


def delete_instance(store: Store, instance_id: str) -> None:
    if store.instance_get(instance_id) is None:
        raise InstanceError(f"实例不存在：{instance_id}")
    store.instance_delete(instance_id)


def load_cards(package: dict[str, Any], card_paths: list[str]) -> list[dict[str, Any]]:
    """装配前读入角色卡文件；只有用户确认过的最终版本可进入实例（CHARACTER_CARD §5.2）。"""
    cards: list[dict[str, Any]] = []
    for path in card_paths:
        try:
            raw = read_json_file(path, what="角色卡文件")  # 与包 / 导入件共用同一道闸（含读取前字节限额）
        except PackageError as exc:
            raise InstanceError(str(exc)) from exc
        if not isinstance(raw, dict):
            raise InstanceError(f"角色卡顶层必须是对象：{path}")
        cards.append(raw)
    return cards


def save_card(path: str, card: dict[str, Any]) -> None:
    """角色卡只保存最终确认版本：确认后写入，不保留生成历史。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)  # 首次导入时创作目录可能还不存在
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(card, ensure_ascii=False, indent=2))


__all__ = [
    "InstanceError",
    "PackageError",
    "TIMELINE_MAIN",
    "allocate_name",
    "create_instance",
    "delete_instance",
    "get_setting",
    "list_instances",
    "load_cards",
    "public_info",
    "rename_instance",
    "save_card",
]
