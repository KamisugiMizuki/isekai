"""从旧开发目录迁移的界面验收（§3.2）：临时数据根 + 假模型。

跑法：.venv/Scripts/python.exe scripts/_probe_ui_u2_migrate.py
日志：scripts/_u2probe_migrate.log

真实场景：先造一个「旧机器上的数据目录」（里面有世界与素材），再让当前这台的界面
把它整份搬进来。看四件事：

  1) 首次设置的「本机检查」这一步就给出迁移入口（不必懂命令行）；
  2) 填一个不是数据目录的路径：检查说清原因，**当前数据不动**；
  3) 填真目录：检查给出世界数 / 素材数 / 要搬的大小，且提示源目录保留、凭据不搬；
  4) 确认迁移 → 世界与素材都过来、时间线全部暂停、源目录原样留在那里；
     目标和源都不在有用户资产时被拒的路径上也验证一次说法。
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

FAKE_REPLY = '{"ok": true}'
LOG = REPO / "scripts" / "_u2probe_migrate.log"


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


async def click_when(cdp: desk.Cdp, selector: str, text: str, *, timeout: float = 30.0) -> bool:
    expr = (
        "(()=>{const nodes=[...document.querySelectorAll("
        + json.dumps(selector)
        + ")].filter(n=>n.offsetParent!==null&&!n.disabled);"
        "const hits=nodes.filter(n=>(n.textContent||'').includes("
        + json.dumps(text)
        + "));if(!hits.length){return false;}hits[hits.length-1].click();return true;})()"
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await cdp.js(expr):
            return True
        await asyncio.sleep(0.3)
    return False


async def set_value(cdp: desk.Cdp, selector: str, value: str) -> bool:
    expr = (
        "(()=>{const n=document.querySelector("
        + json.dumps(selector)
        + ");if(!n){return false;}n.value="
        + json.dumps(value)
        + ";n.dispatchEvent(new Event('input',{bubbles:true}));return true;})()"
    )
    return bool(await cdp.js(expr))


async def visible_text(cdp: desk.Cdp, selector: str = "#u-main") -> str:
    value = await cdp.js(
        "(()=>{const n=document.querySelector(" + json.dumps(selector) + ");return n?(n.innerText||n.textContent||''):'';})()"
    )
    return str(value or "")


async def make_old_root(root: Path) -> None:
    """造一份「旧机器上的数据」：一个世界 + 一条素材。"""
    from isekai_core.config import load_config
    from isekai_core.runtime.service import from_config
    from isekai_core.store import Store
    from isekai_core.world.example import example_card, example_package
    from isekai_core.world.instances import create_instance

    old = root / "old_root"
    cfg = load_config(old)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    runtime = from_config(cfg, store)
    package = example_package("旧机器上的世界")
    info = create_instance(store, package, [example_card(package, name="堤禾")])
    runtime.ensure_instance(info["id"], now_real=time.time())
    timeline = store.timeline_list(info["id"])[0]["id"]
    runtime.activate(info["id"], timeline, now_real=time.time())
    (cfg.paths.packages / "旧素材.md").parent.mkdir(parents=True, exist_ok=True)
    (cfg.paths.packages / "旧素材.md").write_text("从旧机器带过来的素材", encoding="utf-8")
    # 旧根里有一份带密钥的配置：迁移只搬非敏感偏好
    cfg.paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_file.write_text("llm:\n  api_key: sk-old-not-for-travel\n", encoding="utf-8")
    store.close()


def current_state(root: Path) -> dict:
    from isekai_core.config import load_config
    from isekai_core.store import Store

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()  # 当前数据根可能还没建库（第一次启动前）
    try:
        instances = store.instance_list()
        out: dict = {
            "worlds": len(instances),
            "names": sorted(str(item["name"]) for item in instances),
            "states": [],
            "assets": sorted(p.name for p in Path(cfg.paths.packages).glob("*")) if Path(cfg.paths.packages).is_dir() else [],
            "config_has_old_key": "sk-old-not-for-travel" in (
                cfg.paths.config_file.read_text(encoding="utf-8") if cfg.paths.config_file.is_file() else ""
            ),
        }
        for item in instances:
            out["states"].extend(str(row["state"]) for row in store.timeline_list(str(item["id"])))
        out["states"] = sorted(set(out["states"]))
        return out
    finally:
        store.close()


async def main() -> None:
    root = desk.make_root("uiu2mig")
    await make_old_root(root)
    old_root = root / "old_root"
    print("临时根:", root, flush=True)
    print("旧根:", old_root, flush=True)
    print("迁移前当前数据:", json.dumps(current_state(root), ensure_ascii=False), flush=True)

    desk.kill_tree()
    await asyncio.sleep(1.0)
    proc, cdp, _targets = await desk.boot_shell(
        root,
        port=free_port(),
        env_extra={"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": FAKE_REPLY},
    )
    problems: list[str] = []
    seen: list[str] = []

    def say(tag: str, payload: object) -> None:
        text = f"{tag} {json.dumps(payload, ensure_ascii=False)}"
        print(text, flush=True)
        seen.append(text)

    try:
        await cdp.js(desk.STUB)
        if not await wait_true(cdp, "!!(window.__uiApp && window.__uiApp.probeState.connected)"):
            problems.append("正式界面没有连上核心")
            raise SystemExit("止损：界面没连上核心")

        # ---------------- 1) 首次设置里就有入口 ----------------
        await cdp.js("window.__uiApp.navigate({pane:'onboarding'})")
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('从旧的开发目录迁移')"):
            problems.append("首次设置里没有迁移入口")
        card_text = await visible_text(cdp)
        for need in ("选择旧数据目录", "检查这个目录", "源目录始终保留"):
            if need not in card_text:
                problems.append(f"迁移入口缺：{need}")
        say("入口", card_text[:120])

        # ---------------- 2) 不是数据目录：说清原因，数据不动 ----------------
        await set_value(cdp, "#u-migrate-path", str(root / "examples"))
        await click_when(cdp, "#u-main button", "检查这个目录")
        await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('现在还不能迁移')")
        bad = await visible_text(cdp)
        if "data/isekai.db" not in bad:
            problems.append(f"检查没有说清「为什么不行」：{bad[-200:]}")
        if current_state(root)["worlds"] != 0:
            problems.append("检查阶段不该改动当前数据")
        say("坏路径", bad[-160:])

        # ---------------- 3) 真目录：检查给范围 ----------------
        await set_value(cdp, "#u-migrate-path", str(old_root))
        await click_when(cdp, "#u-main button", "检查这个目录")
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('现在还不能迁移')===false && document.querySelector('#u-main').innerText.includes('开始迁移')"):
            problems.append(f"检查真目录没有给结论：{(await visible_text(cdp))[-200:]}")
        good = await visible_text(cdp)
        for need in ("个世界", "个素材文件", "要搬的大小", "不带过来的", "所有时间线处于暂停"):
            if need not in good:
                problems.append(f"检查结果缺：{need}")
        say("真目录检查", good[-220:])

        # ---------------- 4) 迁移 ----------------
        if not await click_when(cdp, "#u-main button", "开始迁移"):
            problems.append(f"检查通过后没有「开始迁移」：{(await visible_text(cdp))[-160:]}")
        if not await wait_true(cdp, "!!document.querySelector('.u-dialog')", timeout=15):
            diag = await cdp.js(
                "JSON.stringify({dialogs:document.querySelectorAll('.u-dialog').length,"
                "buttons:[...document.querySelectorAll('#u-main button')].map(b=>b.textContent.slice(0,12)),"
                "note:(document.querySelector('#u-main .u-note')||{}).innerText})"
            )
            problems.append(f"开始迁移没有二次确认：{diag}")
        else:
            action = await click_when(cdp, ".u-dialog button", "开始迁移", timeout=10)
            print("· 对话框动作:", action, flush=True)
            if not action:
                problems.append("二次确认里没点到「开始迁移」")
        await asyncio.sleep(8)
        after_click = await cdp.js(
            "JSON.stringify({note:(document.querySelector('#u-main .u-note')||{}).innerText,"
            "done:document.querySelector('#u-main').innerText.includes('迁移完成'),"
            "progress:document.querySelector('#u-main').innerText.includes('正在迁移'),"
            "err:[...document.querySelectorAll('#u-main .u-error')].map(n=>n.innerText.slice(0,200))})"
        )
        print("· 点完之后:", after_click, flush=True)
        done = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('迁移完成')", timeout=180)
        state = current_state(root)
        if not done:
            problems.append(f"迁移没有完成：{(await visible_text(cdp))[-240:]}")
        if state["worlds"] != 1:
            problems.append(f"世界没有搬过来：{state}")
        if state["states"] != ["frozen"]:
            problems.append(f"迁移后时间线要全部暂停：{state['states']}")
        if "旧素材.md" not in state["assets"]:
            problems.append(f"素材没有搬过来：{state['assets']}")
        if state["config_has_old_key"]:
            problems.append("密钥不该跟着迁移过来")
        if not old_root.is_dir() or not (old_root / "data" / "isekai.db").is_file():
            problems.append("源目录必须保留")
        say("迁移后", state)
    finally:
        try:
            await proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            desk.kill_tree()
        except Exception:  # noqa: BLE001
            pass

    verdict = "PASS" if not problems else "FAIL"
    summary = [f"结果: {verdict}", *[f" - {item}" for item in problems]]
    seen.extend(summary)
    LOG.write_text("\n".join(seen) + "\n", encoding="utf-8")
    print("\n".join(summary), flush=True)
    print("日志:", LOG, flush=True)
    raise SystemExit(0 if not problems else 1)


if __name__ == "__main__":
    asyncio.run(main())
