"""世界实例：创建、命名、多实例管理与锁定快照。

要点（WORLD_SETTING_SPEC §3、§7.4）：
- 创建 = 装配 → 联合校验 → 命名 → 一次性固化（设定快照 + 种子 + 初始提交），失败不留半个实例；
- 实例与世界包文件解耦：快照在创建时深拷贝，此后改文件不追溯实例；
- 名称全局唯一，冲突自动追加 `_2`、`_3`…，显式重命名冲突则明确拒绝（不静默改名）。
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from ..store import Store
from ..version import APP_VERSION, DATA_FORMAT_VERSION, RULES_VERSION
from .cards import validate_assembly
from .package import PackageError, clone_package, ensure_original_name, normalize_name, unique_name
from .validate import validate_package

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
) -> dict[str, Any]:
    """从世界包与已确认角色卡创建实例；校验不通过即失败且不留半个实例。"""
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
    timeline_id = f"tl-{secrets.token_hex(4)}"
    commit_id = new_commit_id()
    now = time.time()
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
    store.instance_create(
        row,
        timelines=[
            {
                "id": timeline_id,
                "instance_id": instance_id,
                "name": "初始时间线",
                "state": "frozen",  # 创建不等于激活（§4）
                "source_commit": commit_id,
                "created_at": now,
            }
        ],
        commits=[
            {
                "id": commit_id,
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "kind": "import" if imported else "initial",
                "moment": moment,
                "note": "导入创建" if imported else "实例创建",
                "created_at": now,
            }
        ],
    )
    created = store.instance_get(instance_id)
    assert created is not None
    return public_info(created)


def public_info(row: dict[str, Any]) -> dict[str, Any]:
    """管理面可见的实例元数据：不暴露世界内部内容（§3.5）。"""
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
    row = store.instance_get(instance_id)
    if row is None:
        raise InstanceError(f"实例不存在：{instance_id}")
    return json.loads(row["setting"])


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
            raw = json.loads(open(path, encoding="utf-8").read())
        except FileNotFoundError as exc:
            raise InstanceError(f"角色卡文件不存在：{path}") from exc
        except json.JSONDecodeError as exc:
            raise InstanceError(f"角色卡不是合法 JSON（{path}）：{exc}") from exc
        if not isinstance(raw, dict):
            raise InstanceError(f"角色卡顶层必须是对象：{path}")
        cards.append(raw)
    return cards


def save_card(path: str, card: dict[str, Any]) -> None:
    """角色卡只保存最终确认版本：确认后写入，不保留生成历史。"""
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
