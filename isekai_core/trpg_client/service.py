"""TRPG 客户端编排层（TRPG_CLIENT_SPEC §3 / §4 / §14 / §15 / §16）。

这一层是**产品语义**：把既有运行时投影组装成四个产品面、驱动玩家行动的闭环、给状态文案与
结果表达、在玩家面前当最后一道显示闸门。它不新增旁路 API、不复制世界真值（§15 / §18.11），
四个面的材料全部来自 Campaign Runtime / WorldRuntime 的投影。

工作区（§4.1）由调用方持有并传进传出：客户端状态（草稿 / 选中项 / 投影版本 / 受众）是
可丢弃的，权威永远在运行时。
"""

from __future__ import annotations

from typing import Any

from ..log import get_logger
from . import draft as draft_mod
from . import expression, states, views

log = get_logger("trpg-client")

MODES = ("player", "gm")
DRAFT_TIMEOUT_S = 8.0

#: 工作区键（§4.1）：显式作用域 + 可丢弃的界面状态
WORKSPACE_KEYS = (
    "mode", "instance_id", "timeline_id", "campaign_id", "audience", "active_character_id",
    "selected_scene_id", "selected_action_id", "selected_choice_id", "draft_text", "draft_revision",
    "view_revision", "scene_revision",
)


class TrpgClientError(Exception):
    """客户端层拒绝：参数不合法或缺前置（调用方修正后重试）。"""


def default_workspace(**overrides: Any) -> dict[str, Any]:
    ws: dict[str, Any] = {
        "mode": "player", "instance_id": "", "timeline_id": "", "campaign_id": "",
        "audience": "public_party", "active_character_id": "", "selected_scene_id": "",
        "selected_action_id": "", "selected_choice_id": "", "draft_text": "", "draft_revision": 0,
        "view_revision": 0, "scene_revision": 0,
    }
    ws.update({key: value for key, value in overrides.items() if key in WORKSPACE_KEYS})
    return ws


def merge_workspace(workspace: Any, **changes: Any) -> dict[str, Any]:
    """合并工作区：只认白名单键，不把调用方塞进来的杂项当状态。"""
    base = workspace if isinstance(workspace, dict) else {}
    out = default_workspace(**{key: base.get(key, default_workspace()[key]) for key in WORKSPACE_KEYS})
    out.update({key: value for key, value in changes.items() if key in WORKSPACE_KEYS and value is not None})
    return out


class TrpgClient:
    """客户端服务：四个面 + 玩家闭环 + GM 辅助（都在既有运行时投影之上）。"""

    def __init__(self, *, store: Any, cfg: Any = None, runtime: Any = None, campaign: Any = None,
                 llm: Any = None) -> None:
        self.store = store
        self.cfg = cfg
        self.runtime = runtime
        self._campaign = campaign
        self.llm = llm

    # ------------------------------------------------------------------ 基础设施

    @property
    def campaign(self) -> Any:
        if self._campaign is None:
            from ..runtime.trpg import CampaignRuntime

            self._campaign = CampaignRuntime(store=self.store, runtime=self._world())
        return self._campaign

    def _world(self) -> Any:
        if self.runtime is not None and hasattr(self.runtime, "clock_row"):
            return self.runtime
        from ..runtime.service import from_config

        return from_config(self.cfg, self.store)

    def _campaign_row(self, ws: dict[str, Any]) -> dict[str, Any]:
        row = self.store.trpg_get(
            "campaign", instance_id=ws["instance_id"], timeline_id=ws["timeline_id"],
            campaign_id=ws["campaign_id"],
        )
        return row or {}

    def _plugin(self, ruleset_id: str, manifest_path: str = "") -> dict[str, Any]:
        """插件身份（id / version / name / modes）。

        版本闸的比对基准是清单声明的 `ruleset_version`（缺省退回插件版本），所以清单读不动时
        要如实返回空——不能让客户端自己发明一个版本来比对。
        """
        if ruleset_id:
            try:
                from .. import plugins as plugins_mod

                host = plugins_mod.HOST
                for item in (host.list_plugins() if host is not None else []) or []:
                    if isinstance(item, dict) and str(item.get("id") or "") == str(ruleset_id):
                        return item
            except Exception:  # noqa: BLE001 —— 插件宿主不可用不该让客户端读不了战役
                pass
        path = str(manifest_path or "").strip()
        if not path:
            return {}
        from ..runtime import rules as rules_mod

        data: dict[str, Any] = {}
        try:
            data = rules_mod.load_manifest(path)
        except Exception:  # noqa: BLE001 —— 坏清单按空身份处理，由战役运行时去报错
            data = {}
        identity = rules_mod.manifest_identity(path)
        return {
            "id": str(data.get("id") or identity.get("ruleset_id") or ""),
            "version": str(identity.get("ruleset_version") or data.get("version") or ""),
            "name": str(data.get("name") or ""),
            "modes": list(data.get("modes") or []),
        }

    def _plugin_for(self, ws: dict[str, Any], campaign: dict[str, Any] | None = None) -> dict[str, Any]:
        """当前战役的插件身份：规则状态版本要跟清单声明比对（§14.4）。"""
        row = self._campaign_row(ws)
        ruleset_id = str((campaign or {}).get("ruleset_id") or row.get("ruleset_id") or "")
        return self._plugin(ruleset_id, str(row.get("plugin_manifest") or ""))

    def _view(self, ws: dict[str, Any]) -> dict[str, Any]:
        return self.campaign.view(
            ws["instance_id"], ws["timeline_id"], ws["campaign_id"], audience=ws["audience"],
        )

    def _action_row(self, ws: dict[str, Any], action_id: str) -> dict[str, Any]:
        row = self.store.trpg_get(
            "action", instance_id=ws["instance_id"], timeline_id=ws["timeline_id"],
            campaign_id=ws["campaign_id"], action_id=action_id,
        )
        return row or {}

    def _faces(
        self, ws: dict[str, Any], *, view: dict[str, Any], plugin: dict[str, Any] | None = None,
        action: dict[str, Any] | None = None, preview: dict[str, Any] | None = None, stale: bool = False,
    ) -> dict[str, Any]:
        """四个产品面 + 显示闸门读数（§5 / §6 / §7.2 / §十二）。"""
        campaign = view.get("campaign") if isinstance(view.get("campaign"), dict) else {}
        choices = [item for item in (view.get("pending_choices") or []) if isinstance(item, dict)]
        actions = [item for item in (view.get("actions") or []) if isinstance(item, dict)]
        cognition = {}
        if ws["active_character_id"]:
            try:
                cognition = self._world().cognition_project(
                    ws["instance_id"], ws["timeline_id"], observer_id=ws["active_character_id"],
                )
            except Exception:  # noqa: BLE001 —— 角色认知取不到就明说没有，不冒充
                cognition = {}
        faces: dict[str, Any] = {
            "stale": bool(stale),
            "campaign": views.campaign_item(campaign, actions=actions, choices=choices),
            "scene": views.scene_face(view, audience=ws["audience"], campaign=campaign, cognition=cognition),
            "party": views.party_face(view, audience=ws["audience"],
                                      active_character_id=ws["active_character_id"], cognition=cognition),
            "next": states.next_step_copy(campaign_status=str(campaign.get("status") or ""),
                                          actions=actions, choices=choices),
            "world": self._world_view(ws),
        }
        if ws["mode"] == "gm":
            faces["gm"] = views.gm_face(view, plugin=plugin or {}, preview=preview)
        faces["gates"] = {
            "viewer": ws["audience"],
            "violations": expression.check_player_bundle(
                {key: value for key, value in faces.items() if key != "gm"}, viewer=ws["audience"],
            ),
        }
        return faces

    def _world_view(self, ws: dict[str, Any]) -> dict[str, Any]:
        """§4.2 world_view：当前世界时刻与版本（不含世界实情，实情走角色认知）。"""
        try:
            scope = self._world().scope_inspect(ws["instance_id"], ws["timeline_id"])
        except Exception:  # noqa: BLE001
            return {}
        return {"timeline_state": str(scope.get("timeline_state") or ""),
                "revision": int(scope.get("revision") or 0),
                "ruleset_version": str(scope.get("ruleset_version") or ""),
                "available_actions": list(scope.get("available_actions") or [])}

    @staticmethod
    def _scene_revision(view: dict[str, Any]) -> int:
        scene = view.get("scene") if isinstance(view.get("scene"), dict) else {}
        return int(scene.get("revision") or scene.get("scene_revision") or 0)

    def _bundle(self, ws: dict[str, Any], *, view: dict[str, Any] | None = None, stage: str,
                plugin: dict[str, Any] | None = None, action: dict[str, Any] | None = None,
                preview: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        view = view if isinstance(view, dict) else self._view(ws)
        current = self._scene_revision(view)
        # 调用方手里的场景版本与当前投影不一致 → 旧投影作废，重新读而不是接着用（§C0）
        stale = bool(int(ws.get("scene_revision") or 0) and current
                     and int(ws["scene_revision"]) != current)
        ws = merge_workspace(ws, view_revision=int((view.get("campaign") or {}).get("state_revision") or 0),
                             scene_revision=current)
        out: dict[str, Any] = {
            "ok": True, "stage": stage, "workspace": ws,
            "faces": self._faces(ws, view=view, plugin=plugin, action=action, preview=preview, stale=stale),
        }
        out.update(extra)
        return out

    def _payload(self, ws: dict[str, Any], action_id: str) -> dict[str, Any]:
        """行动的规范化载荷（公共模块输出）：受众账记在 `changes` 里，表达层按受众取用。"""
        row = self._action_row(ws, action_id)
        payload = row.get("resolution")
        if isinstance(payload, str) and payload.strip():
            try:
                import json

                payload = json.loads(payload)
            except ValueError:
                payload = {}
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------ C0：只读场景壳

    def enter(self, *, instance_id: str, timeline_id: str, campaign_id: str, mode: str = "player",
              audience: str = "", character_id: str = "", workspace: Any = None,
              recover: bool = True) -> dict[str, Any]:
        """进入 / 恢复战役（§14.1 顺序）：读战役 → 查版本 → 读场景 → 读待选择 → 读未结行动。"""
        if str(mode) not in MODES:
            raise TrpgClientError(f"未知客户端模式：{mode}（只有 player / gm）")
        if not (instance_id and timeline_id and campaign_id):
            raise TrpgClientError("进入战役要求显式的 instance_id / timeline_id / campaign_id")
        ws = merge_workspace(
            workspace, mode=str(mode), instance_id=instance_id, timeline_id=timeline_id,
            campaign_id=campaign_id, active_character_id=character_id or None,
            audience=audience or (workspace or {}).get("audience") or "public_party",
        )
        if ws["audience"] == "gm_only" and ws["mode"] != "gm":
            raise TrpgClientError("gm_only 受众只能在主持模式里看（§十二：不凭身份提升受众）")
        info = self.campaign.info(instance_id, timeline_id, campaign_id)
        plugin = self._plugin_for(ws, info)
        state = self.campaign.rule_state(instance_id, timeline_id, campaign_id)
        block = views.version_block(info, plugin, state_version=str(state.get("state_ruleset_version") or ""))
        recovery = {"interrupted": 0, "committing_recovered": 0}
        if recover and str(info.get("status") or "") in ("active", "waiting"):
            recovery = self.campaign.recover(instance_id, timeline_id)
        view = self._view(ws)
        return self._bundle(ws, view=view, stage="enter", plugin=plugin,
                            version_block=block, recovery=recovery)

    def refresh(self, *, workspace: Any, mode: str = "") -> dict[str, Any]:
        """重新读取（§C0：场景 revision 变化后旧投影作废，重新读而不是接着用）。"""
        ws = merge_workspace(workspace, mode=mode or None)
        if not (ws["instance_id"] and ws["timeline_id"] and ws["campaign_id"]):
            raise TrpgClientError("刷新要求工作区里已有战役作用域")
        info = self.campaign.info(ws["instance_id"], ws["timeline_id"], ws["campaign_id"])
        return self._bundle(ws, stage="refresh", plugin=self._plugin_for(ws, info))

    # ------------------------------------------------------------------ C1：行动闭环

    async def act(self, *, workspace: Any, text: str = "", fields: dict[str, Any] | None = None,
                  confirm: bool = False, action_id: str = "", abandon: bool = False,
                  idempotency_key: str = "") -> dict[str, Any]:
        """一次产品动作（§C1）：草稿 → 确认 → 声明 → 确认 → 裁定（玩家模式自动提交）。

        `action_id` 给了就是**修改**已有未确认行动（确认卡的修改语义，revision +1）；
        `abandon=True` 是放弃（不调插件、不写规则状态、不写世界后果）。
        """
        ws = merge_workspace(workspace)
        if not (ws["instance_id"] and ws["timeline_id"] and ws["campaign_id"]):
            raise TrpgClientError("行动要求工作区里已有战役作用域")
        view = self._view(ws)
        info = view.get("campaign") if isinstance(view.get("campaign"), dict) else {}
        campaign_status = str(info.get("status") or "")
        plugin = self._plugin_for(ws, info)
        # 只有调用方明确给了 action_id 才是「修改这个行动」；工作区里记着的上一个行动不算
        target_action = str(action_id or "")
        if abandon:
            if not target_action:
                raise TrpgClientError("放弃要指明 action_id")
            result = self.campaign.abandon(ws["instance_id"], ws["timeline_id"], ws["campaign_id"],
                                           target_action, reason=str(text or ""))
            return self._bundle(ws, stage="abandoned", plugin=plugin, action_id=target_action,
                                action_status=str(result.get("status") or ""),
                                note="放弃：没有调用规则插件，也没有写规则状态与世界")
        if not states.can_declare(campaign_status):
            # §11.1 / §C0：waiting 与只读状态不给新行动入口，且不假装是别的状态
            return self._bundle(ws, view=view, stage="blocked", plugin=plugin,
                                blocked=states.campaign_line(campaign_status),
                                blocked_status=campaign_status)
        scene = view.get("scene") if isinstance(view.get("scene"), dict) else {}
        card = await self._draft_card(ws, text=text, fields=fields, scene=scene)
        ws = merge_workspace(ws, draft_text=str(text or ""),
                             draft_revision=int(ws["draft_revision"]) + (1 if confirm else 0))
        if not confirm:
            return self._bundle(ws, view=view, stage="draft", plugin=plugin, draft=card)
        if not card["ready"]:
            return self._bundle(ws, view=view, stage="draft_gaps", plugin=plugin, draft=card,
                                blocked="确认卡还有缺口：先补上再声明（不猜）")
        actor = str(card["fields"]["actor"] or ws["active_character_id"] or "")
        if not actor:
            raise TrpgClientError("行动者不明确：给 actor 或在工作区里指定 active_character_id")
        if target_action:
            # 修改已有行动：同一个 action 只允许一个确认版本，改动涨 revision（§11.2）
            row = self._action_row(ws, target_action)
            changes = {key: value for key, value in card["fields"].items() if key != "actor" and value}
            if card["risks"]:
                changes["visible_risks"] = list(card["risks"])
            confirmed = self.campaign.confirm(
                ws["instance_id"], ws["timeline_id"], ws["campaign_id"], target_action,
                action_revision=int(row.get("action_revision") or 1), changes=changes or None,
            )
            ws = merge_workspace(ws, selected_action_id=target_action)
            return await self._resolve_and_maybe_submit(
                ws, view=view, plugin=plugin, action_id=target_action, card=card,
                revision=int(confirmed.get("action_revision") or 1),
                idempotency_key=idempotency_key,
            )
        declared = self.campaign.declare(
            ws["instance_id"], ws["timeline_id"], ws["campaign_id"],
            actor_id=actor, raw_text=str(text or ""), intent=str(card["fields"]["intent"]),
            target_refs=[str(card["fields"]["target"])] if card["fields"]["target"] else [],
            method=str(card["fields"]["method"]), expected_result=str(card["fields"]["expected_result"]),
            visible_risks=list(card["risks"]), requires_confirmation=False, auto_confirm=False,
        )
        new_id = str(declared.get("action_id") or "")
        confirmed = self.campaign.confirm(
            ws["instance_id"], ws["timeline_id"], ws["campaign_id"], new_id,
            action_revision=int(declared.get("action_revision") or 1),
        )
        ws = merge_workspace(ws, selected_action_id=new_id)
        return await self._resolve_and_maybe_submit(
            ws, view=view, plugin=plugin, action_id=new_id, card=card,
            revision=int(confirmed.get("action_revision") or declared.get("action_revision") or 1),
            idempotency_key=idempotency_key,
        )

    async def _resolve_and_maybe_submit(
        self, ws: dict[str, Any], *, view: dict[str, Any], plugin: dict[str, Any], action_id: str,
        card: dict[str, Any], revision: int, idempotency_key: str = "", explicit_retry: bool = False,
    ) -> dict[str, Any]:
        """裁定 + 玩家模式自动提交（§3.1 / §20.2）：GM 模式停在 reviewing 等主持操作。"""
        from ..runtime import campaign as campaign_mod

        try:
            resolved = await self.campaign.resolve(
                ws["instance_id"], ws["timeline_id"], ws["campaign_id"], action_id, plugin_manifest="",
            )
        except campaign_mod.CampaignError as exc:
            # 插件崩溃 / 超时 / 输出非法：行动已经被推成可展示的失败态，客户端如实表达，不猜结果（§C2）
            row = self._action_row(ws, action_id)
            status = str(row.get("status") or "")
            if status not in ("plugin_failed", "interrupted", "awaiting_gm_review", "rejected"):
                raise
            resolved = {"status": status, "action_id": action_id, "errors": [str(exc)]}
        resolved_status = str(resolved.get("status") or "")
        commit: dict[str, Any] = {}
        skipped = ""
        if resolved_status == "reviewing":
            if ws["mode"] == "player":
                key = idempotency_key or f"trpg-client:{action_id}:{revision}"
                commit = self.campaign.commit(
                    ws["instance_id"], ws["timeline_id"], ws["campaign_id"], action_id,
                    idempotency_key=key, audience=ws["audience"], source_mode="action",
                )
            else:
                skipped = "主持模式不自动提交：等 GM 明确提交、拒绝或转待审（§20.3）"
        elif resolved_status in ("needs_review", "rejected"):
            skipped = "裁定结果没有通过公共层校验：规则状态与世界都没落（§六）"
        elif resolved_status == "awaiting_choice":
            skipped = "等待玩家选择"
        elif explicit_retry:
            skipped = "重新裁定没有拿到可用结果"
        row = self._action_row(ws, action_id)
        payload = self._payload(ws, action_id) or resolved
        facts = expression.facts_from_payload(payload, viewer=ws["audience"])
        claims = expression.public_claims(list(payload.get("claims") or []), viewer=ws["audience"])
        fresh = self._view(ws)
        fresh_campaign = fresh.get("campaign") if isinstance(fresh.get("campaign"), dict) else {}
        ruleset_id = str(plugin.get("id") or fresh_campaign.get("ruleset_id") or "")
        ruleset_version = str(plugin.get("version") or fresh_campaign.get("ruleset_version") or "")
        bundle = self._bundle(
            ws, view=fresh, stage="resolved", plugin=plugin, action=card,
            action_id=action_id, revision=revision, resolved_status=resolved_status,
            committed=bool(commit.get("status") in ("committed", "duplicate")),
            commit_status=str(commit.get("status") or ""),
            skipped=skipped, errors=list(resolved.get("errors") or []) + list(commit.get("errors") or []),
            pending=list(resolved.get("pending") or []),
            result=expression.player_result(
                action=row if row else {"action_id": action_id, "status": resolved_status,
                                        "action_revision": revision},
                viewer=ws["audience"], commit=commit, facts=facts, claims=claims,
                scene=(fresh.get("scene") or {}), ruleset_id=ruleset_id, ruleset_version=ruleset_version,
                # 局面是否真的推进看场景转换声明，不看行动状态（提交后行动一律终态 transitioned）
                transitioned=bool(payload.get("scene_transition")),
                next_copy=states.next_step_copy(
                    campaign_status=str(fresh_campaign.get("status") or ""),
                    actions=[item for item in (fresh.get("actions") or []) if isinstance(item, dict)],
                    choices=[item for item in (fresh.get("pending_choices") or []) if isinstance(item, dict)],
                ),
            ),
            time=expression.time_lines(payload=payload, commit=commit or None),
            failures=expression.failure_lines(action=row, commit=commit,
                                              facts=facts if commit.get("status") in ("committed", "duplicate") else []),
        )
        if ws["mode"] == "gm":
            bundle.update(self._gm_layers(ws, plugin=plugin, action_id=action_id, commit=commit,
                                          payload=payload, facts=facts))
        choices = [item for item in (fresh.get("pending_choices") or []) if isinstance(item, dict)]
        if choices:
            # 待选择卡置首（§16.2 / §10.1）：只要当前投影里有开放选择，就先处理它
            bundle["choice"] = expression.choice_card(choices[0], viewer=ws["audience"])
        return bundle

    def _gm_layers(self, ws: dict[str, Any], *, plugin: dict[str, Any], action_id: str,
                   commit: dict[str, Any], payload: dict[str, Any], facts: list[dict[str, Any]]) -> dict[str, Any]:
        """第三 / 四层（§8.2 / §10.2）：只有主持模式拿得到——审计引用与原始规则材料。"""
        origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
        state_revisions = origin.get("expected_state_revisions")
        revisions = state_revisions.values() if isinstance(state_revisions, dict) else []
        return expression.gm_result(
            action=self._action_row(ws, action_id), resolution=payload.get("resolution"),
            commit=commit, facts=facts, plugin=plugin,
            snapshot_revision=int(origin.get("expected_revision") or 0),
            rule_state_base_revision=int(next(iter(revisions), 0) or 0),
            scene_transition=payload.get("scene_transition") or {},
        )

    async def _draft_card(self, ws: dict[str, Any], *, text: str, fields: dict[str, Any] | None,
                          scene: dict[str, Any]) -> dict[str, Any]:
        """确认卡（§6.1）：显式字段优先；模型只补空缺；不确定就报缺口。"""
        explicit = fields if isinstance(fields, dict) else {}
        needed = [key for key in draft_mod.KEY_FIELDS if not str(explicit.get(key) or "").strip()]
        model: dict[str, Any] = {}
        if needed and self.llm is not None and str(text or "").strip():
            known = [str(item.get("target") or item.get("target_ref") or "") for item in
                     (scene.get("public_facts") or []) if isinstance(item, dict)]
            prompt = draft_mod.draft_request(
                text=str(text), actor=str(ws["active_character_id"]),
                scene=scene, known_targets=[item for item in known if item],
            )
            try:
                # 不传小 max_tokens：推理型模型会把预算烧在推理上返回空文本（仓库既有坑）
                raw = await self.llm.chat(prompt, temperature=0.0, timeout=DRAFT_TIMEOUT_S)
            except Exception:  # noqa: BLE001 —— 模型不可用不该让玩家声明不了行动
                log.info("draft parse failed instance=%s campaign=%s", ws["instance_id"], ws["campaign_id"])
                raw = ""
            model = draft_mod.parse_draft(raw)
        return draft_mod.decide(text=text, explicit=explicit, model=model,
                               actor=str(ws["active_character_id"]), scene=scene)

    # ------------------------------------------------------------------ C2：待选择 / 重试

    def choose(self, *, workspace: Any, choice_id: str, option_id: str,
               idempotency_key: str = "") -> dict[str, Any]:
        """处理当前 open choice（§16.2）：不自动声明下一行动。"""
        ws = merge_workspace(workspace, selected_choice_id=choice_id)
        result = self.campaign.select_choice(
            ws["instance_id"], ws["timeline_id"], ws["campaign_id"], choice_id,
            selection=option_id, idempotency_key=idempotency_key or f"trpg-client:{choice_id}:{option_id}",
        )
        ws = merge_workspace(ws, selected_choice_id="")
        return self._bundle(ws, stage="choice", chosen=option_id,
                            duplicate=bool(result.get("duplicate")))

    async def retry(self, *, workspace: Any, kind: str, action_id: str = "",
                    idempotency_key: str = "") -> dict[str, Any]:
        """重试语义（§7.1 末 / §18.8 / §18.9）：恢复原提交与重新裁定是两件事，不许混。

        - `resume_submit`：带原幂等键，只查询和重放原提交结果；
        - `retry_resolve`：显式重试没有拿到结果的裁定（plugin_failed / interrupted）；
        - `reroll`：作为**新的行动声明**（新 action_id / 新 revision），不藏在普通重试里。
        """
        ws = merge_workspace(workspace)
        action_id = action_id or ws["selected_action_id"]
        if not action_id:
            raise TrpgClientError("重试要指明 action_id")
        row = self._action_row(ws, action_id)
        if kind == "resume_submit":
            revision = int(row.get("action_revision") or 1)
            key = idempotency_key or f"trpg-client:{action_id}:{revision}"
            result = self.campaign.commit(
                ws["instance_id"], ws["timeline_id"], ws["campaign_id"], action_id,
                idempotency_key=key, audience=ws["audience"], source_mode="action",
            )
            payload = self._payload(ws, action_id)
            facts = expression.facts_from_payload(payload, viewer=ws["audience"])
            bundle = self._bundle(ws, stage="retry", retry_kind=kind, commit=result,
                                  action_id=action_id,
                                  commit_status=str(result.get("status") or ""),
                                  committed=str(result.get("status") or "") in ("committed", "duplicate"),
                                  errors=list(result.get("errors") or []))
            if ws["mode"] == "gm":
                bundle.update(self._gm_layers(ws, plugin=self._plugin_for(ws, {}), action_id=action_id,
                                              commit=result, payload=payload, facts=facts))
            return bundle
        if kind == "retry_resolve":
            if str(row.get("status") or "") not in ("plugin_failed", "interrupted"):
                raise TrpgClientError(f"当前状态不能显式重试裁定：{row.get('status')}")
            view = self._view(ws)
            plugin = self._plugin_for(ws, view.get("campaign") or {})
            card = {"fields": {"actor": str(row.get("actor_id") or ""), "target": "",
                               "method": str(row.get("method") or ""),
                               "intent": str(row.get("intent") or ""), "expected_result": ""},
                    "risks": [], "gaps": [], "ready": True, "raw_text": "", "sources": {}}
            return await self._resolve_and_maybe_submit(
                ws, view=view, plugin=plugin, action_id=action_id, card=card,
                revision=int(row.get("action_revision") or 1),
                idempotency_key=idempotency_key, explicit_retry=True,
            )
        if kind == "reroll":
            if not row:
                raise TrpgClientError(f"没有这个行动：{action_id}")
            targets = row.get("target_refs")
            try:
                import json

                targets = json.loads(str(targets)) if isinstance(targets, str) else list(targets or [])
            except ValueError:
                targets = []
            fields = {"actor": str(row.get("actor_id") or ""),
                      "target": str(targets[0] if targets else ""),
                      "method": str(row.get("method") or ""), "intent": str(row.get("intent") or ""),
                      "expected_result": ""}
            # 新行动 = 新 action_id / 新 revision（§18.9：重新裁定不能藏在普通重试里）
            return await self.act(workspace={**ws, "selected_action_id": ""},
                                  text=str(row.get("intent") or ""), fields=fields, confirm=True)
        raise TrpgClientError(f"未知重试类型：{kind}（只有 resume_submit / retry_resolve / reroll）")

    # ------------------------------------------------------------------ C3：GM 辅助

    def gm_change(self, *, workspace: Any, form: dict[str, Any], preview_only: bool = False) -> dict[str, Any]:
        """GM 直接变化（§十三）：结构化表单 → 与玩家行动同一条联合提交边界。"""
        ws = merge_workspace(workspace)
        if ws["mode"] != "gm":
            raise TrpgClientError("GM 直接变化只在主持模式下发（不是玩家的快捷绕过）")
        form = form if isinstance(form, dict) else {}
        target = str(form.get("target_ref") or "").strip()
        kind = str(form.get("kind") or "").strip()
        audience = str(form.get("audience") or "public_party").strip()
        key = str(form.get("idempotency_key") or "").strip()
        reason = str(form.get("reason") or "").strip()
        operation = str(form.get("op") or "").strip()
        missing = [name for name, value in (("变化目标", target), ("变化类型", kind), ("操作", operation),
                                            ("受众", audience)) if not value]
        if missing:
            raise TrpgClientError("直接变化缺少：" + "、".join(missing))
        if not key:
            raise TrpgClientError("直接变化必须有幂等键（§十三）")
        if not views.audience_valid(audience):
            raise TrpgClientError(
                f"受众要落在闭集里（public_party / gm_only / character: / player: / npc:）：{audience}"
            )
        # 变化意图的形状：目标是 target_refs 数组（§3.6），不是单个 target
        consequence: dict[str, Any] = {"kind": kind, "target_refs": [target], "visibility": audience}
        if form.get("value") is not None:
            consequence["value"] = form["value"]
        consequence["operation"] = operation
        changes: dict[str, Any] = {"consequences": [consequence]}
        if str(form.get("frame") or "").strip():
            # 事件帧（§5.1）是叙述材料：进事件正文，不代替结构化效果
            changes["consequences"].append({"kind": "world_event", "operation": "create",
                                            "value": str(form["frame"]).strip(), "visibility": audience})
        summary = {"source": "gm_declaration", "target_ref": target, "kind": kind, "op": operation,
                   "audience": audience, "reason": reason, "idempotency_key": key}
        if preview_only:
            # §16.3 第 3 步：预览玩家视角；GM 私有变化依据不进预览（§C3）
            view = self._view(ws)
            preview = views.scene_face(view, audience="public_party", campaign=(view.get("campaign") or {}))
            return self._bundle(ws, view=view, stage="gm_change_preview", preview_form=summary,
                                preview=preview)
        result = self.campaign.gm_change(
            ws["instance_id"], ws["timeline_id"], ws["campaign_id"],
            changes=changes, idempotency_key=key, audience=ws["audience"],
        )
        status = str(result.get("status") or "")
        bundle = self._bundle(
            ws, stage="gm_change", gm_change=summary,
            gm_change_result={
                "status": status,
                "source": "gm_declaration",
                "visible_to": [audience],
                "world_time": expression.time_lines(
                    payload={"world_time_request": result.get("world_time_request") or {}}, commit=result,
                )["world_time"],
                "rescan_scene": status in ("committed", "duplicate"),
                "errors": list(result.get("errors") or []),
                "note": "故事意图只能形成主持候选，不走这个入口",
            },
        )
        bundle["committed"] = status in ("committed", "duplicate")
        bundle["commit_status"] = status
        bundle["errors"] = list(result.get("errors") or [])
        return bundle

    def review(self, *, workspace: Any, action_id: str, decision: str, reason: str = "",
               idempotency_key: str = "") -> dict[str, Any]:
        """待审工作区（§C3）：GM 明确批准（提交）/ 拒绝（放弃）/ 转待审（保持不动）。"""
        ws = merge_workspace(workspace)
        if ws["mode"] != "gm":
            raise TrpgClientError("待审工作区只在主持模式里操作")
        decision = str(decision or "")
        if decision not in ("approve", "reject", "hold"):
            raise TrpgClientError(f"未知裁定决定：{decision}（只有 approve / reject / hold）")
        row = self._action_row(ws, action_id)
        if not row:
            raise TrpgClientError(f"没有这个行动：{action_id}")
        status = str(row.get("status") or "")
        if decision == "hold":
            return self._bundle(ws, stage="review", decision=decision, action_status=status,
                                note="保持待审：不动规则状态与世界")
        if decision == "reject":
            result = self.campaign.reject(ws["instance_id"], ws["timeline_id"], ws["campaign_id"],
                                          action_id, reason=reason or "主持拒绝")
            return self._bundle(ws, stage="review", decision=decision,
                                action_status=str(result.get("status") or ""),
                                reason=str(result.get("reason") or ""),
                                note="已拒绝：裁定退回，规则状态与世界都不写")
        if status not in ("reviewing", "awaiting_gm_review", "conflict", "stale"):
            raise TrpgClientError(f"该行动当前状态不能批准提交：{status}")
        if not self._payload(ws, action_id):
            # 公共层判死的待审（needs_review）没有可提交的裁定：只能改行动 / 重跑裁定 / 拒绝
            raise TrpgClientError(
                f"该行动没有可提交的裁定（{status}）：待审结果要先补充条件或重跑裁定，"
                "批准只对「有裁定的待提交状态」成立"
            )
        revision = int(row.get("action_revision") or 1)
        result = self.campaign.commit(
            ws["instance_id"], ws["timeline_id"], ws["campaign_id"], action_id,
            # 幂等键与玩家自动提交同一条推导规则：客户端才可能不重复写世界地重放自己的提交（§18.8）
            idempotency_key=idempotency_key or f"trpg-client:{action_id}:{revision}",
            audience=ws["audience"], source_mode="action",
            expected_campaign_revision=int(ws["view_revision"]) or None,
        )
        return self._bundle(ws, stage="review", decision=decision,
                            commit_status=str(result.get("status") or ""),
                            committed=str(result.get("status") or "") in ("committed", "duplicate"),
                            errors=list(result.get("errors") or []))
