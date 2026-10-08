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
    """进一个工作区（联络 / 写作 / 跑团）。

    「选择应用」浮层已删除（2026-10-08）：三个工作区与首页都在同一层，从 ⋯ 菜单或首页
    直接进即可，不再有「先弹浮层再点卡片」这一跳。参数沿用旧卡名，调用方不用改。

    **等壳搭好再导航**：`connected` 事件早于 `boot()` 自己那一次导航（它会把 home / onboarding
    排进 `navChain`，晚到的写入会盖掉这里的目标——`open()` 里「更新的导航接管」那条判断）。
    直接抢跑会让工作区根本没挂上，探针后面全线误报，所以这里等 ⋯ 按钮出现、并允许重试。
    """
    pane = APP_PANES[card]
    await wait_true(cdp, "!!document.getElementById('u-more-btn')", timeout=60)
    for _ in range(3):
        await cdp.js(f"window.__uiApp && window.__uiApp.navigate({{pane:{json.dumps(pane)}}})")
        if await wait_true(cdp, f"window.__uiApp.probeState.route.pane==={json.dumps(pane)}", timeout=20):
            return True
        await asyncio.sleep(0.5)
    return False
