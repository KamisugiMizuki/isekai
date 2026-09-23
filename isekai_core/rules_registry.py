"""规则插件登记（USER_INTERFACE_DESIGN §8.5）：本机登记簿，与「外部聊天通道插件」分开。

这里只回答三件事：这台机器上登记过哪些规则、它们现在能不能用、有没有战役还在用。
裁定与进程隔离仍归 `runtime/rules.py` 与插件子进程；本模块不执行任何插件代码。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .runtime import rules as rules_mod
from .ump import Err, UmpError

#: 支持的规则插件协议（与 `rules.load_manifest` 同一口径）
SUPPORTED_PROTOCOL = "isekai.trpg.rules/1"
#: 清单文件名：目录里先认它，没有再扫其它 *.json
MANIFEST_NAMES = ("manifest.json", "rules.json")

STATUS_TEXT = {
    "available": "可用",
    "disabled": "未启用",
    "missing": "依赖缺失",
    "incompatible": "不兼容",
}


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:32]
    except OSError:
        return ""


def find_manifest(target: str | Path) -> Path | None:
    """给目录或清单文件，找到那份清单；找不到返回 None（不猜）。"""
    path = Path(target)
    if path.is_file():
        return path
    if path.is_dir():
        for name in MANIFEST_NAMES:
            candidate = path / name
            if candidate.is_file():
                return candidate
        for candidate in sorted(path.glob("*.json")):
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("protocol") == SUPPORTED_PROTOCOL:
                return candidate
    return None


def inspect(manifest_path: str | Path) -> dict[str, Any]:
    """读一份清单并给出检查摘要（不认识 / 读不动都如实说，不执行它）。"""
    path = Path(manifest_path)
    try:
        manifest = rules_mod.load_manifest(path)
    except rules_mod.RulePluginError as exc:
        return {"manifest_path": str(path), "status": "incompatible", "reason": str(exc),
                "name": "", "ruleset_id": "", "ruleset_version": ""}
    entry = [str(part) for part in manifest.get("entry") or [] if str(part)]
    ruleset_id = str(manifest.get("ruleset_id") or manifest.get("id") or "")
    version = str(manifest.get("ruleset_version") or manifest.get("version") or "")
    missing = "规则插件入口不存在" if not (path.parent / entry[-1]).exists() and len(entry) > 1 else ""
    return {
        "manifest_path": str(path.resolve()),
        "name": str(manifest.get("name") or ruleset_id or path.parent.name),
        "ruleset_id": ruleset_id,
        "ruleset_version": version,
        "protocol": str(manifest.get("protocol") or ""),
        "entry": entry,
        "modes": [str(item) for item in manifest.get("modes") or []],
        "state_schema": str(manifest.get("state_schema") or ""),
        "resident": bool(manifest.get("resident")),
        "has_converters": bool(manifest.get("converters")),
        "status": "missing" if missing else "available",
        "reason": missing,
        "sha256": _sha256(path),
    }


def scan(target: str | Path) -> dict[str, Any]:
    """按用户选中的目录 / 文件给出候选（只读，不登记、不执行）。"""
    folder = Path(target)
    manifests: list[Path] = []
    if folder.is_file():
        manifests = [folder]
    elif folder.is_dir():
        found = find_manifest(folder)
        if found is not None:
            manifests = [found]
        else:
            # 目录里可能一层深地放着多个规则：扫一级子目录（不做递归深挖）
            for child in sorted(item for item in folder.iterdir() if item.is_dir()):
                nested = find_manifest(child)
                if nested is not None:
                    manifests.append(nested)
    else:
        raise UmpError(Err.NOT_FOUND, f"这个位置读不到：{folder}", retryable=False)
    if not manifests:
        return {"candidates": [], "reason": "这里没有找到规则插件清单（manifest.json / rules.json）"}
    return {"candidates": [inspect(item) for item in manifests]}


def status_of(row: dict[str, Any], store: Any) -> dict[str, Any]:
    """登记后的当前状态：可用 / 未启用 / 依赖缺失 / 不兼容 + 被哪些战役引用。"""
    manifest_path = str(row.get("manifest_path") or "")
    current = inspect(manifest_path) if manifest_path else {"status": "missing", "reason": "登记时记录的清单位置已经不在"}
    references = store.rule_plugin_references(str(row.get("ruleset_id") or ""), str(row.get("ruleset_version") or ""))
    base = str(current.get("status") or "missing")
    if base == "available" and not int(row.get("enabled") or 0):
        base = "disabled"
    return {
        "ruleset_id": str(row.get("ruleset_id") or ""),
        "ruleset_version": str(row.get("ruleset_version") or ""),
        "name": str(row.get("name") or "") or str(current.get("name") or ""),
        "manifest_path": manifest_path,
        "enabled": bool(int(row.get("enabled") or 0)),
        "status": base,
        "status_text": STATUS_TEXT.get(base, base),
        "reason": str(current.get("reason") or "") if base in ("missing", "incompatible") else "",
        "protocol": str(row.get("protocol") or ""),
        "modes": json.loads(str(row.get("modes") or "[]")),
        "state_schema": str(row.get("state_schema") or ""),
        "has_converters": bool(int(row.get("has_converters") or 0)),
        "changed_since_registered": bool(str(row.get("sha256") or "")) and str(row.get("sha256") or "") != str(current.get("sha256") or ""),
        "referenced_by": references,
        "referenced_count": len(references),
        "registered_at": float(row.get("registered_at") or 0),
    }


def list_plugins(store: Any) -> dict[str, Any]:
    return {"plugins": [status_of(row, store) for row in store.rule_plugin_list()],
            "supported_protocol": SUPPORTED_PROTOCOL}


def register(store: Any, *, manifest_path: str | Path, request_id: str = "") -> dict[str, Any]:
    """登记 + 启用（界面上是「添加并启用」）：同一身份与版本换了内容不许静默覆盖。"""
    summary = inspect(manifest_path)
    if summary["status"] != "available":
        raise UmpError(Err.INVALID, summary.get("reason") or "这份规则插件清单不能用于登记", retryable=False)
    ruleset_id = str(summary["ruleset_id"])
    version = str(summary["ruleset_version"])
    if not ruleset_id or not version:
        raise UmpError(Err.INVALID, "规则插件清单缺少规则标识或版本", retryable=False)
    existing = store.rule_plugin_get(ruleset_id, version)
    if existing is not None:
        if str(existing.get("sha256") or "") == str(summary["sha256"]):
            saved = store.rule_plugin_upsert({**existing, "enabled": 1, "updated_real": time.time()})
            return {"plugin": status_of(saved, store), "updated": True,
                    "must_not_imply": "登记等于验证过它的规则内容"}
        raise UmpError(
            Err.INVALID,
            f"{ruleset_id} {version} 已经登记过，但内容与这次不一致："
            "同一个规则版本不能被不同内容静默覆盖（要换内容请改版本号，或先移除旧登记）",
            retryable=False,
        )
    saved = store.rule_plugin_upsert({
        "ruleset_id": ruleset_id, "ruleset_version": version, "name": str(summary["name"]),
        "manifest_path": str(summary["manifest_path"]), "protocol": str(summary["protocol"]),
        "modes": json.dumps(summary["modes"], ensure_ascii=False),
        "state_schema": str(summary["state_schema"]), "sha256": str(summary["sha256"]),
        "has_converters": 1 if summary["has_converters"] else 0,
        "enabled": 1, "request_id": str(request_id or ""), "registered_at": time.time(),
        "updated_real": time.time(),
    })
    return {"plugin": status_of(saved, store), "updated": False,
            "must_not_imply": "登记等于验证过它的规则内容"}


def set_enabled(store: Any, *, ruleset_id: str, ruleset_version: str, enabled: bool) -> dict[str, Any]:
    row = store.rule_plugin_get(ruleset_id, ruleset_version)
    if row is None:
        raise UmpError(Err.NOT_FOUND, f"没有登记过这条规则：{ruleset_id} {ruleset_version}", retryable=False)
    saved = store.rule_plugin_upsert({**row, "enabled": 1 if enabled else 0, "updated_real": time.time()})
    return {"plugin": status_of(saved, store),
            "must_not_imply": "停用会撤回已经算出的结果" if not enabled else "启用等于这条规则已经验证过"}


def remove(store: Any, *, ruleset_id: str, ruleset_version: str) -> dict[str, Any]:
    """被战役引用时拒绝移除，并把战役列出来（§8.5）。"""
    row = store.rule_plugin_get(ruleset_id, ruleset_version)
    if row is None:
        raise UmpError(Err.NOT_FOUND, f"没有登记过这条规则：{ruleset_id} {ruleset_version}", retryable=False)
    references = store.rule_plugin_references(ruleset_id, ruleset_version)
    if references:
        raise UmpError(
            Err.INVALID,
            "这些战役还在用这条规则，不能移除：" + "、".join(
                f"{item['name'] or item['campaign_id']}（{item['instance_name']}）" for item in references
            ) + "。可以先停用（只阻止之后调用），或先把那些战役处理掉",
            retryable=False,
        )
    store.rule_plugin_delete(ruleset_id, ruleset_version)
    return {"removed": True, "ruleset_id": ruleset_id, "ruleset_version": ruleset_version}
