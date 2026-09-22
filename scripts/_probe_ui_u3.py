"""辅助写作工作区（界面 U3 §7.1–§7.5）的真壳验收：临时数据根 + 假模型。

跑法：.venv/Scripts/python.exe scripts/_probe_ui_u3.py
日志：scripts/_u3probe.log

真壳（Tauri + CDP）→ 真核心（WebSocket + SQLite）→ 真实例；只有提案那一次调用换假模型。
看六件事：

  1) 进工作区：世界 / 时间线 / 观察角色 / 大纲 四个选择 + 四个分区都在；
  2) 新建大纲 → 绑定：条目按六类出现，状态从「未开始」起算（模板里的状态不算数）；
  3) 条目决定：开始推进要写理由、不需要依据；达成才要依据；
  4) 当前素材：只给该角色合法可知的材料，并带来源标签；
  5) 推进建议：候选卡带方案 / 依据 / 待定问题 / 改世界与否；一条带世界变化的候选
     「预览世界变化 → 确认应用」后**真的**进世界（版本点 +1），另一条「以此起草」；
  6) 文字草稿：编辑 → 保存 → 锁定（解锁），锁定状态在数据库里看得见。
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import _audit2_desk as desk  # noqa: E402

SAMPLE = REPO / "examples" / "sample_world"
WORLD_NAME = "北堤世界"
LOG = REPO / "scripts" / "_u3probe.log"
OUTLINE_NAME = "北堤故事"
THEME = "主题围绕盐与潮：人怎么记住一件没人愿意记的事"
REASON = "先把这条推进起来，后面再补依据"
DRAFT_TEXT = "退潮后的盐滩像一张没写完的账页：谁欠谁的，海水都记得。"


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


async def click_dialog(cdp: desk.Cdp, label: str) -> bool:
    expr = (
        "(()=>{const box=document.querySelector('.u-dialog');if(!box){return false;}"
        "const hit=[...box.querySelectorAll('button')].find(b=>(b.textContent||'').trim().includes("
        + json.dumps(label)
        + "));if(!hit){return false;}hit.click();return true;})()"
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


async def card_button(cdp: desk.Cdp, needle: str, label: str) -> str:
    """在含 needle 的那张候选卡里点 label 按钮"""
    expr = (
        "(()=>{const cards=[...document.querySelectorAll('.u-card')];"
        "const card=cards.find(c=>(c.textContent||'').includes("
        + json.dumps(needle)
        + "));if(!card){return 'no-card';}"
        "const hit=[...card.querySelectorAll('button')].find(b=>(b.textContent||'').trim().includes("
        + json.dumps(label)
        + "));if(!hit){return 'no-button';}hit.click();return 'ok';})()"
    )
    return str(await cdp.js(expr))


async def row_button(cdp: desk.Cdp, needle: str, label: str) -> str:
    expr = (
        "(()=>{const rows=[...document.querySelectorAll('.u-row-line')];"
        "const row=rows.find(r=>(r.textContent||'').includes("
        + json.dumps(needle)
        + "));if(!row){return 'no-row';}"
        "const hit=[...row.querySelectorAll('button')].find(b=>(b.textContent||'').trim().includes("
        + json.dumps(label)
        + "));if(!hit){return 'no-button';}hit.click();return 'ok';})()"
    )
    return str(await cdp.js(expr))


def seed(root: Path) -> dict:
    """真链路造一个世界：一张角色卡 + 一条已启动的主线（不经过导入向导，省时间）。"""
    import time as _time

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
    instance_id = str(info["id"])
    timeline_id = str(store.timeline_list(instance_id)[0]["id"])
    world.ensure_instance(instance_id, now_real=_time.time())
    world.activate(instance_id, timeline_id, now_real=_time.time())
    out = {
        "instance_id": instance_id,
        "timeline_id": timeline_id,
        "card_id": str(card["meta"]["card_id"]),
        "worlds": len(store.instance_list()),
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
        out: dict = {}
        outlines = store.wa_outline_list()
        out["outlines"] = [(str(row["id"]), str(row.get("name") or "")) for row in outlines]
        instances = store.instance_list()
        out["instance_id"] = str(instances[0]["id"]) if instances else ""
        timelines = store.timeline_list(out["instance_id"]) if out["instance_id"] else []
        out["timeline_id"] = str(timelines[0]["id"]) if timelines else ""
        states = store.wa_state_list(out["instance_id"], out["timeline_id"]) if out["instance_id"] else []
        out["bound_outline"] = str(states[0]["outline_id"]) if states else ""
        items = json.loads(str(states[0]["items"])) if states else []
        out["item_status"] = {str(item["id"]): str(item["status"]) for item in items}
        candidates = (
            store.wa_candidate_list(out["instance_id"], out["timeline_id"]) if out["instance_id"] else []
        )
        out["candidates"] = [
            {
                "id": str(row["id"]),
                "kind": str(row.get("kind") or ""),
                "status": str(row.get("status") or ""),
                "locked": float(row.get("locked_at") or 0) > 0,
                "joint": str(row.get("joint_commit_id") or ""),
                "changes": len(json.loads(str(row.get("changes") or "[]"))),
            }
            for row in candidates
        ]
        # 世界事实（事件）条数：提交之后它必须真的多出来——「已生效」不能只在界面上成立
        out["events"] = len(store.event_ids(out["instance_id"], out["timeline_id"])) if out["instance_id"] else 0
        return out
    finally:
        store.close()


async def main() -> None:
    root = desk.make_root("uiu3")
    info = seed(root)
    print("临时根:", root)
    print("播种:", json.dumps(info, ensure_ascii=False), flush=True)

    instance_id, timeline_id, card_id = info.get("instance_id", ""), info.get("timeline_id", ""), info.get("card_id", "")
    # 假模型：一次提案调用（判断点「情节提议」）返回两条候选，其中一条带世界变化
    suggestions = {
        "candidates": [
            {
                "title": "封堤三日",
                "summary": "北堤缺口先封三天，盐价随之翻倍：她知道这笔账要算到谁头上。",
                "outline_ref": "",
                "unsolved": ["封堤的钱谁出"],
                "changes": [
                    {
                        "id": "sgc-1",
                        "kind": "condition",
                        "operation": "set",
                        "certainty": "confirmed",
                        "target_refs": [card_id] if card_id else [],
                        "value": "北堤封三日",
                        "expiry": "until_cleared",
                    }
                ],
            },
            {
                "title": "账页上的名字",
                "summary": "她在旧账页上看见一个不该出现的名字，先不说。",
                "outline_ref": "",
                "unsolved": ["那个人为什么出现在账上"],
                "changes": [],
            },
        ]
    }
    desk.kill_tree()
    await asyncio.sleep(1.0)
    port = free_port()
    print("调试端口:", port, flush=True)
    proc, cdp, _targets = await desk.boot_shell(
        root,
        port=port,
        env_extra={
            "ISEKAI_LLM_FAKE": "1",
            "ISEKAI_LLM_FAKE_REPLY": (SAMPLE / "huichao.json").read_text(encoding="utf-8"),
            "ISEKAI_LLM_FAKE_JUDGEMENTS": json.dumps({"情节提议": json.dumps(suggestions, ensure_ascii=False)}, ensure_ascii=False),
        },
    )
    problems: list[str] = []

    def say(tag: str, payload: object) -> None:
        print(f"{tag} {json.dumps(payload, ensure_ascii=False)}", flush=True)

    try:
        await cdp.js(desk.STUB)
        if not await wait_true(cdp, "!!(window.__uiApp && window.__uiApp.probeState.connected)"):
            raise SystemExit("止损：正式界面没连上核心")

        # ---------------- 1) 进工作区 ----------------
        await click_text(cdp, "nav.u-nav button", "辅助写作")
        await asyncio.sleep(1.6)
        text = await visible_text(cdp)
        for need in ("辅助写作", "世界", "时间线", "观察角色", "大纲", "新建大纲", "绑定到大纲"):
            if need not in text:
                problems.append(f"工作区缺：{need}")
        for tab in ("大纲", "当前素材", "推进建议", "文字草稿"):
            if tab not in text:
                problems.append(f"缺分区：{tab}")
        say("工作区", text[:110])

        # ---------------- 2) 新建大纲 + 绑定 ----------------
        await click_text(cdp, "#u-main button", "新建大纲")
        if not await wait_true(cdp, "!!document.querySelector('#u-wa-outline-name')"):
            problems.append("新建大纲对话框没打开")
        await set_value(cdp, "#u-wa-outline-name", OUTLINE_NAME)
        await set_value(cdp, "#u-wa-outline-theme", THEME)
        await set_value(cdp, "#u-wa-outline-criteria", "读的人能说出这件事被谁记住了")
        await click_dialog(cdp, "保存大纲")
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('" + OUTLINE_NAME + "')"):
            problems.append("新建的大纲没有出现在选择里")
        await click_text(cdp, "#u-main button", "绑定到大纲", last=True)
        if not await wait_true(cdp, "!!document.querySelector('#u-wa-chapter')"):
            problems.append("绑定对话框没打开")
        await set_value(cdp, "#u-wa-chapter", "第一章")
        await click_dialog(cdp, "绑定")
        bound = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('已绑定')")
        state = inspect(root)
        if not bound or not state["bound_outline"]:
            problems.append(f"绑定没有生效：界面={bound} 库={state['bound_outline']}")
        outline_text = await visible_text(cdp)
        for need in ("主题约束", "必达节点", "禁止事项", "角色弧线", "节奏目标", "可变素材"):
            if need not in outline_text:
                problems.append(f"大纲分区缺：{need}")
        say("绑定", {"界面已绑定": bound, "库里": state["bound_outline"], "条目状态": state["item_status"]})

        # ---------------- 3) 条目决定：开始推进（不要依据） ----------------
        chain = ""
        if "主题约束" in outline_text:
            chain = await card_button(cdp, "主题约束", "决定…")
        else:
            chain = "no-card"
        if chain != "ok":
            problems.append(f"条目的决定按钮点不到：{chain}")
        if not await wait_true(cdp, "!!document.querySelector('#u-wa-reason')"):
            problems.append("决定对话框没打开")
        await set_value(cdp, "#u-wa-target", "in_progress")
        await set_value(cdp, "#u-wa-reason", REASON)
        await click_dialog(cdp, "记下这个决定")
        await asyncio.sleep(1.2)
        state = inspect(root)
        started = [key for key, value in state["item_status"].items() if value == "in_progress"]
        if not started:
            problems.append(f"「开始推进」没有落下：{state['item_status']}")
        say("决定", {"进行中": started, "全量": state["item_status"]})

        # ---------------- 4) 检查大纲 ----------------
        await click_text(cdp, "#u-main button", "检查大纲")
        checked = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('检查结果')", timeout=60)
        if not checked:
            problems.append("检查大纲没有给出结果")
        say("检查", (await visible_text(cdp))[-140:])

        # ---------------- 5) 当前素材 ----------------
        await click_text(cdp, "#u-main nav.u-crumbs button", "当前素材")
        await asyncio.sleep(0.6)
        await click_text(cdp, "#u-main button", "读取当前素材")
        await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('她知道的')", timeout=60)
        material = await visible_text(cdp)
        for need in ("她知道的", "作者依据"):
            if need not in material:
                problems.append(f"当前素材缺：{need}")
        say("当前素材", material[:120])

        # ---------------- 6) 推进建议 ----------------
        await click_text(cdp, "#u-main nav.u-crumbs button", "推进建议")
        await asyncio.sleep(0.5)
        await set_value(cdp, "#u-wa-goal", "这一章要让读者第一次怀疑账本")
        await click_text(cdp, "#u-main button", "给我推进建议")
        got = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('封堤三日')", timeout=120)
        advice = await visible_text(cdp)
        if not got:
            problems.append(f"建议没有出现：{advice[-160:]}")
        for need in ("封堤三日", "账页上的名字", "待定问题", "是否改世界"):
            if need not in advice:
                problems.append(f"候选卡缺：{need}")
        state = inspect(root)
        say("建议", {"库里候选": state["candidates"]})

        # 6a) 带世界变化的那条：预览 → 确认应用
        before = state["events"]
        if "预览世界变化" in advice:
            clicked = await card_button(cdp, "封堤三日", "预览世界变化")
            if clicked != "ok":
                problems.append(f"预览按钮点不到：{clicked}")
            if not await wait_true(cdp, "!!document.querySelector('.u-dialog')"):
                problems.append("预览对话框没打开")
            preview = await visible_text(cdp, ".u-dialog")
            for need in ("不改世界", "确认应用"):
                if need not in preview:
                    problems.append(f"预览缺：{need}")
            await click_dialog(cdp, "确认应用")
            applied = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('已生效')", timeout=90)
            after = inspect(root)
            if not applied:
                problems.append("确认应用之后界面没有「已生效」")
            if after["events"] <= before:
                problems.append(f"世界没有真的变化（事件 {before} → {after['events']}）")
            committed = [row for row in after["candidates"] if row["joint"]]
            if not committed:
                problems.append("库里没有带提交依据的候选（已生效不可追溯）")
            say("应用", {"事件": [before, after["events"]], "已提交候选": committed})
        else:
            problems.append("带世界变化的候选没有给出「预览世界变化」")

        # 6b) 文字那条：以此起草
        await asyncio.sleep(0.6)
        drafted = await card_button(cdp, "账页上的名字", "以此起草")
        if drafted != "ok":
            problems.append(f"以此起草点不到：{drafted}")
        await asyncio.sleep(0.8)

        # ---------------- 7) 文字草稿：编辑 → 保存 → 锁定 → 解锁 ----------------
        draft_text = await visible_text(cdp)
        if "正文" not in draft_text or "锁定正文" not in draft_text and "解锁并编辑" not in draft_text:
            problems.append(f"没有进到文字草稿：{draft_text[:140]}")
        await set_value(cdp, "#u-wa-draft-title", "第一章·退潮")
        await set_value(cdp, "#u-wa-draft-body", DRAFT_TEXT)
        await click_text(cdp, "#u-main button", "保存文字草稿")
        saved = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('文字草稿已保存')", timeout=60)
        if not saved:
            problems.append(f"保存文字草稿没有回执：{(await visible_text(cdp))[-140:]}")
        await click_text(cdp, "#u-main button", "锁定正文")
        locked = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('正文已锁定')", timeout=60)
        state = inspect(root)
        locked_rows = [row for row in state["candidates"] if row["locked"]]
        if not locked or not locked_rows:
            problems.append(f"锁定没有生效：界面={locked} 库={state['candidates']}")
        say("锁定", {"界面": locked, "库里锁定件": locked_rows})
        await click_text(cdp, "#u-main button", "解锁并编辑")
        unlocked = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('已解锁')", timeout=60)
        state = inspect(root)
        if not unlocked or any(row["locked"] for row in state["candidates"]):
            problems.append(f"解锁没有生效：{state['candidates']}")
        say("解锁", {"界面": unlocked, "库里锁定件": [row for row in state["candidates"] if row["locked"]]})

        final = inspect(root)
        say("终局", {"条目状态": final["item_status"], "候选": final["candidates"], "事件": final["events"]})
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
        print("结论：PASS（工作区 / 大纲 / 决定 / 素材 / 建议与三种动作 / 文字草稿与锁定）")


if __name__ == "__main__":
    import contextlib

    with LOG.open("w", encoding="utf-8") as handle, contextlib.redirect_stdout(handle):
        asyncio.run(main())
    print(LOG.read_text(encoding="utf-8"))
