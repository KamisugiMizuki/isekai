"""界面空间恒常性探针：操作区（输入框）必须锚在底部，不随消息变多而移动。

跑法：.venv/Scripts/python.exe scripts/_probe_ui_anchor.py

看四件事（全部落在可观察几何与行为上）：
  1) 消息 0 条 / 1 轮 / 9 轮时，输入框底边位置一致，且都在视口内（不用滚动就能打字）；
  2) 滚动发生在消息列表（.u-messages）自己身上，页面容器 #u-main 的 scrollTop 始终为 0；
  3) 停在历史里时来新消息：不强制拉到底，而是给出「有新消息 ↓」；
  4) 点「有新消息」才回到底部。

先例：这些断言在 `.u-pane{height:100%}` 缺失时会全红（输入框被消息越推越下）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import _audit2_desk as desk  # noqa: E402
import _probe_ui_u1 as u1  # noqa: E402  （复用向导走法与点击辅助）
import _ui_nav_common as nav  # noqa: E402  （⋯ 菜单 / 启动器导航原语）

SAMPLE = REPO / "examples" / "sample_world"
BAR = (
    "JSON.stringify((()=>{{const t=document.querySelector({sel});const m=document.getElementById('u-main');"
    "if(!t||!m){{return null;}}const r=t.getBoundingClientRect();const mr=m.getBoundingClientRect();"
    "const chain=[];let p=t.parentElement;for(let i=0;i<6&&p;i++){{const c=getComputedStyle(p);"
    "chain.push([p.className||p.tagName,c.position,c.overflowY,Math.round(p.getBoundingClientRect().height)]);"
    "p=p.parentElement;}}"
    "return {{rowTop:Math.round(r.top),rowBottom:Math.round(r.bottom),mainTop:Math.round(mr.top),"
    "mainBottom:Math.round(mr.bottom),scroll:Math.round(m.scrollTop),scrollable:m.scrollHeight-m.clientHeight,"
    "position:getComputedStyle(t).position,chain:chain}};}})())"
)
MEASURE = (
    "JSON.stringify((()=>{"
    "const box=document.querySelector('.u-composer');"
    "const list=document.querySelector('.u-messages');"
    "const main=document.getElementById('u-main');"
    "const r=box?box.getBoundingClientRect():null;"
    "return {vh:window.innerHeight,"
    "top:r?Math.round(r.top):null,bottom:r?Math.round(r.bottom):null,"
    "listTop:list?Math.round(list.getBoundingClientRect().top):null,"
    "list:[list?list.scrollTop:-1,list?list.scrollHeight:-1,list?list.clientHeight:-1],"
    "main:[main?main.scrollTop:-1,main?main.scrollHeight:-1,main?main.clientHeight:-1],"
    "bubbles:document.querySelectorAll('.u-messages .u-bubble').length};})())"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def measure(cdp: desk.Cdp) -> dict:
    return json.loads(await cdp.js(MEASURE) or "{}")


async def toolbar_ok(cdp: desk.Cdp, selector: str, label: str, problems: list[str]) -> None:
    """滚到中段与到底时，分栏条都得看得见；中段还要贴在视口顶部（sticky）。"""
    mid = await cdp.js(
        "(()=>{const m=document.getElementById('u-main');if(!m){return 0;}"
        "m.scrollTop=Math.round((m.scrollHeight-m.clientHeight)*0.6);return m.scrollTop;})()"
    )
    await asyncio.sleep(0.5)
    got = json.loads(await cdp.js(BAR.format(sel=json.dumps(selector))) or "{}")
    print(f"[分栏条·{label}·中段{mid}px]", json.dumps(got, ensure_ascii=False))
    if not got:
        problems.append(f"{label}：找不到分栏条 {selector}")
        return
    if got["scrollable"] < 40:
        problems.append(f"{label}：视口压矮后页面仍滚不动（scrollable={got['scrollable']}），测不到锚定")
        return
    if got["position"] != "sticky":
        problems.append(f"{label}：分栏条 position={got['position']}（应为 sticky）")
    if not (got["mainTop"] - 8 <= got["rowTop"] <= got["mainTop"] + 40):
        problems.append(
            f"{label}：滚到中段后分栏条没贴在顶部（rowTop={got['rowTop']}、主区顶={got['mainTop']}）"
        )
    await cdp.js("(()=>{const m=document.getElementById('u-main');if(m){m.scrollTop=99999;}})()")
    await asyncio.sleep(0.4)
    end = json.loads(await cdp.js(BAR.format(sel=json.dumps(selector))) or "{}")
    print(f"[分栏条·{label}·到底]", json.dumps({k: end.get(k) for k in ('rowTop', 'rowBottom', 'mainTop', 'mainBottom', 'scroll')}, ensure_ascii=False))
    if end.get("rowTop", 1e9) < end.get("mainTop", 0) - 8 or end.get("rowBottom", 1e9) > end.get("mainBottom", 0):
        problems.append(f"{label}：滚到底后分栏条看不见了（{end.get('rowTop')}..{end.get('rowBottom')}）")


async def say(cdp: desk.Cdp, text: str) -> bool:
    """发一条并等到列表里多出至少一条（假模型秒回）。"""
    before = int(await cdp.js("document.querySelectorAll('.u-messages .u-bubble').length") or 0)
    await u1.set_value(cdp, "#u-contact-input", text)
    await u1.click_text(cdp, "#u-contact-send", "发送")
    ok = await u1.wait_true(
        cdp, f"document.querySelectorAll('.u-messages .u-bubble').length>{before}", timeout=60.0
    )
    await asyncio.sleep(1.2)
    return ok


async def main() -> None:
    root = desk.make_root("uianchor")
    (root / "examples").mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE, root / "examples" / "sample_world")
    config_file = root / "config" / "config.yaml"
    lines = [ln for ln in config_file.read_text(encoding="utf-8").splitlines() if "api_key" not in ln]
    config_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("临时根:", root)

    proc, cdp, _t = await desk.boot_shell(
        root, port=free_port(), env_extra={"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": '{"ok": true}'}
    )
    problems: list[str] = []
    try:
        await cdp.js(desk.STUB)
        if not await u1.wait_true(cdp, "!!(window.__uiApp && window.__uiApp.probeState.connected)"):
            problems.append("正式界面没有连上核心")
        # 走到联络页（与 u1 相同的首次设置路径）
        await u1.click_text(cdp, "#u-main button", "从样例世界开始")
        await u1.wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('本机检查')")
        await u1.click_text(cdp, "#u-main button", "继续：连接 AI")
        await u1.wait_true(cdp, "!!document.getElementById('onb-key')")
        await u1.set_value(cdp, "#onb-key", "probe-key")
        await u1.set_value(cdp, "#onb-base-url", "https://api.example.com/v1")
        await u1.set_value(cdp, "#onb-model", "probe-model")
        await u1.click_text(cdp, "#u-main button", "测试并保存")
        await u1.wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('选择第一件事')", timeout=60)
        await u1.click_text(cdp, "#u-main button", "开始联络")
        await u1.wait_true(cdp, "!!document.getElementById('onb-sample')")
        await u1.click_text(cdp, "#u-main button", "创建并开始联络")
        await u1.wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('开始运行并联络')", timeout=60)
        await u1.click_text(cdp, "#u-main button", "开始运行并联络")
        if not await u1.wait_true(cdp, "!!document.getElementById('u-contact-input')", timeout=60):
            problems.append(f"没有进入角色联络：{(await u1.visible_text(cdp, '#u-main'))[:200]}")
            raise SystemExit(0)

        empty = await measure(cdp)
        print("[0 条]", json.dumps(empty, ensure_ascii=False))
        dump = await cdp.js(
            "JSON.stringify((()=>{const box=document.querySelector('.u-contact');"
            "if(!box){return null;}const out=[];"
            "for(const n of box.querySelectorAll('.u-col-center > *')){const r=n.getBoundingClientRect();"
            "out.push([n.className||n.tagName,Math.round(r.top),Math.round(r.height)]);}"
            "const center=box.querySelector('.u-col-center');const cs=getComputedStyle(center);"
            "const pane=box.parentElement;const pc=getComputedStyle(pane);"
            "return {pane:[pane.className,Math.round(pane.getBoundingClientRect().height),pc.height,pc.display],"
            "contact:[Math.round(box.getBoundingClientRect().height),getComputedStyle(box).height],"
            "rows:getComputedStyle(box).gridTemplateRows,"
            "alignContent:getComputedStyle(box).alignContent,"
            "center:[Math.round(center.getBoundingClientRect().height),cs.height,cs.display,cs.alignSelf],"
            "children:out};})())"
        )
        print("[布局]", dump)
        if not await say(cdp, "在吗？"):
            problems.append("第一条没有发出或没有回复")
        one = await measure(cdp)
        print("[1 轮]", json.dumps(one, ensure_ascii=False))

        for i in range(8):
            if not await say(cdp, f"第 {i + 2} 条"):
                problems.append(f"第 {i + 2} 条没有完成往返")
                break
        many = await measure(cdp)
        print("[9 轮]", json.dumps(many, ensure_ascii=False))

        # --- 1) 操作区位置：三次读数一致，且都在视口内 ---
        for tag, reading in (("0 条", empty), ("1 轮", one), ("9 轮", many)):
            if reading.get("bottom") is None:
                problems.append(f"{tag}：找不到输入框（.u-composer）")
                continue
            if reading["bottom"] > reading["vh"] - 4:
                problems.append(f"{tag}：输入框底边 {reading['bottom']} 超出视口 {reading['vh']}（要滚动才能打字）")
        bottoms = {tag: r.get("bottom") for tag, r in (("0", empty), ("1", one), ("9", many))}
        if None not in bottoms.values() and max(bottoms.values()) - min(bottoms.values()) > 4:
            problems.append(f"输入框位置随消息增长而移动：{bottoms}")

        # --- 2) 滚动归属：列表自己滚，页面容器不滚 ---
        if many.get("main", [0])[0] != 0:
            problems.append(f"页面容器 #u-main 被滚动了（scrollTop={many['main'][0]}）")
        if many.get("list", [0, 0, 0])[1] < 320:
            problems.append(f"消息没攒够，测不出长对话：scrollHeight={many['list'][1]}")
        elif many["list"][1] <= many["list"][2]:
            problems.append(
                f"消息列表没有成为滚动容器：scrollHeight={many['list'][1]} clientHeight={many['list'][2]}"
            )
        if many.get("list", [0, 0, 0])[2] < 120:
            problems.append(f"消息列表被压扁（对话看不见）：clientHeight={many['list'][2]}")
        stuck = many["list"][1] - many["list"][0] - many["list"][2]
        print("[距列表底边]", stuck)
        if stuck > 12:
            problems.append(f"新消息到达后列表没停在底部（差 {stuck}px）")

        # --- 3) 停在历史 + 来新消息：给提示，不强制拉到底 ---
        await cdp.js("(()=>{const l=document.querySelector('.u-messages');l.scrollTop=0;l.dispatchEvent(new Event('scroll'));})()")
        await asyncio.sleep(0.5)
        hinted_before = await cdp.js("(()=>{const n=document.getElementById('u-contact-new');return !!n && !n.hidden;})()")
        if not await say(cdp, "我还在看上面那段"):
            problems.append("滚上去之后这条没发出去")
        await asyncio.sleep(0.6)
        hint_ok = await cdp.js("(()=>{const n=document.getElementById('u-contact-new');return !!n && !n.hidden;})()")
        after = await measure(cdp)
        print("[看历史时来新消息]", json.dumps({"提示前": hinted_before, "提示后": hint_ok, "读数": after}, ensure_ascii=False))
        if not hint_ok:
            problems.append("看历史时来新消息没有给「有新消息 ↓」提示")
        if after["list"][0] >= after["list"][1] - after["list"][2] - 10:
            problems.append("看历史时被强制拉到底了")

        # --- 4) 点提示才回到底部 ---
        await cdp.js("document.getElementById('u-contact-new').click()")
        await asyncio.sleep(0.6)
        back = await measure(cdp)
        if back["list"][0] < back["list"][1] - back["list"][2] - 10:
            problems.append("点「有新消息」没有回到底部")
        if back.get("bottom") is not None and back["bottom"] > back["vh"] - 4:
            problems.append("回到底部之后输入框又跑出视口了")

        # --- 5) 分栏条锚定：把视口压矮让页面滚得动，滚到底后分栏条仍在视口顶部 ---
        await cdp.call(
            "Emulation.setDeviceMetricsOverride", width=1000, height=260, deviceScaleFactor=1, mobile=False
        )
        await asyncio.sleep(0.6)
        await nav.open_menu(cdp, "世界管理")
        await u1.wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('世界与素材')")
        await toolbar_ok(cdp, ".u-tabs", "世界与素材", problems)
        await nav.launch_app(cdp, "isekai Writer")
        await u1.wait_true(cdp, "!!document.querySelector('nav.u-crumbs')")
        await toolbar_ok(cdp, "nav.u-crumbs", "写作分区", problems)
        await cdp.call("Emulation.clearDeviceMetricsOverride")

        # --- 6) 150% 字号下复核同一批不变量（走设置页真实控件，不用 hack）---
        await nav.open_menu(cdp, "设置")
        await u1.wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('通知与外观')")
        # 注意：设置页有两个「保存这一组」（另一组是检索设置）——按所在 section 里那个选择器的兄弟按钮点，别按文案找第一个
        PICK_SIZE = (
            "(()=>{{const v={value};const s=[...document.querySelectorAll('#u-main select')]"
            ".find(n=>[...n.options].some(o=>o.value===v));if(!s){{return false;}}"
            "s.value=v;s.dispatchEvent(new Event('change',{{bubbles:true}}));"
            "const sec=s.closest('section');const btn=[...(sec?sec.querySelectorAll('button'):[])]"
            ".find(b=>(b.textContent||'').includes('保存这一组'));if(!btn){{return false;}}btn.click();return true;}})()"
        )
        picked = await cdp.js(PICK_SIZE.format(value="'150'"))
        if not picked:
            problems.append("设置页找不到文字大小选择器（或它所在的保存按钮）")
        else:
            await u1.wait_true(cdp, "document.documentElement.dataset.textSize==='150'", timeout=20)
        big = await cdp.js("document.documentElement.dataset.textSize")
        if big != "150":
            problems.append(f"字号没有切到 150（读到 {big}）")
        else:
            await nav.launch_app(cdp, "isekai Chat")
            await u1.wait_true(cdp, "!!document.getElementById('u-contact-input')", timeout=30)
            await asyncio.sleep(1.5)
            zoom = await measure(cdp)
            print("[150% 字号]", json.dumps(zoom, ensure_ascii=False))
            if zoom.get("bottom") is None or zoom["bottom"] > zoom["vh"] - 4:
                problems.append(f"150% 字号下输入框跑出视口：{zoom.get('bottom')} / 视口 {zoom.get('vh')}")
            # 大字下空间预算本就紧：这里只卡「至少还看得见一行」，具体读数打出来记文档
            if zoom.get("list", [0, 0, 0])[2] < 48:
                problems.append(f"150% 字号下消息列表被压到看不见：clientHeight={zoom['list'][2]}")
            if zoom.get("main", [1])[0] != 0:
                problems.append(f"150% 字号下页面容器被滚动：scrollTop={zoom['main'][0]}")
            # 复原，别给后面的跑留个 150% 的根
            await nav.open_menu(cdp, "设置")
            await cdp.js(PICK_SIZE.format(value="'100'"))
            await u1.wait_true(cdp, "document.documentElement.dataset.textSize==='100'", timeout=20)

        print("\n结果:", "PASS" if not problems else "FAIL", "；".join(problems))
    finally:
        desk.kill_tree()
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(main())
