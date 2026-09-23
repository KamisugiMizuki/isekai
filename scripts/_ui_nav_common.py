"""正式界面的导航原语（方案 B 之后的探针共用）。

方案 B 删掉了左侧栏（`nav.u-nav`），探针原来点它的入口全部改走这里：
- `open_menu(cdp, "世界管理")` —— ⋯ 菜单里的全局页（首页 / 设置 / 帮助与诊断 / 世界管理）；
- `launch_app(cdp, "isekai Writer", "writing")` —— 启动器里选一个应用（联络 / 写作 / 跑团）。

两者都用真实点击（先点开浮层，再点条目），只有等待条件读路由。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

MENU_PANES = {
    "首页": "home",
    "设置": "settings",
    "帮助与诊断": "help",
    "世界管理": "worlds",
}
APP_PANES = {
    "isekai Chat": "contact",
    "isekai Writer": "writing",
    "isekai GM": "trpg",
}


async def wait_true(cdp: Any, expr: str, *, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await cdp.js(expr):
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.3)
    return False


async def click_text(cdp: Any, selector: str, text: str) -> bool:
    expr = (
        "(()=>{const nodes=[...document.querySelectorAll("
        + json.dumps(selector)
        + ")].filter(n=>n.offsetParent!==null||n.tagName==='OPTION');"
        + "const hit=nodes.find(n=>(n.textContent||'').includes("
        + json.dumps(text)
        + "));if(!hit){return false;}hit.click();return true;})()"
    )
    return bool(await cdp.js(expr))


async def open_menu(cdp: Any, label: str) -> bool:
    """从 ⋯ 菜单打开一个全局页（真实两跳点击 + 路由确认）。"""
    pane = MENU_PANES[label]
    await cdp.js("document.getElementById('u-more-btn')?.click()")
    if not await wait_true(cdp, "!!document.querySelector('.u-more-menu')", timeout=10):
        return False
    if not await click_text(cdp, ".u-more-menu-item", label):
        return False
    return await wait_true(cdp, f"window.__uiApp.probeState.route.pane==={json.dumps(pane)}")


async def launch_app(cdp: Any, card: str) -> bool:
    """从启动器选一个应用（先点返回按钮弹出选择器；在子页面要多点一次回到应用根）。"""
    pane = APP_PANES[card]
    for _ in range(3):
        if await cdp.js("!!document.querySelector('.u-launcher')"):
            break
        await cdp.js("document.getElementById('u-back')?.click()")
        if await wait_true(cdp, "!!document.querySelector('.u-launcher')", timeout=6):
            break
    if not await cdp.js("!!document.querySelector('.u-launcher')"):
        return False
    if not await click_text(cdp, ".u-launcher-card", card):
        return False
    return await wait_true(cdp, f"window.__uiApp.probeState.route.pane==={json.dumps(pane)}")
