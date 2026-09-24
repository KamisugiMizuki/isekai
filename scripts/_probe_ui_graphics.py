"""图形化（把说不清的关系 / 比例 / 流程画出来）的真壳验收：临时数据根 + 假模型。

跑法：.venv/Scripts/python.exe scripts/_probe_ui_graphics.py
截图：.hermes/audits/ui_graphics/*.png（本地证据，不入库）

看五件事（都是「文字说不清、只有画出来才读得懂」的那几处）：

  1) 版本分叉图：两条时间线两条泳道，圆点数 = 提交数，分叉有虚线连回来源提交；
     点圆点能把下面那一行滚出来并高亮（图与操作面共用同一份数据）；
  2) 时间线速度：数字旁边有「现实 1 分钟 ≈ 世界 X」的人话换算，且跟着输入变；
  3) 大纲状态分布：概览一条（带图例）+ 每个分区一条细条，段数与真实状态分布一致；
  4) 用量：计量条是「已用 / 上限（百分比）」+ 人话任务名，页面上不出现内部键名；
  5) 本机检查：✓/✗ 逐项前缀（帮助与诊断与本机检查共用同一个组件）。

判据一律读**渲染后的几何与文本**：svh 存在但宽高为 0 不算画出来。
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

SHOTS = REPO / ".hermes" / "audits" / "ui_graphics"
WORLD_NAME = "图形化验收世界"
OUTLINE_NAME = "图形化验收大纲"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Stub:
    """不联网的生成器（只在播种线索时用一次）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def chat(self, prompt, **kwargs):  # noqa: ANN001, ANN003
        return self.reply


async def seed(root: Path) -> dict:
    """真链路造数据：一条主线 + 一条从其提交分出的分支 + 一份绑好的大纲 + 两条线索。"""
    from isekai_core.config import load_config
    from isekai_core.runtime import life as life_mod
    from isekai_core.runtime.service import from_config
    from isekai_core.store import Store
    from isekai_core.world.example import example_card, example_package
    from isekai_core.world.instances import create_instance
    from isekai_core.writing.service import WritingService

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    world = from_config(cfg, store)
    package = example_package(WORLD_NAME)
    first = example_card(package, name="堤禾")
    second = example_card(package, name="潮生")
    info = create_instance(store, package, [first, second])
    instance_id = str(info["id"])
    timeline_id = str(store.timeline_list(instance_id)[0]["id"])
    card_id = str(first["meta"]["card_id"])
    world.ensure_instance(instance_id, now_real=time.time())
    world.activate(instance_id, timeline_id, now_real=time.time())

    def commit_id(row: dict) -> str:
        return str((row.get("commit") or row or {}).get("id") or "")

    first_commit = commit_id(world.commit(instance_id, timeline_id, note="起点"))
    world.advance(instance_id, timeline_id, max_batches=1, now_real=time.time())
    second_commit = commit_id(world.commit(instance_id, timeline_id, note="第二版"))
    branched = world.fork(instance_id, timeline_id, commit_id=first_commit, name="分支：试演")
    branch_id = str((branched.get("timeline") or branched or {}).get("id") or "")
    if branch_id:
        world.commit(instance_id, branch_id, note="分支上的一版")

    # 一份已经绑到线上的大纲：条目铺开成不同状态，比例条才有东西可画
    wa = WritingService(store=store, cfg=cfg, runtime=world)
    outline = {
        "id": "ol-graphics",
        "name": OUTLINE_NAME,
        "items": [
            {"id": "it-theme", "layer": "theme", "title": "主题约束", "statement": "记住一件没人愿意记的事", "scope": "world", "success_criteria": "读的人能说出这件事被谁记住了"},
            {"id": "it-node-1", "layer": "required_node", "title": "告警", "statement": "她在本章结束前知道那份告警", "scope": "timeline", "success_criteria": "她的行动里出现对告警的回应"},
            {"id": "it-node-2", "layer": "required_node", "title": "账页", "statement": "盐账上有她的名字", "scope": "timeline", "success_criteria": "账页被写进故事"},
            {"id": "it-forbid", "layer": "forbidden", "title": "北堤", "statement": "北堤不得再次崩塌", "scope": "timeline", "success_criteria": "没有出现崩塌"},
            {"id": "it-arc", "layer": "character_arc", "title": "信任", "statement": "她从不信人到愿意托付", "scope": "timeline", "success_criteria": "她把一件要紧事交给别人"},
            {"id": "it-pace", "layer": "pacing", "title": "节奏", "statement": "前三章都在北堤附近", "scope": "timeline", "success_criteria": "场景地点都在北堤一带"},
            {"id": "it-var", "layer": "variable_material", "title": "素材", "statement": "可以用盐价、碑文、旧账本", "scope": "timeline", "success_criteria": "至少用到其中一样"},
        ],
    }
    wa.save_outline(outline)
    wa.bind(instance_id, timeline_id, outline_id="ol-graphics", observers=[card_id], chapter="第一章")
    wa.decide_item(
        instance_id, timeline_id, item_id="it-node-1", status="in_progress",
        reason="先让她听说告警", outline_id="ol-graphics",
    )
    wa.decide_item(
        instance_id, timeline_id, item_id="it-pace", status="achieved",
        reason="三章都在北堤", evidence_refs=[], outline_id="ol-graphics",
    )

    # 线索面板：一条讲出口（主动发言）一条没讲出口（后验拦下）
    world_s = int(world.clock_row(timeline_id)["processed_world"])
    store.knowledge_put(
        {
            "instance_id": instance_id, "timeline_id": timeline_id, "character_id": card_id,
            "id": "kn-gr-1", "world_seconds": world_s, "kind": "claim", "target": "cl-gr-1",
            "source": "src-1", "stance": "recorded", "text": "北堤的通行牌这三天都停发了",
        }
    )
    original = life_mod.activity_label
    life_mod.activity_label = lambda window: "日间活动"  # type: ignore[assignment]
    units = 0
    try:
        await world.proactive_tick(instance_id, timeline_id, llm=Stub("北堤的通行牌停发了，我听说的。"), per_day=2)
        store.knowledge_put(
            {
                "instance_id": instance_id, "timeline_id": timeline_id, "character_id": card_id,
                "id": "kn-gr-2", "world_seconds": world_s, "kind": "claim", "target": "cl-gr-2",
                "source": "src-2", "stance": "recorded", "text": "驿站新到一份灾年编年的补页",
            }
        )
        # 数字越界 → 后验拦下 → 只留「没讲出口」的记号
        await world.proactive_tick(instance_id, timeline_id, llm=Stub("驿站到了 7 份补页。"), per_day=2)
        units = len(store.narrative_unit_list(instance_id, timeline_id, character_id=card_id))
    except Exception as exc:  # noqa: BLE001 - 播种失败要如实记下，不当成产品缺陷
        print(f"线索播种失败：{type(exc).__name__}: {exc}", flush=True)
    finally:
        life_mod.activity_label = original  # type: ignore[assignment]

    commits = store.commit_list(instance_id)
    unit_rows = store.narrative_unit_list(instance_id, timeline_id, character_id=card_id)
    store.close()
    return {
        "instance_id": instance_id,
        "timeline_id": timeline_id,
        "branch_id": branch_id,
        "commits": len(commits),
        "units": len(unit_rows),
        "stages": [str(row["stage"]) for row in unit_rows],
    }


async def shot(cdp: desk.Cdp, name: str, focus: str = "") -> str:
    """截图（可选：先把某个元素滚进视野——量的是「用户看得见的那一屏」）"""
    if focus:
        await cdp.js(
            "(()=>{const n=document.querySelector("
            + json.dumps(focus)
            + ");if(n){n.scrollIntoView({block:'center'});}return !!n;})()"
        )
        await asyncio.sleep(0.6)
    SHOTS.mkdir(parents=True, exist_ok=True)
    raw = await cdp.call("Page.captureScreenshot", format="png")
    path = SHOTS / f"{name}.png"
    path.write_bytes(base64.b64decode(raw["data"]))
    return str(path)


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
        + ";n.dispatchEvent(new Event('input',{bubbles:true}));return true;})()"
    )
    return bool(await cdp.js(expr))


async def click_in_row(cdp: desk.Cdp, needle: str, label: str) -> bool:
    """在文字含 needle 的那一行里点 label 按钮（同名按钮在别处也有，别用「第一个」）"""
    expr = (
        "(()=>{const rows=[...document.querySelectorAll('#u-main tr, #u-main .u-row-line')];"
        "const row=rows.find(r=>(r.innerText||'').includes("
        + json.dumps(needle)
        + "));if(!row){return false;}const hit=[...row.querySelectorAll('button')].find(b=>(b.textContent||'').trim()==="
        + json.dumps(label)
        + ");if(!hit){return false;}hit.click();return true;})()"
    )
    return bool(await cdp.js(expr))


# 分支图：几何与计数一起读（svg 存在但宽高为 0 = 没画出来）
READ_BRANCH = """(()=>{const svg=document.querySelector('#u-main svg.u-branch');
const rect=svg?svg.getBoundingClientRect():null;
return {has:!!svg, w:rect?Math.round(rect.width):0, h:rect?Math.round(rect.height):0,
  dots:document.querySelectorAll('#u-main svg.u-branch circle').length,
  links:document.querySelectorAll('#u-main svg.u-branch path').length,
  lanes:[...document.querySelectorAll('#u-main svg.u-branch text.u-branch-label')].map(t=>t.textContent),
  rate:(document.getElementById('u-rate-note')||{}).textContent||'',
  rows:document.querySelectorAll('#u-main [data-commit-row]').length};})()"""


READ_WRITING = """(()=>{const bars=[...document.querySelectorAll('#u-main .u-bar-wrap')];
return {bars:bars.length,
  first:bars.length?bars[0].querySelector('.u-bar-legend')?.textContent||'':'' ,
  segs:bars.map(b=>b.querySelectorAll('.u-bar-seg').length),
  text:(document.querySelector('#u-main')||{}).innerText||''};})()"""

READ_USAGE = """(()=>{const meters=[...document.querySelectorAll('#u-main .u-meter')];
return {meters:meters.length,
  labels:meters.map(m=>(m.querySelector('.u-meter-label')||{}).textContent||''),
  values:meters.map(m=>(m.querySelector('.u-meter-value')||{}).textContent||''),
  text:(document.querySelector('#u-main')||{}).innerText||''};})()"""


async def main() -> None:
    root = desk.make_root("uigraphics")
    info = await seed(root)
    print("临时根:", root, flush=True)
    print("播种:", json.dumps(info, ensure_ascii=False), flush=True)

    desk.kill_tree()
    await asyncio.sleep(1.0)
    port = free_port()
    proc, cdp, _targets = await desk.boot_shell(root, port=port, env_extra={"ISEKAI_LLM_FAKE": "1"})
    problems: list[str] = []
    skipped: list[str] = []

    def say(tag: str, payload: object) -> None:
        print(f"{tag} {json.dumps(payload, ensure_ascii=False)}", flush=True)

    try:
        await cdp.js(desk.STUB)
        if not await wait_true(cdp, "!!(window.__uiApp && window.__uiApp.probeState.connected)"):
            raise SystemExit("止损：正式界面没连上核心")

        # ---------------- 1) 版本分叉图 ----------------
        await nav.launch_app(cdp, "isekai Chat")
        if not await nav.open_menu(cdp, "世界管理"):
            problems.append("进不去世界与素材")
        else:
            # 列表是异步读出来的：等这一行真的出现再点（否则点在空气上）
            row = await wait_true(
                cdp,
                "!![...document.querySelectorAll('#u-main tr')].find(r=>(r.innerText||'').includes("
                + json.dumps(WORLD_NAME)
                + "))",
                timeout=60,
            )
            if not row:
                problems.append("世界列表里没有播种出来的世界")
                await shot(cdp, "00_worlds")
            elif not await click_in_row(cdp, WORLD_NAME, "打开"):
                problems.append("世界列表里没找到「打开」按钮")
            if not await wait_true(cdp, "!!document.querySelector('#u-main svg.u-branch')", timeout=40):
                problems.append("世界详情里没有分支图")
                await shot(cdp, "00_detail")
            await asyncio.sleep(0.6)
            graph = await cdp.js(READ_BRANCH)
            say("分支图", graph)
            if graph["w"] < 200 or graph["h"] < 40:
                problems.append(f"分支图没有真的占位：{graph['w']}x{graph['h']}")
            if graph["dots"] != info["commits"]:
                problems.append(f"圆点数 {graph['dots']} ≠ 提交数 {info['commits']}")
            if graph["links"] != (1 if info["branch_id"] else 0):
                problems.append(f"分叉连线数={graph['links']}（只有跨线的来源才算分叉）")
            if len(graph["lanes"]) < 2:
                problems.append(f"泳道数不足：{graph['lanes']}")
            # 点圆点 → 下面那一行高亮（图与操作面同一份数据）
            if graph["dots"]:
                await cdp.js(
                    "document.querySelector('#u-main svg.u-branch circle')"
                    ".dispatchEvent(new MouseEvent('click',{bubbles:true}))"
                )
                await asyncio.sleep(0.4)
                hit = await cdp.js("document.querySelectorAll('#u-main li.u-commit-hit').length")
                if not hit:
                    problems.append("点分支图上的提交点没有连到下面的列表")
            await shot(cdp, "01_branch", "#u-main svg.u-branch")

            # ---------------- 2) 世界速度的人话换算 ----------------
            before = await cdp.js("(document.getElementById('u-rate-note')||{}).textContent||''")
            await set_value(cdp, "#u-main input[type=number]", "86400")
            await asyncio.sleep(0.3)
            after = await cdp.js("(document.getElementById('u-rate-note')||{}).textContent||''")
            say("速度换算", {"前": before, "后": after})
            if "现实 1 分钟" not in str(after) or before == after:
                problems.append(f"速度没有人话换算或没跟着输入变：{before!r} → {after!r}")

        # ---------------- 3) 大纲状态分布 ----------------
        await nav.launch_app(cdp, "isekai Writer")
        await wait_true(cdp, "!!document.querySelector('#u-main .u-bar-wrap')")
        await asyncio.sleep(0.6)
        writing = await cdp.js(READ_WRITING)
        say("大纲分布", {"条数": writing["bars"], "首条图例": writing["first"], "各条段数": writing["segs"]})
        if writing["bars"] < 2:
            problems.append(f"大纲页的比例条太少：{writing['bars']}")
        if "已达成" not in str(writing["first"]) or "进行中" not in str(writing["first"]):
            problems.append(f"概览条没有带数值的图例：{writing['first']!r}")
        if len(writing["segs"]) < 2 or max(writing["segs"]) < 2:
            problems.append(f"条目状态没有分段：{writing['segs']}")
        await shot(cdp, "02_outline")

        # ---------------- 4) 用量计量条 ----------------
        if not await nav.open_menu(cdp, "设置"):
            problems.append("进不去设置")
        else:
            await wait_true(cdp, "!!document.querySelector('#u-main .u-meter')")
            await asyncio.sleep(0.6)
            usage = await cdp.js(READ_USAGE)
            say("用量", {"条数": usage["meters"], "标签": usage["labels"][:3], "读数": usage["values"][:3]})
            if usage["meters"] < 1:
                problems.append("用量页没有计量条")
            if not any("上限" in str(item) or "/" in str(item) for item in usage["values"]):
                problems.append(f"计量条没有读数：{usage['values'][:2]}")
            if "instance_tokens_per_day" in str(usage["text"]) or "bucket" in str(usage["text"]):
                problems.append("用量页把内部键名显示给了用户")
            await shot(cdp, "03_usage", "#u-main .u-meter")

        # ---------------- 5) 本机检查清单 ----------------
        if not await nav.open_menu(cdp, "帮助与诊断"):
            problems.append("进不去帮助与诊断")
        else:
            await wait_true(cdp, "!!document.querySelector('#u-main .u-check-line')")
            report = await cdp.js(
                "(()=>{const lines=[...document.querySelectorAll('#u-main .u-check-line')];"
                "return {n:lines.length,glyphs:lines.map(l=>(l.querySelector('.u-check-glyph')||{}).textContent||'')};})()"
            )
            say("检查清单", report)
            if not report["n"]:
                problems.append("帮助页没有检查清单")
            if any(item not in ("✓", "✗") for item in report["glyphs"]):
                problems.append(f"检查项前缀不是勾/叉：{report['glyphs']}")
            await shot(cdp, "04_help", "#u-main .u-checks")

        # ---------------- 6) 线索比例条 ----------------
        await nav.launch_app(cdp, "isekai Chat")
        if not info["units"]:
            skipped.append("线索比例条：播种没有产出口述单元（未测）")
        else:
            await wait_true(cdp, "!!document.querySelector('#u-clue-bar')")
            await asyncio.sleep(1.2)
            spoken = sum(1 for stage in info["stages"] if stage == "spoken")
            held = len(info["stages"]) - spoken
            want = ([f"已经讲出口 {spoken}"] if spoken else []) + ([f"还没讲出口 {held}"] if held else [])
            clue = await cdp.js(
                "(()=>{const box=document.getElementById('u-clue-bar');"
                "return {keys:[...document.querySelectorAll('#u-clue-bar .u-bar-key')].map(k=>k.textContent),"
                "segs:document.querySelectorAll('#u-clue-bar .u-bar-seg').length,"
                "items:[...document.querySelectorAll('#u-clues li button')].map(b=>b.textContent)};})()"
            )
            say("线索", {"条": clue, "库里": info["stages"]})
            if sorted(clue["keys"]) != sorted(want):
                problems.append(f"线索比例条与真实口述单元对不上：界面={clue['keys']} 库={info['stages']}")
            if len(clue["items"]) != spoken:
                problems.append(f"讲出口的线索条数不对：界面 {len(clue['items'])} ≠ 库 {spoken}")
            if any("已经不在当前记录里" in str(text) for text in clue["items"]):
                problems.append(f"线索取错了字段（正文取不到）：{clue['items']}")
            await shot(cdp, "05_contact", "#u-clue-bar")
    finally:
        proc.terminate()
        desk.kill_tree()

    for item in skipped:
        print(f"[DEFERRED] {item}")
    if problems:
        print("\n".join(f"[FAIL] {item}" for item in problems))
        print(f"结论：FAIL（{len(problems)} 项）")
    else:
        print(f"结论：PASS（分支图 / 速度换算 / 大纲分布 / 用量 / 检查清单；截图在 {SHOTS}）")


if __name__ == "__main__":
    asyncio.run(main())
