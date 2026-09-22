"""角色卡起草工作区（界面 U2 §5.2/§5.3）的真壳验收：临时数据根 + 假模型。

跑法：.venv/Scripts/python.exe scripts/_probe_ui_u2_card.py
日志：scripts/_u2probe_card.log

假模型返回的是随发行样例那张角色卡，所以「起草角色卡」这条路是真跑完的
（解析 → 吸收候选 → 校验 → 改字段 → 字段级锁定 → 重跑保持锁定 → 确认）。

看五件事：

  1) 世界设定可以走「用样例设定」这条路（不靠 AI 也进得了创作工作区）；
  2) 准备角色里能起草一张新卡：起草后有分组表单与校验结果；
  3) 表单改动与**字段级锁定**有效：重跑整卡后锁定字段保持手改值；
  4) 「确认角色卡」把卡落进创作目录并标成「可用于创建」，且自动被这一局选中；
  5) 用它创建世界：世界里出现，时间线先暂停，落盘的卡里就是手改后的值。
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

SAMPLE = REPO / "examples" / "sample_world"
FAKE_REPLY = (SAMPLE / "huichao.card1.json").read_text(encoding="utf-8")
WORLD_NAME = "角色卡验收世界"
CARD_NAME = "盐场学徒"
EDITED = "我看盐比看人准"
LOG = REPO / "scripts" / "_u2probe_card.log"


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


async def click_last(cdp: desk.Cdp, selector: str, text: str) -> bool:
    expr = (
        "(()=>{const nodes=[...document.querySelectorAll("
        + json.dumps(selector)
        + ")].filter(n=>n.offsetParent!==null&&!n.disabled);"
        "const hits=nodes.filter(n=>(n.textContent||'').includes("
        + json.dumps(text)
        + "));if(!hits.length){return false;}hits[hits.length-1].click();return true;})()"
    )
    return bool(await cdp.js(expr))


async def click_when(cdp: desk.Cdp, selector: str, text: str, *, timeout: float = 30.0) -> bool:
    """等按钮出现再点（页面渲染是异步的，直接点会扑空）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await click_last(cdp, selector, text):
            return True
        await asyncio.sleep(0.3)
    return False


async def click_card(cdp: desk.Cdp, title: str, label = "用这个") -> str:
    expr = (
        "(()=>{const cards=[...document.querySelectorAll('.u-card')];"
        "const card=cards.find(c=>(c.textContent||'').includes("
        + json.dumps(title)
        + "));if(!card){return 'no-card';}"
        "const hit=[...card.querySelectorAll('button')].find(b=>(b.textContent||'').includes("
        + json.dumps(label)
        + "));if(!hit){return 'no-button';}hit.click();return 'ok';})()"
    )
    return str(await cdp.js(expr))


async def set_value(cdp: desk.Cdp, selector: str, value: str) -> bool:
    expr = (
        "(()=>{const n=document.querySelector("
        + json.dumps(selector)
        + ");if(!n){return false;}n.value="
        + json.dumps(value)
        + ";n.dispatchEvent(new Event('input',{bubbles:true}));return true;})()"
    )
    return bool(await cdp.js(expr))


async def set_field(cdp: desk.Cdp, container: str, label_text: str, value: str) -> bool:
    expr = (
        "(()=>{const box=document.querySelector("
        + json.dumps(container)
        + ");if(!box){return false;}"
        "const fields=[...box.querySelectorAll('.u-field')];"
        "const hit=fields.find(f=>{const s=f.querySelector('.u-field-label');return s&&s.textContent.trim()==="
        + json.dumps(label_text)
        + ";});if(!hit){return false;}const ctl=hit.querySelector('input,textarea,select');if(!ctl){return false;}"
        "ctl.value="
        + json.dumps(value)
        + ";ctl.dispatchEvent(new Event('input',{bubbles:true}));ctl.dispatchEvent(new Event('change',{bubbles:true}));return true;})()"
    )
    return bool(await cdp.js(expr))


async def read_field(cdp: desk.Cdp, container: str, label_text: str) -> str:
    expr = (
        "(()=>{const box=document.querySelector("
        + json.dumps(container)
        + ");if(!box){return '<no-box>';}"
        "const fields=[...box.querySelectorAll('.u-field')];"
        "const hit=fields.find(f=>{const s=f.querySelector('.u-field-label');return s&&s.textContent.trim()==="
        + json.dumps(label_text)
        + ";});if(!hit){return '<no-field>';}const ctl=hit.querySelector('input,textarea,select');"
        "return ctl?String(ctl.value):'<no-ctl>';})()"
    )
    return str(await cdp.js(expr))


async def tick_lock(cdp: desk.Cdp, label_text: str) -> bool:
    """在「锁定字段」里勾上某个字段"""
    expr = (
        "(()=>{const boxes=[...document.querySelectorAll('.u-knobs .u-check')];"
        "const hit=boxes.find(b=>(b.textContent||'').trim()==="
        + json.dumps(label_text)
        + ");if(!hit){return false;}const tick=hit.querySelector('input');if(!tick)return false;"
        "if(!tick.checked)tick.click();return true;})()"
    )
    return bool(await cdp.js(expr))


async def visible_text(cdp: desk.Cdp, selector: str = "#u-main") -> str:
    value = await cdp.js(
        "(()=>{const n=document.querySelector(" + json.dumps(selector) + ");return n?(n.innerText||n.textContent||''):'';})()"
    )
    return str(value or "")


async def note(cdp: desk.Cdp, tag: str) -> None:
    try:
        route = await asyncio.wait_for(cdp.js("JSON.stringify(window.__uiApp ? window.__uiApp.probeState.route : null)"), 10)
        text = await asyncio.wait_for(cdp.js("(document.querySelector('#u-main')||{}).innerText?.slice(0,40)||''"), 10)
    except Exception:  # noqa: BLE001
        route, text = "<超时>", ""
    print(f"· {tag} | route={route} | {str(text)!r}", flush=True)


async def seed(root: Path) -> None:
    import shutil

    from isekai_core.config import load_config
    from isekai_core.onboarding import install_sample
    from isekai_core.store import Store

    (root / "examples").mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE, root / "examples" / "sample_world")
    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    try:
        install_sample(cfg, "sample_world", store=store)
    finally:
        store.close()


def world_state(root: Path) -> dict:
    from isekai_core.config import load_config
    from isekai_core.store import Store

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    try:
        instances = store.instance_list()
        out = {"worlds": len(instances), "states": [], "files": sorted(p.name for p in Path(cfg.paths.packages).glob("*.json"))}
        for item in instances:
            out["states"].extend(str(row["state"]) for row in store.timeline_list(str(item["id"])))
        out["states"] = sorted(set(out["states"]))
        return out
    finally:
        store.close()


async def main() -> None:
    root = desk.make_root("uiu2card")
    await seed(root)
    print("临时根:", root, flush=True)
    print("播种:", json.dumps(world_state(root), ensure_ascii=False), flush=True)

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

        # ---------------- 1) 用样例设定进工作区 ----------------
        await click_text(cdp, "nav.u-nav button", "世界与素材")
        await asyncio.sleep(1.2)
        if not await click_text(cdp, "#u-main button", "创建自己的世界"):
            problems.append("世界与素材里没有「创建自己的世界」入口")
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('让 AI 起草')"):
            problems.append(f"没有进到来源页：{(await visible_text(cdp))[:160]}")
        picked = await click_card(cdp, "用样例设定")
        if picked != "ok":
            problems.append(f"点不开「用样例设定」：{picked}")
        if not await wait_true(cdp, "!!document.querySelector('#u-create-name') && document.querySelector('#u-main').innerText.includes('分区目录')"):
            problems.append(f"样例设定没有载入工作区：{(await visible_text(cdp))[:200]}")
        await set_value(cdp, "#u-create-name", WORLD_NAME)
        await asyncio.sleep(0.4)
        await click_last(cdp, "#u-main button", "确认世界设定")
        # 顶栏步骤条里也有「准备角色」这四个字：等该页独有的文案才对
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('角色卡必须')", timeout=60):
            problems.append(f"确认设定后没有进到准备角色：{(await visible_text(cdp))[-200:]}")
        await note(cdp, "到准备角色")

        # ---------------- 2) 起草角色卡 ----------------
        if not await click_when(cdp, "#u-main button", "新建角色卡"):
            problems.append("准备角色页没有「新建角色卡（AI 起草）」")
        if not await wait_true(cdp, "!!document.querySelector('#u-card-name')"):
            problems.append(f"角色卡工作区没有打开：{(await visible_text(cdp))[-160:]}")
        await set_value(cdp, "#u-card-name", CARD_NAME)
        await set_value(cdp, "#u-card-brief", "盐场里长大的学徒，跟堤禾熟")
        if not await click_when(cdp, "#u-main button", "让 AI 起草"):
            problems.append("角色卡工作区没有「让 AI 起草」按钮")
        drafted = await wait_true(
            cdp,
            "(()=>{const t=document.querySelector('#u-main').innerText;return t.includes('角色内容')&&!t.includes('正在起草');})()",
            timeout=180,
        )
        card_view = await visible_text(cdp)
        if not drafted:
            problems.append(f"角色卡起草没有完成：{card_view[-220:]}")
        for need in ("身份", "来历", "角色内容", "锁定字段"):
            if need not in card_view:
                problems.append(f"角色卡工作区缺：{need}")
        say("起草角色卡", card_view[:160])

        # ---------------- 3) 字段级锁定：改一个字段再重跑 ----------------
        base_value = await read_field(cdp, ".u-card-form", "自我认同")
        typed = await set_field(cdp, ".u-card-form", "自我认同", EDITED)
        locked = await tick_lock(cdp, "自我认同")
        if not typed:
            problems.append(f"卡片表单里没有「自我认同」字段（读到 {base_value}）")
        if not locked:
            problems.append("锁定字段面板里没有「自我认同」")
        if not await click_when(cdp, "#u-main button", "重新起草"):
            problems.append("工作区没有「重新起草（整卡）」")
        await wait_true(cdp, "(()=>{const t=document.querySelector('#u-main').innerText;return !t.includes('正在起草');})()", timeout=180)
        kept = await read_field(cdp, ".u-card-form", "自我认同")
        if kept != EDITED:
            problems.append(f"锁定的字段被重跑覆盖了：{kept!r} ≠ {EDITED!r}")
        say("字段级锁定", {"改后": EDITED, "重跑后": kept})

        # ---------------- 4) 确认角色卡 ----------------
        if not await click_when(cdp, "#u-main button", "确认角色卡", timeout=60):
            problems.append("工作区没有「确认角色卡」")
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('选择这一局的角色')", timeout=60):
            problems.append(f"确认角色卡后没有回到角色列表：{(await visible_text(cdp))[-200:]}")
        cards_text = await visible_text(cdp)
        if f"{CARD_NAME}.card.json" not in cards_text or "可用于创建" not in cards_text:
            problems.append(f"新卡没有进列表或没标成可用：{cards_text[-220:]}")
        if "已选 1 张" not in cards_text and "已选" not in cards_text:
            problems.append("新确认的卡没有被自动选上")
        state = world_state(root)
        if not any(name.startswith(CARD_NAME) for name in state["files"]):
            problems.append(f"角色卡没有落进创作目录：{state['files']}")
        say("确认角色卡", state["files"])

        # ---------------- 5) 用它创建世界 ----------------
        await click_last(cdp, "#u-main button", "去检查与确认")
        await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('创建摘要')")
        await click_last(cdp, "#u-main button", "创建世界")
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('只创建，暂不运行')", timeout=20):
            problems.append("没有进到创建这一步")
        await click_last(cdp, "#u-main button", "只创建，暂不运行")
        created = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('已经建好')", timeout=90)
        final = world_state(root)
        if not created:
            problems.append(f"创建没有走到结果页：{(await visible_text(cdp))[-240:]}")
        if final["worlds"] != 1:
            problems.append(f"世界里应当出现这个世界：{final}")
        if final["states"] != ["frozen"]:
            problems.append(f"「只创建」的时间线要先暂停：{final['states']}")
        say("创建", final)

        saved_cards = sorted(Path(root / "packages").glob(f"{CARD_NAME}*.json"))
        text = saved_cards[0].read_text(encoding="utf-8") if saved_cards else ""
        if EDITED not in text:
            problems.append("落盘的角色卡里没有手改并锁定的那个值")
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
