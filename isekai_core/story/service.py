"""OC 故事层服务：编排入口（OC_STORY_LAYER_SPEC §3.2 / §4 / §八）。

分工（§3）：

- 世界事实、认知投影、会话固化、叙事候选、版本语义全在底层（WorldRuntime 对外接口 /
  会话核心 / 叙事中介）。本层**只读它们、只编排它们**；
- 本层负责：首次进入与联络入口、以角色 / 世界 / 时间线为中心的导航、输入分类、
  把核心错误翻译成产品状态、展示表达与时刻、引导保存分支 / 恢复版本；
- 本层不做：创建事实、绕过会话核心写消息、把候选或愿望写成世界事实、加平行真值（§3.3）。

场景级只能回 `preparing / available / catching_up / blocked`，轮次级才可能是
`generating / expressed / deferred / handoff`——两者都不是新状态机，只是底层结果的翻译（§4.4）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..log import get_logger
from ..ump import Err, UmpError
from . import classify, expression, state, view

log = get_logger("isekai.story")

#: 判断点的预算任务名（档位见 runtime/budget.py：已接受对话的一环，走高优先级档）
CLASSIFY_TASK = "story_classify"
#: 判断点超时：判不出来就按预筛 / 默认兜底，不拖住这一轮
CLASSIFY_TIMEOUT_S = 8.0
#: 首页带出的最近消息条数（§6.1 角色消息与投递状态）
HOME_MESSAGES = 8
#: 首次进入步骤（§4.1 的流程顺序）：标签是产品语言，客户端动作键由客户端映射到具体接口
ENTER_STEPS: tuple[tuple[str, str, str], ...] = (
    ("describe", "描述世界与角色", "generate_world"),
    ("review", "查看 / 修改生成结果", "review_generation"),
    ("confirm", "确认设定", "confirm_setting"),
    ("create", "创建锁定实例", "create_instance"),
    ("pick", "选择第一次联络的角色", "select_character"),
    ("talk", "进入会话", "open_session"),
)
#: 创作目录里不算「可用世界包」的后缀（草稿 / 候选 / 导出件）
_NON_PACKAGE_SUFFIX = (".candidate.json", ".draft.json", ".isekai.json")


class StoryService:
    """OC 故事层：把底层能力编排成「持续联络一个角色」的产品语义。"""

    def __init__(self, *, store: Any, cfg: Any = None, runtime: Any = None, server: Any = None) -> None:
        self.store = store
        self.cfg = cfg
        #: 世界运行层（装配时注入；缺了只影响读数，不影响产品状态的说法）
        self.runtime = runtime
        #: 核心服务对象：读 `state` 判断存储是否可用（§4.4 被阻断）
        self.server = server

    # ---------------------------------------------------------------- 基础读数

    def _clock(self, timeline_id: str) -> dict[str, Any]:
        runtime = self.runtime
        if runtime is not None and timeline_id:
            try:
                return dict(runtime.clock_row(timeline_id))
            except Exception:
                return {}
        try:
            return dict(self.store.clock_get(timeline_id) or {})
        except Exception:
            return {}

    def _character_name(self, instance: dict[str, Any], timeline_id: str, character_id: str, world: int) -> str:
        runtime = self.runtime
        if runtime is None:
            return character_id
        try:
            for card in runtime.cards(instance, timeline_id=timeline_id, world_seconds=world):
                if str((card.get("meta") or {}).get("card_id") or "") == character_id:
                    return str((card.get("identity") or {}).get("name") or character_id)
        except Exception:
            return character_id
        return character_id

    def _facts(self, instance_id: str, timeline_id: str, character_id: str = "") -> dict[str, Any]:
        """一次读齐场景判定要用的东西：作用域、线状态、追赶、兼容性、存储。"""
        instance = self.store.instance_get(instance_id) if instance_id else None
        timeline = self.store.timeline_get(timeline_id) if timeline_id else None
        if timeline is not None and instance is not None and str(timeline["instance_id"]) != instance_id:
            timeline = None
        session = self.store.session_find(instance_id, timeline_id, character_id) if (
            instance is not None and timeline is not None and character_id
        ) else None
        clock = self._clock(timeline_id) if timeline is not None else {}
        world = int(clock.get("processed_world") or 0)
        generation = int(clock.get("generation") or 0)
        reason = ""
        scope_ready = bool(instance is not None and timeline is not None)
        if instance is None:
            reason = "还没有这个世界实例" if instance_id else "还没有选择世界实例"
        elif timeline is None:
            reason = "这条生活线不存在"
        elif character_id:
            runtime = self.runtime
            try:
                known = runtime is not None and any(
                    str((card.get("meta") or {}).get("card_id") or "") == character_id
                    for card in runtime.cards(instance, timeline_id=timeline_id, world_seconds=world)
                )
            except Exception:
                known = False
            if not known:
                scope_ready = False
                reason = "这位角色还不在当前生活线里"
        compatibility_blocked = False
        if instance is not None and self.runtime is not None:
            try:
                compatibility_blocked = not self.runtime.compatible(instance_id)
            except Exception:
                compatibility_blocked = False
        catching = False
        target = world
        timeline_state = str((timeline or {}).get("state") or "")
        if instance is not None and timeline is not None and self.runtime is not None:
            try:
                scope = self.runtime.scope_inspect(instance_id, timeline_id)
                timeline_state = str(scope.get("timeline_state") or timeline_state)
                catching = timeline_state == "catching_up"
                target = int(scope.get("target_watermark") or world)
            except Exception:
                catching = False
        return {
            "instance": instance,
            "timeline": timeline,
            "session": session,
            "scope_ready": scope_ready,
            "reason": reason,
            "timeline_state": timeline_state,
            "catching_up": catching,
            "compatibility_blocked": compatibility_blocked,
            "persistence_blocked": str(getattr(self.server, "state", "") or "") == "persistence_blocked",
            "world": world,
            "target": target,
            "generation": generation,
        }

    def _scene_payload(self, facts: dict[str, Any], *, character_id: str = "") -> dict[str, Any]:
        product_state = state.scene_state(
            scope_ready=bool(facts["scope_ready"]),
            compatibility_blocked=bool(facts["compatibility_blocked"]),
            persistence_blocked=bool(facts["persistence_blocked"]),
            timeline_state=str(facts["timeline_state"]),
            catching_up=bool(facts["catching_up"]),
        )
        reason = str(facts["reason"])
        if not reason:
            if product_state == "catching_up":
                reason = "世界还在追赶：现在能用的是已经完成的那一刻"
            elif product_state == "blocked":
                if facts["persistence_blocked"]:
                    reason = "存储不可用：先不产生新内容"
                elif facts["compatibility_blocked"]:
                    reason = "这个实例的版本兼容性阻断：只读历史，等兼容版本或确认转换"
                else:
                    reason = f"这条生活线当前是 {facts['timeline_state']}，不能继续对话"
        instance = facts["instance"]
        session = facts["session"] or {}
        return state.envelope(
            instance_id=str((instance or {}).get("id") or ""),
            timeline_id=str((facts["timeline"] or {}).get("id") or ""),
            character_id=character_id,
            session_id=str(session.get("id") or ""),
            product_state=product_state,
            reason=reason,
            observed_revision=int(facts["world"]),
            world_time=int(facts["world"]),
            processed_watermark=int(facts["world"]),
            runtime_generation=int(facts["generation"]),
        )

    # ---------------------------------------------------------------- §4.4 场景 / §6 可见面

    def scene(self, instance_id: str, timeline_id: str, *, character_id: str = "") -> dict[str, Any]:
        """场景级主状态：打开角色时看到的那一个（§4.4）。"""
        return self._scene_payload(self._facts(instance_id, timeline_id, character_id), character_id=character_id)

    def home(self, instance_id: str, timeline_id: str, *, character_id: str) -> dict[str, Any]:
        """用户可见面（§6.1）：当前角色 / 世界 / 会话、已完成时刻、最近消息与投递、必要反馈。"""
        facts = self._facts(instance_id, timeline_id, character_id)
        scene = self._scene_payload(facts, character_id=character_id)
        instance = facts["instance"] or {}
        setting = {}
        if instance:
            try:
                setting = self.runtime.setting(instance) if self.runtime is not None else {}
            except Exception:
                setting = {}
        package = setting.get("world_package") if isinstance(setting.get("world_package"), dict) else {}
        meta = package.get("meta") if isinstance(package.get("meta"), dict) else {}
        world_name = str(meta.get("display_name") or setting.get("original_name") or instance.get("name") or "")
        clock = self._clock(timeline_id) if timeline_id else {}
        world = int(clock.get("processed_world") or 0)
        label = ""
        if instance and self.runtime is not None:
            try:
                label = self.runtime.calendar(instance).describe(world)
            except Exception:
                label = ""
        session = facts["session"] or {}
        thread = self.store.thread_for_session(str(session.get("id") or "")) if session else None
        messages: list[dict[str, Any]] = []
        if session:
            page = self.store.history_page(str(session["id"]), limit=HOME_MESSAGES)
            for row in page["messages"]:
                role = str(row.get("role") or "")
                delivery = self.store.delivery_rollup(int(row["seq"])) if role in ("character", "notice") else ""
                messages.append(
                    view.message_row(row, text=self.store.message_text(row), delivery=delivery)
                )
        notes = self._notes(facts, scene)
        return view.home_payload(
            scene=scene,
            character=(character_id, self._character_name(instance, timeline_id, character_id, world)),
            world_name=world_name,
            timeline=(timeline_id, str((facts["timeline"] or {}).get("name") or "")),
            session={
                "id": str(session.get("id") or ""),
                "channel": str((thread or {}).get("channel_id") or ""),
                "thread": str((thread or {}).get("thread_id") or ""),
            },
            time_info={
                "world_seconds": world,
                "world_label": label,
                "day_seconds": int((package.get("calendar") or {}).get("day_seconds") or 0),
                "real_seconds": time.time(),
                "target_watermark": int(facts.get("target") or world),
                "catching_up": bool(facts["catching_up"]),
            },
            messages=messages,
            notes=notes,
        )

    def _notes(self, facts: dict[str, Any], scene: dict[str, Any]) -> list[str]:
        """必要反馈（§6.1）：世界停止推进、版本阻断这类必须让用户知道的事。"""
        notes: list[str] = []
        if facts["persistence_blocked"]:
            notes.append("存储不可用：现在不产生新内容，恢复后再继续。")
        if facts["compatibility_blocked"]:
            notes.append("这个实例需要兼容处理：可以先只读历史，或确认转换。")
        if facts["catching_up"]:
            notes.append("世界正在追赶：此刻显示的是已经完成的那一刻，目标时刻还没发生。")
        if str(facts["timeline_state"]) in ("frozen", "archived"):
            notes.append(
                "这条生活线已冻结（或已归档）：历史可读，恢复推进需要先激活或换一条线。"
            )
        if scene.get("product_state") == "preparing" and scene.get("reason"):
            notes.append(str(scene["reason"]))
        return notes

    # ---------------------------------------------------------------- §3.5 轮次

    def turn(
        self, instance_id: str, timeline_id: str, *, character_id: str, seq: int | None = None
    ) -> dict[str, Any]:
        """一轮联络的产品结果（§3.5）：expressed / waiting / blocked / deferred / handoff。"""
        facts = self._facts(instance_id, timeline_id, character_id)
        scene = self._scene_payload(facts, character_id=character_id)
        session = facts["session"] or {}
        inbound = None
        if session:
            row = self.store.message_get(int(seq)) if seq else self.store.last_inbound(str(session["id"]))
            if row is not None and str(row.get("role") or "") == "user" and str(row["session_id"]) == str(session.get("id")):
                inbound = row
        if inbound is None:
            product_state = state.turn_state(scene=str(scene["product_state"]))
            return state.envelope(
                instance_id=instance_id,
                timeline_id=timeline_id,
                character_id=character_id,
                session_id=str(session.get("id") or ""),
                product_state=product_state,
                reason=str(scene.get("reason") or "还没有联络记录"),
                observed_revision=int(facts["world"]),
                world_time=int(facts["world"]),
                processed_watermark=int(facts["world"]),
                runtime_generation=int(facts["generation"]),
            )
        reply_id = str(inbound.get("message_id") or inbound.get("reply_message_id") or "")
        reply = self.store.outbound_by_message_id(reply_id) if reply_id else None
        error_code = str(inbound.get("error_code") or "")
        product_state = state.turn_state(
            scene=str(scene["product_state"]),
            inbound_state=str(inbound.get("state") or ""),
            error_code=error_code,
            has_reply=reply is not None,
        )
        extra: dict[str, Any] = {"seq": int(inbound["seq"]), "input_state": str(inbound.get("state") or "")}
        if product_state == "expressed" and reply is not None:
            batches = len(self.store.delivery_rows(int(reply["seq"])))
            extra["delivery"] = state.delivery_extra(self.store.delivery_rollup(int(reply["seq"])), batches=batches)
            extra["message_id"] = str(reply["message_id"] or "")
        if product_state == "deferred" and error_code:
            extra["error_kind"] = state.error_kind(error_code)
        if product_state in ("deferred", "blocked") and error_code:
            extra["note"] = state.KIND_NOTES[state.error_kind(error_code)]
        if product_state == "handoff":
            target = error_code[len(state.HANDOFF_PREFIX) :]
            extra["handoff"] = target
            extra["note"] = state.KIND_NOTES["routing"]
        return state.envelope(
            instance_id=instance_id,
            timeline_id=timeline_id,
            character_id=character_id,
            session_id=str(session.get("id") or ""),
            product_state=product_state,
            reason=str(scene.get("reason") or ""),
            observed_revision=int(facts["world"]),
            world_time=int(facts["world"]),
            processed_watermark=int(facts["world"]),
            runtime_generation=int(facts["generation"]),
            extra=extra,
        )

    # ---------------------------------------------------------------- §3.4 输入分类

    def _reserve_classify(self, instance_id: str, timeline_id: str, prompt_text: str) -> Any:
        """判断点也要走调用预算（§2.8）：它是每轮联络的固定税，不能没有账。"""
        runtime = self.runtime
        if runtime is None or not (instance_id and timeline_id):
            return None
        try:
            reservation = runtime.reserve_call(instance_id, timeline_id, CLASSIFY_TASK, prompt_text=prompt_text)
        except Exception:
            log.exception("classify reservation failed")
            return None
        if not reservation.get("ok"):
            log.info("classify skipped instance=%s blocked=%s", instance_id, reservation.get("blocked"))
            return None
        return reservation

    def _settle_classify(self, reservation: Any, *, prompt_text: str, reply: str, outcome: str = "ok") -> None:
        if reservation is None or self.runtime is None:
            return
        try:
            self.runtime.settle_call(reservation, prompt_text=prompt_text, reply=reply, outcome=outcome)
        except Exception:
            log.exception("classify settle failed")

    async def classify(
        self,
        text: str,
        *,
        llm: Any = None,
        instance_id: str = "",
        timeline_id: str = "",
        character_id: str = "",
    ) -> dict[str, Any]:
        """定这一轮的唯一主类别（§3.4）：结构性请求走预筛，其余问一次模型，失败兜底。

        分类**不改变权限**：返回值只说明走哪条路，`handoff` 也只是「这一轮不在普通联络里执行」。
        """
        body = str(text or "")
        if classify.prefilter(body) in classify.HANDOFF_TARGETS:
            return classify.decide(body)  # 预筛硬命中：省一次调用
        model: tuple[str, str] | None = None
        if llm is not None:
            prompt = classify.classify_request(body)
            prompt_text = "\n".join(str(item.get("content") or "") for item in prompt)
            reservation = self._reserve_classify(instance_id, timeline_id, prompt_text)
            raw = ""
            try:
                # 不传小的 max_tokens：推理型模型会把预算烧在推理上返回空文本（本仓库既有坑）
                raw = await llm.chat(prompt, temperature=0.0, timeout=CLASSIFY_TIMEOUT_S)
            except Exception:
                log.info("classify call failed instance=%s character=%s", instance_id, character_id)
                self._settle_classify(reservation, prompt_text=prompt_text, reply="", outcome="error")
            else:
                self._settle_classify(reservation, prompt_text=prompt_text, reply=str(raw or ""))
                model = classify.parse_classify(raw)
        verdict = classify.decide(body, model=model)
        log.debug(
            "classify category=%s source=%s character=%s", verdict["category"], verdict["source"], character_id
        )
        return verdict

    # ---------------------------------------------------------------- §五 表达契约

    def deferred_topics(self, instance_id: str, timeline_id: str, character_id: str) -> list[str]:
        """她没讲出口的事（只进提示词，不给用户看）：追问时的边界就取这里。"""
        if not (instance_id and timeline_id and character_id):
            return []
        try:
            rows = self.store.narrative_unit_list(instance_id, timeline_id, character_id=character_id)
        except Exception:
            return []
        return expression.unit_topics(rows)

    def expression_block(self, instance_id: str, timeline_id: str, character_id: str, *, followup: bool) -> str:
        """本层给会话核心的唯一增量：表达契约（§五），不改她知道什么。"""
        return expression.turn_block(
            deferred_topics=self.deferred_topics(instance_id, timeline_id, character_id), followup=bool(followup)
        )

    def is_followup(self, text: str, *, category: str = "") -> bool:
        """追问口径：分类结果优先，没有就用预筛兜（进程重启后也能按文本判）。"""
        return str(category) in ("followup", "status_inquiry") or classify.prefilter(text) == "followup"

    def handoff_notice(self, target: str) -> str:
        """转交说明文本（会话层以 system_notice 上线，不冒充她的回复）。"""
        return classify.notice_for(target)

    # ---------------------------------------------------------------- §八 版本与创作

    def _require_runtime(self) -> Any:
        if self.runtime is None:
            raise UmpError(Err.INTERNAL, "运行层不可用：无法执行版本操作", retryable=True)
        return self.runtime

    def _commit_of(self, instance_id: str, timeline_id: str, commit_id: str) -> dict[str, Any]:
        if not commit_id:
            raise UmpError(Err.INVALID, "缺少 commit_id", retryable=False)
        commit = self.store.commit_get(commit_id)
        if commit is None or str(commit["instance_id"]) != instance_id:
            raise UmpError(Err.NOT_FOUND, f"没有该提交：{commit_id}", retryable=False)
        if str(commit["timeline_id"]) != timeline_id:
            raise UmpError(Err.INVALID, "只能操作本线自己的提交", retryable=False)
        return commit

    def branch(self, instance_id: str, timeline_id: str, *, commit_id: str, name: str = "") -> dict[str, Any]:
        """保留分支（§八）：从旧提交开出另一条生活线，原线继续保留。

        本层只编排：分叉本身仍由运行层的提交 / 分叉语义完成，不自己复制或修补状态（§3.3）。
        """
        runtime = self._require_runtime()
        self._commit_of(instance_id, timeline_id, commit_id)
        result = runtime.fork(instance_id, timeline_id, commit_id=commit_id, name=name)
        tl = result.get("timeline") or {}
        return {
            "status": "ok",
            "action": "branch",
            "commit_id": commit_id,
            "timeline": {"id": str(tl.get("id") or ""), "name": str(tl.get("name") or ""), "state": str(tl.get("state") or "")},
            "note": "分支继承共同过去；原线继续保留，两条线之后的表达与状态互不回流。",
            "must_not_imply": "原线已经被替换",
        }

    def restore(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        commit_id: str,
        confirm: bool = False,
        saved: bool = False,
        acknowledge_unsaved: bool = False,
    ) -> dict[str, Any]:
        """恢复版本（§八）：先说明覆盖范围、先给保存路径，再覆盖当前线。

        - 未确认 / 未保存：只回覆盖范围与保存路径，**不动世界**；
        - 已投递出去的内容不宣称可撤回（§7 表 / 验收）；
        - 不自行复制、合并或修补运行状态：执行仍走运行层回滚语义。
        """
        runtime = self._require_runtime()
        self._commit_of(instance_id, timeline_id, commit_id)
        snapshot = self.store.commit_snapshot_get(commit_id) or {}
        now_world = int((self._clock(timeline_id)).get("processed_world") or 0)
        target_world = int(snapshot.get("world") or 0)
        delivered = int(self.store.timeline_delivered_replies(timeline_id))
        coverage = {
            "commit_id": commit_id,
            "to_world": target_world,
            "now_world": now_world,
            "delta_seconds": max(0, now_world - target_world),
            "delivered_replies": delivered,
            "note": "当前线会回到那一时刻；之后的故事不再属于当前线。",
        }
        save_paths = [
            {
                "action": "branch",
                "label": "把现在的进展保存为另一条生活线",
                "params": ["commit_id", "name"],
            },
            {"action": "export", "label": "导出当前实例留档", "params": ["path"]},
        ]
        warning = (
            f"有 {delivered} 条回复已经投递到外部通道（用户可能已经读过），恢复不保证撤回它们。"
            if delivered
            else "恢复覆盖本线核心历史：请先确认没有需要保留的进展。"
        )
        if not confirm or not (saved or acknowledge_unsaved):
            return {
                "status": "waiting",
                "action": "restore",
                "product_state": "blocked",
                "reason": "恢复是覆盖操作：先确认覆盖范围，并先保存当前进展（保存为分支或导出）",
                "coverage": coverage,
                "save_paths": save_paths,
                "warning": warning,
                "must_not_imply": state.MUST_NOT_IMPLY["blocked"],
            }
        result = runtime.rollback(instance_id, timeline_id, commit_id=commit_id)
        return {
            "status": "ok",
            "action": "restore",
            "coverage": coverage,
            "warning": str(result.get("warning") or warning),
            "result": {
                "generation": int(result.get("generation") or 0),
                "world": int(result.get("world") or 0),
                "voided_inputs": int(result.get("voided_inputs") or 0),
                "cancelled_replies": int(result.get("cancelled_replies") or 0),
                "delivered_replies_kept": int(result.get("delivered_replies_kept") or 0),
            },
            "must_not_imply": "已经投递出去的内容可以撤回",
        }

    # ---------------------------------------------------------------- §4.1 首次进入

    def _packages(self) -> list[Path]:
        root = getattr(getattr(self.cfg, "paths", None), "packages", None)
        if root is None:
            return []
        try:
            return [
                path
                for path in sorted(Path(root).glob("*.json"))
                if not path.name.endswith(_NON_PACKAGE_SUFFIX)
            ]
        except OSError:
            return []

    def enter(self, *, instance_id: str = "", timeline_id: str = "", character_id: str = "") -> dict[str, Any]:
        """首次进入 / 日常开场（§4.1）：把「下一步做什么」用产品语言说清，自己不创建任何东西。

        内部术语（实例 / 时间线 / 认知）不是必经步骤：每一步都只描述用户要做的事，
        客户端动作键另行给出（§4.1 末段）。
        """
        packages = self._packages()
        instances = self.store.instance_list()
        facts = self._facts(instance_id, timeline_id, character_id) if instance_id and timeline_id else {}
        characters: list[dict[str, str]] = []
        if facts.get("instance") and self.runtime is not None:
            world = int(facts.get("world") or 0)
            try:
                for card in self.runtime.cards(facts["instance"], timeline_id=timeline_id, world_seconds=world):
                    characters.append(
                        {
                            "card_id": str((card.get("meta") or {}).get("card_id") or ""),
                            "name": str((card.get("identity") or {}).get("name") or ""),
                        }
                    )
            except Exception:
                characters = []
        session = facts.get("session") or {}
        thread = self.store.thread_for_session(str(session.get("id") or "")) if session else None
        done = {
            "describe": bool(packages),
            "review": bool(packages),
            "confirm": bool(packages),
            "create": bool(instances),
            "pick": bool(characters),
            "talk": bool(session and thread),
        }
        steps = [
            view.enter_step(key, label, client_action=action, done=done.get(key, False))
            for key, label, action in ENTER_STEPS
        ]
        scene = (
            self._scene_payload(facts, character_id=character_id)
            if facts
            else state.envelope(
                instance_id="", timeline_id="", product_state="preparing", reason="还没有选择世界实例"
            )
        )
        return {
            **scene,
            "steps": steps,
            "instances": [{"id": str(row["id"]), "name": str(row.get("name") or "")} for row in instances],
            "characters": characters,
            "note": "按步骤做完就能开始联络：首次流程不需要先弄懂底层概念，也不用先配置什么。",
        }
