"""世界设定层 CLI（阶段 1）：用管理面驱动核心完成世界包 / 角色卡 / 实例的全部操作。

用法（默认自行拉起核心，退出时关闭）：
  python -m isekai_core.world_cli package template --name 灰潮纪 --out greytide.json
  python -m isekai_core.world_cli package validate --file greytide.json
  python -m isekai_core.world_cli package generate --brief "退潮后的盐碱世界" --out greytide.json
  python -m isekai_core.world_cli card template --package greytide.json --name 堤禾 --out tihe.json
  python -m isekai_core.world_cli card confirm --package greytide.json --file tihe.json
  python -m isekai_core.world_cli instance create --package greytide.json --card tihe.json
  python -m isekai_core.world_cli instance list
  python -m isekai_core.world_cli instance export --id in-xxxx --out backup.json
  python -m isekai_core.world_cli instance import --file backup.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .client import MgmtClient
from .cli import spawn_core
from .config import load_config
from .world import ops

OP_BY_COMMAND = {
    ("package", "template"): "world.package.template",
    ("package", "load"): "world.package.load",
    ("package", "save"): "world.package.save",
    ("package", "validate"): "world.package.validate",
    ("package", "generate"): "world.package.generate",
    ("package", "revise"): "world.package.revise",
    ("package", "fill"): "world.package.fill",
    ("card", "template"): "world.card.template",
    ("card", "load"): "world.card.load",
    ("card", "save"): "world.card.save",
    ("card", "validate"): "world.card.validate",
    ("card", "confirm"): "world.card.confirm",
    ("card", "generate"): "world.card.generate",
    ("instance", "list"): "instance.list",
    ("instance", "create"): "instance.create",
    ("instance", "info"): "instance.info",
    ("instance", "setting"): "instance.setting",
    ("instance", "rename"): "instance.rename",
    ("instance", "delete"): "instance.delete",
    ("instance", "export"): "instance.export",
    ("instance", "import"): "instance.import",
    ("backup", "create"): "backup.create",
    ("backup", "restore"): "backup.restore",
    ("backup", "list"): "backup.list",
    ("backup", "pack"): "backup.pack.create",
    ("backup", "packs"): "backup.pack.list",
    ("setup", "migrate-check"): "migrate.inspect",
    ("setup", "migrate"): "migrate.run",
    ("backup", "verify"): "backup.pack.verify",
    ("backup", "stage"): "backup.pack.stage",
    ("backup", "apply"): "backup.pack.apply",
    ("runtime", "proactive"): "runtime.proactive",
    ("runtime", "first-contact"): "runtime.first_contact",
    ("proactive", "list"): "proactive.list",
    ("runtime", "clock"): "runtime.clock",
    ("runtime", "activate"): "runtime.activate",
    ("runtime", "freeze"): "runtime.freeze",
    ("runtime", "rate"): "runtime.rate",
    ("runtime", "advance"): "runtime.advance",
    ("runtime", "card-add"): "runtime.card.add",
    ("runtime", "propose"): "runtime.propose",
    ("runtime", "extract"): "runtime.extract",
    ("runtime", "budget"): "runtime.budget",
    ("disclose", "confirm"): "disclose.confirm",
    ("disclose", "list"): "disclose.list",
    ("disclose", "suggest"): "disclose.suggest",
    ("plugin", "list"): "plugin.list",
    ("plugin", "install"): "plugin.install",
    ("narrative", "map"): "narrative.map",
    ("event", "draft"): "event.draft",
    ("event", "confirm"): "event.confirm",
    ("runtime", "timeline-rename"): "runtime.timeline.rename",
    ("runtime", "timeline-archive"): "runtime.timeline.archive",
    ("runtime", "timeline-delete"): "runtime.timeline.delete",
    ("runtime", "commit"): "runtime.commit",
    ("runtime", "commits"): "runtime.commits",
    ("runtime", "fork"): "runtime.fork",
    ("runtime", "rollback"): "runtime.rollback",
    ("runtime", "budget-set"): "runtime.budget.set",
    ("runtime", "consume-time"): "runtime.time.consume",
    # OC 故事层（OC_STORY_LAYER_SPEC §四 ~ §八）
    ("story", "enter"): "story.enter",
    ("story", "scene"): "story.scene",
    ("story", "home"): "story.home",
    ("story", "turn"): "story.turn",
    ("story", "classify"): "story.classify",
    ("story", "branch"): "story.branch",
    ("story", "restore"): "story.restore",
    # Writing Assistant（WRITING_ASSISTANT_SPEC §三 ~ §九）
    ("wa", "outline-save"): "wa.outline.save",
    ("wa", "outline-list"): "wa.outline.list",
    ("wa", "outline-get"): "wa.outline.get",
    ("wa", "bind"): "wa.bind",
    ("wa", "state"): "wa.state",
    ("wa", "evaluate"): "wa.evaluate",
    ("wa", "item-decide"): "wa.item.decide",
    ("wa", "observe"): "wa.observe",
    ("wa", "suggest"): "wa.suggest",
    ("wa", "propose"): "wa.candidate.propose",
    ("wa", "candidate-decide"): "wa.candidate.decide",
    ("wa", "commit"): "wa.candidate.commit",
    ("wa", "gm-declare"): "wa.gm.declare",
    ("wa", "gm-approve"): "wa.gm.approve",
    ("wa", "branch"): "wa.branch",
    # 对外接口（WORLD_RUNTIME_INTERFACE_SPEC §四~§六）
    ("runtime", "scope"): "runtime.scope.inspect",
    ("runtime", "snapshot"): "runtime.snapshot.read",
    ("runtime", "cognition"): "runtime.cognition.project",
    ("runtime", "subject"): "runtime.subject.state.read",
    ("runtime", "history"): "runtime.history.read",
    ("runtime", "gen-check"): "runtime.generation.check",
    ("runtime", "invalidate"): "runtime.task.invalidate",
    ("runtime", "change-preview"): "runtime.change.preview",
    ("runtime", "change-commit"): "runtime.change.commit",
    ("runtime", "time-advance"): "runtime.time.advance",
    # TRPG 战役运行时（TRPG_CAMPAIGN_RUNTIME_SPEC）
    ("trpg", "campaign-new"): "trpg.campaign.create",
    ("trpg", "campaign-list"): "trpg.campaign.list",
    ("trpg", "campaign-info"): "trpg.campaign.info",
    ("trpg", "campaign-status"): "trpg.campaign.status",
    ("trpg", "scene-open"): "trpg.scene.open",
    ("trpg", "scene"): "trpg.scene.view",
    ("trpg", "declare"): "trpg.action.declare",
    ("trpg", "confirm"): "trpg.action.confirm",
    ("trpg", "abandon"): "trpg.action.abandon",
    ("trpg", "reject"): "trpg.action.reject",
    ("trpg", "resolve"): "trpg.action.resolve",
    ("trpg", "commit"): "trpg.commit",
    ("trpg", "gm-change"): "trpg.gm.change",
    ("trpg", "migrate"): "trpg.campaign.migrate",
    # 规则插件登记簿（USER_INTERFACE_DESIGN §8.5）：与通道插件 `plugin` 组分开
    ("rules", "scan"): "rules.scan",
    ("rules", "list"): "rules.list",
    ("rules", "register"): "rules.register",
    ("rules", "enable"): "rules.enable",
    ("rules", "disable"): "rules.disable",
    ("rules", "remove"): "rules.remove",
    ("trpg", "choose"): "trpg.choice.select",
    ("trpg", "rule-state"): "trpg.rule_state.read",
    ("trpg", "recover"): "trpg.recover",
    # TRPG 客户端层（TRPG_CLIENT_SPEC）：四个产品面 / 行动闭环 / 主持辅助
    ("client", "enter"): "trpg.client.enter",
    ("client", "refresh"): "trpg.client.refresh",
    ("client", "act"): "trpg.client.act",
    ("client", "choice"): "trpg.client.choice",
    ("client", "retry"): "trpg.client.retry",
    ("client", "gm-change"): "trpg.client.gm_change",
    ("client", "review"): "trpg.client.review",
    ("client", "switch"): "trpg.client.switch",
    ("client", "branch"): "trpg.client.branch",
    ("client", "rollback"): "trpg.client.rollback",
    ("client", "express"): "trpg.client.express",
    ("runtime", "backfill"): "runtime.backfill",
    ("event", "render"): "event.render",
    ("event", "expand"): "event.expand",
    # 首次使用支撑（ONBOARDING_AND_RECOVERY §3 / §4）：本机检查 / 随发行样例 / 界面草稿 / 连接测试
    ("setup", "readiness"): "app.readiness",
    ("setup", "sample-list"): "world.sample.list",
    ("setup", "sample-install"): "world.sample.install",
    ("setup", "ai-test"): "settings.test",
    ("setup", "draft-save"): "ui.draft.save",
    ("setup", "draft-list"): "ui.draft.list",
    ("setup", "draft-load"): "ui.draft.load",
    ("setup", "draft-discard"): "ui.draft.discard",
}


def build_args(ns: argparse.Namespace) -> dict[str, Any]:
    """按操作契约拼参数：世界包 = path/package，角色卡 = card_path/card，实例 = package_path + card_paths。"""
    group, cmd = ns.group, ns.command

    def read(path: str | None) -> dict[str, Any]:
        if not path:
            raise SystemExit("缺少文件路径（--file / --package / --card）")
        return json.loads(Path(path).read_text(encoding="utf-8"))

    if group == "package":
        if cmd == "template":
            return {"name": ns.name or "未命名世界", "density": ns.density or "normal"}
        if cmd in ("load", "save", "validate", "revise", "fill"):
            target = ns.file or ns.package
            args = {"path": target, "package": read(target)}
            if cmd == "revise":
                args["instruction"] = ns.instruction or ""
            if cmd == "fill":
                args["section"] = ns.section or ""
            return args
        if cmd == "generate":
            return {"brief": ns.brief or "", "name": ns.name or "未命名世界"}
    if group == "card":
        args: dict[str, Any] = {}
        if ns.package:
            args["package_path"] = ns.package
        if cmd == "template":
            return {**args, "name": ns.name or "未命名角色"}
        if cmd == "load":
            return {"card_path": ns.file}
        if cmd == "save":
            return {"card_path": ns.file, "card": read(ns.file)}
        if cmd in ("validate", "confirm"):
            return {**args, "card_path": ns.file, **({"moment": int(ns.moment)} if ns.moment else {})}
        if cmd == "generate":
            return {**args, "brief": ns.brief or ""}
    if group == "runtime" and cmd == "first-contact":
        return {"instance_id": ns.id, "timeline_id": ns.timeline, "character_id": ns.card or "",
                "channel_id": "builtin", "thread_id": getattr(ns, "thread", "main") or "main"}
    if group == "proactive":
        return {"instance_id": ns.id, "timeline_id": ns.timeline}
    if group == "plugin":
        if cmd == "install":
            return {"archive": ns.archive or ns.file or "", "replace": bool(ns.replace)}
        return {}
    if group == "rules":
        # 规则插件登记簿（与通道插件 plugin 组分开）：只读扫描 / 本机登记 / 启停 / 移除
        target = ns.dir or ns.manifest or ""
        if cmd == "scan":
            return {"dir": target}
        if cmd == "register":
            return {"manifest_path": target}
        if cmd == "list":
            return {}
        return {"ruleset_id": ns.ruleset or "", "ruleset_version": ns.ruleset_version or ""}
    if group == "setup":
        args = {}
        if cmd in ("migrate-check", "migrate"):
            args["path"] = ns.file or ns.package
        if cmd == "migrate":
            args["note"] = ns.note or "从旧开发目录迁移"
        return args
    if group == "backup":
        args = {}
        if cmd == "create":
            args["note"] = ns.note or ""
        if cmd == "pack":
            args["kind"] = "manual"
            args["note"] = ns.note or ""
        if cmd in ("verify", "stage"):
            args["path"] = ns.file or ns.package
        if cmd == "apply":
            args["staged"] = ns.file or ns.package
            args["note"] = ns.note or ""
        if cmd == "restore":
            args["path"] = ns.file or ns.package
        return args
    if group == "disclose":
        args = {"instance_id": ns.id, "timeline_id": ns.timeline}
        if cmd == "confirm":
            args["from_character"] = ns.card or ""
            args["to_character"] = ns.at or ""
            args["refs"] = ns.ref or ""
            args["note"] = ns.note or ""
        if cmd == "list":
            args["to_character"] = ns.at or ""
        if cmd == "suggest":
            args["from_character"] = ns.card or ""
            args["to_character"] = ns.at or ""
            if ns.limit:
                args["limit"] = int(ns.limit)
        return args
    if group == "narrative":
        return {"instance_id": ns.id, "timeline_id": ns.timeline, "character_id": ns.card or ""}
    if group == "trpg":
        args: dict[str, Any] = {"instance_id": ns.id, "timeline_id": ns.timeline}
        if ns.campaign:
            args["campaign_id"] = ns.campaign
        if cmd == "campaign-new":
            args.update({
                "name": getattr(ns, "name", "") or "",
                "ruleset_id": ns.ruleset or "",
                "ruleset_version": ns.ruleset_version or "",
                "plugin_manifest": ns.plugin or "",
                "status": ns.status or "active",
                "participants": [ns.actor] if ns.actor else [],
            })
            if getattr(ns, "host_mode", None):
                args["host_mode"] = ns.host_mode
            if ns.kind:
                args["scene"] = {"kind": ns.kind, "location_refs": [ns.ref] if ns.ref else []}
        if cmd == "campaign-status":
            args.update({
                "status": ns.status or "",
                "reason": ns.reason or "",
                "accept_ruleset_version": ns.accept_ruleset_version or "",
            })
        if cmd == "scene-open":
            args["kind"] = ns.kind or "exploration"
            if getattr(ns, "name", "") or "":
                args["name"] = ns.name
            if getattr(ns, "brief", "") or "":
                args["brief"] = ns.brief
            if getattr(ns, "advance_mode", None):
                args["advance_mode"] = ns.advance_mode
        if cmd == "declare":
            args.update({
                "actor_id": ns.actor or "",
                "intent": ns.intent or "",
                "raw_text": ns.intent or "",
                "auto_confirm": bool(ns.auto_confirm),
                "require_confirmation": bool(getattr(ns, "require_confirmation", False)),
            })
        if cmd == "confirm":
            args.update({"action_id": ns.action or "", "action_revision": int(ns.revision or 0)})
        if cmd in ("abandon", "commit"):
            args["action_id"] = ns.action or ""
        if cmd == "commit":
            args["idempotency_key"] = ns.idempotency or ""
            args["source_mode"] = ns.source_mode or "action"
            if getattr(ns, "campaign_revision", None) is not None:
                args["campaign_revision"] = int(ns.campaign_revision)
        if cmd == "gm-change":
            args["changes"] = ns.changes or ""
            args["idempotency_key"] = ns.idempotency or ""
            if getattr(ns, "source", None):
                args["source"] = ns.source
        if cmd in ("commit", "gm-change", "scene"):
            if ns.audience:
                args["audience"] = ns.audience
        if cmd == "migrate":
            args["converter_id"] = ns.converter or ""
        if cmd == "resolve":
            args.update({
                "action_id": ns.action or "",
                "plugin_manifest": ns.plugin or "",
                "actor_id": ns.actor or "",
                "intent": ns.intent or "",
            })
        if cmd == "choose":
            args.update({"choice_id": ns.choice or "", "selection": ns.selection or ""})
        return args
    if group == "setup":
        if cmd == "sample-install":
            return {"sample": ns.name or ns.file or "", "request_id": ns.request or ""}
        if cmd == "ai-test":
            overrides = {
                "base_url": ns.base_url,
                "model": ns.model,
                "api_key": ns.api_key,
                "timeout_s": float(ns.timeout) if ns.timeout else None,
                "max_tokens": int(ns.max_tokens) if ns.max_tokens else None,
                "temperature": float(ns.temperature) if ns.temperature is not None else None,
            }
            llm = {key: value for key, value in overrides.items() if value is not None}
            return {"llm": llm} if llm else {}
        if cmd == "draft-save":
            payload = json.loads(ns.payload) if ns.payload else None
            return {
                "key": ns.key or "",
                "module": ns.module or "cli",
                "target": ns.target or "",
                "text": ns.text or "",
                "payload": payload,
            }
        if cmd in ("draft-load", "draft-discard"):
            return {"key": ns.key or ""}
        if cmd == "draft-list":
            return {"module": ns.module or ""}
        return {}
    if group == "client":
        args: dict[str, Any] = {
            "instance_id": ns.id, "timeline_id": ns.timeline, "campaign_id": ns.campaign or "",
            "mode": ns.mode or "player",
        }
        if ns.audience:
            args["audience"] = ns.audience
        if ns.card:
            args["character_id"] = ns.card
        if getattr(ns, "ws_file", None):
            path = Path(ns.ws_file)
            if path.exists():
                # 工作区由客户端持有并传进传出（§4.1）：CLI 把它落在文件里
                args["workspace"] = json.loads(path.read_text(encoding="utf-8"))
        if cmd == "act":
            args.update({"text": ns.text or ns.intent or "", "confirm": bool(ns.confirm),
                         "action_id": ns.action or "", "abandon": bool(getattr(ns, "abandon", False))})
            if getattr(ns, "action_fields", None):
                raw = str(ns.action_fields)
                args["fields"] = (json.loads(raw) if raw.lstrip().startswith("{")
                                  else json.loads(Path(raw).read_text(encoding="utf-8")))
        if cmd == "choice":
            args.update({"choice_id": ns.choice or "", "selection": ns.selection or ""})
        if cmd == "retry":
            args.update({"kind": ns.retry_kind or "", "action_id": ns.action or ""})
        if cmd in ("act", "choice", "retry", "gm-change", "review") and ns.idempotency:
            args["idempotency_key"] = ns.idempotency
        if cmd == "gm-change":
            form: dict[str, Any] = {
                "target_ref": ns.ref or ns.actor or "", "kind": ns.kind or "",
                "audience": ns.audience or "public_party", "reason": ns.reason or ns.note or "",
                "idempotency_key": ns.idempotency or "",
            }
            if getattr(ns, "value", None):
                raw = str(ns.value)
                form["value"] = (json.loads(raw) if raw.lstrip().startswith(("{", "[")) else raw)
            if getattr(ns, "frame", None):
                form["frame"] = ns.frame
            if getattr(ns, "op", None):
                form["op"] = ns.op
            args["form"] = form
            args["preview_only"] = bool(getattr(ns, "preview_only", False))
        if cmd == "review":
            args.update({"action_id": ns.action or "", "decision": ns.decision or ""})
        if cmd == "switch":
            args.update({"character_id": ns.card or "", "mode": ns.mode or "player"})
        if cmd == "branch":
            args.update({"commit_id": ns.commit or "", "name": ns.display_name or "",
                         "activate": bool(ns.activate), "confirm": bool(ns.confirm)})
        if cmd == "rollback":
            args.update({"commit_id": ns.commit or "", "confirm": bool(ns.confirm),
                         "saved": bool(ns.saved)})
        if cmd == "express":
            args.update({"text": ns.text or "", "action_id": ns.action or "",
                         "audience": ns.audience or "public_party", "as_frame": bool(ns.as_frame)})
        return args
    if group == "story":
        args: dict[str, Any] = {}
        if ns.id:
            args["instance_id"] = ns.id
        if ns.timeline:
            args["timeline_id"] = ns.timeline
        if ns.card:
            args["character_id"] = ns.card
        if cmd == "turn" and ns.seq is not None:
            args["seq"] = int(ns.seq)
        if cmd == "classify":
            args["text"] = ns.text or ""
        if cmd in ("branch", "restore"):
            args["commit_id"] = ns.commit or ns.file or ""
        if cmd == "branch" and ns.display_name:
            args["name"] = ns.display_name
        if cmd == "restore":
            args["confirm"] = bool(ns.confirm)
            args["saved"] = bool(ns.saved)
            args["acknowledge_unsaved"] = bool(ns.acknowledge_unsaved)
        return args
    if group == "wa":
        args: dict[str, Any] = {}
        if ns.id:
            args["instance_id"] = ns.id
        if ns.timeline:
            args["timeline_id"] = ns.timeline
        if ns.outline:
            args["outline_id"] = ns.outline
        if cmd == "outline-save":
            args["outline"] = read(ns.file)
        if cmd == "outline-get":
            args["outline_id"] = ns.outline or ns.file or ""
        if cmd == "bind":
            args["observers"] = [name.strip() for name in str(ns.observers or "").split(",") if name.strip()]
            args["chapter"] = ns.chapter or ""
        if cmd == "item-decide":
            args["item_id"] = ns.item or ns.ref or ""
            args["status"] = ns.status or ""
            args["reason"] = ns.reason or ns.note or ""
            args["evidence_refs"] = [name.strip() for name in str(ns.evidence or "").split(",") if name.strip()]
        if cmd == "observe":
            args["observer_id"] = ns.observer or ns.card or ""
            args["audience"] = ns.audience or "author"
        if cmd == "suggest":
            args["observer_id"] = ns.observer or ns.card or ""
            args["goal"] = ns.goal or ns.instruction or ""
            args["limit"] = int(ns.limit or 3)
        if cmd == "propose":
            args["candidate_id"] = ns.ref or ""
            args["kind"] = ns.kind or "world_change"
            args["title"] = ns.display_name or ""
            args["summary"] = ns.instruction or ns.note or ""
            args["audience"] = ns.audience or "author"
            args["unsolved"] = ns.unsolved or []
            args["text"] = ns.text or ""
            if ns.item:
                args["item_refs"] = [name.strip() for name in str(ns.item).split(",") if name.strip()]
            if ns.basis:
                args["basis"] = json.loads(ns.basis)
            if ns.changes:
                args["changes"] = json.loads(ns.changes) if str(ns.changes).lstrip().startswith(("[", "{")) else read(ns.changes)
        if cmd == "candidate-decide":
            args["candidate_id"] = ns.ref or ""
            args["status"] = ns.status or ""
            args["reason"] = ns.reason or ns.note or ""
            args["text"] = ns.text or ""
        if cmd == "commit":
            args["candidate_id"] = ns.ref or ""
            args["idempotency_key"] = ns.idempotency or ""
        if cmd == "gm-declare":
            args["candidate_id"] = ns.ref or ""
            args["campaign_id"] = ns.campaign or ""
            args["title"] = ns.display_name or ""
            args["audience"] = ns.audience or "gm"
            payload = ns.gm_changes or ns.changes or ""
            args["gm_changes"] = json.loads(payload) if str(payload).lstrip().startswith("{") else read(payload)
            if ns.basis:
                args["basis"] = json.loads(ns.basis)
        if cmd == "gm-approve":
            args["candidate_id"] = ns.ref or ""
            args["idempotency_key"] = ns.idempotency or ""
        if cmd == "branch":
            args["commit_id"] = ns.commit or ns.file or ""
            args["name"] = ns.display_name or ""
        return args
    if group == "event":
        args = {"instance_id": ns.id, "timeline_id": ns.timeline}
        if cmd == "draft":
            args["intent"] = ns.instruction or ""
            args["payload"] = ns.candidate or ""
            args["commit_id"] = ns.commit or ""
        if cmd == "confirm":
            args["draft_id"] = ns.ref or ns.file or ""
            args["name"] = ns.display_name or ""
        if cmd == "render":
            args["event_id"] = ns.file
        if cmd == "expand":
            args["claim_id"] = ns.file
            args["character_id"] = ns.card
            if ns.note:
                args["question"] = ns.note
        return args
    if group == "runtime":
        args: dict[str, Any] = {"instance_id": ns.id, "timeline_id": ns.timeline}
        if cmd == "rate":
            args["rate"] = int(ns.rate or 0)
        if cmd == "activate" and ns.rate:
            args["rate"] = int(ns.rate)
        if cmd == "advance":
            args["max_batches"] = int(ns.max_batches or 16)
        if cmd == "consume-time":
            args["seconds"] = int(ns.seconds or 0)
            args["cause"] = ns.cause or ""
            args["time_source"] = ns.time_source or "world_process"
        if cmd == "scope":
            pass
        if cmd == "snapshot":
            if ns.request:
                args["request"] = ns.request
            if ns.ttl_seconds is not None:
                args["ttl_seconds"] = int(ns.ttl_seconds)
        if cmd == "cognition":
            args["observer_id"] = ns.observer or ns.actor or ""
            if ns.query:
                args["query"] = ns.query
            if ns.at_revision is not None:
                args["at_revision"] = int(ns.at_revision)
        if cmd == "subject":
            args["subject_id"] = ns.subject or ""
            if ns.fields:
                args["fields"] = ns.fields
            if ns.audience:
                args["audience"] = ns.audience
        if cmd == "history":
            args["cursor"] = ns.cursor or ""
            args["limit"] = int(ns.limit or 50)
            if ns.filters:
                args["filters"] = ns.filters
        if cmd in ("change-preview", "change-commit"):
            args["changes"] = ns.changes or "[]"
            if ns.ruleset_patches:
                args["rule_state_patches"] = ns.ruleset_patches
            if ns.expected_revision is not None:
                args["expected_revision"] = int(ns.expected_revision)
        if cmd == "change-commit":
            args["idempotency_key"] = ns.idempotency or ""
            args["preview_id"] = ns.preview_id or ""
            args["source_module"] = ns.source_module or ""
        if cmd == "gen-check":
            args["snapshot_id"] = ns.snapshot_id or ""
            if ns.generation is not None:
                args["runtime_generation"] = int(ns.generation)
            if ns.source_refs:
                args["source_refs"] = ns.source_refs
        if cmd == "invalidate":
            if ns.generation is not None:
                args["runtime_generation"] = int(ns.generation)
            args["reason"] = ns.reason or ""
        if cmd == "time-advance":
            args["duration"] = int(ns.duration or ns.seconds or 0)
            args["reason"] = ns.reason or ns.cause or ""
            args["source"] = ns.time_source or "world_process"
        if cmd == "timeline-rename":
            args["name"] = ns.display_name or ""
            args["description"] = ns.note
        if cmd == "timeline-delete":
            args["confirm"] = bool(ns.confirm)
        if cmd in ("commit", "rollback"):
            args["note"] = ns.note or ""
        if cmd in ("fork", "rollback"):
            args["commit_id"] = ns.file or ns.commit or ""
        if cmd == "fork":
            args["name"] = ns.display_name or ""
            args["activate"] = bool(ns.activate)
        if cmd == "rollback":
            args["confirm"] = bool(ns.confirm)
        if cmd == "budget-set":
            for key in ("instance_tokens_per_day", "timeline_tokens_per_day", "task_tokens_per_day"):
                value = getattr(ns, key, None)
                if value is not None:
                    args[key] = int(value)
            if ns.task:
                args["task"] = ns.task
                args["pause"] = bool(ns.pause) if not ns.pause_resume else False
        if cmd == "card-add":
            args["card_path"] = ns.card
            args["joined_world"] = int(ns.at) if ns.at is not None else None
            args["note"] = ns.note or ""
            args["acquainted"] = bool(ns.acquainted)
        return args
    if group == "instance":
        if cmd == "list":
            return {}
        if cmd == "create":
            return {
                "package_path": ns.package,
                "card_paths": [item.strip() for item in str(ns.card or "").split(",") if item.strip()],
                **({"display_name": ns.display_name} if ns.display_name else {}),
            }
        if cmd in ("info", "setting", "delete"):
            return {"id": ns.id}
        if cmd == "rename":
            return {"id": ns.id, "name": ns.name or ""}
        if cmd == "export":
            return {"id": ns.id, "path": ns.out}
        if cmd == "import":
            return {"path": ns.file, **({"display_name": ns.display_name} if ns.display_name else {})}
    raise SystemExit(f"未实现的命令：{group} {cmd}")


def persist(ns: argparse.Namespace, result: dict[str, Any]) -> None:
    """候选落盘：校验通过写 --out；未通过写 <out>.candidate.json（不冒充最终版本）。"""
    target = getattr(ns, "out", None)
    # 生成类返回 candidate，骨架类返回 package / card
    payload = result.get("candidate") or result.get("package") or result.get("card")
    if not target or not isinstance(payload, dict) or not payload:
        return
    path = Path(target) if not result.get("errors") else Path(f"{target}.candidate.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def print_result(result: dict[str, Any], *, out: str | None = None, hide_candidate: bool = True) -> int:
    """打印读数。

    `hide_candidate` 只对生成类命令成立（那里的 `candidate` 是整份世界包 / 角色卡，写在 --out 里）；
    编剧层的 `candidate` 是**产品本身**（候选与它的生命周期），必须打出来——所以它关掉这个开关。
    """
    errors = result.get("errors")
    if errors:
        print("校验未通过：")
        for item in errors:
            print(f"  - {item}")
        if result.get("candidate"):
            print("（候选未落盘为最终版本；修正后可 save 或重新 generate）")
        return 1
    if out and (result.get("candidate") or result.get("package") or result.get("card")):
        print(f"已写入 {out}")
    usage = result.get("usage")
    if isinstance(usage, dict):
        state = "已暂停（未继续重试）" if usage.get("paused") else "完成"
        print(f"用量：调用 {usage.get('calls')}/{usage.get('limit')} 次，{state}")
    drop = {"candidate"} if hide_candidate else set()
    print(json.dumps({key: value for key, value in result.items() if key not in drop},
                     ensure_ascii=False, indent=2))
    return 0


async def run(ns: argparse.Namespace) -> int:
    cfg = load_config(ns.root)
    op = OP_BY_COMMAND[(ns.group, ns.command)]
    proc = None
    endpoint, mgmt_token = ns.endpoint, ns.mgmt
    if endpoint is None:
        proc, ready = spawn_core(ns.root)
        endpoint, mgmt_token = ready["endpoint"], ready["mgmt"]
    mgmt = MgmtClient(endpoint, mgmt_token or "")
    await mgmt.connect()
    try:
        # ponytail: 生成类操作同步等待模型返回；真需要长任务队列时再改作业式接口
        timeout = 600.0 if op in ops.ASYNC_OPS else 30.0
        result = await mgmt.call(op, timeout=timeout, **build_args(ns))
    except Exception as exc:  # noqa: BLE001 —— CLI 只负责把错误讲清楚
        print(f"操作失败：{exc}")
        return 1
    finally:
        await mgmt.close()
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)
    persist(ns, result)
    if ns.group == "client" and getattr(ns, "ws_file", None) and isinstance(result, dict):
        workspace = result.get("workspace")
        if isinstance(workspace, dict):
            Path(ns.ws_file).write_text(json.dumps(workspace, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"工作区已写入 {ns.ws_file}")
    return print_result(result, out=ns.out, hide_candidate=ns.group != "wa")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="isekai-world", description="世界设定层 CLI（阶段 1）")
    parser.add_argument("--root", default=None, help="数据根目录")
    parser.add_argument("--endpoint", default=None, help="连接已有核心（默认自行拉起）")
    parser.add_argument("--mgmt", default=None, help="已有核心的管理凭据")
    parser.add_argument(
        "group",
        choices=[
            "package", "card", "instance", "runtime", "event", "disclose", "backup", "proactive", "narrative",
            "trpg", "plugin", "story", "wa", "client", "setup", "rules",
        ],
    )
    parser.add_argument("command", help="/".join(f"{g}.{c}" for g, c in OP_BY_COMMAND))
    parser.add_argument("--name", default=None)
    parser.add_argument("--density", default=None)
    parser.add_argument("--file", default=None, help="输入文件")
    parser.add_argument("--out", default=None, help="输出文件（结果落盘）")
    parser.add_argument("--package", default=None, help="目标世界包文件")
    parser.add_argument("--brief", default=None, help="对话式生成的描述")
    parser.add_argument("--instruction", default=None, help="修订指令")
    parser.add_argument("--section", default=None, help="待补全段落")
    parser.add_argument("--card", default=None, help="角色卡文件（多个用逗号分隔）")
    parser.add_argument("--display-name", dest="display_name", default=None)
    parser.add_argument("--id", default=None, help="实例标识")
    parser.add_argument("--timeline", default=None, help="时间线标识（运行层命令）")
    parser.add_argument("--rate", default=None, help="倍率（世界秒 / 现实秒）")
    parser.add_argument("--max-batches", dest="max_batches", default=None, help="单次推进的最大批数")
    parser.add_argument("--moment", default=None, help="校验基准时刻（世界秒）")
    parser.add_argument("--at", default=None, help="补卡：锚定补入的世界时刻（缺省=该线已完成水位）")
    parser.add_argument("--note", default=None, help="补卡：备注")
    parser.add_argument("--acquainted", action="store_true", help="补卡：声明与联络者已相识")
    parser.add_argument("--instance-tokens", dest="instance_tokens_per_day", type=int, default=None,
                        help="预算：实例总预算（token 量级 / 现实日）")
    parser.add_argument("--timeline-tokens", dest="timeline_tokens_per_day", type=int, default=None,
                        help="预算：单条时间线预算")
    parser.add_argument("--task-tokens", dest="task_tokens_per_day", type=int, default=None,
                        help="预算：单任务预算")
    parser.add_argument("--task", default=None, help="预算：要暂停 / 恢复的任务名")
    parser.add_argument("--pause", action="store_true", help="预算：暂停该任务")
    parser.add_argument("--commit", default=None, help="版本：提交标识")
    parser.add_argument("--ref", default=None, help="草案标识等引用")
    parser.add_argument("--confirm", action="store_true", help="版本：确认破坏性操作（回滚）")
    parser.add_argument("--activate", action="store_true", help="版本：分叉后立即激活")
    parser.add_argument("--resume", dest="pause_resume", action="store_true", help="预算：恢复该任务")
    parser.add_argument("--candidate", default=None, help="把文件内容当作候选对象提交")
    parser.add_argument("--limit", default=None, help="条数上限（披露候选等）")
    parser.add_argument("--campaign", default=None, help="TRPG：战役标识")
    parser.add_argument("--ruleset", default=None, help="TRPG：规则系统标识")
    parser.add_argument("--ruleset-version", dest="ruleset_version", default=None, help="TRPG：规则版本")
    parser.add_argument("--plugin", default=None, help="TRPG：规则插件清单路径")
    parser.add_argument("--archive", default=None, help="插件分发包路径（zip；plugin install 用）")
    parser.add_argument("--replace", action="store_true", help="插件事务：同名目录已存在时显式覆盖")
    parser.add_argument("--actor", default=None, help="TRPG：行动者（玩家角色标识）")
    parser.add_argument("--action", default=None, help="TRPG：行动标识")
    parser.add_argument("--revision", type=int, default=None, help="TRPG：行动 / 场景版本")
    parser.add_argument("--choice", default=None, help="TRPG：待选择标识")
    parser.add_argument("--selection", default=None, help="TRPG：选择结果")
    parser.add_argument("--idempotency", default=None, help="TRPG：联合提交幂等键")
    parser.add_argument("--intent", default=None, help="TRPG：行动意图 / 原始声明")
    parser.add_argument("--kind", default=None, help="TRPG：场景类型")
    parser.add_argument("--status", default=None, help="TRPG：战役状态")
    parser.add_argument("--reason", default=None, help="TRPG：状态变更原因")
    parser.add_argument("--auto-confirm", dest="auto_confirm", action="store_true", help="TRPG：低风险行动直接确认")
    parser.add_argument("--require-confirmation", dest="require_confirmation", action="store_true",
                        help="TRPG：这是关键行动——任何主持模式下都要玩家确认（§4.2 / §八）")
    parser.add_argument("--host-mode", dest="host_mode", default=None,
                        help="TRPG：主持责任模式 assisted（缺省）/ autonomous / cohost")
    parser.add_argument("--advance-mode", dest="advance_mode", default=None,
                        help="TRPG：场景推进节拍 instant / continuous（缺省）/ opposed / world")
    parser.add_argument("--campaign-revision", dest="campaign_revision", type=int, default=None,
                        help="TRPG：提交闭包里基于的战役版本（不一致 → conflict）")
    parser.add_argument("--seconds", type=int, default=None, help="TRPG / 时钟：时间消耗秒数")
    parser.add_argument("--cause", default=None, help="TRPG / 时钟：时间消耗原因（必填）")
    parser.add_argument("--time-source", dest="time_source", default=None,
                        help="时钟：时间消耗来源 world_process / player_action / gm_declaration")
    parser.add_argument("--accept-ruleset-version", dest="accept_ruleset_version", default=None,
                        help="TRPG：人工确认规则状态改用新规则版本解释（§十六）")
    parser.add_argument("--audience", default=None,
                        help="TRPG：受众（public_party / gm_only / player:… / character:… / npc:…）")
    parser.add_argument("--source", default=None,
                        help="TRPG：GM 直接变化的来源（gm_declaration / world_process / npc_script）")
    parser.add_argument("--converter", default=None, help="TRPG：状态转换器标识（规则版本迁移）")
    parser.add_argument("--dir", default=None, help="规则插件：清单所在目录（rules scan / register）")
    parser.add_argument("--manifest", default=None, help="规则插件：清单文件路径（rules scan / register）")
    parser.add_argument("--accept-losses", dest="accept_losses", action="store_true",
                        help="TRPG：显式接受有信息损失的规则状态转换")
    parser.add_argument("--changes", default=None, help="TRPG：GM 直接变化的 changes（内联 JSON 或文件路径）")
    parser.add_argument("--mode", default=None, help="客户端：player（缺省）/ gm")
    parser.add_argument("--ws-file", dest="ws_file", default=None,
                        help="客户端：工作区文件（§4.1，传进传出，可丢弃）")
    parser.add_argument("--action-fields", dest="action_fields", default=None,
                        help="客户端：行动确认卡字段（内联 JSON 或文件）")
    parser.add_argument("--decision", default=None, help="客户端：待审决定 approve / reject / hold")
    parser.add_argument("--retry-kind", dest="retry_kind", default=None,
                        help="客户端：重试类型 resume_submit / retry_resolve / reroll")
    parser.add_argument("--abandon", action="store_true", help="客户端：放弃指定行动（不调插件、不写世界）")
    parser.add_argument("--as-frame", dest="as_frame", action="store_true",
                        help="客户端：人工表达写成事件帧（进事件正文，只许公开材料）")
    parser.add_argument("--preview-only", dest="preview_only", action="store_true",
                        help="客户端：GM 直接变化只预览玩家视角，不提交")
    parser.add_argument("--value", default=None, help="客户端：GM 直接变化的值（内联 JSON 或文本）")
    parser.add_argument("--frame", default=None, help="客户端：GM 直接变化的事件帧（叙述材料）")
    parser.add_argument("--op", default=None, help="客户端：GM 直接变化的操作（set / change / reveal / advance）")
    parser.add_argument("--observer", default=None, help="接口：观察者角色标识")
    parser.add_argument("--subject", default=None, help="接口：主体标识")
    parser.add_argument("--query", default=None, help="接口：认知投影 query（内联 JSON）")
    parser.add_argument("--fields", default=None, help="接口：字段选择（内联 JSON 数组）")
    parser.add_argument("--cursor", default=None, help="接口：历史游标（世界秒:序号）")
    parser.add_argument("--filters", default=None, help="接口：历史过滤（内联 JSON）")
    parser.add_argument("--request", default=None, help="接口：快照请求（内联 JSON）")
    parser.add_argument("--ttl-seconds", dest="ttl_seconds", type=int, default=None, help="接口：快照有效期（秒）")
    parser.add_argument("--at-revision", dest="at_revision", type=int, default=None, help="接口：读取基准修订")
    parser.add_argument("--expected-revision", dest="expected_revision", type=int, default=None,
                        help="接口：写入方基于的版本")
    parser.add_argument("--preview-id", dest="preview_id", default=None, help="接口：预览标识")
    parser.add_argument("--source-module", dest="source_module", default=None, help="接口：调用方模块标识")
    parser.add_argument("--duration", type=int, default=None, help="接口：时间推进秒数（time-advance）")
    parser.add_argument("--source-refs", dest="source_refs", default=None, help="接口：来源引用（内联 JSON 数组）")
    parser.add_argument("--generation", type=int, default=None, help="接口：运行世代")
    parser.add_argument("--snapshot-id", dest="snapshot_id", default=None, help="接口：快照标识")
    parser.add_argument("--ruleset-patches", dest="ruleset_patches", default=None,
                        help="接口：规则状态 patch（内联 JSON 数组）")
    parser.add_argument("--source-mode", dest="source_mode", default=None,
                        help="TRPG：后果来源 action（角色行动）/ gm_declaration（GM 直接裁定）")
    parser.add_argument("--seq", type=int, default=None, help="故事层：要看哪一轮（缺省=最近一轮）")
    parser.add_argument("--text", default=None, help="故事层：要分类的输入原文")
    parser.add_argument("--saved", action="store_true", help="故事层：已先保存当前进展（分支或导出）")
    parser.add_argument("--acknowledge-unsaved", dest="acknowledge_unsaved", action="store_true",
                        help="故事层：显式接受「不先保存就恢复」")
    parser.add_argument("--outline", default=None, help="编剧层：大纲标识")
    parser.add_argument("--observers", default=None, help="编剧层：观察视角（逗号分隔的角色标识）")
    parser.add_argument("--chapter", default=None, help="编剧层：章节名")
    parser.add_argument("--item", default=None, help="编剧层：大纲条目标识（多个用逗号分隔）")
    parser.add_argument("--evidence", default=None, help="编剧层：依据引用（事件 / 说法标识，逗号分隔）")
    parser.add_argument("--basis", default=None, help="编剧层：候选依据（内联 JSON）")
    parser.add_argument("--unsolved", default=None, action="append", help="编剧层：未解决问题（可重复）")
    parser.add_argument("--gm-changes", dest="gm_changes", default=None, help="编剧层：GM 直接变化载荷（内联 JSON 或文件）")
    parser.add_argument("--goal", default=None, help="编剧层：章节目标（情节提议用）")
    # 首次使用支撑（setup 组）：AI 连接测试用正在编辑的值，草稿是界面输入的本机持久化
    parser.add_argument("--base-url", dest="base_url", default=None, help="setup：测试用的服务地址")
    parser.add_argument("--model", default=None, help="setup：测试用的模型名")
    parser.add_argument("--api-key", dest="api_key", default=None, help="setup：测试用的访问密钥（不写入配置）")
    parser.add_argument("--timeout", default=None, help="setup：测试用的等待时间（秒）")
    parser.add_argument("--max-tokens", dest="max_tokens", default=None, help="setup：测试用的单次输出长度")
    parser.add_argument("--temperature", default=None, help="setup：测试用的生成随机程度")
    parser.add_argument("--key", default=None, help="setup：草稿键（如 contact:builtin）")
    parser.add_argument("--module", default=None, help="setup：草稿所属模块（contact / writing / world）")
    parser.add_argument("--target", default=None, help="setup：草稿目标对象")
    parser.add_argument("--payload", default=None, help="setup：草稿附带数据（内联 JSON）")
    ns = parser.parse_args(argv)
    if (ns.group, ns.command) not in OP_BY_COMMAND:
        parser.error(f"未知命令 {ns.group} {ns.command}")
    return asyncio.run(run(ns))


if __name__ == "__main__":
    sys.exit(main())
