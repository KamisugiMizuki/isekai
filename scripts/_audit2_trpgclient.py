"""TRPG 客户端（TRPG_CLIENT_SPEC）行为级审计探针。

问题：客户端层是「真产品语义」还是「文档换了个名字的转发」？

判法（真 WebSocket + 真 SQLite + 真插件子进程，只把 LLM 换成 FakeLLM）：

- A 段：规范 ⇄ 常数（§7.1 用户可见状态表 / §十二 受众闭集 / §6.1 确认卡字段）；
- B 段：C0 只读场景壳（战役选择项、场景六类、受众隔离、waiting 闸、只读态、投影作废）；
- C 段：C1 行动闭环（草稿不猜、缺口不放行、声明·确认·修改·放弃、自动提交、幂等、重新裁定）；
- D 段：C2 失败 / 选择 / 恢复（插件崩溃、待审、待选择、双轨时间、重启恢复、版本阻断）；
- E 段：C3 GM 与视角（gm_declaration、玩家视角预览、待审工作区、显示闸门负例）；
- F 段：§十八 产品不变量抽查（引用前几段的真实读数，不另起一套）。

只读项目代码；数据写临时目录；不需要联网（FakeLLM）。
用法：`.venv/Scripts/python.exe scripts/_audit2_trpgclient.py [--only 关键字] [--json]`
段间依赖：**F 段（§十八 不变量）读的是前面各段的读数**（如 F03 读 C07、F12 读 E03）——
分段跑时它必然报「缺该条读数」。要 F 段的真读数就整跑（不要 `--only`）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _audit2_rulecommon as common  # noqa: E402

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.client import MgmtClient  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402
from isekai_core.trpg_client import draft as draft_mod  # noqa: E402
from isekai_core.trpg_client import expression, states, views  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS = common.RESULTS

#: 客户端探针用的插件：一个成功、一个只有 GM 私有后果、一个待审、一个崩溃、一个待选择、
#: 一个明确无变化、一个非法时间请求。
PLUGIN_SOURCE = '''
import json, sys

request = json.loads(sys.stdin.readline())
mode = str(request.get("intent") or "ok")
state = request.get("rule_state") or {}
base = int(state.get("state_revision") or 0)
patch = {"ruleset_id": state.get("ruleset_id"), "base_state_revision": base,
         "operations": [{"path": "/actors/pc-1/hp", "op": "add" if base == 0 else "increase", "value": 1}]}

consequences = [{"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "封条已揭",
                 "expiry": "with_cause", "certainty": "confirmed"}]
claims = [{"id": "cl-1", "text": "岗位的封条是新的", "source_id": "src-1", "audience": "public_party"}]
transition = {"status": "advanced"}

if mode == "crash":
    sys.exit(3)
if mode == "private":
    # 只有 GM 看得到的后果：玩家面必须看不到它，GM 审计面必须看得到
    consequences = [{"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "密探已布防",
                     "expiry": "with_cause", "visibility": "gm_only", "certainty": "confirmed"}]
    claims = []
elif mode == "candidate":
    consequences = [{"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "戒备",
                     "expiry": "with_cause", "certainty": "candidate"}]
    claims = []
elif mode == "choice":
    consequences = [
        {"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "封锁",
         "expiry": "with_cause", "certainty": "confirmed"},
        {"kind": "player_choice", "operation": "create", "choice_id": "ch-1",
         "value": ["追上去", "先撤退"], "audience": "public_party", "certainty": "confirmed"},
        {"kind": "time_advance", "operation": "advance",
         "value": {"seconds": 600, "cause": "追查去向"}, "certainty": "confirmed"},
    ]
    claims = []
elif mode == "time_bad":
    consequences = [{"kind": "time_advance", "operation": "advance",
                     "value": {"seconds": -600, "cause": "倒流"}, "certainty": "confirmed"}]
    claims = []
    transition = {}
elif mode == "nothing":
    consequences = []
    claims = []
    transition = {}

answer = {"resolution": {"system": "client-probe", "outcome": mode, "degree": "regular",
                         "rolls": [{"d10": 7}], "dc": 12},
          "rule_state_patch": patch, "consequences": consequences, "claims": claims,
          "scene_transition": transition, "participants": ["pc-1"]}
print(json.dumps(answer, ensure_ascii=False), flush=True)
'''

#: FakeLLM 的草稿解析回复：字段整理一次到位（证明模型路径能填字段）
DRAFT_REPLY = json.dumps({
    "target": "off-1", "method": "徒手", "intent": "揭开封条", "expected_result": "", "risks": ["被抓住"],
}, ensure_ascii=False)


def spec_text(name: str) -> str:
    hits = [path for path in (ROOT / "docs").rglob(name)]
    return hits[0].read_text(encoding="utf-8") if hits else ""


CLIENT_SPEC = spec_text("TRPG_CLIENT_SPEC.md")


@asynccontextmanager
async def core() -> AsyncIterator[Any]:
    """临时根目录里跑真核心（真 WS + 真 SQLite），只换 LLM。"""
    with tempfile.TemporaryDirectory(prefix="trpgclient-audit-") as tmp:
        folder = Path(tmp) / "config"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "config.yaml").write_text(
            "runtime:\n  sleep_wait_min_s: 0.05\n  sleep_wait_max_s: 0.05\n  max_active_timelines: 24\n", encoding="utf-8"
        )
        cfg = load_config(tmp)
        runtime = await build_runtime(cfg, llm=FakeLLM([DRAFT_REPLY]))
        endpoint = await runtime.server.start()
        mgmt = MgmtClient(endpoint, runtime.server.mgmt_token)
        await mgmt.connect()
        try:
            yield SimpleNamespace(cfg=cfg, store=runtime.store, world=runtime.world,
                                  service=runtime.service, mgmt=mgmt, root=Path(tmp))
        finally:
            await mgmt.close()
            await runtime.service.shutdown()
            await runtime.server.close()
            runtime.store.close()


async def call(h: Any, op: str, **args: Any) -> dict[str, Any]:
    result = await h.mgmt.call(op, timeout=120.0, **_inject(op, args))
    if op.startswith("trpg.client."):
        _remember(args, result)
    return result


async def call_err(h: Any, op: str, **args: Any) -> str:
    try:
        await h.mgmt.call(op, timeout=120.0, **_inject(op, args))
    except Exception as exc:  # noqa: BLE001 —— 探针就是要看拒绝原因
        return str(exc)
    return ""


#: 客户端工作区（§4.1）：真客户端自己拿着并传进传出，探针按 (战役, 模式) 缓存
STATE: dict[str, dict[str, Any]] = {}


def _ws_key(args: dict[str, Any]) -> str:
    return f"{args.get('campaign_id') or ''}:{args.get('mode') or 'player'}"


def _inject(op: str, args: dict[str, Any]) -> dict[str, Any]:
    if not op.startswith("trpg.client.") or args.get("workspace") or op == "trpg.client.enter":
        return args
    cached = STATE.get(_ws_key(args))
    return {**args, "workspace": cached} if cached else args


def _remember(args: dict[str, Any], result: dict[str, Any]) -> None:
    workspace = result.get("workspace") if isinstance(result, dict) else None
    if isinstance(workspace, dict):
        STATE[_ws_key({**args, **workspace})] = workspace


async def new_campaign(h: Any, manifest: Path, *, ruleset_id: str = "client-rules",
                       version: str = "1.0") -> tuple[str, str, str, str]:
    package = example_package("灰潮纪", moment=DAY * 1500 + 30000)
    card = example_card(package, name="堤禾")
    info = create_instance(h.store, package, [card])
    instance_id = str(info["id"])
    timeline_id = str(h.store.timeline_list(instance_id)[0]["id"])
    h.world.ensure_instance(instance_id, now_real=time.time())
    h.world.activate(instance_id, timeline_id, now_real=time.time())
    actor = str(card["meta"]["card_id"])
    common.ACTORS[instance_id] = actor
    created = await call(
        h, "trpg.campaign.create", instance_id=instance_id, timeline_id=timeline_id,
        ruleset_id=ruleset_id, ruleset_version=version, plugin_manifest=str(manifest),
        host_mode="autonomous", participants=[actor],
        scene={"kind": "conflict", "location_refs": ["rl-1"], "public_facts": [{"text": "岗位的门半掩着"}],
               "active_risks": [{"text": "夜里有人巡岗"}], "available_actions": ["查岗", "问人"]},
    )
    return instance_id, timeline_id, str(created["campaign_id"]), actor


def events(h: Any, instance_id: str, timeline_id: str) -> list[dict[str, Any]]:
    return [row for row in h.store.event_window(instance_id, timeline_id, until=10**15, limit=500)
            if str(row["source"]).startswith("trpg") or row["source"] == "gm_declaration"]


def rule_state(h: Any, instance_id: str, timeline_id: str, campaign_id: str, ruleset_id: str) -> dict[str, Any]:
    return h.store.trpg_get("rule_state", instance_id=instance_id, timeline_id=timeline_id,
                            campaign_id=campaign_id, ruleset_id=ruleset_id) or {}


async def act_player(h: Any, scope: dict[str, Any], *, fields: dict[str, Any], intent: str,
                     confirm: bool = True, **extra: Any) -> dict[str, Any]:
    return await call(
        h, "trpg.client.act", **scope, mode="player", audience="public_party",
        text=intent, fields=fields, confirm=confirm, **extra,
    )


# ------------------------------------------------------------------ A 段：规范 ⇄ 常数


def _tokens(cell: str) -> list[str]:
    return [item.strip() for item in re.split(r"[、，；;,/／]", cell) if item.strip()]


def section_a() -> None:
    """§7.1 状态表：规范表格行 ⇄ `states.VISIBLE_STATES` 双向对照。"""
    table = re.findall(r"^\|\s*([^|]+?)\s*\|([^|]+?)\|([^|]+?)\|([^|]+?)\|\s*$", CLIENT_SPEC, re.M)
    rows = [row for row in table if "`" in row[0]]
    seen = 0
    for node, label, actions, forbidden in rows:
        for name in [item.strip().strip("`") for item in node.split("/") if item.strip().strip("`")]:
            entry = states.VISIBLE_STATES.get(name)
            if entry is None:
                common.fail(f"A01 §7.1 {name}", "状态表里有这一行", "实现里没有该状态", node)
                continue
            seen += 1
            same_label = label.strip() == str(entry["label"])
            want_actions = _tokens(actions)
            want_forbidden = _tokens(forbidden)
            same_actions = all(any(want in have for have in entry["actions"]) for want in want_actions)
            same_forbidden = all(any(want in have for have in entry["forbidden"]) for want in want_forbidden)
            common.expect(
                f"A01 §7.1 {name}", "文案 / 可操作 / 禁止与规范逐条一致",
                bool(same_label and same_actions and same_forbidden),
                f"label_ok={same_label} actions_ok={same_actions} forbidden_ok={same_forbidden} "
                f"impl_label={entry['label']}", label.strip(),
            )
    common.expect("A01 §7.1 覆盖", "规范表里的状态都实现（≥17 行）",
                  seen >= 17 and len(states.VISIBLE_STATES) == 17,
                  f"逐条核对 {seen} 行 / 实现 {len(states.VISIBLE_STATES)} 行", f"规范表 {len(rows)} 行")
    unknown = states.user_state("travelling")
    common.expect("A02 未知状态不猜", "如实说未知、不给可操作项",
                  not unknown["known"] and "未知状态" in unknown["label"] and not unknown["actions"],
                  json.dumps(unknown, ensure_ascii=False)[:160], "§7.1")
    pairs = [
        ("public_party", "character:pc-1", True),   # 队伍公开对角色可见
        ("character:a", "character:b", False),      # 定向材料不跨角色
        ("character:a", "character:a", True),
        ("", "public_party", False),                # 未标记 = 最严（仅 GM）
        ("gm_only", "public_party", False),
        ("gm_only", "gm_only", True),
    ]
    got = [(material, viewer, views.audience_allows(material, viewer)) for material, viewer, _ in pairs]
    wrong = [item for item, (_, _, want) in zip(got, pairs) if item[2] is not want]
    common.expect("A03 受众闭集与默认严", "定向材料只给同名受众；未标记按最严（仅 GM）",
                  not wrong and views.audience_valid("character:pc-1") and not views.audience_valid("everyone"),
                  f"wrong={wrong} valid(character:pc-1)={views.audience_valid('character:pc-1')} "
                  f"valid(everyone)={views.audience_valid('everyone')}", "§十二")
    common.expect("A04 确认卡字段", "行动者 / 目标 / 方法 / 意图 / 预期结果 + 风险",
                  set(draft_mod.CARD_FIELDS) == {"actor", "target", "method", "intent", "expected_result"},
                  str(sorted(draft_mod.CARD_FIELDS)), "§6.1")


# ------------------------------------------------------------------ B 段：C0


async def section_b(h: Any, manifest: Path) -> None:
    instance_id, timeline_id, campaign_id, actor = await new_campaign(h, manifest)
    scope = {"instance_id": instance_id, "timeline_id": timeline_id, "campaign_id": campaign_id}
    entered = await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
                         character_id=actor)
    item = entered["faces"]["campaign"]
    common.expect("B01 C0 战役选择项", "名称 / 规则系统与版本 / 状态 / 实例与时间线 / 需要处理的事项",
                  bool(item["display_name"] and item["ruleset"] == "client-rules" and item["ruleset_version"] == "1.0"
                       and item["status"] == "active" and item["instance"] and item["timeline"]
                       and item["needs_attention"]["next"]),
                  json.dumps({k: item[k] for k in ("ruleset", "ruleset_version", "status", "instance", "timeline")},
                             ensure_ascii=False), "§5.1")
    scene = entered["faces"]["scene"]
    common.expect("B02 C0 场景六类", "公开事实 / 未知 / 风险 / 可行动作 / 行动结果 / 下一选择",
                  set(scene) >= {"public_facts", "unknowns", "risks", "available_actions", "action_results",
                                 "next_choice"}
                  and scene["public_facts"] and scene["risks"] and scene["available_actions"],
                  json.dumps({k: scene[k] for k in ("public_facts", "risks", "available_actions", "unknowns")},
                             ensure_ascii=False)[:220], "§5.2")
    common.expect("B03 C0 受众隔离", "玩家面看不到 gm_only resolution；GM 面拿得到",
                  "resolution" not in json.dumps(entered["faces"], ensure_ascii=False)
                  and entered["faces"]["gates"]["violations"] == []
                  and "gm" not in entered["faces"],
                  f"gates={entered['faces']['gates']['violations']}", "§十二 / C0")
    gm = await call(h, "trpg.client.enter", **scope, mode="gm", audience="gm_only", character_id=actor)
    common.expect("B03b C0 主持面", "主持面有规则状态版本、工作区、直接变化入口字段",
                  "gm" in gm["faces"] and gm["faces"]["gm"]["direct_change_form"]["fields"]
                  and gm["faces"]["gm"]["rule_state"]["ruleset"] == "client-rules",
                  json.dumps(gm["faces"]["gm"]["direct_change_form"], ensure_ascii=False)[:160], "§7.2")
    blocked_before = len(events(h, instance_id, timeline_id))
    draft = await act_player(h, scope, fields={}, intent="我揭开封条", confirm=False)
    common.expect("B04 C0 输入框不是提交", "未确认只出草稿，不声明、不裁减、不写世界",
                  draft["stage"] == "draft" and not draft["draft"]["ready"]
                  and not h.store.trpg_list("action", instance_id=instance_id, timeline_id=timeline_id)
                  and len(events(h, instance_id, timeline_id)) == blocked_before,
                  f"stage={draft['stage']} actions={len(h.store.trpg_list('action', **scope))}", "§C0 / §18.1")
    stale = await call(h, "trpg.client.refresh", **{**scope, "workspace": {**entered["workspace"],
                                                                          "scene_revision": 999}})
    common.expect("B05 C0 投影作废", "场景 revision 变化后旧投影被丢弃并重新读取",
                  stale["faces"]["stale"] is True and stale["workspace"]["scene_revision"] != 999,
                  f"stale={stale['faces']['stale']} rev={stale['workspace']['scene_revision']}", "C0")
    await call(h, "trpg.campaign.status", **scope, status="paused", reason="整理")
    paused = await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
                        character_id=actor)
    paused_act = await act_player(h, scope, fields={"target": "off-1", "intent": "试探"}, intent="试探")
    common.expect("B06 C0 只读态", "paused 明确只读：不给可提交按钮、行动入口被阻断",
                  paused["faces"]["scene"]["read_only"] and not paused["faces"]["scene"]["available_actions"]
                  and paused_act["stage"] == "blocked",
                  f"read_only={paused['faces']['scene']['read_only']} stage={paused_act['stage']}", "C0")
    await call(h, "trpg.campaign.status", **scope, status="archived", reason="收尾")
    archived = await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
                          character_id=actor)
    common.expect("B06b C0 归档只读", "archived 只读且文案说明",
                  archived["faces"]["scene"]["read_only"] and "归档" in archived["faces"]["campaign"]["status_line"],
                  archived["faces"]["campaign"]["status_line"], "C0")


# ------------------------------------------------------------------ C 段：C1


async def section_c(h: Any, manifest: Path) -> None:
    instance_id, timeline_id, campaign_id, actor = await new_campaign(h, manifest)
    scope = {"instance_id": instance_id, "timeline_id": timeline_id, "campaign_id": campaign_id}
    entered = await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
                         character_id=actor)
    cards = await act_player(h, scope, fields={}, intent="我揭开封条", confirm=False)
    common.expect("C01 C1 草稿卡", "字段整理 + 来源 + 风险（模型只补空缺）",
                  cards["draft"]["fields"]["intent"] == "揭开封条" and cards["draft"]["sources"]["intent"] == "model"
                  and cards["draft"]["fields"]["target"] == "off-1" and cards["draft"]["risks"],
                  json.dumps(cards["draft"], ensure_ascii=False)[:240], "§6.1")
    gaps = await act_player(h, scope, fields={"method": "徒手"}, intent="我去看看", confirm=True)
    common.expect("C02 C1 缺口不放行", "关键字段不明确如实报缺口，不带着猜测去声明",
                  gaps["stage"] == "draft_gaps" and gaps["draft"]["gaps"]
                  and not h.store.trpg_list("action", **scope),
                  f"stage={gaps['stage']} gaps={gaps['draft']['gaps']}", "§6.1 / §18.2")
    done = await act_player(h, scope, fields={"target": "off-1", "intent": "揭开封条", "method": "徒手"},
                            intent="我揭开封条")
    rows = h.store.trpg_list("action", **scope)
    common.expect("C03 C1 闭环", "声明 → 确认 → 真插件裁定 → 玩家模式自动提交",
                  done.get("stage") == "resolved" and done.get("resolved_status") == "reviewing"
                  and done.get("committed") is True and rows and str(rows[-1]["status"]) == "transitioned",
                  f"resolved={done.get('resolved_status')} commit={done.get('commit_status')} "
                  f"row={rows[-1]['status'] if rows else ''}", "§16.1 / §20.2")
    facts = done["result"]["result"]["happened"]
    common.expect("C03b C1 结果第一层", "已经发生 / 当前局面 / 下一步 分开说，且来自已提交后果",
                  any("封条已揭" in line or "活动受限" in line for line in facts)
                  and bool(done["result"]["result"]["next"]),
                  json.dumps(done["result"]["result"], ensure_ascii=False)[:240], "§8.1")
    common.expect("C03c C1 公开说法", "公开说法作为说法给出，不当成事实",
                  "岗位的封条是新的" in done["result"]["reasons"]["public_summary"],
                  json.dumps(done["result"]["reasons"]["public_summary"], ensure_ascii=False), "§3.9 / §8.2")
    common.expect("C03d C1 玩家面没有 GM 层", "第三 / 四层只在主持模式出现",
                  "audit" not in done and "raw_resolution" not in done
                  and done["faces"]["gates"]["violations"] == [],
                  f"keys={sorted(done)[:12]}", "§8.2 / §10.2")

    # 修改：声明后停在 awaiting_confirmation，客户端改成新 intent → revision 涨
    declared = await call(h, "trpg.action.declare", **scope, actor_id=actor, intent="先看门缝",
                          raw_text="先看门缝", target_refs=["off-1"], auto_confirm=False)
    action_id = str(declared["action_id"])
    revised = await act_player(h, scope, fields={"target": "off-1", "intent": "改成从窗户进", "method": "翻窗"},
                               intent="改成从窗户进", action_id=action_id)
    row = h.store.trpg_get("action", **scope, action_id=action_id)
    common.expect("C04 C1 修改涨版本", "同一行动只能一个确认版本，改动 → revision +1 并走新版本",
                  int(row["action_revision"]) == 2 and "窗户" in str(row["intent"])
                  and revised["committed"] is True and revised["revision"] == 2,
                  f"revision={row['action_revision']} status={row['status']} committed={revised.get('committed')}",
                  "§11.2 / §18.7")
    before_abandon = len(events(h, instance_id, timeline_id))
    state_before = int(rule_state(h, instance_id, timeline_id, campaign_id, "client-rules").get("state_revision") or 0)
    pending = await call(h, "trpg.action.declare", **scope, actor_id=actor, intent="试试别处的门",
                         raw_text="试试别处的门", auto_confirm=False)
    dropped = await call(h, "trpg.client.act", **scope, mode="player", audience="public_party",
                         text="算了", action_id=str(pending["action_id"]), abandon=True)
    dropped_row = h.store.trpg_get("action", **scope, action_id=str(pending["action_id"]))
    common.expect("C05 C1 放弃", "不调插件、不写规则状态、不写世界后果",
                  dropped["stage"] == "abandoned" and str(dropped_row["status"]) == "abandoned"
                  and len(events(h, instance_id, timeline_id)) == before_abandon
                  and int(rule_state(h, instance_id, timeline_id, campaign_id, "client-rules")
                          .get("state_revision") or 0) == state_before,
                  f"status={dropped_row['status']} events={len(events(h, instance_id, timeline_id))}", "§18.2 / C1")
    unconfirmed = await call(h, "trpg.action.declare", **scope, actor_id=actor, intent="未经确认的试探",
                             raw_text="", target_refs=["off-1"], auto_confirm=False)
    refused = await call_err(h, "trpg.action.resolve", **scope, action_id=str(unconfirmed["action_id"]),
                             plugin_manifest=str(manifest), actor_id=actor, intent="未经确认的试探")
    common.expect("C06 C1 未确认不裁定", "未确认的关键行动进不了裁定链",
                  "确认" in refused, refused[:150], "§18.2 / C1")

    # GM 模式：不自动提交
    gm_scope = {**scope, "mode": "gm", "audience": "gm_only"}
    before = len(events(h, instance_id, timeline_id))
    gm_done = await call(h, "trpg.client.act", **gm_scope, text="GM 试一次", character_id=actor,
                         confirm=True,
                         fields={"target": "off-1", "intent": "GM 试一次", "method": "暗中观察"})
    common.expect("C07 C1 GM 不自动提交", "GM 模式停在 reviewing 等主持操作；世界没变",
                  gm_done.get("resolved_status") == "reviewing" and gm_done.get("committed") is False
                  and len(events(h, instance_id, timeline_id)) == before
                  and "主持" in str(gm_done.get("skipped") or ""),
                  f"stage={gm_done.get('stage')} resolved={gm_done.get('resolved_status')} "
                  f"committed={gm_done.get('committed')} errors={gm_done.get('errors')}", "§20.3 / §18.3")
    approved = await call(h, "trpg.client.review", **gm_scope, action_id=str(gm_done["action_id"]),
                          decision="approve")
    common.expect("C07b C3 主持批准", "批准走同一条联合提交，来源是行动路径",
                  approved["committed"] is True, f"commit={approved.get('commit_status')}", "§20.3")
    count_after = len(events(h, instance_id, timeline_id))
    replay = await call(h, "trpg.client.retry", **scope, mode="player", audience="public_party",
                        kind="resume_submit", action_id=str(gm_done["action_id"]))
    common.expect("C08 C1 幂等重放", "同幂等键只返回原结果，不重复写世界",
                  "duplicate" in json.dumps(replay.get("commit"), ensure_ascii=False)
                  and len(events(h, instance_id, timeline_id)) == count_after,
                  f"commit={json.dumps(replay.get('commit'), ensure_ascii=False)[:120]}", "§18.8")
    reroll = await call(h, "trpg.client.retry", **scope, mode="player", audience="public_party",
                        kind="reroll", action_id=str(gm_done["action_id"]))
    common.expect("C09 C1 重新裁定是新版本", "reroll 产生新的 action_id / revision，不藏在普通重试里",
                  reroll["action_id"] != str(gm_done["action_id"]) and reroll["committed"] is True,
                  f"new={reroll['action_id']} old={gm_done['action_id']}", "§18.9")
    none_scope = {**scope}
    nothing = await call(h, "trpg.client.act", **none_scope, mode="player", audience="public_party",
                         text="什么都不干", character_id=actor, confirm=True,
                         fields={"target": "off-1", "intent": "nothing", "method": "原地不动"})
    public = (nothing.get("result") or {}).get("result") or {}
    branches = nothing.get("failures") or {}
    common.expect("C10 C1 明确无变化也落账", "没有世界后果也能提交，并如实走「明确无变化」那一支",
                  nothing.get("committed") is True and public.get("kind") == "no_change"
                  and any("没有改变" in line for line in public.get("happened") or [])
                  and branches.get("no_change"),
                  f"stage={nothing.get('stage')} committed={nothing.get('committed')} "
                  f"kind={public.get('kind')} happened={public.get('happened')} "
                  f"branches={json.dumps(branches, ensure_ascii=False)[:160]} errors={nothing.get('errors')}",
                  "§18.14 / §9.3")
    gm_view = await call(h, "trpg.client.enter", **gm_scope, character_id=actor)
    common.expect("C11 C3 原始裁定受限展开", "GM 面有审计层与原始材料，且带引用与版本",
                  bool(gm_view["faces"]["gm"]["review_queue"] is not None)
                  and gm_view["faces"]["gates"]["violations"] == [],
                  json.dumps(gm_view["faces"]["gm"]["rule_state"], ensure_ascii=False), "§8.3 / §10.2")
    res = await call(h, "trpg.client.retry", **scope, mode="gm", audience="gm_only",
                     kind="resume_submit", action_id=str(gm_done["action_id"]))
    common.expect("C11b C3 四层只给 GM", "主持返回里有 audit / raw_resolution（原始骰点与难度）",
                  "audit" in res and "raw_resolution" in res and res["audit"]["level"].get("outcome"),
                  json.dumps(res.get("audit", {}).get("level"), ensure_ascii=False), "§8.3 / §10.2")


# ------------------------------------------------------------------ D 段：C2


async def section_d(h: Any, manifest: Path) -> None:
    instance_id, timeline_id, campaign_id, actor = await new_campaign(h, manifest)
    scope = {"instance_id": instance_id, "timeline_id": timeline_id, "campaign_id": campaign_id}
    await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party", character_id=actor)

    crash = await act_player(h, scope, fields={"target": "off-1", "intent": "crash", "method": "徒手"},
                             intent="crash")
    common.expect("D01 C2 插件失败不猜", "plugin_failed / interrupted，不猜骰点、不说世界已变",
                  crash["result"]["result"]["kind"] == "unresolved"
                  and crash["commit_status"] in ("", "plugin_failed")
                  and not crash["result"]["result"]["happened"]
                  and any("plugin_failed" in item or "interrupted" in item or "fail" in item
                          for item in crash.get("errors") or []) is not None,
                  f"resolved={crash.get('resolved_status')} commit={crash.get('commit_status')} "
                  f"errors={crash.get('errors')}", "§C2 / §18.14")

    before_events = len(events(h, instance_id, timeline_id))
    before_state = int(rule_state(h, instance_id, timeline_id, campaign_id, "client-rules").get("state_revision") or 0)
    review = await act_player(h, scope, fields={"target": "off-1", "intent": "candidate", "method": "徒手"},
                              intent="candidate")
    row = h.store.trpg_get("action", **scope, action_id=str(review["action_id"]))
    common.expect("D02 C2 待审不落半条", "非法后果进待审：规则状态与世界都没落，行动挂起",
                  str(row["status"]) == "awaiting_gm_review" and review["committed"] is False
                  and len(events(h, instance_id, timeline_id)) == before_events
                  and int(rule_state(h, instance_id, timeline_id, campaign_id, "client-rules")
                          .get("state_revision") or 0) == before_state,
                  f"status={row['status']} events={len(events(h, instance_id, timeline_id))}", "§18.5 / C2")
    gm = {**scope, "mode": "gm", "audience": "gm_only"}
    queue = await call(h, "trpg.client.enter", **gm, character_id=actor)
    common.expect("D02b C3 待审工作区", "GM 面能看到挂起的行动与原因",
                  any(str(item["action_id"]) == str(review["action_id"]) for item in queue["faces"]["gm"]["review_queue"]),
                  json.dumps(queue["faces"]["gm"]["review_queue"], ensure_ascii=False)[:200], "C3 / §7.2")
    held = await call(h, "trpg.client.review", **gm, action_id=str(review["action_id"]), decision="hold")
    rejected = await call(h, "trpg.client.review", **gm, action_id=str(review["action_id"]), decision="reject",
                          reason="不合规则")
    common.expect("D02c C3 主持拒绝", "hold 不动；reject 退回该行动（rejected），世界仍没变",
                  "不动规则状态与世界" in held["note"] and rejected["action_status"] == "rejected"
                  and len(events(h, instance_id, timeline_id)) == before_events,
                  f"hold={held['note']} reject={rejected['action_status']}", "C3")

    clock_before = int(h.world.clock_row(timeline_id)["processed_world"])
    choice = await act_player(h, scope, fields={"target": "off-1", "intent": "choice", "method": "追上去"},
                              intent="choice")
    waiting = await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
                         character_id=actor)
    common.expect("D03 C2 待选择闸", "choice 卡在场景首位，关键行动被锁，战役 waiting",
                  choice.get("choice") and choice["choice"]["options"]
                  and [item["text"] for item in choice["choice"]["options"]] == ["追上去", "先撤退"]
                  and waiting["faces"]["next"]["locks"] == ["新的关键行动"]
                  and waiting["faces"]["campaign"]["status"] == "waiting",
                  f"choice={json.dumps(choice.get('choice'), ensure_ascii=False)[:160]} "
                  f"next={waiting['faces']['next']['next']}", "§16.2 / C2")
    clock_after_commit = int(h.world.clock_row(timeline_id)["processed_world"])
    common.expect("D04 C2 双轨时间", "规则节拍与世界时间分开显示；时间消耗真的前移了世界时钟",
                  "已前进 600 秒" in choice["time"]["world_time"]
                  and 600 <= clock_after_commit - clock_before <= 660  # 实时结算会多走几秒
                  and "规则节拍" in choice["time"]["rule_beat"],
                  f"time={choice['time']} clock={clock_before}->{clock_after_commit}", "§11 / §18.13")
    locked = await act_player(h, scope, fields={"target": "off-1", "intent": "抢在别人前面", "method": "跑"},
                              intent="抢在别人前面")
    common.expect("D04b C2 待选择时不放行新行动", "waiting 期间不给新的关键行动入口",
                  locked["stage"] == "blocked" and "等待选择" in locked["blocked"],
                  f"stage={locked['stage']} blocked={locked.get('blocked')}", "§16.2 / §18.6")
    picked = await call(h, "trpg.client.choice", **scope, mode="player", audience="public_party",
                        choice_id="ch-1", option_id="追上去")
    again = await call(h, "trpg.client.choice", **scope, mode="player", audience="public_party",
                       choice_id="ch-1", option_id="先撤退")
    resumed = await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
                         character_id=actor)
    common.expect("D05 C2 选择与重复选择", "选择后回到 active；重复选择返回原结果，不重新裁定",
                  picked["faces"]["campaign"]["status"] == "active" and again["duplicate"] is True
                  and not resumed["faces"]["campaign"]["read_only"]
                  and not resumed["faces"]["scene"]["next_choice"],
                  f"status={picked['faces']['campaign']['status']} duplicate={again['duplicate']}", "§11.3 / C2")

    before_time_bad = int(h.world.clock_row(timeline_id)["processed_world"])
    bad = await act_player(h, scope, fields={"target": "off-1", "intent": "time_bad", "method": "等"},
                           intent="time_bad")
    drifted = int(h.world.clock_row(timeline_id)["processed_world"]) - before_time_bad
    common.expect("D06 C2 非法时间不动钟", "时间请求非法时客户端不移动世界时钟，并停在真实状态",
                  drifted <= 5  # 只有实时结算的漂移，没有那 600 秒
                  and "未移动" in bad["time"]["world_time"] and bad["committed"] is False,
                  f"time={bad['time']['world_time']} drift={drifted}", "§11 / C2")

    # 重启恢复：把在途行动改回 resolving，再看恢复与显式重试
    row = h.store.trpg_get("action", **scope, action_id=str(review["action_id"]))
    stuck = await call(h, "trpg.action.declare", **scope, actor_id=actor, intent="ok",
                       raw_text="ok", target_refs=["off-1"], auto_confirm=True)
    stuck_id = str(stuck["action_id"])
    h.store.trpg_upserts({"action": [{**h.store.trpg_get("action", **scope, action_id=stuck_id),
                                      "status": "resolving"}]})
    recovered = await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
                           character_id=actor)
    stuck_row = h.store.trpg_get("action", **scope, action_id=stuck_id)
    common.expect("D07 C2 重启恢复", "resolving 进 interrupted，客户端给显式重试入口",
                  recovered["recovery"]["interrupted"] >= 1 and str(stuck_row["status"]) == "interrupted",
                  f"recovery={recovered['recovery']} status={stuck_row['status']}", "§13 / C2")
    retried = await call(h, "trpg.client.retry", **scope, mode="player", audience="public_party",
                         kind="retry_resolve", action_id=stuck_id)
    common.expect("D07b C2 显式重试", "显式重试走真实裁定，拿到新结果并提交",
                  retried.get("resolved_status") == "reviewing" and retried.get("committed") is True,
                  f"resolved={retried.get('resolved_status')} commit={retried.get('commit_status')} "
                  f"errors={retried.get('errors')} skipped={retried.get('skipped')}", "§7.1")

    # 版本阻断 + 人工接受
    manifest.write_text(json.dumps({
        "id": "client-rules", "name": "client probe", "version": "2.0",
        "protocol": "isekai.trpg.rules/1", "entry": [sys.executable, "main.py"],
    }), encoding="utf-8")
    blocked = await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
                         character_id=actor)
    gate_err = await call_err(h, "trpg.client.act", **scope, mode="player", audience="public_party",
                              text="ok", character_id=actor, confirm=True,
                              fields={"target": "off-1", "intent": "ok", "method": "徒手"})
    common.expect("D08 C2 版本阻断", "插件版本与规则状态不一致：阻断、给出可用出口、禁止继续裁定",
                  blocked["version_block"]["blocked"] is True
                  and blocked["version_block"]["forbidden"] and blocked["version_block"]["allowed"]
                  and "2.0" in blocked["version_block"]["reason"] and "版本" in gate_err,
                  f"{json.dumps(blocked['version_block'], ensure_ascii=False)[:170]} | 裁定被拒：{gate_err[:80]}",
                  "§14.4 / C2")
    accepted = await call(h, "trpg.campaign.status", **scope, status="active", reason="人工接受新版本",
                          accept_ruleset_version="2.0")
    after = await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
                       character_id=actor)
    common.expect("D09 C2 人工接受", "人工接受是主持人承担的明确决定：接受后阻断解除，且留下记录",
                  after["version_block"]["blocked"] is False
                  and ("接受规则版本" in str(accepted.get("note") or "")) and "2.0" in str(accepted.get("note")),
                  f"note={accepted.get('note')} block={after['version_block']}", "§14.4 / C2")


# ------------------------------------------------------------------ E 段：C3


async def section_e(h: Any, manifest: Path) -> None:
    manifest.write_text(json.dumps({
        "id": "client-rules", "name": "client probe", "version": "1.0",
        "protocol": "isekai.trpg.rules/1", "entry": [sys.executable, "main.py"],
    }), encoding="utf-8")
    instance_id, timeline_id, campaign_id, actor = await new_campaign(h, manifest)
    scope = {"instance_id": instance_id, "timeline_id": timeline_id, "campaign_id": campaign_id}
    gm = {**scope, "mode": "gm", "audience": "gm_only"}
    await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party", character_id=actor)
    preview = await call(h, "trpg.client.gm_change", **gm, preview_only=True,
                         form={"target_ref": "off-1", "kind": "condition", "op": "create", "audience": "public_party",
                               "reason": "GM 私下放水", "idempotency_key": "gm-preview-1",
                               "value": "岗哨换班", "frame": "夜里换了岗"})
    common.expect("E01 C3 玩家视角预览", "预览只含玩家视角材料，不含主持依据",
                  preview["stage"] == "gm_change_preview" and preview["preview_form"]["reason"] == "GM 私下放水"
                  and "GM 私下放水" not in json.dumps(preview["faces"]["gm"].get("player_preview") or {},
                                                      ensure_ascii=False)
                  and preview["faces"]["gates"]["violations"] == [],
                  json.dumps(preview["faces"]["gm"].get("player_preview") or {}, ensure_ascii=False)[:220],
                  "§16.3 / C3")
    before = len(events(h, instance_id, timeline_id))
    change = await call(h, "trpg.client.gm_change", **gm,
                        form={"target_ref": "off-1", "kind": "condition", "op": "create", "audience": "public_party",
                              "reason": "GM 放水", "idempotency_key": "gm-1", "value": "岗哨换班",
                              "frame": "夜里换了岗"})
    written = events(h, instance_id, timeline_id)
    common.expect("E02 C3 gm_declaration", "GM 直接变化落成 gm_declaration，走同一条联合提交边界",
                  change["gm_change_result"]["status"] in ("committed", "duplicate")
                  and len(written) > before and str(written[-1]["source"]) == "gm_declaration"
                  and change["gm_change_result"]["visible_to"] == ["public_party"]
                  and change["gm_change_result"]["rescan_scene"] is True,
                  f"status={change['gm_change_result']['status']} "
                  f"errors={change['gm_change_result'].get('errors')} "
                  f"source={written[-1]['source'] if written else ''}",
                  "§十三 / C3")
    common.expect("E02b C3 故事意图不进这里", "直接变化结果里明确故事意图只能形成主持候选",
                  "故事意图" in change["gm_change_result"]["note"],
                  change["gm_change_result"]["note"], "§十三")
    private = await act_player(h, scope, fields={"target": "off-1", "intent": "private", "method": "试探"},
                               intent="private")
    common.expect("E03 C3 私有后果不进玩家面", "gm_only 后果不进玩家结果，闸门仍然干净",
                  not any("密探已布防" in line for line in private["result"]["result"]["happened"])
                  and private["faces"]["gates"]["violations"] == []
                  and private["commit_status"] in ("committed", "duplicate"),
                  json.dumps(private["result"]["result"], ensure_ascii=False)[:200], "§十二 / C3")
    gm_after = await call(h, "trpg.client.retry", **gm, kind="resume_submit",
                          action_id=str(private["action_id"]))
    common.expect("E03b C3 GM 拿得到私有材料", "同一批后果在 GM 审计层可见（受众允许）",
                  any("密探已布防" in json.dumps(item, ensure_ascii=False)
                      for item in (gm_after.get("audit") or {}).get("effects") or []),
                  json.dumps((gm_after.get("audit") or {}).get("effects"), ensure_ascii=False)[:200],
                  "§8.3 / §12")

    leak = {"faces": {"scene": {"public_facts": [{"text": "密探已布防", "audience": "gm_only"}]}}}
    violations = views.display_gate(leak, "public_party")
    private_ok = views.display_gate(leak, "gm_only")
    common.expect("E04 C3 闸门负例", "把 gm_only 材料塞进玩家面时闸门必须报违规（不是摆设）",
                  len(violations) == 1 and violations[0]["kind"] == "audience" and not private_ok,
                  json.dumps(violations, ensure_ascii=False), "§十二")
    denied = views.display_gate({"reasons": {"resolution": {"rolls": [7]}}}, "public_party")
    common.expect("E04b C3 字段负例", "玩家面出现 resolution / rolls 这类字段也要报违规",
                  len(denied) >= 1, json.dumps(denied, ensure_ascii=False), "§十二")

    # 待审 → 批准（GM 明确提交）
    review = await act_player(h, scope, fields={"target": "off-1", "intent": "candidate", "method": "徒手"},
                              intent="candidate")
    approve_err = await call_err(h, "trpg.client.review", **gm, action_id=str(review["action_id"]),
                                 decision="approve")
    rejected = await call(h, "trpg.client.review", **gm, action_id=str(review["action_id"]),
                          decision="reject", reason="规则上不成立")
    common.expect("E05 C3 待审工作区出口", "没有可提交裁定的待审不能直接批准（只有改 / 重跑 / 拒绝），"
                  "拒绝不留世界痕迹",
                  "没有可提交的裁定" in approve_err and rejected["action_status"] == "rejected"
                  and not any("candidate" in str(item.get("summary") or "")
                              for item in events(h, instance_id, timeline_id)),
                  f"approve={approve_err[:80]} reject={rejected['action_status']}", "§20.3 / §18.3")


# ------------------------------------------------------------------ G 段：C4


async def _multi_campaign(h: Any, manifest: Path) -> tuple[str, str, str, str, str]:
    """同一个用户控制两个角色：一个实例、一张战役、两份私密视图。"""
    package = example_package("灰潮纪", moment=DAY * 1500 + 30000)
    card_a = example_card(package, name="堤禾")
    card_b = example_card(package, name="渡舟")
    info = create_instance(h.store, package, [card_a, card_b])
    instance_id = str(info["id"])
    timeline_id = str(h.store.timeline_list(instance_id)[0]["id"])
    h.world.ensure_instance(instance_id, now_real=time.time())
    h.world.activate(instance_id, timeline_id, now_real=time.time())
    actor_a = str(card_a["meta"]["card_id"])
    actor_b = str(card_b["meta"]["card_id"])
    common.ACTORS[instance_id] = actor_a
    created = await call(
        h, "trpg.campaign.create", instance_id=instance_id, timeline_id=timeline_id,
        ruleset_id="client-rules", ruleset_version="1.0", plugin_manifest=str(manifest),
        host_mode="autonomous", participants=[actor_a, actor_b],
        scene={"kind": "conflict", "location_refs": ["rl-1"],
               "public_facts": [{"text": "岗位的门半掩着"}],
               "private_views": {f"character:{actor_a}": [{"text": "堤禾认得墙上的划痕"}],
                                 f"character:{actor_b}": [{"text": "渡舟听见水声"}]},
               "available_actions": ["查岗"]},
    )
    return instance_id, timeline_id, str(created["campaign_id"]), actor_a, actor_b


async def section_g(h: Any, manifest: Path) -> None:
    """C4：多角色切换（不合并私密认知）+ 第二个真实规则插件（不写死第一套规则字段）。"""
    instance_id, timeline_id, campaign_id, actor_a, actor_b = await _multi_campaign(h, manifest)
    scope = {"instance_id": instance_id, "timeline_id": timeline_id, "campaign_id": campaign_id}
    entered = await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
                         character_id=actor_a)
    first = await call(h, "trpg.client.switch", **scope, workspace=entered["workspace"],
                       character_id=actor_a)
    second = await call(h, "trpg.client.switch", **scope, workspace=first["workspace"],
                        character_id=actor_b)
    a_views = json.dumps(first["faces"]["party"], ensure_ascii=False)
    b_views = json.dumps(second["faces"]["party"], ensure_ascii=False)
    common.expect("G01 C4 切角色不合并认知", "切换只换 actor / audience：当前角色的私密材料才可见",
                  "堤禾认得墙上的划痕" in a_views and "渡舟听见水声" not in a_views
                  and "渡舟听见水声" in b_views and "堤禾认得墙上的划痕" not in b_views
                  and second["workspace"]["audience"] == f"character:{actor_b}"
                  and second["faces"]["gates"]["violations"] == [],
                  f"A面={a_views[:150]} B面={b_views[:150]}", "§6 / C4")
    denied = await call_err(h, "trpg.client.switch", **scope, character_id=actor_b, audience="gm_only")
    common.expect("G01b C4 不凭身份提升受众", "玩家模式下不能切到 gm_only 受众",
                  "gm_only" in denied and "主持" in denied, denied[:120], "§十二")
    rows_before = len(h.store.trpg_list("action", **scope))
    await call(h, "trpg.client.enter", **scope, mode="player", audience=f"character:{actor_a}",
               character_id=actor_a)
    await call(h, "trpg.client.refresh", **scope, mode="player", audience="public_party")
    common.expect("G01c C4 只读不写", "读路径（enter / refresh）不建行动、不写事件",
                  len(h.store.trpg_list("action", **scope)) == rows_before
                  and not events(h, instance_id, timeline_id),
                  f"actions={len(h.store.trpg_list('action', **scope))}", "§18.1 / C4")

    # 第二个真实插件：潮汐骰池（骰池 / 压力 / 际遇，与第一套规则的属性 / 技能 / 骰点全不同）
    tide = ROOT / "examples" / "tide_rules_plugin" / "manifest.json"
    instance_id, timeline_id, campaign_id, actor = await new_campaign(
        h, tide, ruleset_id="tide", version="0.1.0"
    )
    scope = {"instance_id": instance_id, "timeline_id": timeline_id, "campaign_id": campaign_id}
    await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
               character_id=actor)
    second_plugin = await call(
        h, "trpg.client.act", **scope, mode="player", audience="public_party", character_id=actor,
        # 故意不带「也许 / 耗尽」：那两个词在潮汐插件里分别进候选与拒绝分支，不是这次要验的东西
        text="沿墙摸过去查岗", confirm=True,
        fields={"target": "off-1", "intent": "沿墙摸过去查岗", "method": "沿墙摸过去"},
    )
    gm = {**scope, "mode": "gm", "audience": "gm_only"}
    audit = await call(h, "trpg.client.enter", **gm, character_id=actor)
    surface = json.dumps({"result": second_plugin.get("result"), "faces": second_plugin.get("faces")},
                         ensure_ascii=False)
    common.expect("G02 C4 差异化规则插件", "同一套客户端流程跑第二套规则：不读第一套规则的属性 / 资源 / 骰点字段",
                  second_plugin.get("committed") is True
                  and not any(name in surface for name in ("/hp", "\"hp\"", "\"sp\"", "edge", "\"pool\"", "\"stress\""))
                  and "gm" in audit["faces"],
                  f"stage={second_plugin.get('stage')} commit={second_plugin.get('commit_status')} "
                  f"resolved={second_plugin.get('resolved_status')} errors={second_plugin.get('errors')} "
                  f"surface={surface[:160]}", "§20.7 / C4")
    tide_audit = await call(h, "trpg.client.retry", **gm, kind="resume_submit",
                            action_id=str(second_plugin["action_id"]))
    common.expect("G02b C4 等级来自插件声明", "结果等级读的是插件 resolve 的通用字段（system / outcome / degree）",
                  bool(((tide_audit.get("audit") or {}).get("level") or {}).get("outcome")),
                  json.dumps((tide_audit.get("audit") or {}).get("level"), ensure_ascii=False), "§8.3 / C4")

    # OC 与 TRPG 共用世界：TRPG 提交的事实与说法走同一条合法认知路径。
    # 潮汐骰子按 action_id 定种子，**失败分支不申报说法** —— 所以「认知里有没有说法」不能写死：
    # 判据按这次的真实结果分档（失败 ⇒ 不许出现这条说法；其余 ⇒ 必须出现）。
    cognition = h.world.cognition_project(instance_id, timeline_id, observer_id=actor)
    history = await call(h, "runtime.history.read", instance_id=instance_id, timeline_id=timeline_id,
                         filters={"source": "trpg_action"})
    outcome = ((tide_audit.get("audit") or {}).get("level") or {}).get("outcome")
    claim_texts = [str(item.get("text") or "") for item in cognition.get("claims") or []]
    mine = [text for text in claim_texts if text.startswith("潮线记下")]
    expects_claim = outcome != "失败"
    common.expect("G03 C4 跨应用共用世界", "TRPG 提交进同一份世界历史；认知里有没有这条说法按插件申报的结果分档",
                  any(str(item.get("source")) == "trpg_action" for item in history.get("items") or [])
                  and bool(mine) == expects_claim,
                  f"history={len(history.get('items') or [])} outcome={outcome} 潮线说法={mine} "
                  f"claims={json.dumps(claim_texts, ensure_ascii=False)[:120]}", "§19 C4")


# ------------------------------------------------------------------ H 段：C5


async def section_h(h: Any, manifest: Path) -> None:
    """C5：分支 / 回滚的风险确认与本地状态清理；结构化结果的人工表达入口。"""
    instance_id, timeline_id, campaign_id, actor = await new_campaign(h, manifest)
    scope = {"instance_id": instance_id, "timeline_id": timeline_id, "campaign_id": campaign_id}
    gm = {**scope, "mode": "gm", "audience": "gm_only"}
    entered = await call(h, "trpg.client.enter", **scope, mode="player", audience="public_party",
                         character_id=actor)
    done = await act_player(h, scope, fields={"target": "off-1", "intent": "ok", "method": "徒手"},
                            intent="ok")
    commit = h.world.commit(instance_id, timeline_id, kind="manual", note="分支点")
    commit_id = str(commit["id"])
    timelines_before = len(h.store.timeline_list(instance_id))

    brief = await call(h, "trpg.client.branch", **gm, commit_id=commit_id)
    common.expect("H01 C5 分支前置说明", "未确认时只给说明：不带入什么、要选是否激活、原线保留",
                  brief["stage"] == "branch_confirm" and brief["brief"]["confirm_required"]
                  and len(brief["brief"]["lines"]) >= 4
                  and len(h.store.timeline_list(instance_id)) == timelines_before,
                  json.dumps(brief["brief"], ensure_ascii=False)[:200], "§14.2 / C5")
    made = await call(h, "trpg.client.branch", **gm, workspace=brief["workspace"], commit_id=commit_id,
                      name="试演线", confirm=True)
    common.expect("H02 C5 建线不激活", "确认后新建时间线；未选激活时当前线不变，原线保留",
                  made["new_timeline_id"] and len(h.store.timeline_list(instance_id)) == timelines_before + 1
                  and made["workspace"]["timeline_id"] == timeline_id
                  and made["original_timeline_kept"] is True,
                  f"new={made.get('new_timeline_id')} ws={made['workspace']['timeline_id']} "
                  f"lines={len(h.store.timeline_list(instance_id))}", "§14.2")
    activated = await call(h, "trpg.client.branch", **gm, workspace=made["workspace"], commit_id=commit_id,
                           name="试演线2", activate=True, confirm=True)
    common.expect("H03 C5 激活后重读", "选激活就换线：工作区时间线变了，旧选中项作废",
                  activated["workspace"]["timeline_id"] == activated["new_timeline_id"]
                  and activated["workspace"]["selected_action_id"] == ""
                  and activated["faces"]["campaign"]["timeline"],
                  f"ws={activated['workspace']['timeline_id']} new={activated.get('new_timeline_id')}",
                  "§14.2 / §C0")
    # 回滚前再推一次规则状态（2），这样回滚到提交点能看出真的退回去了
    await act_player(h, scope, fields={"target": "off-1", "intent": "ok", "method": "徒手"},
                     intent="ok")
    # 回滚要在**原线**上做：工作区换成原线的视图（H03 之后它还指着试演线）
    back = {**gm, "workspace": {**activated["workspace"], "timeline_id": timeline_id,
                                "scene_revision": 999}}
    state_before = int(rule_state(h, instance_id, timeline_id, campaign_id, "client-rules")
                       .get("state_revision") or 0)
    risky = await call(h, "trpg.client.rollback", **back, commit_id=commit_id)
    common.expect("H04 C5 回滚前置说明", "未确认时列出会失效什么 / 外部表达不保证消失 / 世代变化 / 需重读；不执行",
                  risky["stage"] == "rollback_confirm" and len(risky["brief"]["lines"]) >= 5
                  and int(rule_state(h, instance_id, timeline_id, campaign_id, "client-rules")
                          .get("state_revision") or 0) == state_before,
                  json.dumps(risky["brief"], ensure_ascii=False)[:200], "§14.3 / C5")
    # 手里再塞一个过期的场景版本：验证回滚后客户端真的把旧投影作废并重新读取（§14.3）
    rolled = await call(h, "trpg.client.rollback", **{k: v for k, v in back.items() if k != "workspace"},
                        workspace={**risky["workspace"], "scene_revision": 999},
                        commit_id=commit_id, confirm=True, saved=True)
    state_after = int(rule_state(h, instance_id, timeline_id, campaign_id, "client-rules")
                      .get("state_revision") or 0)
    common.expect("H05 C5 回滚后清空并重读", "回滚让规则状态退回提交点；旧场景缓存 / 待选择 / 草稿 / 选中项清空",
                  state_after < state_before and len(rolled["discarded"]) >= 4
                  and rolled["workspace"]["draft_text"] == ""
                  and rolled["workspace"]["selected_action_id"] == ""
                  and rolled["stage"] == "rollback" and rolled["faces"]["gates"]["violations"] == [],
                  f"state {state_before}->{state_after} stale={rolled['faces']['stale']} "
                  f"ws_rev={rolled['workspace']['scene_revision']} "
                  f"scene={json.dumps(rolled['faces']['scene']['scene'], ensure_ascii=False)} "
                  f"empty={rolled['faces']['scene']['empty']}",
                  "§14.3 / C5")
    # 主持面工作区（audience=gm_only 是主持人自己的视角），材料受众由每次表达单独给
    tell_scope = {k: v for k, v in gm.items() if k != "audience"}
    told = await call(h, "trpg.client.express", **tell_scope, workspace=rolled["workspace"],
                      text="GM 补叙：夜里换了岗，封条是新的",
                      action_id=str(done.get("action_id") or ""), audience="public_party")
    claims = [item for item in h.store.claim_list(instance_id, timeline_id)
              if "补叙" in str(item.get("text") or "")]
    seedy = await call_err(h, "trpg.client.express", **tell_scope, text="私下安排", audience="gm_only",
                           as_frame=True)
    framed = await call(h, "trpg.client.express", **tell_scope, text="GM 补叙：门缝里的风是冷的",
                        audience="public_party", as_frame=True)
    event_text = " ".join(str(item.get("summary") or "") for item in
                          h.store.event_window(instance_id, timeline_id, until=10**15, limit=200))
    common.expect("H06 C5 人工表达入口", "主持写的正文落成受众标记的说法；事件帧只许公开材料",
                  told.get("committed") is True and claims and str(claims[0]["audience"]) == "public_party"
                  and "事件正文" in seedy and framed.get("committed") is True
                  and "门缝里的风是冷的" in event_text,
                  f"told={told.get('commit_status')} claims={len(claims)} seedy={seedy[:60]} "
                  f"framed={framed.get('commit_status')}", "§C5 / §十三")


# ------------------------------------------------------------------ F 段：不变量


def section_f() -> None:
    """§十八 产品不变量：逐条引用前面几段的读数（同一批探针，不再另造一套证据）。"""
    def find(clause: str) -> str:
        for item in RESULTS:
            if clause in str(item["clause"]):
                return str(item["status"])
        return "（缺该条读数）"

    rows = [
        ("F01 §18.1 行动声明不是世界事实", "B04", "草稿不声明、不写世界"),
        ("F02 §18.2 未确认不进裁定链", "C06", "未确认被拒"),
        ("F03 §18.3 玩家自动提交 / GM 明确提交", "C07", "GM 停在 reviewing"),
        ("F04 §18.4 插件成功 ≠ 已提交", "C09", "reroll 前必须提交才落世界"),
        ("F05 §18.5 patch 与后果同批", "D02", "待审时两者都没落"),
        ("F06 §18.6 待选择不是世界事实", "D03", "只给合法选项"),
        ("F07 §18.7 revision 唯一确认版本", "C04", "修改涨版本"),
        ("F08 §18.8 幂等", "C08", "重复提交返回原结果"),
        ("F09 §18.9 重新裁定是新版本", "C09", "新 action_id"),
        ("F10 §18.14 失败四分 / 明确无变化", "C10", "无变化也落账"),
        ("F11 §18.13 规则时间与世界时间分开", "D04", "双轨显示"),
        ("F12 §18.12 GM 私有材料不进玩家面", "E03", "私有后果不进玩家结果"),
    ]
    for clause, ref, expected in rows:
        status = find(ref)
        common.expect(clause, f"{expected}（读数来自 {ref}）", status == "PASS",
                      f"{ref} → {status}", f"§十八 / {ref}")


# ------------------------------------------------------------------ 主流程


async def run(only: str) -> int:
    global CLIENT_SPEC
    manifest = Path(tempfile.mkdtemp(prefix="trpgclient-rules-")) / "manifest.json"
    manifest.write_text(json.dumps({
        "id": "client-rules", "name": "client probe", "version": "1.0",
        "protocol": "isekai.trpg.rules/1", "entry": [sys.executable, "main.py"],
    }), encoding="utf-8")
    (manifest.parent / "main.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    async with core() as h:
        if not only or "A" in only.upper()[:2]:
            section_a()
        if not only or only.upper().startswith("B"):
            await section_b(h, manifest)
        if not only or only.upper().startswith("C"):
            await section_c(h, manifest)
        if not only or only.upper().startswith("D"):
            await section_d(h, manifest)
        if not only or only.upper().startswith("E"):
            await section_e(h, manifest)
        if not only or only.upper().startswith("G"):
            await section_g(h, manifest)
        if not only or only.upper().startswith("H"):
            await section_h(h, manifest)
    section_f()
    passed = len([item for item in RESULTS if item["status"] == "PASS"])
    failed = len([item for item in RESULTS if item["status"] == "FAIL"])
    print(f"\nTOTAL={len(RESULTS)} PASS={passed} FAIL={failed}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TRPG 客户端层审计探针")
    parser.add_argument("--only", default="", help="只跑包含该关键字的段（A/B/C/D/E/F）")
    parser.add_argument("--json", action="store_true", help="把读数写成 JSON")
    ns = parser.parse_args(argv)
    common.ONLY = ns.only
    code = asyncio.run(run(ns.only))
    if ns.json:
        print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
