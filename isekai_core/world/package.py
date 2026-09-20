"""世界包：结构、模板与文件读写。

- 单文件 JSON，可手编、可 diff，只保存当前确认版本（WORLD_SETTING_SPEC §2.3）。
- 「原始世界包名称」独立记录：编辑显示名不改它，创建 / 导出把该记录嵌入快照（§7.2）。
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable

PACKAGE_SCHEMA_VERSION = "1.0"
DENSITIES = ("sparse", "normal", "rich")
NAME_MAX_LEN = 64


class PackageError(ValueError):
    """世界包文件层面的错误：结构不可解析、写入失败等。"""


def new_package_id() -> str:
    return f"wp-{secrets.token_hex(6)}"


def normalize_name(name: str) -> str:
    """名称比较规则：去首尾空白 + NFKC 归一 + 大小写折叠，跨端一致（§7.4）。"""
    return unicodedata.normalize("NFKC", str(name)).strip().casefold()


def unique_name(base: str, taken: Iterable[str]) -> str:
    """冲突时追加 `_2`、`_3`…，取最小可用序号；base 去空白后为空则抛错。"""
    wanted = str(base).strip()
    if not wanted:
        raise PackageError("名称不能为空")
    existing = {normalize_name(name) for name in taken}
    if normalize_name(wanted) not in existing:
        return wanted
    index = 2
    while True:
        candidate = f"{wanted}_{index}"
        if normalize_name(candidate) not in existing:
            return candidate
        index += 1


def _entry(prefix: str, index: int) -> str:
    return f"{prefix}-{index}"


def template_package(
    name: str = "未命名世界",
    *,
    density: str = "normal",
    day_seconds: int = 86400,
    era: str | None = None,
) -> dict[str, Any]:
    """表单式生成的最小骨架：字段齐备、内容待填，需通过校验后才能创建实例。"""
    display = normalize_name(name) or "未命名世界"
    return {
        "meta": {
            "schema": PACKAGE_SCHEMA_VERSION,
            "package_id": new_package_id(),
            "original_name": display,
            "display_name": name.strip() or "未命名世界",
            "description": "",
            "density": density if density in DENSITIES else "normal",
        },
        "calendar": {
            "era": era or "新纪元",
            "day_seconds": day_seconds,
            "months": [{"name": "一月", "days": 30}, {"name": "二月", "days": 30}, {"name": "三月", "days": 30}],
            "week": {"name": "周", "days": 7},
            "segments": [
                {"id": "seg-night", "name": "夜", "start": 0, "end": int(day_seconds * 0.25)},
                {"id": "seg-morning", "name": "晨", "start": int(day_seconds * 0.25), "end": int(day_seconds * 0.5)},
                {"id": "seg-day", "name": "昼", "start": int(day_seconds * 0.5), "end": int(day_seconds * 0.75)},
                {"id": "seg-evening", "name": "暮", "start": int(day_seconds * 0.75), "end": day_seconds},
            ],
            "initial_moment": 0,
        },
        "world": {
            "axioms": [{"id": "ax-1", "text": ""}],
            "geography": "",
            "society": "",
            "lexicon": {"note": "", "terms": [{"term": "", "meaning": ""}]},
            "institutions": [
                {
                    "id": "inst-1",
                    "name": "",
                    "mandate": "",
                    "scope": "",
                    "succession": "",
                    "validity": "",
                }
            ],
            "customs": [
                {
                    "id": "cus-1",
                    "name": "",
                    "applies_to": "",
                    "practice": "",
                    "basis": "",
                    "variation": "",
                }
            ],
        },
        "environment": {"types": []},
        "sources": [{"id": "src-1", "name": "", "kind": "personal", "reach": ""}],
        "canon": [{"id": "cf-1", "statement": "", "tags": []}],
        "narratives": [{"id": "nv-1", "text": "", "source_id": "src-1", "canon_ref": "cf-1", "obtain": [], "confidence": "believed"}],
        "entities": [],
        "races": [{"id": "rc-1", "name": "", "lifespan": {"min_years": 60, "max_years": 90}}],
        "historiography": [],
        "events": {"families": [{"id": "ef-1", "name": "", "templates": [{"id": "et-1", "summary": "", "preconditions": [], "effects": [{"kind": "", "target": ""}], "weight": 1}]}]},
        "life": [{"id": "lf-1", "name": "", "sleep": True, "windows": [{"start": 0, "end": day_seconds, "activity": ""}]}],
        "roles": [{"id": "rl-1", "name": "", "description": "", "life_template": "lf-1", "channels": ["src-1"]}],
        "comms": {"mechanisms": [{"id": "cm-1", "name": "", "limits": ""}]},
        "initial_state": {"events": [], "rumors": [], "mysteries": []},
    }


def ensure_original_name(package: dict[str, Any]) -> str:
    """原始名称在包首次通过系统确认时记录；之后改显示名不生效（§7.2）。"""
    meta = package.setdefault("meta", {})
    original = meta.get("original_name")
    if isinstance(original, str) and original.strip():
        return original.strip()
    fallback = str(meta.get("display_name") or "").strip() or "未命名世界"
    meta["original_name"] = fallback
    return fallback


def load_package(path: str | os.PathLike[str]) -> dict[str, Any]:
    file = Path(path)
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackageError(f"世界包文件不存在：{file}") from exc
    except json.JSONDecodeError as exc:
        raise PackageError(f"世界包不是合法 JSON（{file}）：{exc}") from exc
    if not isinstance(raw, dict):
        raise PackageError("世界包顶层必须是对象")
    if not isinstance(raw.get("meta"), dict):
        raise PackageError("世界包缺少 meta 段")
    return raw


def save_package(path: str | os.PathLike[str], package: dict[str, Any]) -> None:
    """原子替换最新版：先写临时文件再替换，失败不覆盖原文件（§2.4）。"""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    ensure_original_name(package)
    text = json.dumps(package, ensure_ascii=False, indent=2)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(file.parent), delete=False, suffix=".tmp"
    )
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, file)


def clone_package(package: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(package, ensure_ascii=False))
