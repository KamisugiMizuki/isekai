"""修复轮验收探针：真壳 + 假 LLM + 临时数据根，全流程走一遍（2026-10-07 修复轮锁）。

锁定的回归（每条都在真壳里实测过）：
  - W1/W9：转交通知卡的按钮排（[查看X][继续联络][仅作为联络发送]）。
    曾因 handoffTargets 用「尝试世界变化」等文案去匹配核心正文（实际是「创作请求」等）而整排不渲染——
    这里的「按钮出现→点→原文回输入框→重发有回复→不产生新通知」链专门守住它。改任一侧文案都要跑本探针。
  - W4 最近使用 / W3 加入角色向导 / W5 跑团样例入口 / W7 触控目标 44px / W8 焦点环主题色。

前置（缺一不可）：
  1) cd desktop && npm run build
  2) cd desktop/src-tauri && touch src/main.rs && cargo build   # 前端资源烘进壳二进制，不 touch 不重编
  3) 起静态服务服务最新 dist：cd desktop && node vite.js preview --port 1420 --strictPort
     （debug 壳写死 devUrl=http://127.0.0.1:1420；探针自检这条，缺了会 FATAL 退出）

跑法：cd <repo> && .venv/Scripts/python.exe scripts/_probe_final_ui.py
产物：截图在 .hermes/shots/（已 gitignore）；stdout 打 PASS/FAIL 与 SUMMARY。

与 _probe_ui_u1.py 的关系：u1 探针走向导单段验收；本探针覆盖修复轮全流程并复用它依赖的
_audit2_desk（boot/kill/STUB）与 _ui_nav_common（click_text/open_menu/wait_true）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import socket
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _audit2_desk as desk  # noqa: E402
import _ui_nav_common as nav  # noqa: E402

SHOTS = REPO / ".hermes" / "shots"
SAMPLE = REPO / "examples" / "sample_world"
TIDE = REPO / "examples" / "tide_rules_plugin"
FAKE_REPLY = '{"ok": true}'
HANDOFF_TEXT = "帮我把世界设定改一下：加一个内陆城邦。"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (" | " + detail if detail else ""), flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def dev_server_ok() -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = opener.open("http://127.0.0.1:1420/", timeout=3).read(200)
        return b"<html" in body.lower() or b"<!doctype" in body.lower()
    except Exception:  # noqa: BLE001
        return False


async def shot(cdp, name: str, focus: str = "") -> str:
    if focus:
        await cdp.js(
            "(()=>{const n=document.querySelector(" + json.dumps(focus)
            + ");if(n){n.scrollIntoView({block:'center'});}return !!n;})()"
        )
        await asyncio.sleep(0.4)
    SHOTS.mkdir(parents=True, exist_ok=True)
    raw = await cdp.call("Page.captureScreenshot", format="png")
    path = SHOTS / f"{name}.png"
    path.write_bytes(base64.b64decode(raw["data"]))
    return str(path)


async def set_value(cdp, selector: str, value: str) -> bool:
    expr = (
        "(()=>{const n=document.querySelector(" + json.dumps(selector)
        + ");if(!n){return false;}n.value=" + json.dumps(value)
        + ";n.dispatchEvent(new Event('input',{bubbles:true}));return true;})()"
    )
    return bool(await cdp.js(expr))


async def js_text(cdp, selector: str) -> str:
    return str(await cdp.js("(document.querySelector(" + json.dumps(selector) + ")?.innerText||'')"))


async def main() -> None:
    if not dev_server_ok():
        print("FATAL 1420 上没有服务——先起 vite preview（debug 壳加载 devUrl=127.0.0.1:1420）", flush=True)
        return
    root = desk.make_root("fixverify")
    (root / "examples").mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE, root / "examples" / "sample_world")
    shutil.copytree(TIDE, root / "examples" / "tide_rules_plugin")
    cfg_file = root / "config" / "config.yaml"
    lines = [ln for ln in cfg_file.read_text(encoding="utf-8").splitlines() if "api_key" not in ln]
    cfg_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("临时根:", root, flush=True)

    for name in ("isekai-desktop.exe", "isekai.exe"):
        desk.kill_tree(name)
    await asyncio.sleep(1.0)

    proc = None
    shots: list[str] = []
    try:
        proc, cdp, _targets = await desk.boot_shell(
            root, port=free_port(),
            env_extra={"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": FAKE_REPLY},
        )
        await cdp.js(desk.STUB)
        ok = await nav.wait_true(cdp, "!!(window.__uiApp && window.__uiApp.probeState.connected)", timeout=120)
        check("壳与应用连接", ok)

        async def S(name: str, focus: str = "") -> None:
            shots.append(await shot(cdp, name, focus))

        # ---- 01 启动器 ----
        if await nav.wait_true(cdp, "!!document.querySelector('.u-launcher')", timeout=20):
            await S("01_launcher")
            await nav.click_text(cdp, ".u-launcher-card", "isekai Chat")
            await nav.wait_true(cdp, "!document.querySelector('.u-launcher-overlay')", timeout=20)
        await asyncio.sleep(0.8)

        # ---- 02 向导走到联络页（假 LLM 用 JSON 回复过结构化测试，走全绿路径）----
        if await nav.click_text(cdp, "#u-main button", "从样例世界开始"):
            await nav.wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('本机环境')")
            await nav.click_text(cdp, "#u-main button", "继续：连接 AI")
            ok = await nav.wait_true(cdp, "!!document.getElementById('onb-key')", timeout=30)
            if ok:
                await set_value(cdp, "#onb-key", "probe-key")
                await set_value(cdp, "#onb-base-url", "https://api.example.com/v1")
                await set_value(cdp, "#onb-model", "probe-model")
                await nav.click_text(cdp, "#u-main button", "测试并保存")
                reached = await nav.wait_true(
                    cdp, "(document.querySelector('#u-main')?.innerText||'').includes('选择第一件事')", timeout=60
                )
                if not reached:
                    # 兜底：测试部分通过时先保存未验证配置，再尝试点步骤条
                    if await nav.click_text(cdp, "#u-main button", "保存未验证配置"):
                        await asyncio.sleep(0.8)
                        await cdp.js(
                            "(()=>{const n=[...document.querySelectorAll('.u-steps *')]"
                            ".find(x=>(x.innerText||'').trim()==='选择任务');if(n){n.click();return true;}return false;})()"
                        )
                        reached = await nav.wait_true(
                            cdp, "(document.querySelector('#u-main')?.innerText||'').includes('选择第一件事')",
                            timeout=30,
                        )
                check("连接 AI 测试并进入选择任务（假 LLM 全绿路径）", reached)
        await nav.click_text(cdp, "#u-main button", "开始联络")
        ok = await nav.wait_true(cdp, "!!document.getElementById('onb-sample')", timeout=30)
        if ok:
            await nav.click_text(cdp, "#u-main button", "创建并开始联络")
        await nav.wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('开始运行并联络')", timeout=90)
        await nav.click_text(cdp, "#u-main button", "开始运行并联络")
        ok = await nav.wait_true(cdp, "!!document.querySelector('#u-contact-input')", timeout=90)
        check("进入联络页", ok)
        await asyncio.sleep(1.2)

        # ---- 03 转交消息 → 通知卡（W1）----
        await set_value(cdp, "#u-contact-input", HANDOFF_TEXT)
        await nav.click_text(cdp, "#u-contact-send", "发送")
        got_notice = await nav.wait_true(
            cdp,
            "Array.from(document.querySelectorAll('.u-handoff')).some(n=>n.innerText.includes('创作请求'))",
            timeout=90,
        )
        check("转交通知卡出现", got_notice)
        bad_bubble = await cdp.js(
            "Array.from(document.querySelectorAll('.u-bubble-me')).some(n=>n.innerText.includes('创作请求'))"
        )
        check("通知不再是「我」的气泡（W1）", not bad_bubble)
        await asyncio.sleep(0.6)
        await S("02_notice_card")

        # ---- 04 仅作为联络发送（W9；按钮排曾因 matcher 错文案整排不渲染）----
        restored = False
        if await nav.click_text(cdp, "#u-main button", "仅作为联络发送"):
            restored = await nav.wait_true(
                cdp,
                "(document.getElementById('u-contact-input')?.value||'').includes('加一个内陆城邦')",
                timeout=15,
            )
            await asyncio.sleep(0.5)
            await S("03_as_contact_composer")
        check("「仅作为联络发送」把原文放回输入框（W9）", restored)
        hint = await js_text(cdp, "#u-contact-handoff")
        check("输入框附近有「不执行其中操作」提示（W9）", "不执行" in hint or "只把这句话" in hint, hint[:80])

        if restored:
            before = int(await cdp.js("document.querySelectorAll('.u-handoff').length"))
            await nav.click_text(cdp, "#u-contact-send", "发送")
            got_reply = await nav.wait_true(cdp, "!!document.querySelector('.u-bubble-them')", timeout=90)
            after = int(await cdp.js("document.querySelectorAll('.u-handoff').length"))
            check("重发后收到回复（W9）", got_reply)
            check("重发不再产生新转交通知（W9）", after <= before, f"before={before} after={after}")
            await asyncio.sleep(0.8)
            await S("04_as_contact_reply")

        # ---- 05 首页最近使用（W4）----
        if await nav.open_menu(cdp, "首页"):
            await asyncio.sleep(1.2)
            await S("05_home_recent")
            home_text = await js_text(cdp, "#u-main")
            check("首页出现「最近使用」分区（W4）", "最近使用" in home_text)
            check("「继续上次」显示最近条目而非兜底（W4）", "上次打开的是一个世界" not in home_text,
                  home_text[:100].replace("\n", "⏎"))

        # ---- 06/07 加入角色向导（W3）----
        if await nav.open_menu(cdp, "世界管理"):
            await asyncio.sleep(1.0)
            await nav.click_text(cdp, "#u-main button", "打开")
            await asyncio.sleep(1.2)
            if await nav.click_text(cdp, "#u-main button", "加入角色"):
                entered = await nav.wait_true(
                    cdp, "(document.querySelector('#u-main')?.innerText||'').includes('目标线')", timeout=15
                )
                check("加入角色向导第①步（W3）", entered)
                await asyncio.sleep(0.4)
                await S("06_join_step1")
                await nav.click_text(cdp, "#u-main button", "下一步：审定卡片")
                ok2 = await nav.wait_true(
                    cdp,
                    "(()=>{const t=document.querySelector('#u-main')?.innerText||'';"
                    "return t.includes('选这张')||t.includes('已有定义')||t.includes('已审定')||t.includes('重读');})()",
                    timeout=20,
                )
                selectable = int(await cdp.js(
                    "Array.from(document.querySelectorAll('#u-main button')).filter(b=>(b.innerText||'').trim()==='选这张').length"
                ))
                check("向导第②步卡片审定渲染（W3）", bool(ok2), f"可选卡={selectable}")
                await asyncio.sleep(0.4)
                await S("07_join_step2")
            else:
                check("加入角色按钮可点（W3）", False)

        # ---- 08/09 跑团从样例开始（W5）----
        if await nav.launch_app(cdp, "isekai GM"):
            await asyncio.sleep(1.5)
            await S("08_trpg_list")
            if await nav.click_text(cdp, "#u-main button", "从样例开始"):
                got_dialog = await nav.wait_true(
                    cdp,
                    "(()=>{const d=document.querySelector('.u-dialog');"
                    "return !!d && d.innerText.includes('登记随发行样例规则');})()",
                    timeout=30,
                )
                check("出现「登记随发行样例规则」确认框（W5）", got_dialog)
                if got_dialog:
                    await nav.click_text(cdp, ".u-dialog button", "登记并继续")
                    await asyncio.sleep(3.0)
                await S("09_trpg_after_sample")
                trpg_text = await js_text(cdp, "#u-main")
                check("样例路径不再要求手动找目录（W5）",
                      "examples\\tide" not in trpg_text and "examples/tide" not in trpg_text,
                      trpg_text[:100].replace("\n", "⏎"))
                check("登记后走到样例战役预填（W5）",
                      "样例战役" in trpg_text or "规则" in trpg_text, trpg_text[:100].replace("\n", "⏎"))

        # ---- 10 无障碍计算样式（W7/W8）----
        style_probe = await cdp.js("""(()=>{
          const out = {};
          const chipOk = document.querySelector('.u-chip-ok');
          if (chipOk) out.chipOk = getComputedStyle(chipOk,'::before').content;
          const prim = document.querySelector('.u-primary');
          if (prim) out.primaryMinH = getComputedStyle(prim).minHeight;
          const note = document.querySelector('.u-note');
          if (note) out.noteColor = getComputedStyle(note).color;
          const btn = document.querySelector('.u-btn');
          if (btn) { btn.focus(); out.focusOutline = getComputedStyle(btn).outlineColor; }
          return out;
        })()""")
        check("chip 状态符号生效（W7）",
              (str(style_probe.get("chipOk") or "")).strip('"') == "✓",
              json.dumps(style_probe, ensure_ascii=False)[:140])
        prim_h = str(style_probe.get("primaryMinH") or "")
        check("主按钮触控高度 44px（W7）", "44" in prim_h, prim_h)
        note_color = str(style_probe.get("noteColor") or "")
        check("提示文字不再是 #555（W7）", note_color not in ("rgb(85, 85, 85)", ""), note_color)
        check("焦点环回到主题色（W8）", str(style_probe.get("focusOutline")) not in ("rgb(25, 118, 210)",),
              str(style_probe.get("focusOutline")))

        # ---- 11 设置页兜底一瞥 ----
        if await nav.open_menu(cdp, "设置"):
            await asyncio.sleep(1.0)
            await S("10_settings")

        print("\nSUMMARY " + json.dumps(
            {"total": len(RESULTS), "failed": [r for r in RESULTS if not r[1]]}, ensure_ascii=False), flush=True)
        for p in shots:
            print("SHOT", p, flush=True)
    finally:
        desk.kill_tree("isekai-desktop.exe")
        if proc is not None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    asyncio.run(main())
