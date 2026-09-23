"""导航 / 分栏高亮 / 返回层级的界面探针（2026-09-24 那批 UI 修复的回归）。

跑法（仓库根）：
  .venv/Scripts/python.exe scripts/_probe_ui_nav.py
前置：`npm run build` + `cargo build --features custom-protocol`（资源烘进 exe）。

覆盖六件事，全部读真实渲染结果（计算样式 / DOM 属性 / 路由），不看源码猜：

  N1 「从样例世界开始」在 AI 已配置时直接落到「准备材料」（不再回到本机检查）；
  N2 写作分区切换后当前格有可见高亮（计算样式与其它格不同）且有 aria-current；
  N3 「世界设定 / 角色卡」分栏高亮跟着内容走（不再停在「我的世界」）；
  N4 子页面顶栏返回写明去处，点它回到应用根；应用根点它是「选择应用」；
  N5 刚选过应用（<5 秒）再点返回，选择器照样弹出（用户的明确点击不被守卫吞掉）；
  N6 联络页「时间线与版本」落到世界详情；设置页 `sub:"extensions"` 滚到扩展那一节。
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
import _ui_nav_common as nav  # noqa: E402

SAMPLE = REPO / "examples" / "sample_world"
FAKE_REPLY = '{"ok": true}'
PROBLEMS: list[str] = []


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def check(name: str, ok: bool, detail: str) -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name} — {detail}", flush=True)
    if not ok:
        PROBLEMS.append(f"{name}: {detail}")


async def wait_true(cdp: desk.Cdp, expr: str, *, timeout: float = 45.0) -> bool:
    import time

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


async def text_of(cdp: desk.Cdp, selector: str) -> str:
    return str(await cdp.js(f"(document.querySelector({json.dumps(selector)})?.innerText||'')"))


async def route_pane(cdp: desk.Cdp) -> str:
    return str(await cdp.js("window.__uiApp.probeState.route.pane"))


async def tab_states(cdp: desk.Cdp, selector: str) -> list[list[str]]:
    value = await cdp.js(
        "(()=>{const bs=[...document.querySelectorAll(" + json.dumps(selector) + ")];"
        "return JSON.stringify(bs.map(b=>[(b.textContent||'').trim(),"
        "getComputedStyle(b).backgroundColor,getComputedStyle(b).fontWeight,"
        "b.getAttribute('aria-current')||'']));})()"
    )
    return json.loads(value) if value else []


async def main() -> None:
    root = desk.make_root("uinav")           # make_root 自带假密钥 → readiness.ai.configured = true
    (root / "examples").mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE, root / "examples" / "sample_world")
    print("临时根:", root, flush=True)

    proc, cdp, _targets = await desk.boot_shell(
        root,
        port=free_port(),
        env_extra={"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": FAKE_REPLY},
    )
    try:
        await cdp.js(desk.STUB)
        if not await wait_true(cdp, "!!(window.__uiApp && window.__uiApp.probeState.connected)"):
            check("N0 界面连上核心", False, "没有连上核心")
            return
        await cdp.js("document.querySelector('.u-launcher-overlay')?.remove()")

        # ---------------- N1 入口跳步 ----------------
        await cdp.js("window.__uiApp.navigate({pane:'home'})")
        await wait_true(cdp, "!!document.querySelector('#u-main button')")
        await click_text(cdp, "#u-main button", "从样例世界开始")
        landed_sample = await wait_true(cdp, "!!document.getElementById('onb-sample')", timeout=30)
        body = await text_of(cdp, "#u-main")
        check(
            "N1 AI 已配置时「从样例世界开始」直接到准备材料",
            landed_sample and "本机环境" not in body,
            f"样例选择器={landed_sample}｜本机检查正文出现={'本机环境' in body}｜前 40 字：{body[:40]!r}",
        )

        # 顺手用它把世界建出来（后面 N6 要用）
        await click_text(cdp, "#u-main button", "创建并开始联络")
        created = await wait_true(
            cdp,
            "[...document.querySelectorAll('#u-main button')].some(b=>b.textContent.includes('开始运行并联络'))",
            timeout=60,
        )
        await click_text(cdp, "#u-main button", "开始运行并联络")
        in_contact = await wait_true(cdp, "!!document.querySelector('#u-contact-input')", timeout=60)
        check("N1b 向导一路走到联络页", in_contact, f"created={created}")

        # ---------------- N6a 联络页 → 时间线与版本 ----------------
        if in_contact:
            await click_text(cdp, "#u-main button", "时间线与版本")
            in_detail = await wait_true(
                cdp, "(document.querySelector('#u-main')?.innerText||'').includes('返回世界列表')", timeout=30
            )
            check("N6a「时间线与版本」落到世界详情", in_detail, f"路由={await route_pane(cdp)}")
        else:
            check("N6a「时间线与版本」落到世界详情", False, "没进联络页，跳过")

        # ---------------- N2 写作分区高亮 ----------------
        await cdp.js("window.__uiApp.navigate({pane:'writing'})")
        await wait_true(cdp, "document.querySelectorAll('.u-crumbs button').length>=4", timeout=30)
        await click_text(cdp, ".u-crumbs button", "推进建议")
        await wait_true(cdp, "[...document.querySelectorAll('.u-crumbs button')].some(b=>b.getAttribute('aria-current')==='page')")
        states = await tab_states(cdp, ".u-crumbs button")
        active = [row for row in states if row[3] == "page"]
        others = [row for row in states if row[3] != "page"]
        distinct = bool(active and others and active[0][1] != others[0][1] and active[0][2] != others[0][2])
        check(
            "N2 写作分区的当前格高亮",
            len(active) == 1 and distinct,
            f"active={active}｜对照={others[:1]}",
        )

        # ---------------- N3 世界与素材分栏 ----------------
        await cdp.js("window.__uiApp.navigate({pane:'worlds'})")
        await wait_true(cdp, "!!document.querySelector('.u-tabs button')", timeout=30)
        await click_text(cdp, ".u-tabs button", "世界设定 / 角色卡")
        await wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('世界设定与角色卡')", timeout=30)
        states = await tab_states(cdp, ".u-tabs button")
        assets = [row for row in states if row[0] == "世界设定 / 角色卡"]
        worlds = [row for row in states if row[0] == "我的世界"]
        check(
            "N3 点「世界设定 / 角色卡」后高亮跟着走",
            bool(assets) and assets[0][3] == "page" and bool(worlds) and worlds[0][3] == "",
            f"assets={assets}｜worlds={worlds}",
        )

        # ---------------- N4 返回层级 ----------------
        # 先在 Writer 应用里（app_mode=writer），子页面的「返回」应当写「返回辅助写作」
        if not await nav.launch_app(cdp, "isekai Writer"):
            check("N4 子页返回写明去处并回到应用根", False, "启动器里没进 Writer")
        else:
            await cdp.js("window.__uiApp.navigate({pane:'settings', sub:'extensions'})")
            await wait_true(cdp, "!!document.getElementById('u-set-extensions')", timeout=30)  # 设置页独有
            top = await cdp.js("Math.round(document.getElementById('u-set-extensions').getBoundingClientRect().top)")
            view_h = await cdp.js("window.innerHeight")
            scrolled = await cdp.js("document.querySelector('.u-main').scrollTop")
            check(
                "N6b 设置页 extensions 定位到扩展节",
                isinstance(top, (int, float)) and top >= 0 and top < view_h - 120 and scrolled > 100,
                f"该节顶部 y={top}（视口 {view_h}）｜滚动位置={scrolled}",
            )
            back_label = str(await cdp.js("document.getElementById('u-back')?.textContent||''"))
            on_settings = await route_pane(cdp)
            await click_text(cdp, "#u-back", "返回辅助写作")
            returned = await wait_true(cdp, "window.__uiApp.probeState.route.pane==='writing'", timeout=30)
            check(
                "N4 子页返回写明去处并回到应用根",
                "返回辅助写作" in back_label and on_settings == "settings" and returned,
                f"标签={back_label!r}｜点前={on_settings}｜点后={await route_pane(cdp)}",
            )
            root_label = str(await cdp.js("document.getElementById('u-back')?.textContent||''"))
            check("N4b 应用根的返回是选择应用", "选择应用" in root_label, f"标签={root_label!r}")

        # ---------------- N5 5 秒内返回照样弹选择器 ----------------
        await click_text(cdp, ".u-launcher-card", "isekai Writer")
        await wait_true(cdp, "window.__uiApp.probeState.route.pane==='writing'", timeout=20)
        await click_text(cdp, "#u-back", "选择应用")          # 刚选过 < 5 秒
        again = await wait_true(cdp, "!!document.querySelector('.u-launcher')", timeout=20)
        check("N5 刚选过应用再点返回仍有选择器", bool(again), f"5 秒内={again}")

        print("\n结果:", "PASS" if not PROBLEMS else f"FAIL {len(PROBLEMS)} 项", flush=True)
        for item in PROBLEMS:
            print(" -", item, flush=True)
    finally:
        desk.kill_tree()
        _ = proc


async def run() -> None:
    desk.kill_tree()          # 老规矩：清掉上一次留下的壳（单实例插件会把新壳交给旧实例）
    await main()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(run(), timeout=600))
