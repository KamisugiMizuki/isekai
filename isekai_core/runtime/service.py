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
from ..version import DEFAULT_MAX_PARTS, DEFAULT_MAX_TEXT_LEN
from . import reaction
from . import budget as budget_mod
from ..world import instances
from ..world.cards import region_of
from ..world.validate import custom_index as _custom_index, office_index as _office_index

from . import (
    change as change_mod,
    cognition,
    disclosure,
    drafts,
    embedding as embedding_mod,
    environment,
    events,
    institutions,
    intents,
    narrative,
    proactive,
    life,
)
from . import memory as memory_mod, personality, planning, versioning
from . import trpg as trpg_runtime
from .calendar import Calendar, calendar_from_package
from .clock import DEFAULT_RATE_MAX, ClockState, RateCommand, describe, natural_second, settle, target_world

log = get_logger("isekai.runtime")

#: 时间消耗的来源分类（§十四）：世界过程 / 玩家行动 / GM 裁定分开记账
TIME_CONSUME_SOURCES = ("world_process", "player_action", "gm_declaration")


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


def _reaction_rows(
    cards: list[dict[str, Any]],
    *,
    effects: list[dict[str, Any]],
    experiences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把这一批的后果与经历落成**有来源**的短期反应（§11.1）：素材没来源就不进状态。

    后果按效果的目标（角色标识 / role_id）落到对应角色；经历按角色标识落。
    """
    role_to_character = {
        str(card.get("role_id") or ""): str((card.get("meta") or {}).get("card_id") or "") for card in cards
    }
    joined = {str((card.get("meta") or {}).get("card_id") or "") for card in cards}
    out: list[dict[str, Any]] = []
    for effect in effects:
        character_id = role_to_character.get(str(effect.get("target") or ""))
        if character_id:
            out.append(reaction.from_effect(effect, character_id=character_id))
    for experience in experiences:
        character_id = str(experience.get("character_id") or "")
        if character_id and character_id in joined:
            out.append(reaction.from_experience(experience, character_id=character_id))
    return out


def _known_event_ids(
    store: Any, instance_id: str, timeline_id: str, knowledge: list[dict[str, Any]]
) -> set[str]:
    """她能触达的事件标识：直接获知的、以及她掌握的说法所归属的事件。

    制度 / 惯例的变化由事件产生；「听说这件事」与「听说了关于它的说法」都算知道。
    """
    known = {str(row.get("target") or "") for row in knowledge}
    known |= {str(row.get("id") or "") for row in knowledge}
    for claim in store.claim_list(instance_id, timeline_id):
        if str(claim.get("id") or "") in known:
            known.add(str(claim.get("event_id") or ""))
    known.discard("")
    return known



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
        memory_archived_recall_min: float = 0.82,
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
        self.memory_archived_recall_min = max(0.0, min(1.0, float(memory_archived_recall_min)))
        #: 远程 embedding（MEMORY_SPEC §5.2）：缺配置即退化全文召回
        self.embedding_model = str(memory_embedding_model or "")
        self.embedding_base_url = str(memory_embedding_base_url or "")
        self.embedding_api_key = str(memory_embedding_api_key or "")
        #: 已确认的向量维度（0 = 本次进程还没成功调用过）；同名换维度要靠它比对
        self.embedding_dim = 0
        #: 自动提交（§5.1）：默认现实 1 小时或新增事件 50 条，可配置可关；手动提交不受开关限制
        self.autocommit_enabled = bool(autocommit_enabled)
        self.autocommit_minutes = max(1, int(autocommit_minutes))
        self.autocommit_events = max(1, int(autocommit_events))
        #: TRPG 战役运行时（TRPG_CAMPAIGN_RUNTIME_SPEC）：战役编排 + 规则状态托管 + 联合提交
        self.campaign = trpg_runtime.CampaignRuntime(store, self)

    # ---------- 基础读取 ----------

    def describe_world(self, instance_id: str, world_seconds: int) -> str:
        """世界时刻的人话标签（历法视图，§2.1）。"""
        instance = self.store.instance_get(instance_id) or {}
        return self.calendar(instance).describe(int(world_seconds))

    # ---------- 多角色披露（§七，阶段 5） ----------

    def disclose(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        from_character: str,
        to_character: str,
        refs: list[str],
        note: str = "",
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """用户明确向指定角色披露指定片段（§7.1）：独立确认事务，授权先落库再允许读取。

        范围必须明确到具体消息；含糊转述不产生授权。授权不改变世界真值，也不把来源角色的
        经历变成接收角色的亲历。
        """
        import time as _time

        now = _time.time() if now_real is None else float(now_real)
        scope = disclosure.normalize_scope({
            "from_character": from_character, "to_character": to_character, "refs": refs, "note": note,
        })
        cards = {str((card.get("meta") or {}).get("card_id")): card for card in self.cards(
            self.store.instance_get(instance_id) or {}, timeline_id=timeline_id
        )}
        if scope["from_character"] not in cards:
            raise RuntimeStateError("来源角色不在本线")
        if scope["to_character"] not in cards:
            raise RuntimeStateError("接收角色不在本线")
        resolved: list[dict[str, Any]] = []
        for ref in scope["refs"]:
            row = self.store.message_by_ref(instance_id, timeline_id, ref)
            if row is None:
                raise RuntimeStateError(f"披露范围里的片段不存在：{ref}")
            session = self.store.session_get(str(row["session_id"])) or {}
            if str(session.get("character_id")) != scope["from_character"]:
                raise RuntimeStateError(f"片段不属于来源角色：{ref}")
            resolved.append({
                "ref": ref, "role": str(row["role"]), "text": self.store.message_text(row),
                "world_seconds": self.world_moment(instance_id, timeline_id),
            })
        watermark = self.world_moment(instance_id, timeline_id)
        refs_key = ",".join(sorted(item["ref"] for item in resolved))
        ident = "dc-" + events.stable_key(
            instance_id, timeline_id, scope["from_character"], scope["to_character"], refs_key
        )[:12]
        if self.store.disclosure_get(ident) is not None:
            return {"id": ident, "reused": True, "scope": resolved}
        self.store.disclosure_add({
            "id": ident, "instance_id": instance_id, "timeline_id": timeline_id,
            "from_character": scope["from_character"], "to_character": scope["to_character"],
            "scope": json.dumps({"refs": resolved, "note": scope["note"]}, ensure_ascii=False),
            "granted_world": watermark, "granted_real": now, "note": scope["note"], "state": "granted",
        })
        # 授权落库之后才允许接收角色下一轮读到（§7.1 独立确认事务）
        return {"id": ident, "reused": False, "granted_world": watermark, "scope": resolved}

    def disclosures(
        self, instance_id: str, timeline_id: str, *, to_character: str | None = None, until: int | None = None
    ) -> list[dict[str, Any]]:
        """披露清单只回管理元数据，不额外提供别的角色记忆或世界实情（DESKTOP_SPEC §6）。"""
        rows = self.store.disclosure_list(instance_id, timeline_id, to_character=to_character, until=until)
        return [
            {
                "id": str(row["id"]),
                "from_character": str(row["from_character"]),
                "to_character": str(row["to_character"]),
                "granted_world": int(row["granted_world"]),
                "note": str(row.get("note") or ""),
                "count": len((json.loads(row["scope"] or "{}").get("refs") or [])),
            }
            for row in rows
        ]

    def disclosed_fragments(
        self, instance_id: str, timeline_id: str, to_character: str, *, until: int | None = None
    ) -> list[dict[str, Any]]:
        """接收角色可读的转述片段（作用域视图，不复制来源角色的其他内容）。"""
        out: list[dict[str, Any]] = []
        for row in self.store.disclosure_list(instance_id, timeline_id, to_character=to_character, until=until):
            scope = json.loads(row["scope"] or "{}")
            speaker = self._display_name(instance_id, timeline_id, str(row["from_character"]))
            for item in scope.get("refs") or []:
                if not str(item.get("text") or "").strip():
                    continue
                out.append({
                    "disclosure_id": str(row["id"]),
                    "from_character": str(row["from_character"]),
                    "source_name": speaker,
                    "ref": str(item.get("ref") or ""),
                    "text": str(item["text"]),
                    "granted_world": int(row["granted_world"]),
                })
        return out

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
        # 制度与职位也是已登记目标：用户可借合法事件改变某职位的持有者（§八 第 3 条）
        world = package.get("world") if isinstance(package.get("world"), dict) else {}
        for item in world.get("institutions") or []:
            if not isinstance(item, dict):
                continue
            if item.get("id"):
                targets.add(str(item["id"]))
            for office in item.get("offices") or []:
                if isinstance(office, dict) and office.get("id"):
                    targets.add(str(office["id"]))
        channels = sorted(
            str(item["id"]) for item in package.get("comms", {}).get("sources", []) if isinstance(item, dict) and item.get("id")
        ) if isinstance(package.get("comms"), dict) else []
        targets.update(channels)  # 世界级渠道也是已登记来源点（docstring：效果只能指向它们）
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
        if not source_commit:
            # 预览时按「当前」定的草案：世界若已走远，含义可能变了 → 要求重新起草，不静默换分叉基础
            clock = self.clock_row(source_timeline)
            preview_world = int(draft["created_world"] or 0)
            if int(clock["processed_world"]) != preview_world:
                raise RuntimeStateError(
                    f"草案的来源点已经变化（预览在 {preview_world}，现在已到 {int(clock['processed_world'])}）："
                    "请重新起草再确认，不静默换一个分叉基础"
                )
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
        # 效果要落成真状态才算数（§八#3）：制度 / 惯例 / 环境与引擎事件走同一条效果路径，
        # 只在 effect_state 里留一行不改 institution_state，等于「接受了但什么都没发生」。
        instance = self.store.instance_get(instance_id) or {}
        institution_rows = self._institution_rows(
            instance, instance_id, timeline_id, rows["effects"], deaths=[], from_world=world, to_world=world
        )
        environment_rows = self._environment_rows(
            instance,
            instance_id,
            timeline_id,
            self.calendar(instance),
            rows["effects"],
            from_world=world,
            to_world=world,
        )
        applied = self.store.apply_runtime_batch(
            timeline_id=timeline_id, generation=int(clock["generation"]), processed_world=world,
            catching_up=False, events=[rows["event"]], claims=rows["claims"], knowledge=rows["knowledge"],
            effects=rows["effects"], environment=environment_rows,
            institution=institution_rows["institution"], customs=institution_rows["customs"],
        )
        if not applied:
            raise RuntimeStateError("注入被拒（世代已变）")
        return {"scheduled": False, "event": ident, "effects": len(rows["effects"]), "claims": len(rows["claims"])}

    def apply_external_event(
        self, instance_id: str, timeline_id: str, payload: dict[str, Any], *, source: str, action_id: str,
        resolution: dict[str, Any], now_real: float | None = None,
    ) -> dict[str, Any]:
        """Apply a rule-plugin result on the current line; the rule engine stays external."""
        instance = self.store.instance_get(instance_id)
        if instance is None:
            raise RuntimeStateError(f"实例不存在：{instance_id}")
        clock = self.clock_row(timeline_id)
        world = int(clock["processed_world"])
        ident = f"ev-{events.stable_key(instance_id, timeline_id, source, action_id)[:12]}"
        rows = self._user_event_rows(
            instance_id, timeline_id, payload, ident=ident, world=world,
            source=source, template="trpg.action",
        )
        rows["event"]["detail"] = json.dumps({"resolution": resolution}, ensure_ascii=False)
        institution_rows = self._institution_rows(
            instance, instance_id, timeline_id, rows["effects"], deaths=[], from_world=world, to_world=world
        )
        environment_rows = self._environment_rows(
            instance, instance_id, timeline_id, self.calendar(instance), rows["effects"],
            from_world=world, to_world=world,
        )
        applied = self.store.apply_runtime_batch(
            timeline_id=timeline_id, generation=int(clock["generation"]), processed_world=world,
            catching_up=False, events=[rows["event"]], claims=rows["claims"], knowledge=rows["knowledge"],
            effects=rows["effects"], environment=environment_rows,
            institution=institution_rows["institution"], customs=institution_rows["customs"],
        )
        if not applied:
            raise RuntimeStateError("规则结果写入被拒（世代已变）")
        return {"event": ident, "world": world, "effects": len(rows["effects"]), "claims": len(rows["claims"])}

    def _user_event_rows(
        self, instance_id: str, timeline_id: str, payload: dict[str, Any], *, ident: str, world: int,
        source: str = "user", template: str = "user.introduced",
    ) -> dict[str, list[dict[str, Any]]]:
        """外部输入事件的登记行：与引擎事件同形，获知交给常规传播链。"""
        event_row = {
            "instance_id": instance_id, "timeline_id": timeline_id, "id": ident, "world_seconds": world,
            "seq": 0, "kind": "world", "family": "政治", "template": template,
            "source": source, "summary": str(payload["intent"])[:200], "detail": str(payload["intent"]),
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
        # 写快照前先结算已到期的倍率命令：否则快照把「上一段」的陈旧倍率当成有效倍率固化（§2.3.4）
        row = self._settled_row(timeline_id, time.time())
        # 已结算账本行到此无读者：提交快照已把有效倍率记进 commit.rate，回滚照它跑（§七），
        # 待生效行才算控制状态 —— 故提交点是清账本的安全时点，不这样清就会随每次变更无界积行。
        self.store.rate_clear_settled(timeline_id)
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
        if not self.compatible(instance_id):
            return None  # 兼容性阻断：不推进、不落提交（§7.6）
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
        # 对话历史随分叉带到新线（§六 共同过去）：快照里的 dialog 要落到新线自己的会话上，
        # 会话身份含时间线，不能直接复用旧线行
        self._carry_dialog(instance_id, timeline_id, new_id, list(snapshot.get("dialog") or []))
        # 分支也留一个自己的提交点（带快照，回滚 / 再分叉都指得到它）
        record = self.commit(instance_id, new_id, kind="initial", note=f"分叉自 {commit_id}")
        if activate:
            self.activate(instance_id, new_id, now_real=now_real)
        return {"timeline": self.store.timeline_get(new_id), "commit": record, "source_commit": commit_id}

    def _carry_dialog(
        self, instance_id: str, source_timeline: str, target_timeline: str, rows: list[dict[str, Any]]
    ) -> int:
        """把来源提交点的对话搬到新线：按角色重建会话，再按会话分组写回消息。"""
        if not rows:
            return 0
        sessions = {
            str(item["id"]): item
            for item in self.store.instance_sessions(instance_id)
            if str(item.get("timeline_id") or "") == source_timeline
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            source = sessions.get(str(row.get("session_id") or ""))
            if source is None:
                continue
            target = self.store.session_ensure(
                instance_id, target_timeline, str(source.get("character_id") or "")
            )
            grouped.setdefault(str(target["id"]), []).append(row)
        written = 0
        for session_id, items in grouped.items():
            self.store.instance_import_messages(session_id, items)
            written += len(items)
        return written

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
        delivered = self.store.timeline_delivered_replies(timeline_id)
        voided = self.store.timeline_void_inflight(timeline_id)
        cancelled = self.store.timeline_cancel_undelivered(timeline_id)
        load_rows = dict(snapshot.get("runtime") or {})
        # 正文 §三：回滚同时恢复目标提交点的对话（会话行按会话标识回写）
        load_rows["dialog"] = list(snapshot.get("dialog") or [])
        self.store.runtime_load(instance_id, timeline_id, load_rows, clear=True)
        now = float(now_real if now_real is not None else time.time())
        self.store.clock_put({
            **self.clock_row(timeline_id),
            "base_real": now,
            "base_world": int(snapshot.get("world") or 0),
            # 回滚后按**提交那一刻**的倍率跑（选定口径）：照快照值重锚，不重放历史倍率命令（§七）
            "rate": int(snapshot.get("rate") or 1),
            "high_water_real": now,
            "anchor_real": now,
            "processed_world": int(snapshot.get("world") or 0),
            "catching_up": 0,
            "limited": 0,
        })
        # 回滚跨过补卡点：目标快照里没有的成员资格转为撤销（记录留着，不能复活，§3.7 末条）
        self.store.character_join_revoke_missing(
            timeline_id,
            {
                str(item.get("character_id") or "")
                for item in (load_rows.get("characters") or [])
                if isinstance(item, dict)
            },
            moment=int(snapshot.get("world") or 0),
        )
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
            "delivered_replies_kept": delivered,
            "warning": (
                f"有 {delivered} 条回复已经投递到外部平台（用户可能已经读过），回滚不保证消除它们；"
                "核心历史与此后的生成 / 投递已按回滚点恢复。"
                if delivered
                else "回滚覆盖本线核心历史；请确认没有需要保留的进展（必要时先从当前提交分叉或导出）。"
            ),
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

    def queue_disclosed_sources(self, instance_id: str, timeline_id: str, character_id: str) -> int:
        """把接收角色看到的转述登记进提取来源（§4.1 / §7.1）：记成转述，不当亲历。"""
        added = 0
        for item in self.disclosed_fragments(instance_id, timeline_id, character_id):
            entry = disclosure.transcribe_entry(item)
            added += int(self.store.memory_task_add({
                "id": f"mt-dsc-{events.stable_key(instance_id, timeline_id, character_id, item['disclosure_id'], item['ref'])[:12]}",
                "instance_id": instance_id, "timeline_id": timeline_id, "character_id": character_id,
                "source_kind": "disclosed", "source_ref": f"{item['disclosure_id']}:{item['ref']}",
                "source_world": int(item["granted_world"]), "created_world": int(item["granted_world"]),
                "text": entry["text"],
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
            experiences = self.store.experience_window(
                instance_id, timeline_id, character_id, until=10**15, limit=200
            )
            # 日程切片（life）按世界日采样：一天 20 条窗口切片里留一条做锚点（§4.1）
            kept, skipped = memory_mod.select_experience_sources(
                experiences, day_seconds=self.calendar(self.store.instance_get(instance_id) or {}).day_seconds
            )
            for row in experiences:
                if str(row["id"]) not in kept:
                    continue
                rows.append(("experience", str(row["id"]), int(row["world_seconds"])))
            for row in self.store.knowledge_window(
                instance_id, timeline_id, character_id, until=10**15, limit=200
            ):
                rows.append(("claim", str(row["id"]), int(row["world_seconds"])))
            for row in self.store.intent_list(instance_id, timeline_id, character_id):
                rows.append(("intent", str(row["id"]), int(row["source_world"])))
            self.queue_disclosed_sources(instance_id, timeline_id, character_id)
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
        if kind == "disclosed":
            return {
                "text": str(task.get("text") or ""),
                "source": "联络者转述",
                "when": when,
                "sources": [{"kind": "dialog", "ref": ref, "source_role": "other_character", "via": ref.split(":")[0]}],
                "world": int(task["source_world"]),
            }
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
            text = str(row.get("summary") or row.get("activity") or "")
            if not text:
                return None  # 空材料不值得花一次调用：当来源不可达处理
            return {
                "text": text,
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
        if not self.compatible(instance_id):
            return {"extracted": 0, "written": 0, "pending": 0, "calls": 0}  # 不跑派生任务（§7.6）
        cap = max(1, min(int(limit), memory_mod.BATCH_SIZE))
        tasks = self.store.memory_tasks(instance_id, timeline_id)[:cap]
        if not tasks:
            return {"extracted": 0, "written": 0, "pending": 0, "calls": 0}
        row = self.clock_row(timeline_id)
        watermark = int(row["processed_world"])
        generation = int(row["generation"])
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
            fresh = self.clock_row(timeline_id)
            if int(fresh["generation"]) != generation or str(
                self.store.timeline_get(timeline_id)["state"]
            ) != "active":
                # 迟到结果不许写回：线在这轮调用期间被回滚 / 冻结 / 归档（§4.1、验收 2 / 9）。
                # 保留待处理，重试留给下次调度；来源真没了时下轮会按「来源不可达」丢。
                for material in materials:
                    self.store.memory_task_set(
                        str(material["task"]["id"]), state="pending", note="线已回滚或冻结"
                    )
                pending += len(materials)
                continue
            if not entries:
                # 分不清「确实没记下什么」与「回答不可解析」：看有没有结构化载荷。
                # 有载荷而解析为空 = 模型说没有可记内容；没载荷 = 失败，保留待处理可重试。
                parseable = "[" in text and "]" in text
                for material in materials:
                    self.store.memory_task_set(
                        str(material["task"]["id"]),
                        state="done" if parseable else "pending",
                        note="无可记内容" if parseable else "提取结果不可解析",
                    )
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
                    # 幂等键 =「来源 + 条目文本」：一个来源可以产出多条不同条目，
                    # 只按来源去重会把第二条静默丢掉；重跑同一来源仍命中同键不重复写（§4.1）
                    "source_key": f"{task['id']}#{events.stable_key(entry['text'])[:12]}",
                    "decay_world": watermark,
                })
                if saved is not None:
                    written += 1
                self.store.memory_task_set(str(task["id"]), state="done")
            mentioned = {str(entry["ref"]) for entry in entries}
            for material in materials:
                if str(material["ref"]) not in mentioned:
                    # 本轮回答没提到它 = 看过了、没什么值得记：结算掉，别下一轮又拿它调一次模型（§4.1）
                    self.store.memory_task_set(
                        str(material["task"]["id"]), state="done", note="本轮未提到可记内容"
                    )
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
        prompt, unit = self.system_prompt(session, topic=topic, with_unit=True)
        instance_id = str(session.get("instance_id") or "")
        timeline_id = str(session.get("timeline_id") or "")
        character_id = str(session.get("character_id") or "")
        if not (instance_id and timeline_id and character_id):
            return {"prompt": prompt, "memory_ids": [], "brief": "", "unit": unit}
        try:
            recalled = self.recall(
                instance_id, timeline_id, character_id, topic=topic, world_seconds=world_seconds,
                query_vector=query_vector,
            )
        except Exception:  # 召回失败不得阻断对话
            return {"prompt": prompt, "memory_ids": [], "brief": "", "unit": unit}
        block = disclosure.brief_block(
            self.disclosed_fragments(instance_id, timeline_id, character_id)
        )
        if block:
            prompt = prompt + chr(10) + chr(10) + chr(10).join(block)
        brief = recalled["brief"]["text"]
        if brief:
            prompt = prompt + chr(10) + chr(10) + "她此刻想得起来的事（按她自己的记性，别当成盘点）：" + chr(10) + brief
        # 短期反应进语气与取舍（§11.1）：只给还有效的那几条，不展示内部字段与强度数值
        at = int(world_seconds if world_seconds is not None else 0)
        if not at:
            try:
                at = int(self.clock_row(timeline_id)["processed_world"])
            except Exception:
                at = 0
        tendency = reaction.tendency_block(
            self.store.reaction_list(instance_id, timeline_id, character_id=character_id), watermark=at
        )
        if tendency:
            prompt = prompt + chr(10) + chr(10) + "她眼下的处境（短期反应，只作语气与取舍的依据，别当成情绪报告）：" + chr(10) + tendency
        return {"prompt": prompt, "memory_ids": recalled["ids"], "brief": brief, "unit": unit}

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
            instance_id, timeline_id, model=self.embedding_model, dim=int(self.embedding_dim or 0)
        )[: max(1, int(limit))]
        if not rows and not self.embedding_dim:
            # 维度还没确认过：拿一条已嵌入的当样本走一次调用，否则「同名换维度」时缺向量查询
            # 查空就直接 return，第二段的按真实维度重建永远走不到（§5.2）
            sample = self.store.memory_embedding_peek(instance_id, timeline_id, model=self.embedding_model)
            rows = [sample] if sample else []
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
        if vectors:
            self.embedding_dim = len(vectors[0])  # 一次成功调用就能确认当前维度
        for row, vector in zip(rows, vectors):
            self.store.memory_embedding_put(
                str(row["id"]),
                instance_id=instance_id,
                timeline_id=timeline_id,
                model=self.embedding_model,
                vector=vector,
                content_hash=embedding_mod.content_hash(str(row["text"])),
            )
        rebuilt = 0
        # 同名换维度也要重建：库内旧向量维度与本次不同 → 按真实维度再补一轮
        if vectors:
            stale = self.store.memory_missing_embeddings(
                instance_id, timeline_id, model=self.embedding_model, dim=len(vectors[0]), limit=8
            )
            if stale:
                try:
                    more = await self._embed([str(item["text"]) for item in stale])
                except Exception:
                    more = []
                for row, vector in zip(stale, more):
                    self.store.memory_embedding_put(
                        memory_id=str(row["id"]),
                        instance_id=instance_id,
                        timeline_id=timeline_id,
                        model=self.embedding_model,
                        vector=vector,
                        content_hash=embedding_mod.content_hash(str(row["text"])),
                    )
                    rebuilt += 1
        return {"embedded": len(vectors), "rebuilt": rebuilt, "model": self.embedding_model}

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

    async def compact_backlog(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        llm: Any,
        now_real: float,
        batch: int = 40,
        limit: int = 1,
    ) -> dict[str, int]:
        """积压汇总（§4.1）：把堆久了的**流水账类**来源批量归一，成本 O(批数) 而不是 O(条数)。

        与 `extract_memories` 的分工：那条路一条来源一次精提（对话、转述、打算不能粗），
        这条路只吃 `experience` / `claim` 的旧账，一次调用吃一批、产出少量概括条目。
        世界时间跑得比现实预算快（追赶一段就是几百个世界日），没有这条，积压只会越长越大。
        """
        from ..log import get_logger

        log = get_logger("isekai.memory")
        if not self.compatible(instance_id):
            return {"materials": 0, "calls": 0, "written": 0, "pending": 0, "batches": 0}
        cap = max(2, min(int(batch), 120))
        all_tasks = [
            task for task in self.store.memory_tasks(instance_id, timeline_id)
            if str(task["source_kind"]) in ("experience", "claim")
        ]
        if len(all_tasks) < cap // 2:
            return {"materials": 0, "calls": 0, "written": 0, "pending": len(all_tasks), "batches": 0}
        row = self.clock_row(timeline_id)
        watermark = int(row["processed_world"])
        generation = int(row["generation"])
        day_seconds = self.calendar(self.store.instance_get(instance_id)).day_seconds
        written = calls = batches = materials_total = 0
        by_character: dict[str, list[dict[str, Any]]] = {}
        for task in all_tasks:
            by_character.setdefault(str(task["character_id"]), []).append(task)
        for character_id, items in by_character.items():
            if batches >= max(1, int(limit)):
                break
            materials: list[dict[str, Any]] = []
            for task in items[:cap]:
                material = self._source_material(task)
                if material is None:
                    self.store.memory_task_set(str(task["id"]), state="dropped", note="来源不可达")
                    continue
                materials.append({**material, "ref": str(task["id"]), "task": task})
            if len(materials) < 2:
                continue
            materials_total += len(materials)
            prompt = memory_mod.compaction_prompt(
                name=self._display_name(instance_id, timeline_id, character_id),
                from_label=str(materials[0].get("when") or ""),
                to_label=str(materials[-1].get("when") or ""),
                items=materials,
            )
            prompt_text = "\n".join(str(item.get("content") or "") for item in prompt)
            reservation = self.reserve_call(
                instance_id, timeline_id, "memory_compact", prompt_text=prompt_text, now_real=now_real
            )
            if not reservation.get("ok"):
                break
            calls += 1
            batches += 1
            try:
                text = await llm.chat(prompt, temperature=0.3, timeout=90.0)
            except Exception:
                log.exception("memory compaction failed character=%s", character_id)
                self.settle_call(reservation, prompt_text=prompt_text, outcome="error")
                continue
            self.settle_call(reservation, prompt_text=prompt_text, reply=str(text or ""))
            entries = memory_mod.parse_extraction(str(text or ""), {str(item["ref"]) for item in materials})
            if int(self.clock_row(timeline_id)["generation"]) != generation:
                # 迟到结果不许写回；这批保持待处理，留给下次
                continue
            by_ref = {str(item["ref"]): item for item in materials}
            for entry in entries:
                material = by_ref[entry["ref"]]
                task = material["task"]
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
                    "strength": memory_mod.decayed_strength(
                        entry["strength"], from_world=int(task["source_world"]), to_world=watermark,
                        day_seconds=day_seconds, per_day=self.memory_decay_per_day,
                    ),
                    "confidence": entry["confidence"],
                    "source_key": f"compact:{task['id']}#{events.stable_key(entry['text'])[:12]}",
                    "decay_world": watermark,
                })
                if saved is not None:
                    written += 1
            # 这一批无论有没有被提到都合上账：汇总就是把这段流水账收尾
            parseable = "[" in str(text or "") and "]" in str(text or "")
            for material in materials:
                self.store.memory_task_set(
                    str(material["task"]["id"]),
                    state="done" if parseable else "pending",
                    note="已汇总" if parseable else "汇总结果不可解析",
                )
        return {
            "materials": materials_total,
            "calls": calls,
            "written": written,
            "pending": len(all_tasks),
            "batches": batches,
        }

    async def organize_memories(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        llm: Any = None,
        now_real: float | None = None,
        max_items: int = 3,
    ) -> dict[str, Any]:
        """记忆整理（§六）：挂角色作息节律，把过长的旧条目压短——只调表达，不造事实。

        不合并相互矛盾的来源；改写结果按 version+1 固化成新条目并指向旧条目（历史可查当时原文）。
        同一旧条目只整理一次（来源键去重），补算不会反复改写同一段过去。
        """
        import time as _time

        now = _time.time() if now_real is None else float(now_real)
        instance = self.store.instance_get(instance_id)
        if instance is None:
            raise RuntimeStateError(f"实例不存在：{instance_id}")
        calendar = self.calendar(instance)
        world = int(self.clock_row(timeline_id)["processed_world"])
        changed: list[dict[str, Any]] = []
        for card in self.cards(instance, timeline_id=timeline_id, world_seconds=world):
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id:
                continue
            # 节律：睡眠时段整理；无睡眠角色按世界日界（当日起初四分之一）触发
            plan = self.store.plan_latest(instance_id, timeline_id, character_id)
            window = life.current_window(plan, world)
            sleeping_now = str((window or {}).get("activity") or "").lower() == "sleep"
            at_day_edge = world % max(1, int(calendar.day_seconds)) < int(calendar.day_seconds) // 4
            if not (sleeping_now or at_day_edge):
                continue
            rows = [
                row
                for row in self.store.memory_scope(
                    instance_id, timeline_id, character_id, until=world
                )
                if str(row.get("state")) != "archived"
                and len(str(row.get("text") or "")) > 60
                and not str(row.get("source_key") or "").startswith("organize:")
            ][: max(0, int(max_items))]
            for row in rows:
                if llm is None:
                    break
                try:
                    text = await llm.chat(memory_mod.organize_prompt(row), temperature=0.3, timeout=30.0)
                except Exception:
                    continue
                short = memory_mod.parse_organized(str(text), str(row.get("text") or ""))
                if not short or short == str(row.get("text") or ""):
                    continue
                new_row = {
                    **{key: row.get(key) for key in (
                        "instance_id", "timeline_id", "character_id", "kind",
                        "happened_world", "learned_world", "semantic_watermark",
                    )},
                    "id": f"mm-org-{row['id'][-10:]}",
                    "text": short,
                    # memory_scope 给的是 JSON 文本；memory_add 会再序列化一次 → 必须先解回列表
                    "sources": json.loads(str(row.get("sources") or "[]")),
                    "recorded_world": world,
                    "strength": row.get("strength"),
                    "confidence": row.get("confidence"),
                    "source_key": f"organize:{row['id']}",
                }
                created = self.store.memory_add(new_row)
                if created is not None:
                    # 整理后的版本取代原文：旧条归档留档、新版版本号 +1（§六）
                    self.store.memory_supersede(old_id=str(row["id"]), new_id=str(created.get("id") or ""))
                    changed.append({"from": str(row["id"]), "to": str(created.get("id") or "")})
        return {"organized": len(changed), "items": changed}

    def _drop_archived_unless_strong(
        self,
        ranked: list[dict[str, Any]],
        *,
        topic: str,
        vector_scores: dict[str, float] | None = None,
        until: int | None = None,
    ) -> list[dict[str, Any]]:
        """归档条目默认不进召回；**强相关**才唤起（§六），作用域与来源过滤已在前面做过。

        强相关 = 逐字命中（话题与条目文本互相包含）或向量相似度过线；唤起时打上标记，
        让表述层知道这是「模糊记起」，不是笃定的事实。

        `until` 是查询水位：当时还没被替代的条目按当时认知算当前版本，不算归档（§4.2 问历史读旧版本）。
        """
        query = str(topic or "").strip()
        vec = dict(vector_scores or {})
        pending = [str(item["id"]) for item in ranked if str(item.get("state")) == "archived"]
        marks = {}
        if pending and until is not None:
            instance_id = str(ranked[0].get("instance_id") or "")
            timeline_id = str(ranked[0].get("timeline_id") or "")
            marks = self.store.memory_supersede_moments(instance_id, timeline_id, pending)
        out: list[dict[str, Any]] = []
        for item in ranked:
            if str(item.get("state")) != "archived":
                out.append(item)
                continue
            mark = int(marks.get(str(item.get("id")) or "", 0))
            if mark:
                # 被新版取代（整理 / 明确纠正）：当前认知用新版，不召回旧版；
                # 查询水位在替代之前时它还是当时的当前版本，按当时认知给出（§4.2）
                if until is None or mark <= int(until):
                    continue
                out.append(item)
                continue
            text = str(item.get("text") or "")
            literal = bool(query) and (query in text or text in query)
            score = float(vec.get(str(item.get("id")) or "", 0.0))
            if literal or score >= float(self.memory_archived_recall_min):
                out.append({**item, "fuzzy": True})
        return out

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
        # 已授权的转述：作为**视图**参与召回，不复制来源角色的其他内容（§7.1）
        for item in self.disclosed_fragments(instance_id, timeline_id, character_id, until=int(world_seconds)):
            entry = disclosure.transcribe_entry(item)
            entries = entries + [{
                "id": f"mm-disc-{item['disclosure_id'][-6:]}-{abs(hash(item['ref'])) % 10000}",
                "text": entry["text"],
                "kind": entry["kind"],
                "sources": json.dumps(entry["sources"], ensure_ascii=False),
                "learned_world": int(item["granted_world"]),
                "strength": 0.6,
                "confidence": 0.8,
                "state": "active",
            }]
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
        ranked = self._drop_archived_unless_strong(
            ranked, topic=topic, vector_scores=vector_scores, until=int(world_seconds)
        )[:40]
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
        if timeline_id is None:
            return cards
        if world_seconds is None:
            # 没给水位就按该线当前水位：补入的角色默认算「已在本线」，别让她隐形
            clock = self.store.clock_get(timeline_id)
            world_seconds = int(clock["processed_world"]) if clock else 0
        world_seconds = int(world_seconds)
        joined_ids = self.store.character_join_ids(str(instance["id"]))
        out: list[dict[str, Any]] = []
        known: set[str] = set()
        for card in cards:
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            known.add(character_id)
            if character_id in joined_ids:
                # 补入的角色：只有本线成员资格仍有效时她在这条线可选（§3.7 第 1 条）；
                # 撤销过的定义留在实例快照里，但不因快照保留而在本线重新暴露。
                if self.store.character_membership(str(instance["id"]), timeline_id, character_id, until=world_seconds) != "joined":
                    continue
            out.append(card)
        # 兼容早期补卡：定义只在成员资格行里（迁移前的库）
        for row in self.store.character_join_list(instance["id"], timeline_id, until=world_seconds):
            if str(row["character_id"]) in known:
                continue
            try:
                out.append(json.loads(str(row["card"])))
            except json.JSONDecodeError:
                log.warning("补入角色卡损坏 card=%s", row.get("character_id"))
        return out

    def assert_member(self, instance: dict[str, Any], timeline_id: str, character_id: str) -> None:
        """角色选择 / 会话创建 / 认知查询前的成员资格检查（§3.7 第 1 条）。

        依据该线**当前水位**的角色集合：撤销过的补入角色与从未装配过的标识都在此被挡。
        """
        clock = self.store.clock_get(timeline_id)
        world_seconds = int(clock["processed_world"]) if clock else 0
        known = {
            str((card.get("meta") or {}).get("card_id") or "")
            for card in self.cards(instance, timeline_id=timeline_id, world_seconds=world_seconds)
        }
        if str(character_id) not in known:
            raise RuntimeStateError(f"该角色不在本线的角色集合里：{character_id}")

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

    # ------------------------------------------------ 对外接口（WORLD_RUNTIME_INTERFACE_SPEC）

    def envelope(
        self, instance_id: str, timeline_id: str, *, status: str = "ok", extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """返回信封（§3.2）：所有对外接口响应都带这一套，省得每个调用方自己拼。"""
        row = self.clock_row(timeline_id)
        out: dict[str, Any] = {
            "status": str(status),
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "observed_revision": int(row["processed_world"]),
            "world_time": int(row["processed_world"]),
            "processed_watermark": int(row["processed_world"]),
            "runtime_generation": int(row["generation"]),
        }
        out.update(extra or {})
        return out

    def scope_inspect(
        self, instance_id: str, timeline_id: str, *, now_real: float | None = None
    ) -> dict[str, Any]:
        """§4.1：底层 ready 与否 + 管理元数据。不返回世界实情、角色状态或任务正文。"""
        instance, timeline = self._rows(instance_id, timeline_id)
        row = self.clock_row(timeline_id)
        state = self.state_of(row)
        now = float(now_real if now_real is not None else time.time())
        target = target_world(state, now)
        processed = int(row["processed_world"])
        timeline_state = str(timeline["state"])
        catching = processed < target or int(row.get("catching_up") or 0)
        if timeline_state == "active" and catching:
            timeline_state = "catching_up"
        ruleset_version = ""
        campaigns = getattr(self.campaign, "campaigns", None) if getattr(self, "campaign", None) else None
        if campaigns is not None:
            for item in campaigns(instance_id, timeline_id) or []:
                if str(item.get("status")) in ("active", "waiting", "preparing"):
                    ruleset_version = str(item.get("ruleset_version") or "")
                    break
        actions = {
            "active": ["read", "preview", "commit", "advance", "fork", "rollback"],
            "catching_up": ["read"],
            "frozen": ["read", "activate", "fork", "rollback"],
            "archived": ["read", "fork"],
        }.get(timeline_state, ["read"])
        return self.envelope(instance_id, timeline_id, extra={
            "timeline_state": timeline_state,
            "target_watermark": int(target),
            "revision": int(row["processed_world"]),
            "ruleset_version": ruleset_version,
            "available_actions": actions,
        })

    #: §4.2 `include` 的可选投影 → character_snapshot 的键
    SNAPSHOT_INCLUDES = {
        "time": "world_seconds",
        "current_activity": "current_activity",
        "active_effects": "effects",
        "experiences": "experiences",
        "claims": "knowledge",
        "plans": "plan",
    }

    def read_snapshot(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        request: dict[str, Any] | None = None,
        now_real: float | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """§4.2：一致、可复用的世界快照句柄。**不等于写入许可**。

        追赶中 / 冻结 / 归档时明确不可用（`not_ready`），不用旧状态冒充当前状态。
        """
        request = request if isinstance(request, dict) else {}
        _instance, timeline = self._rows(instance_id, timeline_id)
        row = self.clock_row(timeline_id)
        processed = int(row["processed_world"])
        now = float(now_real if now_real is not None else time.time())
        target = target_world(self.state_of(row), now)
        if str(timeline["state"]) != "active":
            return self.envelope(instance_id, timeline_id, status="not_ready", extra={
                "reason": f"时间线当前是 {timeline['state']}", "snapshot_id": "", "payload": {},
            })
        if processed < target:
            return self.envelope(instance_id, timeline_id, status="not_ready", extra={
                "reason": f"还在追赶：水位 {processed} < 目标 {target}", "snapshot_id": "", "payload": {},
            })
        includes = [str(item) for item in (request.get("include") or [])] or list(self.SNAPSHOT_INCLUDES)
        unknown = [item for item in includes if item not in self.SNAPSHOT_INCLUDES]
        if unknown:
            return self.envelope(instance_id, timeline_id, status="rejected", extra={
                "reason": f"未知的 include：{unknown}", "snapshot_id": "", "payload": {},
            })
        payload: dict[str, Any] = {}
        for character_id in [str(item) for item in (request.get("characters") or [])]:
            snapshot = self.character_snapshot(instance_id, timeline_id, character_id, world_seconds=processed)
            selected: dict[str, Any] = {}
            for key in includes:
                if key == "time":
                    selected[key] = int(snapshot.get("world_seconds") or processed)
                elif self.SNAPSHOT_INCLUDES[key] in snapshot:
                    selected[key] = snapshot[self.SNAPSHOT_INCLUDES[key]]
            payload[character_id] = selected
        return self.envelope(instance_id, timeline_id, extra={
            "snapshot_id": f"snap-{processed}",
            "revision": processed,
            "expires_at": now + max(0, int(ttl_seconds)),
            "payload": payload,
        })

    #: §4.3 purpose 闭集
    COGNITION_PURPOSES = ("dialogue", "player_observation", "narrative_candidate", "audit")

    def cognition_project(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        observer_id: str,
        query: dict[str, Any] | None = None,
        at_revision: int | None = None,
    ) -> dict[str, Any]:
        """§4.3：按观察者取合法可知投影——上层生成角色视角材料的唯一底层入口。

        只读该观察者自己的经历与获知（窗口本来就按角色存储），不返回未获知事件、
        他人私聊或实情层字段；「不知道」是合法结果（`known_unknowns`）。
        """
        query = query if isinstance(query, dict) else {}
        observer = str(observer_id or "")
        if not observer:
            return self.envelope(instance_id, timeline_id, status="rejected", extra={"reason": "缺少 observer_id"})
        purpose = str(query.get("purpose") or "dialogue")
        if purpose not in self.COGNITION_PURPOSES:
            return self.envelope(instance_id, timeline_id, status="rejected", extra={
                "reason": f"未知 purpose：{purpose}",
            })
        row = self.clock_row(timeline_id)
        until = int(at_revision if at_revision is not None else row["processed_world"])
        observations = [
            {"text": str(item.get("summary") or ""), "kind": str(item.get("kind") or ""),
             "world_seconds": int(item["world_seconds"]), "ref": str(item["id"]),
             "when": self.describe_world(instance_id, int(item["world_seconds"]))}
            for item in self.store.experience_window(instance_id, timeline_id, observer, until=until, limit=50)
        ]
        claims = [
            {"text": str(item.get("text") or ""), "kind": str(item.get("kind") or ""),
             "target": str(item.get("target") or ""), "source": str(item.get("source") or ""),
             "stance": str(item.get("stance") or ""), "world_seconds": int(item["world_seconds"]),
             "ref": str(item["id"])}
            for item in self.store.knowledge_window(instance_id, timeline_id, observer, until=until, limit=50)
        ]
        known_unknowns = [
            {"ref": str(item["id"]), "text": str(item.get("intent") or ""), "stage": str(item.get("stage") or "")}
            for item in self.store.intent_list(instance_id, timeline_id, observer)
            if str(item.get("stage")) in ("waiting", "deferred")
        ]
        source_refs = sorted({item["ref"] for item in observations} | {item["ref"] for item in claims})
        return self.envelope(instance_id, timeline_id, extra={
            "observer_id": observer,
            "purpose": purpose,
            "observed_revision": until,
            "observations": observations,
            "claims": claims,
            "known_unknowns": known_unknowns,
            "source_refs": source_refs,
        })

    def subject_state(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        subject_id: str,
        fields: list[str] | None = None,
        audience: str = "gm_only",
    ) -> dict[str, Any]:
        """§4.4：主体的结构化状态投影。非 GM 受众只拿公开字段族（规则属性、会话历史不在此）。"""
        subject = str(subject_id or "")
        if not subject:
            return self.envelope(instance_id, timeline_id, status="rejected", extra={"reason": "缺少 subject_id"})
        row = self.clock_row(timeline_id)
        world = int(row["processed_world"])
        snapshot = self.character_snapshot(instance_id, timeline_id, subject, world_seconds=world)
        join = next(
            (item for item in self.store.character_join_list(instance_id, timeline_id, state=None)
             if str(item["character_id"]) == subject),
            {},
        )
        public = {
            "subject_id": subject,
            "state_at_revision": world,
            "active_effects": snapshot.get("effects") or [],
            "current_activity": str(snapshot.get("current_activity") or ""),
            "membership": str(join.get("state") or ""),
            "archive_state": "archived" if str(snapshot.get("archived")) not in ("", "0") else "active",
            "source_refs": [],
        }
        if audience != "gm_only":
            picked = {key: value for key, value in public.items() if not fields or key in fields}
            return self.envelope(instance_id, timeline_id, extra={"audience": audience, "subject": picked})
        full = {
            **public,
            "units": snapshot.get("units") or [],
            "all_units": snapshot.get("all_units") or [],
            "knowledge": snapshot.get("knowledge") or [],
            "experiences": snapshot.get("experiences") or [],
            "plan": snapshot.get("plan") or {},
            "reactions": snapshot.get("reactions") or [],
            "institutions": snapshot.get("institutions") or [],
        }
        picked = {key: value for key, value in full.items() if not fields or key in fields}
        return self.envelope(instance_id, timeline_id, extra={"audience": audience, "subject": picked})

    def history_read(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        cursor: str = "",
        limit: int = 50,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """§4.5：已固化的世界事件与说法的只读历史。

        游标是 `世界秒:序号`（事件表主序），往回翻用 `until=游标世界秒`。事件层是**已发生事实**
        的公开层，按观察者是否合法获知要看 `cognition.project`；这里 `audience` 过滤只作用在
        说法（claim 自带受众列）上，不假装事件层有受众。
        """
        filters = filters if isinstance(filters, dict) else {}
        row = self.clock_row(timeline_id)
        until = int(row["processed_world"])
        if cursor:
            try:
                until = min(until, int(str(cursor).split(":")[0]))
            except ValueError:
                return self.envelope(instance_id, timeline_id, status="rejected", extra={"reason": f"游标不可解析：{cursor}"})
        count = max(1, min(int(limit), 200))
        wanted_kind = str(filters.get("event_kind") or "")
        wanted_source = str(filters.get("source") or "")
        wanted_subject = str(filters.get("subject") or "")
        wanted_audience = str(filters.get("audience") or "")
        time_range = filters.get("time_range") or []
        since = int(time_range[0]) if len(time_range) == 2 else 0
        to = int(time_range[1]) if len(time_range) == 2 else until
        events = [
            item for item in self.store.event_window(instance_id, timeline_id, until=until, limit=count * 3)
            if (not wanted_kind or str(item.get("kind")) == wanted_kind)
            and (not wanted_source or str(item.get("source")) == wanted_source)
            and (not wanted_subject or wanted_subject in str(item.get("effects") or ""))
            and since <= int(item["world_seconds"]) <= to
        ][-count:]
        claims = [
            {"ref": str(item["id"]), "event_id": str(item["event_id"]), "text": str(item["text"]),
             "source": str(item["source_id"]), "audience": str(item["audience"]),
             "world_seconds": int(item["earliest_world"]), "kind": "claim"}
            for item in self.store.claim_list(instance_id, timeline_id)
            if int(item["earliest_world"]) <= until
            and (not wanted_audience or str(item.get("audience")) == wanted_audience)
            and since <= int(item["earliest_world"]) <= to
        ]
        items = [
            {"ref": str(item["id"]), "kind": "event", "event_kind": str(item.get("kind")),
             "family": str(item.get("family")), "template": str(item.get("template")),
             "source": str(item.get("source")), "summary": str(item.get("summary")),
             "world_seconds": int(item["world_seconds"]), "seq": int(item.get("seq") or 0),
             "effects": item.get("effects") or "[]"}
            for item in events
        ]
        items.sort(key=lambda item: (int(item["world_seconds"]), int(item.get("seq") or 0)))
        next_cursor = ""
        if items:
            first = items[0]
            next_cursor = f"{int(first['world_seconds'])}:{int(first.get('seq') or 0)}"
        return self.envelope(instance_id, timeline_id, extra={
            "items": items,
            "claims": claims,
            "next_cursor": next_cursor,
            "obsolescense_note": "",  # 见 §4.5：事件区分发生 / 固化 / 传播 / 获知时刻
        })

    def generation_check(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        snapshot_id: str = "",
        runtime_generation: int | None = None,
        source_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """§6.3：异步模块固化结果前的检查。`stale` 只能存本地，不能写回世界。"""
        row = self.clock_row(timeline_id)
        try:
            self.store.write_probe()
        except Exception:  # noqa: BLE001 - 不可写就是要报 persistence_blocked
            return self.envelope(instance_id, timeline_id, status="persistence_blocked",
                                 extra={"reason": "存储不可写"})
        for ref in source_refs or []:
            join = next(
                (item for item in self.store.character_join_list(instance_id, timeline_id, state=None)
                 if str(item["character_id"]) == str(ref)),
                None,
            )
            if join is not None and str(join.get("state")) != "active":
                return self.envelope(instance_id, timeline_id, status="member_archived",
                                     extra={"reason": f"成员已撤销：{ref}"})
        current = int(row["generation"])
        if runtime_generation is not None and int(runtime_generation) != current:
            return self.envelope(instance_id, timeline_id, status="stale",
                                 extra={"reason": f"世代不一致：请求 {runtime_generation}，当前 {current}"})
        if snapshot_id:
            want = str(snapshot_id)
            processed = int(row["processed_world"])
            if want.startswith("snap-") and want[5:].isdigit() and int(want[5:]) > processed:
                return self.envelope(instance_id, timeline_id, status="conflict",
                                     extra={"reason": f"快照 {want} 比当前水位 {processed} 新"})
        return self.envelope(instance_id, timeline_id, status="valid")

    def invalidate_tasks(
        self, instance_id: str, timeline_id: str, *, generation: int | None = None, reason: str = ""
    ) -> dict[str, Any]:
        """§6.4：让指定世代的派生任务失效（只取消未提交的候选 / 生成 / 投递工作）。

        实现就是提升运行世代——迟到结果按世代被拒，**已固化历史不受影响**（撤销事实只能走回滚）。
        """
        row = self.clock_row(timeline_id)
        before = int(row["generation"])
        self.store.clock_put({**row, "generation": before + 1})
        return self.envelope(instance_id, timeline_id, extra={
            "invalidated_generation": before if generation is None else int(generation),
            "runtime_generation": before + 1,
            "reason": str(reason or "管理面要求使旧世代任务失效"),
        })

    def change_preview(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        changes: list[dict[str, Any]] | None = None,
        rule_state_patches: list[dict[str, Any]] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """§5.2：不改世界地检查一批变化意图。预览不是提交承诺。"""
        changes = changes if isinstance(changes, list) else []
        errors = change_mod.validate_intents(changes)
        if errors:
            return self.envelope(instance_id, timeline_id, status="rejected", extra={
                "errors": errors, "accepted_candidates": [], "rejected_candidates": [],
                "needs_review": [], "projected_effects": [], "conflicts": [],
            })
        instance = self.store.instance_get(instance_id) or {}
        row = self.clock_row(timeline_id)
        world = int(row["processed_world"])
        translated = change_mod.translate(changes, package=self.setting(instance)["world_package"], world_seconds=world)
        conflicts: list[dict[str, Any]] = []
        if expected_revision is not None and int(expected_revision) != world:
            conflicts.append({"kind": "revision", "expected": int(expected_revision), "current": world})
        projected: list[dict[str, Any]] = []
        rejected = list(translated["rejected"])
        if translated["effects"]:
            try:
                targets, channels = self._known_targets(instance, timeline_id, world_seconds=world)
                normalized = drafts.normalize_draft(
                    self.setting(instance)["world_package"], translated,
                    known_targets=targets, world_seconds=world, default_channels=channels,
                )
                projected = list(normalized.get("effects") or [])
            except ValueError as exc:
                rejected.append({"id": "*", "reason": f"世界后果无法映射：{exc}"})
        elif translated["claims"]:
            projected = []
        else:
            rejected.append({"id": "*", "reason": "这批意图没有能落成事实效果的内容"})
        base = change_mod.preview_id(instance_id, timeline_id, world, changes)
        # 一个都翻不成事实效果时如实说 rejected：预览不该报 ok 却什么都不给（§八）
        status = "rejected" if (rejected and not projected) else "ok"
        return self.envelope(instance_id, timeline_id, status=status, extra={
            "preview_id": base,
            "base_revision": world,
            "accepted_candidates": translated["accepted"],
            "rejected_candidates": rejected,
            "needs_review": translated["needs_review"],
            "projected_effects": projected,
            "projected_observations": [],
            "projected_rule_state_revisions": {
                str(item.get("ruleset_id") or ""): int(item.get("base_state_revision") or 0) + 1
                for item in (rule_state_patches or []) if isinstance(item, dict)
            },
            "conflicts": conflicts,
        })

    def change_commit(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        changes: list[dict[str, Any]] | None = None,
        idempotency_key: str = "",
        preview_id: str = "",
        expected_revision: int | None = None,
        source_module: str = "",
    ) -> dict[str, Any]:
        """§5.3：原子提交一批已确认的变化。**这是高级模块唯一的世界事实写入口。**

        幂等：事件标识由幂等键派生，重放先查该事件是否已在——在就返回原结果，不再施加效果。
        """
        changes = changes if isinstance(changes, list) else []
        key = str(idempotency_key or "").strip()
        if not key:
            return self.envelope(instance_id, timeline_id, status="rejected",
                                 extra={"reason": "会改变状态的调用必须带 idempotency_key"})
        errors = change_mod.validate_intents(changes)
        if errors:
            return self.envelope(instance_id, timeline_id, status="rejected", extra={"errors": errors})
        instance = self.store.instance_get(instance_id) or {}
        _inst_rows, timeline = self._rows(instance_id, timeline_id)
        if str(timeline["state"]) != "active":
            return self.envelope(instance_id, timeline_id, status="not_ready",
                                 extra={"reason": f"时间线当前是 {timeline['state']}"})
        row = self.clock_row(timeline_id)
        world = int(row["processed_world"])
        ident = f"ev-iface-{events.stable_key(instance_id, timeline_id, source_module or 'iface', key)[:12]}"
        existing = self.store.event_get(instance_id, timeline_id, ident)
        if existing is not None:
            return self.envelope(instance_id, timeline_id, status="duplicate", extra={
                "commit_id": str(existing["id"]), "new_revision": int(existing["world_seconds"]),
                "event_refs": [str(existing["id"])],
            })
        if expected_revision is not None and int(expected_revision) != world:
            return self.envelope(instance_id, timeline_id, status="conflict", extra={
                "expected": int(expected_revision), "current": world,
            })
        if preview_id and str(preview_id) != change_mod.preview_id(instance_id, timeline_id, world, changes):
            return self.envelope(instance_id, timeline_id, status="conflict",
                                 extra={"reason": "预览已失效：基准版本或内容变了，请重新预览"})
        translated = change_mod.translate(changes, package=self.setting(instance)["world_package"], world_seconds=world)
        if translated["needs_review"]:
            return self.envelope(instance_id, timeline_id, status="needs_review", extra={
                "needs_review": translated["needs_review"],
            })
        if translated["rejected"]:
            return self.envelope(instance_id, timeline_id, status="rejected",
                                 extra={"rejected_candidates": translated["rejected"]})
        try:
            targets, channels = self._known_targets(instance, timeline_id, world_seconds=world)
            normalized = drafts.normalize_draft(
                self.setting(instance)["world_package"], translated,
                known_targets=targets, world_seconds=world, default_channels=channels,
            )
        except ValueError as exc:
            return self.envelope(instance_id, timeline_id, status="rejected",
                                 extra={"reason": f"世界后果无法映射：{exc}"})
        event_rows = self._user_event_rows(
            instance_id, timeline_id, normalized, ident=ident, world=world,
            source=str(source_module or "interface"), template="iface.change",
        )
        event_rows["event"]["detail"] = json.dumps(
            {"changes": changes, "idempotency_key": key, "source_module": str(source_module or "")},
            ensure_ascii=False,
        )
        institution_rows = self._institution_rows(
            instance, instance_id, timeline_id, event_rows["effects"], deaths=[],
            from_world=world, to_world=world,
        )
        environment_rows = self._environment_rows(
            instance, instance_id, timeline_id, self.calendar(instance), event_rows["effects"],
            from_world=world, to_world=world,
        )
        applied = self.store.apply_runtime_batch(
            timeline_id=timeline_id,
            generation=int(row["generation"]),
            processed_world=world,
            catching_up=False,
            events=[event_rows["event"]],
            claims=event_rows["claims"],
            knowledge=event_rows["knowledge"],
            effects=event_rows["effects"],
            environment=environment_rows,
            institution=institution_rows["institution"],
            customs=institution_rows["customs"],
        )
        if not applied:
            return self.envelope(instance_id, timeline_id, status="stale",
                                 extra={"reason": "运行世代已变，整批未落盘"})
        return self.envelope(instance_id, timeline_id, extra={
            "commit_id": ident,
            "new_revision": world,
            "event_refs": [ident],
            "effect_refs": [str(item.get("id") or "") for item in event_rows["effects"]],
            "knowledge_refs": [str(item.get("id") or "") for item in event_rows["knowledge"]],
            "rule_state_refs": [],
        })

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

    def compatible(self, instance_id: str) -> bool:
        """兼容检查（§7.6）：blocked 的实例不推进、不跑派生任务、不接受新对话提交。"""
        row = self.store.instance_get(instance_id)
        return row is None or str(instances.compatibility(row)[0]) != "blocked"

    def _require_compatible(self, instance_id: str) -> None:
        """兼容检查先于推进：blocked 的实例不能激活 / 推进（只读或先转换）。"""
        if self.compatible(instance_id):
            return
        row = self.store.instance_get(instance_id) or {}
        status, note = instances.compatibility(row)
        raise RuntimeStateError(f"实例兼容性阻断，不能推进：{note or status}")

    def activate(
        self, instance_id: str, timeline_id: str, *, now_real: float | None = None, rate: int | None = None
    ) -> dict[str, Any]:
        """激活：以当前现实时间重新锚定，不补算冻结期间的间隔（§2.4、§2.6）。

        倍率超过当前上限（上限被调低 / 导入端上限更低）时不静默改写：保持冻结，
        要求调用方在激活操作中确认一个合法倍率（§2.4）。
        """
        now_real = time.time() if now_real is None else float(now_real)
        self._require_compatible(instance_id)
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

    def freeze(self, instance_id: str, timeline_id: str, *, now_real: float | None = None) -> dict[str, Any]:
        """冻结：结算已生效倍率段、取消未生效请求；冻结线不推进也不接受倍率调整。

        `now_real` 省略时取当前现实时间——归档 / 管理面这类调用方不该被迫自己算时基。
        """
        now_real = time.time() if now_real is None else float(now_real)
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
                # 倍率取结算后的状态：`row` 是结算前的行，照它写回会把刚生效的新倍率丢掉
                "rate": int(state.rate),
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

    def _settled_row(self, timeline_id: str, now_real: float) -> dict[str, Any]:
        """结算已到期的倍率命令并把结果落回 clock 行，返回落库后的行（§2.3.4）。

        `clock.rate` 是**当前倍率段**的流速，只在结算时前进：要拿行值当真值用（取快照、
        冻结、调整倍率）就得先结算，别直接读行 —— 读到的可能是上一段的陈旧倍率。
        """
        row = self.clock_row(timeline_id)
        _, consumed = self._settle_due(timeline_id, row, now_real)
        self.store.rate_apply(consumed)
        return self.clock_row(timeline_id)

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
        # 结算可能刚把当前倍率写进库里：给下面的「没变就不必再改」判断用结算后的行
        row = self.clock_row(timeline_id)
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

    def _advance_guard(self, instance_id: str) -> None:
        """推进前的统一前置（兼容性）。放在所有 advance 入口必经处。"""
        self._require_compatible(instance_id)

    def advance(
        self, instance_id: str, timeline_id: str, *, now_real: float, max_batches: int | None = None
    ) -> dict[str, Any]:
        """把水位从已处理时刻推进到目标时刻，按世界日分批、每批原子（§2.6）。"""
        self._require_compatible(instance_id)
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
            prelim_intents = self._revise_intents(
                instance, instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            institution_rows = self._institution_rows(
                instance,
                instance_id,
                timeline_id,
                world_rows["effects"] + prelim_intents["effects"],
                deaths=self._death_cards(death_rows["events"]),
                from_world=processed,
                to_world=stop,
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
            intent_rows = prelim_intents
            spread = self._propagate_and_clear(
                instance, instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            committed = self.store.apply_runtime_batch(
                timeline_id=timeline_id,
                generation=generation,
                processed_world=stop,
                catching_up=stop < target,
                # 受限状态按**这一批之后的实况**记（附录 B #20）：追平的那一批就该清 0，
                # 不能把循环前算出来的 True 一路写到最后一批、留到下一次 advance 才自愈
                limited=1 if ((target - stop) > self.catch_up_lag_seconds) else 0,
                plans=plans,
                units=units,
                experiences=experiences + intent_rows['experiences'],
                events=world_rows['events'] + intent_rows['events'] + death_rows['events'],
                claims=world_rows['claims'] + death_rows['claims'],
                knowledge=world_rows['knowledge'] + death_rows['knowledge'] + spread['knowledge'],
                effects=world_rows['effects'] + intent_rows['effects'],
                intents=intent_rows['intents'],
                environment=environment_rows,
                institution=institution_rows["institution"],
                customs=institution_rows["customs"],
                clear_effects=spread['clear_effects'],
                reactions=_reaction_rows(
                    cards,
                    effects=world_rows['effects'] + intent_rows['effects'],
                    experiences=experiences + intent_rows['experiences'],
                ),
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

    def consume_time(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        seconds: int,
        cause: str,
        source: str = "world_process",
        now_real: float | None = None,
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        """场景内时间消耗（§十四）：世界时间往前跳 N 秒，再按正常批次结算这段时间。

        与 `advance` 的分工：`advance` 是「跟上真实时间」（动水位 `processed_world`），
        `consume` 是「世界时间跳到尚未发生的时刻」（动锚点 `base_world`）。两者都不许倒退。

        `cause` 必填：一次时间跳跃如果没有理由，以后没人答得上「这段时间为什么过去了」——
        它会被记进提交说明，回滚与审计都靠它。
        """
        seconds = int(seconds or 0)
        if seconds <= 0:
            raise RuntimeStateError("时间消耗必须为正秒数")
        if not str(cause or "").strip():
            raise RuntimeStateError("时间消耗必须给出原因")
        if source not in TIME_CONSUME_SOURCES:
            raise RuntimeStateError(f"未知时间消耗来源：{source}（只接受 {'/'.join(TIME_CONSUME_SOURCES)}）")
        self._require_compatible(instance_id)
        _instance, timeline = self._rows(instance_id, timeline_id)
        if timeline["state"] != "active":
            return {"state": "frozen", "consumed_seconds": 0, "cause": str(cause)}
        row = self.clock_row(timeline_id)
        applied = self.store.apply_runtime_batch(
            timeline_id=timeline_id,
            generation=int(row["generation"]),
            processed_world=int(row["processed_world"]),
            catching_up=False,
            clock_shift_seconds=seconds,
        )
        if not applied:
            # 世代已变（冻结 / 回滚后的迟到任务）：锚点没动，如实报错，不假装消耗过
            raise RuntimeStateError("时间线世代已变，本次时间消耗未生效")
        # 世界过程推进与玩家行动分开记账：来源进提交说明，回滚 / 审计能按它区分
        mark = self.commit(
            instance_id, timeline_id, kind="time_consume", note=f"{cause}（+{seconds}s, {source}）"
        )
        now = float(now_real if now_real is not None else time.time())
        settled = self.advance(instance_id, timeline_id, now_real=now, max_batches=max_batches)
        after = self.clock_row(timeline_id)
        return {
            "state": str(settled.get("state") or ""),
            "consumed_seconds": seconds,
            "cause": str(cause),
            "source": str(source),
            "commit_id": str(mark.get("id") or ""),
            "processed_world": int(after["processed_world"]),
            "catching_up": int(after.get("catching_up") or 0),
            "batches": int(settled.get("batches") or 0),
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
                targets=[character_id, str(card.get("role_id") or ""), region_of(card)],
            )
            note = life.effect_note(
                constraints,
                character_id,
                str(card.get("role_id") or ""),
                region_of(card),
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

    @staticmethod
    def _death_cards(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """本批身故：取角色标识与姓名（供制度状态判定在任者是否已身故）。"""
        out: list[dict[str, Any]] = []
        for event in events or []:
            template = str(event.get("template") or "")
            if not template.startswith("death:"):
                continue
            out.append(
                {
                    "card_id": template.split(":", 1)[1],
                    "event_id": str(event.get("id") or ""),
                    "names": [str(event.get("subject_name") or ""), str(event.get("summary") or "")],
                }
            )
        return out

    async def first_contact(
        self,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        *,
        channel_id: str,
        thread_id: str,
        llm: Any = None,
        now_real: float | None = None,
        max_text_len: int = 0,
        max_parts: int = 0,
    ) -> dict[str, Any]:
        """初次联络的独立开场（§5.6）：只投向触发视图的 thread，一次性，不占主动配额。

        没有打开信号就不该走到这里（调用方=通道声明的视图打开）；也不广播、不改长期主动目标。
        """
        import time as _time

        now = _time.time() if now_real is not None else float(now_real or 0.0)
        if now_real is None:
            now = _time.time()
        instance = self.store.instance_get(instance_id)
        if instance is None:
            raise RuntimeStateError(f"实例不存在：{instance_id}")
        session = self.store.session_ensure(instance_id, timeline_id, character_id)
        existing = self.store.first_contact_get(str(session["id"]))
        if existing is not None:
            return {"reused": True, "message_id": str(existing["message_id"])}
        if self.store.session_has_inbound(str(session["id"])):
            # 用户已经先开口：初见意向并入首轮回复，不再补一条独立开场（§5.6 不双发）
            return {"spoken": False, "reason": "用户已先发言，不另发开场"}

        world = int(self.clock_row(timeline_id)["processed_world"])
        card = self.card_of(instance, character_id, timeline_id=timeline_id, world_seconds=world)
        snapshot = self.character_snapshot(instance_id, timeline_id, character_id, world_seconds=world)
        calendar = self.calendar(instance)
        day = calendar.day_index(world)
        unit = narrative.weave(
            narrative.rank(
                narrative.materials(
                    experiences=snapshot.get("experiences") or [],
                    knowledge=snapshot.get("knowledge") or [],
                    world_seconds=world,
                    day_seconds=calendar.day_seconds,
                )
            ),
            day_seconds=calendar.day_seconds,
        )
        text = ""
        if llm is not None:
            if unit is None:
                # 手边没有可讲的近况：只打招呼，不补造趣事（§5.6 / SPEC §4.5）
                try:
                    text = await llm.chat(
                        self._first_contact_prompt(card, snapshot), temperature=0.7, timeout=45.0
                    )
                except Exception:
                    text = ""
            else:
                text, _findings = await self._speak_unit(
                    card,
                    unit,
                    activity=str(snapshot.get("current_activity") or ""),
                    llm=llm,
                    temperature=0.7,
                    opener=True,
                )
        if not proactive.proactive_text_allowed(text):
            return {"spoken": False, "reason": "开场没生成出来"}

        target = self.store.thread_get(channel_id, thread_id) or {}
        message_id = f"m-{__import__('secrets').token_hex(6)}"
        self.store.outbound_put(
            session_id=str(session["id"]),
            message_id=message_id,
            reply_to=None,
            covers=[],
            batches=self._channel_batches(
                str(text).strip(), channel_id=channel_id, max_text_len=max_text_len, max_parts=max_parts
            ),
            target_channel=channel_id,
            target_thread=thread_id,
            binding_version=int(target.get("binding_version") or 0),
            binding_token=str(target.get("binding_token") or ""),
        )
        self.store.first_contact_put(
            {
                "session_id": str(session["id"]),
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "character_id": character_id,
                "message_id": message_id,
                "at_world": world,
                "created_real": now,
            }
        )
        if unit is not None:
            self._record_narrative(
                unit,
                instance_id=instance_id,
                timeline_id=timeline_id,
                character_id=character_id,
                world=world,
                world_day=day,
                stage="spoken",
                message_id=message_id,
            )
        return {"spoken": True, "message_id": message_id, "world": world}

    def _first_contact_prompt(self, card: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, str]]:
        """开场只说她已经历 / 已获知的东西；没有素材就照实说，不编造时间进展。"""
        name = str((card.get("identity") or {}).get("name") or "她")
        knowledge = [
            f"- {str(item.get('text') or '')}"
            for item in (snapshot.get("knowledge") or [])[:5]
            if str(item.get("text") or "").strip()
        ]
        activity = str(snapshot.get("current_activity") or "")
        facts = "\n".join(knowledge) or "（她手边没有可讲的近况）"
        return [
            {
                "role": "system",
                "content": (
                    f"你是{name}。这是你第一次主动跟联络者开口，写一句自然的话（1-2 句，口语，"
                    "不要解释、不要加引号、不要列点、不要提设定或来源标签，也不要假装刚做完什么大事）。"
                    + (f"你此刻在做：{activity}。" if activity else "")
                ),
            },
            {"role": "user", "content": f"你可以提的近况（只用这些，没有就只打个招呼）：\n{facts}"},
        ]

    async def proactive_tick(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        llm: Any = None,
        now_real: float | None = None,
        per_day: int = 2,
        max_text_len: int = 0,
        max_parts: int = 0,
    ) -> dict[str, Any]:
        """世界源主动发言（§五）：按节律与配额，从她**已获知**的素材里挑一条固化成消息。

        不生成就算了——没有素材、在睡觉、额度用完、角色归档，任何一条都直接跳过；
        离线补算不补造过去每个发送时机（只按当前时刻评定一次）。
        """
        import time as _time

        if not self.compatible(instance_id):
            return {"spoken": 0, "messages": [], "skipped": {"compatibility": "实例兼容性阻断"}, "world": 0}

        now = _time.time() if now_real is None else float(now_real)
        instance = self.store.instance_get(instance_id)
        timeline = self.store.timeline_get(timeline_id) or {}
        if instance is None or not timeline:
            raise RuntimeStateError("实例或时间线不存在")
        if str(timeline.get("state") or "") != "active":
            return {"spoken": 0, "skipped": "时间线未激活"}

        clock = self.clock_row(timeline_id)
        world = int(clock["processed_world"])
        calendar = self.calendar(instance)
        day = calendar.day_index(world)
        spoken: list[dict[str, Any]] = []
        skipped: dict[str, str] = {}
        for card in self.cards(instance, timeline_id=timeline_id, world_seconds=world):
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id:
                continue
            if events.is_dead(
                instance_id,
                timeline_id,
                character_id,
                self.store.event_window(instance_id, timeline_id, until=world, limit=400),
            ):
                skipped[character_id] = "已归档"
                continue
            session = self.store.session_ensure(instance_id, timeline_id, character_id)
            target = self.store.thread_for_session(str(session["id"]))
            plan = self.store.plan_latest(instance_id, timeline_id, character_id)
            activity = life.activity_label(life.current_window(plan, world))
            knowledge = self.store.knowledge_window(
                instance_id, timeline_id, character_id, until=world, limit=40
            )
            experiences = self.store.experience_window(
                instance_id, timeline_id, character_id, until=world, limit=12
            )
            effects_now = self.store.effect_window(
                instance_id,
                timeline_id,
                until=world,
                targets=[character_id, str(card.get("role_id") or ""), region_of(card)],
            )
            consumed = self.store.proactive_consumed(
                instance_id, timeline_id, character_id
            ) | self.store.narrative_consumed_refs(instance_id, timeline_id, character_id)
            deferred_refs = self.store.narrative_deferred_refs(
                instance_id, timeline_id, character_id, world_day=day
            )
            ranked = narrative.rank(
                narrative.materials(
                    experiences=experiences,
                    knowledge=knowledge,
                    consumed=consumed,
                    world_seconds=world,
                    day_seconds=calendar.day_seconds,
                ),
                deferred_refs=deferred_refs,
            )
            ok, why = proactive.should_speak(
                archived=False,
                activity=activity,
                quota=proactive.quota_left(
                    self.store.proactive_day_count(
                        instance_id, timeline_id, character_id, world_day=day
                    ),
                    per_day,
                ),
                materials=ranked,
            )
            if not ok:
                skipped[character_id] = why
                continue
            unit = narrative.weave(ranked, day_seconds=calendar.day_seconds)
            if unit is None:
                skipped[character_id] = "没有可用素材"
                continue
            was_deferred = bool(deferred_refs & set(unit["refs"]))
            if llm is None:
                # 没有可用的生成器：不算她「没讲出口」，也不记暂缓
                skipped[character_id] = "生成失败"
                continue
            # 戏剧性 / 三幕 / 分享欲（§9.5）：只改「这会儿说不说、用什么口气」，
            # 不改候选集合与事实——世界照旧只从合法候选与受支持效果长出来。
            blocked = bool(
                life.effect_note(effects_now, character_id, str(card.get("role_id") or ""), region_of(card))
            )
            pending = [
                row
                for row in self.store.intent_list(instance_id, timeline_id, character_id)
                if str(row["stage"]) in ("adopted", "waiting", "deferred")
            ]
            drama_score = narrative.drama(unit, blocked=blocked, unresolved=bool(pending))
            drive = narrative.share_drive(self.store.unit_list(instance_id, timeline_id, character_id))
            if not narrative.willingness(drive, drama_score=drama_score):
                skipped[character_id] = "这会儿不想讲自己的事"
                continue
            act = narrative.act_of(unit, unresolved=bool(pending))
            generation = self._generation(timeline_id)
            text, findings = await self._speak_unit(
                card, unit, activity=activity, llm=llm, deferred=was_deferred, act=act
            )
            if not text:
                # 没讲出口也算一次取舍：记下来，本日内降级但不封死（§5.2）
                self._record_narrative(
                    unit,
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    character_id=character_id,
                    world=world,
                    world_day=day,
                    stage="deferred",
                    audit=findings,
                    note=str((findings[0] or {}).get("detail") or "") if findings else "",
                )
                skipped[character_id] = "素材在但没想好怎么说"
                continue
            if self._generation(timeline_id) != generation:
                # 生成期间回滚 / 重新激活：迟到的文本不写回去（NARRATIVE_LAYER §7.2）
                skipped[character_id] = "线已换代，迟到结果作废"
                continue
            message_id = f"m-{__import__('secrets').token_hex(6)}"
            target_channel = str((target or {}).get("channel_id") or "")
            batches = self._channel_batches(
                str(text).strip(), channel_id=target_channel, max_text_len=max_text_len, max_parts=max_parts
            )
            self.store.outbound_put(
                session_id=str(session["id"]),
                message_id=message_id,
                reply_to=None,
                covers=[],
                batches=batches,
                target_channel=target_channel,
                target_thread=str((target or {}).get("thread_id") or ""),
                binding_version=int((target or {}).get("binding_version") or 0),
                binding_token=str((target or {}).get("binding_token") or ""),
            )
            self.store.proactive_log_add(
                {
                    "instance_id": instance_id,
                    "timeline_id": timeline_id,
                    "character_id": character_id,
                    "world_day": int(day),
                    "material_ref": str(unit["primary"]),
                    "message_id": message_id,
                    "created_world": world,
                    "created_real": now,
                    "state": "fixed",
                }
            )
            self._record_narrative(
                unit,
                instance_id=instance_id,
                timeline_id=timeline_id,
                character_id=character_id,
                world=world,
                world_day=day,
                stage="spoken",
                message_id=message_id,
            )
            spoken.append({"character_id": character_id, "message_id": message_id, "material": unit["primary"]})
        return {"spoken": len(spoken), "messages": spoken, "skipped": skipped, "world": world}

    def _proactive_prompt(
        self,
        card: dict[str, Any],
        unit: dict[str, Any],
        activity: str,
        *,
        strict: bool = False,
        deferred: bool = False,
        opener: bool = False,
        act: str = "",
    ) -> list[dict[str, str]]:
        """一句话主动消息：素材来自她已获知 / 亲历的东西，不送秘密原文，也不许喊口号。

        结构性边界（只说这些 / 可以只讲一部分 / 不许把听来的说成亲历）随约束行一起进上下文
        （NARRATIVE_LAYER §6.1）；`strict` 是后验检查不过后的第二次尝试，只加提醒、不扩范围。
        """
        name = str((card.get("identity") or {}).get("name") or "她")
        who = f"这是你第一次主动跟联络者开口。你是{name}。" if opener else f"你是{name}。"
        head = (
            f"{who}用一句口语化的消息把下面的事告诉联络者，只写这一句，"
            "不要解释、不要加引号、不要列点、不要提设定或来源标签。"
        )
        if strict:
            head += narrative.strict_note()
        lines = narrative.constraint_lines(unit, activity=activity, deferred=deferred, act=act)
        return [
            {"role": "system", "content": head},
            {"role": "user", "content": "\n".join(lines)},
        ]

    async def _narrative_check(
        self, llm: Any, unit: dict[str, Any], text: str, *, activity: str = ""
    ) -> tuple[bool, str]:
        """后验一致性检查（NARRATIVE_LAYER §6.2）：结构检查先行，语义交给一次便宜判断。

        - 数字越界 / 空文本是确定性检查；
        - 来源、时间、范围、关系、处境要看语义：正文里没有可核对数字时再花一次调用问，
          问不出来（超时 / 解析失败）按通过，不误杀合法叙述（关键词从来不是唯一判据）。

        这笔调用走调用预算（§2.8）：它是每条可见回复的固定税，不能没有账。
        预算拒绝时按通过（与超时 / 解析失败一个口径），并留一条日志——注意管理面的预算视图
        只登记**已受理**的预占（`budget_reserve` 被拒时不写行），所以「这次没查」看日志，
        和 memory_extract / intent_propose 的拒绝惯例保持一致。
        """
        findings = narrative.audit(text, unit, activity=activity)
        if findings:
            return False, str(findings[0].get("detail") or findings[0].get("kind") or "")
        if llm is None or narrative.has_checkable_numbers(text):
            return True, ""
        prompt = narrative.audit_request(unit, text, activity=activity)
        prompt_text = "\n".join(str(item.get("content") or "") for item in prompt)
        instance_id = str(unit.get("instance_id") or "")
        timeline_id = str(unit.get("timeline_id") or "")
        reservation = None
        if instance_id and timeline_id:
            reservation = self.reserve_call(
                instance_id, timeline_id, "narrative_audit", prompt_text=prompt_text
            )
            if not reservation.get("ok"):
                log.info(
                    "narrative audit skipped line=%s blocked=%s", timeline_id, reservation.get("blocked")
                )
                return True, ""
        try:
            raw = await llm.chat(prompt, temperature=0.0, timeout=12.0)
        except Exception:
            if reservation is not None:
                self.settle_call(reservation, prompt_text=prompt_text, outcome="error")
            return True, ""
        if reservation is not None:
            self.settle_call(reservation, prompt_text=prompt_text, reply=str(raw or ""))
        parsed = narrative.parse_audit(str(raw))
        if parsed is None:
            return True, ""
        ok, why = parsed
        return ok, why

    async def _speak_unit(
        self,
        card: dict[str, Any],
        unit: dict[str, Any],
        *,
        activity: str,
        llm: Any,
        deferred: bool = False,
        temperature: float = 0.6,
        opener: bool = False,
        act: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        """按叙事单元生成一句话，并跑后验检查（有界重试，不扩大可见材料范围）。

        返回 (可用文本, 最后一次的检查结果)；文本为空表示两次都没过——调用方按「没讲出口」处理。
        """
        findings: list[dict[str, Any]] = []
        why = ""
        for attempt in range(2):
            text = ""
            if llm is not None:
                try:
                    text = await llm.chat(
                        self._proactive_prompt(
                            card, unit, activity, strict=attempt > 0, deferred=deferred, opener=opener, act=act
                        ),
                        temperature=temperature,
                        timeout=45.0,
                    )
                except Exception:
                    text = ""
            if not proactive.proactive_text_allowed(text):
                why = "生成失败" if not text else "素材在但没想好怎么说"
                continue
            ok, detail = await self._narrative_check(llm, unit, text, activity=activity)
            if ok:
                return str(text).strip(), []
            why = detail or "没讲出口"
            findings = [{"kind": "audit", "detail": detail}]
        return "", (findings or [{"kind": "audit", "detail": why}])

    def _generation(self, timeline_id: str) -> int:
        """运行世代：生成期间回滚 / 重新激活会让迟到的候选与文本作废（NARRATIVE_LAYER §7.2）。"""
        try:
            return int(self.clock_row(timeline_id).get("generation") or 0)
        except Exception:
            return -1

    def _pending_hint(
        self,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        card: dict[str, Any],
        calendar: Calendar,
        *,
        watermark: int,
        effects: list[dict[str, Any]],
        intents: list[dict[str, Any]],
    ) -> str:
        """她手上最悬着的那条线（§9.5-2/3）：戏剧性 + 三幕位置只**提示**她该想哪一步。

        提示只影响她自己的提案措辞与取舍；可行性与效果照旧由闭集与前置条件决定——
        评分不产生事实，也不放宽任何合法门槛。
        """
        unit = narrative.weave(
            narrative.rank(
                narrative.materials(
                    experiences=self.store.experience_window(
                        instance_id, timeline_id, character_id, until=watermark, limit=8
                    ),
                    knowledge=self.store.knowledge_window(
                        instance_id, timeline_id, character_id, until=watermark, limit=8
                    ),
                    world_seconds=watermark,
                    day_seconds=calendar.day_seconds,
                )
            ),
            day_seconds=calendar.day_seconds,
        )
        if unit is None:
            return ""
        unresolved = any(
            str(row.get("stage")) in ("adopted", "waiting", "deferred") for row in intents
        )
        blocked = bool(
            life.effect_note(effects, character_id, str(card.get("role_id") or ""), region_of(card))
        )
        if narrative.act_of(unit, unresolved=unresolved) not in ("起", "承"):
            return ""
        if narrative.drama(unit, blocked=blocked, unresolved=unresolved) < narrative.DRAMA_MIN:
            return ""
        return str(unit.get("topic") or "")

    async def audit_reply(
        self, unit: dict[str, Any] | None, text: str, *, llm: Any = None
    ) -> tuple[bool, str]:
        """会话问答轮的后验检查（§6.2 落地口径）：与主动路径同一套判据，不另立标准。"""
        if not unit:
            return True, ""
        return await self._narrative_check(
            llm, unit, str(text or ""), activity=str(unit.get("activity") or "")
        )

    def record_turn_unit(
        self,
        session: dict[str, Any],
        unit: dict[str, Any] | None,
        *,
        message_id: str,
        findings: list[dict[str, Any]] | None = None,
    ) -> None:
        """问答轮里她讲过这条线索：记成 spoken——与主动消息同一本账，别把同一件事讲第二遍。"""
        if not unit:
            return
        instance_id = str(session.get("instance_id") or "")
        timeline_id = str(session.get("timeline_id") or "")
        character_id = str(session.get("character_id") or "")
        if not (instance_id and timeline_id and character_id):
            return
        try:
            world = self.world_moment(instance_id, timeline_id)
        except Exception:
            return
        calendar = self.calendar(self.store.instance_get(instance_id) or {})
        self._record_narrative(
            unit,
            instance_id=instance_id,
            timeline_id=timeline_id,
            character_id=character_id,
            world=world,
            world_day=calendar.day_index(world),
            stage="spoken",
            message_id=str(message_id),
            audit=list(findings or []),
        )

    def narrative_map(
        self, instance_id: str, timeline_id: str, *, character_id: str | None = None
    ) -> dict[str, Any]:
        """故事图谱（§9.5-1）：她讲过的线索 + 没讲出口的记号 + 它们之间的关系。

        只给管理元数据与**用户已经看过**的正文；实情层、未获知内容与他人私聊一律不进（DESIGN §2.2-9）。
        """
        rows = self.store.narrative_unit_list(instance_id, timeline_id, character_id=character_id)

        def text_of(message_id: str) -> str:
            row = self.store.outbound_by_message_id(str(message_id)) if message_id else None
            return self.store.message_text(row) if row is not None else ""

        return narrative.map_payload(rows, text_of=text_of)

    def disclosure_candidates(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        from_character: str,
        to_character: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """跨角色披露的候选（§9.5-4）：从对方**讲过**的线索里挑，用户选定后才走 disclose 授权。

        自动的只是「挑出来摆到台面上」这一步；默认隔离不变——没有哪一条会自己走进接收角色的认知，
        授权仍走 `disclose()` 的显式确认（SESSION_CORE §7.1）。
        """
        if not from_character or not to_character or from_character == to_character:
            return []
        granted: set[str] = set()
        for row in self.store.disclosure_list(instance_id, timeline_id):
            if str(row.get("from_character")) != from_character or str(row.get("to_character")) != to_character:
                continue
            try:
                scope = json.loads(str(row.get("scope") or "{}"))
            except json.JSONDecodeError:
                scope = {}
            for item in scope.get("refs") or []:
                # scope.refs 存的是片段对象（ref/role/text），不是裸字符串
                granted.add(str(item.get("ref") or "") if isinstance(item, dict) else str(item))
        out: list[dict[str, Any]] = []
        for row in self.store.narrative_unit_list(instance_id, timeline_id, character_id=from_character):
            if str(row.get("stage")) != "spoken" or not str(row.get("message_id") or ""):
                continue
            ref = str(row["message_id"])
            if ref in granted:
                continue
            message = self.store.outbound_by_message_id(ref)
            if message is None:
                continue
            out.append(
                {
                    "ref": ref,
                    "unit": str(row.get("id") or ""),
                    "at_world": int(row.get("created_world") or 0),
                    "text": self.store.message_text(message)[:120],
                }
            )
            if len(out) >= max(1, int(limit)):
                break
        return out

    def _record_narrative(
        self,
        unit: dict[str, Any],
        *,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        world: int,
        world_day: int,
        stage: str,
        message_id: str = "",
        audit: list[dict[str, Any]] | None = None,
        note: str = "",
    ) -> None:
        """落一条叙事单元记录：讲出来的算消费，没讲出口的只降级（NARRATIVE_LAYER §7.1）。"""
        self.store.narrative_unit_put(
            {
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "character_id": character_id,
                "id": str(unit["id"]),
                "primary_ref": str(unit["primary"]),
                "refs": json.dumps([str(item) for item in unit["refs"]], ensure_ascii=False),
                "entry": str(unit.get("entry") or ""),
                "relation": str(unit.get("relation") or ""),
                "topic": str(unit.get("topic") or ""),
                "stage": str(stage),
                "message_id": str(message_id),
                "world_day": int(world_day),
                "audit": json.dumps(list(audit or []), ensure_ascii=False),
                "note": str(note),
                "created_world": int(unit.get("at_world") or world),
                "updated_world": int(world),
            }
        )

    def _channel_batches(
        self, text: str, *, channel_id: str, max_text_len: int = 0, max_parts: int = 0
    ) -> list[list[str]]:
        """按目标通道**协商的分段能力**分批（CHANNEL_PLUGIN_SPEC §2.4）。

        主动消息与独立开场也走同一条：固化时不拆，投递侧只会标 incompatible，
        这条消息就永远发不出去（普通回复在会话层已按同一口径分批）。
        """
        from ..session import plan_batches, split_parts  # 局部导入：session 顶层已依赖 runtime

        channel = self.store.channel_get(channel_id) if channel_id else None
        caps: dict[str, Any] = {}
        if channel is not None:
            try:
                caps = json.loads(str(channel.get("capabilities") or "{}"))
            except (json.JSONDecodeError, TypeError):
                caps = {}
        ceiling_len = int(max_text_len or DEFAULT_MAX_TEXT_LEN)
        ceiling_parts = int(max_parts or DEFAULT_MAX_PARTS)
        limit_len = min(int(caps.get("max_text_len") or ceiling_len), ceiling_len)
        if not caps.get("segments", False):
            limit_parts = 1
        else:
            limit_parts = min(int(caps.get("max_parts") or ceiling_parts), ceiling_parts)
        return plan_batches(split_parts(str(text), max(1, limit_len)), max(1, limit_parts))

    def _institution_rows(
        self,
        instance: dict[str, Any],
        instance_id: str,
        timeline_id: str,
        effects: list[dict[str, Any]],
        *,
        deaths: list[dict[str, Any]] | None = None,
        from_world: int,
        to_world: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """制度与惯例状态（阶段 6）：只吃合法事件效果，变化带来源与发生时刻。

        没有合法候选就什么都不写——不用配额或话术造变化。
        """
        package = self.setting(instance)["world_package"]
        offices = institutions.office_index(package)
        customs = institutions.custom_index(package)
        if not offices and not customs:
            return {"institution": [], "customs": []}
        rows = self.store.institution_list(instance_id, timeline_id)
        custom_state = self.store.custom_list(instance_id, timeline_id)
        if not rows and offices:
            rows = institutions.office_rows(
                package, instance_id=instance_id, timeline_id=timeline_id, world_seconds=from_world
            )
        if not custom_state and customs:
            custom_state = institutions.custom_rows(
                package, instance_id=instance_id, timeline_id=timeline_id, world_seconds=from_world
            )
        new_rows, new_customs = institutions.apply_effects(
            rows, custom_state, effects, package, world_seconds=to_world
        )
        if deaths:
            # 声明的延续规则：在任者身故即出缺（有依据：身故事件 + 包内声明）
            for vacated in institutions.vacancies_for_deaths(
                new_rows, deaths, package, world_seconds=to_world
            ):
                for index, row in enumerate(new_rows):
                    if str(row["office_id"]) == str(vacated["office_id"]):
                        new_rows[index] = vacated
        changed_rows = [row for row in new_rows if int(row["updated_world"]) == int(to_world)]
        changed_customs = [row for row in new_customs if int(row["updated_world"]) == int(to_world)]
        return {"institution": changed_rows, "customs": changed_customs}

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
                "priority": events.event_priority(candidate),
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

    def _entity_death_row(
        self, entity: dict[str, Any], *, instance_id: str, timeline_id: str, world_seconds: int, calendar: Calendar
    ) -> dict[str, Any] | None:
        """登记实体（要点人物）的寿终行：与角色卡身故同形，单独记账、可产生死讯说法（§四）。"""
        entity_id = str(entity.get("id") or "")
        died = entity.get("died")
        if not entity_id or not isinstance(died, int) or isinstance(died, bool):
            return None
        surrogate = {
            "meta": {"card_id": entity_id},
            "identity": {
                "name": str(entity.get("name") or "某人"),
                "born": int(entity.get("born") or 0) if isinstance(entity.get("born"), int) else 0,
                "died": int(died),
            },
        }
        instance = self.store.instance_get(instance_id) or {}
        row = events.death_event(
            surrogate,
            instance_id=instance_id,
            timeline_id=timeline_id,
            world_seconds=int(world_seconds),
            calendar=calendar,
            seed=self.seed_of(instance),
        )
        row.pop("_seed", None)
        return row

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
        # 登记实体的寿终（§四）：卡外的人也不是背景板——要点人物的生死同样登记为事件 + 死讯说法
        for entity in package.get("entities") or []:
            if not isinstance(entity, dict) or str(entity.get("kind") or "") != "person":
                continue
            entity_id = str(entity.get("id") or "")
            died = entity.get("died")
            if not entity_id or not isinstance(died, int) or isinstance(died, bool):
                continue
            if events.is_dead(instance_id, timeline_id, entity_id, known):
                continue
            if not (from_world < int(died) <= to_world):
                continue
            row = self._entity_death_row(
                entity, instance_id=instance_id, timeline_id=timeline_id, world_seconds=int(died), calendar=calendar
            )
            if row is None:
                continue
            out["events"].append(row)
            claims = events.dump_rows(
                events.claim_rows(
                    {"summary": row["summary"], "effects": []},
                    package=package,
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    event_ident=str(row["id"]),
                    world_seconds=int(died),
                    calendar=calendar,
                )
            )
            out["claims"].extend(claims)
            for other in cards:
                out["knowledge"].extend(
                    events.grants(row, claims, other, world_seconds=int(died), calendar=calendar)
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
        # 同族的后续事件（自然恢复的判定依据）：取**最早**那一条，解除时刻记它的世界时刻
        family_moments: dict[str, list[int]] = {}
        for item in self.store.event_window(instance_id, timeline_id, until=to_world, limit=200):
            family = str(item.get("family") or "")
            if family:
                family_moments.setdefault(family, []).append(int(item["world_seconds"]))
        for effect in self.store.effect_window(instance_id, timeline_id, until=to_world):
            expiry = str(effect.get("expiry"))
            started = int(effect["from_world"])
            # 解除时刻 = 条件首次成立的世界时刻，不取批边界：换一种分批方式不改变留档
            if expiry == "with_cause":
                moment = started + day_seconds
                if moment <= to_world:
                    rows["clear_effects"].append((str(effect["id"]), instance_id, moment))
            elif expiry == "natural_recovery":
                family = str(effect.get("family") or "")
                later = [moment for moment in family_moments.get(family, []) if moment > started]
                # 同族在该后果之后仍有新事件 → 声明的自然条件成立；没有依据就保持有效
                if later:
                    rows["clear_effects"].append((str(effect["id"]), instance_id, min(later)))
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
            package_for_state = self.setting(instance)["world_package"]
            if not self.store.institution_list(instance_id, timeline_id):
                for row in institutions.office_rows(
                    package_for_state,
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    world_seconds=world_seconds,
                ):
                    self.store.institution_put(row)
            if not self.store.custom_list(instance_id, timeline_id):
                for row in institutions.custom_rows(
                    package_for_state,
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    world_seconds=world_seconds,
                ):
                    self.store.custom_put(row)
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
                region_of(card),
            ],
        )
        activity = life.activity_label(life.current_window(plan, world_seconds))
        note = life.effect_note(
            effects,
            character_id,
            str(card.get("role_id") or ""),
            region_of(card),
        )
        if note and activity:
            activity = f"{activity}（受影响的后果：{note}）"
        world_package = self.setting(self.store.instance_get(instance_id) or {})["world_package"]
        reactions = self.store.reaction_list(instance_id, timeline_id, character_id=character_id)
        return {
            "units": personality.visible(units),
            "all_units": units,
            "plan": plan,
            "current_activity": activity,
            "experiences": experiences,
            "knowledge": knowledge,
            "effects": effects,
            "reactions": [row for row in reactions if str(row.get("stage")) in reaction.LIVE_STAGES],
            "reaction_tendency": reaction.tendency_block(reactions, watermark=world_seconds),
            "intents": [
                row
                for row in self.store.intent_list(instance_id, timeline_id, character_id)
                if str(row["stage"]) in ("adopted", "waiting", "deferred")
            ],
            "institutions": institutions.observations(
                self.store.institution_list(instance_id, timeline_id),
                self.store.custom_list(instance_id, timeline_id),
                _known_event_ids(self.store, instance_id, timeline_id, knowledge),
                world_seconds=world_seconds,
                effects=self.store.effect_window(
                    instance_id,
                    timeline_id,
                    until=world_seconds,
                    targets=[
                        str(row["office_id"])
                        for row in self.store.institution_list(instance_id, timeline_id)
                    ]
                    + [
                        str(row["custom_id"])
                        for row in self.store.custom_list(instance_id, timeline_id)
                    ],
                ),
                declared_offices={
                    office_id: str(office.get("holder") or "")
                    for office_id, office in _office_index(world_package).items()
                },
                declared_customs={
                    custom_id: str(custom.get("practice") or "")
                    for custom_id, custom in _custom_index(world_package).items()
                },
                holder_names={
                    str(item.get("id")): str(item.get("name"))
                    for item in self.setting(self.store.instance_get(instance_id) or {})[
                        "world_package"
                    ].get("entities") or []
                    if isinstance(item, dict) and item.get("id")
                },
            ),
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
        # 回填期一并确定要点人物的生死（§3.6 / §四）：早已身故的登记实体也补一条寿终事件与死讯说法，
        # 只登记事件与说法、不施加效果（沿用回填的边界）。
        calendar = self.calendar(instance)
        package = self.setting(instance)["world_package"]
        moment = int(instance["moment"] or 0)
        key_figures = set(
            events.backfill_key_figures(package, seed=self.seed_of(instance), rules_version=self.rules_of(instance))
        )
        for entity in package.get("entities") or []:
            if not isinstance(entity, dict) or str(entity.get("kind") or "") != "person":
                continue
            died = entity.get("died")
            if isinstance(died, int) and not isinstance(died, bool):
                if int(died) >= moment:
                    continue
            elif str(entity.get("id") or "") in key_figures:
                # 要点人物：回填期一并确定寿终（§3.6 条 2 / EVENT_ENGINE §四）。没被挑中的
                # 登记人物保留在册但不补造生死——缺寿命依据只留名，正是留白。
                derived = events.death_moment(
                    {
                        "identity": {
                            "race_id": entity.get("race_id"),
                            "born": entity.get("born"),
                            "died": None,
                            "lifespan": entity.get("lifespan"),
                        }
                    },
                    package,
                    calendar,
                )
                if not isinstance(derived, int) or int(derived) >= moment:
                    continue
                entity = {**entity, "died": int(derived)}
            else:
                continue
            row = self._entity_death_row(
                entity, instance_id=instance_id, timeline_id=timeline_id, world_seconds=int(entity["died"]), calendar=calendar
            )
            if row is None:
                continue
            rows.append(row)
            claims.extend(
                events.dump_rows(
                    events.claim_rows(
                        {"summary": row["summary"], "effects": []},
                        package=package,
                        instance_id=instance_id,
                        timeline_id=timeline_id,
                        event_ident=str(row["id"]),
                        world_seconds=int(entity["died"]),
                        calendar=calendar,
                    )
                )
            )
        if not rows:
            return 0
        # 创建期联合校验（§3.6 条 4）：产物立不住就不固化，别留下可运行的半个实例
        errors = events.backfill_product_errors(package, rows, claims)
        if errors:
            raise RuntimeStateError("历史回填未通过创建期联合校验：" + "；".join(errors[:5]))
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
                pending=self._pending_hint(
                    instance_id,
                    timeline_id,
                    character_id,
                    card,
                    calendar,
                    watermark=watermark,
                    effects=snapshot["effects"],
                    intents=snapshot["intents"],
                ),
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
        request_id: str = "",
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把角色锚定补入该线（§3.7 / 附录 B #18）。

        - 新角色一直存在：卡片自带个人史，补入只决定她自哪一刻起出现在本线；
        - 三处留痕：实例设定快照里的**不可变定义**、本线成员资格、本线**加入提交**；
        - 同一 `request_id` 重试返回原子发布结果，不重复登记；回滚撤销过的记录不能复活；
        - 可选 `event`：用户自定义的加入世界事件，走既有事件路径登记为世界事件；
        - 补入不激活该线：冻结线锚定冻结时刻（调用方随后自行决定是否激活）。
        """
        instance, timeline = self._rows(instance_id, timeline_id)
        calendar = self.calendar(instance)
        if request_id:
            prior = self.store.character_join_by_request(instance_id, timeline_id, request_id)
            if prior is not None:
                if str(prior.get("state") or "active") != "active":
                    raise RuntimeStateError("这条补入已被回滚撤销：要再补入请换新的请求标识（不能复活旧成员资格）")
                return {**self._join_public(prior, calendar, timeline), "reused": True}
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
        died = (card.get("identity") or {}).get("died")
        if isinstance(died, int) and died <= joined_world:
            raise RuntimeStateError("补入时刻该角色已身故，不能补入（个人史与世界既定历史冲突）")
        if self.store.character_membership(instance_id, timeline_id, character_id, until=watermark) == "joined":
            raise RuntimeStateError(f"该角色已在本线：{character_id}")
        # 定义是实例级不可变对象：同一 card_id 不允许第二份不同定义（含跨线补卡）
        for item in self.setting(instance).get("cards") or []:
            if str((item.get("meta") or {}).get("card_id") or "") != character_id:
                continue
            if json.dumps(item, ensure_ascii=False, sort_keys=True) != json.dumps(card, ensure_ascii=False, sort_keys=True):
                raise RuntimeStateError(
                    f"该角色已在本线（实例里已有同一标识的不可变定义）：补卡只增加角色，不能借同一标识改写既有角色卡 {character_id}"
                )
            break
        from ..world.cards import validate_card  # 局部导入：设定层与运行层不互相依赖

        errors = validate_card(card, self.setting(instance)["world_package"], moment=joined_world)
        if not bool((card.get("meta") or {}).get("confirmed")):
            errors.append("meta: 角色卡未确认，不能补入")
        if errors:
            raise RuntimeStateError("补入校验未通过：" + "；".join(str(item) for item in errors[:6]))
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
        plan = life.expand_plan(
            card,
            calendar,
            day_index=calendar.day_index(joined_world),
            instance_id=instance_id,
            timeline_id=timeline_id,
            created_world=joined_world,
        )
        # 定义进实例快照（实例级不可变对象）；成员资格、状态与日程只进本线——一次原子发布
        setting = self.setting(instance)
        definitions = [
            item for item in (setting.get("cards") or []) if str((item.get("meta") or {}).get("card_id") or "") != character_id
        ]
        definitions.append(card)
        join_row = {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "character_id": character_id,
            "joined_world": joined_world,
            "card": json.dumps(card, ensure_ascii=False, sort_keys=True),
            "note": note,
            "acquainted": 1 if acquainted else 0,
            "created_real": float(now_real),
            "request_id": str(request_id),
        }
        self.store.character_join_publish(
            join_row, units=units, plan=plan, setting={**setting, "cards": definitions}
        )
        # 加入提交：补卡在目标线留下的第三个锚点（回滚点 / 分叉点）
        commit = self.commit(instance_id, timeline_id, kind="join", note=f"补入角色 {character_id}")
        self.store.character_join_set_commit(instance_id, timeline_id, character_id, str(commit["id"]))
        event_ref = ""
        if event:
            # 用户自定义的加入世界事件：走既有待执行事件路径 + 即时施加（§3.7）
            pending_id = f"pe-join-{character_id}-{request_id or secrets.token_hex(4)}"
            self.store.pending_event_add(
                {
                    "id": pending_id,
                    "instance_id": instance_id,
                    "timeline_id": timeline_id,
                    "at_world": joined_world,
                    "payload": json.dumps(
                        {
                            "intent": str(event.get("summary") or event.get("intent") or "补卡时的世界事件"),
                            "effects": list(event.get("effects") or []),
                            "claims": list(event.get("claims") or []),
                        },
                        ensure_ascii=False,
                    ),
                    "state": "pending",
                    "note": "补卡加入事件",
                    "created_world": watermark,
                    "created_at": float(now_real),
                }
            )
            self.apply_due_pending_events(instance_id, timeline_id, to_world=joined_world)
            event_ref = f"ev-user-{events.stable_key(instance_id, timeline_id, pending_id)[:12]}"
        latest = self.store.character_join_by_request(instance_id, timeline_id, request_id) if request_id else None
        if latest is None:
            latest = next(
                (
                    item
                    for item in self.store.character_join_list(instance_id, timeline_id, until=watermark)
                    if str(item["character_id"]) == character_id
                ),
                join_row,
            )
        return {**self._join_public(latest, calendar, timeline), "event": event_ref}

    def _join_public(self, row: dict[str, Any], calendar: Any, timeline: dict[str, Any]) -> dict[str, Any]:
        """补入的公开结果：只给管理元数据与锚点，不带剧情摘要。"""
        joined_world = int(row.get("joined_world") or 0)
        return {
            "instance": str(row.get("instance_id") or ""),
            "timeline": str(row.get("timeline_id") or ""),
            "character": str(row.get("character_id") or ""),
            "name": str((json.loads(str(row.get("card") or "{}")).get("identity") or {}).get("name") or ""),
            "joined_world": joined_world,
            "joined_label": calendar.describe(joined_world),
            "commit_id": str(row.get("commit_id") or ""),
            "state": str(row.get("state") or "active"),
            "acquainted": bool(row.get("acquainted")),
            "timeline_state": str(timeline.get("state") or ""),
            "note": str(row.get("note") or ""),
            "units": len(self.store.unit_list(str(row.get("instance_id") or ""), str(row.get("timeline_id") or ""), str(row.get("character_id") or ""))),
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
        self,
        session: dict[str, Any],
        *,
        topic: str | None = None,
        now_real: float | None = None,
        with_unit: bool = False,
    ) -> Any:
        """会话层用的扮演定义：锁定设定 + 该角色截至当前水位的认知切片（无实情层注入）。

        `with_unit=True` 时连「本轮用到的叙事单元」一起返回（会话侧的后验检查要用它）。
        """
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
        prompt = cognition.render_prompt(context)
        block, unit = self._opening_block(
            snapshot,
            instance_id=str(session.get("instance_id") or ""),
            timeline_id=str(session.get("timeline_id") or ""),
            calendar=calendar,
            world=world,
            topic=topic,
        )
        if block:
            prompt = prompt + chr(10) + chr(10) + block
        if with_unit:
            return prompt, unit
        return prompt

    def _opening_block(
        self,
        snapshot: dict[str, Any],
        *,
        instance_id: str = "",
        timeline_id: str = "",
        calendar: Calendar,
        world: int,
        topic: str | None,
    ) -> tuple[str, dict[str, Any] | None]:
        """自然开场素材（SESSION_CORE §5.4 / NARRATIVE_LAYER §5）：给她「最近能提起的事」。

        只在没有明确查询主题的开场里给；材料全部来自她**已经历 / 已获知**的东西，
        没有素材就什么都不加（不补造趣事，也不因为多问几遍就多给）。返回 (提示块, 本轮单元)。
        """
        if not narrative.is_open_turn(str(topic or "")):
            return "", None
        unit = narrative.weave(
            narrative.rank(
                narrative.materials(
                    experiences=snapshot.get("experiences") or [],
                    knowledge=snapshot.get("knowledge") or [],
                    world_seconds=int(world),
                    day_seconds=calendar.day_seconds,
                    limit=6,
                )
            ),
            day_seconds=calendar.day_seconds,
            limit_extra=1,
        )
        if unit is None:
            return "", None
        activity = str(snapshot.get("current_activity") or "")
        unit["activity"] = activity  # 后验检查核数字时与约束行同一口径
        # 合成出来的 unit 不是数据库行：补上出处，后验审计才记得到账（§2.8）
        if instance_id:
            unit.setdefault("instance_id", str(instance_id))
        if timeline_id:
            unit.setdefault("timeline_id", str(timeline_id))
        return "\n".join(narrative.constraint_lines(unit, activity=activity)), unit
