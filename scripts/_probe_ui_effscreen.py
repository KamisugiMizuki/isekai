"""效率筛查改动的真壳验收（P0-7 流式增量预览 / P2-13 试演不落线 / P1-18 退出三选一通道）。

跑法：.venv/Scripts/python.exe scripts/_probe_ui_effscreen.py

三件事都打在**可观察行为**上：
  A. 联络：发送后先出现 `.u-bubble-preview` 增量预览气泡，固化回复到达后预览消失、正文只剩一份；
  B. 写作：带世界变化的候选 →「另开分支试演」→「得到试演预览（不建线）」后**时间线数量不变** →
     「应用于试演线」后才多出一条线；
  C. 退出：壳注册了 `quit_timeout_pending` / `quit_decision` 两个命令（页面侧可调），
     且默认不处于「等待用户选择」状态。
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
import _probe_ui_u3 as u3  # noqa: E402  （复用 seed / click_text / set_value 等）
import _ui_nav_common as nav  # noqa: E402

SAMPLE = REPO / "examples" / "sample_world"
FAKE_REPLY = "退潮了，滩上还留着昨天的水痕。"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def timeline_count(root: Path) -> int:
    from isekai_core.config import load_config
    from isekai_core.store import Store

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    try:
        instances = store.instance_list()
        total = 0
        for row in instances:
            total += len(store.timeline_list(str(row["id"])))
        return total
    finally:
        store.close()


def push_world_awake(root: Path, *, hours: int = 6) -> str:
    """把她挪出睡眠窗：会话在睡眠期会把回复压到窗口结束（P1-19），探针不该在那儿干等几小时。

    世界时钟在核心进程里跑，这里用同一条运行层入口推进它（SQLite WAL + busy_timeout 允许并发写）。
    """
    from isekai_core.config import load_config
    from isekai_core.runtime.service import from_config
    from isekai_core.store import Store

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    try:
        world = from_config(cfg, store)
        instances = store.instance_list()
        if not instances:
            return "no-instance"
        instance_id = str(instances[0]["id"])
        timeline_id = str(store.timeline_list(instance_id)[0]["id"])
        out = world.consume_time(
            instance_id, timeline_id, seconds=hours * 3600,
            cause="探针：把她挪到清醒时段（流式验收前置）", max_batches=64,
        )
        return json.dumps(out, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return f"ERR:{exc}"
    finally:
        store.close()


async def main() -> None:
    root = desk.make_root("effscreen")
    (root / "examples").mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE, root / "examples" / "sample_world")
    # 首启的真实状态：还没有访问密钥（首页才会给出「先连接 AI」）
    profile = root / "config" / "config.yaml"
    lines = [line for line in profile.read_text(encoding="utf-8").splitlines() if "api_key" not in line]
    profile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("临时根:", root, flush=True)

    # 假模型：联络用普通句子；「情节提议」判断点返回一条带世界变化的候选（与 u3 同口径）。
    # 目标用样例世界已登记的区域 `pl-1`（真校验会要求目标在册，不许编一个空目标）。
    suggestions = {
        "candidates": [
            {
                "title": "封堤三日",
                "summary": "北堤缺口先封三天，盐价随之翻倍。",
                "outline_ref": "",
                "unsolved": ["封堤的钱谁出"],
                "changes": [
                    {
                        "id": "sgc-eff-1",
                        "kind": "condition",
                        "operation": "set",
                        "certainty": "confirmed",
                        "target_refs": ["pl-1"],
                        "value": "北堤封三日",
                        "expiry": "until_cleared",
                    }
                ],
            }
        ]
    }

    desk.kill_tree()
    await asyncio.sleep(1.0)
    proc, cdp, _targets = await desk.boot_shell(
        root,
        port=free_port(),
        env_extra={
            "ISEKAI_LLM_FAKE": "1",
            "ISEKAI_LLM_FAKE_REPLY": FAKE_REPLY,
            "ISEKAI_LLM_FAKE_JUDGEMENTS": json.dumps(
                {"情节提议": json.dumps(suggestions, ensure_ascii=False)}, ensure_ascii=False
            ),
        },
    )
    problems: list[str] = []
    notes: dict[str, object] = {}
    try:
        await cdp.js(desk.STUB)
        if not await u3.wait_true(cdp, "!!(window.__uiApp && window.__uiApp.probeState.connected)", timeout=140):
            raise SystemExit("止损：正式界面没连上核心")
        # 发送闸与建议都看 `readiness.ai.configured`：先经**正式界面自己的 API** 写入假服务配置
        # （用户界面没有 debug 壳的 `window.__mgmtCall`，只有 `window.__uiApi()`）
        saved = await cdp.js(
            "window.__uiApi().saveSettings({llm:{api_key:'sk-probe-effscreen-0000',"
            "base_url:'https://api.example.com/v1',model:'probe-model'}})"
            ".then(r=>JSON.stringify(r))",
            await_promise=True,
        )
        ready = await cdp.js(
            "window.__uiApi().readiness().then(r=>JSON.stringify(r.ai||{}))", await_promise=True
        )
        print("[AI 配置]", str(saved)[:120], str(ready)[:160], flush=True)
        if '"configured":true' not in str(ready):
            problems.append(f"写完假配置后 readiness.ai 仍不是已配置：{ready}")
        await cdp.js("window.__uiApp.navigate({pane:'home'})")
        await u3.wait_true(cdp, "!!document.querySelector('#u-main')", timeout=30)

        # ---------------- 0) 走真链路建一个世界（样例 → 创建 → 联络） ----------------
        await u3.click_text(cdp, "#u-main button", "从样例世界开始")
        await u3.wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('准备材料')", timeout=60)
        await cdp.js("window.__uiApp.navigate({pane:'onboarding', sub:'check'})")
        await u3.wait_true(cdp, "!!document.querySelector('#u-main')", timeout=30)
        await cdp.js("window.__uiApp.navigate({pane:'onboarding', sub:'sample'})")
        await u3.wait_true(cdp, "!!document.getElementById('onb-sample')", timeout=60)
        await u3.click_text(cdp, "#u-main button", "创建并开始联络")
        await u3.wait_true(
            cdp, "(document.querySelector('#u-main')?.innerText||'').includes('开始运行并联络')", timeout=120
        )
        await u3.click_text(cdp, "#u-main button", "开始运行并联络")
        in_contact = await u3.wait_true(cdp, "!!document.querySelector('#u-contact-input')", timeout=120)
        if not in_contact:
            problems.append(f"没有进入角色联络：{await u3.visible_text(cdp)}"[:200])

        # ---------------- A) 流式增量预览（P0-7） ----------------
        if in_contact:
            # 先把她挪到清醒时段（否则会话按 P1-19 把回复压到睡眠窗结束，探针要等几小时）
            moved = push_world_awake(root)
            notes["A_挪时钟"] = moved[:120]
            # 发送按钮有三道闸（AI 配置 / 连接 / 内容）：等到它真的可点再点
            await u3.wait_true(
                cdp, "(()=>{const b=document.getElementById('u-contact-send');return !!b && !b.disabled;})()", timeout=40
            )
            await u3.set_value(cdp, "#u-contact-input", "在吗？")
            await asyncio.sleep(0.3)
            # 增量帧是 4 字符一段、几乎瞬时到齐：靠 50ms 轮询会漏，改用 MutationObserver 记「出现过」
            await cdp.js(
                "(()=>{window.__previewSeen=0;window.__previewMax=0;window.__previewTexts=[];"
                "const ob=new MutationObserver(()=>{const n=document.querySelector('.u-bubble-preview');"
                "if(n){window.__previewSeen++;const t=(n.innerText||'').trim();"
                "if(t&&window.__previewTexts[window.__previewTexts.length-1]!==t)window.__previewTexts.push(t);"
                "if(t.length>window.__previewMax)window.__previewMax=t.length;}});"
                "ob.observe(document.body,{childList:true,subtree:true,characterData:true});"
                "window.__previewObserver=ob;return true;})()"
            )
            await u3.click_text(cdp, "#u-contact-send", "发送")
            sent = await u3.wait_true(
                cdp, "(document.querySelector('.u-messages')?.innerText||'').includes('在吗？')", timeout=20
            )
            if not sent:
                await u3.click_text(cdp, "#u-contact-send", "发送")
                sent = await u3.wait_true(
                    cdp, "(document.querySelector('.u-messages')?.innerText||'').includes('在吗？')", timeout=20
                )
            seen_preview = False
            preview_seen_with_content = False
            deadline = time.time() + 45
            while time.time() < deadline:
                try:
                    hit = await cdp.js(
                        "(()=>{const n=document.querySelector('.u-bubble-preview');"
                        "return n?JSON.stringify({text:(n.innerText||'').slice(0,60)}):'';})()"
                    )
                except Exception:  # noqa: BLE001
                    hit = ""
                if hit:
                    if not seen_preview:
                        seen_preview = True
                    if "正在生成" in str(hit) or len(str(hit)) > 12:
                        preview_seen_with_content = True
                if await cdp.js("!!document.querySelector('.u-bubble-them')"):
                    break
                await asyncio.sleep(0.05)
            replied = await u3.wait_true(cdp, "!!document.querySelector('.u-bubble-them')", timeout=150)
            await asyncio.sleep(0.6)
            leftover = await cdp.js("document.querySelectorAll('.u-bubble-preview').length")
            observed = await cdp.js(
                "JSON.stringify({seen:window.__previewSeen||0,max:window.__previewMax||0,"
                "texts:(window.__previewTexts||[]).slice(0,4)})"
            )
            try:
                obs = json.loads(str(observed) or "{}")
            except json.JSONDecodeError:
                obs = {}
            seen_preview = seen_preview or int(obs.get("seen") or 0) > 0
            body = await u3.visible_text(cdp, ".u-messages")
            status = await u3.visible_text(cdp, ".u-composer")
            # 固化正文只能有一份：预览节点不得残留成第二条「她的话」
            them = await cdp.js("document.querySelectorAll('.u-bubble-them:not(.u-bubble-preview)').length")
            notes["A_预览"] = {"已发送": sent, "出现过": seen_preview, "观察": obs,
                            "带内容": preview_seen_with_content,
                            "残留": leftover, "固化条数": them, "状态行": status[-120:],
                            "片段": body[-120:]}
            if not sent:
                problems.append("发送后界面没有显示自己那条消息")
            if not seen_preview:
                problems.append("发送后没有出现过 `.u-bubble-preview` 增量预览气泡（流式未生效）")
            if leftover:
                problems.append(f"固化回复到达后仍有 {leftover} 个预览气泡残留")
            if not replied or not body.strip():
                problems.append("联络真往返没有拿到固化回复")

        # ---------------- B) 试演不落线（P2-13） ----------------
        before = timeline_count(root)
        await nav.launch_app(cdp, "isekai Writer")
        await u3.wait_true(cdp, "(document.querySelector('#u-main')?.innerText||'').includes('辅助写作')", timeout=60)
        await u3.click_text(cdp, "#u-main button", "新建大纲")
        await u3.wait_true(cdp, "!!document.querySelector('#u-wa-outline-name')", timeout=30)
        await u3.set_value(cdp, "#u-wa-outline-name", "试演验收大纲")
        await u3.set_value(cdp, "#u-wa-outline-theme", "盐与潮：人怎么记住一件没人愿意记的事")
        await u3.set_value(cdp, "#u-wa-outline-criteria", "读的人能说出这件事被谁记住了")
        await u3.click_dialog(cdp, "保存大纲")
        await u3.wait_true(cdp, "document.querySelector('#u-main').innerText.includes('试演验收大纲')", timeout=30)
        await u3.click_text(cdp, "#u-main button", "绑定 / 更新绑定", last=True)
        await u3.wait_true(cdp, "!!document.querySelector('#u-wa-chapter')", timeout=30)
        await u3.set_value(cdp, "#u-wa-chapter", "第一章")
        await u3.click_dialog(cdp, "绑定")
        await u3.wait_true(cdp, "document.querySelector('#u-main').innerText.includes('已绑定')", timeout=60)
        await u3.click_text(cdp, "#u-main nav.u-tools button", "推进建议")
        await asyncio.sleep(0.5)
        await u3.set_value(cdp, "#u-wa-goal", "这一章要让读者第一次怀疑账本")
        await u3.click_text(cdp, "#u-main button", "给我推进建议")
        got = await u3.wait_true(
            cdp, "document.querySelector('#u-main').innerText.includes('封堤三日')", timeout=150
        )
        if not got:
            problems.append(f"推进建议没有出现：{await u3.visible_text(cdp)}"[-200:])

        preview_ok = applied_ok = False
        if got:
            clicked = await u3.card_button(cdp, "封堤三日", "另开分支试演")
            if clicked != "ok":
                problems.append(f"「另开分支试演」按钮点不到：{clicked}")
            if not await u3.wait_true(cdp, "!!document.querySelector('.u-dialog')", timeout=30):
                problems.append("试演对话框没有打开")
            dialog_text = await u3.visible_text(cdp, ".u-dialog")
            notes["B_对话框"] = dialog_text[:200]
            if "不落线" not in dialog_text:
                problems.append(f"试演对话框没有说明「不落线」：{dialog_text[:120]}")
            # 需要版本点才算用得上「应用于试演线」：没有就先存一个
            if "保存当前版本点" in dialog_text:
                await u3.click_dialog(cdp, "保存当前版本点")
                await asyncio.sleep(1.5)
            # 1) 只读预览：这一步不许建线
            await u3.click_dialog(cdp, "得到试演预览")
            preview_ok = await u3.wait_true(
                cdp,
                "(document.querySelector('.u-dialog .u-note')?.innerText||'').includes('预览通过')",
                timeout=60,
            )
            after_preview = timeline_count(root)
            notes["B_预览"] = {"提示通过": preview_ok, "建线前": before, "预览后": after_preview}
            if not preview_ok:
                problems.append(
                    f"「得到试演预览（不建线）」没有给出预览结论：{(await u3.visible_text(cdp, '.u-dialog'))[:160]}"
                )
            if after_preview != before:
                problems.append(f"试演预览阶段就建了线：{before} → {after_preview}（规范要求采用才建线）")
            # 2) 应用于试演线：这一步才该多出一条线
            await u3.click_dialog(cdp, "应用于试演线")
            applied_ok = await u3.wait_true(
                cdp, "(document.querySelector('#u-main')?.innerText||'').includes('已建线并切到')", timeout=90
            )
            after_apply = timeline_count(root)
            notes["B_应用"] = {"提示已建线": applied_ok, "应用后": after_apply}
            if not applied_ok:
                problems.append("「应用于试演线」之后界面没有报告已建线")
            if after_apply != before + 1:
                problems.append(f"「应用于试演线」没有正好多出一条线：{before} → {after_apply}")

        # ---------------- C) 退出三选一通道（P1-18） ----------------
        probe_c = await cdp.js(
            "(async()=>{const out={internals:!!window.__TAURI_INTERNALS__,global:!!window.__TAURI__};"
            "try{const inv=(window.__TAURI_INTERNALS__&&window.__TAURI_INTERNALS__.invoke)"
            "||(window.__TAURI__&&window.__TAURI__.core&&window.__TAURI__.core.invoke);"
            "if(!inv){out.result='no-invoke';return JSON.stringify(out);}"
            "out.pending=await inv('quit_timeout_pending');out.decision=await inv('quit_decision',{choice:'wait'});"
            "out.result='ok';}catch(e){out.result='ERR:'+String(e);}"
            "return JSON.stringify(out);})()",
            await_promise=True,
        )
        notes["C_退出通道"] = probe_c
        try:
            parsed = json.loads(str(probe_c) or "{}")
        except json.JSONDecodeError:
            parsed = {}
        if parsed.get("result") != "ok":
            problems.append(f"退出三选一的壳命令调不通：{probe_c}")
        elif parsed.get("pending") is not False:
            problems.append(f"未退出时 quit_timeout_pending 应为 false：{probe_c}")

        print("[A/B/C]", json.dumps(notes, ensure_ascii=False, default=str)[:900], flush=True)
        print("\n结果:", "PASS" if not problems else "FAIL", "；".join(problems), flush=True)
    finally:
        desk.kill_tree()
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(main())
