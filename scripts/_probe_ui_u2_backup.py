"""正式界面 U2 的备份验收（§9.1/§9.2）：单文件全量备份 → 校验 → 恢复。

跑法：.venv/Scripts/python.exe scripts/_probe_ui_u2_backup.py
日志：scripts/_u2probe_backup.log

看四件事（都在真壳里做，临时数据根 + 假模型）：

  1) 设置 → 数据与备份能打出一份单文件备份，列表里显示「完整」与大小；
  2) 「校验」给的是真结果（内容与清单一一对上）；
  3) 恢复的预检阶段不改数据；点「取消」后世界数量不变；
  4) 恢复走完：数据换回备份时点（被删掉的世界回来了），全部时间线暂停。
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
WORLD_NAME = "备份验收世界"
LOG = REPO / "scripts" / "_u2probe_backup.log"


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


async def visible_text(cdp: desk.Cdp, selector: str = "#u-main") -> str:
    value = await cdp.js(
        "(()=>{const n=document.querySelector(" + json.dumps(selector) + ");return n?(n.innerText||n.textContent||''):'';})()"
    )
    return str(value or "")


async def click_row(cdp: desk.Cdp, name: str, label: str) -> str:
    expr = (
        "(()=>{const rows=[...document.querySelectorAll('tr,.u-row-line')];"
        "const row=rows.find(r=>(r.textContent||'').includes("
        + json.dumps(name)
        + "));if(!row){return 'no-row';}"
        "const hit=[...row.querySelectorAll('button,a')].find(b=>(b.textContent||'').trim()==="
        + json.dumps(label)
        + ");if(!hit){return 'no-button';}hit.click();return 'ok';})()"
    )
    return str(await cdp.js(expr))


async def dialog_action(cdp: desk.Cdp, label: str) -> str:
    expr = (
        "(()=>{const boxes=[...document.querySelectorAll('.u-dialog')];if(!boxes.length){return 'no-dialog';}"
        "const box=boxes[boxes.length-1];"
        "const hit=[...box.querySelectorAll('button')].find(b=>(b.textContent||'').trim()==="
        + json.dumps(label)
        + ");if(!hit){return 'no-button';}hit.click();return 'ok';})()"
    )
    return str(await cdp.js(expr))


async def set_dialog_input(cdp: desk.Cdp, value: str) -> bool:
    expr = (
        "(()=>{const boxes=[...document.querySelectorAll('.u-dialog')];if(!boxes.length){return false;}"
        "const box=boxes[boxes.length-1];const input=box.querySelector('input.u-input');if(!input){return false;}"
        "input.value="
        + json.dumps(value)
        + ";input.dispatchEvent(new Event('input',{bubbles:true}));return true;})()"
    )
    return bool(await cdp.js(expr))


async def seed(root: Path) -> dict:
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
    info = create_instance(store, package, [example_card(package, name="堤禾"), example_card(package, name="潮生")])
    instance_id = str(info["id"])
    timeline_id = str(store.timeline_list(instance_id)[0]["id"])
    world.ensure_instance(instance_id, now_real=time.time())
    world.activate(instance_id, timeline_id, now_real=time.time())
    (root / "packages" / "draft.md").parent.mkdir(parents=True, exist_ok=True)
    (root / "packages" / "draft.md").write_text("素材也会进备份", encoding="utf-8")
    return {"instance_id": instance_id, "timeline_id": timeline_id}


def db_state(root: Path) -> dict:
    """直接读库：世界数与全部线状态（判据不靠界面自述）。"""
    from isekai_core.config import load_config
    from isekai_core.store import Store

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    try:
        instances = store.instance_list()
        states: list[str] = []
        for item in instances:
            states.extend(str(row["state"]) for row in store.timeline_list(str(item["id"])))
        return {"worlds": len(instances), "names": sorted(str(i["name"]) for i in instances), "timeline_states": sorted(set(states))}
    finally:
        store.close()


async def main() -> None:
    root = desk.make_root("uiu2b")
    (root / "examples").mkdir(parents=True, exist_ok=True)
    seeded = await seed(root)
    before = db_state(root)
    print("临时根:", root)
    print("播种:", json.dumps(before, ensure_ascii=False))

    proc, cdp, _targets = await desk.boot_shell(
        root,
        port=free_port(),
        env_extra={"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": FAKE_REPLY},
    )
    problems: list[str] = []
    seen: list[str] = []

    def say(tag: str, payload: object) -> None:
        text = f"{tag} {json.dumps(payload, ensure_ascii=False)}"
        print(text)
        seen.append(text)

    try:
        await cdp.js(desk.STUB)
        if not await wait_true(cdp, "!!(window.__uiApp && window.__uiApp.probeState.connected)"):
            problems.append("正式界面没有连上核心")
            raise SystemExit("止损：界面没连上核心")

        # ---------------- 1) 打一份备份 ----------------
        if not await click_text(cdp, "nav.u-nav button", "设置"):
            problems.append("侧栏没有「设置」入口")
        await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('数据与备份')")
        await click_text(cdp, "#u-main button", "立即备份全部数据")
        made = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('备份完成')")
        settings_text = await visible_text(cdp)
        if not made:
            problems.append(f"没有打出备份：{settings_text[-220:]}")
        if not made or "完整" not in settings_text:
            problems.append("列表里没有给出完整性状态")
        say("备份", {"完成": made, "片段": settings_text[-160:]})

        # ---------------- 2) 校验 ----------------
        await click_text(cdp, "#u-main button", "校验")
        verified = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('这份备份完整')")
        if not verified:
            problems.append(f"校验没有给通过的结果：{(await visible_text(cdp))[-200:]}")
        say("校验", {"通过": verified})

        # ---------------- 3) 恢复预检：取消不改数据 ----------------
        if not await click_text(cdp, "#u-main button", "恢复…"):
            problems.append("备份行里没有恢复入口")
        staged = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('替换当前数据')")
        if not staged:
            problems.append(f"预检没有给出替换范围：{(await visible_text(cdp))[-220:]}")
        await click_text(cdp, "#u-main button", "取消（不改动数据）")
        await asyncio.sleep(0.8)
        cancelled = db_state(root)
        if cancelled["worlds"] != before["worlds"]:
            problems.append(f"预检+取消不该动数据：{before} → {cancelled}")
        say("预检取消", cancelled)

        # ---------------- 4) 造差异，再真恢复 ----------------
        await click_text(cdp, "nav.u-nav button", "世界与素材")
        await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('删除')")
        # 备份是在这个世界之后打的：删掉它，恢复后应当回来
        picked_delete = await click_row(cdp, WORLD_NAME, "删除")
        if picked_delete != "ok":
            problems.append(f"世界行里没有删除入口：{picked_delete}")
        await wait_true(cdp, "!!document.querySelector('.u-dialog')")
        await set_dialog_input(cdp, WORLD_NAME)
        confirmed_delete = await dialog_action(cdp, "删除这个世界")
        if confirmed_delete != "ok":
            problems.append(f"删除世界的确认按钮没点到：{confirmed_delete}")
        gone = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('已删除')", timeout=40)
        after_delete = db_state(root)
        if not gone or after_delete["worlds"] != 0:
            problems.append(f"没有造出差异（删除世界失败）：{after_delete}")
        say("删除世界", after_delete)

        await click_text(cdp, "nav.u-nav button", "设置")
        await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('数据与备份')")
        await click_text(cdp, "#u-main button", "恢复…")
        await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('替换当前数据')")
        typed = await cdp.js(
            "(()=>{const boxes=[...document.querySelectorAll('#u-main input.u-input')];"
            "const hit=boxes.find(b=>(b.placeholder||'').includes('RESTORE'));if(!hit){return false;}"
            "hit.value='RESTORE';hit.dispatchEvent(new Event('input',{bubbles:true}));return true;})()"
        )
        if not typed:
            problems.append("恢复确认框没找到")
        await click_text(cdp, "#u-main button", "保留当前数据并恢复")
        restored = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('恢复完成')", timeout=120)
        after_restore = db_state(root)
        if not restored:
            problems.append(f"恢复没有走完：{(await visible_text(cdp))[-260:]}")
        if after_restore["worlds"] != before["worlds"]:
            problems.append(f"恢复后世界数应当回到备份时点：{before} → {after_restore}")
        if after_restore["timeline_states"] not in (["frozen"], []):
            problems.append(f"恢复后所有线应当暂停：{after_restore}")
        if not (root / "packages" / "draft.md").read_text(encoding="utf-8") == "素材也会进备份":
            problems.append("素材没有随恢复回来")
        say("恢复", after_restore)
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
    print("\n".join(summary))
    print("日志:", LOG)
    raise SystemExit(0 if not problems else 1)


if __name__ == "__main__":
    asyncio.run(main())
