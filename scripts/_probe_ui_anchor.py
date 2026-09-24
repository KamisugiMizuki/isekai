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

SAMPLE = REPO / "examples" / "sample_world"
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

        print("\n结果:", "PASS" if not problems else "FAIL", "；".join(problems))
    finally:
        desk.kill_tree()
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(main())
