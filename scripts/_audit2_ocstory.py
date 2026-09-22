"""OC_STORY_LAYER_SPEC（OC 故事层）行为级审计探针。

问题：规范里那份「行为验收」表（§十二 十二行）与 §3.4 / §4.4 / §五 / §六 的约束，
在代码里是**真行为**还是只有文档与符号？

判法（真 WebSocket + 真 SQLite + 真实例，只把 LLM 换成 FakeLLM）：

- A 段：规范 ⇄ 常数的双向对照（状态表、六类、黑箱禁词、注册表、CLI 同名命令）；
- B 段：逐条跑 §十二 的十二个场景，读数来自入站状态 / 出站 role / 投递汇总 / 事件条数 / 提示词；
- C 段：消费方事实——故事层是不是走 WorldRuntime 对外接口，而不是绕过它写库
  （这一条也是 `_audit2_iface.py` 里唯一 DEFERRED 的收口：现在有第二类真实调用方了）。

只读项目代码；数据写在临时目录；联网不需要（FakeLLM）。
用法：`.venv/Scripts/python.exe scripts/_audit2_ocstory.py [--only 关键字] [--json]`
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.client import MgmtClient, UmpClient  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM, LLMError  # noqa: E402
from isekai_core.story import classify, expression, state, view  # noqa: E402
from isekai_core.world import ops as world_ops  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402
from isekai_core.world_cli import OP_BY_COMMAND  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[dict[str, Any]] = []
ONLY = ""
CONTACT = '{"category": "contact_share", "why": "分享近况"}'
FOLLOWUP = '{"category": "followup", "why": "追问旧事"}'


def spec_text() -> str:
    """规范正文：按名字递归找，别写死 docs 路径（docs 分过目录）。"""
    hits = [path for path in (ROOT / "docs").rglob("OC_STORY_LAYER_SPEC.md")]
    return hits[0].read_text(encoding="utf-8") if hits else ""


def check(clause: str, expected: str, observed: str, status: str, evidence: str = "", code_ref: str = "") -> None:
    RESULTS.append({"clause": clause, "status": status, "expected": expected, "observed": observed,
                    "evidence": evidence, "code_ref": code_ref})
    print(f"[{status:8}] {clause} :: {observed[:170]}")


def only_matches(clause: str) -> bool:
    return not ONLY or ONLY in clause


@contextlib.asynccontextmanager
async def core(*, replies: list[str] | None = None, fail_with: LLMError | None = None) -> AsyncIterator[Any]:
    """临时根目录里跑真核心（真 WS + 真 SQLite），只换 LLM。"""
    with tempfile.TemporaryDirectory(prefix="ocstory-") as tmp:
        folder = Path(tmp) / "config"
        folder.mkdir(parents=True, exist_ok=True)
        # 睡眠期等待压到 0.05s：探针不该为节拍等分钟（世界时刻也取她的白天）
        (folder / "config.yaml").write_text(
            "runtime:\n  sleep_wait_min_s: 0.05\n  sleep_wait_max_s: 0.05\n", encoding="utf-8"
        )
        cfg = load_config(tmp)
        fake = FakeLLM(replies or ["收到。"], fail_with=fail_with)
        fake.judgements["输入分类"] = CONTACT
        runtime = await build_runtime(cfg, llm=fake)
        endpoint = await runtime.server.start()
        mgmt = MgmtClient(endpoint, runtime.server.mgmt_token)
        await mgmt.connect()
        try:
            yield SimpleNamespace(
                cfg=cfg, store=runtime.store, world=runtime.world, service=runtime.service,
                fake=fake, mgmt=mgmt, endpoint=endpoint, root=Path(tmp),
            )
        finally:
            await mgmt.close()
            await runtime.service.shutdown()
            await runtime.server.close()
            runtime.store.close()


async def room(h: Any, *, names: tuple[str, ...] = ("堤禾",), channel: str = "builtin",
               thread: str = "dm-1", activate: bool = True, moment: int = DAY * 1500 + 30000):
    """真实例 + 通道 + 会话 + thread 绑定（与 conftest.bind_thread 同款，探针自带一份）。"""
    package = example_package("灰潮纪", moment=moment)
    cards = [example_card(package, name=name) for name in names]
    info = create_instance(h.store, package, cards)
    timeline_id = h.store.timeline_list(info["id"])[0]["id"]
    h.world.ensure_instance(info["id"], now_real=time.time())
    if activate:
        h.world.activate(info["id"], timeline_id, now_real=time.time())
    ids = [str(card["meta"]["card_id"]) for card in cards]
    issued = await h.mgmt.call("channel.ensure", name=channel, capabilities={})
    session = (await h.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=timeline_id,
                                 character_id=ids[0]))["session"]
    bound = (await h.mgmt.call("thread.bind", channel=channel, thread_id=thread, session_id=session["id"]))["thread"]
    client = UmpClient(endpoint=h.endpoint, channel_id=channel, name=channel, credential=issued["credential"])
    await client.connect()
    return client, {"thread": bound, "session": session}, info["id"], timeline_id, ids


async def say(client: UmpClient, thread: dict, text: str) -> str:
    ref = await client.send_user_message(
        thread_id=str(thread["thread_id"]), binding_token=str(thread["binding_token"]), text=text
    )
    await client.expect(lambda env: env.type == "accepted")
    return ref


def counts(h: Any, instance_id: str, timeline_id: str) -> dict[str, int]:
    return {
        "events": len(h.store.event_ids(instance_id, timeline_id)),
        "effects": len(h.store.effect_active_ids(instance_id, timeline_id)),
        "claims": len(h.store.claim_list(instance_id, timeline_id)),
    }


# ------------------------------------------------------------------ A 段：规范 ⇄ 常数


def static_facts() -> None:
    spec = spec_text()
    if only_matches("A1 规范可读"):
        check("A1 规范可读", "能读到 OC_STORY_LAYER_SPEC 正文", f"{len(spec)} 字符",
              "PASS" if spec else "FAIL", evidence="按文件名递归查找 docs/ 下的规范",
              code_ref="docs/oc-story/OC_STORY_LAYER_SPEC.md")

    if only_matches("A2 §4.4 产品状态表"):
        rows = [("preparing", "准备中"), ("available", "可联络"), ("catching_up", "世界追赶中"),
                ("generating", "生成中"), ("expressed", "已表达"), ("deferred", "暂缓"),
                ("handoff", "需转交"), ("blocked", "被阻断")]
        miss = [name for name, label in rows if state.LABELS.get(name) != label or label not in spec]
        covered = all(state.ACTIONS.get(name) for name, _ in rows) and all(
            state.MUST_NOT_IMPLY.get(name) for name, _ in rows
        )
        check("A2 §4.4 产品状态表",
              "八行状态的中文名同时出现在规范与实现里，且每行都有「可做的事」与「不得暗示」",
              f"状态 {len(state.PRODUCT_STATES)} 项、场景级 {list(state.SCENE_STATES)}；缺 {miss or '（无）'}；"
              f"动作/禁暗示齐全={covered}",
              "PASS" if not miss and covered else "FAIL",
              evidence=f"返回结果闭集 {list(state.RESULT_STATUSES)} 与 §3.5 一致",
              code_ref="isekai_core/story/state.py")

    if only_matches("A3 §3.4 六类主类别"):
        wanted = ["联络分享", "近况询问", "旧话题追问", "请求改变世界", "版本操作", "TRPG 行动"]
        miss = [label for label in wanted if label not in spec or label not in classify.LABELS.values()]
        check("A3 §3.4 六类主类别",
              "六类主类别与规范表格一一对应，且三类结构性请求给出转交目标",
              f"闭集 {len(classify.CATEGORIES)} 类；缺 {miss or '（无）'}；转交目标 {classify.HANDOFF_TARGETS}",
              "PASS" if not miss and len(classify.HANDOFF_TARGETS) == 3 else "FAIL",
              evidence="分类只影响路由：HANDOFF_NOTICES 说明「普通对话没有执行它」",
              code_ref="isekai_core/story/classify.py")

    if only_matches("A4 §6.2 黑箱禁词"):
        wanted = ["truth", "canon", "claim", "cognition", "memory", "score", "prompt", "audit",
                  "candidate", "narrative"]
        miss = [token for token in wanted if token not in view.INTERNAL_TOKENS]
        bad = view.internals_in({"truth_layer": 1, "memory_row": {}, "narrative_unit": 2})
        check("A4 §6.2 黑箱禁词",
              "实情层 / 事件表 / 记忆表 / 认知条目 / 评分 / 候选 / 审计 / 提示词都在禁词表里，且自检有效",
              f"禁词 {len(view.INTERNAL_TOKENS)} 项；缺 {miss or '（无）'}；负例命中 {len(bad)} 条",
              "PASS" if not miss and len(bad) == 3 else "FAIL",
              evidence="internals_in() 只查键不查正文：产品面带出内部字段名即算越界",
              code_ref="isekai_core/story/view.py")

    if only_matches("A5 §3.3 不写世界"):
        writers = []
        for path in sorted((ROOT / "isekai_core/story").glob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for token in ("apply_runtime_batch", "change_commit", "inbound_put(", "outbound_put(",
                          "effect_add", "claim_put(", "knowledge_put("):
                if token in text:
                    writers.append(f"{path.name}:{token}")
        check("A5 §3.3 不写世界",
              "故事层本体不写世界事实、不直接写消息、不旁路提交",
              f"命中 {writers or '（无）'}",
              "PASS" if not writers else "FAIL",
              evidence="版本操作也走运行层语义（runtime.fork / rollback），不自己复制或修补状态",
              code_ref="isekai_core/story/*.py")

    if only_matches("A6 管理面与 CLI 注册"):
        registered = set(world_ops.SYNC_OPS) | set(world_ops.ASYNC_OPS)
        story_ops = sorted(op for op in registered if op.startswith("story."))
        cli = sorted(name for (group, _cmd), name in OP_BY_COMMAND.items() if group == "story")
        missing_cli = [op for op in story_ops if op not in cli]
        check("A6 管理面与 CLI 注册",
              "story.* 既在管理面注册表里，也有同名 CLI 命令",
              f"管理面 {story_ops}；CLI {cli}；缺 {missing_cli or '（无）'}",
              "PASS" if story_ops and not missing_cli else "FAIL",
              evidence="注册表与 CLI 表都是显式列名：新增 op 必须同时进两处",
              code_ref="isekai_core/world/ops.py、isekai_core/world_cli.py")

    if only_matches("A7 §五 表达契约三维"):
        block = expression.turn_block(deferred_topics=["那封信"], followup=True)
        dims = {"来源": "亲历", "时间": "还没发生", "意愿": "先不说"}
        miss = [name for name, word in dims.items() if word not in block]
        bound = "被再问一遍也不多给" in block
        check("A7 §五 表达契约三维",
              "来源 / 时间 / 意愿三维都在契约行里；追问轮带「不扩权」边界行",
              f"缺 {miss or '（无）'}；边界行={bound}",
              "PASS" if not miss and bound else "FAIL",
              evidence="契约只约束怎么说，不给材料；材料仍由会话核心按合法视图给",
              code_ref="isekai_core/story/expression.py")


# ------------------------------------------------------------------ B 段：§十二 十二行


async def acceptance_rows() -> None:
    # 一、首次创作与第一次联络
    if only_matches("B1 首次创作与第一次联络"):
        async with core() as h:
            started = await h.mgmt.call("story.enter")
            pending = await h.mgmt.call("story.turn", instance_id="in-none", timeline_id="tl-none",
                                        character_id="cc-x")
            (Path(h.cfg.paths.packages)).mkdir(parents=True, exist_ok=True)
            (Path(h.cfg.paths.packages) / "灰潮纪.json").write_text(
                json.dumps(example_package("灰潮纪"), ensure_ascii=False), encoding="utf-8"
            )
            after = await h.mgmt.call("story.enter")
            done = {step["key"]: step["done"] for step in after["steps"]}
            labels = [step["label"] for step in after["steps"]]
            ok = (
                started["product_state"] == "preparing"
                and pending["product_state"] == "preparing" and pending["status"] == "waiting"
                and not h.fake.calls
                and done["describe"] and not done["create"]
                and view.internals_in(after) == []
                and labels[3] == "创建锁定实例" and labels[5] == "进入会话"
            )
            check("B1 首次创作与第一次联络",
                  "没就绪时停在准备状态、不生成假回复；有世界包后按产品语言推进",
                  f"初始 {started['product_state']}；未就绪轮 {pending['status']}/生成调用 {len(h.fake.calls)}；"
                  f"世界包就绪 {done}；步骤 {labels[:3]}…",
                  "PASS" if ok else "FAIL",
                  evidence="§4.1 六步只描述用户要做的事；失败停在准备 / 修订，不生成假角色回复",
                  code_ref="story.enter / story.turn")

    # 二、普通分享
    if only_matches("B2 普通分享"):
        async with core(replies=["嗯，听着就累。"]) as h:
            client, bound, instance_id, timeline_id, cards = await room(h)
            try:
                before = counts(h, instance_id, timeline_id)
                ref = await say(client, bound["thread"], "我今天加班到很晚，有点累")
                await client.expect(lambda env: env.type == "reply")
                after = counts(h, instance_id, timeline_id)
                row = h.store.inbound_find(str(bound["thread"]["channel_id"]), "dm-1", ref)
                turn = await h.mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                         character_id=cards[0])
                ok = (before == after and row["state"] == "done" and row["text"] == "我今天加班到很晚，有点累"
                      and turn["status"] == "expressed" and turn["delivery"]["state"] == "sent"
                      and turn["delivery"]["delivered"] is False)
                check("B2 普通分享",
                      "入站固化为她收到的联络内容；世界事件 / 效果 / 说法条数不变",
                      f"事实 {before} → {after}；入站 state={row['state']}；产品 {turn['status']}；"
                      f"投递 {turn['delivery']['state']}（送达={turn['delivery']['delivered']}）",
                      "PASS" if ok else "FAIL",
                      evidence="分类走判断点通道；普通分享不调用 change.commit",
                      code_ref="session._story_gate / story.turn")
            finally:
                await client.close()

    # 三、近况询问
    if only_matches("B3 近况询问"):
        async with core(replies=["水位尺还是老样子。"]) as h:
            h.fake.judgements["输入分类"] = '{"category": "status_inquiry", "why": "问她近况"}'
            client, bound, instance_id, timeline_id, cards = await room(h)
            try:
                await say(client, bound["thread"], "你最近怎么样？")
                await client.expect(lambda env: env.type == "reply")
                prompt = str((h.fake.calls[-1][0] or {}).get("content") or "")
                scope = await h.mgmt.call("runtime.scope.inspect", instance_id=instance_id,
                                          timeline_id=timeline_id)
                turn = await h.mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                         character_id=cards[0])
                unknown = "有碑刻提到崩堤当夜曾有人登堤敲钟。"
                creator = "她父亲的旧账本里有那份告警的一页抄件"
                ok = (unknown not in prompt and creator not in prompt and "天罚" in prompt
                      and "她这次说话的表达契约" in prompt
                      and turn["observed_revision"] == scope["processed_watermark"])
                check("B3 近况询问",
                      "回复只来自已完成水位与角色合法视图；未获知说法 / 创作者背景不进提示词",
                      f"未获知说法在提示词里={'否' if unknown not in prompt else '是'}；"
                      f"创作者背景={'否' if creator not in prompt else '是'}；已获知说法={'在' if '天罚' in prompt else '缺'}；"
                      f"水位 {turn['observed_revision']}=={scope['processed_watermark']}",
                      "PASS" if ok else "FAIL",
                      evidence="认知投影是唯一底层入口；本层只加表达契约，不搬材料",
                      code_ref="runtime.cognition_project / story.expression_block")
            finally:
                await client.close()

    # 四、同一话题重复追问
    if only_matches("B4 重复追问"):
        async with core(replies=["……嗯。", "还是那句话。", "不想说这个。"]) as h:
            h.fake.judgements["输入分类"] = FOLLOWUP
            client, bound, instance_id, timeline_id, cards = await room(h)
            try:
                h.store.narrative_unit_put({
                    "instance_id": instance_id, "timeline_id": timeline_id, "character_id": cards[0],
                    "id": "nu-deferred-1", "primary_ref": "xp-1", "refs": json.dumps(["xp-1"]),
                    "entry": "experience", "relation": "并列", "topic": "那封没有署名的信",
                    "stage": "deferred", "message_id": "", "world_day": 1500,
                    "created_world": DAY * 1500, "updated_world": DAY * 1500,
                })
                boundary_hits = 0
                for index in range(3):
                    await say(client, bound["thread"], f"上次说的那封信后来呢（第 {index + 1} 次）")
                    await client.expect(lambda env: env.type == "reply")
                    prompt = str((h.fake.calls[-1][0] or {}).get("content") or "")
                    if "她之前没讲出口的事（界限不变）：" in prompt and "被再问一遍也不多给" in prompt:
                        boundary_hits += 1
                unit = [row for row in h.store.narrative_unit_list(instance_id, timeline_id,
                                                                   character_id=cards[0])
                        if str(row["id"]) == "nu-deferred-1"][0]
                check("B4 重复追问",
                      "角色保持既有暂缓 / 拒绝边界，不按追问次数扩大权限",
                      f"三次追问带边界行 {boundary_hits}/3；单元 stage 仍={unit['stage']}、message_id={unit['message_id'] or '（空）'}",
                      "PASS" if boundary_hits == 3 and unit["stage"] == "deferred" and not unit["message_id"] else "FAIL",
                      evidence="边界取她的「没讲出口」单元；材料访问权不因追问次数变化",
                      code_ref="story.expression.unit_topics / session._story_contract")
            finally:
                await client.close()

    # 五、请求改变世界
    if only_matches("B5 请求改变世界"):
        async with core(replies=["（她不该在这一轮说话）"]) as h:
            client, bound, instance_id, timeline_id, cards = await room(h)
            try:
                before = counts(h, instance_id, timeline_id)
                ref = await say(client, bound["thread"], "帮我把世界设定改成终年下雪")
                notice = await client.expect(lambda env: env.type == "system_notice")
                row = h.store.inbound_find(str(bound["thread"]["channel_id"]), "dm-1", ref)
                turn = await h.mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                         character_id=cards[0])
                changes = [{"id": "c-1", "kind": "condition", "operation": "set", "certainty": "confirmed",
                            "target_refs": [cards[0]], "value": "封堤", "expiry": "until_cleared"}]
                preview = await h.mgmt.call("runtime.change.preview", instance_id=instance_id,
                                            timeline_id=timeline_id, changes=changes)
                mid = counts(h, instance_id, timeline_id)
                committed = await h.mgmt.call("runtime.change.commit", instance_id=instance_id,
                                              timeline_id=timeline_id, changes=changes, idempotency_key="oc-audit-1",
                                              preview_id=preview["preview_id"], source_module="oc_story")
                after = counts(h, instance_id, timeline_id)
                ok = (turn["status"] == "handoff" and row["error_code"] == "handoff:creation"
                      and before == mid and after["events"] == before["events"] + 1 and not h.fake.calls
                      and "没有执行" in notice.payload["text"])
                check("B5 请求改变世界",
                      "普通轮次返回 handoff 或澄清；只有显式 preview → commit 成功后才出现世界变化",
                      f"产品 {turn['status']}（{turn.get('handoff')}）；入站 {row['error_code']}；"
                      f"事实 {before['events']} →（预览后 {mid['events']}）→ {after['events']}；生成调用 {len(h.fake.calls)}",
                      "PASS" if ok else "FAIL",
                      evidence="转交以 system_notice 说明，不冒充她的回复；世界变化只走显式路径",
                      code_ref="session._story_handoff / runtime.change.commit")
            finally:
                await client.close()

    # 六、TRPG 行动
    if only_matches("B6 TRPG 行动"):
        async with core(replies=["（她不该替玩家掷骰）"]) as h:
            client, bound, instance_id, timeline_id, cards = await room(h)
            try:
                before = counts(h, instance_id, timeline_id)
                ref = await say(client, bound["thread"], "我先攻，掷骰攻击那个守卫")
                notice = await client.expect(lambda env: env.type == "system_notice")
                row = h.store.inbound_find(str(bound["thread"]["channel_id"]), "dm-1", ref)
                turn = await h.mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                         character_id=cards[0])
                page = h.store.history_page(bound["session"]["id"], limit=10)
                her = [item for item in page["messages"] if item["role"] == "character"]
                ok = (turn["handoff"] == "trpg" and row["error_code"] == "handoff:trpg" and not her
                      and before == counts(h, instance_id, timeline_id) and not h.fake.calls
                      and "掷骰" in notice.payload["text"])
                check("B6 TRPG 行动",
                      "转入 TRPG 入口，不在 OC 会话里生成骰点 / 成功 / 失败结论",
                      f"产品 {turn['status']}（{turn.get('handoff')}）；角色发言 {len(her)} 条；生成调用 {len(h.fake.calls)}",
                      "PASS" if ok else "FAIL",
                      evidence="OC 层不猜骰点、不生成行动结果：行动必须走 TRPG 规则层",
                      code_ref="story.classify.HANDOFF_TARGETS")
            finally:
                await client.close()

    # 七、离线回访
    if only_matches("B7 离线回访"):
        async with core(replies=["刚巡堤回来。"]) as h:
            client, bound, instance_id, timeline_id, cards = await room(h, activate=False)
            try:
                h.world.activate(instance_id, timeline_id, now_real=time.time(), rate=3600)
                await asyncio.sleep(1.05)
                scene = await h.mgmt.call("story.scene", instance_id=instance_id, timeline_id=timeline_id,
                                          character_id=cards[0])
                home = await h.mgmt.call("story.home", instance_id=instance_id, timeline_id=timeline_id,
                                         character_id=cards[0])
                await say(client, bound["thread"], "在吗")
                await client.expect(lambda env: env.type == "reply")
                turn = await h.mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                         character_id=cards[0])
                ok = (scene["product_state"] == "catching_up"
                      and home["time"]["world_seconds"] == scene["processed_watermark"]
                      and home["time"]["world_seconds"] < home["time"]["target_watermark"]
                      and turn["world_time"] == scene["processed_watermark"]
                      and any("追赶" in note for note in home["notes"]))
                check("B7 离线回访",
                      "只使用已完成水位；追赶中不把目标水位冒充已发生",
                      f"场景 {scene['product_state']}；已完成 {home['time']['world_seconds']} < 目标 "
                      f"{home['time']['target_watermark']}；回复水位 {turn['world_time']}",
                      "PASS" if ok else "FAIL",
                      evidence="目标时刻只作「还在追赶」的指标，不当事实；错过的主动表达不批量补发（既有链路）",
                      code_ref="story.scene / story.home / story.turn")
            finally:
                await client.close()

    # 八、叙事生成失败
    if only_matches("B8 叙事生成失败"):
        async with core(fail_with=LLMError("llm_unavailable", "boom")) as h:
            client, bound, instance_id, timeline_id, cards = await room(h)
            try:
                far = 10**15
                before = counts(h, instance_id, timeline_id)
                experiences = len(h.store.experience_window(instance_id, timeline_id, cards[0], until=far))
                consumed = h.store.narrative_consumed_refs(instance_id, timeline_id, cards[0])
                ref = await say(client, bound["thread"], "我今天把账本翻了一遍")
                await client.expect(lambda env: env.type == "error")
                row = h.store.inbound_find(str(bound["thread"]["channel_id"]), "dm-1", ref)
                page = h.store.history_page(bound["session"]["id"], limit=20)
                her = [item for item in page["messages"] if item["role"] == "character"]
                turn = await h.mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                         character_id=cards[0])
                ok = (row["state"] == "failed" and not her and before == counts(h, instance_id, timeline_id)
                      and len(h.store.experience_window(instance_id, timeline_id, cards[0], until=far)) == experiences
                      and h.store.narrative_consumed_refs(instance_id, timeline_id, cards[0]) == consumed
                      and turn["status"] == "deferred" and turn["error_kind"] == "model")
                check("B8 叙事生成失败",
                      "经历与获知保留；回复降级为暂缓 / 保守表达；不重复消费素材",
                      f"入站 {row['state']}；角色发言 {len(her)} 条；事实 {before}；"
                      f"经历 {experiences} 条不变；消费引用不变={h.store.narrative_consumed_refs(instance_id, timeline_id, cards[0]) == consumed}；"
                      f"产品 {turn['status']}（{turn.get('error_kind')}）",
                      "PASS" if ok else "FAIL",
                      evidence="失败类别（模型 / 世界 / 存储 / 通道 / 版本）分别翻译成产品状态",
                      code_ref="story.state.error_kind / session._generate")
            finally:
                await client.close()

    # 九、投递失败 / unknown
    if only_matches("B9 投递状态"):
        async with core(replies=["知道了。", "路上小心。"]) as h:
            client, bound, instance_id, timeline_id, cards = await room(h)
            try:
                ref = await say(client, bound["thread"], "我出门了")
                first = await client.expect(lambda env: env.type == "reply")
                await client.report_delivery(thread_id=str(bound["thread"]["thread_id"]),
                                             binding_token=str(bound["thread"]["binding_token"]),
                                             message_id=first.payload["message_id"], batch_index=0, state="accepted")
                delivered = await h.mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                              character_id=cards[0])
                await say(client, bound["thread"], "晚点再说")
                second = await client.expect(lambda env: env.type == "reply")
                fixed = h.store.outbound_by_message_id(second.payload["message_id"])
                h.store.delivery_set(int(fixed["seq"]), 0, "unknown")
                unknown = await h.mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                            character_id=cards[0])
                her = [item for item in h.store.history_page(bound["session"]["id"], limit=20)["messages"]
                       if item["role"] == "character"]
                ok = (delivered["delivery"]["state"] == "delivered" and delivered["delivery"]["delivered"] is True
                      and unknown["delivery"]["state"] == "unknown" and unknown["delivery"]["delivered"] is False
                      and len(her) == 2 and len(h.fake.calls) == 2)
                check("B9 投递状态",
                      "固化状态与投递状态分开；未确认送达不算成功，也不重新生成同一回复",
                      f"确认后 {delivered['delivery']['state']}（送达={delivered['delivery']['delivered']}）→ "
                      f"未知 {unknown['delivery']['state']}（送达={unknown['delivery']['delivered']}）；"
                      f"固化回复 {len(her)} 条 / 生成 {len(h.fake.calls)} 次",
                      "PASS" if ok else "FAIL",
                      evidence="expressed 只说明已固化；总条数不变 = 没有重新生成",
                      code_ref="story.state.delivery_extra")
            finally:
                await client.close()

    # 十、同世界不同角色
    if only_matches("B10 同世界不同角色"):
        async with core(replies=["记下了。", "我也听说了。"]) as h:
            first, bound_a, instance_id, timeline_id, cards = await room(h, names=("堤禾", "芦生"))
            issued = await h.mgmt.call("channel.ensure", name="second", capabilities={})
            session = (await h.mgmt.call("session.ensure", instance_id=instance_id, timeline_id=timeline_id,
                                         character_id=cards[1]))["session"]
            bound = (await h.mgmt.call("thread.bind", channel="second", thread_id="dm-2",
                                       session_id=session["id"]))["thread"]
            second = UmpClient(endpoint=h.endpoint, channel_id="second", name="second",
                               credential=issued["credential"])
            await second.connect()
            bound_b = {"thread": bound, "session": session}
            secret = "我在盐滩捡到了一枚刻字的铜扣"
            try:
                await say(first, bound_a["thread"], secret)
                await first.expect(lambda env: env.type == "reply")
                talk_a = "\n".join(str(item.get("content") or "") for item in h.fake.calls[-1])
                await say(second, bound_b["thread"], "今天风大")
                await second.expect(lambda env: env.type == "reply")
                talk_b = "\n".join(str(item.get("content") or "") for item in h.fake.calls[-1])
                home_a = await h.mgmt.call("story.home", instance_id=instance_id, timeline_id=timeline_id,
                                           character_id=cards[0])
                home_b = await h.mgmt.call("story.home", instance_id=instance_id, timeline_id=timeline_id,
                                           character_id=cards[1])
                ok = (secret in talk_a and secret not in talk_b and "今天风大" in talk_b and "今天风大" not in talk_a
                      and home_a["session"]["id"] != home_b["session"]["id"]
                      and [item["text"] for item in home_b["messages"]] != [item["text"] for item in home_a["messages"]])
                check("B10 同世界不同角色",
                      "相同世界可产生不同合法表达；角色之间不自动共享对话与上下文",
                      f"A 的输入含对方原话={'是' if secret in talk_a else '否'}；B 含={'是' if secret in talk_b else '否'}；"
                      f"会话 {'不同' if home_a['session']['id'] != home_b['session']['id'] else '相同'}",
                      "PASS" if ok else "FAIL",
                      evidence="默认隔离；用户主动披露仍走既有授权流程（disclose.confirm）",
                      code_ref="story.home / runtime.cognition_project")
            finally:
                await first.close()
                await second.close()

    # 十一、分支与恢复
    if only_matches("B11 分支与恢复"):
        async with core(replies=["嗯。", "又是一天。"]) as h:
            client, bound, instance_id, timeline_id, cards = await room(h)
            try:
                await say(client, bound["thread"], "今天先到这儿")
                await client.expect(lambda env: env.type == "reply")
                mark = await h.mgmt.call("runtime.commit", instance_id=instance_id, timeline_id=timeline_id,
                                          note="分支点")
                commit_id = mark["commit"]["id"]
                point = int(h.store.commit_snapshot_get(commit_id)["world"])
                h.world.consume_time(instance_id, timeline_id, seconds=3600, cause="日常推进",
                                     now_real=time.time(), max_batches=8)
                line_world = int(h.store.clock_get(timeline_id)["processed_world"])
                branched = await h.mgmt.call("story.branch", instance_id=instance_id, timeline_id=timeline_id,
                                             commit_id=commit_id, name="另一种可能")
                new_line = branched["timeline"]["id"]
                h.world.consume_time(instance_id, timeline_id, seconds=3600, cause="继续推进",
                                     now_real=time.time(), max_batches=8)
                branch_world = int(h.store.clock_get(new_line)["processed_world"])
                preview = await h.mgmt.call("story.restore", instance_id=instance_id, timeline_id=timeline_id,
                                            commit_id=commit_id)
                held = await h.mgmt.call("story.restore", instance_id=instance_id, timeline_id=timeline_id,
                                         commit_id=commit_id, confirm=True)
                page = h.store.history_page(bound["session"]["id"], limit=10)
                her = [item for item in page["messages"] if item["role"] == "character"][0]
                h.store.delivery_set(int(her["seq"]), 0, "accepted")
                done = await h.mgmt.call("story.restore", instance_id=instance_id, timeline_id=timeline_id,
                                         commit_id=commit_id, confirm=True, saved=True)
                ok = (new_line and branch_world == point and line_world > point
                      and preview["status"] == "waiting" and held["status"] == "waiting"
                      and {item["action"] for item in preview["save_paths"]} == {"branch", "export"}
                      and done["status"] == "ok" and "不保证" in done["warning"]
                      and int(h.store.clock_get(timeline_id)["processed_world"]) == point)
                check("B11 分支与恢复",
                      "分支后未来不回流；覆盖前有明确提示与保存路径；已投递内容不宣称可撤回",
                      f"分叉点 {point} → 原线 {line_world}（分支停在 {branch_world}）；"
                      f"预演 {preview['status']} / 未保存 {held['status']} / 执行 {done['status']}；"
                      f"已投递 {done.get('coverage', {}).get('delivered_replies', 0)} 条",
                      "PASS" if ok else "FAIL",
                      evidence="本层只编排：分叉与覆盖仍走运行层语义，不自己复制 / 合并 / 修补状态",
                      code_ref="story.branch / story.restore")
            finally:
                await client.close()

    # 十二、世界冻结 / 版本阻断
    if only_matches("B12 世界冻结与阻断"):
        async with core(replies=["（冻结线不该说话）"]) as h:
            client, bound, instance_id, timeline_id, cards = await room(h)
            try:
                h.world.freeze(instance_id, timeline_id)
                scene = await h.mgmt.call("story.scene", instance_id=instance_id, timeline_id=timeline_id,
                                          character_id=cards[0])
                home = await h.mgmt.call("story.home", instance_id=instance_id, timeline_id=timeline_id,
                                         character_id=cards[0])
                await client.send_user_message(thread_id=str(bound["thread"]["thread_id"]),
                                               binding_token=str(bound["thread"]["binding_token"]), text="在吗")
                error = await client.expect(lambda env: env.type == "error", timeout=10)
                turn = await h.mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                         character_id=cards[0])
                ok = (scene["product_state"] == "blocked" and scene["can"] == ["view_reason", "recover_or_export"]
                      and any("冻结" in note for note in home["notes"])
                      and error.payload["code"] == "state_blocked" and not h.fake.calls
                      and turn["status"] == "blocked"
                      and h.store.last_inbound(str(bound["session"]["id"])) is None)
                check("B12 世界冻结与阻断",
                      "只读或显示恢复入口；普通重试绕不过闸门（闸门在生成之前）",
                      f"场景 {scene['product_state']}（可做 {scene['can']}）；发送被拒 code={error.payload['code']}；"
                      f"生成调用 {len(h.fake.calls)}；入站未落库={h.store.last_inbound(str(bound['session']['id'])) is None}",
                      "PASS" if ok else "FAIL",
                      evidence="闸门位置：接收即拒（时间线冻结 / 兼容阻断 / 角色归档）",
                      code_ref="session.accept / story.scene")
            finally:
                await client.close()

    # 补充：输入分类闭集与转交目标
    if only_matches("B13 分类闭集"):
        async with core() as h:
            scripted = {"contact_share": "", "status_inquiry": "", "followup": "",
                        "world_change": "creation", "version_op": "version", "trpg_action": "trpg"}
            matched = 0
            for category, target in scripted.items():
                h.fake.judgements["输入分类"] = json.dumps({"category": category, "why": "脚本"},
                                                          ensure_ascii=False)
                verdict = await h.mgmt.call("story.classify", text="这是一句测试输入")
                if verdict["category"] == category and verdict["handoff"] == target:
                    matched += 1
            before = len(h.fake.judgement_calls)
            hit = await h.mgmt.call("story.classify", text="回滚到上一个存档")
            rule_saves = len(h.fake.judgement_calls) == before
            h.fake.judgements.clear()
            fallback = await h.mgmt.call("story.classify", text="嗯……")
            ok = (matched == 6 and hit["source"] == "rule" and rule_saves
                  and fallback["category"] == "contact_share" and fallback["source"] == "default")
            check("B13 分类闭集",
                  "六类都能返回；结构性请求走预筛省调用；分不清时按联络处理",
                  f"六类命中 {matched}/6；预筛命中 {hit['category']}（未花调用={rule_saves}）；"
                  f"兜底 {fallback['category']}（{fallback['source']}）",
                  "PASS" if ok else "FAIL",
                  evidence="§3.4：无法确定按联络内容处理并保留原文；分类不改变权限",
                  code_ref="story.classify.decide")

    # 补充：用户可见面黑箱
    if only_matches("B14 可见面黑箱"):
        async with core(replies=["嗯。"]) as h:
            client, bound, instance_id, timeline_id, cards = await room(h)
            try:
                await say(client, bound["thread"], "今天怎么样")
                await client.expect(lambda env: env.type == "reply")
                payloads = {
                    "home": await h.mgmt.call("story.home", instance_id=instance_id, timeline_id=timeline_id,
                                              character_id=cards[0]),
                    "turn": await h.mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                              character_id=cards[0]),
                    "scene": await h.mgmt.call("story.scene", instance_id=instance_id, timeline_id=timeline_id,
                                               character_id=cards[0]),
                }
                leaked = {name: view.internals_in(payload) for name, payload in payloads.items()}
                bad = {name: hits for name, hits in leaked.items() if hits}
                check("B14 可见面黑箱",
                      "产品面上不出现实情层 / 事件表 / 记忆表 / 评分 / 候选 / 提示词等内部字段",
                      f"三个面越界项 {bad or '（无）'}",
                      "PASS" if not bad else "FAIL",
                      evidence="白名单构造（不从底层行透传）：内部字段名出现即算越界",
                      code_ref="story.view.internals_in")
            finally:
                await client.close()


# ------------------------------------------------------------------ C 段：消费方事实


async def consumer_facts() -> None:
    if only_matches("C1 消费 WorldRuntime 对外接口"):
        src = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                        for path in sorted((ROOT / "isekai_core/story").glob("*.py")))
        used = [token for token in ("scope_inspect", "compatible(", "cards(", "clock_row", "reserve_call",
                                    "settle_call", "fork(", "rollback(")
                if token in src]
        missing = [token for token in ("scope_inspect", "fork(", "rollback(") if token not in src]
        check("C1 消费 WorldRuntime 对外接口",
              "故事层经 §四~§六 的接口读世界与做版本操作（不读内部表、不旁路提交）",
              f"用到的接口面 {used}；缺 {missing or '（无）'}",
              "PASS" if not missing else "FAIL",
              evidence="这是 `_audit2_iface.py` 唯一 DEFERRED（缺第二类真实调用方）的收口：故事层就是消费方",
              code_ref="isekai_core/story/service.py")

    if only_matches("C2 分类闸在生成之前"):
        src = (ROOT / "isekai_core/session.py").read_text(encoding="utf-8", errors="replace")
        gate = "_story_gate(batch)" in src and src.index("_story_gate(batch)") < src.index("await self._generate(batch[0]")
        contract = "_story_contract(rows)" in src
        check("C2 分类闸在生成之前",
              "会话链路里分类闸先于生成，表达契约随扮演定义进上下文",
              f"闸在生成前={gate}；表达契约接入={contract}",
              "PASS" if gate and contract else "FAIL",
              evidence="转交轮不生成、不入她的上下文（没被回应的请求不是联络内容）",
              code_ref="isekai_core/session.py _run / _build_messages")


def report() -> int:
    total = len(RESULTS)
    passed = sum(1 for item in RESULTS if item["status"] == "PASS")
    failed = sum(1 for item in RESULTS if item["status"] == "FAIL")
    deferred = sum(1 for item in RESULTS if item["status"] == "DEFERRED")
    print(f"\nTOTAL={total} PASS={passed} FAIL={failed} DEFERRED={deferred}")
    if failed:
        print("FAIL 明细：")
        for item in RESULTS:
            if item["status"] == "FAIL":
                print(f"  - {item['clause']}：{item['observed'][:200]}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    global ONLY
    parser = argparse.ArgumentParser(description="OC 故事层行为级审计探针")
    parser.add_argument("--only", default="", help="只跑包含该关键字的检查")
    parser.add_argument("--json", action="store_true", help="把读数写成 JSON")
    ns = parser.parse_args(argv)
    ONLY = ns.only

    static_facts()
    asyncio.run(acceptance_rows())
    asyncio.run(consumer_facts())
    code = report()

    audit_dir = ROOT / ".hermes/audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = audit_dir / f"ocstory_{stamp}{'_json' if ns.json else ''}.txt"
    payload = json.dumps(RESULTS, ensure_ascii=False, indent=2) if ns.json else "\n".join(
        f"[{item['status']:8}] {item['clause']} :: {item['observed']}\n        期望：{item['expected']}\n"
        f"        判据：{item['evidence']}（{item['code_ref']}）"
        for item in RESULTS
    )
    target.write_text(payload, encoding="utf-8")
    print(f"读数已存：{target.relative_to(ROOT).as_posix()}")
    return code


if __name__ == "__main__":
    sys.exit(main())
