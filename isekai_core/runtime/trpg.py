"""TRPG 战役运行时（TRPG_CAMPAIGN_RUNTIME_SPEC）：战役 / 场景 / 行动 / 待选择的编排与联合提交。

分层（§一）：本模块不认识属性、骰点、职业、资源等规则语义；规则语义在插件里，
世界真值在 WorldRuntime 里。本模块只做三件事：

- 持有战役编排状态（战役、场景、行动、待选择）并守住状态机；
- 托管规则私有状态附件的版本边界（读写 revision，不解析内容）；
- 把「规则状态 patch + 世界后果 + 场景转换」放进**同一个提交单元**（`store.apply_runtime_batch`）。

联合提交是本模块存在的理由：插件裁定成功 ≠ 世界已经改变，而规则扣了资源、世界没变
（或反之）都是半条状态。任一步不合法，三类状态一起不落盘。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..log import get_logger
from . import campaign as campaign_mod

#: 因果来源细分（TRPG_CAMPAIGN_RUNTIME_SPEC §二十一 残余第 1 条 + TRPG_RULE_COMMON_MODULE_SPEC §105
#: 的 source 轴：规则裁定 / GM 宣告 / 世界过程）。`action` 只由行动路径内部使用，
#: 其余可直接由 `trpg.gm.change(source=…)` 指定；事件落库用右边的来源标识。
SOURCE_EVENT: dict[str, str] = {
    "action": "trpg_action",                 # 角色行动（经插件裁定）
    "gm_declaration": "gm_declaration",      # GM 直接裁定
    "world_process": "trpg_world_process",   # 世界自身的 NPC / 环境推进（与玩家行动分开记账）
    "npc_script": "trpg_npc_script",         # 剧本 / NPC 自动行为（不经骰点插件）
}
#: 直声明路径（`trpg.gm.change`）可指定的来源；`action` 不在此列（行动走行动路径）
DIRECT_SOURCES: tuple[str, ...] = ("gm_declaration", "world_process", "npc_script")
from . import drafts, rules

log = get_logger("isekai.trpg")

#: 联合提交的合法终态（§12.3）
COMMIT_STATUSES = ("committed", "duplicate", "rejected", "conflict", "needs_review", "stale")


class CampaignRuntimeError(campaign_mod.CampaignError):
    """战役运行时的可预期错误（调用方翻成管理面错误码）。

    继承 `CampaignError` 是有意的：管理面只在一个地方把这一族翻成 `invalid_input`，
    另起炉灶的兄弟异常会漏成 internal（§12 错误分类）。
    """


#: 失败态不可直达时走这条合法路径（§11.2 状态机：冲突 / 过期只能从 committing 出）
#: 已经结束、只在 recent 里露面的行动状态
_CLOSED_ACTION_STATES = ("transitioned", "abandoned", "rejected")

_FAIL_PATHS: dict[tuple[str, str], tuple[str, ...]] = {
    ("reviewing", "conflict"): ("committing", "conflict"),
    ("reviewing", "stale"): ("committing", "stale"),
    ("reviewing", "awaiting_gm_review"): ("awaiting_gm_review",),
    ("resolving", "awaiting_gm_review"): ("reviewing", "awaiting_gm_review"),
    ("reviewing", "rejected"): ("rejected",),
}


def _loads(text: Any, fallback: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(str(text or ""))
    except (TypeError, ValueError):
        return fallback


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


class CampaignRuntime:
    """核心托管的战役编排（一个核心一份，随 RuntimeService 一起构造）。"""

    def __init__(self, store: Any, runtime: Any) -> None:
        self.store = store
        self.runtime = runtime
        # 常驻规则插件会话：按清单路径复用（状态仍走快照，见 rules.RulePluginSession）
        self.rule_sessions: dict[str, rules.RulePluginSession] = {}
        self.rule_session_idle_s = 900.0  # 闲置这么久就收掉，下次重开；不起后台扫

    # ------------------------------------------------------------ 战役

    def create(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        ruleset_id: str,
        ruleset_version: str = "",
        plugin_manifest: str = "",
        participants: list[str] | None = None,
        status: str = "active",
        note: str = "",
        scene: dict[str, Any] | None = None,
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """建立战役：绑定实例 / 时间线 / 规则版本；可选同时开首个场景。"""
        self._require_line(instance_id, timeline_id)
        if not str(ruleset_id or "").strip():
            raise CampaignRuntimeError("战役必须声明 ruleset_id")
        if status not in ("preparing", "active"):
            raise CampaignRuntimeError("新战役只能是 preparing 或 active")
        campaign_mod.transition("campaign", "preparing", status, what="战役")
        now = float(now_real if now_real is not None else time.time())
        world = self._world(instance_id, timeline_id)
        row = {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "campaign_id": campaign_mod.new_id("cp"),
            "ruleset_id": str(ruleset_id),
            "ruleset_version": str(ruleset_version or ""),
            "plugin_manifest": str(plugin_manifest or ""),
            "participants": _dumps(list(participants or [])),
            "current_scene_id": "",
            "state_revision": 1,
            "status": status,
            "note": str(note or ""),
            "created_world": world,
            "updated_world": world,
            "created_real": now,
            "updated_real": now,
        }
        rows: dict[str, Any] = {"campaign": [row]}
        if scene:
            scene_row = self._scene_row(instance_id, timeline_id, row["campaign_id"], world, **scene)
            row["current_scene_id"] = scene_row["scene_id"]
            rows["scene"] = [scene_row]
        self.store.trpg_upserts(rows)
        return {**campaign_mod.public_campaign(row), "scene_id": row["current_scene_id"]}

    def campaigns(self, instance_id: str, timeline_id: str | None = None) -> list[dict[str, Any]]:
        keys: dict[str, Any] = {"instance_id": instance_id}
        if timeline_id:
            keys["timeline_id"] = timeline_id
        return [campaign_mod.public_campaign(row) for row in self.store.trpg_list("campaign", **keys)]

    def info(self, instance_id: str, timeline_id: str, campaign_id: str) -> dict[str, Any]:
        return campaign_mod.public_campaign(self._campaign_row(instance_id, timeline_id, campaign_id))

    def status(
        self,
        instance_id: str,
        timeline_id: str,
        campaign_id: str,
        *,
        status: str,
        reason: str = "",
        accept_ruleset_version: str = "",
    ) -> dict[str, Any]:
        """战役状态迁移：全部记录原因，不接受隐式跳转（§11.1）。

        `accept_ruleset_version`：人工确认规则状态改用新版本解释（§十六 没有转换器时的
        唯一合法出口）。它会把规则状态行的版本重铸到新值，所以必须在同一批里写，
        并把这次接受记进 note——不接受「改个字符串让闸门闭嘴」。
        """
        row = self._campaign_row(instance_id, timeline_id, campaign_id)
        target = campaign_mod.transition("campaign", str(row["status"]), str(status), what="战役")
        accepted = str(accept_ruleset_version or "").strip()
        now = time.time()
        rows: dict[str, Any] = {}
        if accepted and accepted != str(row["ruleset_version"] or ""):
            old = str(row["ruleset_version"] or "")
            row["ruleset_version"] = accepted
            row["note"] = f"接受规则版本 {old or '(未声明)'} → {accepted}（无转换器，人工确认）"
            state = self.store.trpg_get(
                "rule_state", instance_id=instance_id, timeline_id=timeline_id,
                campaign_id=campaign_id, ruleset_id=str(row["ruleset_id"]),
            )
            if state is not None:
                rows["rule_state"] = [{**state, "ruleset_version": accepted, "updated_real": now}]
        elif target == str(row["status"]) and not rows:
            return campaign_mod.public_campaign(row)
        row = {
            **row,
            "status": target,
            "note": str(row.get("note") or reason or ""),
            "updated_world": self._world(instance_id, timeline_id),
            "updated_real": now,
        }
        rows["campaign"] = [row]
        self.store.trpg_upserts(rows)
        return campaign_mod.public_campaign(row)

    # ------------------------------------------------------------ 场景

    def _scene_row(
        self,
        instance_id: str,
        timeline_id: str,
        campaign_id: str,
        world: int,
        *,
        scene_id: str | None = None,
        kind: str = "exploration",
        location_refs: list[str] | None = None,
        participants: list[str] | None = None,
        public_facts: list[Any] | None = None,
        private_views: dict[str, Any] | None = None,
        active_risks: list[Any] | None = None,
        available_actions: list[Any] | None = None,
        turn_state: dict[str, Any] | None = None,
        status: str = "open",
    ) -> dict[str, Any]:
        return {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "campaign_id": campaign_id,
            "scene_id": str(scene_id or campaign_mod.new_id("sc")),
            "kind": str(kind or "exploration"),
            "location_refs": _dumps(list(location_refs or [])),
            # 只记引用（世界快照标识 + 水位），不复制世界事实正文（§3.2）
            "world_snapshot": _dumps({"world": int(world), "revision": int(world)}),
            "participants": _dumps(list(participants or [])),
            "public_facts": _dumps(list(public_facts or [])),
            "private_views": _dumps(dict(private_views or {})),
            "active_risks": _dumps(list(active_risks or [])),
            "available_actions": _dumps(list(available_actions or [])),
            "turn_state": _dumps(dict(turn_state or {})),
            "status": str(status),
            "revision": 1,
            "created_world": int(world),
            "updated_world": int(world),
        }

    def open_scene(
        self, instance_id: str, timeline_id: str, campaign_id: str, **fields: Any
    ) -> dict[str, Any]:
        row = self._campaign_row(instance_id, timeline_id, campaign_id)
        if str(row["status"]) == "archived":
            raise CampaignRuntimeError("归档战役不能开新场景")
        world = self._world(instance_id, timeline_id)
        scene = self._scene_row(instance_id, timeline_id, campaign_id, world, **fields)
        row = {**row, "current_scene_id": scene["scene_id"], "updated_world": world, "updated_real": time.time()}
        self.store.trpg_upserts({"scene": [scene], "campaign": [row]})
        return {**scene, "world_snapshot": _loads(scene["world_snapshot"], {})}

    def view(
        self, instance_id: str, timeline_id: str, campaign_id: str, *, audience: Any = "public_party"
    ) -> dict[str, Any]:
        """可行动局面投影：战役 + 当前场景 + 未结行动 + 开放待选择 + 规则状态版本（不含内容）。

        `audience` 可以是单个受众，也可以是上层为「同一用户的多个角色」显式传进来的一串（取并集）——
        核心不把 `user:` 猜成角色（§十五 / §二十一 残余第 2 条）。
        """
        campaign_row = self._campaign_row(instance_id, timeline_id, campaign_id)
        scene_id = str(campaign_row.get("current_scene_id") or "")
        scene = (
            self.store.trpg_get(
                "scene", instance_id=instance_id, timeline_id=timeline_id,
                campaign_id=campaign_id, scene_id=scene_id,
            )
            if scene_id
            else None
        )
        if not campaign_mod.audience_ok(audience):
            raise CampaignRuntimeError(
                f"未知受众：{audience}（§十五 闭集：public_party / gm_only / player: / character: / npc:；"
                "同一用户的多个角色要并就显式传一串，核心不做用户级归并）"
            )
        keys = {"instance_id": instance_id, "timeline_id": timeline_id, "campaign_id": campaign_id}
        all_actions = [
            row for row in self.store.trpg_list("action", **keys)
            if campaign_mod.audience_visible(str(row.get("audience") or ""), audience)
        ]
        actions = [
            campaign_mod.action_view(row, audience=audience)
            for row in all_actions
            if str(row["status"]) not in _CLOSED_ACTION_STATES
        ]
        recent = [
            campaign_mod.action_view(row, audience=audience)
            for row in all_actions
            if str(row["status"]) in _CLOSED_ACTION_STATES
        ][-5:]
        choices = [
            {**row, "choices": _loads(row.get("choices"), [])}
            for row in self.store.trpg_list("choice", **keys)
            if str(row["status"]) == "open"
            and campaign_mod.audience_visible(str(row.get("audience") or ""), audience)
        ]
        state = self.store.trpg_get("rule_state", ruleset_id=str(campaign_row["ruleset_id"]), **keys)
        out: dict[str, Any] = {
            "campaign": campaign_mod.public_campaign(campaign_row),
            "actions": actions,
            "recent": recent,
            "pending_choices": choices,
            "rule_state": {
                "ruleset_id": str(campaign_row["ruleset_id"]),
                "state_revision": int(state["state_revision"]) if state else 0,
            },
        }
        if scene is not None:
            # 场景材料按受众裁剪（§十五）：私密视图只给对应受众，GM 拿全份
            out["scene"] = campaign_mod.scene_view(scene, audience=audience)
        return out

    # ------------------------------------------------------------ 行动

    def declare(
        self,
        instance_id: str,
        timeline_id: str,
        campaign_id: str,
        *,
        actor_id: str,
        raw_text: str = "",
        intent: str = "",
        target_refs: list[str] | None = None,
        method: str = "",
        expected_result: str = "",
        preconditions: list[str] | None = None,
        visible_risks: list[str] | None = None,
        auto_confirm: bool = False,
        action_id: str | None = None,
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """行动声明（§3.3）：落到 interpreted；关键行动停在 awaiting_confirmation。"""
        campaign_row = self._campaign_row(instance_id, timeline_id, campaign_id)
        self._require_live(campaign_row, what="声明行动")
        intent = str(intent or raw_text or "").strip()
        if not intent:
            raise CampaignRuntimeError("行动声明缺少意图（intent 或 raw_text 至少一个）")
        now = float(now_real if now_real is not None else time.time())
        world = self._world(instance_id, timeline_id)
        row = {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "campaign_id": campaign_id,
            "scene_id": str(campaign_row.get("current_scene_id") or ""),
            "action_id": str(action_id or campaign_mod.new_id("act")),
            "actor_id": str(actor_id or ""),
            "raw_text": str(raw_text or ""),
            "intent": intent,
            "target_refs": _dumps(list(target_refs or [])),
            "method": str(method or ""),
            "expected_result": str(expected_result or ""),
            "preconditions": _dumps(list(preconditions or [])),
            "visible_risks": _dumps(list(visible_risks or [])),
            "confirmation": "confirmed" if auto_confirm else "pending",
            "action_revision": 1,
            "status": "confirmed" if auto_confirm else "awaiting_confirmation",
            "created_world": world,
            "updated_world": world,
            "created_real": now,
            "updated_real": now,
        }
        # received → interpreted 是同一时刻的内部步骤，落库时直接给最终态
        campaign_mod.transition("action", "received", "interpreted", what="行动")
        campaign_mod.transition(
            "action", "interpreted", "confirmed" if auto_confirm else "awaiting_confirmation", what="行动"
        )
        self.store.trpg_upserts({"action": [row]})
        return campaign_mod.action_view(row, audience="gm_only")

    def confirm(
        self,
        instance_id: str,
        timeline_id: str,
        campaign_id: str,
        action_id: str,
        *,
        action_revision: int,
        changes: dict[str, Any] | None = None,
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """确认 / 修改（§11.2）：同一 action 只能有一个确认版本，修改要涨 revision。"""
        row = self._action_row(instance_id, timeline_id, campaign_id, action_id)
        campaign_row = self._campaign_row(instance_id, timeline_id, campaign_id)
        self._require_live(campaign_row, what="确认行动")
        if str(row["status"]) not in ("awaiting_confirmation", "interpreted", "modified"):
            raise CampaignRuntimeError(f"该行动当前状态不能确认：{row['status']}")
        if int(action_revision) != int(row["action_revision"]):
            raise CampaignRuntimeError(
                f"行动版本不一致：当前 {row['action_revision']}，请求 {action_revision}"
            )
        revised = dict(row)
        if changes:
            for field in ("intent", "method", "expected_result", "target_refs", "visible_risks"):
                if field in changes:
                    value = changes[field]
                    revised[field] = _dumps(value) if field in ("target_refs", "visible_risks") else str(value)
            revised["action_revision"] = int(row["action_revision"]) + 1
            campaign_mod.transition("action", str(row["status"]), "modified", what="行动")
        target = campaign_mod.transition("action", str(row["status"]), "confirmed", what="行动")
        revised.update(
            {
                "status": target,
                "confirmation": "confirmed",
                "updated_world": self._world(instance_id, timeline_id),
                "updated_real": float(now_real if now_real is not None else time.time()),
            }
        )
        self.store.trpg_upserts({"action": [revised]})
        return campaign_mod.action_view(revised, audience="gm_only")

    def abandon(
        self, instance_id: str, timeline_id: str, campaign_id: str, action_id: str, *, reason: str = ""
    ) -> dict[str, Any]:
        row = self._action_row(instance_id, timeline_id, campaign_id, action_id)
        status = str(row["status"])
        if status in ("committed", "transitioned", "abandoned"):
            raise CampaignRuntimeError(f"该行动已经结束：{status}")
        row = {
            **row,
            "status": campaign_mod.transition("action", status, "abandoned", what="行动"),
            "confirmation": "abandoned",
            "failure_code": "abandoned",
            "resolution": row.get("resolution") if not reason else _dumps({"note": reason}),
            "updated_world": self._world(instance_id, timeline_id),
            "updated_real": time.time(),
        }
        self.store.trpg_upserts({"action": [row]})
        return campaign_mod.action_view(row, audience="gm_only")

    async def resolve(
        self,
        instance_id: str,
        timeline_id: str,
        campaign_id: str,
        action_id: str,
        *,
        plugin_manifest: str,
        world_snapshot: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """调用规则插件（§四 5. resolve）：**只拿裁定**，不写世界、不写规则状态。

        插件崩溃 / 超时 / 输出非法 → 行动进 plugin_failed，不落半条结果（§12.2）。
        """
        row = self._action_row(instance_id, timeline_id, campaign_id, action_id)
        campaign_row = self._campaign_row(instance_id, timeline_id, campaign_id)
        self._require_live(campaign_row, what="裁定行动")
        status = str(row["status"])
        if status in ("committing", "committed", "transitioned"):
            raise CampaignRuntimeError(f"该行动已经提交，不能重新裁定：{status}")
        if str(row["confirmation"]) != "confirmed" and status != "interrupted":
            raise CampaignRuntimeError("未确认的关键行动不得进入裁定链（§11.2）")
        if status in ("reviewing", "awaiting_choice", "awaiting_gm_review") and _loads(
            row.get("resolution"), {}
        ):
            raise CampaignRuntimeError("该行动已有裁定结果，请先提交或放弃")
        ruleset_id = str(campaign_row["ruleset_id"])
        state = self.store.trpg_get(
            "rule_state", instance_id=instance_id, timeline_id=timeline_id,
            campaign_id=campaign_id, ruleset_id=ruleset_id,
        )
        identity = rules.manifest_identity(plugin_manifest)
        manifest_ruleset = str(identity.get("ruleset_id") or "")
        if manifest_ruleset and manifest_ruleset != ruleset_id:
            raise CampaignRuntimeError(
                f"插件与战役规则系统不一致：插件 {manifest_ruleset}，战役 {ruleset_id}"
            )
        self._require_compatible(campaign_row, state, plugin_manifest)
        world = self._world(instance_id, timeline_id)
        request = {
            "type": "resolve_action",
            "protocol": "isekai.trpg.rules/1",
            "campaign_id": campaign_id,
            "scene_id": str(row.get("scene_id") or ""),
            "action_id": action_id,
            "action_revision": int(row["action_revision"]),
            "actor_id": str(row["actor_id"]),
            "intent": str(row["intent"]),
            "world_snapshot": {
                "snapshot_id": str((world_snapshot or {}).get("snapshot_id") or ""),
                "revision": int((world_snapshot or {}).get("revision") or world),
            },
            "rule_state": {
                "ruleset_id": ruleset_id,
                # 告诉插件「你要解释的是哪个版本写的状态」，不是战役创建时随手写的字符串
                "ruleset_version": str(
                    (state.get("ruleset_version") if state else "") or campaign_row["ruleset_version"] or ""
                ),
                "state_revision": int(state["state_revision"]) if state else 0,
                "opaque_state": _loads(state["opaque_state"], {}) if state else {},
            },
            "context": _loads(row.get("preconditions"), {}),
        }
        self._set_action_status(row, "snapshotting")
        self._set_action_status(self._action_row(instance_id, timeline_id, campaign_id, action_id), "resolving")
        try:
            session = await self._rule_session(plugin_manifest or str(campaign_row["plugin_manifest"]))
            result = await rules.resolve(
                plugin_manifest or str(campaign_row["plugin_manifest"]),
                request, timeout=timeout, session=session,
            )
        except rules.RulePluginError as exc:
            self._set_action_status(self._action_row(instance_id, timeline_id, campaign_id, action_id),
                                    "plugin_failed", failure_code="plugin_failed")
            raise CampaignRuntimeError(str(exc)) from exc
        normalized = normalize_result(result)
        if normalized["errors"]:
            failed = self._action_row(instance_id, timeline_id, campaign_id, action_id)
            self._fail(failed, "awaiting_gm_review", code="needs_review")
            return {"status": "needs_review", "errors": normalized["errors"], "action_id": action_id}
        row = {
            **self._action_row(instance_id, timeline_id, campaign_id, action_id),
            "status": campaign_mod.transition(
                "action", str(self._action_row(instance_id, timeline_id, campaign_id, action_id)["status"]),
                "reviewing", what="行动",
            ),
            "resolution": _dumps(normalized["payload"]),
            "updated_world": world,
            "updated_real": time.time(),
        }
        self.store.trpg_upserts({"action": [row]})
        return {
            "status": "reviewing",
            "action_id": action_id,
            "resolution": normalized["resolution"],
            "rule_state_patch": normalized["rule_state_patch"],
            "consequences": normalized["consequences"],
            "scene_transition": normalized["scene_transition"],
            "participants": normalized["participants"],
            "legacy_effects": normalized["legacy_effects"],
        }

    # ------------------------------------------------------------ 联合提交

    def commit(
        self,
        instance_id: str,
        timeline_id: str,
        campaign_id: str,
        action_id: str,
        *,
        idempotency_key: str,
        audience: str = "public_party",
        source_mode: str = "action",
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """行动路径的联合提交（§十二）：规则状态 patch + 世界后果 + 场景转换，同批成功或同批失败。

        `source_mode`：`action` = 角色行动的因果后果；`gm_declaration` = GM 直接裁定
        （§十五 来源标注，审计面要能区分这两种，别混成一个来源）。
        """
        if not str(idempotency_key or "").strip():
            raise CampaignRuntimeError("联合提交必须带幂等键")
        existing = self.store.trpg_commit_by_key(instance_id, timeline_id, idempotency_key)
        if existing is not None:
            return {**_loads(existing["result"], {}), "status": "duplicate",
                    "joint_commit_id": str(existing["joint_commit_id"])}
        campaign_row = self._campaign_row(instance_id, timeline_id, campaign_id)
        self._require_live(campaign_row, what="提交行动")
        action_row = self._action_row(instance_id, timeline_id, campaign_id, action_id)
        status = str(action_row["status"])
        if status == "committed":
            return self._result_of(str(action_row["joint_commit_id"]))
        if status not in ("reviewing", "awaiting_choice", "awaiting_gm_review", "conflict", "stale"):
            raise CampaignRuntimeError(f"该行动当前状态不能提交：{status}")
        payload = _loads(action_row.get("resolution"), {})
        if not payload:
            raise CampaignRuntimeError("该行动还没有裁定结果，先跑 resolve")
        return self._joint_apply(
            campaign_row, payload,
            instance_id=instance_id, timeline_id=timeline_id, campaign_id=campaign_id,
            action_row=action_row, action_id=action_id, source_mode=source_mode,
            idempotency_key=idempotency_key, audience=audience, now_real=now_real,
        )

    def gm_change(
        self,
        instance_id: str,
        timeline_id: str,
        campaign_id: str,
        *,
        changes: dict[str, Any],
        idempotency_key: str,
        audience: str = "public_party",
        now_real: float | None = None,
        source: str = "gm_declaration",
    ) -> dict[str, Any]:
        """GM 直接变化（§十五）：不过行动、不过插件，直接提交后果。

        `changes` = `{consequences[]?, claims[]?, participants[]?, rule_state_patch?, scene_transition?}`，
        与插件响应同形状但不含 `resolution`——所以它走的是同一条联合提交管线，
        规则状态与世界后果照样同批成功或同批失败，来源落 `gm_declaration`。
        """
        if not isinstance(changes, dict) or not changes:
            raise CampaignRuntimeError("GM 直接变化必须给出 changes 对象")
        for key in ("consequences", "claims", "participants"):
            if key in changes and not isinstance(changes[key], list):
                raise CampaignRuntimeError(f"changes.{key} 必须是数组")
        if not str(idempotency_key or "").strip():
            raise CampaignRuntimeError("联合提交必须带幂等键")
        if source not in DIRECT_SOURCES:
            # §二十一 残余第 1 条：直声明的来源要能细分（GM 裁定 / 世界过程 / 剧本推进），
            # 角色行动不在这里——它有自己的行动路径与规则裁定。
            raise CampaignRuntimeError(
                f"未知来源：{source}（直声明只接受 {' / '.join(DIRECT_SOURCES)}；角色行动请走行动路径）"
            )
        existing = self.store.trpg_commit_by_key(instance_id, timeline_id, idempotency_key)
        if existing is not None:
            return {**_loads(existing["result"], {}), "status": "duplicate",
                    "joint_commit_id": str(existing["joint_commit_id"])}
        campaign_row = self._campaign_row(instance_id, timeline_id, campaign_id)
        self._require_live(campaign_row, what="提交 GM 裁定")
        payload = {**changes, "resolution": {"system": "gm", "outcome": "declared"}}
        return self._joint_apply(
            campaign_row, payload,
            instance_id=instance_id, timeline_id=timeline_id, campaign_id=campaign_id,
            action_row=None, action_id="", source_mode=source,
            idempotency_key=idempotency_key, audience=audience, now_real=now_real,
        )

    def _joint_apply(
        self,
        campaign_row: dict[str, Any],
        payload: dict[str, Any],
        *,
        instance_id: str,
        timeline_id: str,
        campaign_id: str,
        action_row: dict[str, Any] | None,
        action_id: str,
        source_mode: str,
        idempotency_key: str,
        audience: str,
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """唯一的一条联合提交管线（§十二）：行动与 GM 直接变化共用它。

        `action_row=None` 表示这次没有行动在背后（GM 直接变化）：不做行动状态迁移，
        场景取战役当前场景，其余（规则状态、世界后果、时间消耗、幂等账本）完全一致。
        """
        if source_mode not in SOURCE_EVENT:
            raise CampaignRuntimeError(
                f"未知来源：{source_mode}（只接受 {' / '.join(SOURCE_EVENT)}）"
            )
        if not campaign_mod.audience_ok(audience):
            raise CampaignRuntimeError(f"未知受众：{audience}（§十五 闭集）")
        status = str(action_row["status"]) if action_row is not None else ""
        ruleset_id = str(campaign_row["ruleset_id"])
        state_key = {
            "instance_id": instance_id, "timeline_id": timeline_id,
            "campaign_id": campaign_id, "ruleset_id": ruleset_id,
        }
        state = self.store.trpg_get("rule_state", **state_key)
        self._require_compatible(campaign_row, state, str(campaign_row.get("plugin_manifest") or ""))
        patch = payload.get("rule_state_patch") or None
        patch_errors = campaign_mod.validate_patch(patch)
        if patch_errors:
            return self._review(action_row, patch_errors)
        new_state = None
        patch_paths: list[str] = []
        merged_from: list[int] = []
        if patch:
            if str(patch.get("ruleset_id")) != ruleset_id:
                return self._review(action_row, ["rule_state_patch.ruleset_id 与战役不一致"])
            base = int(patch.get("base_state_revision", -1))
            current = int(state["state_revision"]) if state else 0
            if base != current:
                # §二十一 残余第 4 条：分片合并——base 落后但**触及路径与中间提交不相交**时并入当前 revision；
                # 有交集 / 中间记录缺失一律照旧冲突（不确定就别猜）。
                merged_from = self._mergeable_revisions(
                    instance_id, timeline_id, campaign_id, ruleset_id, base=base, current=current, patch=patch
                )
                if merged_from is None:
                    return self._conflict(action_row, current_revision=current, requested=base)
                patch = {**patch, "base_state_revision": current}
            patch_paths = sorted(
                {str(item.get("path") or "") for item in patch.get("operations") or [] if isinstance(item, dict)}
            )
            try:
                new_state = campaign_mod.apply_patch(
                    _loads(state["opaque_state"], {}) if state else {}, list(patch.get("operations") or [])
                )
            except campaign_mod.CampaignError as exc:
                return self._review(action_row, [str(exc)])

        instance = self.store.instance_get(instance_id) or {}
        world = self._world(instance_id, timeline_id)
        consequences = payload.get("consequences")
        if consequences is None:
            consequences = payload.get("effects") or []
        claims = payload.get("claims") or []
        draft_payload = {
            "intent": str((action_row or {}).get("intent") or ("GM 直接裁定" if action_row is None else "")),
            "effects": [
                {**item, "expiry": str(item.get("expiry") or "with_cause")}
                for item in consequences if isinstance(item, dict)
            ],
            "claims": claims,
            "participants": payload.get("participants") or [],
        }
        try:
            targets, channels = self.runtime._known_targets(instance, timeline_id, world_seconds=world)
            normalized = drafts.normalize_draft(
                self.runtime.setting(instance)["world_package"], draft_payload,
                known_targets=targets, world_seconds=world, default_channels=channels,
            )
        except ValueError as exc:
            return self._review(action_row, [f"世界后果无法映射：{exc}"])

        clock = self.runtime.clock_row(timeline_id)
        ident = f"ev-trpg-{campaign_mod.new_id('x').split('-')[1]}"
        event_rows = self.runtime._user_event_rows(
            instance_id, timeline_id, normalized, ident=ident, world=world,
            source=SOURCE_EVENT[source_mode],
            template="trpg.action",
        )
        event_rows["event"]["detail"] = _dumps({"campaign_id": campaign_id, "action_id": action_id,
                                                "resolution": payload.get("resolution") or {}})
        institution_rows = self.runtime._institution_rows(
            instance, instance_id, timeline_id, event_rows["effects"], deaths=[], from_world=world, to_world=world
        )
        environment_rows = self.runtime._environment_rows(
            instance, instance_id, timeline_id, self.runtime.calendar(instance), event_rows["effects"],
            from_world=world, to_world=world,
        )

        # 场景转换：待选择只在战役侧，不进世界事实（§5.4）
        transition = payload.get("scene_transition") or {}
        # 场景内时间消耗（§十四）：只有结构化、有原因、为正秒数的请求才允许改动世界时间。
        # 这里只校验；实际前移锚点与规则状态 patch 在同一个批次里落（下面 apply_runtime_batch）。
        time_request = transition.get("world_time_request") or None
        shift_seconds = 0
        time_cause = ""
        time_source = ""
        if time_request is not None:
            if not isinstance(time_request, dict):
                return self._review(action_row, ["world_time_request 必须是对象（{seconds, cause?}）"])
            try:
                shift_seconds = int(time_request.get("seconds") or 0)
            except (TypeError, ValueError):
                return self._review(action_row, ["world_time_request.seconds 必须是整数"])
            if shift_seconds <= 0:
                return self._review(action_row, ["world_time_request.seconds 必须为正秒数"])
            time_cause = str(time_request.get("cause") or "").strip() or f"TRPG 行动 {action_id}"
            # 来源分类（§十四 世界过程 / 玩家行动 / GM 裁定分开记账）：不给就按路径推
            time_source = str(
                time_request.get("source") or ("player_action" if action_row is not None else "gm_declaration")
            )
            if time_source not in ("world_process", "player_action", "gm_declaration"):
                return self._review(action_row, [f"world_time_request.source 不在闭集内：{time_source}"])
        scene_rows: list[dict[str, Any]] = []
        choice_rows: list[dict[str, Any]] = []
        scene = None
        if transition:
            scene = self.store.trpg_get(
                "scene", instance_id=instance_id, timeline_id=timeline_id,
                campaign_id=campaign_id,
                scene_id=str((action_row or {}).get("scene_id") or campaign_row["current_scene_id"] or ""),
            )
            if scene is not None:
                scene = self._apply_transition(scene, transition, world)
                scene_rows.append(scene)
            for index, item in enumerate(transition.get("available_choices") or []):
                if not campaign_mod.audience_ok(item.get("audience") or campaign_mod.PUBLIC_PARTY):
                    return self._review(
                        action_row, [f"available_choices[{index}] 的受众不在闭集内：{item.get('audience')}"]
                    )
                choice_rows.append(
                    {
                        "instance_id": instance_id,
                        "timeline_id": timeline_id,
                        "campaign_id": campaign_id,
                        "scene_id": str(scene["scene_id"]) if scene
                        else str((action_row or {}).get("scene_id") or campaign_row["current_scene_id"] or ""),
                        "choice_id": str(item.get("choice_id") or campaign_mod.new_id("ch")),
                        "action_id": action_id,
                        "prompt_ref": str(item.get("prompt_ref") or ""),
                        "choices": _dumps(list(item.get("options") or item.get("choices") or [])),
                        "audience": str(item.get("audience") or "public_party"),
                        "created_revision": int(scene["revision"]) if scene else 1,
                        "status": "open",
                        "created_real": time.time(),
                        "created_world": world,
                    }
                )

        joint_id = campaign_mod.new_id("jc")
        action_out = None
        if action_row is not None:
            next_status = "awaiting_choice" if choice_rows else "transitioned"
            campaign_mod.transition("action", status, "committing", what="行动")
            action_out = {
                **action_row,
                "status": next_status,
                "audience": str(audience),
                "joint_commit_id": joint_id,
                "failure_code": "",
                "updated_world": world,
                "updated_real": time.time(),
            }
        next_campaign_status = str(campaign_row["status"])
        if choice_rows and next_campaign_status != "waiting":
            next_campaign_status = campaign_mod.transition(
                "campaign", next_campaign_status, "waiting", what="战役"
            )
        campaign_out = {
            **campaign_row,
            "status": next_campaign_status,
            "state_revision": int(campaign_row["state_revision"]) + 1,
            "current_scene_id": str(scene["scene_id"]) if scene else campaign_row["current_scene_id"],
            "updated_world": world,
            "updated_real": time.time(),
        }
        state_out = None
        if patch and new_state is not None:
            state_out = {
                **state_key,
                # 记「写这份状态的插件声明的规则版本」：版本闸比对的基准（§十六 第 2 层）
                "ruleset_version": str(
                    rules.manifest_identity(str(campaign_row.get("plugin_manifest") or "")).get(
                        "ruleset_version"
                    )
                    or campaign_row["ruleset_version"]
                    or ""
                ),
                "state_revision": (int(state["state_revision"]) if state else 0) + 1,
                "opaque_state": _dumps(new_state),
                "created_world": int(state["created_world"]) if state else world,
                "updated_world": world,
                "updated_real": time.time(),
            }
        result = {
            "status": "committed",
            "joint_commit_id": joint_id,
            "audience": str(audience),
            "campaign_revision": campaign_out["state_revision"],
            "world_revision": world,
            "world_commit_id": "",
            "state_revisions": {ruleset_id: int(state_out["state_revision"]) if state_out else (
                int(state["state_revision"]) if state else 0
            )},
            "scene_id": campaign_out["current_scene_id"],
            "scene_revision": int(scene["revision"]) if scene else 0,
            "open_choices": [str(item["choice_id"]) for item in choice_rows],
            "effects": len(event_rows["effects"]),
            "claims": len(event_rows["claims"]),
            "world_time_request": time_request,
            "world_time_applied": bool(shift_seconds),
            "world_time_source": time_source,
            "patch_paths": patch_paths,
            "merged_from": merged_from,
        }
        commit_row = {
            "joint_commit_id": joint_id,
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "campaign_id": campaign_id,
            "action_id": str(action_id or ""),
            "idempotency_key": str(idempotency_key),
            "campaign_revision": int(campaign_out["state_revision"]),
            "world_revision": world,
            "state_revisions": _dumps(result["state_revisions"]),
            "status": "committed",
            "result": _dumps(result),
            "created_world": world,
            "created_real": time.time(),
        }
        trpg_rows: dict[str, Any] = {
            "campaign": [campaign_out],
            "commit": [commit_row],
        }
        if action_out is not None:
            trpg_rows["action"] = [action_out]
        if state_out is not None:
            trpg_rows["rule_state"] = [state_out]
        if scene_rows:
            trpg_rows["scene"] = scene_rows
        if choice_rows:
            trpg_rows["choice"] = choice_rows
        applied = self.store.apply_runtime_batch(
            timeline_id=timeline_id,
            generation=int(clock["generation"]),
            processed_world=world,
            catching_up=False,
            events=[event_rows["event"]],
            claims=event_rows["claims"],
            knowledge=event_rows["knowledge"],
            effects=event_rows["effects"],
            environment=environment_rows,
            institution=institution_rows["institution"],
            customs=institution_rows["customs"],
            trpg=trpg_rows,
            clock_shift_seconds=shift_seconds,
        )
        if not applied:
            # 世代已变（冻结 / 回滚后的迟到提交）：整批没落盘，如实返回 stale，不假装提交过
            if action_row is not None:
                self._fail(
                    self._action_row(instance_id, timeline_id, campaign_id, action_id), "stale", code="stale"
                )
            return {"status": "stale", "action_id": str(action_id or ""), "reason": "运行世代已变，请重新读取场景"}
        if shift_seconds:
            # 锚点已在上面那一批里前移（与规则状态同一个事务）；这里按正常批次结算这段时间。
            # 结算读数只出现在首次返回值里——账本是只增不改的，重放拿原结果 + runtime.clock 看收尾。
            settled = self.runtime.advance(instance_id, timeline_id, now_real=time.time())
            result["world_time_settled"] = {
                "state": str(settled.get("state") or ""),
                "processed_world": int(settled.get("processed_world") or 0),
                "batches": int(settled.get("batches") or 0),
                "cause": time_cause,
                "source": time_source,
            }
        return result

    # ------------------------------------------------------------ 待选择

    def select_choice(
        self,
        instance_id: str,
        timeline_id: str,
        campaign_id: str,
        choice_id: str,
        *,
        selection: str,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """选择（§11.3）：过期或已选的重试返回原结果，不重新裁定。"""
        campaign_row = self._campaign_row(instance_id, timeline_id, campaign_id)
        self._require_live(campaign_row, what="作出选择", allow_waiting=True)
        row = self.store.trpg_get(
            "choice", instance_id=instance_id, timeline_id=timeline_id,
            campaign_id=campaign_id, choice_id=choice_id,
        )
        if row is None:
            raise CampaignRuntimeError(f"没有这个待选择：{choice_id}")
        if str(row["status"]) == "selected":
            return {**row, "choices": _loads(row["choices"], []), "duplicate": True}
        if str(row["status"]) != "open":
            raise CampaignRuntimeError(f"该待选择已经关闭：{row['status']}")
        options = _loads(row["choices"], [])
        if options and selection not in [str(item) if not isinstance(item, dict) else str(item.get("id") or item.get("value") or "") for item in options]:
            raise CampaignRuntimeError(f"选择不在候选项内：{selection}")
        row = {**row, "status": "selected", "selection": str(selection), "updated_real": time.time()}
        others = [
            item for item in self.store.trpg_list(
                "choice", instance_id=instance_id, timeline_id=timeline_id, campaign_id=campaign_id
            )
            if str(item["choice_id"]) != choice_id and str(item["status"]) == "open"
        ]
        campaign_out = {
            **campaign_row,
            # 还有别的待选择就继续 waiting，别让战役提前回到可行动（§11.1）
            "status": campaign_mod.transition("campaign", str(campaign_row["status"]), "active", what="战役")
            if str(campaign_row["status"]) == "waiting" and not others
            else str(campaign_row["status"]),
            "state_revision": int(campaign_row["state_revision"]) + 1,
            "updated_world": self._world(instance_id, timeline_id),
            "updated_real": time.time(),
        }
        self.store.trpg_upserts({"choice": [row], "campaign": [campaign_out]})
        return {**row, "choices": options, "idempotency_key": str(idempotency_key or ""), "duplicate": False}

    async def migrate_ruleset(
        self,
        instance_id: str,
        timeline_id: str,
        campaign_id: str,
        *,
        converter_id: str = "",
        to_version: str = "",
        accept_losses: bool = False,
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """规则版本转换（§十六）：由插件声明的转换器执行，核心只搬运与记账。

        - 没有状态 = 没有可转的东西，直接说清楚；
        - 幂等键 = `converter|from>to`：同一转换重放返回原记录，不重复改状态；
        - 转换失败 / 有信息损失（未显式接受）→ 停在 needs_review，**原状态保持可恢复**；
        - 记录写在 `trpg_commit` 账本里（`status=converted`），六个字段在 result 里。
        """
        campaign_row = self._campaign_row(instance_id, timeline_id, campaign_id)
        self._require_live(campaign_row, what="转换规则版本")
        manifest = str(campaign_row.get("plugin_manifest") or "")
        state = self.store.trpg_get(
            "rule_state", instance_id=instance_id, timeline_id=timeline_id,
            campaign_id=campaign_id, ruleset_id=str(campaign_row["ruleset_id"]),
        )
        if state is None:
            raise CampaignRuntimeError("战役还没有规则状态，没有可转换的内容")
        identity = rules.manifest_identity(manifest)
        from_version = str(state.get("ruleset_version") or "") or str(campaign_row["ruleset_version"] or "")
        target_version = str(to_version or identity.get("ruleset_version") or "")
        if not target_version:
            raise CampaignRuntimeError("没有可用的目标规则版本（清单没声明，也没显式给出）")
        if from_version == target_version:
            # 版本已经一致：先看这是不是同一次转换的重放，不是才报错
            done = self._converted_record(instance_id, timeline_id, campaign_id, to_version=from_version)
            if done is not None:
                return {**_loads(done["result"], {}), "status": "duplicate",
                        "joint_commit_id": str(done["joint_commit_id"])}
            raise CampaignRuntimeError(f"规则状态已经是 {target_version}，不需要转换")
        converter = rules.pick_converter(
            rules.converters_of(manifest), converter_id=converter_id,
            from_version=from_version, to_version=target_version,
        )
        if converter is None:
            raise CampaignRuntimeError(
                f"插件没有声明可用的转换器（{from_version} → {target_version}）"
                "；人工确认接受请用 trpg.campaign.status(accept_ruleset_version=…)"
            )
        chosen = str(converter.get("converter_id"))
        key = f"{chosen}|{from_version}>{target_version}"
        existing = self._converted_record(instance_id, timeline_id, campaign_id, key=key)
        if existing is not None:
            return {**_loads(existing["result"], {}), "status": "duplicate",
                    "joint_commit_id": str(existing["joint_commit_id"])}

        state_revision = int(state["state_revision"])
        request = {
            "type": "convert_state",
            "protocol": "isekai.trpg.rules/1",
            "converter_id": chosen,
            "converter_version": str(converter.get("converter_version") or identity.get("ruleset_version") or ""),
            "campaign_id": campaign_id,
            "from_version": from_version,
            "to_version": target_version,
            "state_revision": state_revision,
            "opaque_state": _loads(state["opaque_state"], {}),
        }
        try:
            converted = await rules.convert(manifest, converter, request)
        except rules.RulePluginError as exc:
            # 转换失败保留原状态：不写新 revision，不降级、不清空
            return {
                "status": "needs_review",
                "converter_id": chosen,
                "old_ruleset_version": from_version,
                "new_ruleset_version": target_version,
                "old_state_revision": state_revision,
                "errors": [str(exc)],
            }
        losses = list(converted.get("losses") or [])
        if losses and not accept_losses:
            return {
                "status": "needs_review",
                "converter_id": chosen,
                "old_ruleset_version": from_version,
                "new_ruleset_version": target_version,
                "old_state_revision": state_revision,
                "losses": losses,
                "errors": ["转换有信息损失，需要显式接受（accept_losses=True）"],
            }

        now = float(now_real if now_real is not None else time.time())
        record = {
            "converter_id": chosen,
            "converter_version": str(request["converter_version"]),
            "old_ruleset_version": from_version,
            "old_state_revision": state_revision,
            "new_ruleset_version": target_version,
            "new_state_revision": state_revision + 1,
            "losses": losses,
        }
        joint_id = campaign_mod.new_id("jc")
        state_out = {
            **state,
            "ruleset_version": target_version,
            "state_revision": state_revision + 1,
            "opaque_state": _dumps(converted["opaque_state"]),
            "updated_world": self._world(instance_id, timeline_id),
            "updated_real": now,
        }
        note = (
            f"规则版本转换 {from_version} → {target_version}（{chosen}）；"
            f"损失 {len(losses)} 项" + ("；已人工接受" if losses else "")
        )
        campaign_out = {
            **campaign_row,
            "ruleset_version": target_version,
            "state_revision": int(campaign_row["state_revision"]) + 1,
            "note": note,
            "updated_world": state_out["updated_world"],
            "updated_real": now,
        }
        result = {
            "status": "converted",
            "joint_commit_id": joint_id,
            "campaign_id": campaign_id,
            **record,
            "notes": str(converted.get("notes") or ""),
        }
        ledger = {
            "joint_commit_id": joint_id,
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "campaign_id": campaign_id,
            "action_id": "",
            "idempotency_key": key,
            "campaign_revision": int(campaign_out["state_revision"]),
            "world_revision": int(state_out["updated_world"]),
            "state_revisions": _dumps({str(campaign_row["ruleset_id"]): int(state_out["state_revision"])}),
            "status": "converted",
            "result": _dumps(result),
            "created_world": int(state_out["updated_world"]),
            "created_real": now,
        }
        self.store.trpg_upserts(
            {"rule_state": [state_out], "campaign": [campaign_out], "commit": [ledger]}
        )
        return result

    # ------------------------------------------------------------ 规则状态与恢复

    def rule_state(self, instance_id: str, timeline_id: str, campaign_id: str) -> dict[str, Any]:
        """读规则状态附件（§5.4）：给内容的是受信调用方；核心不解析 opaque_state。"""
        campaign_row = self._campaign_row(instance_id, timeline_id, campaign_id)
        row = self.store.trpg_get(
            "rule_state", instance_id=instance_id, timeline_id=timeline_id,
            campaign_id=campaign_id, ruleset_id=str(campaign_row["ruleset_id"]),
        )
        return {
            "campaign_id": campaign_id,
            "ruleset_id": str(campaign_row["ruleset_id"]),
            "ruleset_version": str(campaign_row["ruleset_version"] or ""),
            # 写这份状态时插件声明的规则版本：与上面不一致 = 需要版本闸或人工确认（§十六）
            "state_ruleset_version": str(row["ruleset_version"] or "") if row else "",
            "state_revision": int(row["state_revision"]) if row else 0,
            "opaque_state": _loads(row["opaque_state"], {}) if row else {},
        }

    def recover(self, instance_id: str, timeline_id: str) -> dict[str, Any]:
        """重启恢复（§十三）：在途行动按真实状态归位，不重跑随机裁定、不替玩家选择。"""
        keys = {"instance_id": instance_id, "timeline_id": timeline_id}
        interrupted = recovered = 0
        for row in self.store.trpg_list("action", **keys):
            status = str(row["status"])
            if status in ("snapshotting", "resolving"):
                self.store.trpg_upserts({"action": [{**row, "status": "interrupted",
                                                     "failure_code": "interrupted"}]})
                interrupted += 1
            elif status == "committing":
                by_action = [
                    item for item in self.store.trpg_list("commit", **keys)
                    if str(item["action_id"]) == str(row["action_id"]) and str(item["status"]) == "committed"
                ]
                if by_action:
                    self.store.trpg_upserts({"action": [{**row, "status": "transitioned",
                                                         "joint_commit_id": str(by_action[-1]["joint_commit_id"])}]})
                else:
                    self.store.trpg_upserts({"action": [{**row, "status": "interrupted",
                                                         "failure_code": "interrupted"}]})
                recovered += 1
        return {"interrupted": interrupted, "committing_recovered": recovered}

    # ------------------------------------------------------------ 内部

    async def _rule_session(self, manifest_path: str) -> rules.RulePluginSession | None:
        """按清单决定走常驻会话还是一次性进程：清单 `resident: true` 才常驻。

        闲置超时就地收掉（一次比较，不起后台任务）；清单读不动就退回一次性路径，
        让它按原来的方式报错，不在这里改变错误语义。
        """
        try:
            manifest = rules.load_manifest(manifest_path)
        except rules.RulePluginError:
            return None
        if not manifest.get("resident"):
            return None
        key = str(Path(manifest_path))
        entry = [str(part) for part in manifest["entry"] if str(part)]
        session = self.rule_sessions.get(key)
        if session is not None and session.last_used_real:
            if (time.time() - session.last_used_real) > self.rule_session_idle_s:
                await session.close()
                session = None
        if session is None:
            session = rules.RulePluginSession(
                Path(manifest_path), entry, share=bool(manifest.get("share"))
            )
            self.rule_sessions[key] = session
        return session

    async def close_rule_sessions(self) -> int:
        """收掉所有常驻会话（核心退出 / 测试收尾）；返回关掉几个。"""
        closed = 0
        for session in list(self.rule_sessions.values()):
            if session.alive():
                closed += 1
            await session.close()
        self.rule_sessions.clear()
        return closed

    def _require_line(self, instance_id: str, timeline_id: str) -> None:
        if not instance_id or not timeline_id:
            raise CampaignRuntimeError("战役运行时调用必须带实例与时间线")
        if self.store.timeline_get(timeline_id) is None:
            raise CampaignRuntimeError(f"时间线不存在：{timeline_id}")

    def _world(self, instance_id: str, timeline_id: str) -> int:
        return int(self.runtime.world_moment(instance_id, timeline_id))

    def _campaign_row(self, instance_id: str, timeline_id: str, campaign_id: str) -> dict[str, Any]:
        row = self.store.trpg_get(
            "campaign", instance_id=instance_id, timeline_id=timeline_id, campaign_id=campaign_id
        )
        if row is None:
            raise CampaignRuntimeError(f"没有这个战役：{campaign_id}")
        return row

    def _action_row(self, instance_id: str, timeline_id: str, campaign_id: str, action_id: str) -> dict[str, Any]:
        row = self.store.trpg_get(
            "action", instance_id=instance_id, timeline_id=timeline_id,
            campaign_id=campaign_id, action_id=action_id,
        )
        if row is None:
            raise CampaignRuntimeError(f"没有这个行动：{action_id}")
        return row

    @staticmethod
    def _require_live(campaign_row: dict[str, Any], *, what: str, allow_waiting: bool = False) -> None:
        """战役状态闸（§11.1）：blocked/paused/archived 一律拒；waiting 只放行对应的输入。"""
        status = str(campaign_row["status"])
        if status == "waiting" and not allow_waiting:
            raise CampaignRuntimeError(f"战役在等待选择（waiting），先处理待选择再做{what}（§11.1）")
        if status in ("archived", "blocked", "paused"):
            raise CampaignRuntimeError(f"战役当前状态（{status}）不接受{what}")

    def _require_compatible(
        self, campaign_row: dict[str, Any], state_row: dict[str, Any] | None, manifest_path: str
    ) -> None:
        """规则版本闸（§十六 第 2 层）：插件解释不了这份 opaque_state → blocked，不静默降级或替换。

        比对基准是**插件声明的规则版本**（清单 `ruleset_version`，缺省退回插件版本），
        不是战役创建时写的字符串——真正危险的场景是插件升级后继续拿旧状态跑。
        出口只有两条：插件声明转换器（未实现），或在 `status(accept_ruleset_version=…)`
        里人工确认接受（会留下记录）。
        """
        if not state_row:
            return
        written = str(state_row.get("ruleset_version") or "")
        identity = rules.manifest_identity(manifest_path)
        declared = str(identity.get("ruleset_version") or "")
        if not written or not declared or written == declared:
            return
        reason = f"规则版本不兼容：规则状态由 {written} 写入，当前插件声明 {declared}（§十六）"
        self._block_campaign(campaign_row, reason)
        raise CampaignRuntimeError(reason)

    def _block_campaign(self, campaign_row: dict[str, Any], reason: str) -> None:
        row = {**campaign_row, "note": reason, "updated_real": time.time()}
        try:
            row["status"] = campaign_mod.transition("campaign", str(campaign_row["status"]), "blocked", what="战役")
        except campaign_mod.CampaignError:
            pass  # preparing/paused 等没有直达 blocked 的边：只记原因，靠 status() 人工恢复
        self.store.trpg_upserts({"campaign": [row]})

    def _set_action_status(self, row: dict[str, Any], status: str, *, failure_code: str = "") -> dict[str, Any]:
        """按状态机迁一步并落库，返回新行（连续推进时不用回读）。"""
        target = campaign_mod.transition("action", str(row["status"]), status, what="行动")
        out = {**row, "status": target, "failure_code": failure_code, "updated_real": time.time()}
        self.store.trpg_upserts({"action": [out]})
        return out

    def _fail(self, row: dict[str, Any] | None, status: str, *, code: str = "") -> dict[str, Any] | None:
        """把行动推到失败态：状态机不允许直达时走合法中间态（reviewing → committing → conflict/stale）。

        `row=None` = 这次提交背后没有行动（GM 直接变化），没有可标记的行。
        """
        if row is None:
            return None
        path = _FAIL_PATHS.get((str(row["status"]), status), (status,))
        for step in path:
            row = self._set_action_status(row, step, failure_code=code)
        return row

    def _apply_transition(self, scene: dict[str, Any], transition: dict[str, Any], world: int) -> dict[str, Any]:
        turn = _loads(scene.get("turn_state"), {})
        if transition.get("next_actor"):
            turn["next_actor"] = str(transition["next_actor"])
        if transition.get("next_phase"):
            turn["phase"] = str(transition["next_phase"])
        if transition.get("rule_time_delta") is not None:
            turn["rule_time"] = float(turn.get("rule_time") or 0.0) + float(transition["rule_time_delta"])
        status = str(transition.get("status") or "advanced")
        return {
            **scene,
            "turn_state": _dumps(turn),
            "status": "waiting_choice" if status == "waiting_choice" else str(scene.get("status") or "open"),
            "revision": int(scene["revision"]) + 1,
            "updated_world": int(world),
        }

    def _review(self, action_row: dict[str, Any] | None, errors: list[str]) -> dict[str, Any]:
        """待审：有行动就把行动挂起来等 GM；GM 直接变化没有行动行，只回待审结果。"""
        if action_row is None:
            return {"status": "needs_review", "action_id": "", "errors": errors}
        return self._needs_review(action_row, errors)

    def _needs_review(self, action_row: dict[str, Any], errors: list[str]) -> dict[str, Any]:
        self._fail(action_row, "awaiting_gm_review", code="needs_review")
        return {"status": "needs_review", "action_id": str(action_row["action_id"]), "errors": errors}

    def _mergeable_revisions(
        self,
        instance_id: str,
        timeline_id: str,
        campaign_id: str,
        ruleset_id: str,
        *,
        base: int,
        current: int,
        patch: dict[str, Any],
    ) -> list[int] | None:
        """分片合并判定（§二十一 残余第 4 条）：能并就返回被并进来的 revision 列表，不能并返回 None。

        判据只有一条：本次 patch 触及的路径（JSON 指针）与 `base..current` 之间**每一次**提交记下的
        `patch_paths` 完全不相交。中间任何一次提交没留下路径记录（更早版本的数据、或状态被直接改过）
        就不并——把不确定的情况留给冲突，比猜错安全。
        """
        if base < 0 or base >= current:
            return None
        incoming = {
            str(item.get("path") or "") for item in patch.get("operations") or [] if isinstance(item, dict)
        }
        if not incoming:
            return None
        seen: dict[int, set[str] | None] = {}
        for row in self.store.trpg_list(
            "commit", instance_id=instance_id, timeline_id=timeline_id, campaign_id=campaign_id
        ):
            revisions = _loads(row.get("state_revisions"), {}) or {}
            revision = int(revisions.get(ruleset_id) or 0)
            if not (base < revision <= current):
                continue
            recorded = (_loads(row.get("result"), {}) or {}).get("patch_paths")
            seen[revision] = {str(item) for item in recorded} if isinstance(recorded, list) else None
        if len(seen) != current - base:
            return None
        for paths in seen.values():
            if paths is None or paths & incoming:
                return None
        return sorted(seen)

    def _conflict(
        self, action_row: dict[str, Any] | None, *, current_revision: int, requested: int
    ) -> dict[str, Any]:
        """版本冲突：整批不落盘；有行动就标 conflict，GM 直接变化只回报冲突读数。"""
        self._fail(action_row, "conflict", code="conflict")
        return {
            "status": "conflict",
            "action_id": str((action_row or {}).get("action_id") or ""),
            "rule_state_revision": current_revision,
            "requested_base": requested,
        }

    def _converted_record(
        self, instance_id: str, timeline_id: str, campaign_id: str, *, key: str = "", to_version: str = ""
    ) -> dict[str, Any] | None:
        """账本里的转换记录（§十六 可审计 + 幂等）：按幂等键或目标版本找回原来那次。"""
        for row in self.store.trpg_list(
            "commit", instance_id=instance_id, timeline_id=timeline_id, campaign_id=campaign_id
        ):
            if str(row["status"]) != "converted":
                continue
            if key and str(row["idempotency_key"]) != key:
                continue
            result = _loads(row["result"], {})
            if to_version and str(result.get("new_ruleset_version") or "") != to_version:
                continue
            return row
        return None

    def _result_of(self, joint_commit_id: str) -> dict[str, Any]:
        row = self.store.trpg_get("commit", joint_commit_id=joint_commit_id)
        if row is None:
            return {"status": "committed", "joint_commit_id": joint_commit_id}
        return {**_loads(row["result"], {}), "status": "duplicate", "joint_commit_id": joint_commit_id}


def normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    """把插件响应收敛成四段结果（§五）；旧 `effects` 字段按 B0 兼容路径接受。

    这是「规则共用模块」的最小职责：结构、命名空间与确定性标记的检查在这里，
    目标 / 效果闭集 / 版本 / 因果的检查在 WorldRuntime 的提交边界（§六）。
    """
    errors: list[str] = []
    resolution = result.get("resolution")
    if not isinstance(resolution, dict) or not resolution:
        errors.append("插件响应缺少 resolution 对象")
        resolution = {} if not isinstance(resolution, dict) else resolution
    patch = result.get("rule_state_patch")
    if patch is not None:
        errors.extend(campaign_mod.validate_patch(patch))
    consequences = result.get("consequences")
    legacy = False
    if consequences is None:
        consequences = result.get("effects")
        legacy = True
    if consequences is None:
        consequences = []
    if not isinstance(consequences, list):
        errors.append("consequences / effects 必须是数组")
        consequences = []
    for index, item in enumerate(consequences):
        if not isinstance(item, dict):
            errors.append(f"consequences[{index}] 必须是对象")
            continue
        if str(item.get("certainty") or "confirmed") != "confirmed":
            errors.append(f"consequences[{index}]: 候选 / 未确认内容不能提交为世界事实")
    transition = result.get("scene_transition")
    if transition is not None and not isinstance(transition, dict):
        errors.append("scene_transition 必须是对象")
        transition = {}
    claims = result.get("claims") or []
    if not isinstance(claims, list):
        errors.append("claims 必须是数组")
        claims = []
    payload = {
        "resolution": resolution,
        "consequences": consequences,
        "rule_state_patch": patch,
        "scene_transition": transition or {},
        "claims": claims,
        "participants": result.get("participants") or [],
    }
    return {
        "errors": errors,
        "payload": payload,
        "resolution": resolution,
        "rule_state_patch": patch,
        "consequences": consequences,
        "scene_transition": transition or {},
        "participants": payload["participants"],
        "legacy_effects": legacy,
    }
