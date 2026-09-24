"""跑团工作区（界面 U4 §8.1–§8.5）的真壳验收：临时数据根 + 真规则插件子进程。

跑法：.venv/Scripts/python.exe scripts/_probe_ui_u4.py
日志：scripts/_u4probe.log

看六件事：

  1) 战役列表与「本机规则」分组：规则插件的状态直接来自登记簿（规则 ≠ 通道插件）；
  2) 新建战役（名称 → 世界/时间线 → 规则 → 角色 → 场景）真的建成，名称与场景名跟着数据走；
  3) 玩家面：场景公开材料 / 未知 / 待处理都在，受众闸 0 违规；
  4) 行动：查看确认卡 → 确认并裁定 → 有真实裁定与世界后果，行动落库；
  5) 主持面：待处理行动 / 直接变化 / 场景准备都在，切回玩家视图后主持内容不再出现；
  6) 库里读得到：战役名、场景名、行动行、世界事件。

原生目录选择对话框驱动不了（历史结论），所以规则登记在探针里用核心路径预置；
界面侧验证的是「列表 / 挑选 / 状态分组」。
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import _audit2_desk as desk  # noqa: E402
import _ui_nav_common as nav  # noqa: E402

LOG = REPO / "scripts" / "_u4probe.log"
PLUGIN = REPO / "examples" / "tide_rules_plugin" / "manifest.json"
WORLD_NAME = "跑团世界"
CAMPAIGN_NAME = "北堤调查"
SCENE_NAME = "夜里的北堤"
INTENT = "沿着堤顶走近那段裂口，看清它到底有多深"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_true(cdp: desk.Cdp, expr: str, *, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await cdp.js(expr):
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.25)
    return False


async def click_text(cdp: desk.Cdp, selector: str, text: str, *, last: bool = False) -> bool:
    expr = (
        "(()=>{const nodes=[...document.querySelectorAll("
        + json.dumps(selector)
        + ")].filter(n=>n.offsetParent!==null||n.tagName==='OPTION');"
        + "const hits=nodes.filter(n=>(n.textContent||'').includes("
        + json.dumps(text)
        + "));if(!hits.length){return false;}"
        + ("hits[hits.length-1]" if last else "hits[0]")
        + ".click();return true;})()"
    )
    return bool(await cdp.js(expr))


async def set_value(cdp: desk.Cdp, selector: str, value: str) -> bool:
    expr = (
        "(()=>{const n=document.querySelector("
        + json.dumps(selector)
        + ");if(!n){return false;}n.value="
        + json.dumps(value)
        + ";n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}));return true;})()"
    )
    return bool(await cdp.js(expr))


async def visible_text(cdp: desk.Cdp, selector: str = "#u-main") -> str:
    value = await cdp.js(
        "(()=>{const n=document.querySelector(" + json.dumps(selector) + ");return n?(n.innerText||n.textContent||''):'';})()"
    )
    return str(value or "")


def seed(root: Path) -> dict:
    """真链路造世界 + 登记规则（规则登记簿是本机事实，探针直接落一次）。"""
    import time as _time

    from isekai_core import rules_registry
    from isekai_core.config import load_config
    from isekai_core.runtime.service import from_config
    from isekai_core.store import Store
    from isekai_core.world.example import example_card, example_package
    from isekai_core.world.instances import create_instance

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    world = from_config(cfg, store)
    package = example_package(WORLD_NAME)
    card = example_card(package, name="堤禾")
    info = create_instance(store, package, [card])
    timeline_id = str(store.timeline_list(info["id"])[0]["id"])
    world.ensure_instance(info["id"], now_real=_time.time())
    registered = rules_registry.register(store, manifest_path=PLUGIN)
    out = {
        "instance_id": str(info["id"]),
        "timeline_id": timeline_id,
        "card_id": str(card["meta"]["card_id"]),
        "rules": registered["plugin"]["ruleset_id"] + " " + registered["plugin"]["ruleset_version"],
    }
    store.close()
    return out


def inspect(root: Path) -> dict:
    from isekai_core.config import load_config
    from isekai_core.store import Store

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    try:
        instances = store.instance_list()
        out: dict = {"instances": [str(item["name"]) for item in instances]}
        if not instances:
            return out
        instance_id = str(instances[0]["id"])
        timelines = store.timeline_list(instance_id)
        out["timelines"] = [(str(item["id"]), str(item["name"]), str(item["state"])) for item in timelines]
        campaigns = store.trpg_list("campaign", instance_id=instance_id)
        out["campaigns"] = [
            {"name": str(row.get("name") or ""), "status": str(row.get("status") or ""),
             "ruleset": f"{row.get('ruleset_id')} {row.get('ruleset_version')}",
             "scene": str(row.get("current_scene_id") or "")}
            for row in campaigns
        ]
        scenes = store.trpg_list("scene", instance_id=instance_id)
        out["scenes"] = [{"name": str(row.get("name") or ""), "brief": str(row.get("brief") or "")} for row in scenes]
        actions = store.trpg_list("action", instance_id=instance_id)
        out["actions"] = [{"id": str(row.get("action_id") or ""), "status": str(row.get("status") or ""),
                           "actor": str(row.get("actor_id") or "")} for row in actions]
        timeline_id = str(timelines[0]["id"]) if timelines else ""
        out["events"] = len(store.event_ids(instance_id, timeline_id)) if timeline_id else 0
        out["plugin_rows"] = len(store.rule_plugin_list())
        return out
    finally:
        store.close()


async def main() -> None:
    root = desk.make_root("uiu4")
    info = seed(root)
    print("临时根:", root)
    print("播种:", json.dumps(info, ensure_ascii=False), flush=True)

    desk.kill_tree()
    await asyncio.sleep(1.0)
    port = free_port()
    print("调试端口:", port, flush=True)
    proc, cdp, _targets = await desk.boot_shell(
        root,
        port=port,
        env_extra={"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": "（跑团探针不需要模型）"},
    )
    problems: list[str] = []

    def say(tag: str, payload: object) -> None:
        print(f"{tag} {json.dumps(payload, ensure_ascii=False)}", flush=True)

    try:
        await cdp.js(desk.STUB)
        if not await wait_true(cdp, "!!(window.__uiApp && window.__uiApp.probeState.connected)"):
            raise SystemExit("止损：正式界面没连上核心")

        # ---------------- 1) 战役列表 + 本机规则 ----------------
        await nav.launch_app(cdp, "isekai GM")
        await asyncio.sleep(1.6)
        text = await visible_text(cdp)
        for need in ("继续已有战役", "新建战役", "从样例开始", "本机规则"):
            if need not in text:
                problems.append(f"跑团首屏缺：{need}")
        if "潮汐" not in text and info["rules"] not in text:
            problems.append(f"本机规则里没有登记过的规则：{text[:200]}")
        say("首屏", text[:140])

        # ---------------- 2) 新建战役 ----------------
        await click_text(cdp, "#u-main button", "新建战役")
        if not await wait_true(cdp, "!!document.querySelector('#u-trpg-name')", timeout=30):
            problems.append("新建向导没有打开")
        await set_value(cdp, "#u-trpg-name", CAMPAIGN_NAME)
        await set_value(cdp, "#u-trpg-instance", info["instance_id"])
        await set_value(cdp, "#u-trpg-timeline", info["timeline_id"])
        # 规则下拉的 value 是「清单绝对路径|id|版本」（登记簿给出的真路径），按第一项选最稳
        await cdp.js(
            "(()=>{const s=document.querySelector('#u-trpg-rule');if(!s||!s.options.length){return false;}"
            "s.value=s.options[0].value;s.dispatchEvent(new Event('change',{bubbles:true}));return s.value;})()"
        )
        await set_value(cdp, "#u-trpg-pc-" + info["card_id"], info["card_id"])
        await cdp.js(
            "(()=>{const b=document.querySelector('#u-trpg-pc-"
            + info["card_id"]
            + "');if(!b)return false;if(!b.checked){b.click();}return b.checked;})()"
        )
        await set_value(cdp, "#u-trpg-scene-name", SCENE_NAME)
        await set_value(cdp, "#u-trpg-scene-brief", "潮声比白天更近，堤顶能听见水下的石头在动")
        await set_value(cdp, "#u-trpg-scene-location", "rl-1")
        wizard = await visible_text(cdp)
        for need in ("战役名称", "时间线", "规则与版本", "开场场景", "创建摘要"):
            if need not in wizard:
                problems.append(f"新建向导缺：{need}")
        summary = await cdp.js("(()=>{const n=document.querySelector('#u-tprg-summary')||document.querySelector('#u-trpg-summary');return n?(n.innerText||''):'';})()")
        if CAMPAIGN_NAME not in str(summary) or SCENE_NAME not in str(summary):
            problems.append(f"创建摘要里没有名称化显示：{summary}")
        diag = await cdp.js(
            "(()=>{const s=document.querySelector('#u-trpg-rule');"
            "const box=[...document.querySelectorAll('.u-field')].find(f=>(f.textContent||'').includes('规则与版本'));"
            "const sum=[...document.querySelectorAll('.u-section')].find(x=>(x.textContent||'').includes('创建摘要'));"
            "return JSON.stringify({规则下拉选项:s?s.options.length:-1,规则下拉当前值:s?s.value:'',"
            "这一栏显示:box?box.innerText.slice(0,80):'',摘要:sum?sum.innerText.slice(0,200):'（没有摘要分区）',"
            "勾选的参与者:document.querySelectorAll('#u-main input[type=checkbox]:checked').length});})()"
        )
        print("· 诊断:", diag, flush=True)
        await click_text(cdp, "#u-main button", "开始战役")
        # 记时读数：既等回执也等局面页，卡在哪一步看得见（别再拿「失败」当结论）
        began = time.time()
        started = False
        trace: list[str] = []
        while time.time() - began < 300:
            note = str(await cdp.js("(()=>{const n=document.querySelector('#u-main .u-note');return n?(n.innerText||'').slice(0,80):'';})()") or "")
            page = str(await cdp.js("(()=>{const n=document.querySelector('#u-main');return n?(n.innerText||''):'';})()") or "")
            if "当前场景" in page:
                started = True
                break
            if len(trace) < 6:
                trace.append(f"{int(time.time() - began)}s:{note[:40]}")
            await asyncio.sleep(2.0)
        if not started:
            note_text = await cdp.js("(()=>{const n=document.querySelector('#u-main .u-note');return n?(n.innerText||'').slice(0,160):'';})()")
            problems.append(f"开始战役之后没有进到进行中页面：note={note_text}｜轨迹={trace}｜页面尾：{(await visible_text(cdp))[-160:]}")
        state = inspect(root)
        if not state["campaigns"] or state["campaigns"][0]["name"] != CAMPAIGN_NAME:
            problems.append(f"库里没有这次战役 / 名称没落库：{state['campaigns']}")
        if not state["scenes"] or state["scenes"][0]["name"] != SCENE_NAME:
            problems.append(f"场景名没落库：{state['scenes']}")
        say("新建", {"战役": state["campaigns"], "场景": state["scenes"], "时间线": state["timelines"]})

        # ---------------- 3) 玩家面 ----------------
        play = await visible_text(cdp)
        for need in ("当前场景", "待处理", "过去结果", "我想……"):
            if need not in play:
                problems.append(f"进行中页面缺：{need}")
        if SCENE_NAME not in play:
            problems.append("场景名没有显示出来（不该拿内部标识当标题）")
        if "这一面有" in play and "不该出现的内容" in play:
            problems.append("玩家面受众闸报了违规内容")
        # 行动流程轨（图形化）：四个阶段画成一条，刚开始时应停在「写下行动」
        rail_expr = (
            "(()=>{const r=document.querySelector('#u-main .u-rail');if(!r){return null;}"
            "return {steps:[...r.querySelectorAll('.u-rail-label')].map(x=>x.textContent),"
            "current:(r.querySelector('.u-rail-current .u-rail-label')||{}).textContent||''};})()"
        )
        rail = await cdp.js(rail_expr)
        if not rail or len(rail.get("steps") or []) != 4:
            problems.append(f"玩家面没有行动流程轨：{rail}")
        elif rail.get("current") != "写下行动":
            problems.append(f"行动流程轨的当前位置不对：{rail}")
        # 截图（本地证据）：流程轨与页面一起看，别只读文本
        try:
            raw = await cdp.call("Page.captureScreenshot", format="png")
            shots = REPO / ".hermes" / "audits" / "ui_graphics"
            shots.mkdir(parents=True, exist_ok=True)
            (shots / "06_trpg_play.png").write_bytes(base64.b64decode(raw["data"]))
        except Exception as exc:  # noqa: BLE001
            print(f"截图失败：{type(exc).__name__}: {exc}", flush=True)
        say("玩家面", play[:180])

        # ---------------- 4) 行动：确认卡 → 确认并裁定 ----------------
        before = inspect(root)
        await set_value(cdp, "#u-trpg-intent", INTENT)
        await cdp.js(
            "(()=>{const s=document.querySelector('#u-trpg-actor');if(!s||!s.options.length){return false;}"
            "s.value=s.options[0].value;s.dispatchEvent(new Event('change',{bubbles:true}));return s.value;})()"
        )
        await set_value(cdp, "#u-trpg-target", "rl-1")
        await set_value(cdp, "#u-trpg-method", "沿堤顶走过去，看水深")
        await click_text(cdp, "#u-main button", "查看行动确认卡")
        carded = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('行动确认卡已就绪')", timeout=120)
        if not carded:
            card_text = await cdp.js(
                "(()=>{const s=[...document.querySelectorAll('.u-section')].find(x=>(x.textContent||'').includes('行动确认卡'));"
                "return s?(s.innerText||'').slice(0,240):'（没有确认卡分区）';})()"
            )
            problems.append(f"没有拿到行动确认卡：卡面={card_text}")
        # 页面会重画，行动表单要重新填一遍（人也会这么干）
        await set_value(cdp, "#u-trpg-intent", INTENT)
        await set_value(cdp, "#u-trpg-target", "rl-1")
        await set_value(cdp, "#u-trpg-method", "沿堤顶走过去，看水深")
        await cdp.js(
            "(()=>{const s=document.querySelector('#u-trpg-actor');if(!s||!s.options.length){return false;}"
            "s.value=s.options[0].value;s.dispatchEvent(new Event('change',{bubbles:true}));return s.value;})()"
        )
        await click_text(cdp, "#u-main button", "确认并裁定")
        resolved = await wait_true(
            cdp,
            "(()=>{const t=document.querySelector('#u-main .u-note')?.innerText||document.querySelector('#u-main').innerText;"
            "return t.includes('已固化');})()",
            timeout=180,
        )
        await asyncio.sleep(2.0)
        after = inspect(root)
        rail_done = await cdp.js(rail_expr)
        if not rail_done or rail_done.get("current") != "写入世界":
            problems.append(f"裁定提交之后流程轨没有走到「写入世界」：{rail_done}")
        if not after["actions"]:
            problems.append(f"行动没有落库：{after['actions']}")
        if not resolved:
            problems.append(f"确认并裁定之后界面没有阶段读数：{(await visible_text(cdp))[-200:]}")
        if after["events"] <= before["events"]:
            problems.append(f"世界没有真实变化（事件 {before['events']} → {after['events']}）")
        say("行动", {"行动行": after["actions"], "事件": [before["events"], after["events"]]})

        # ---------------- 5) 主持面 ----------------
        await click_text(cdp, "#u-main button", "主持准备")
        gm = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('视角：主持')", timeout=90)
        if not gm:
            # 第一次切换撞上核心忙段时再点一次（用户也会这么干）
            await click_text(cdp, "#u-main button", "主持准备")
            gm = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('视角：主持')", timeout=90)
        gm_text = await visible_text(cdp)
        for need in ("待处理行动", "直接变化", "场景准备", "暂停战役"):
            if need not in gm_text:
                problems.append(f"主持面缺：{need}")
        say("主持面", gm_text[:170])
        await click_text(cdp, "#u-main button", "主持准备")
        await asyncio.sleep(1.5)
        back = await visible_text(cdp)
        if "直接变化" in back or "待处理行动" in back:
            problems.append("切回玩家视图后主持内容还在（受众投影没有重取）")
        if "视角：主持" in back:
            problems.append("切回玩家视图之后还标着主持")
        say("切回", back[:120])

        final = inspect(root)
        say("终局", {"战役": final["campaigns"], "行动": final["actions"], "事件": final["events"],
                     "规则登记": final["plugin_rows"]})
    finally:
        try:
            await proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        desk.kill_tree()

    if problems:
        print("\n".join(f"[FAIL] {item}" for item in problems))
        print(f"结论：FAIL（{len(problems)} 项）")
    else:
        print("结论：PASS（战役列表与规则分组 / 新建战役 / 玩家面 / 行动与裁定 / 主持面与视角切换）")


if __name__ == "__main__":
    import contextlib

    with LOG.open("w", encoding="utf-8") as handle, contextlib.redirect_stdout(handle):
        asyncio.run(main())
    print(LOG.read_text(encoding="utf-8"))
