"""正式界面 U2（时间线管理 / 尝试世界变化）的真壳验收：临时数据根 + 假模型。

跑法：.venv/Scripts/python.exe scripts/_probe_ui_u2.py
日志：scripts/_u2probe.log（跑完读这个文件，内有每一步读数）

看四件事（都打在可观察行为上）：

  1) 世界详情的「时间线」块给出运行状态与操作（启动 / 暂停 / 改名 / 归档 / 删除）；
  2) 「尝试世界变化」：描述 + 已登记对象（按名称）+ 变化种类 + 持续方式 → 草案；
     草案阶段世界**没有**任何变化；
  3) 确认草案 → 从来源版本另开一条新线（默认暂停），原线不动；
  4) 删除时间线要键入名称确认（对不上就取消，对上才删）；归档后从常用列表隐藏、开关能看回来。
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
import _ui_nav_common as nav  # noqa: E402

FAKE_REPLY = '{"ok": true}'
WORLD_NAME = "验收世界"
LINE_NAME = "用户引入：堤上事务"
LOG = REPO / "scripts" / "_u2probe.log"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_true(cdp: desk.Cdp, expr: str, *, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await cdp.js(expr):
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.25)
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
    """在含 name 的那一行里点 label 按钮（比全局找第一个「打开」稳）。"""
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
    """点最后一个对话框里的按钮；返回 'ok' / 'no-dialog' / 'no-button'。"""
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
    """真链路造一个世界：两张角色卡 + 一条已启动的主线（不经过导入向导，省时间）。"""
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
    first = example_card(package, name="堤禾")
    second = example_card(package, name="潮生")
    info = create_instance(store, package, [first, second])
    instance_id = str(info["id"])
    timeline_id = str(store.timeline_list(instance_id)[0]["id"])
    world.ensure_instance(instance_id, now_real=time.time())
    world.activate(instance_id, timeline_id, now_real=time.time())
    return {
        "instance_id": instance_id,
        "timeline_id": timeline_id,
        "cards": {"堤禾": str(first["meta"]["card_id"]), "潮生": str(second["meta"]["card_id"])},
    }


def timeline_rows(root: Path, instance_id: str) -> list[dict]:
    from isekai_core.config import load_config
    from isekai_core.store import Store

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    try:
        return [
            {"id": str(item["id"]), "name": str(item["name"]), "state": str(item["state"])}
            for item in store.timeline_list(instance_id)
        ]
    finally:
        store.close()


async def main() -> None:
    root = desk.make_root("uiu2")
    (root / "examples").mkdir(parents=True, exist_ok=True)
    seeded = await seed(root)
    print("临时根:", root)
    print("播种:", seeded["instance_id"], "线:", seeded["timeline_id"])

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

        # ---------------- 1) 世界详情：时间线块 ----------------
        if not await nav.open_menu(cdp, "世界管理"):
            problems.append("⋯ 菜单打不开「世界管理」")
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('验收世界')"):
            problems.append("世界里没有列出刚建的世界")
        else:
            opened = await click_row(cdp, WORLD_NAME, "打开")
            if opened != "ok":
                problems.append(f"世界列表里点不开这一行：{opened}")
            # 列表页也有「时间线」这一列，必须等详情页独有的元素出现
            if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('返回世界列表')"):
                problems.append("世界详情没有打开")
        detail = await visible_text(cdp)
        for need in ("运行中", "改名", "归档", "删除…", "尝试世界变化"):
            if need not in detail:
                problems.append(f"世界详情缺：{need}")
        say("详情", detail[:160])

        # ---------------- 2) 尝试世界变化：草案不改世界 ----------------
        await click_text(cdp, "#u-main button", "尝试世界变化")
        if not await wait_true(cdp, "!!document.querySelector('#u-change-intent')"):
            problems.append("没有打开「尝试世界变化」表单")
        await set_value(cdp, "#u-change-intent", "让堤禾这几天走不开")
        picked = await click_text(cdp, "#u-change-target option", "堤禾")
        kinds = await visible_text(cdp, "#u-change-kind")
        await click_text(cdp, "#u-change-kind option", "活动受限")
        await set_value(cdp, "#u-change-value", "堤上事务缠身，这几日走不开")
        picked_kind = await cdp.js("document.querySelector('#u-change-kind') ? document.querySelector('#u-change-kind').value : ''")
        if not picked:
            problems.append("「改变对象」里没有已登记的角色")
        if "活动受限" not in kinds:
            problems.append(f"「变化种类」没有给可读名称：{kinds[:120]}")
        before = timeline_rows(root, seeded["instance_id"])
        await click_text(cdp, "#u-main button", "查看草案")
        got_draft = await wait_true(
            cdp, "document.querySelector('#u-main').innerText.includes('草案编号')", timeout=60
        )
        draft_view = await visible_text(cdp)
        after_draft = timeline_rows(root, seeded["instance_id"])
        if not got_draft:
            problems.append(f"没有生成草案：{draft_view[-220:]}")
        elif "活动受限" not in draft_view or "让堤禾这几天走不开" not in draft_view:
            problems.append(f"草案没有列出意图与可执行内容：{draft_view[-260:]}")
        if len(after_draft) != len(before):
            problems.append(f"草案阶段不该建线：{before} → {after_draft}")
        say("草案", {"种类值": picked_kind, "时间线数": len(after_draft), "有草案": got_draft})

        # ---------------- 3) 确认：另开一条暂停的新线 ----------------
        lines = after_draft
        if got_draft:
            await set_value(cdp, "#u-change-name", LINE_NAME)
            await click_text(cdp, "#u-main button", "确认并新建时间线")
            arrived = await wait_true(
                cdp,
                "(()=>{const t=document.querySelector('#u-main').innerText;return t.includes('已归档')||t.includes('已暂停');})()",
                timeout=60,
            )
            lines = timeline_rows(root, seeded["instance_id"])
            new_lines = [item for item in lines if item["id"] != seeded["timeline_id"]]
            detail_after = await visible_text(cdp)
            if not arrived:
                problems.append(f"确认后没有回到世界详情：{detail_after[-220:]}")
            if len(new_lines) != 1:
                problems.append(f"确认后应恰好新增一条线：{lines}")
            elif new_lines[0]["state"] != "frozen":
                problems.append(f"新线先暂停：{new_lines[0]}")
            elif new_lines[0]["name"] != LINE_NAME:
                problems.append(f"新线名称应取自表单：{new_lines[0]}")
            say("确认", lines)

        # ---------------- 4) 删除：名称对不上就取消，对上才删 ----------------
        opened_dialog = await click_row(cdp, LINE_NAME, "删除…")
        if opened_dialog != "ok" or not await wait_true(cdp, "!!document.querySelector('.u-dialog')"):
            problems.append(f"删除没有要求键入名称确认（{opened_dialog}）")
        else:
            await set_dialog_input(cdp, "随便写个不对的名字")
            mismatch = await dialog_action(cdp, "删除这条线")
            await asyncio.sleep(0.8)
            still = timeline_rows(root, seeded["instance_id"])
            if len(still) != len(lines):
                problems.append(f"名称对不上却删掉了：{still}")
            say("删除·名称不符", {"动作": mismatch, "时间线": still})

            again = await click_row(cdp, LINE_NAME, "删除…")
            await wait_true(cdp, "!!document.querySelector('.u-dialog')")
            await set_dialog_input(cdp, LINE_NAME)
            matched = await dialog_action(cdp, "删除这条线")
            gone = await wait_true(
                cdp, "document.querySelector('#u-main').innerText.includes('已删除时间线')", timeout=30
            )
            if not gone:
                problems.append(f"名称对上没有删除（{again}/{matched}）：{(await visible_text(cdp))[-200:]}")
            say("删除·名称相符", {"动作": matched, "已删": gone})
        remaining = timeline_rows(root, seeded["instance_id"])
        if len(remaining) != 1:
            problems.append(f"删除后应只剩主线：{remaining}")
        say("删除后", remaining)

        # ---------------- 5) 归档：从常用列表隐藏，开关能看回来 ----------------
        await click_row(cdp, "初始时间线", "归档")
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('显示已归档')"):
            problems.append("归档后没有出现「显示已归档」开关")
        hidden = await visible_text(cdp)
        if "运行中" in hidden or "已暂停" in hidden:
            problems.append("归档后不该继续占着常用列表")
        await click_text(cdp, "#u-main button", "显示已归档")
        shown = await wait_true(cdp, "!!document.querySelector('#u-main .u-row-line')")
        archived = await visible_text(cdp)
        if not shown or "已归档" not in archived:
            problems.append(f"打开开关后看不到已归档的线：{archived[-200:]}")
        still_offered = await cdp.js(
            "[...document.querySelectorAll('#u-main button')].filter(n=>n.textContent.trim()==='归档').length"
        )
        if still_offered:
            problems.append("已归档的线不该再给归档按钮")
        state = timeline_rows(root, seeded["instance_id"])
        if state and state[0]["state"] != "archived":
            problems.append(f"归档状态没有落库：{state}")
        say("归档", state)
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
