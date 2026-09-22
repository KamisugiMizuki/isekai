"""正式界面 U1 的真壳验收：临时数据根 + 假模型，经 CDP 驱动真壳（不碰本机数据）。

跑法：.venv/Scripts/python.exe scripts/_probe_ui_u1.py

看六件事（每条都打在可观察行为上，不看源码猜）：

  1) 首启进首页：三个任务入口、先连接 AI、推荐下一步都在，且状态取自真实读数；
  2) 首次设置向导：本机检查五项真读 → AI 连接测试两项通过并保存 → 选择任务；
  3) 从样例开始：随发行样例复制进创作目录 → 创建世界 → 启动 → 进联络；
  4) 联络真往返：发送 → 界面出现自己的话与她的回复（真 UMP + 真 SQLite + 假模型）；
  5) 未发送输入按 §3.5 落进 ui_draft（停止输入 1 秒后「已保存」），界面能恢复它；
  6) 帮助与诊断读数、以及「高级调试 → 回到正式界面」的往返。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import _audit2_desk as desk  # noqa: E402

SAMPLE = REPO / "examples" / "sample_world"
FAKE_REPLY = '{"ok": true}'


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
        await asyncio.sleep(0.3)
    return False


async def click_text(cdp: desk.Cdp, selector: str, text: str) -> bool:
    expr = (
        "(()=>{const nodes=[...document.querySelectorAll("
        + json.dumps(selector)
        + ")].filter(n=>n.offsetParent!==null||n.tagName==='OPTION');"
        + "const hit=nodes.find(n=>(n.textContent||'').includes("
        + json.dumps(text)
        + "));if(!hit){return false;}hit.click();return true;})()"
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


async def visible_text(cdp: desk.Cdp, selector: str) -> str:
    value = await cdp.js(
        "(()=>{const n=document.querySelector(" + json.dumps(selector) + ");return n?(n.innerText||n.textContent||''):'';})()"
    )
    return str(value or "")


async def draft_texts(cdp: desk.Cdp) -> list[str]:
    value = await cdp.js(
        "window.__uiApi().draftList('contact').then(r=>JSON.stringify((r.drafts||[]).map(d=>[d.text,d.state])))",
        await_promise=True,
    )
    try:
        return json.loads(value) if value else []
    except (TypeError, json.JSONDecodeError):
        return []


async def main() -> None:
    root = desk.make_root("uiu1")
    (root / "examples").mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE, root / "examples" / "sample_world")
    # 首启的真实状态：还没有访问密钥（这样首页才会给出「先连接 AI」）
    config_file = root / "config" / "config.yaml"
    lines = [line for line in config_file.read_text(encoding="utf-8").splitlines() if "api_key" not in line]
    config_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("临时根:", root)

    proc, cdp, _targets = await desk.boot_shell(
        root,
        port=free_port(),
        env_extra={"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": FAKE_REPLY},
    )
    problems: list[str] = []
    try:
        await cdp.js(desk.STUB)
        ok = await wait_true(cdp, "!!(window.__uiApp && window.__uiApp.probeState.connected)")
        if not ok:
            problems.append("正式界面没有连上核心")
        state = await cdp.js("JSON.stringify(window.__uiApp.probeState)")
        print("[连接]", state)

        # ---------------- 1) 首页 ----------------
        home = await visible_text(cdp, "#u-main")
        cards = await cdp.js("document.querySelectorAll('#u-main .u-card').length")
        print("[首页]", json.dumps({"卡片": cards, "片段": home[:80]}, ensure_ascii=False))
        if cards != 3:
            problems.append(f"首页任务入口数={cards}（应 3）")
        if "先连接 AI" not in home:
            problems.append("未配置 AI 时首页没有给出「先连接 AI」")
        if "推荐下一步" not in home:
            problems.append("无数据时首页没有给出推荐下一步")

        # ---------------- 2) 首次设置向导 ----------------
        if not await click_text(cdp, "#u-main button", "从样例世界开始"):
            problems.append("点不到「从样例世界开始」")
        await wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('首次设置')")
        check_note = await visible_text(cdp, "#u-main")
        if "本机环境" not in check_note:
            problems.append(f"向导第一步不是本机检查：{check_note[:60]}")
        if "继续：连接 AI" not in check_note:
            problems.append(f"本机检查没有通过：{check_note[:160]}")
        await click_text(cdp, "#u-main button", "继续：连接 AI")
        await wait_true(cdp, "!!document.getElementById('onb-key')")
        await set_value(cdp, "#onb-key", "sk-probe-1234567890")
        await set_value(cdp, "#onb-base-url", "https://api.example.com/v1")
        await set_value(cdp, "#onb-model", "probe-model")
        await click_text(cdp, "#u-main button", "测试并保存")
        reached_task = await wait_true(
            cdp, "(document.querySelector('#u-main')?.innerText||'').includes('选择第一件事')", timeout=60.0
        )
        prefs = await cdp.js("JSON.stringify(window.__uiApp.probeState.prefs)")
        print("[向导]", json.dumps({"进入选择任务": reached_task, "偏好": prefs}, ensure_ascii=False)[:400])
        if not reached_task:
            problems.append(f"连接测试没有走通：{(await visible_text(cdp, '#u-main'))[:300]}")
        elif '"ai.verified":true' not in str(prefs):
            problems.append("测试通过但没记下「已验证」")
        if "api_key" in str(await visible_text(cdp, "#u-main")) and "访问密钥" not in await visible_text(cdp, "#u-main"):
            problems.append("界面把密钥当正文显示了")

        # ---------------- 3) 从样例创建 ----------------
        await click_text(cdp, "#u-main button", "开始联络")
        await wait_true(cdp, "!!document.getElementById('onb-sample')")
        options = await cdp.js(
            "[...document.querySelectorAll('#onb-character option')].map(o=>o.textContent).join(',')"
        )
        print("[样例角色]", options)
        if "堤禾" not in str(options):
            problems.append(f"样例角色列表不对：{options}")
        await click_text(cdp, "#u-main button", "创建并开始联络")
        created = await wait_true(
            cdp, "(document.querySelector('#u-main')?.innerText||'').includes('开始运行并联络')", timeout=60.0
        )
        if not created:
            problems.append(f"创建没有走到开始使用：{(await visible_text(cdp, '#u-main'))[:300]}")
        else:
            start_text = await visible_text(cdp, "#u-main")
            print("[开始使用]", json.dumps(start_text[:120], ensure_ascii=False))
            if "灰潮纪" not in start_text:
                problems.append(f"开始使用没有报出世界名：{start_text[:120]}")
        await click_text(cdp, "#u-main button", "开始运行并联络")
        in_contact = await wait_true(cdp, "!!document.querySelector('#u-contact-input')", timeout=60.0)
        if not in_contact:
            problems.append(f"没有进入角色联络：{(await visible_text(cdp, '#u-main'))[:300]}")

        # ---------------- 4) 联络真往返 ----------------
        header = await visible_text(cdp, ".u-contact-head")
        print("[联络头]", header[:120])
        if "灰潮纪" not in header and "堤禾" not in header:
            problems.append(f"联络头没有世界 / 角色信息：{header[:80]}")
        run_state = await wait_true(cdp, "(document.querySelector('.u-contact-head .u-chip')?.textContent||'')==='运行中'")
        if not run_state:
            problems.append("世界线没有显示为运行中（启动没生效）")
        await set_value(cdp, "#u-contact-input", "在吗？")
        await click_text(cdp, "#u-contact-send", "发送")
        mine = await wait_true(cdp, "(document.querySelector('.u-messages')?.innerText||'').includes('在吗？')")
        reply = await wait_true(
            cdp, "!!document.querySelector('.u-bubble-them')", timeout=90.0
        )
        messages = await visible_text(cdp, ".u-messages")
        print("[消息]", json.dumps(messages[:200], ensure_ascii=False))
        if not mine:
            problems.append("发送后界面没有显示自己那条")
        if not reply:
            problems.append(f"没有等到她的回复：{messages[:160]}")

        # ---------------- 5) 未发送输入按 §3.5 持久化 ----------------
        await set_value(cdp, "#u-contact-input", "这句话还没发出去")
        saved = await wait_true(
            cdp, "(document.getElementById('u-contact-draft')?.textContent||'').includes('已保存')", timeout=30.0
        )
        drafts = await draft_texts(cdp)
        print("[草稿]", json.dumps({"槽": saved, "草稿": drafts}, ensure_ascii=False))
        if not saved:
            problems.append(
                f"草稿没有显示「已保存」：{await visible_text(cdp, '#u-contact-draft')}"
            )
        if not any("这句话还没发出去" in str(item[0]) for item in drafts):
            problems.append(f"草稿没有落到 ui_draft：{drafts}")

        # 切页再回来：草稿应从核心读回
        await cdp.js("window.__uiApp.navigate({pane:'help'})")
        await wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('帮助与诊断')")
        # ---------------- 6) 帮助与诊断 + 高级调试往返 ----------------
        help_text = await visible_text(cdp, "#u-main")
        for want in ("本机检查明细", "后台服务", "高级调试", "常见问题"):
            if want not in help_text:
                problems.append(f"帮助页缺少「{want}」")
        await click_text(cdp, "#u-main button", "进入高级调试")
        in_debug = await wait_true(
            cdp, "document.body.dataset.mode==='debug' && getComputedStyle(document.getElementById('debug-app')).display!=='none'"
        )
        debug_status = await visible_text(cdp, "#status")
        print("[高级调试]", json.dumps({"进入": in_debug, "状态": debug_status}, ensure_ascii=False))
        if not in_debug:
            problems.append("没有切到高级调试界面")
        await cdp.js("document.getElementById('back-to-user').click()")
        back = await wait_true(cdp, "document.body.dataset.mode==='user'")
        if not back:
            problems.append("回不到正式界面")

        # 回联络页：草稿应仍在输入框里（界面从 ui_draft 读回）
        await cdp.js("window.__uiApp.navigate({pane:'contact'})")
        await wait_true(cdp, "!!document.querySelector('#u-contact-input')", timeout=30.0)
        restored = await wait_true(
            cdp,
            "(document.querySelector('#u-contact-input')?.value||'').includes('这句话还没发出去')",
            timeout=30.0,
        )
        if not restored:
            diag = await cdp.js(
                "JSON.stringify({route:window.__uiApp.probeState.route,navLog:window.__uiApp.probeState.navLog,"
                "current:Object.getPrototypeOf(window.__uiApp).constructor.name,"
                "paneName:(()=>{const p=Object.values(window.__uiApp).find(v=>v&&v.constructor&&v.constructor.name.endsWith('Pane'));return p?p.constructor.name:null;})(),"
                "input:!!document.getElementById('u-contact-input'),mainHead:(document.getElementById('u-main')||{}).innerText?.slice(0,40),"
                "errors:[...document.querySelectorAll('#u-main .u-error')].map(n=>n.innerText.slice(0,120))})"
            )
            print("[回访诊断]", str(diag)[:700])
            problems.append("回到联络页没有恢复未发送内容")

        print("\n结果:", "PASS" if not problems else "FAIL", "；".join(problems))
    finally:
        desk.kill_tree()
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(main())
