#!/usr/bin/env python
"""生成工作区 P1 行为审计（DESKTOP_GENERATION_WORKSPACE_SPEC §二 / §3.1-§3.2 / §七 P1）。

用法（仓库根，.venv 解释器）：
  .venv/Scripts/python.exe scripts/_audit2_gen_ws.py static   # 静态：旋钮表与核心对拍 + 入口/内联框替换
  .venv/Scripts/python.exe scripts/_audit2_gen_ws.py main     # 真壳 + CDP + 假 LLM（给得出一份合法包）

判据照 §七 P1 的验收列：入口按钮进工作区且管理页不再有内联输入框；旋钮全量接上提示词；
生成后三段各显示状态；保存后包出现在管理页列表；未保存返回有提示；草稿续作先问。
只读策略与 `_audit2_desk.py` 相同：仓库内文件一律不改，可写数据落在 %LOCALAPPDATA%/Temp。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 复用 _audit2_desk 的 CDP / 起壳 / 记账工具
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _audit2_desk import (  # noqa: E402
    EXE,
    FAKE_KEY,
    STUB,
    boot_shell,
    check,
    dump,
    make_root,
    mgmt_call,
)

from isekai_core.world.example import example_package  # noqa: E402
from isekai_core.world.generator import KNOB_COUNTS, KNOB_LISTS, KNOB_TONES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAIN_TS = (ROOT / "desktop" / "src" / "main.ts").read_text(encoding="utf-8")
INDEX = (ROOT / "desktop" / "index.html").read_text(encoding="utf-8")


def ts_table(name: str) -> list[tuple[str, str]]:
    """取 main.ts 里 `const <name>: Array<[string, string]> = [ ["k","标签"], … ];` 的表。"""
    body = MAIN_TS.split(f"const {name}: Array<[string, string]> = [")[1].split("];")[0]
    return [(key, label) for key, label in re.findall(r'\["([a-z_]+)", "([^"]+)"\]', body)]


def section_static() -> None:
    check("G0 §3.2 旋钮表与核心同源（9 取向 / 15 计数 / 3 内容指定）",
          "PASS" if (ts_table("GENWS_TONES") == list(KNOB_TONES)
                     and ts_table("GENWS_COUNTS") == list(KNOB_COUNTS)
                     and ts_table("GENWS_LISTS") == list(KNOB_LISTS)) else "FAIL",
          f"TS 取向={len(ts_table('GENWS_TONES'))}/核心={len(KNOB_TONES)}；"
          f"TS 计数={len(ts_table('GENWS_COUNTS'))}/核心={len(KNOB_COUNTS)}；"
          f"TS 内容指定={len(ts_table('GENWS_LISTS'))}/核心={len(KNOB_LISTS)}",
          clause="§3.2 参数层旋钮清单（13 计数 + 9 取向 + 3 内容指定）",
          code="desktop/src/main.ts GENWS_* · isekai_core/world/generator.py KNOB_*")
    leftovers = [item for item in ("pkg-brief", "pkg-generate", "pkg-name") if f'id="{item}"' in INDEX]
    check("G0b §二 管理页两组改成入口按钮，不再有内联生成输入框",
          "PASS" if ("pkg-open-workspace" in INDEX and "pane-genws" in INDEX and not leftovers) else "FAIL",
          f"入口按钮={'pkg-open-workspace' in INDEX}；工作区 pane={'pane-genws' in INDEX}；残留内联框={leftovers}",
          clause="§二 入口改造：[AI 生成世界包] 只负责进工作区",
          code="desktop/index.html #pkg-open-workspace / #pane-genws")


async def section_main() -> None:
    root = make_root("genws")
    reply = json.dumps(example_package(), ensure_ascii=False)
    proc, cdp, _targets = await boot_shell(
        root, 9931, {"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": reply}
    )
    try:
        await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
        await cdp.js(STUB)
        await mgmt_call(cdp, "settings.set", llm={"api_key": FAKE_KEY, "model": "audit-fake"})

        # ---- G6 草稿续作：先放一份草稿，进工作区应先问「继续上次」
        await mgmt_call(cdp, "world.draft.save", name="a2genws-package", kind="package",
                        payload=example_package(), errors=["设定核心：审计预置的未过项"])
        await cdp.pane("manage")
        await cdp.js("document.getElementById('pkg-open-workspace').click()")
        await asyncio.sleep(1.5)
        resumed = await cdp.js(
            "({open: !document.getElementById('pane-genws').classList.contains('hidden'),"
            " manage_hidden: document.getElementById('pane-manage').classList.contains('hidden'),"
            " crumb: document.getElementById('gw-crumb').textContent,"
            " knobs: document.querySelectorAll('#gw-knobs input, #gw-knobs textarea').length,"
            " asked: (window.__confirmArgs||[]).slice(-1)[0] || '',"
            " resume_note: document.getElementById('gw-resume').textContent,"
            " segs: document.querySelectorAll('#gw-segments li').length,"
            " errors: document.getElementById('gw-errors').textContent})")
        check("G1-G2 旋钮全量渲染（9+15+3=27）+ 草稿续作先问「继续上次」",
              "PASS" if (resumed["open"] and resumed["manage_hidden"]
                         and resumed["crumb"].startswith("管理 ▸ 世界包 ▸ 生成")
                         and int(resumed["knobs"]) == len(KNOB_TONES) + len(KNOB_COUNTS) + len(KNOB_LISTS) == 27
                         and "继续上次" in str(resumed["asked"])
                         and "已载回草稿" in str(resumed["resume_note"])
                         and int(resumed["segs"]) == 3 and "审计预置的未过项" in str(resumed["errors"])) else "FAIL",
              f"工作区可见={resumed['open']}／管理页隐藏={resumed['manage_hidden']}；面包屑={resumed['crumb']!r}；"
              f"旋钮控件={resumed['knobs']}（期望 27）；进入时的确认问句={str(resumed['asked'])[:80]!r}；"
              f"载回提示={resumed['resume_note']!r}；段状态行={resumed['segs']}；错误清单={resumed['errors']!r}",
              clause="§五 草稿续作：进入工作区时若该名下有草稿先问「继续上次 / 重新开始」；§3.1 三列 + 顶栏返回",
              code="desktop/src/main.ts genwsOpen / genwsMaybeResume / genwsRenderKnobs")

        # ---- G3 生成：旋钮进提示词 → 三段各显示状态 + 摘要
        await cdp.js(
            "document.getElementById('gw-name').value='审计工作区世界';"
            "document.getElementById('gw-file').value='a2genws.json';"
            "document.getElementById('gw-brief').value='一片灰潮沿岸的三城邦';"
            "document.getElementById('gw-knob-genre').value='低魔海国';"
            "document.getElementById('gw-knob-races').value='2';"
            "document.getElementById('gw-knob-festivals').value='0';"
            "window.__confirmArgs=[];"
            "document.getElementById('gw-generate').click()")
        await asyncio.sleep(6.0)
        generated = await cdp.js(
            "({note: document.getElementById('gw-generate-note').textContent,"
            " status: document.getElementById('gw-status').textContent,"
            " segs: [...document.querySelectorAll('#gw-segments li')].map(e=>e.textContent),"
            " rows: document.querySelectorAll('#gw-summary dt').length,"
            " summary: document.getElementById('gw-summary').textContent,"
            " errors: document.getElementById('gw-errors').textContent,"
            " confirm: (window.__confirmArgs||[]).slice(-1)[0] || ''})")
        seg_text = " | ".join(generated["segs"])
        check("G3 §3.1/§3.2 填表 → 生成 → 三段各显示状态 + ③ 摘要与错误清单",
              "PASS" if (len(generated["segs"]) == 3
                         and all(label in seg_text for label in ("设定核心", "双轨与名册", "机制与现状"))
                         and int(generated["rows"]) >= 8
                         and not generated["errors"].strip()
                         and "调用" in str(generated["status"])) else "FAIL",
              f"生成结果提示={generated['note']!r}；状态条={generated['status']!r}；三段状态={generated['segs']}；"
              f"摘要行数={generated['rows']}（{str(generated['summary'])[:120]!r}）；校验清单={generated['errors']!r}；"
              f"确认框（旋钮计数写进去了）={str(generated['confirm'])[:160]!r}",
              clause="§七 P1 验收：填表 → 生成（FakeLLM）→ 三段各显示状态；§3.1 ③ 摘要 + 校验错误清单",
              code="desktop/src/main.ts genwsGenerate / genwsRenderSegments / genwsRenderSummary")

        # ---- G5 未保存返回有提示（先取消 → 留在工作区；再保存 → 返回成功）
        await cdp.js("window.__confirmArgs=[];"
                     "window.confirm=(m)=>{window.__confirmArgs.push(String(m)); return false;};"
                     "document.getElementById('gw-back').click()")
        await asyncio.sleep(1.0)
        held = await cdp.js(
            "({note: (window.__confirmArgs||[]).slice(-1)[0] || '',"
            " still: !document.getElementById('pane-genws').classList.contains('hidden')})")
        await cdp.js("window.__confirmArgs=[];"
                     "window.confirm=(m)=>{window.__confirmArgs.push(String(m)); return true;};"
                     "document.getElementById('gw-save').click()")
        await asyncio.sleep(2.0)
        saved = await cdp.js(
            "({note: document.getElementById('gw-generate-note').textContent,"
            " status: document.getElementById('gw-status').textContent,"
            " file: `${location.origin}`,"
            " in_list: Array.from(document.getElementById('pkg-select').options).some(o=>o.value==='a2genws.json')})")
        on_disk = (root / "packages" / "a2genws.json").exists()
        check("G4-G5 §七 P1 验收：未保存时返回有提示；保存后包出现在管理页列表（真落盘）",
              "PASS" if ("未保存" in str(held["note"]) and held["still"]
                         and "已保存" in str(saved["note"]) and saved["in_list"] and on_disk) else "FAIL",
              f"返回时确认文案={str(held['note'])[:120]!r}（取消后仍在工作区={held['still']}）；"
              f"保存提示={saved['note']!r}；管理页列表含该包={saved['in_list']}；磁盘上={on_disk}",
              clause="§五 顶栏与返回：离开前未保存提示；§七 P1 验收：保存后包出现在管理页列表",
              code="desktop/src/main.ts genwsClose / genwsSave / world.package.save")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
        dump("genws")


async def amain() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "static"
    started = time.time()
    if which in ("static", "all"):
        section_static()
    if which in ("main", "all"):
        if not EXE.exists():
            check("main", "SKIP", f"没有可执行件 {EXE}（先构建桌壳）")
        else:
            await section_main()
    print(f"[genws] 用时 {time.time() - started:.1f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(amain())
