"""Writing Assistant 服务（WRITING_ASSISTANT_SPEC §3 ~ §9）：大纲、观察、候选、偏离与提交编排。

分工（§四 / §十）：

- 本层拥有：大纲定义、时间线上的达成状态、候选 / 决定、文本草稿；
- WorldRuntime 拥有：世界事实、时间、认知、版本与原子提交；
- TRPG 规则层拥有：行动、裁定与 GM 直接变化的联合提交路径；
- 本层**不拥有世界真值**：候选不是事实，草稿不是事实，GM 声明也要走既有提交边界。

阅读顺序：`outline.py`（分层约束与偏离判定）→ `candidates.py`（候选生命周期）→ 本文件（编排）。
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Iterable

from ..log import get_logger
from ..ump import Err, UmpError
from ..runtime import cognition
from . import candidates as cand
from . import outline as outline_mod

log = get_logger("isekai.writing")

#: 观察受众（§5.2 三层输出）：player 只拿玩家观察层；gm / author 另拿主持依据与下一步编排
AUDIENCES: tuple[str, ...] = ("player", "gm", "author")
#: 本层受众 → TRPG 联合提交的受众闭集（两套词表不同名，必须显式翻译）
TRPG_AUDIENCE = {"player": "public_party", "gm": "gm_only", "author": "gm_only"}
#: 玩家观察层能看到的快照投影（§5.2 第 1 层：只给该视角能获得的材料）
OBSERVE_INCLUDES: tuple[str, ...] = ("time", "current_activity", "experiences", "claims", "active_effects")


class WritingError(RuntimeError):
    """业务拒绝（形状合法但这一步不该做）：调用方按提示处理，不是系统故障。"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(str(raw or ""))
    except json.JSONDecodeError:
        return default
    return parsed if isinstance(parsed, type(default)) else default


class WritingService:
    """面向小说作者（默认）与 GM（同一套能力的一种应用方式）的编排层。"""

    def __init__(self, *, store: Any, cfg: Any = None, runtime: Any = None) -> None:
        self.store = store
        self.cfg = cfg
        self.runtime = runtime

    # ---------------------------------------------------------------- 背景读数

    def _runtime(self) -> Any:
        if self.runtime is None:
            raise UmpError(Err.INTERNAL, "运行层不可用：无法读取世界或提交变化", retryable=True)
        return self.runtime

    def _line(self, instance_id: str, timeline_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        instance = self.store.instance_get(str(instance_id or ""))
        if instance is None:
            raise UmpError(Err.NOT_FOUND, f"实例不存在：{instance_id}", retryable=False)
        timeline = self.store.timeline_get(str(timeline_id or ""))
        if timeline is None or str(timeline["instance_id"]) != str(instance_id):
            raise UmpError(Err.NOT_FOUND, f"时间线不存在：{timeline_id}", retryable=False)
        return instance, timeline

    def _world(self, instance_id: str, timeline_id: str) -> tuple[int, int]:
        row = self._runtime().clock_row(timeline_id)
        return int(row["processed_world"]), int(row["generation"])

    def _refs(self, instance_id: str, timeline_id: str) -> set[str]:
        """世界里已经能对上的引用（事件 / 说法 / 说法指向的事件）：判定只读比对，不给写权限。"""
        refs = set(self.store.event_ids(instance_id, timeline_id))
        for row in self.store.claim_list(instance_id, timeline_id):
            refs.add(str(row.get("id") or ""))
            refs.add(str(row.get("event_id") or ""))
        return {ref for ref in refs if ref}

    # ---------------------------------------------------------------- §三 / §4.1 大纲定义

    def save_outline(self, outline: dict[str, Any]) -> dict[str, Any]:
        """保存大纲定义（作者资产，可跨时间线复用）：**未通过校验不得落盘**。"""
        normalized = outline_mod.normalize_outline(outline)
        errors = outline_mod.validate_outline(normalized)
        if errors:
            raise UmpError(Err.INVALID, "大纲未通过校验，未写入：" + "；".join(errors[:6]), retryable=False)
        saved = self.store.wa_outline_put({
            "id": normalized["id"],
            "name": normalized["name"],
            "payload": _dumps(normalized),
        })
        return {"outline": normalized, "errors": [], "saved_real": float(saved.get("updated_real") or 0)}

    def outline(self, outline_id: str) -> dict[str, Any]:
        row = self.store.wa_outline_get(str(outline_id))
        if row is None:
            raise UmpError(Err.NOT_FOUND, f"没有该大纲：{outline_id}", retryable=False)
        return self._definition(row)

    def outlines(self) -> list[dict[str, Any]]:
        return [
            {"id": str(row["id"]), "name": str(row["name"]),
             "items": len(_loads(row["payload"], {}).get("items") or []),
             "updated_real": float(row.get("updated_real") or 0)}
            for row in self.store.wa_outline_list()
        ]

    def _definition(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = _loads(row.get("payload"), {})
        payload["id"] = str(payload.get("id") or row.get("id") or "")
        payload["name"] = str(payload.get("name") or row.get("name") or "")
        return outline_mod.normalize_outline(payload)

    # ---------------------------------------------------------------- §4.1 绑定与状态

    def bind(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        outline_id: str,
        observers: Iterable[str] = (),
        chapter: str = "",
    ) -> dict[str, Any]:
        """把大纲绑到实例 + 时间线（并记观察视角）。

        首次绑定：条目按定义的结构来，状态一律从「未开始」起——模板里写着的达成状态
        不能随绑定混进进度（§11.1）。已经绑定过就是**改元数据**（观察者 / 章节）：
        各线的进度原样保留，不清零。
        """
        definition = self.outline(outline_id)
        self._line(instance_id, timeline_id)
        world, generation = self._world(instance_id, timeline_id)
        existing = self.store.wa_state_get(instance_id, timeline_id, definition["id"])
        if existing is not None:
            saved = self.store.wa_state_put({
                **existing,
                "observers": _dumps([str(name) for name in observers]),
                "chapter": str(chapter or ""),
            })
            return {
                "state": self._state_payload(saved, definition),
                "updated": True,
                "note": "只改了观察视角 / 章节：这条线上的进度原样保留",
            }
        items = [
            outline_mod.normalize_item(
                {**item, "evaluated_world": world, "status": "unstarted", "reason": "", "evidence_refs": []}
            )
            for item in definition["items"]
        ]
        row = self.store.wa_state_put({
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "outline_id": definition["id"],
            "observers": _dumps([str(name) for name in observers]),
            "chapter": str(chapter or ""),
            "items": outline_mod.dump_items(items),
            "evaluated_world": world,
            "evaluated_generation": generation,
        })
        return {"state": self._state_payload(row, definition)}

    def state(self, instance_id: str, timeline_id: str, *, outline_id: str = "") -> dict[str, Any]:
        row = self._state_row(instance_id, timeline_id, outline_id)
        if row is None:
            # 没绑定时如实说空：不静默用定义冒充状态（达成状态是「这条线上的事」）
            raise UmpError(Err.NOT_FOUND, "这条时间线上还没有绑定大纲", retryable=False)
        definition = self._definition(self.store.wa_outline_get(str(row["outline_id"])) or {})
        return self._state_payload(row, definition)

    def _state_row(self, instance_id: str, timeline_id: str, outline_id: str) -> dict[str, Any] | None:
        if outline_id:
            return self.store.wa_state_get(instance_id, timeline_id, outline_id)
        rows = self.store.wa_state_list(instance_id, timeline_id)
        return rows[0] if rows else None

    def _state_payload(self, row: dict[str, Any], definition: dict[str, Any]) -> dict[str, Any]:
        return {
            "instance_id": str(row.get("instance_id") or ""),
            "timeline_id": str(row.get("timeline_id") or ""),
            "outline_id": str(row.get("outline_id") or ""),
            "outline_name": str(definition.get("name") or ""),
            "observers": _loads(row.get("observers"), []),
            "chapter": str(row.get("chapter") or ""),
            "items": self._merged_items(definition, row),
            "evaluated_world": int(row.get("evaluated_world") or 0),
            "evaluated_generation": int(row.get("evaluated_generation") or 0),
        }

    def _merged_items(self, definition: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
        """定义给结构（层级 / 范围 / 判据），状态给进度（达成 / 偏离 / 依据）。"""
        progress = {str(item["id"]): item for item in outline_mod.load_items(row.get("items"))}
        merged: list[dict[str, Any]] = []
        for item in definition["items"]:
            saved = progress.get(item["id"])
            merged.append(outline_mod.normalize_item({**item, **({k: v for k, v in saved.items()
                                                                     if k in ("status", "reason", "evidence_refs",
                                                                              "evaluated_world")} if saved else {})}))
        return merged

    def evaluate(self, instance_id: str, timeline_id: str, *, outline_id: str = "") -> dict[str, Any]:
        """只读评估（§七 / §十二 3·7·9）：报告依据、缺口与偏离，**不自动改状态、不写世界**。

        评估会记下这次用的水位（§4.2 的「最近一次评估所依据的世界快照」），回滚之后
        同一个条目会重算出「依据没了」，等创作者决定——不静默把偏离重解释成达成（§十-6）。
        """
        row = self._state_row(instance_id, timeline_id, outline_id)
        if row is None:
            raise UmpError(Err.NOT_FOUND, "这条时间线上还没有绑定大纲", retryable=False)
        definition = self._definition(self.store.wa_outline_get(str(row["outline_id"])) or {})
        world, generation = self._world(instance_id, timeline_id)
        items = self._merged_items(definition, row)
        report = outline_mod.evaluate(items, refs=self._refs(instance_id, timeline_id), world_time=world)
        stamped = [outline_mod.normalize_item({**item, "evaluated_world": world}) for item in items]
        self.store.wa_state_put({**row, "items": outline_mod.dump_items(stamped),
                                 "evaluated_world": world, "evaluated_generation": generation})
        return {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "outline_id": str(row["outline_id"]),
            "observed_revision": world,
            "evaluated_world": world,
            "runtime_generation": generation,
            "items": stamped,
            "evidence": report["evidence"],
            "gaps": report["gaps"],
            "deviations": report["deviations"],
            # §十二 第 3 行：报告缺口或提出候选，不自动制造世界事实、不把候选标成已达成
            "must_not_imply": "候选已经成了世界事实",
        }

    def decide_item(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        item_id: str,
        status: str,
        reason: str,
        evidence_refs: Iterable[str] = (),
        outline_id: str = "",
    ) -> dict[str, Any]:
        """创作者决定（§4.2 四类推动力里唯一一句话能定的一种）：状态迁移 + 理由 + 依据。

        硬约束标记达成时必须带得住在世界里对得上的依据——规则成功但世界提交失败不算达成（§十二 7）。
        """
        row = self._state_row(instance_id, timeline_id, outline_id)
        if row is None:
            raise UmpError(Err.NOT_FOUND, "这条时间线上还没有绑定大纲", retryable=False)
        definition = self._definition(self.store.wa_outline_get(str(row["outline_id"])) or {})
        items = self._merged_items(definition, row)
        target = [item for item in items if item["id"] == str(item_id)]
        if not target:
            raise UmpError(Err.NOT_FOUND, f"大纲里没有这条条目：{item_id}", retryable=False)
        if not str(reason or "").strip():
            raise UmpError(Err.INVALID, "改条目状态必须给出理由（偏离可以被接受，不能被静默掩盖）", retryable=False)
        item = target[0]
        try:
            outline_mod.transition(item["status"], status)
        except ValueError as exc:
            raise WritingError(str(exc)) from exc
        if item["layer"] == "forbidden" and status == "achieved":
            raise WritingError("禁止事项触发就是偏离：把它标成偏离并给出决定，不能标成达成")
        refs = self._refs(instance_id, timeline_id)
        resolved = {str(name) for name in evidence_refs}
        unknown = sorted(resolved - refs)
        if unknown:
            raise WritingError(
                "依据在世界里对不上（规则成功但世界提交失败不算达成）：" + "、".join(unknown[:5])
            )
        merged = outline_mod.normalize_item({
            **item, "status": status, "reason": str(reason), "evidence_refs": sorted(resolved),
        })
        if str(status) == "achieved" and not outline_mod.evidence_ok(merged, refs):
            # 只对「标记达成」施加依据要求（§11.1）：开始 / 偏离 / 放弃不需要世界依据，
            # 但达成必须带得住——规则成功 ≠ 世界已改。
            raise WritingError("硬约束标记达成需要可追溯的世界依据（事件 / 说法标识）：先提交或给出依据")
        world, generation = self._world(instance_id, timeline_id)
        updated = [merged if entry["id"] == item["id"] else entry for entry in items]
        saved = self.store.wa_state_put({**row, "items": outline_mod.dump_items(updated),
                                        "evaluated_world": world, "evaluated_generation": generation})
        return {"state": self._state_payload(saved, definition), "item": merged,
                "must_not_imply": "草稿或模型判断已经让这条达成了"}

    # ---------------------------------------------------------------- §5.2 观察（只读投影）

    def observe(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        observer_id: str,
        outline_id: str = "",
        audience: str = "author",
    ) -> dict[str, Any]:
        """三层输出（§5.2）：玩家观察层 / 主持依据层（默认只给 GM 与作者）/ 下一步编排层。"""
        mode = str(audience or "author")
        if mode not in AUDIENCES:
            raise UmpError(Err.INVALID, f"未知受众：{mode}（只接受 {'/'.join(AUDIENCES)}）", retryable=False)
        runtime = self._runtime()
        self._line(instance_id, timeline_id)
        snapshot = runtime.read_snapshot(
            instance_id, timeline_id,
            request={"characters": [str(observer_id)], "include": list(OBSERVE_INCLUDES)},
        )
        if str(snapshot.get("status")) != "ok":
            # 追赶中 / 冻结 / 归档：如实说不可用，不拿旧状态冒充当前（§4.2 快照规则）
            return {"status": str(snapshot.get("status") or "not_ready"), "audience": mode,
                    "observer_id": str(observer_id), "reason": str(snapshot.get("reason") or ""),
                    "player_view": {}, "next_step": {"candidates": [], "gaps": []}}
        projection = runtime.cognition_project(
            instance_id, timeline_id,
            observer_id=str(observer_id),
            query={"purpose": "player_observation" if mode == "player" else "narrative_candidate"},
        )
        view = (snapshot.get("payload") or {}).get(str(observer_id), {})
        materials: list[dict[str, Any]] = []
        try:
            instance = self.store.instance_get(instance_id) or {}
            package = self._runtime().setting(instance).get("world_package") or {}
            card = self._runtime().card_of(instance, str(observer_id), timeline_id=timeline_id,
                                           world_seconds=int(snapshot.get("world_time") or 0))
            materials = cognition.knowledge_slice(
                package, card,
                world_seconds=int(snapshot.get("world_time") or 0),
                experiences=view.get("experiences") or [],
                knowledge=view.get("claims") or [],
                topic="",
                limit=24,
            )
        except Exception as exc:  # 认知层缺料不该让观察整体失败，但也不许悄悄当成功
            log.warning("观察层的角色材料不可用：%s", exc)
        payload: dict[str, Any] = {
            "status": "ok",
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "observer_id": str(observer_id),
            "audience": mode,
            "observed_revision": int(snapshot.get("revision") or 0),
            "world_time": int(snapshot.get("world_time") or 0),
            "player_view": {
                "current_activity": view.get("current_activity", ""),
                "experiences": view.get("experiences", []),
                "claims": view.get("claims", []),
                # 素材只从认知层唯一的底层入口取（§5.2 第 1 层）：带上来源与态度标签，
                # 未获知的说法、他人的私密内容、规则状态正文都不会出现在这里
                "materials": materials,
                "observations": projection.get("observations", []),
                "known_unknowns": projection.get("known_unknowns", []),
            },
            "next_step": {"candidates": [], "gaps": []},
            "must_not_imply": "候选已经发生",
        }
        try:
            state = self.state(instance_id, timeline_id, outline_id=outline_id)
            report = self.evaluate(instance_id, timeline_id, outline_id=outline_id)
        except UmpError:
            state, report = None, {"gaps": [], "deviations": [], "evidence": []}
        rows = self.store.wa_candidate_list(instance_id, timeline_id, outline_id=outline_id or None)
        opened = [
            cand.public_candidate(item)
            for item in rows
            if str(item.get("status")) in cand.UNCOMMITTED and cand.visible_to(item, mode)
        ]
        if mode == "player":
            # 玩家受众（§5.2 第 1 层 / §11.1）：候选只留「这条建议是什么」，依据与世界变化意图
            # 都属主持材料；大纲缺口与偏离是作者的编排依据，也一并裁掉——界面不靠隐藏控件挡。
            payload["next_step"] = {
                "candidates": [
                    cand.player_candidate(item)
                    for item in rows
                    if str(item.get("status")) in cand.UNCOMMITTED and cand.visible_to(item, "player")
                ],
                "gaps": [],
                "deviations": [],
                "withheld": ["主持依据", "大纲缺口与偏离", "世界变化意图"],
            }
        else:
            payload["next_step"] = {"candidates": opened, "gaps": report["gaps"], "deviations": report["deviations"]}
        if mode != "player":
            # 主持依据层：默认只给 GM / 作者（§5.2 第 2 层）
            payload["gm_basis"] = {
                "outline_items": [
                    {"id": item["id"], "layer": item["layer"], "strength": item["strength"],
                     "status": item["status"], "statement": item["statement"]}
                    for item in (state or {}).get("items", [])
                ],
                "evidence": report["evidence"],
                "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            }
        return payload

    # ---------------------------------------------------------------- §4.3 候选生命周期

    def propose(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        candidate_id: str,
        kind: str = "world_change",
        item_refs: Iterable[str] = (),
        title: str = "",
        summary: str = "",
        basis: dict[str, Any] | None = None,
        audience: str = "author",
        changes: Iterable[dict[str, Any]] = (),
        unsolved: Iterable[str] = (),
        outline_id: str = "",
        text: str = "",
    ) -> dict[str, Any]:
        """提一条候选（§4.3）：固定快照 + 依据 + 受众 + 来源水位 + 可能变化 + 未解决问题。

        带世界变化的候选先过 `runtime.change.preview`：翻不成事实效果的如实进 rejected / needs_review，
        并给出需要改的依据——不硬塞近似效果，也不把候选说成已经发生。
        """
        if not str(candidate_id or "").strip():
            raise UmpError(Err.INVALID, "候选必须有稳定标识", retryable=False)
        if str(kind) not in cand.KINDS:
            raise UmpError(Err.INVALID, f"未知候选类型：{kind}", retryable=False)
        runtime = self._runtime()
        self._line(instance_id, timeline_id)
        row = self._state_row(instance_id, timeline_id, outline_id)
        definition = self._definition(self.store.wa_outline_get(str(row["outline_id"])) or {}) if row else {"items": []}
        known = {item["id"] for item in definition["items"]}
        refs = [str(name) for name in item_refs]
        unknown = [name for name in refs if name not in known]
        if unknown:
            raise UmpError(Err.INVALID, "候选引用了不在大纲里的条目：" + "、".join(unknown), retryable=False)
        world, generation = self._world(instance_id, timeline_id)
        change_list = [item for item in changes if isinstance(item, dict)]
        status, reason, preview_id = "proposed", "", ""
        if change_list:
            preview = runtime.change_preview(
                instance_id, timeline_id, changes=change_list, expected_revision=world
            )
            preview_id = str(preview.get("preview_id") or "")
            rejected = list(preview.get("rejected_candidates") or [])
            pending = list(preview.get("needs_review") or [])
            conflicts = list(preview.get("conflicts") or [])
            # 通配拒绝（`*`：这批意图没有能落成事实效果的内容）不是「这条候选不成立」——
            # 它等于「暂时还没有可提交的事实效果」，属于待确认（§4.3），只有指名条目的
            # 拒绝（目标不合法、效果越界）才算候选被驳回。
            named = [item for item in rejected if str(item.get("id") or "") != "*"]
            if named or conflicts:
                status = "rejected"
                reason = "；".join(
                    [f"{item.get('id')}：{item.get('reason')}" for item in named]
                    + [f"版本冲突：{item.get('kind')}" for item in conflicts]
                )
            elif pending or rejected:
                status = "proposed"
                reason = "需要确认后才能提交：" + "；".join(
                    str(item.get("reason") or "") for item in (pending or rejected)
                )
        existing = self.store.wa_candidate_get(instance_id, timeline_id, str(candidate_id))
        if existing is not None and float(existing.get("locked_at") or 0) > 0:
            # 锁定的正文（§7.5）：新生成永远另起一稿，不能覆盖这一份
            raise WritingError(
                f"候选 {candidate_id} 的正文已锁定：新生成会另起一稿（要改先解锁，或复制为新稿）"
            )
        if existing is not None and str(existing.get("status")) in ("approved", "committed"):
            # 已采用的产物不许被同名提案覆盖（§11.1）：重新提案要给新标识，旧的照旧在
            raise WritingError(
                f"候选 {candidate_id} 已经{'进世界' if str(existing.get('status')) == 'committed' else '被采用'}："
                "重新提案要用新标识（这条不会被改写）"
            )
        saved = self.store.wa_candidate_put({
            "instance_id": instance_id, "timeline_id": timeline_id, "id": str(candidate_id),
            "outline_id": str(row["outline_id"]) if row else "", "kind": str(kind),
            "item_refs": _dumps(refs), "title": str(title), "summary": str(summary),
            "basis": _dumps({
                "fact": str((basis or {}).get("fact") or ""),
                "causality": str((basis or {}).get("causality") or ""),
                "outline": str((basis or {}).get("outline") or ""),
            }),
            "audience": str(audience), "base_world": world, "base_generation": generation,
            "changes": _dumps(change_list), "unsolved": _dumps([str(name) for name in unsolved]),
            "text": str(text), "status": status, "reason": reason, "preview_id": preview_id,
        })
        return {"candidate": cand.public_candidate(saved), "status": status, "reason": reason}

    def decide(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        candidate_id: str,
        status: str,
        reason: str,
        text: str = "",
    ) -> dict[str, Any]:
        """创作者选择（§4.3）：`approved` 只表示同意采用，**不表示世界已经改变**。"""
        row = self.store.wa_candidate_get(instance_id, timeline_id, candidate_id)
        if row is None:
            raise UmpError(Err.NOT_FOUND, f"没有该候选：{candidate_id}", retryable=False)
        if not str(reason or "").strip():
            raise UmpError(Err.INVALID, "候选决定必须给出理由", retryable=False)
        if str(status) == "committed":
            # 「已生效」只能由真实提交结果产生（§11.1）：这里说清该走哪条路，不静默放行
            raise WritingError(
                "不能靠状态决定把候选标成已生效：世界变化候选走 wa.candidate.commit，"
                "GM 直接变化走 wa.gm.approve"
            )
        try:
            cand.transition(str(row["status"]), status)
        except ValueError as exc:
            raise WritingError(str(exc)) from exc
        body = str(text or "")
        if body and str(row["kind"]) != "text":
            # 文本候选才锁草稿；世界变化候选不靠一段散文变成事实
            raise WritingError("只有文本候选能锁定草稿文本")
        if str(status) == "approved" and str(row["kind"]) == "text" and not body:
            body = str(row.get("text") or "") or str(row.get("summary") or "")
        saved = self.store.wa_candidate_put({**row, "status": str(status), "reason": str(reason), "text": body})
        return {"candidate": cand.public_candidate(saved),
                "must_not_imply": "批准就等于世界已经改变"}

    def lock_text(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        candidate_id: str,
        locked: bool = True,
    ) -> dict[str, Any]:
        """锁定 / 解锁一份正文（§7.5）：先保存成功再锁定；解锁只针对这一份。

        锁定后：同名提案被拒、新生成拿到的是另一稿；世界恢复也不会改写它（正文不随回滚消失）。
        """
        row = self.store.wa_candidate_get(instance_id, timeline_id, candidate_id)
        if row is None:
            raise UmpError(Err.NOT_FOUND, f"没有该候选：{candidate_id}", retryable=False)
        if str(row.get("kind")) != "text":
            raise WritingError("只有文字草稿能锁定：世界变化候选不靠一段散文变成事实")
        item = cand.normalize_candidate(row)
        if locked:
            if item["status"] != "approved":
                raise WritingError("先保存这段文字（采用它）再锁定：锁定只对已保存的稿子生效")
            if not item["text"].strip():
                raise WritingError("这份稿子还是空的：先写下正文再锁定")
        saved = self.store.wa_candidate_put({**row, "locked_at": time.time() if locked else 0.0})
        return {"candidate": cand.public_candidate(saved),
                "must_not_imply": "锁定等于世界已经改变" if not locked else "锁定会阻止你以后修改（要先解锁）"}

    def commit(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        candidate_id: str,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """把已批准的**世界变化候选**提交给 WorldRuntime（§4.3 / §十二 10）。

        写入前先 `generation.check`：过期 / 冲突的候选不写回旧世界；提交仍由
        `runtime.change.commit` 唯一入口完成——本层不自己落事实。
        """
        row = self.store.wa_candidate_get(instance_id, timeline_id, candidate_id)
        if row is None:
            raise UmpError(Err.NOT_FOUND, f"没有该候选：{candidate_id}", retryable=False)
        item = cand.normalize_candidate(row)
        if item["status"] != "approved":
            raise WritingError("只有已批准的候选才能提交（approved 只表示同意采用，提交是另一步）")
        changes = item["changes"]
        if not changes:
            raise WritingError("这条候选没有世界变化：文本草稿不进世界，走导出或锁定即可")
        runtime = self._runtime()
        key = str(idempotency_key or "").strip() or f"wa-{item['id']}"
        check = runtime.generation_check(
            instance_id, timeline_id,
            snapshot_id=f"snap-{item['base_world']}",
            runtime_generation=item["base_generation"],
            source_refs=[str(name) for name in item["item_refs"]],
        )
        if str(check.get("status")) != "valid":
            saved = self.store.wa_candidate_put({
                **row, "status": "stale",
                "reason": f"写入前检查不通过：{check.get('status')}（{check.get('reason') or ''}）",
            })
            return {"status": str(check.get("status")), "candidate": cand.public_candidate(saved),
                    "reason": str(check.get("reason") or "候选来自旧世代 / 旧水位，不写回")}
        # 世代没过期、但水位可能已经往前走了：按**当前**世界重新预览一次（§八「重新表达为当前线上的
        # 新变化并再次经过 preview / commit」）。预览标识绑基准版本，拿旧标识去提交必然 conflict——
        # 与其把「世界动了几秒」当成失败，不如让这条意图重新过一遍校验再落。
        world_now = int(runtime.clock_row(timeline_id)["processed_world"])
        fresh = runtime.change_preview(
            instance_id, timeline_id, changes=changes, expected_revision=world_now
        )
        named = [item_ for item_ in (fresh.get("rejected_candidates") or [])
                 if str(item_.get("id") or "") != "*"]
        if named or list(fresh.get("conflicts") or []):
            saved = self.store.wa_candidate_put({
                **row, "status": "rejected",
                "reason": "提交前重新预览没过：" + "；".join(
                    [f"{item_.get('id')}：{item_.get('reason')}" for item_ in named]
                    + [f"版本冲突：{item_.get('kind')}" for item_ in list(fresh.get("conflicts") or [])]
                ),
            })
            return {"status": "rejected", "candidate": cand.public_candidate(saved),
                    "reason": str(saved["reason"]),
                    "rejected_candidates": named, "conflicts": list(fresh.get("conflicts") or [])}
        refreshed = "" if int(row["base_world"]) == world_now else f"提交前按当前水位重新预览（{row['base_world']} → {world_now}）"
        result = runtime.change_commit(
            instance_id, timeline_id, changes=changes, idempotency_key=key,
            preview_id=str(fresh.get("preview_id") or item["preview_id"]),
            source_module="writing_assistant",
        )
        verdict = str(result.get("status") or "")
        if verdict in ("ok", "duplicate"):
            saved = self.store.wa_candidate_put({
                **row, "status": "committed",
                "reason": f"已提交（{verdict}）" + (f"；{refreshed}" if refreshed else ""),
                "joint_commit_id": str(result.get("commit_id") or result.get("new_revision") or ""),
            })
            return {"status": verdict, "candidate": cand.public_candidate(saved),
                    "commit_id": str(result.get("commit_id") or ""),
                    "new_revision": int(result.get("new_revision") or 0),
                    "event_refs": list(result.get("event_refs") or []),
                    "must_not_imply": "提交成功也自动生成了合格散文"}
        # 冲突 / 非法 / 需要确认：留在 approved，如实回报，不谎报成功
        saved = self.store.wa_candidate_put({
            **row, "status": "stale" if verdict == "conflict" else str(row["status"]),
            "reason": f"提交被拒：{verdict}（{result.get('reason') or ''}）",
        })
        return {"status": verdict, "candidate": cand.public_candidate(saved),
                "reason": str(result.get("reason") or ""),
                "errors": list(result.get("errors") or []),
                "rejected_candidates": list(result.get("rejected_candidates") or [])}

    # ---------------------------------------------------------------- §5 / §九 GM 辅助

    def gm_declare(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        candidate_id: str,
        campaign_id: str,
        gm_changes: dict[str, Any],
        title: str = "",
        basis: dict[str, Any] | None = None,
        audience: str = "gm",
    ) -> dict[str, Any]:
        """GM 直接变化声明（§5.1）：本层只形成**待批准的结构**，不写世界。

        实际提交走 TRPG 规则层的 GM 直接变化路径（`gm_approve` → 联合提交），
        与规则裁定共用同一套后果校验。
        """
        if not str(campaign_id or "").strip():
            raise UmpError(Err.INVALID, "GM 直接变化要指明战役（提交走 TRPG 规则层）", retryable=False)
        if not isinstance(gm_changes, dict) or not gm_changes:
            raise UmpError(Err.INVALID, "GM 直接变化必须给出结构化 changes（不是一句自由文本）", retryable=False)
        self._line(instance_id, timeline_id)
        world, generation = self._world(instance_id, timeline_id)
        saved = self.store.wa_candidate_put({
            "instance_id": instance_id, "timeline_id": timeline_id, "id": str(candidate_id),
            "kind": "world_change", "item_refs": "[]", "title": str(title), "summary": "GM 直接变化声明",
            "basis": _dumps({"fact": str((basis or {}).get("fact") or ""),
                             "causality": str((basis or {}).get("causality") or ""),
                             "outline": str((basis or {}).get("outline") or "")}),
            "audience": str(audience), "base_world": world, "base_generation": generation,
            "gm_changes": _dumps(gm_changes), "campaign_id": str(campaign_id),
            "source_mode": "gm_declaration", "status": "proposed",
            "reason": "待 GM 批准后经 TRPG GM 变化路径提交",
        })
        return {"candidate": cand.public_candidate(saved),
                "must_not_imply": "声明本身已经是世界事实"}

    def gm_approve(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        candidate_id: str,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """GM 明确批准并提交（§九）：经 Campaign Runtime 的 `gm_change` 联合提交边界。"""
        row = self.store.wa_candidate_get(instance_id, timeline_id, candidate_id)
        if row is None:
            raise UmpError(Err.NOT_FOUND, f"没有该候选：{candidate_id}", retryable=False)
        item = cand.normalize_candidate(row)
        if item["source_mode"] != "gm_declaration":
            raise WritingError("这不是 GM 直接变化声明：普通候选走 wa.candidate.commit")
        try:
            cand.transition(item["status"], "approved")
        except ValueError as exc:
            raise WritingError(str(exc)) from exc
        approved = self.store.wa_candidate_put({**row, "status": "approved", "reason": "GM 已批准"})
        campaign = getattr(self._runtime(), "campaign", None)
        if campaign is None:
            raise WritingError("TRPG 战役运行时未挂载：GM 直接变化没有提交路径")
        try:
            result = campaign.gm_change(
                instance_id, timeline_id, item["campaign_id"],
                changes=item["gm_changes"],
                idempotency_key=str(idempotency_key or "").strip() or f"wa-gm-{item['id']}",
                audience=TRPG_AUDIENCE.get(str(item["audience"] or "gm"), "gm_only"),
                source="gm_declaration",
            )
        except Exception as exc:  # 提交失败不谎报成功：留在 approved
            saved = self.store.wa_candidate_put({**approved, "reason": f"提交失败：{type(exc).__name__}"})
            return {"status": "failed", "candidate": cand.public_candidate(saved),
                    "reason": f"{type(exc).__name__}: {exc}"}
        verdict = str(result.get("status") or "")
        if verdict in ("ok", "committed", "duplicate"):
            saved = self.store.wa_candidate_put({
                **approved, "status": "committed", "reason": f"GM 直接变化已提交（{verdict}）",
                "joint_commit_id": str(result.get("joint_commit_id") or ""),
            })
            return {"status": verdict, "candidate": cand.public_candidate(saved),
                    "joint_commit_id": str(result.get("joint_commit_id") or ""),
                    "event_refs": list(result.get("event_refs") or [])}
        saved = self.store.wa_candidate_put({**approved, "reason": f"提交未通过：{verdict}"})
        return {"status": verdict, "candidate": cand.public_candidate(saved), "result": result}

    # ---------------------------------------------------------------- §八 分支试演

    def trial_branch(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        commit_id: str,
        name: str = "",
        outline_id: str = "",
    ) -> dict[str, Any]:
        """试演候选优先走新分支（§八）：主线不被污染，项目不提供世界线合并。"""
        runtime = self._runtime()
        self._line(instance_id, timeline_id)
        if not str(commit_id or "").strip():
            raise UmpError(Err.INVALID, "缺少 commit_id", retryable=False)
        result = runtime.fork(instance_id, timeline_id, commit_id=commit_id, name=name)
        line = result.get("timeline") or {}
        new_id = str(line.get("id") or "")
        bound = None
        row = self._state_row(instance_id, timeline_id, outline_id)
        if new_id and row is not None:
            # 大纲定义跨线复用，达成与偏离各自独立：新线从「未开始」重新记
            bound = self.bind(
                instance_id, new_id,
                outline_id=str(row["outline_id"]),
                observers=_loads(row.get("observers"), []),
                chapter=str(row.get("chapter") or ""),
            )
        return {
            "status": "ok",
            "timeline": {"id": new_id, "name": str(line.get("name") or ""), "state": str(line.get("state") or "")},
            "source_commit": str(commit_id),
            "state": (bound or {}).get("state"),
            "note": "分支继承共同过去；主线不被污染，项目不提供世界线合并",
            "must_not_imply": "两条线可以合并回去",
        }

    # ---------------------------------------------------------------- §六 模型提议（W3）

    #: 判断点标识：与 llm.JUDGEMENT_MARK 同款——测试替身据此把这类调用与回复生成分开
    SUGGEST_MARK = "【判断点】情节提议"

    async def suggest(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        outline_id: str = "",
        observer_id: str = "",
        goal: str = "",
        limit: int = 3,
        llm: Any = None,
        prefix: str = "cand",
    ) -> dict[str, Any]:
        """让系统提出多条情节推进 / 冲突后果（§六）：提议是**候选**，不写世界、不自动达成条目。

        一次便宜调用（`wa_suggest` 档），失败返回空提议并说明——不编造候选。
        """
        row = self._state_row(instance_id, timeline_id, outline_id)
        definition = self._definition(self.store.wa_outline_get(str(row["outline_id"])) or {}) if row else {"items": []}
        items = self._merged_items(definition, row) if row else []
        report = outline_mod.evaluate(items, refs=self._refs(instance_id, timeline_id),
                                      world_time=self._world(instance_id, timeline_id)[0]) if items else {
            "gaps": [], "evidence": [], "deviations": []}
        material = ""
        if observer_id:
            observed = self.observe(instance_id, timeline_id, observer_id=str(observer_id),
                                    outline_id=outline_id, audience="author")
            view = observed.get("player_view") or {}
            lines = [f"- （她的经历）{item.get('summary') or item}" for item in view.get("experiences") or []]
            lines += [f"- （她听说的）{item.get('text') or item}" for item in view.get("claims") or []]
            material = "\n".join(lines[:12]) or "（暂时没有可用的角色材料）"
        gaps = "\n".join(f"- {item['item_id']}：{item['detail']}" for item in report["gaps"][:6]) or "（没有硬约束缺口）"
        prompt = [
            {"role": "system", "content": (
                f"{self.SUGGEST_MARK}\n"
                "你在给一部持续运行的作品提情节候选，只输出 JSON。\n"
                "候选必须是**未发生的建议**，不许把任何一条当成已经发生；不许编造上面没给过的事实。\n"
                f'输出：{{"candidates": [{{"title": "短标题", "summary": "一到两句话的推进", '
                f'"outline_ref": "对应的条目 id 或空串", "unsolved": ["还没定的点"], '
                f'"changes": [{{"kind": "变化类别", "operation": "动作", "target_refs": ["对象 id 或空串"], '
                f'"value": "变化后的值"}}]}}]}}\n'
                "changes 只在这一条推进需要改变世界时才给（不需要就留空数组）：它只是候选，"
                "能不能落成事实由核心校验，写不进去会被如实标出来。\n"
                f"最多 {max(1, int(limit))} 条。"
            )},
            {"role": "user", "content": (
                f"本章目标：{str(goal or '（未指定）')}\n"
                f"大纲缺口：\n{gaps}\n"
                f"可用角色材料：\n{material or '（未指定观察视角）'}"
            )},
        ]
        prompt_text = "\n".join(str(item.get("content") or "") for item in prompt)
        reservation = self._reserve_suggest(instance_id, timeline_id, prompt_text)
        if reservation is False:  # 预算拒绝：不调用、不编造
            return {"status": "paused", "candidates": [], "reason": "调用预算已拒绝本次提议"}
        raw = ""
        try:
            raw = await llm.chat(prompt, temperature=0.8, timeout=45.0)
        except Exception as exc:
            self._settle_suggest(reservation, prompt_text=prompt_text, reply="", outcome="error")
            return {"status": "failed", "candidates": [], "reason": f"{type(exc).__name__}: {exc}"}
        self._settle_suggest(reservation, prompt_text=prompt_text, reply=str(raw or ""))
        proposals, parse_error = self._parse_suggestions(raw)
        if parse_error:
            # 「没有建议」和「没拿到可用输出」是两件事（§11.1）：如实说哪一种，
            # 带上模型原文的短摘要（单行、截断），让人能排错而不是看着空清单猜。
            excerpt = " ".join(str(raw or "").split())[:160]
            return {
                "status": "unparsable",
                "candidates": [],
                "reason": f"模型输出没法解析成候选列表：{parse_error}",
                "raw_excerpt": excerpt,
            }
        world, generation = self._world(instance_id, timeline_id)
        # 每次生成独立身份（§11.1）：这一轮的标识带本次批次号，不覆盖上一轮（尤其已采用的）候选
        batch = secrets.token_hex(3)
        created: list[dict[str, Any]] = []
        for index, item in enumerate(proposals[: max(1, int(limit))]):
            known = {entry["id"] for entry in items}
            ref = str(item.get("outline_ref") or "")
            saved = self.store.wa_candidate_put({
                "instance_id": instance_id, "timeline_id": timeline_id,
                "id": f"{prefix}-{batch}-{index + 1}", "outline_id": str(row["outline_id"]) if row else "",
                "kind": "scene", "item_refs": _dumps([ref] if ref in known else []),
                "title": str(item.get("title") or "")[:80], "summary": str(item.get("summary") or ""),
                "basis": _dumps({"fact": "", "causality": material[:200], "outline": ref}),
                "audience": "author", "base_world": world, "base_generation": generation,
                "unsolved": _dumps([str(name) for name in item.get("unsolved") or []]),
                "status": "proposed", "reason": "模型提议，未经创作者选择",
            })
            raw_changes = [change for change in (item.get("changes") or []) if isinstance(change, dict)]
            public = cand.public_candidate(saved)
            if raw_changes:
                # 改世界不是「多写一段散文」：这条候选要按正式路径过一遍校验（§7.3）。
                # propose 返回的是公开面（含被驳回的原因），直接用它，不让界面自己猜。
                try:
                    outcome = self.propose(
                        instance_id, timeline_id, candidate_id=str(saved.get("id") or ""),
                        kind="scene", outline_id=str(row["outline_id"]) if row else "",
                        item_refs=[ref] if ref in known else [],
                        title=str(item.get("title") or "")[:80], summary=str(item.get("summary") or ""),
                        basis={"fact": "", "causality": material[:200], "outline": ref},
                        unsolved=[str(name) for name in item.get("unsolved") or []],
                        changes=raw_changes,
                    )
                    public = outcome.get("candidate") or public
                except Exception:  # noqa: BLE001
                    log.exception("suggest change propose failed")
            created.append(public)
        if not created:
            # 解析成功但一条可用候选都没有：这是「没有建议」，不是失败
            return {"status": "ok", "candidates": [],
                    "reason": "模型这一轮没有给出可用的候选",
                    "must_not_imply": "这些提议已经发生或已被采用"}
        return {"status": "ok", "candidates": created,
                "must_not_imply": "这些提议已经发生或已被采用"}

    def _reserve_suggest(self, instance_id: str, timeline_id: str, prompt_text: str) -> Any:
        runtime = self.runtime
        if runtime is None:
            return None
        try:
            reservation = runtime.reserve_call(instance_id, timeline_id, "wa_suggest", prompt_text=prompt_text)
        except Exception:
            log.exception("wa suggest reservation failed")
            return None
        if not reservation.get("ok"):
            log.info("wa suggest skipped instance=%s blocked=%s", instance_id, reservation.get("blocked"))
            return False
        return reservation

    def _settle_suggest(self, reservation: Any, *, prompt_text: str, reply: str, outcome: str = "ok") -> None:
        if not reservation or reservation is False or self.runtime is None:
            return
        try:
            self.runtime.settle_call(reservation, prompt_text=prompt_text, reply=reply, outcome=outcome)
        except Exception:
            log.exception("wa suggest settle failed")

    @staticmethod
    def _parse_suggestions(raw: Any) -> tuple[list[dict[str, Any]], str]:
        """解析提议。返回 (候选, 失败原因)。

        失败原因非空 = 「没拿到可用输出」（空回复 / 不是 JSON / 不是那个形状）；
        原因为空且候选为空 = 「模型这一轮没有建议」。两件事都要如实分开报（§11.1）。
        """
        body = str(raw or "").strip()
        if not body:
            return [], "模型返回了空内容"
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            return [], "输出里没有 JSON 对象"
        try:
            payload = json.loads(body[start : end + 1])
        except json.JSONDecodeError:
            return [], "JSON 解析失败"
        if not isinstance(payload, dict) or "candidates" not in payload:
            return [], "JSON 里没有 candidates 字段"
        items = payload.get("candidates")
        if not isinstance(items, list):
            return [], "candidates 不是列表"
        return [item for item in items if isinstance(item, dict)], ""

    # ---------------------------------------------------------------- 工具

    def public_state(self, instance_id: str, timeline_id: str) -> dict[str, Any]:
        """这条线上与 Writing Assistant 有关的公开面（不含世界正文）。"""
        states = self.store.wa_state_list(instance_id, timeline_id)
        rows = self.store.wa_candidate_list(instance_id, timeline_id)
        return {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "outlines": [{"outline_id": str(row["outline_id"]), "chapter": str(row.get("chapter") or ""),
                          "evaluated_world": int(row.get("evaluated_world") or 0)} for row in states],
            "candidates": [cand.public_candidate(row) for row in rows],
            "generated_real": time.time(),
        }
