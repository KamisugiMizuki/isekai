"""世界运行层服务：时间线时钟、倍率、水位推进与角色状态。

职责（WORLD_RUNTIME_SPEC §2、§4、§10、§11）：
- 每线独立的时钟与倍率；激活即重锚（冻结期间不补算），冻结先结算已生效段并取消未生效请求；
- 水位推进按世界日分批、每批原子提交，重复执行不重复产生经历（幂等）；
- 角色状态（性格单元 / 生活线 / 经历）落库在运行层，会话层经认知接口只拿切片。
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from ..log import get_logger
from ..store import Store
from . import budget as budget_mod
from . import cognition, drafts, embedding as embedding_mod, environment, events, intents, life
from . import memory as memory_mod, personality, planning, versioning
from .calendar import Calendar, calendar_from_package
from .clock import DEFAULT_RATE_MAX, ClockState, RateCommand, describe, natural_second, settle, target_world

log = get_logger("isekai.runtime")

class RuntimeStateError(ValueError):
    """运行层拒绝该操作：状态不允许或参数非法。"""


def from_config(cfg: Any, store: Store) -> "RuntimeService":
    """运行层的**唯一构造入口**：按 RuntimeConfig 字段名对齐传参，不再各处手抄一份。

    名字对不上的参数自动跳过——新增运行层配置只要两处同名就生效。
    """
    import inspect

    runtime_cfg = cfg.runtime
    wanted = set(inspect.signature(RuntimeService.__init__).parameters) - {"self", "store"}
    params = {name: getattr(runtime_cfg, name) for name in wanted if hasattr(runtime_cfg, name)}
    return RuntimeService(store, **params)


class RuntimeService:
    def __init__(
        self,
        store: Store,
        *,
        rate_max: int | None = None,
        max_active_timelines: int = 4,
        catch_up_batches: int = 8,
        catch_up_lag_seconds: int = 0,
        render_calls_per_day: int = 20,
        instance_tokens_per_day: int = 400_000,
        timeline_tokens_per_day: int = 150_000,
        task_tokens_per_day: int = 60_000,
        priority_reserve_ratio: float = 0.25,
        memory_extract_per_day: int = 40,
        memory_recall_limit: int = 6,
        memory_brief_tokens: int = 900,
        memory_decay_per_day: float = 0.02,
        memory_embedding_model: str = "",
        memory_embedding_base_url: str = "",
        memory_embedding_api_key: str = "",
        autocommit_enabled: bool = True,
        autocommit_minutes: int = 60,
        autocommit_events: int = 50,
    ) -> None:
        self.store = store
        #: 倍率上限：全局统一、仅开发者可配置（§2.2），默认 2592000 世界秒 / 现实秒
        self.rate_max = int(rate_max or DEFAULT_RATE_MAX)
        #: 同时激活的时间线上限（§4）
        self.max_active_timelines = max(1, int(max_active_timelines))
        #: 单次推进的批数上限（§2.6 预算）
        self.catch_up_batches = max(1, int(catch_up_batches))
        #: 滞后超过该世界秒数即进入「追赶受限」（§2.6）；0 = 与目标同步才退出受限
        self.catch_up_lag_seconds = max(0, int(catch_up_lag_seconds))
        #: 表述 / 展开 / 自主提案的现实日调用上限（§2.8 单任务预算）
        self.render_calls_per_day = max(0, int(render_calls_per_day))
        #: 三层预算上限（§2.8）：可由管理面按实例覆盖
        self.budget_limits = {
            "instance_tokens_per_day": max(0, int(instance_tokens_per_day)),
            "timeline_tokens_per_day": max(0, int(timeline_tokens_per_day)),
            "task_tokens_per_day": max(0, int(task_tokens_per_day)),
        }
        self.priority_reserve_ratio = max(0.0, min(1.0, float(priority_reserve_ratio)))
        #: 角色记忆参数（MEMORY_SPEC §十）
        self.memory_extract_per_day = max(0, int(memory_extract_per_day))
        self.memory_recall_limit = max(1, int(memory_recall_limit))
        self.memory_brief_tokens = max(0, int(memory_brief_tokens))
        self.memory_decay_per_day = max(0.0, min(1.0, float(memory_decay_per_day)))
        #: 远程 embedding（MEMORY_SPEC §5.2）：缺配置即退化全文召回
        self.embedding_model = str(memory_embedding_model or "")
        self.embedding_base_url = str(memory_embedding_base_url or "")
        self.embedding_api_key = str(memory_embedding_api_key or "")
        #: 自动提交（§5.1）：默认现实 1 小时或新增事件 50 条，可配置可关；手动提交不受开关限制
        self.autocommit_enabled = bool(autocommit_enabled)
        self.autocommit_minutes = max(1, int(autocommit_minutes))
        self.autocommit_events = max(1, int(autocommit_events))

    # ---------- 基础读取 ----------

    def describe_world(self, instance_id: str, world_seconds: int) -> str:
        """世界时刻的人话标签（历法视图，§2.1）。"""
        instance = self.store.instance_get(instance_id) or {}
        return self.calendar(instance).describe(int(world_seconds))

    # ---------- 用户引入事件（§八，阶段 4） ----------

    def _known_targets(self, instance: dict[str, Any], timeline_id: str, *, world_seconds: int) -> tuple[set[str], list[str]]:
        """来源点已登记的对象与渠道：效果只能指向它们（不泄露任何未登记内容）。"""
        package = self.setting(instance)["world_package"]
        targets: set[str] = set()
        for card in self.cards(instance, timeline_id=timeline_id, world_seconds=world_seconds):
            targets.add(str((card.get("meta") or {}).get("card_id") or ""))
            targets.add(str(card.get("role_id") or ""))
            for channel in card.get("channels") or []:
                if channel.get("source_id"):
                    targets.add(str(channel["source_id"]))
        for entity in package.get("entities") or []:
            if isinstance(entity, dict) and entity.get("id"):
                targets.add(str(entity["id"]))
        for item in (package.get("environment") or {}).get("types") or []:
            if isinstance(item, dict) and item.get("id"):
                targets.add(str(item["id"]))
        channels = sorted(
            str(item["id"]) for item in package.get("comms", {}).get("sources", []) if isinstance(item, dict) and item.get("id")
        ) if isinstance(package.get("comms"), dict) else []
        targets.discard("")
        return targets, channels

    async def draft_user_event(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        intent: str = "",
        payload: dict[str, Any] | None = None,
        source_commit: str | None = None,
        llm: Any = None,
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """生成草案（§八 第 1–5 条）：只翻译与校验，不施加任何效果。"""
        import time as _time

        now = _time.time() if now_real is None else float(now_real)
        instance = self.store.instance_get(instance_id)
        if instance is None:
            raise RuntimeStateError(f"实例不存在：{instance_id}")
        if source_commit:
            source = self.store.commit_get(source_commit)
            if source is None or str(source["instance_id"]) != instance_id:
                raise RuntimeStateError(f"没有该提交：{source_commit}")
            if str(source["timeline_id"]) != timeline_id:
                raise RuntimeStateError("来源提交不属于这条线")
            watermark = int(source["moment"])
        else:
            watermark = self.world_moment(instance_id, timeline_id)
        targets, channels = self._known_targets(instance, timeline_id, world_seconds=watermark)

        candidate = dict(payload or {})
        candidate.setdefault("intent", intent)
        if llm is not None and str(intent or "").strip() and not candidate.get("effects"):
            package = self.setting(instance)["world_package"]
            allowed = planning.allowed_targets(package, {"meta": {"card_id": ""}, "role_id": "", "channels": []}, knowledge=[], observations=[])
            allowed = {kind: sorted(targets) for kind in allowed}  # 管理面：目标集合是来源点已登记对象
            prompt = drafts.proposal_prompt(
                intent=str(intent), world_label=self.describe_world(instance_id, watermark),
                allowed=allowed, channels=channels,
            )
            prompt_text = "\n".join(str(item.get("content") or "") for item in prompt)
            reservation = self.reserve_call(
                instance_id, timeline_id, "user_event_draft", prompt_text=prompt_text, now_real=now
            )
            if reservation.get("ok"):
                try:
                    text = await llm.chat(prompt, temperature=0.3, timeout=60.0)
                    self.settle_call(reservation, prompt_text=prompt_text, reply=text)
                    proposed = drafts.parse_proposal(text, targets)
                    candidate = {**candidate, **{k: v for k, v in proposed.items() if v}}
                except Exception:
                    self.settle_call(reservation, prompt_text=prompt_text, outcome="error")

        try:
            normalized = drafts.normalize_draft(
                self.setting(instance)["world_package"], candidate,
                known_targets=targets, world_seconds=watermark, default_channels=channels,
            )
        except ValueError as exc:
            # 无法表达成受支持事件就明确拒绝（不把自由文本当已执行），提示只说用户自己的输入
            return {"accepted": False, "reason": str(exc)}

        draft_id = drafts.draft_id_for(instance_id, timeline_id, normalized["intent"], normalized["at_world"])
        row = {
            "id": draft_id, "instance_id": instance_id, "source_timeline": timeline_id,
            "source_commit": source_commit or "", "payload": json.dumps(normalized, ensure_ascii=False),
            "state": "draft", "timeline_id": "", "created_world": watermark, "created_at": now,
        }
        existing = self.store.draft_get(draft_id)
        if existing is not None and str(existing["state"]) == "confirmed":
            return {"accepted": True, "draft": drafts.public_draft({**normalized, "id": draft_id, "confirmed": True,
                                                                  "timeline_id": existing["timeline_id"]}),
                    "reused": True}
        self.store.draft_put(row)
        return {"accepted": True, "draft": drafts.public_draft({**normalized, "id": draft_id, "source": {
            "timeline_id": timeline_id, "commit_id": source_commit or "", "world": watermark}}),
            "targets_seen": len(targets)}

    def confirm_user_event(
        self, instance_id: str, draft_id: str, *, name: str = "", now_real: float | None = None
    ) -> dict[str, Any]:
        """确认后原子建线并注入（§八 第 6–8 条）：即时事件立即生效，预约只写待执行状态。"""
        import time as _time

        now = _time.time() if now_real is None else float(now_real)
        draft = self.store.draft_get(draft_id)
        if draft is None:
            raise RuntimeStateError(f"没有该草案：{draft_id}")
        if str(draft["state"]) == "confirmed":
            return {"timeline_id": str(draft["timeline_id"]), "reused": True}
        source_timeline = str(draft["source_timeline"])
        source_commit = str(draft["source_commit"] or "")
        payload = json.loads(draft["payload"])
        # 来源点重新校验（§八 第 7 条）：用当前提交 / 当前水位，不静默换基础
        base_commit = source_commit or self.commit(instance_id, source_timeline, kind="auto", note="用户引入事件的来源点")["id"]
        branch = self.fork(
            instance_id, source_timeline, commit_id=base_commit,
            name=name or f"引入事件：{str(payload.get('intent') or '')[:12]}", now_real=now,
        )
        new_line = str(branch["timeline"]["id"])
        try:
            result = self._inject_user_event(instance_id, new_line, payload, draft_id=draft_id, now_real=now)
        except Exception:
            # 失败不留下半条线
            try:
                self.store.timeline_delete(instance_id, new_line)
            except Exception:
                pass
            raise
        self.store.draft_put({**draft, "state": "confirmed", "timeline_id": new_line, "payload": draft["payload"]})
        return {"timeline_id": new_line, "commit": branch["commit"], **result}

    def _inject_user_event(
        self, instance_id: str, timeline_id: str, payload: dict[str, Any], *, draft_id: str, now_real: float
    ) -> dict[str, Any]:
        """注入：即时 → 与水位同批落效果 / 说法 / 获知；预约 → 只写待执行状态。"""
        clock = self.clock_row(timeline_id)
        world = int(clock["processed_world"])
        ident = f"ev-user-{events.stable_key(instance_id, timeline_id, draft_id)[:12]}"
        if payload["when"] == "scheduled":
            self.store.pending_event_add({
                "id": f"pe-{ident[3:]}", "instance_id": instance_id, "timeline_id": timeline_id,
                "at_world": int(payload["at_world"]), "payload": json.dumps(payload, ensure_ascii=False),
                "state": "pending", "note": "", "created_world": world, "created_at": now_real,
            })
            return {"scheduled": True, "at_world": int(payload["at_world"]), "event": ident}
        rows = self._user_event_rows(instance_id, timeline_id, payload, ident=ident, world=world)
        applied = self.store.apply_runtime_batch(
            timeline_id=timeline_id, generation=int(clock["generation"]), processed_world=world,
            catching_up=False, events=[rows["event"]], claims=rows["claims"], knowledge=rows["knowledge"],
            effects=rows["effects"],
        )
        if not applied:
            raise RuntimeStateError("注入被拒（世代已变）")
        return {"scheduled": False, "event": ident, "effects": len(rows["effects"]), "claims": len(rows["claims"])}

    def _user_event_rows(
        self, instance_id: str, timeline_id: str, payload: dict[str, Any], *, ident: str, world: int
    ) -> dict[str, list[dict[str, Any]]]:
        """用户事件的登记行：与引擎事件同形（效果 + 说法），获知交给常规传播链（§五）。"""
        event_row = {
            "instance_id": instance_id, "timeline_id": timeline_id, "id": ident, "world_seconds": world,
            "seq": 0, "kind": "world", "family": "政治", "template": "user.introduced",
            "source": "user", "summary": str(payload["intent"])[:200], "detail": str(payload["intent"]),
            "text_source": "template", "effects": json.dumps(payload["effects"], ensure_ascii=False),
            "share_value": 0.7, "importance": 0.8, "created_real": time.time(),
        }
        effects: list[dict[str, Any]] = []
        for index, item in enumerate(payload["effects"]):
            effects.append({
                "id": f"ef-{ident[3:]}-{index}", "instance_id": instance_id, "timeline_id": timeline_id,
                "event_id": ident, "target": str(item["target"]), "kind": str(item["kind"]),
                "family": "政治", "value": item.get("value"), "from_world": world,
                "expiry": str(item.get("expiry") or "with_cause"), "recovery": str(item.get("recovery") or ""),
                "active": 1, "cleared_at": None,
            })
        claims: list[dict[str, Any]] = []
        knowledge: list[dict[str, Any]] = []
        cards = self.cards(
            self.store.instance_get(instance_id) or {}, timeline_id=timeline_id, world_seconds=world
        )
        for index, item in enumerate(payload.get("claims") or []):
            claim_id = f"cl-{ident[3:]}-{index}"
            claim = {
                "id": claim_id, "instance_id": instance_id, "timeline_id": timeline_id, "event_id": ident,
                "source_id": str(item.get("source_id") or ""), "text": str(item["text"]),
                "audience": str(item.get("audience") or "public"), "earliest_world": world,
                "credibility": float(item.get("credibility") or 0.6), "derived_from": None,
            }
            claims.append(claim)
            # 事件就发生在当前水位：按常规规则直接结算到达（与后续批次的传播同一套判定）
            for card in cards:
                grant = events.claim_grant(claim, card, world_seconds=world)
                if grant is not None:
                    knowledge.append(grant)
        return {"event": event_row, "effects": effects, "claims": claims, "knowledge": knowledge}

    def apply_due_pending_events(self, instance_id: str, timeline_id: str, *, to_world: int) -> dict[str, int]:
        """预约事件到点复核后施加或记为未执行（§八 末条，验收 17）：条件失效不强行执行。"""
        due = self.store.pending_events_due(instance_id, timeline_id, until=to_world)
        applied = skipped = cancelled = 0
        for row in due:
            payload = json.loads(row["payload"])
            instance = self.store.instance_get(instance_id) or {}
            targets, _ = self._known_targets(instance, timeline_id, world_seconds=int(row["at_world"]))
            stale = [item for item in payload["effects"] if str(item["target"]) not in targets]
            if stale:
                self.store.pending_event_set(str(row["id"]), state="cancelled", note="条件失效：目标不再是本线参与者")
                cancelled += 1
                continue
            clock = self.clock_row(timeline_id)
            ident = f"ev-user-{events.stable_key(instance_id, timeline_id, row['id'])[:12]}"
            rows = self._user_event_rows(instance_id, timeline_id, payload, ident=ident, world=int(row["at_world"]))
            ok = self.store.apply_runtime_batch(
                timeline_id=timeline_id, generation=int(clock["generation"]),
                processed_world=int(clock["processed_world"]), catching_up=False,
                events=[rows["event"]], claims=rows["claims"], knowledge=rows["knowledge"], effects=rows["effects"],
            )
            if ok and self.store.pending_event_set(str(row["id"]), state="applied", note="已施加"):
                applied += 1
            else:
                skipped += 1
        return {"applied": applied, "cancelled": cancelled, "skipped": skipped}

    # ---------- 版本管理（阶段 4，§5 / §6 / §7） ----------

    def commit(
        self, instance_id: str, timeline_id: str, *, kind: str = "manual", note: str = ""
    ) -> dict[str, Any]:
        """在一致边界取快照（§5.1）：提交是回滚点与分叉点。"""
        row = self.clock_row(timeline_id)
        snapshot = versioning.snapshot_of(self.store, instance_id, timeline_id, note=note)
        commit_id = f"cm-{secrets.token_hex(6)}"
        record = versioning.make_commit_row(
            commit_id, instance_id, timeline_id, kind=kind, moment=int(row["processed_world"]), note=note
        )
        self.store.commit_add(record, snapshot=snapshot)
        self.store.commit_state_set(
            timeline_id, instance_id, last_commit_at=time.time(), last_commit_moment=int(row["processed_world"])
        )
        return versioning.public_commit(record)

    def commits(self, instance_id: str, timeline_id: str | None = None) -> list[dict[str, Any]]:
        """提交列表只给管理元数据，不带剧情摘要（§5.1）。"""
        return [
            versioning.public_commit(row)
            for row in self.store.commit_list(instance_id, timeline_id)
        ]

    def rename_timeline(self, instance_id: str, timeline_id: str, *, name: str, description: str | None = None) -> dict[str, Any]:
        """命名 / 描述（§四）：列表只露管理元数据。"""
        if not str(name or "").strip():
            raise RuntimeStateError("名称不能为空")
        self.store.timeline_update(timeline_id, name=str(name).strip(), description=description)
        return self.store.timeline_get(timeline_id) or {}

    def archive_timeline(self, instance_id: str, timeline_id: str, *, now_real: float | None = None) -> dict[str, Any]:
        """归档先冻结、不删除数据（§四）。"""
        state = str((self.store.timeline_get(timeline_id) or {}).get("state") or "")
        if state == "active":
            self.freeze(instance_id, timeline_id, now_real=now_real)
        self.store.timeline_set_state(timeline_id, "archived")
        return self.store.timeline_get(timeline_id) or {}

    def delete_timeline(self, instance_id: str, timeline_id: str) -> dict[str, Any]:
        """删除一条线：停止该线任务、解绑通道、清理数据；其他线引用的提交保留（§四）。"""
        lines = self.store.timeline_list(instance_id)
        if len(lines) <= 1:
            raise RuntimeStateError("实例至少要保留一条时间线")
        counts = self.store.timeline_delete(instance_id, timeline_id)
        return {"deleted": timeline_id, "counts": counts}

    def maybe_auto_commit(self, instance_id: str, timeline_id: str, *, now_real: float) -> dict[str, Any] | None:
        """自动提交：现实时间到达间隔，或新增事件数到阈值（§5.1，可配置、可关）。"""
        if not self.autocommit_enabled:
            return None
        state = self.store.commit_state_get(timeline_id)
        events_since = self.store.event_count_since(
            instance_id, timeline_id, since=int(state.get("last_commit_moment") or 0)
        )
        elapsed = float(now_real) - float(state.get("last_commit_at") or 0.0)
        due = elapsed >= self.autocommit_minutes * 60 or events_since >= self.autocommit_events
        if not due:
            return None
        return self.commit(instance_id, timeline_id, kind="auto", note="自动提交")

    def fork(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        commit_id: str,
        name: str = "",
        activate: bool = False,
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """从不可变来源提交分叉（§六）：创建本身不等于激活。"""
        source = self.store.commit_get(commit_id)
        if source is None or str(source["instance_id"]) != instance_id:
            raise RuntimeStateError(f"没有该提交：{commit_id}")
        snapshot = self.store.commit_snapshot_get(commit_id) or {}
        payload = dict(snapshot.get("runtime") or {})
        new_id = f"tl-{secrets.token_hex(4)}"
        self.store.timeline_add({
            "id": new_id,
            "instance_id": instance_id,
            "name": name or f"{timeline_id} 的分支",
            "state": "frozen",  # 创建不等于激活（§四）
            "source_commit": commit_id,
            "created_at": time.time(),
        })
        self.store.clock_put({
            "timeline_id": new_id,
            "base_real": float(now_real if now_real is not None else time.time()),
            "base_world": int(snapshot.get("world") or 0),
            "rate": int(snapshot.get("rate") or 1),
            "high_water_real": float(now_real if now_real is not None else time.time()),
            "anchor_real": float(now_real if now_real is not None else time.time()),
            "processed_world": int(snapshot.get("world") or 0),
            "generation": 0,
            "catching_up": 0,
            "limited": 0,
        })
        self.store.runtime_load(instance_id, new_id, payload)
        # 分支也留一个自己的提交点（带快照，回滚 / 再分叉都指得到它）
        record = self.commit(instance_id, new_id, kind="initial", note=f"分叉自 {commit_id}")
        if activate:
            self.activate(instance_id, new_id, now_real=now_real)
        return {"timeline": self.store.timeline_get(new_id), "commit": record, "source_commit": commit_id}

    def rollback(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        commit_id: str,
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """回滚（覆盖语义，§七）：保持线身份，把有效历史覆盖到所选可达提交。

        被截去的未来退出当前线；随后以**回滚完成时的现实时间**重新锚定；本机激活 / 冻结状态
        不来自历史，原来激活则继续、原来冻结则停在回滚点。
        """
        source = self.store.commit_get(commit_id)
        if source is None or str(source["instance_id"]) != instance_id:
            raise RuntimeStateError(f"没有该提交：{commit_id}")
        if str(source["timeline_id"]) != timeline_id:
            raise RuntimeStateError("只能回滚到本线自己的提交")
        snapshot = self.store.commit_snapshot_get(commit_id)
        if snapshot is None:
            raise RuntimeStateError("该提交没有快照，无法回滚")
        line = self.store.timeline_get(timeline_id) or {}
        before_state = str(line.get("state") or "frozen")
        before_clock = self.clock_row(timeline_id)
        # 原子切换：先提升运行世代让迟到结果失效，再在同一事务里清空 + 写回
        self.store.clock_put({**before_clock, "generation": int(before_clock["generation"]) + 1})
        # 飞行中的输入作废、未投递的回复取消：旧世代的结果不得写回或继续发送（§七）
        voided = self.store.timeline_void_inflight(timeline_id)
        cancelled = self.store.timeline_cancel_undelivered(timeline_id)
        self.store.runtime_load(instance_id, timeline_id, dict(snapshot.get("runtime") or {}), clear=True)
        now = float(now_real if now_real is not None else time.time())
        self.store.clock_put({
            **self.clock_row(timeline_id),
            "base_real": now,
            "base_world": int(snapshot.get("world") or 0),
            "rate": int(snapshot.get("rate") or 1),
            "high_water_real": now,
            "anchor_real": now,
            "processed_world": int(snapshot.get("world") or 0),
            "catching_up": 0,
            "limited": 0,
        })
        # 本机状态不从历史恢复：原来激活继续激活，原来冻结停在回滚点
        self.store.timeline_set_state(timeline_id, before_state)
        self.store.commit_state_set(
            timeline_id, instance_id, last_commit_at=now, last_commit_moment=int(snapshot.get("world") or 0)
        )
        return {
            "timeline": self.store.timeline_get(timeline_id),
            "commit": versioning.public_commit(source),
            "state": before_state,
            "world": int(snapshot.get("world") or 0),
            "generation": int(self.clock_row(timeline_id)["generation"]),
            "voided_inputs": voided,
            "cancelled_replies": cancelled,
        }

    # ---------- 角色记忆（MEMORY_SPEC） ----------

    def queue_dialog_turn(
        self,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        *,
        world_seconds: int,
        user_ref: str,
        user_text: str,
        reply_message_id: str = "",
        reply_text: str = "",
    ) -> int:
        """把一轮已固化的来往登记成待提取来源（§4.1）。

        多条入站共享一份回复时：输入按各自来源分别登记，回复按本轮唯一标识登记一次。
        """
        added = 0
        if user_ref:
            added += int(self.store.memory_task_add({
                "id": f"mt-dlg-{events.stable_key(instance_id, timeline_id, character_id, 'user', user_ref)[:12]}",
                "instance_id": instance_id, "timeline_id": timeline_id, "character_id": character_id,
                "source_kind": "dialog", "source_ref": f"user:{user_ref}",
                "source_world": int(world_seconds), "created_world": int(world_seconds),
                "text": str(user_text or ""),
            }))
        if reply_message_id:
            added += int(self.store.memory_task_add({
                "id": f"mt-dlg-{events.stable_key(instance_id, timeline_id, character_id, 'reply', reply_message_id)[:12]}",
                "instance_id": instance_id, "timeline_id": timeline_id, "character_id": character_id,
                "source_kind": "dialog", "source_ref": f"reply:{reply_message_id}",
                "source_world": int(world_seconds), "created_world": int(world_seconds),
                "text": str(reply_text or ""),
            }))
        return added

    def queue_world_sources(self, instance_id: str, timeline_id: str, *, since_world: int = 0) -> int:
        """世界侧来源（§4.1）：她的经历、她已获知的说法、她自己的打算——不传未过滤实情。"""
        added = 0
        for card in self.cards(self.store.instance_get(instance_id) or {}, timeline_id=timeline_id):
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id:
                continue
            rows: list[tuple[str, str, int]] = []
            for row in self.store.experience_window(
                instance_id, timeline_id, character_id, until=10**15, limit=200
            ):
                rows.append(("experience", str(row["id"]), int(row["world_seconds"])))
            for row in self.store.knowledge_window(
                instance_id, timeline_id, character_id, until=10**15, limit=200
            ):
                rows.append(("claim", str(row["id"]), int(row["world_seconds"])))
            for row in self.store.intent_list(instance_id, timeline_id, character_id):
                rows.append(("intent", str(row["id"]), int(row["source_world"])))
            for kind, ref, world in rows:
                if int(world) <= int(since_world):
                    continue
                added += int(self.store.memory_task_add({
                    "id": f"mt-{kind[:3]}-{events.stable_key(instance_id, timeline_id, character_id, kind, ref)[:12]}",
                    "instance_id": instance_id, "timeline_id": timeline_id, "character_id": character_id,
                    "source_kind": kind, "source_ref": ref,
                    "source_world": int(world), "created_world": int(world), "text": "",
                }))
        return added

    def _source_material(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """按来源取材料；来源不可达（回滚 / 删除 / 迟到）时返回 None，任务标 dropped（§4.1）。"""
        instance_id, timeline_id = str(task["instance_id"]), str(task["timeline_id"])
        ref, kind = str(task["source_ref"]), str(task["source_kind"])
        when = self.describe_world(instance_id, int(task["source_world"]))
        if kind == "dialog":
            role = "user" if ref.startswith("user:") else "character"
            text = str(task.get("text") or "")
            if not text:
                return None
            return {
                "text": text,
                "source": "对话·联络者" if role == "user" else "对话·她自己说",
                "when": when,
                "sources": [{"kind": "dialog", "ref": ref, "source_role": role}],
                "world": int(task["source_world"]),
            }
        if kind == "experience":
            row = next(
                (item for item in self.store.experience_window(
                    instance_id, timeline_id, str(task["character_id"]), until=10**15, limit=500
                ) if str(item["id"]) == ref),
                None,
            )
            if row is None:
                return None
            return {
                "text": str(row.get("activity") or ""),
                "source": "经历",
                "when": self.describe_world(instance_id, int(row["world_seconds"])),
                "sources": [{"kind": "experience", "ref": ref}],
                "world": int(row["world_seconds"]),
            }
        if kind == "claim":
            row = next(
                (item for item in self.store.knowledge_window(
                    instance_id, timeline_id, str(task["character_id"]), until=10**15, limit=500
                ) if str(item["id"]) == ref),
                None,
            )
            if row is None:
                return None
            return {
                "text": str(row.get("text") or ""),
                "source": "听说／读到的",
                "when": self.describe_world(instance_id, int(row["world_seconds"])),
                "sources": [{"kind": "claim", "ref": ref, "via": row.get("via") or ""}],
                "world": int(row["world_seconds"]),
            }
        row = next(
            (item for item in self.store.intent_list(instance_id, timeline_id, str(task["character_id"]))
             if str(item["id"]) == ref),
            None,
        )
        if row is None:
            return None
        return {
            "text": f"{row.get('object')}（依据：{row.get('basis')}）",
            "source": "她自己的打算",
            "when": when,
            "sources": [{"kind": "intent", "ref": ref}],
            "world": int(row.get("source_world") or 0),
        }

    async def extract_memories(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        llm: Any,
        now_real: float,
        limit: int = 20,
    ) -> dict[str, Any]:
        """有界提取（§4.1）：按来源打包、单次调用、校验不过当没提取过；失败保留待处理。"""
        from ..log import get_logger

        log = get_logger("isekai.memory")
        cap = max(1, min(int(limit), memory_mod.BATCH_SIZE))
        tasks = self.store.memory_tasks(instance_id, timeline_id)[:cap]
        if not tasks:
            return {"extracted": 0, "written": 0, "pending": 0, "calls": 0}
        row = self.clock_row(timeline_id)
        watermark = int(row["processed_world"])
        day_seconds = self.calendar(self.store.instance_get(instance_id)).day_seconds
        written = calls = pending = 0
        by_character: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            by_character.setdefault(str(task["character_id"]), []).append(task)
        for character_id, items in by_character.items():
            materials: list[dict[str, Any]] = []
            for task in items:
                material = self._source_material(task)
                if material is None:
                    self.store.memory_task_set(str(task["id"]), state="dropped", note="来源不可达")
                    continue
                materials.append({**material, "ref": str(task["id"]), "task": task})
            if not materials:
                continue
            known = self.store.memory_scope(instance_id, timeline_id, character_id, until=watermark)
            prompt = memory_mod.extraction_prompt(
                name=self._display_name(instance_id, timeline_id, character_id),
                world_label=self.describe_world(instance_id, watermark),
                items=materials,
                existing=[{"id": item["id"], "text": item["text"]} for item in known[-8:]],
            )
            prompt_text = "\n".join(str(item.get("content") or "") for item in prompt)
            reservation = self.reserve_call(
                instance_id, timeline_id, "memory_extract", prompt_text=prompt_text, now_real=now_real
            )
            if not reservation.get("ok"):
                pending += len(materials)
                continue
            calls += 1
            try:
                text = await llm.chat(prompt, temperature=0.3, timeout=90.0)
                if not str(text or "").strip():
                    # 推理型模型偶发把预算烧在 reasoning 里返回空正文：原样重试一次，仍空就当失败
                    text = await llm.chat(prompt, temperature=0.3, timeout=90.0)
            except Exception:  # 提取失败不阻断对话与世界推进
                log.exception("memory extraction failed character=%s", character_id)
                self.settle_call(reservation, prompt_text=prompt_text, outcome="error")
                for material in materials:
                    self.store.memory_task_set(str(material["task"]["id"]), state="pending", note="模型不可用")
                pending += len(materials)
                continue
            self.settle_call(reservation, prompt_text=prompt_text, reply=text)
            entries = memory_mod.parse_extraction(text, {str(item["ref"]) for item in materials})
            by_ref = {str(item["ref"]): item for item in materials}
            if not entries:
                for material in materials:
                    self.store.memory_task_set(str(material["task"]["id"]), state="done", note="无可记内容")
                continue
            for entry in entries:
                material = by_ref[entry["ref"]]
                task = material["task"]
                strength = memory_mod.decayed_strength(
                    entry["strength"],
                    from_world=int(task["source_world"]),
                    to_world=watermark,
                    day_seconds=day_seconds,
                    per_day=self.memory_decay_per_day,
                )
                saved = self.store.memory_add({
                    "id": f"mm-{events.stable_key(instance_id, timeline_id, character_id, entry['text'])[:12]}",
                    "instance_id": instance_id,
                    "timeline_id": timeline_id,
                    "character_id": character_id,
                    "text": entry["text"],
                    "kind": entry["kind"],
                    "sources": material["sources"],
                    "happened_world": material["world"],
                    "learned_world": int(task["source_world"]),
                    "recorded_world": watermark,
                    "semantic_watermark": watermark,
                    "strength": strength,
                    "confidence": entry["confidence"],
                    "source_key": str(task["id"]),
                    "decay_world": watermark,
                })
                if saved is not None:
                    written += 1
                self.store.memory_task_set(str(task["id"]), state="done")
        return {"extracted": len(tasks), "written": written, "pending": pending, "calls": calls}

    def turn_context(
        self,
        session: dict[str, Any],
        *,
        topic: str = "",
        world_seconds: int | None = None,
        query_vector: list[float] | None = None,
    ) -> dict[str, Any]:
        """本轮扮演定义 + 记忆简报（§5.1 第 5 步）：简报只进生成上下文，不展示给用户。"""
        prompt = self.system_prompt(session, topic=topic)
        instance_id = str(session.get("instance_id") or "")
        timeline_id = str(session.get("timeline_id") or "")
        character_id = str(session.get("character_id") or "")
        if not (instance_id and timeline_id and character_id):
            return {"prompt": prompt, "memory_ids": [], "brief": ""}
        try:
            recalled = self.recall(
                instance_id, timeline_id, character_id, topic=topic, world_seconds=world_seconds,
                query_vector=query_vector,
            )
        except Exception:  # 召回失败不得阻断对话
            return {"prompt": prompt, "memory_ids": [], "brief": ""}
        brief = recalled["brief"]["text"]
        if brief:
            prompt = prompt + chr(10) + chr(10) + "她此刻想得起来的事（按她自己的记性，别当成盘点）：" + chr(10) + brief
        return {"prompt": prompt, "memory_ids": recalled["ids"], "brief": brief}

    # ---------- 远程向量（§5.2） ----------

    @property
    def embedding_ready(self) -> bool:
        return bool(self.embedding_model and self.embedding_base_url and self.embedding_api_key)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """发一次远程请求；未配置时直接抛错，调用方退化全文召回。"""
        return await embedding_mod.embed(
            texts,
            model=self.embedding_model,
            base_url=self.embedding_base_url,
            api_key=self.embedding_api_key,
        )

    async def embed_memories(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        llm: Any = None,
        now_real: float | None = None,
        limit: int = 16,
    ) -> dict[str, Any]:
        """把缺向量 / 指纹不符的条目补齐（§5.2）：受共享预算约束，失败保留待嵌入状态。"""
        import time as _time

        now = _time.time() if now_real is None else float(now_real)
        if not self.embedding_ready:
            return {"embedded": 0, "skipped": "not_configured"}
        rows = self.store.memory_missing_embeddings(
            instance_id, timeline_id, model=self.embedding_model
        )[: max(1, int(limit))]
        if not rows:
            return {"embedded": 0}
        reservation = self.reserve_call(
            instance_id,
            timeline_id,
            "embedding",
            tokens_est=sum(len(str(item["text"])) for item in rows) // 3 + 64,
            now_real=now,
        )
        if not reservation.get("ok"):
            return {"embedded": 0, "paused": True, "blocked": reservation.get("blocked")}
        try:
            vectors = await self._embed([str(item["text"]) for item in rows])
        except Exception as exc:
            self.settle_call(reservation, outcome="error")
            # 配置错字（模型名 / 地址 / 凭据）要一眼看得出来，别只回一个异常类名
            return {
                "embedded": 0,
                "error": type(exc).__name__,
                "reason": str(exc)[:200],
                "model": self.embedding_model,
                "base_url": self.embedding_base_url,
            }
        self.settle_call(reservation, reply="".join(str(item["text"]) for item in rows))
        for row, vector in zip(rows, vectors):
            self.store.memory_embedding_put(
                str(row["id"]),
                instance_id=instance_id,
                timeline_id=timeline_id,
                model=self.embedding_model,
                vector=vector,
                content_hash=embedding_mod.content_hash(str(row["text"])),
            )
        return {"embedded": len(vectors), "model": self.embedding_model}

    async def embed_query(
        self, text: str, *, instance_id: str = "", timeline_id: str = "", now_real: float | None = None
    ) -> list[float] | None:
        """查询向量：拿不到就返回 None，召回退化全文（不阻断对话）。

        和补齐任务同受共享预算约束（§2.8 / §5.2）：预算不够就不发请求，别绕过账本。
        """
        import time as _time

        if not self.embedding_ready or not str(text or "").strip():
            return None
        if instance_id and timeline_id:
            reservation = self.reserve_call(
                instance_id, timeline_id, "embedding",
                tokens_est=len(str(text)) // 3 + 64,
                now_real=now_real if now_real is not None else _time.time(),
            )
            if not reservation.get("ok"):
                return None
        else:
            reservation = None
        try:
            vectors = await self._embed([str(text)])
        except Exception:
            if reservation:
                self.settle_call(reservation, outcome="error")
            return None
        if reservation:
            self.settle_call(reservation, reply=str(text))
        return vectors[0] if vectors else None

    def recall(
        self,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        *,
        topic: str = "",
        world_seconds: int | None = None,
        limit: int | None = None,
        query_vector: list[float] | None = None,
    ) -> dict[str, Any]:
        """混合召回（§5）：先限定可访问集合，再排序，再按预算打包简报；向量不可用即退化全文。"""
        if world_seconds is None:
            world_seconds = int(self.clock_row(timeline_id)["processed_world"])
        entries = self.store.memory_scope(instance_id, timeline_id, character_id, until=int(world_seconds))
        if not entries:
            return {"entries": [], "ids": [], "brief": {"lines": [], "ids": [], "text": ""}}
        day_seconds = self.calendar(self.store.instance_get(instance_id)).day_seconds
        vector_scores = self.store.memory_vector_scores(
            instance_id, timeline_id, character_id, topic,
            query_vector=query_vector, model=self.embedding_model,
        )
        ranked = memory_mod.rank(
            query=topic,
            entries=entries,
            now_world=int(world_seconds),
            day_seconds=day_seconds,
            vector_scores=vector_scores,
        )
        for item in ranked:
            item["source_label"] = memory_mod.source_label(json.loads(item.get("sources") or "[]"))
        ranked = [item for item in ranked if str(item.get("state")) != "archived"][:40]
        brief = memory_mod.pack_brief(
            ranked, budget_tokens=self.memory_brief_tokens, limit=limit or self.memory_recall_limit
        )
        return {"entries": ranked, "ids": brief["ids"], "brief": brief}

    def cite_memories(
        self,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        *,
        turn_id: str,
        memory_ids: list[str],
        world_seconds: int,
    ) -> int:
        """一轮被实际采纳后按 id 强化，同轮幂等（§5.3）。"""
        strengthened = 0
        for memory_id in memory_ids:
            if self.store.memory_cite(
                turn_id, memory_id, timeline_id=timeline_id, character_id=character_id,
                world_seconds=int(world_seconds),
            ):
                strengthened += 1
        return strengthened

    def decay_memories(self, timeline_id: str, *, to_world: int) -> int:
        """按世界时间衰减（§六）：冻结期间不调用即不衰减；按 decay_world 幂等。"""
        line = self.store.timeline_get(timeline_id)
        if line is None:
            return 0
        day_seconds = self.calendar(self.store.instance_get(str(line["instance_id"]))).day_seconds
        return self.store.memory_decay(
            timeline_id=timeline_id, to_world=int(to_world), day_seconds=day_seconds,
            per_day=self.memory_decay_per_day,
        )

    def _display_name(self, instance_id: str, timeline_id: str, character_id: str) -> str:
        instance = self.store.instance_get(instance_id) or {}
        for card in self.cards(instance, timeline_id=timeline_id):
            if str((card.get("meta") or {}).get("card_id")) == character_id:
                return str((card.get("identity") or {}).get("name") or character_id)
        return character_id

    # ---------- 三层预算（§2.8） ----------

    def budget_limits_for(self, instance_id: str) -> dict[str, int]:
        """实例策略覆盖全局配置（管理面可调上限）。"""
        limits = dict(self.budget_limits)
        policy = self.store.budget_policy_get(instance_id)
        for key in list(limits):
            value = policy.get(key)
            if isinstance(value, int) and value > 0:
                limits[key] = int(value)
        return limits

    def reserve_call(
        self,
        instance_id: str,
        timeline_id: str,
        task: str,
        *,
        prompt_text: str = "",
        tokens_est: int | None = None,
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """发起前原子预占；拒绝时返回 ok=False 与三层用量（调用方要如实上报，不得照常调用）。"""
        import time as _time

        bucket = int((now_real if now_real is not None else _time.time()) // 86400)
        policy = self.store.budget_policy_get(instance_id)
        if task in (policy.get("paused_tasks") or []):
            return {"ok": False, "blocked": ["paused"], "task": task}
        limits = self.budget_limits_for(instance_id)
        used = self.store.call_ledger_get(instance_id, timeline_id, task, bucket=bucket)
        if task in ("event_render", "event_expand") and used >= self.render_calls_per_day:
            return {"ok": False, "blocked": ["task_calls"], "task": task, "calls": used,
                    "limit": self.render_calls_per_day}
        index = budget_mod.task_index(task)
        reserve = {index: int(limits["instance_tokens_per_day"] * self.priority_reserve_ratio)}
        est = tokens_est if tokens_est is not None else (budget_mod.estimate_tokens(prompt_text) + 512)
        return self.store.budget_reserve(
            instance_id=instance_id,
            timeline_id=timeline_id,
            task=task,
            bucket=bucket,
            priority=index,
            tokens_est=est,
            limits=limits,
            reserved_for_higher=reserve,
        )

    def settle_call(
        self,
        reservation: dict[str, Any],
        *,
        prompt_text: str = "",
        reply: str = "",
        outcome: str = "ok",
        calls: int = 1,
    ) -> dict[str, Any] | None:
        """结算真实消耗（成功 / 失败 / 超时都算；没有真实用量时按估算量级记）。"""
        if not reservation or not reservation.get("ok"):
            return None
        tokens = budget_mod.estimate_tokens(prompt_text) + budget_mod.estimate_tokens(reply)
        return self.store.budget_settle(reservation["id"], tokens_actual=tokens, outcome=outcome, calls=calls)

    def release_call(self, reservation: dict[str, Any]) -> bool:
        return bool(reservation and reservation.get("ok") and self.store.budget_release(reservation["id"]))

    def budget_view(self, instance_id: str, *, now_real: float | None = None) -> dict[str, Any]:
        """非内容性的预算状态（§2.8）：用量 / 上限 / 暂停的任务，不含正文与密钥。"""
        import time as _time

        bucket = int((now_real if now_real is not None else _time.time()) // 86400)
        policy = self.store.budget_policy_get(instance_id)
        return {
            "bucket": bucket,
            "limits": self.budget_limits_for(instance_id),
            "paused_tasks": policy.get("paused_tasks") or [],
            "rows": self.store.budget_rows(instance_id, bucket=bucket),
            "usage": self.store.budget_usage(instance_id, bucket=bucket),
            "priority_order": list(budget_mod.PRIORITIES),
        }

    def _rows(self, instance_id: str, timeline_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        instance = self.store.instance_get(instance_id)
        if instance is None:
            raise RuntimeStateError(f"实例不存在：{instance_id}")
        timeline = next(
            (item for item in self.store.timeline_list(instance_id) if item["id"] == timeline_id), None
        )
        if timeline is None:
            raise RuntimeStateError(f"时间线不存在：{timeline_id}")
        return instance, timeline

    def setting(self, instance: dict[str, Any]) -> dict[str, Any]:
        return json.loads(instance["setting"])

    def calendar(self, instance: dict[str, Any]) -> Calendar:
        return calendar_from_package(self.setting(instance)["world_package"])

    def cards(
        self, instance: dict[str, Any], *, timeline_id: str | None = None, world_seconds: int | None = None
    ) -> list[dict[str, Any]]:
        """实例锁定的角色卡；给了线与水位时并入已补入的角色（§九 / 附录 B #18）。"""
        cards = list(self.setting(instance).get("cards") or [])
        if timeline_id is None or world_seconds is None:
            return cards
        joined = self.store.character_join_list(instance["id"], timeline_id, until=int(world_seconds))
        for row in joined:
            try:
                cards.append(json.loads(str(row["card"])))
            except json.JSONDecodeError:
                log.warning("补入角色卡损坏 card=%s", row.get("character_id"))
        return cards

    def seed_of(self, instance: dict[str, Any]) -> str:
        """锁定种子：候选抽样只依赖它 + 规则版本 + 历法日 + 槽序（附录 A）。"""
        return str(instance.get("seed") or "")

    def rules_of(self, instance: dict[str, Any]) -> str:
        return str(instance.get("rules_version") or "")

    def clock_row(self, timeline_id: str) -> dict[str, Any]:
        row = self.store.clock_get(timeline_id)
        if row is None:
            raise RuntimeStateError(f"时间线没有时钟记录：{timeline_id}")
        return row

    def state_of(self, row: dict[str, Any]) -> ClockState:
        return ClockState(
            base_real=float(row["base_real"]),
            base_world=int(row["base_world"]),
            rate=int(row["rate"]),
            high_water_real=float(row["high_water_real"]),
        )

    # ---------- 生命周期 ----------

    def init_timeline(self, instance_id: str, timeline_id: str, *, moment: int, now_real: float) -> dict[str, Any]:
        """创建 / 导入时为每条线建立时钟记录：冻结、倍率 1、水位 = 初始世界时刻。"""
        row = {
            "timeline_id": timeline_id,
            "base_real": float(now_real),
            "base_world": int(moment),
            "rate": 1,
            "high_water_real": float(now_real),
            "anchor_real": float(now_real),
            "processed_world": int(moment),
            "generation": 1,
        }
        self.store.clock_put(row)
        return row

    def activate(
        self, instance_id: str, timeline_id: str, *, now_real: float, rate: int | None = None
    ) -> dict[str, Any]:
        """激活：以当前现实时间重新锚定，不补算冻结期间的间隔（§2.4、§2.6）。

        倍率超过当前上限（上限被调低 / 导入端上限更低）时不静默改写：保持冻结，
        要求调用方在激活操作中确认一个合法倍率（§2.4）。
        """
        instance, timeline = self._rows(instance_id, timeline_id)
        if timeline["state"] == "active":
            return self.view(instance_id, timeline_id, now_real=now_real)
        active = self.active_timelines()
        if len(active) >= self.max_active_timelines:
            raise RuntimeStateError(f"同时激活的时间线已达上限 {self.max_active_timelines} 条")
        row = self.clock_row(timeline_id)
        stored_rate = int(row["rate"])
        effective_rate = stored_rate
        if rate is not None:
            effective_rate = self._check_rate(rate)
        elif stored_rate > self.rate_max:
            raise RuntimeStateError(
                f"该线倍率 {stored_rate} 超过当前上限 {self.rate_max}，需要在激活时确认一个合法倍率"
            )
        world_at_freeze = target_world(self.state_of(row), float(row["anchor_real"]))
        self.store.rate_cancel_pending(timeline_id)  # 冻结期间的待生效请求不跨重启恢复（§2.3.6）
        self.store.clock_put(
            {
                **row,
                "base_real": float(now_real),
                "base_world": int(world_at_freeze),
                "rate": effective_rate,
                "high_water_real": float(now_real),
                "anchor_real": float(now_real),
                "processed_world": max(int(row["processed_world"]), int(world_at_freeze)),
                "generation": int(row["generation"]) + 1,  # 旧世代任务一律失效（§4）
                "catching_up": 0,
                "limited": 0,
            }
        )
        self.store.timeline_set_state(timeline_id, "active")
        result = self.view(instance_id, timeline_id, now_real=now_real)
        result["confirmed_rate"] = effective_rate if rate is not None else None
        _ = instance
        return result

    def freeze(self, instance_id: str, timeline_id: str, *, now_real: float) -> dict[str, Any]:
        """冻结：结算已生效倍率段、取消未生效请求；冻结线不推进也不接受倍率调整。"""
        row = self.clock_row(timeline_id)
        state, consumed = self._settle_due(timeline_id, row, now_real)
        self.store.rate_apply(consumed)
        cancelled = self.store.rate_cancel_pending(timeline_id)
        world = target_world(state, now_real)
        self.store.clock_put(
            {
                **row,
                "base_real": float(now_real),
                "base_world": int(world),
                "high_water_real": max(float(row["high_water_real"]), float(now_real)),
                "anchor_real": float(now_real),
                "processed_world": max(int(row["processed_world"]), int(world)),
                "generation": int(row["generation"]) + 1,  # 冻结使旧世代任务失效（§4）
                "catching_up": 0,
                "limited": 0,
            }
        )
        self.store.timeline_set_state(timeline_id, "frozen")
        instance = self.store.instance_get(instance_id) or {}
        calendar = self.calendar(instance) if instance else None
        return {
            "instance": instance_id,
            "timeline": timeline_id,
            "state": "frozen",
            "world_seconds": int(world),
            "label": calendar.describe(int(world)) if calendar else "",
            "processed_world": int(max(int(row["processed_world"]), int(world))),
            "cancelled_commands": cancelled,
        }

    def _check_rate(self, rate: int) -> int:
        """倍率范围校验：正整数、受全局上限约束（§2.2）。"""
        if not isinstance(rate, int) or isinstance(rate, bool) or not 1 <= rate <= self.rate_max:
            raise RuntimeStateError(f"倍率必须是 1..{self.rate_max} 的整数")
        return int(rate)

    def _settle_due(
        self, timeline_id: str, row: dict[str, Any], now_real: float
    ) -> tuple[ClockState, list[int]]:
        pending = self.store.rate_pending(timeline_id)
        id_of: dict[int, int] = {}
        commands: list[RateCommand] = []
        for item in pending:
            command = RateCommand(
                input_real=float(item["input_real"]),
                effective_real=int(item["effective_real"]),
                rate=int(item["rate"]),
                seq=int(item["seq"]),
            )
            id_of[id(command)] = int(item["id"])
            commands.append(command)
        state, consumed = settle(self.state_of(row), now_real, commands)
        ids = [id_of[id(command)] for command in consumed if id(command) in id_of]
        if ids or self.state_of(row) != state:
            self.store.clock_put(
                {
                    **row,
                    "base_real": state.base_real,
                    "base_world": state.base_world,
                    "rate": state.rate,
                    "high_water_real": max(float(row["high_water_real"]), state.high_water_real),
                }
            )
        return state, ids

    # ---------- 倍率 ----------

    def set_rate(self, instance_id: str, timeline_id: str, *, rate: int, now_real: float) -> dict[str, Any]:
        """倍率调整：权威输入时刻 → 严格晚于它的第一个自然整秒生效；先持久化再确认（§2.3）。"""
        _, timeline = self._rows(instance_id, timeline_id)
        if timeline["state"] != "active":
            raise RuntimeStateError("冻结线不接受倍率调整（需先激活）")
        rate = self._check_rate(rate)
        row = self.clock_row(timeline_id)
        state, consumed = self._settle_due(timeline_id, row, now_real)
        self.store.rate_apply(consumed)
        if int(row["rate"]) == rate and not self.store.rate_pending(timeline_id):
            return {"changed": False, "rate": rate, "effective_real": None, "command_id": None}
        effective = natural_second(now_real)
        pending = self.store.rate_pending(timeline_id)
        same = next(
            (
                item
                for item in pending
                if int(item["effective_real"]) == effective and int(item["rate"]) == rate
            ),
            None,
        )
        if same is not None:  # 重试同一请求：不产生第二次变更
            return {
                "changed": True,
                "rate": rate,
                "effective_real": effective,
                "command_id": int(same["id"]),
                "duplicate": True,
            }
        seq = 1 + max([int(item["seq"]) for item in pending], default=0)
        command_id = self.store.rate_add(
            timeline_id, input_real=float(now_real), effective_real=effective, rate=rate, seq=seq
        )
        return {
            "changed": True,
            "rate": rate,
            "effective_real": effective,
            "command_id": command_id,
            "rate_at_effect": int(state.rate),
        }

    # ---------- 视图 ----------

    def view(self, instance_id: str, timeline_id: str, *, now_real: float) -> dict[str, Any]:
        """管理面 / 通道可见的时钟视图：只有当前查看且激活的线才给时钟（§3）。"""
        instance, timeline = self._rows(instance_id, timeline_id)
        calendar = self.calendar(instance)
        row = self.clock_row(timeline_id)
        state = self.state_of(row)
        if timeline["state"] != "active":
            return {
                "instance": instance_id,
                "timeline": timeline_id,
                "state": "frozen",
                "processed_world": int(row["processed_world"]),
                "label": "已冻结",
            }
        # 视图按「已到期的倍率命令已生效」投影，但不落库（写入发生在 activate / advance / freeze）
        pending = [
            RateCommand(
                input_real=float(item["input_real"]),
                effective_real=int(item["effective_real"]),
                rate=int(item["rate"]),
                seq=int(item["seq"]),
            )
            for item in self.store.rate_pending(timeline_id)
        ]
        state, _ = settle(state, now_real, pending)
        world = target_world(state, now_real)
        return {
            "instance": instance_id,
            "timeline": timeline_id,
            "state": "active",
            "world_seconds": world,
            "calendar": calendar.to_calendar(world),
            "label": calendar.describe(world),
            "rate": state.rate,
            "processed_world": int(row["processed_world"]),
            "catching_up": int(row["processed_world"]) < world,
            "clock": describe(state, now_real),
        }

    # ---------- 水位推进 ----------

    def advance(
        self, instance_id: str, timeline_id: str, *, now_real: float, max_batches: int | None = None
    ) -> dict[str, Any]:
        """把水位从已处理时刻推进到目标时刻，按世界日分批、每批原子（§2.6）。"""
        instance, timeline = self._rows(instance_id, timeline_id)
        if timeline["state"] != "active":
            return {"state": "frozen", "processed_world": int(self.clock_row(timeline_id)["processed_world"])}
        row = self.clock_row(timeline_id)
        state, consumed = self._settle_due(timeline_id, row, now_real)
        self.store.rate_apply(consumed)
        row = self.clock_row(timeline_id)
        target = target_world(state, now_real)
        processed = int(row["processed_world"])
        if processed > target:
            # 附录 B #20：处理水位超过合法目标另记为一致性错误，不静默回退、不假装追平
            log.error(
                "watermark ahead of target timeline=%s processed=%s target=%s", timeline_id, processed, target
            )
            return {"state": "inconsistent", "processed_world": processed, "target": target, "batches": 0}
        if target <= processed:
            if int(row.get("catching_up") or 0) or int(row.get("limited") or 0):
                self.store.clock_put({**row, "catching_up": 0, "limited": 0})
            return {"state": "current", "processed_world": processed, "target": target, "batches": 0}
        calendar = self.calendar(instance)
        cards = self.cards(instance, timeline_id=timeline_id, world_seconds=processed)
        generation = int(row["generation"])
        # 追赶受限（§2.6）：滞后超过预算即进入，停止扩大目标、只按已完成水位回答，不跳过事实效果
        limited = (target - processed) > self.catch_up_lag_seconds
        budget = self.catch_up_batches if max_batches is None else max(1, int(max_batches))
        produced = 0
        batches = 0
        while processed < target and batches < budget:
            day = calendar.day_index(processed)
            stop = min(target, (day + 1) * calendar.day_seconds)
            plans, units, experiences = self._collect_batch(
                instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            world_rows = self._world_event_rows(
                instance, instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            death_rows = self._death_rows(
                instance, instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            environment_rows = self._environment_rows(
                instance,
                instance_id,
                timeline_id,
                calendar,
                world_rows["effects"] + [dict(item) for item in []],
                from_world=processed,
                to_world=stop,
            )
            intent_rows = self._revise_intents(
                instance, instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            spread = self._propagate_and_clear(
                instance, instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            committed = self.store.apply_runtime_batch(
                timeline_id=timeline_id,
                generation=generation,
                processed_world=stop,
                catching_up=stop < target,
                limited=limited,
                plans=plans,
                units=units,
                experiences=experiences + intent_rows['experiences'],
                events=world_rows['events'] + intent_rows['events'] + death_rows['events'],
                claims=world_rows['claims'] + death_rows['claims'],
                knowledge=world_rows['knowledge'] + death_rows['knowledge'] + spread['knowledge'],
                effects=world_rows['effects'] + intent_rows['effects'],
                intents=intent_rows['intents'],
                environment=environment_rows,
                clear_effects=spread['clear_effects'],
            )
            if not committed:
                # 世代已变（冻结 / 重启后迟到）或水位已被别的批次推过：本批整批不落盘
                return {
                    "state": "stale",
                    "processed_world": int(self.clock_row(timeline_id)["processed_world"]),
                    "target": target,
                    "batches": batches,
                }
            processed = stop
            batches += 1
            produced += len(experiences)
            # 记忆按世界时长衰减（§六）：冻结期间不推进即不衰减，幂等
            self.decay_memories(timeline_id, to_world=stop)
            self.apply_due_pending_events(instance_id, timeline_id, to_world=stop)
        return {
            "state": "current" if processed >= target else "catching_up",
            "processed_world": processed,
            "target": target,
            "batches": batches,
            "experiences": produced,
            "limited": limited and processed < target,
        }

    def _collect_batch(
        self,
        instance_id: str,
        timeline_id: str,
        cards: list[dict[str, Any]],
        calendar: Calendar,
        *,
        from_world: int,
        to_world: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """收集一批事实转移（不落盘）：事件与后果、次日计划、单元衰减、已完成窗口的经历。"""
        plans: list[dict[str, Any]] = []
        units: list[dict[str, Any]] = []
        experiences: list[dict[str, Any]] = []
        day_seconds = calendar.day_seconds
        dead = {
            str((card.get("meta") or {}).get("card_id"))
            for card in cards
            if events.is_dead(
                instance_id,
                timeline_id,
                str((card.get("meta") or {}).get("card_id") or ""),
                self.store.event_window(instance_id, timeline_id, until=to_world, limit=400),
            )
        }
        for card in cards:
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id or character_id in dead:
                continue  # 已身故的角色不再产生计划与经历
            # 当前日与下一日的计划（世界日界由历法决定，不由入睡重新定义）
            for day_index in {calendar.day_index(from_world), calendar.day_index(to_world)}:
                if self.store.plan_get(instance_id, timeline_id, character_id, day_index) is None:
                    plans.append(
                        life.expand_plan(
                            card,
                            calendar,
                            day_index=day_index,
                            instance_id=instance_id,
                            timeline_id=timeline_id,
                            created_world=from_world,
                        )
                    )
            rows = self.store.unit_list(instance_id, timeline_id, character_id)
            if rows:
                units.extend(
                    personality.apply_time(
                        rows, from_world=from_world, to_world=to_world, day_seconds=day_seconds
                    )
                )
            # 收割昨日与今日的计划：跨日窗口属于昨日，其尾部落在今日（§11 附录 B #7）
            day_here = calendar.day_index(from_world)
            constraints = self.store.effect_window(
                instance_id,
                timeline_id,
                until=to_world,
                targets=[character_id, str(card.get("role_id") or ""), str((card.get("identity") or {}).get("region") or "")],
            )
            note = life.effect_note(
                constraints,
                character_id,
                str(card.get("role_id") or ""),
                str((card.get("identity") or {}).get("region") or ""),
            )
            for day_index in (day_here - 1, day_here):
                plan = self.store.plan_get(instance_id, timeline_id, character_id, day_index)
                if plan is None:
                    continue
                try:
                    windows = json.loads(str(plan["windows"])).get("windows", [])
                except json.JSONDecodeError:
                    continue
                items = self._harvest(instance_id, timeline_id, character_id, calendar, windows, from_world, to_world)
                if note:
                    for item in items:
                        item["summary"] = f"{item['summary']}（受影响的后果：{note}）"
                        item["source_ref"] = constraints[0]["id"] if constraints else None
                experiences.extend(items)
        return plans, units, experiences

    def _environment_rows(
        self,
        instance: dict[str, Any],
        instance_id: str,
        timeline_id: str,
        calendar: Calendar,
        effects: list[dict[str, Any]],
        *,
        from_world: int,
        to_world: int,
    ) -> list[dict[str, Any]]:
        """环境状态推进（§11.2）：声明的自然变化 + 本批事件里的环境效果，同一批提交。"""
        package = self.setting(instance)["world_package"]
        types = environment.env_types(package)
        if not types:
            return []
        rows = self.store.environment_list(instance_id, timeline_id)
        if not rows:
            rows = environment.initial_rows(
                package, instance_id=instance_id, timeline_id=timeline_id, world_seconds=from_world
            )
        changed: dict[str, dict[str, Any]] = {}
        for row in environment.advance_rows(
            rows, types, from_world=from_world, to_world=to_world, day_seconds=calendar.day_seconds
        ):
            changed[str(row["type_id"])] = row
        for row in environment.apply_effects(rows, effects, types, world_seconds=to_world):
            changed[str(row["type_id"])] = row
        return list(changed.values())

    def _revise_intents(
        self,
        instance: dict[str, Any],
        instance_id: str,
        timeline_id: str,
        cards: list[dict[str, Any]],
        calendar: Calendar,
        *,
        from_world: int,
        to_world: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """打算重议与受支持行动（§11.3）：条件不足就等，窗口过了先延期后放弃，条件满足才提交事件。"""
        rows: dict[str, list[dict[str, Any]]] = {"intents": [], "events": [], "effects": [], "experiences": []}
        known_events = self.store.event_ids(instance_id, timeline_id)
        active_effects = self.store.effect_window(instance_id, timeline_id, until=to_world)
        for card in cards:
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id:
                continue
            dead = events.is_dead(
                instance_id,
                timeline_id,
                character_id,
                self.store.event_window(instance_id, timeline_id, until=to_world, limit=400),
            )
            for row in self.store.intent_list(instance_id, timeline_id, character_id):
                if str(row["stage"]) in ("done", "abandoned"):
                    continue
                if dead:
                    rows["intents"].append(
                        {**row, "stage": "abandoned", "note": "她已不在了", "updated_world": int(to_world)}
                    )
                    continue
                decision = intents.decide(
                    row,
                    world_seconds=to_world,
                    events_present=known_events,
                    active_effects=active_effects,
                )
                if decision == "keep":
                    continue
                updated = {**row, "updated_world": int(to_world)}
                if decision == "wait":
                    _, effect = intents.parse(row)
                    blocked = intents.blockers(row, active_effects)
                    reasons = []
                    unmet = events.unmet_preconditions(
                        intents.parse(row)[0], events=known_events, effects=set()
                    )
                    if unmet:
                        reasons.append(f"条件未到：{', '.join(unmet)}")
                    if blocked:
                        reasons.append(f"受阻于：{', '.join(str(item['kind']) for item in blocked)}")
                    note = "；".join(reasons) or str(row.get("note") or "")
                    if str(row["stage"]) != "waiting" or note != str(row.get("note") or ""):
                        updated["stage"] = "waiting"
                        updated["note"] = note
                        rows["intents"].append(updated)
                    continue
                if decision == "defer":
                    updated["stage"] = "deferred"
                    updated["note"] = determined_note = "目标时间窗已过且条件未满足：延期"
                    rows["intents"].append(updated)
                    _ = determined_note
                    continue
                if decision == "abandon":
                    updated["stage"] = "abandoned"
                    updated["note"] = "延期后仍未满足条件：放弃"
                    rows["intents"].append(updated)
                    continue
                if decision == "act":
                    _, effect = intents.parse(row)
                    action = intents.action_event(
                        row,
                        instance_id=instance_id,
                        timeline_id=timeline_id,
                        world_seconds=int(to_world),
                        calendar=calendar,
                    )
                    action.pop("_calendar", None)
                    rows["events"].append(action)
                    known_events.add(str(action["id"]))
                    if effect:
                        rows["effects"].extend(
                            events.effect_rows(
                                {"effects": [effect]},
                                instance_id=instance_id,
                                timeline_id=timeline_id,
                                event_ident=str(action["id"]),
                                world_seconds=int(to_world),
                                family="",
                            )
                        )
                    rows["experiences"].append(
                        {
                            "id": f"xp-act-{character_id}-{row['id']}",
                            "instance_id": instance_id,
                            "timeline_id": timeline_id,
                            "character_id": character_id,
                            "world_seconds": int(to_world),
                            "kind": "action",
                            "summary": f"自己动手了：{row.get('object')}",
                            "source_ref": str(row["id"]),
                            "confidence": "experienced",
                        }
                    )
                    updated["stage"] = "done"
                    updated["note"] = "已按受支持的行动效果提交事件"
                    rows["intents"].append(updated)
        return rows

    def _world_event_rows(
        self,
        instance: dict[str, Any],
        instance_id: str,
        timeline_id: str,
        cards: list[dict[str, Any]],
        calendar: Calendar,
        *,
        from_world: int,
        to_world: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """当日世界级事件：固定节庆先占名额 → 随机候选 → 前置条件（§3.1、§四）。

        事件只在**已推进到的那一段**落成：当日更晚的时刻留到下一批再判，重算不重抽。
        """
        out: dict[str, list[dict[str, Any]]] = {"events": [], "claims": [], "knowledge": [], "effects": []}
        package = self.setting(instance)["world_package"]
        seed, rules = self.seed_of(instance), self.rules_of(instance)
        day_index = calendar.day_index(from_world)
        known_events = self.store.event_ids(instance_id, timeline_id)
        known_effects = self.store.effect_active_ids(instance_id, timeline_id)
        for candidate in events.plan_day(
            package,
            seed=seed,
            rules_version=rules,
            day_index=day_index,
            calendar=calendar,
            events=known_events,
            effects=known_effects,
        ):
            ident = events.event_id(seed, rules, day_index, str(candidate["slot"]))
            at = events.event_moment(seed, rules, day_index, str(candidate["slot"]), calendar.day_seconds)
            if ident in known_events or at > to_world:
                continue
            row = {
                "id": ident,
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "world_seconds": at,
                "seq": int(events.stable_key(ident)[:6], 16),
                "kind": "world",
                "family": str(candidate["family"]),
                "template": str(candidate["template"]),
                "source": "engine",
                "summary": str(candidate["summary"]),
                "detail": events.detail_text(candidate),
                "text_source": "template",
                "effects": events.as_json(candidate["effects"]),
                "share_value": 1 if candidate.get("fixed") else 0,
                "importance": 0.6 if candidate.get("fixed") else 0.4,
                "created_real": 0.0,
            }
            if not events.share_qualified(row):
                row["importance"] = 0.5
            out["events"].append(row)
            out["effects"].extend(
                events.effect_rows(
                    candidate,
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    event_ident=ident,
                    world_seconds=at,
                    family=str(candidate["family"]),
                )
            )
            claims = events.dump_rows(
                events.claim_rows(
                    candidate,
                    package=package,
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    event_ident=ident,
                    world_seconds=at,
                    calendar=calendar,
                )
            )
            out["claims"].extend(claims)
            for card in cards:
                out["knowledge"].extend(events.grants(row, claims, card, world_seconds=at, calendar=calendar))
        return out

    def _death_rows(
        self,
        instance: dict[str, Any],
        instance_id: str,
        timeline_id: str,
        cards: list[dict[str, Any]],
        calendar: Calendar,
        *,
        from_world: int,
        to_world: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """寿终事件（§四）：由寿命模型与世界时刻推出，单独记账、可产生死讯说法。"""
        out: dict[str, list[dict[str, Any]]] = {"events": [], "claims": [], "knowledge": []}
        package = self.setting(instance)["world_package"]
        known = self.store.event_window(instance_id, timeline_id, until=to_world, limit=400)
        for card in cards:
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id or events.is_dead(instance_id, timeline_id, character_id, known):
                continue
            moment = events.death_moment(card, package, calendar)
            if moment is None or not (from_world < moment <= to_world):
                continue
            row = events.death_event(
                card,
                instance_id=instance_id,
                timeline_id=timeline_id,
                world_seconds=moment,
                calendar=calendar,
                seed=self.seed_of(instance),
            )
            row.pop("_seed", None)
            out["events"].append(row)
            claims = events.dump_rows(
                events.claim_rows(
                    {"summary": row["summary"], "effects": []},
                    package=package,
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    event_ident=str(row["id"]),
                    world_seconds=moment,
                    calendar=calendar,
                )
            )
            out["claims"].extend(claims)
            for other in cards:
                if str((other.get("meta") or {}).get("card_id")) == character_id:
                    continue  # 死者不需要自己的死讯
                out["knowledge"].extend(
                    events.grants(row, claims, other, world_seconds=moment, calendar=calendar)
                )
        return out

    def _propagate_and_clear(
        self,
        instance: dict[str, Any],
        instance_id: str,
        timeline_id: str,
        cards: list[dict[str, Any]],
        calendar: Calendar,
        *,
        from_world: int,
        to_world: int,
    ) -> dict[str, list[Any]]:
        """传播到达的获知（§五）与后果的解除（§六）。"""
        rows: dict[str, list[Any]] = {"knowledge": [], "clear_effects": []}
        for claim in self.store.claim_list(instance_id, timeline_id):
            earliest = int(claim.get("earliest_world") or 0)
            if not (from_world < earliest <= to_world):
                continue
            for card in cards:
                grant = events.claim_grant(claim, card, world_seconds=earliest)
                if grant is not None:
                    rows["knowledge"].append(grant)
        day_seconds = calendar.day_seconds
        # 同族的后续事件（自然恢复的判定依据）：按世界时刻排一次即可
        later_by_family: dict[str, int] = {}
        for item in self.store.event_window(instance_id, timeline_id, until=to_world, limit=200):
            family = str(item.get("family") or "")
            if family:
                later_by_family[family] = max(later_by_family.get(family, 0), int(item["world_seconds"]))
        for effect in self.store.effect_window(instance_id, timeline_id, until=to_world):
            expiry = str(effect.get("expiry"))
            started = int(effect["from_world"])
            if expiry == "with_cause" and started + day_seconds <= to_world:
                rows["clear_effects"].append((str(effect["id"]), instance_id))
            elif expiry == "natural_recovery":
                family = str(effect.get("family") or "")
                # 同族在该后果之后仍有新事件 → 声明的自然条件成立；没有依据就保持有效
                if family and later_by_family.get(family, 0) > started:
                    rows["clear_effects"].append((str(effect["id"]), instance_id))
        _ = instance
        return rows

    def _harvest(
        self,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        calendar: Calendar,
        windows: list[dict[str, Any]],
        from_world: int,
        to_world: int,
    ) -> list[dict[str, Any]]:
        """把本批内已结束的活动窗记为经历（幂等：同一窗口的 id 固定）。"""
        out: list[dict[str, Any]] = []
        for window in windows:
            if not (from_world < int(window["end"]) <= to_world):
                continue
            out.append(
                {
                    "id": f"xp-{character_id}-{int(window['start'])}",
                    "instance_id": instance_id,
                    "timeline_id": timeline_id,
                    "character_id": character_id,
                    "world_seconds": int(window["end"]),
                    "kind": "life",
                    "summary": f"{window.get('activity') or 'activity'}（{calendar.describe(int(window['start']))}）",
                    "source_ref": None,
                    "confidence": "experienced",
                }
            )
        return out

    # ---------- 角色状态 ----------

    def bootstrap(self, instance_id: str, timeline_id: str, *, world_seconds: int) -> dict[str, int]:
        """创建 / 导入后把角色卡的初始单元、首日计划落进运行层。"""
        instance, _ = self._rows(instance_id, timeline_id)
        calendar = self.calendar(instance)
        units = plans = 0
        intents_created = 0
        for card in self.cards(instance, timeline_id=timeline_id, world_seconds=world_seconds):
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id:
                continue
            if not self.store.unit_list(instance_id, timeline_id, character_id):
                for row in personality.initial_rows(
                    card, instance_id=instance_id, timeline_id=timeline_id, world_seconds=world_seconds
                ):
                    self.store.unit_put(row)
                    units += 1
            if not self.store.environment_list(instance_id, timeline_id):
                for row in environment.initial_rows(
                    self.setting(instance)["world_package"],
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    world_seconds=world_seconds,
                ):
                    self.store.environment_put(row)
            for row in intents.initial_rows(
                card, instance_id=instance_id, timeline_id=timeline_id, world_seconds=world_seconds
            ):
                if not self.store.intent_list(instance_id, timeline_id, character_id):
                    self.store.intent_put(row)
                    intents_created += 1
            day_index = calendar.day_index(world_seconds)
            if self.store.plan_get(instance_id, timeline_id, character_id, day_index) is None:
                self.store.plan_put(
                    life.expand_plan(
                        card,
                        calendar,
                        day_index=day_index,
                        instance_id=instance_id,
                        timeline_id=timeline_id,
                        created_world=world_seconds,
                    )
                )
                plans += 1
        return {"units": units, "plans": plans}

    def character_snapshot(
        self, instance_id: str, timeline_id: str, character_id: str, *, world_seconds: int
    ) -> dict[str, Any]:
        """内部读取（会话 / 认知用）：单元与计划的最新视图，不对外暴露数值面板。"""
        units = self.store.unit_list(instance_id, timeline_id, character_id)
        plan = self.store.plan_latest(instance_id, timeline_id, character_id)
        experiences = self.store.experience_window(
            instance_id, timeline_id, character_id, until=world_seconds, limit=12
        )
        knowledge = self.store.knowledge_window(
            instance_id, timeline_id, character_id, until=world_seconds, limit=20
        )
        card = self.card_of(
            self.store.instance_get(instance_id) or {},
            character_id,
            timeline_id=timeline_id,
            world_seconds=world_seconds,
        )
        effects = self.store.effect_window(
            instance_id,
            timeline_id,
            until=world_seconds,
            targets=[
                character_id,
                str(card.get("role_id") or ""),
                str((card.get("identity") or {}).get("region") or ""),
            ],
        )
        activity = life.activity_label(life.current_window(plan, world_seconds))
        note = life.effect_note(
            effects,
            character_id,
            str(card.get("role_id") or ""),
            str((card.get("identity") or {}).get("region") or ""),
        )
        if note and activity:
            activity = f"{activity}（受影响的后果：{note}）"
        return {
            "units": personality.visible(units),
            "all_units": units,
            "plan": plan,
            "current_activity": activity,
            "experiences": experiences,
            "knowledge": knowledge,
            "effects": effects,
            "intents": [
                row
                for row in self.store.intent_list(instance_id, timeline_id, character_id)
                if str(row["stage"]) in ("adopted", "waiting", "deferred")
            ],
            "observations": environment.observations(
                self.store.environment_list(instance_id, timeline_id),
                environment.env_types(self.setting(self.store.instance_get(instance_id) or {}).get("world_package", {})),
                card,
                world_seconds=world_seconds,
            ),
        }

    def backfill(self, instance_id: str, timeline_id: str) -> int:
        """历史回填（§3.3）：把包内既定的史料与初始事实落成历史条目，不施加效果、不产生获知。"""
        instance, _ = self._rows(instance_id, timeline_id)
        if self.store.event_ids(instance_id, timeline_id):
            return 0
        rows, claims = events.backfill_rows(
            self.setting(instance)["world_package"],
            instance_id=instance_id,
            timeline_id=timeline_id,
            seed=self.seed_of(instance),
            rules_version=self.rules_of(instance),
        )
        if not rows:
            return 0
        return self.store.runtime_load(
            instance_id, timeline_id, {"watermark": 0, "events": rows, "claims": claims}
        )

    def ensure_instance(self, instance_id: str, *, now_real: float) -> dict[str, int]:
        """补齐运行层状态：缺时钟的时间线建时钟，缺角色状态的按初始水位补齐（幂等）。"""
        instance = self.store.instance_get(instance_id)
        if instance is None:
            raise RuntimeStateError(f"实例不存在：{instance_id}")
        clocks = 0
        for timeline in self.store.timeline_list(instance_id):
            self.backfill(instance_id, timeline["id"])  # 幂等：已有条目即跳过
            row = self.store.clock_get(timeline["id"])
            if row is None:
                row = self.init_timeline(
                    instance_id, timeline["id"], moment=int(instance["moment"]), now_real=now_real
                )
                clocks += 1
            self.bootstrap(
                instance_id,
                timeline["id"],
                world_seconds=max(int(instance["moment"]), int(row["processed_world"])),
            )
        return {"timelines": len(self.store.timeline_list(instance_id)), "created_clocks": clocks}

    def active_timelines(self) -> list[tuple[str, str]]:
        """所有处于激活状态的时间线（实例, 时间线）。"""
        pairs = []
        for instance in self.store.instance_list():
            for timeline in self.store.timeline_list(instance["id"]):
                if timeline["state"] == "active":
                    pairs.append((instance["id"], timeline["id"]))
        return pairs

    def catch_up_all(self, *, now_real: float, max_batches: int = 8) -> dict[str, Any]:
        """推进所有激活线（核心启动与周期 tick 用；冻结线自动跳过）。"""
        advanced: dict[str, Any] = {}
        for instance_id, timeline_id in self.active_timelines():
            try:
                advanced[timeline_id] = self.advance(
                    instance_id, timeline_id, now_real=now_real, max_batches=max_batches
                )
            except Exception:  # 单线推进失败不影响其他线
                log.exception("advance failed timeline=%s", timeline_id)
        return advanced

    # ---------- 性格驱动入口 ----------

    def drive_unit(
        self,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        *,
        driver: str,
        semantic: str,
        basis: str,
        strength: float,
        direction: int,
        source_key: str,
        world_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        """把一次驱动落到单元上（§10.1）：来源键去重、变化连续、迁移隐式。

        - 同一来源只消费一次：重试、补算、重新载入都返回 None，不重复强化（§10.3）；
        - 已有同义单元按其累积稳定度调整，后来的弱驱动不会把它裁到本次驱动的生成上限；
        - 驱动水位不得超过已完成水位（不能拿尚未发生的影响改角色）。

        阶段 2 的调用方：对话驱动由会话层整理后调用（§10.1 的「AI 命名 + 确定性数值」分界），
        事件驱动由阶段 3 的事件引擎调用；本方法只做落库与规则，不反问模型。
        """
        if driver not in personality.MODES:
            raise RuntimeStateError(f"未知驱动：{driver}")
        watermark = int(self.clock_row(timeline_id)["processed_world"])
        at = watermark if world_seconds is None else int(world_seconds)
        if at > watermark:
            raise RuntimeStateError("驱动水位不能超过已完成水位")
        rows = self.store.unit_list(instance_id, timeline_id, character_id)
        if any(personality.has_consumed(row, source_key) for row in rows):
            return None
        updated = personality.apply_drive(
            rows,
            mode=driver,
            source_key=source_key,
            semantic=semantic or None,
            strength=float(strength),
            positive=direction >= 0,
            world_seconds=at,
            basis=basis,
            identity={
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "character_id": character_id,
            },
        )
        touched = next(
            (
                row
                for row in updated
                if str(row.get("semantic")) == semantic and personality.has_consumed(row, source_key)
            ),
            None,
        )
        for row in updated:
            if personality.has_consumed(row, source_key):
                self.store.unit_put(row)
        return touched

    # ---------- 角色自主生成打算（§11.3，需模型） ----------

    async def propose_intents(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        llm: Any,
        now_real: float,
    ) -> dict[str, Any]:
        """后台替在世角色想一步：闭集校验 + 可知目标 + 预算记账，失败就当没想过。

        只有在世、未竟之事未满、且距离上次提案超过一个世界日的角色才提案。
        """
        from ..log import get_logger

        log = get_logger("isekai.runtime.planning")
        instance, _ = self._rows(instance_id, timeline_id)
        timeline = next(item for item in self.store.timeline_list(instance_id) if item["id"] == timeline_id)
        if timeline["state"] != "active":
            return {"proposed": 0, "reason": "frozen"}
        row = self.clock_row(timeline_id)
        watermark = int(row["processed_world"])
        calendar = self.calendar(instance)
        package = self.setting(instance)["world_package"]
        known_events = self.store.event_window(instance_id, timeline_id, until=watermark, limit=400)
        budget_bucket = int(now_real // 86400)
        remaining = int(self.render_calls_per_day)
        proposed: list[dict[str, Any]] = []
        paused = False
        blocked: list[str] = []
        for card in self.cards(instance, timeline_id=timeline_id, world_seconds=watermark):
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id or events.is_dead(instance_id, timeline_id, character_id, known_events):
                continue
            live = [
                item
                for item in self.store.intent_list(instance_id, timeline_id, character_id)
                if str(item["stage"]) in ("adopted", "waiting")
            ]
            if len(live) >= planning.MAX_LIVE_INTENTS:
                continue
            last = max((int(item["updated_world"]) for item in live), default=0)
            if last and watermark - last < calendar.day_seconds:
                continue  # 同一世界日内不重复提案
            used = self.store.call_ledger_get(instance_id, timeline_id, "intent_propose", bucket=budget_bucket)
            _ = (used, remaining)
            snapshot = self.character_snapshot(
                instance_id, timeline_id, character_id, world_seconds=watermark
            )
            allowed = planning.allowed_targets(
                package,
                card,
                knowledge=snapshot["knowledge"],
                observations=snapshot["observations"],
            )
            if not any(allowed.values()):
                continue  # 没有她可知的目标可指，就不提案
            messages = planning.prompt(
                name=str((card.get("identity") or {}).get("name") or ""),
                occupation=str((card.get("identity") or {}).get("occupation") or ""),
                world_label=calendar.describe(watermark),
                aims=snapshot["intents"],
                knowledge=snapshot["knowledge"],
                effects=self.store.effect_window(
                    instance_id,
                    timeline_id,
                    until=watermark,
                    targets=[character_id, str(card.get("role_id") or "")],
                ),
                observations=snapshot["observations"],
                allowed=allowed,
            )
            prompt_text = "\n".join(item["content"] for item in messages)
            reservation = self.reserve_call(
                instance_id, timeline_id, "intent_propose", prompt_text=prompt_text, now_real=now_real
            )
            if not reservation.get("ok"):
                log.info("intent proposal paused line=%s blocked=%s", timeline_id, reservation.get("blocked"))
                paused = True
                blocked = list(reservation.get("blocked") or [])
                continue
            try:
                text = await llm.chat(messages, temperature=0.7, timeout=60.0)
            except Exception:  # 模型不可用不该影响世界推进
                log.exception("intent proposal failed character=%s", character_id)
                self.settle_call(reservation, prompt_text=prompt_text, outcome="error")
                continue
            self.settle_call(reservation, prompt_text=prompt_text, reply=text)
            decision = planning.parse(text, allowed)
            if decision is None:
                continue
            fresh = self.clock_row(timeline_id)  # 提案期间世界可能已推进：按最新水位与世代提交
            watermark = int(fresh["processed_world"])
            ident = f"in-auto-{events.stable_key(instance_id, timeline_id, character_id, watermark)[:10]}"
            applied = self.store.apply_runtime_batch(
                timeline_id=timeline_id,
                generation=int(fresh["generation"]),
                processed_world=watermark,
                catching_up=False,
                intents=[
                    {
                        "id": ident,
                        "instance_id": instance_id,
                        "timeline_id": timeline_id,
                        "character_id": character_id,
                        "object": decision["object"],
                        "basis": decision["basis"],
                        "strength": decision["strength"],
                        "window_from": watermark,
                        "window_to": watermark + 7 * calendar.day_seconds,
                        "preconditions": "[]",
                        "effect": json.dumps(decision["effect"], ensure_ascii=False),
                        "stage": "adopted",
                        "note": "",
                        "source_world": watermark,
                        "updated_world": watermark,
                    }
                ],
            )
            if not applied:
                log.info("intent proposal discarded (stale batch) character=%s", character_id)
                continue
            proposed.append({"character": character_id, "intent": ident, **decision})
            if len(proposed) >= 8:
                break
        return {
            "proposed": len(proposed),
            "items": proposed,
            "budget": {
                "paused": paused,
                "blocked": blocked,
                "calls": self.store.call_ledger_get(instance_id, timeline_id, "intent_propose", bucket=budget_bucket),
                "limit": remaining,
            },
        }

    # ---------- 补卡（角色集合扩充） ----------

    def add_character(
        self,
        instance_id: str,
        timeline_id: str,
        card: dict[str, Any],
        *,
        now_real: float,
        joined_world: int | None = None,
        note: str = "",
        acquainted: bool = False,
    ) -> dict[str, Any]:
        """把角色锚定补入该线（§九 / 附录 B #18）。

        - 新角色一直存在：卡片自带个人史，补入只决定她自哪一刻起出现在本线；
        - 个人史与初始知识按同一认知契约投影（卡片先在设定层过校验，此处只做锚定）；
        - 补入不激活该线：冻结线锚定冻结时刻（调用方随后自行决定是否激活）；
        - 加入点之前的经历与水位不变，回滚跨越加入点即一致退出（回滚属阶段 4）。
        """
        instance, timeline = self._rows(instance_id, timeline_id)
        row = self.clock_row(timeline_id)
        watermark = int(row["processed_world"])
        if joined_world is None:
            joined_world = (
                watermark
                if timeline["state"] == "active"
                else target_world(self.state_of(row), float(row["anchor_real"]))
            )
        joined_world = int(joined_world)
        if joined_world > watermark:
            raise RuntimeStateError("补入时刻不能晚于已完成水位（不能从尚未发生的时刻开始）")
        if joined_world < int(instance["moment"]):
            raise RuntimeStateError("补入时刻不能早于实例初始时刻")
        character_id = str((card.get("meta") or {}).get("card_id") or "")
        if not character_id:
            raise RuntimeStateError("角色卡缺少 card_id")
        if int((card.get("identity") or {}).get("born") or 0) > joined_world:
            raise RuntimeStateError("补入时刻早于角色出生时刻")
        existing = {
            str((item.get("meta") or {}).get("card_id"))
            for item in self.cards(instance, timeline_id=timeline_id, world_seconds=watermark)
        }
        if character_id in existing:
            raise RuntimeStateError(f"该角色已在本线：{character_id}")
        from ..world.cards import validate_card  # 局部导入：设定层与运行层不互相依赖

        errors = validate_card(card, self.setting(instance)["world_package"], moment=joined_world)
        if not bool((card.get("meta") or {}).get("confirmed")):
            errors.append("meta: 角色卡未确认，不能补入")
        if errors:
            raise RuntimeStateError("补入校验未通过：" + "；".join(str(item) for item in errors[:6]))
        calendar = self.calendar(instance)
        self.store.character_join_add(
            {
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "character_id": character_id,
                "joined_world": joined_world,
                "card": json.dumps(card, ensure_ascii=False, sort_keys=True),
                "note": note,
                "acquainted": 1 if acquainted else 0,
                "created_real": float(now_real),
            }
        )
        units = personality.initial_rows(
            card, instance_id=instance_id, timeline_id=timeline_id, world_seconds=joined_world
        )
        if acquainted:  # 「已相识」声明：只补一条对话单元，不改任何既有角色状态
            units.append(
                {
                    "id": "join-acquainted",
                    "instance_id": instance_id,
                    "timeline_id": timeline_id,
                    "character_id": character_id,
                    "mode": "dialog",
                    "semantic": "与联络者已相识",
                    "basis": "补卡时的已相识声明",
                    "confidence": 0.6,
                    "stability": 0.0,
                    "archived": 0,
                    "consumed": "[]",
                    "updated_world": joined_world,
                }
            )
        for unit in units:
            self.store.unit_put(unit)
        self.store.plan_put(
            life.expand_plan(
                card,
                calendar,
                day_index=calendar.day_index(joined_world),
                instance_id=instance_id,
                timeline_id=timeline_id,
                created_world=joined_world,
            )
        )
        return {
            "instance": instance_id,
            "timeline": timeline_id,
            "character": character_id,
            "name": str((card.get("identity") or {}).get("name") or ""),
            "joined_world": joined_world,
            "joined_label": calendar.describe(joined_world),
            "units": len(units),
            "acquainted": bool(acquainted),
            "timeline_state": timeline["state"],
            "note": note,
        }

    # ---------- 会话接入 ----------

    def world_moment(self, instance_id: str, timeline_id: str, *, now_real: float | None = None) -> int:
        """可对话的世界时刻 = 已处理水位（不把未推进的目标当既有状态，§2.6）。"""
        return int(self.clock_row(timeline_id)["processed_world"])

    def card_of(
        self,
        instance: dict[str, Any],
        character_id: str,
        *,
        timeline_id: str | None = None,
        world_seconds: int | None = None,
    ) -> dict[str, Any]:
        for card in self.cards(instance, timeline_id=timeline_id, world_seconds=world_seconds):
            if str((card.get("meta") or {}).get("card_id")) == character_id:
                return card
        raise RuntimeStateError(f"实例内没有该角色：{character_id}")

    def system_prompt(
        self, session: dict[str, Any], *, topic: str | None = None, now_real: float | None = None
    ) -> str:
        """会话层用的扮演定义：锁定设定 + 该角色截至当前水位的认知切片（无实情层注入）。"""
        import time as _time

        from . import cognition

        instance, _ = self._rows(session["instance_id"], session["timeline_id"])
        calendar = self.calendar(instance)
        world = self.world_moment(session["instance_id"], session["timeline_id"], now_real=now_real or _time.time())
        card = self.card_of(
            instance,
            str(session["character_id"]),
            timeline_id=session["timeline_id"],
            world_seconds=world,
        )
        snapshot = self.character_snapshot(
            session["instance_id"], session["timeline_id"], str(session["character_id"]), world_seconds=world
        )
        context = cognition.play_context(
            self.setting(instance)["world_package"],
            card,
            world_seconds=world,
            calendar_label=calendar.describe(world),
            topic=topic,
            current_activity=str(snapshot["current_activity"] or ""),
            units=snapshot["units"],
            experiences=snapshot["experiences"],
            knowledge=snapshot["knowledge"],
            intents=snapshot["intents"],
            observations=snapshot["observations"],
        )
        return cognition.render_prompt(context)
