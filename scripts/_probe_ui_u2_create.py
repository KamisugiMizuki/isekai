"""创建世界（界面 U2 §5.2/§5.3）的真壳验收：临时数据根 + 假模型起草。

跑法：.venv/Scripts/python.exe scripts/_probe_ui_u2_create.py
日志：scripts/_u2probe_create.log

假模型返回的是随发行样例那份世界设定，所以「AI 起草」这条链路是真的走完的
（解析 → 吸收候选 → 校验 → 编辑 → 保存）。看六件事：

  1) 来源页给出四种来源，AI 起草这条路能进世界设定工作区；
  2) 起草后有分区目录与条目，校验给出真实结果；
  3) 条目能在表单里改（不是 JSON 弹框）：改名称、锁定、加一条都落到候选上；
  4) 「确认世界设定」把这份设定存进创作目录（素材文件名由应用给）；
  5) 准备角色只用已确认的角色卡；检查页给创建摘要；
  6) 创建世界：世界里出现这个世界、时间线先暂停，且**用户改过的那条内容**确实进了新世界。
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

SAMPLE = REPO / "examples" / "sample_world"
# 假模型照着样例那份设定回答：起草结果是一份能通过校验的真设定
FAKE_REPLY = (SAMPLE / "huichao.json").read_text(encoding="utf-8")
WORLD_NAME = "运河世界"
BRIEF = "一个靠运河吃饭的城邦，外面是越来越深的潮"
EDITED = "运河是命脉（我改过这条）"
LOG = REPO / "scripts" / "_u2probe_create.log"


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
        + "const hit=nodes.find(n=>(n.textContent||'').includes("
        + json.dumps(text)
        + "));if(!hit){return false;}hit.click();return true;})()"
    )
    return bool(await cdp.js(expr))


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


async def click_last(cdp: desk.Cdp, selector: str, text: str) -> bool:
    """点最后一个含这段文字的按钮（页面动作 vs 顶部步骤条：同名时要点页面的那个）"""
    expr = (
        "(()=>{const nodes=[...document.querySelectorAll("
        + json.dumps(selector)
        + ")].filter(n=>n.offsetParent!==null&&!n.disabled);"
        "const hits=nodes.filter(n=>(n.textContent||'').includes("
        + json.dumps(text)
        + "));if(!hits.length){return false;}hits[hits.length-1].click();return true;})()"
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


async def set_field(cdp: desk.Cdp, container: str, label_text: str, value: str) -> bool:
    """在指定容器里按字段名填值（表单是 label + 控件，不靠下标猜）"""
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


async def visible_text(cdp: desk.Cdp, selector: str = "#u-main") -> str:
    value = await cdp.js(
        "(()=>{const n=document.querySelector(" + json.dumps(selector) + ");return n?(n.innerText||n.textContent||''):'';})()"
    )
    return str(value or "")


async def row_button(cdp: desk.Cdp, needle: str, label: str) -> str:
    """在含 needle 的那一行里点 label 按钮"""
    expr = (
        "(()=>{const rows=[...document.querySelectorAll('.u-row-line')];"
        "const row=rows.find(r=>(r.textContent||'').includes("
        + json.dumps(needle)
        + "));if(!row){return 'no-row';}"
        "const hit=[...row.querySelectorAll('button,a')].find(b=>(b.textContent||'').trim()==="
        + json.dumps(label)
        + ");if(!hit){return 'no-button';}hit.click();return 'ok';})()"
    )
    return str(await cdp.js(expr))


async def row_button_in(cdp: desk.Cdp, container: str, needle: str, label: str) -> str:
    """在指定容器里、含 needle 的那一行点 label 按钮"""
    expr = (
        "(()=>{const box=document.querySelector("
        + json.dumps(container)
        + ");if(!box){return 'no-box';}const rows=[...box.querySelectorAll('.u-row-line')];"
        "const row=rows.find(r=>(r.textContent||'').includes("
        + json.dumps(needle)
        + "));if(!row){return 'no-row';}"
        "const hit=[...row.querySelectorAll('button,a')].find(b=>(b.textContent||'').trim()==="
        + json.dumps(label)
        + ");if(!hit){return 'no-button';}hit.click();return 'ok';})()"
    )
    return str(await cdp.js(expr))


async def entry_button(cdp: desk.Cdp, index: int, label: str) -> str:
    expr = (
        "(()=>{const rows=[...document.querySelectorAll('.u-create-entries .u-row-line')];"
        "const row=rows["
        + str(int(index))
        + "];if(!row){return 'no-row';}"
        "const hit=[...row.querySelectorAll('button')].find(b=>(b.textContent||'').trim()==="
        + json.dumps(label)
        + ");if(!hit){return 'no-button';}hit.click();return 'ok';})()"
    )
    return str(await cdp.js(expr))


async def section_count(cdp: desk.Cdp, section_label: str) -> int:
    """分区目录里那个分区的条数（读数，不猜）"""
    expr = (
        "(()=>{const rows=[...document.querySelectorAll('.u-create-catalog .u-row-line')];"
        "const row=rows.find(r=>(r.textContent||'').includes("
        + json.dumps(section_label)
        + "));if(!row){return -1;}const m=(row.textContent||'').match(/(\\d+)\\s*条/);return m?Number(m[1]):-2;})()"
    )
    return int(await cdp.js(expr))


async def seed(root: Path) -> None:
    """装一份随发行样例：角色卡是已确认的，创建时可以直接选。"""
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
        out = {"worlds": len(instances), "names": sorted(str(i["name"]) for i in instances), "states": []}
        for item in instances:
            out["states"].extend(str(row["state"]) for row in store.timeline_list(str(item["id"])))
        out["states"] = sorted(set(out["states"]))
        out["files"] = sorted(p.name for p in Path(cfg.paths.packages).glob("*.json"))
        return out
    finally:
        store.close()


async def main() -> None:
    root = desk.make_root("uiu2c")
    await seed(root)
    print("临时根:", root)
    print("播种:", json.dumps(world_state(root), ensure_ascii=False))

    # 清掉上一次探针留下的壳：单实例插件会把新进程交给旧实例（旧实例的窗口/数据根都不对）
    desk.kill_tree()
    await asyncio.sleep(1.0)
    port = free_port()
    print("调试端口:", port, flush=True)
    proc, cdp, _targets = await desk.boot_shell(
        root,
        port=port,
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

        # ---------------- 1) 来源 ----------------
        await nav.open_menu(cdp, "世界管理")
        await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('世界')")
        await asyncio.sleep(1.2)
        print("· route:", await cdp.js("JSON.stringify(window.__uiApp.probeState.route)"), flush=True)
        clicked = await click_text(cdp, "#u-main button", "创建自己的世界")
        if not clicked:
            diag = await cdp.js(
                "JSON.stringify([...document.querySelectorAll('#u-main button')].map(b=>[b.textContent.slice(0,14),b.disabled,b.offsetParent!==null]))"
            )
            problems.append(f"世界与素材里没有「创建自己的世界」入口（按钮：{diag}）")
        await asyncio.sleep(1.5)
        print(
            "· 点了入口:",
            clicked,
            "| route:",
            await cdp.js("JSON.stringify(window.__uiApp.probeState.route)"),
            flush=True,
        )
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('让 AI 起草')"):
            problems.append(f"没有进到来源页：{(await visible_text(cdp))[:160]}")
        sources = await visible_text(cdp)
        for need in ("让 AI 起草", "自己填写", "用样例设定", "导入已有设定"):
            if need not in sources:
                problems.append(f"来源页缺：{need}")
        say("来源", sources[:120])

        picked = await click_card(cdp, "让 AI 起草")
        if picked != "ok":
            problems.append(f"点不开「让 AI 起草」：{picked}")
        if not await wait_true(cdp, "!!document.querySelector('#u-create-name')"):
            problems.append("世界设定工作区没有打开")

        # ---------------- 2) 起草 ----------------
        await set_value(cdp, "#u-create-name", WORLD_NAME)
        await set_value(cdp, "#u-create-brief", BRIEF)
        if not await click_text(cdp, "#u-main button", "让 AI 起草"):
            problems.append("世界设定页没有「让 AI 起草」按钮")
        drafted = await wait_true(
            cdp,
            "(()=>{const t=document.querySelector('#u-main').innerText;return t.includes('分区目录')&&!t.includes('正在起草');})()",
            timeout=180,
        )
        after_draft = await visible_text(cdp)
        if not drafted:
            problems.append(f"起草没有完成：{after_draft[-220:]}")
        for need in ("分区目录", "检查与预览", "锁定 = AI 不覆盖", "更多参数"):
            if need not in after_draft:
                problems.append(f"世界设定工作区缺：{need}")
        if "还有" in after_draft and "项需要处理" in after_draft:
            problems.append(f"样例那份设定本该通过校验：{after_draft[-260:]}")
        say("起草", after_draft[:200])

        # ---------------- 3) 条目表单：改一条并锁定 ----------------
        opened = await row_button_in(cdp, ".u-create-catalog", "世界公理", "打开")
        if opened != "ok":
            opened = await row_button_in(cdp, ".u-create-catalog", "世界公理", "在编辑")
        if opened != "ok":
            problems.append(f"分区目录里打不开「世界公理」：{opened}")
        await asyncio.sleep(0.4)
        picked_entry = await entry_button(cdp, 0, "编辑")
        if picked_entry != "ok":
            problems.append(f"条目行里点不开编辑：{picked_entry}")
        await wait_true(cdp, "!!document.querySelector('.u-create-form')")
        typed = await set_field(cdp, ".u-create-form", "内容", EDITED)
        if not typed:
            problems.append("条目表单里没有「内容」字段")
        locked = await click_text(cdp, ".u-create-form button", "锁定此条")
        if not locked:
            problems.append("条目表单里没有锁定入口")
        form_text = await visible_text(cdp, ".u-create-form")
        if "已锁定" not in form_text:
            problems.append(f"锁定后没有给出状态：{form_text[:160]}")
        say("编辑条目", {"改字段": typed, "锁定": locked})

        # ---------------- 4) 加一条 + 确认设定 ----------------
        before = await section_count(cdp, "世界公理")
        await click_text(cdp, ".u-create-entries button", "添加一条")
        await asyncio.sleep(0.5)
        after = await section_count(cdp, "世界公理")
        if before < 0 or after != before + 1:
            problems.append(f"「添加一条」没有落到分区上：{before} → {after}")
        # 新加的那条会立刻被选中：填上内容（空条目本来就该被校验拦下）
        filled_new = await set_field(cdp, ".u-create-form", "内容", "新加的一条公理：潮水每年退一尺")
        if not filled_new:
            problems.append("新加的条目没有打开可填的表单")
        await click_text(cdp, "#u-main button", "确认世界设定，去选角色")
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('选择这一局的角色')"):
            problems.append(f"确认设定后没有进到准备角色：{(await visible_text(cdp))[-220:]}")
        state = world_state(root)
        if not any(name.startswith(WORLD_NAME) for name in state["files"]):
            problems.append(f"确认设定没有把设定存进创作目录：{state['files']}")
        say("确认设定", state["files"])

        # ---------------- 5) 准备角色 → 检查 ----------------
        # 一次勾一张（勾选会重画这一页，循环里拿到的旧节点会失效）
        total_cards = int(await cdp.js("document.querySelectorAll('#u-main input[type=checkbox]').length"))
        for index in range(total_cards):
            await cdp.js(
                "(()=>{const boxes=[...document.querySelectorAll('#u-main input[type=checkbox]')];"
                f"const box=boxes[{index}];if(box&&!box.checked){{box.click();return true;}}return false;}})()"
            )
            await asyncio.sleep(0.4)
        cards_text = await visible_text(cdp)
        if not total_cards:
            problems.append("准备角色页没有可勾选的角色卡")
        if cards_text.count("huichao.card") and "已选" not in cards_text:
            problems.append(f"勾选后没有给出已选清单：{cards_text[-160:]}")
        if "可用于创建" not in cards_text:
            problems.append("角色卡没有给出「可用于创建」状态")
        await click_last(cdp, "#u-main button", "去检查与确认")
        review = await visible_text(cdp)
        for need in ("创建摘要", "设定来源", "校验"):
            if need not in review:
                problems.append(f"检查页缺：{need}")
        say("检查", review[:200])

        # ---------------- 6) 创建世界（先暂停） ----------------
        if not await click_last(cdp, "#u-main button", "创建世界"):
            problems.append("检查页没有「创建世界」这个动作")
        if not await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('只创建，暂不运行')", timeout=20):
            diag = await cdp.js(
                "JSON.stringify({crumbs:[...document.querySelectorAll('.u-crumbs button')].map(b=>[b.textContent,b.disabled]),"
                "buttons:[...document.querySelectorAll('#u-main button')].map(b=>b.textContent.slice(0,18))})"
            )
            problems.append(f"没有进到创建这一步：{diag}")
        if not await click_last(cdp, "#u-main button", "只创建，暂不运行"):
            problems.append("创建页没有「只创建，暂不运行」")
        created = await wait_true(cdp, "document.querySelector('#u-main').innerText.includes('已经建好')", timeout=90)
        final = world_state(root)
        if not created:
            problems.append(f"创建没有走到结果页：{(await visible_text(cdp))[-260:]}")
        if final["worlds"] != 1:
            problems.append(f"世界里应当出现这个世界：{final}")
        if final["states"] != ["frozen"]:
            problems.append(f"「只创建」的时间线要先暂停：{final['states']}")
        say("创建", final)

        # 用户改过的那条内容确实进了新世界（读落盘的世界设定）
        saved = sorted(Path(root / "packages").glob(f"*{WORLD_NAME}*.json"))
        text = saved[0].read_text(encoding="utf-8") if saved else ""
        if EDITED not in text:
            problems.append("用户改过的内容没有进到保存的设定里")
        if text and "#" in text:
            problems.append("保存的设定看着不像真 JSON")
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
