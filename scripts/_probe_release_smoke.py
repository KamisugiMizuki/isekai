"""发行件冒烟（U5）：跑 release/isekai/isekai.exe，验数据根、自带解释器与首屏可用。

跑法：.venv/Scripts/python.exe scripts/_probe_release_smoke.py
前提：先跑过 scripts/build_release.py。

判据全是真实读数：数据根里出现 `isekai.db` / 随包样例；shell.log 里出现 `packaged=true`；
界面连上核心并且能从样例世界开一份；不碰本机真实数据根（把 LOCALAPPDATA 指到临时目录）。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import _audit2_desk as desk  # noqa: E402

PACKAGE = REPO / "release" / "isekai"
LOG = REPO / "scripts" / "_release_smoke.log"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_true(cdp: desk.Cdp, expr: str, *, timeout: float = 60.0) -> bool:
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
        "const hit=nodes.find(n=>(n.textContent||'').includes("
        + json.dumps(text)
        + "));if(!hit){return false;}hit.click();return true;})()"
    )
    return bool(await cdp.js(expr))


async def main() -> None:
    if not (PACKAGE / "isekai.exe").exists():
        raise SystemExit(f"没有发行件：{PACKAGE}（先跑 scripts/build_release.py）")
    data_root_parent = desk.make_root("u5pack")
    print("LOCALAPPDATA:", data_root_parent, flush=True)
    data_root = data_root_parent / "isekai"

    desk.kill_tree()
    await asyncio.sleep(1.0)
    port = free_port()
    env = dict(os.environ)
    env.update({
        "LOCALAPPDATA": str(data_root_parent),
        "PYTHONIOENCODING": "utf-8",
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={port}",
    })
    env.pop("ISEKAI_ROOT", None)
    env.pop("ISEKAI_PACKAGED", None)
    proc = __import__("subprocess").Popen([str(PACKAGE / "isekai.exe")], cwd=str(PACKAGE), env=env)
    problems: list[str] = []
    try:
        cdp, _targets = await desk.Cdp.attach(port, timeout=120)
        await cdp.js(desk.STUB)
        connected = await wait_true(cdp, "!!(window.__uiApp && window.__uiApp.probeState.connected)", timeout=120)
        if not connected:
            problems.append("界面没有连上核心（发行件里的自带解释器可能没起来）")

        # 数据根：程序目录只放程序，数据在用户目录
        # config/ 是首次写入设置时才建的：这里只查首次运行就该有的东西
        for name in ("data/isekai.db", "examples/sample_world", "logs/shell.log"):
            target = data_root / name
            if not target.exists():
                problems.append(f"数据根里缺 {name}")
        shell_log = (data_root / "logs" / "shell.log")
        text = shell_log.read_text(encoding="utf-8", errors="replace") if shell_log.exists() else ""
        if "packaged=true" not in text:
            problems.append("壳没有走发行件路径（shell.log 里没有 packaged=true）")
        if str(PACKAGE) in text and (data_root / "isekai.db").exists() is False:
            problems.append("壳把数据写进了程序目录")

        # 首屏 + 从样例世界开一份（真链路）
        steps: list[str] = []
        await click_text(cdp, "nav.u-nav button", "世界与素材")
        await asyncio.sleep(1.5)
        before = await cdp.js("(()=>{const n=document.querySelector('#u-main');return n?(n.innerText||'').slice(0,80):'';})()")
        steps.append(f"世界与素材：{before}")
        clicked = await click_text(cdp, "#u-main button", "从样例开始")
        steps.append(f"点「从样例开始」：{clicked}")
        await asyncio.sleep(2.0)
        after = await cdp.js("(()=>{const n=document.querySelector('#u-main');return n?(n.innerText||''):'';})()")
        steps.append(f"之后：{after}")
        route = str(await cdp.js("JSON.stringify(window.__uiApp.probeState.route)"))
        steps.append(f"route：{route}")
        if "onboarding" not in route:
            problems.append(f"点了「从样例开始」但没进首次设置流程（route={route}｜页面：{str(after)[:80]}）")
        print("\n".join(steps), flush=True)
    finally:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        desk.kill_tree()

    print("数据根内容:", sorted(item.name for item in data_root.iterdir()) if data_root.exists() else "（不存在）")
    if problems:
        print("\n".join(f"[FAIL] {item}" for item in problems))
        print(f"结论：FAIL（{len(problems)} 项）")
    else:
        print("结论：PASS（自带解释器 / 数据根在用户目录 / 随包样例到位 / 界面可用）")


if __name__ == "__main__":
    import contextlib

    with LOG.open("w", encoding="utf-8") as handle, contextlib.redirect_stdout(handle):
        asyncio.run(main())
    print(LOG.read_text(encoding="utf-8"))
