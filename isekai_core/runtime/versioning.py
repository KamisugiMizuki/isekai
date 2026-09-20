"""阶段 4：版本管理——提交 / 分叉 / 回滚（WORLD_RUNTIME_SPEC §5 / §6 / §7）。

存储用**全量快照**（§六：完整快照与 diff 等价，本阶段先用全量验证相同语义）。
纳入提交的是世界运行状态；本机激活集合、视图、待生效倍率、通道绑定与凭据、投递回执、
运行世代等**控制状态**不进快照，也不随回滚恢复。
"""

from __future__ import annotations

import json
import time
from typing import Any


class VersionError(ValueError):
    """版本操作的前置条件不满足（提交不存在、线不存在等）。"""


def snapshot_of(store: Any, instance_id: str, timeline_id: str, *, note: str = "") -> dict[str, Any]:
    """取一致快照（§5.1）：整条线的世界运行状态 + 该线的对话原文与提取任务。"""
    clock = store.clock_get(timeline_id) or {}
    runtime = store.runtime_dump(instance_id, timeline_id, watermark=int(clock.get("processed_world") or 0))
    sessions = [
        item
        for item in store.instance_sessions(instance_id)
        if str(item.get("timeline_id")) == timeline_id
    ]
    dialog: list[dict[str, Any]] = []
    for session in sessions:
        page = store.history_page(str(session["id"]), limit=10000)
        for row in page.get("messages") or []:
            dialog.append({**row, "session_id": str(session["id"])})
    from ..version import DATA_FORMAT_VERSION, RULES_VERSION

    world_seed = ""
    try:
        from .service import RuntimeService  # 局部导入避免循环

        world_seed = RuntimeService(store).seed_of(store.instance_get(instance_id) or {})
    except Exception:  # 种子取不到不影响快照（空串如实记录）
        world_seed = ""
    return {
        "note": str(note or ""),
        "world": int(clock.get("processed_world") or 0),
        "rate": int(clock.get("rate") or 1),
        # 语义元数据（§5.1）：确定性复算要用的规则版本、数据格式与锁定种子，与抽样同源
        "rules_version": str(RULES_VERSION),
        "data_format": str(DATA_FORMAT_VERSION),
        "seed": str(world_seed),
        "sessions": [
            {key: value for key, value in item.items() if key in (
                "id", "instance_id", "timeline_id", "character_id", "channel_id", "thread_id", "created_at"
            )}
            for item in sessions
        ],
        "dialog": dialog,
        "runtime": runtime,
    }


def make_commit_row(commit_id: str, instance_id: str, timeline_id: str, *, kind: str, moment: int, note: str = "") -> dict[str, Any]:
    return {
        "id": commit_id,
        "instance_id": instance_id,
        "timeline_id": timeline_id,
        "kind": str(kind),
        "moment": int(moment),
        "note": str(note or ""),
        "created_at": time.time(),
    }


def public_commit(row: dict[str, Any]) -> dict[str, Any]:
    """列表只给管理元数据：标识 / 时间 / 备注 / 来源关系，不生成泄漏剧情的摘要（§5.1）。"""
    return {
        "id": str(row["id"]),
        "kind": str(row["kind"]),
        "moment": int(row["moment"]),
        "note": str(row.get("note") or ""),
        "timeline_id": str(row["timeline_id"]),
        "created_at": float(row.get("created_at") or 0.0),
    }


def parse_snapshot(payload: str | bytes | None) -> dict[str, Any]:
    if not payload:
        return {}
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise VersionError("快照损坏") from exc
    return data if isinstance(data, dict) else {}


# ---------- 快照的存储形态（§6 diff 复制 / §8 压缩）：全量与 diff 等价 ----------

SNAPSHOT_FULL = "full"
SNAPSHOT_DELTA = "delta"
#: 链长上限：超过就把最新的那条物化成全量，链重新从它开始（§8 减少 diff 链长度）
MAX_DELTA_CHAIN = 8


def _ordered_sections(payload: dict[str, Any]) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, Any]]:
    """把快照拆成 (段名顺序, {段名: {行键: 行}}, 标量)，行的先后按原样保留。

    容器（如 `runtime`）不整块进出：往下走一层，段名带前缀（`runtime/events`），
    diff 才落到**行**这一级，而不是「整个 runtime 变了」。
    """
    order: list[str] = []
    sections: dict[str, dict[str, Any]] = {}
    scalars: dict[str, Any] = {}

    def walk(prefix: str, node: dict[str, Any]) -> None:
        for key, value in (node or {}).items():
            name = f"{prefix}{key}"
            if isinstance(value, list):
                rows: dict[str, Any] = {}
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        rows[_row_key(item, index)] = item
                order.append(name)
                sections[name] = rows
            elif isinstance(value, dict) and any(isinstance(item, list) for item in value.values()):
                walk(f"{name}/", value)
            else:
                scalars[name] = value

    walk("", payload or {})
    return order, sections, scalars


def _set_nested(out: dict[str, Any], name: str, value: Any) -> None:
    parts = str(name).split("/")
    node = out
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _row_key(item: dict[str, Any], index: int) -> str:
    """行标识：优先主键类字段；没有就用该行全部 `*_id` 字段的组合（如环境行的 type_id）。

    索引只能当最后的兜底——按位置当键会让「换了一行」被误判成「改了同一行」。
    """
    for field in ("id", "message_id", "commit_id", "seq"):
        value = item.get(field)
        if value not in (None, ""):
            return f"{field}:{value}"
    parts = [
        f"{field}={item[field]}"
        for field in sorted(item)
        if field.endswith("_id") and item[field] not in (None, "")
    ]
    if parts:
        return "|".join(parts)
    return f"#{index}"


def encode_delta(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """祖先基线 + 差异（含删除标记）→ 目标（§6）。差异只看两侧，不看祖先之外的东西。

    每段带 `order`（目标里的行序）：物化时照它排，避免「排序键恰好等于主键」这种默认假设。
    """
    base_order, base_rows, base_scalars = _ordered_sections(base)
    order, rows, scalars = _ordered_sections(target)
    sections: dict[str, Any] = {}
    for name in set(base_rows) | set(rows):
        before, after = base_rows.get(name, {}), rows.get(name, {})
        added = [after[key] for key in after if key not in before]
        replaced = [after[key] for key in after if key in before and after[key] != before[key]]
        deleted = sorted(key for key in before if key not in after)
        row_order = list(after)
        if added or replaced or deleted or row_order != list(before):
            # 行序也算差异：物化要能逐字段对上，不能靠「恰好按主键排」
            sections[name] = {
                "added": added, "replaced": replaced, "deleted": deleted, "order": row_order,
            }
    return {
        "kind": SNAPSHOT_DELTA,
        "sections": sections,
        "order": order,
        "scalars": scalars if scalars != base_scalars else {},
    }


def apply_delta(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """物化（§6 所谓「diff 合并」就是祖先基线与差异的状态合成，不是时间线合并）。

    - 删除标记优先：祖先层还有旧值也不复活；
    - 行的先后按 delta 记下的 `order`，没有就沿用基线的顺序；
    - 标量（水位 / 规则版本等）有更新就用新值；容器照旧嵌套回原样。
    """
    base_order, base_rows, base_scalars = _ordered_sections(base)
    sections = (delta or {}).get("sections") or {}
    order = [str(name) for name in (delta or {}).get("order") or []]
    if not order:
        order = list(base_order)
        for name in sections:
            if name not in order:
                order.append(str(name))
    out: dict[str, Any] = {}
    for name in order:
        rows = dict(base_rows.get(name, {}))
        part = sections.get(name) or {}
        for key in part.get("deleted") or []:
            rows.pop(key, None)
        for item in (part.get("replaced") or []) + (part.get("added") or []):
            if isinstance(item, dict):
                rows[_row_key(item, 0)] = item
        row_order = [str(key) for key in (part.get("order") or [])] or list(rows)
        ordered = [rows[key] for key in row_order if key in rows]
        ordered.extend(rows[key] for key in rows if key not in row_order)
        _set_nested(out, name, ordered)
    scalars = dict(base_scalars)
    scalars.update((delta or {}).get("scalars") or {})
    for name, value in scalars.items():
        _set_nested(out, str(name), value)
    return out


def dump_snapshot(payload: dict[str, Any], *, kind: str = SNAPSHOT_FULL, base: str = "") -> str:
    """序列化：全量直接存正文；diff 存差异并记下它物化自哪一条（§6 / §8）。"""
    if kind == SNAPSHOT_DELTA:
        return json.dumps({"kind": SNAPSHOT_DELTA, "base": str(base), "body": payload}, ensure_ascii=False)
    return json.dumps({"kind": SNAPSHOT_FULL, "body": payload}, ensure_ascii=False)


def snapshot_kind(payload: str | None) -> tuple[str, str]:
    """返回值形：("delta", base_id) 或 ("full", "")；老数据（没有 kind 的裸正文）当全量。"""
    data = parse_snapshot(payload)
    kind = str(data.get("kind") or SNAPSHOT_FULL)
    if kind == SNAPSHOT_DELTA:
        return SNAPSHOT_DELTA, str(data.get("base") or "")
    return SNAPSHOT_FULL, ""
