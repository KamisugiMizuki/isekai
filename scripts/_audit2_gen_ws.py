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
    wait_note,
)

from isekai_core.world.example import example_package  # noqa: E402
from isekai_core.world.generator import (  # noqa: E402
    KNOB_COUNTS,
    KNOB_LISTS,
    KNOB_TONES,
    PACKAGE_SEGMENTS,
)

SEGMENT_LABELS = [label for label, _ in PACKAGE_SEGMENTS]
ROOT = Path(__file__).resolve().parent.parent
MAIN_TS = (ROOT / "desktop" / "src" / "main.ts").read_text(encoding="utf-8")
INDEX = (ROOT / "desktop" / "index.html").read_text(encoding="utf-8")


def ts_table(name: str) -> list[tuple[str, str]]:
    """取 main.ts 里 `const <name>: Array<[string, string]> = [ ["k","标签"], … ];` 的表。"""
    body = MAIN_TS.split(f"const {name}: Array<[string, string]> = [")[1].split("];")[0]
    return [(key, label) for key, label in re.findall(r'\["([a-z_]+)", "([^"]+)"\]', body)]


def ts_segment_keys() -> list[tuple[str, tuple[str, ...]]]:
    """取 main.ts 里 `GENWS_SEGMENT_KEYS`（段名 → 顶层键列表）。"""
    body = MAIN_TS.split("const GENWS_SEGMENT_KEYS: Array<[string, string[]]> = [")[1].split("];")[0]
    rows = []
    for label, keys in re.findall(r'\["([^"]+)", \[([^\]]*)\]\]', body):
        rows.append((label, tuple(part.strip().strip('"') for part in keys.split(",") if part.strip())))
    return rows


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
    card_leftovers = [item for item in ("card-brief", "card-generate") if f'id="{item}"' in INDEX]
    check("G0d §二 角色卡组同样改成入口按钮（管理页不再有内联角色描述 / 生成按钮）",
          "PASS" if ("card-open-workspace" in INDEX and "pane-cardws" in INDEX and not card_leftovers) else "FAIL",
          f"入口按钮={'card-open-workspace' in INDEX}；工作区 pane={'pane-cardws' in INDEX}；残留内联控件={card_leftovers}",
          clause="§二 入口改造：两组都只留「AI 生成 …」按钮；§四 卡工作区",
          code="desktop/index.html #card-open-workspace / #pane-cardws")
    check("G0c §3.1 分段与核心 PACKAGE_SEGMENTS 同源（段名 → 顶层键）",
          "PASS" if ts_segment_keys() == [(label, tuple(keys)) for label, keys in PACKAGE_SEGMENTS] else "FAIL",
          f"TS 段={[(label, list(keys)) for label, keys in ts_segment_keys()]}；"
          f"核心段={[(label, list(keys)) for label, keys in PACKAGE_SEGMENTS]}",
          clause="§3.1 ② 分段与条目：段 = 若干顶层键（重跑这段的粒度）",
          code="desktop/src/main.ts GENWS_SEGMENT_KEYS · isekai_core/world/generator.py PACKAGE_SEGMENTS")


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

        # ---- G6/G7 P2：条目表（加 / 改 / 删 / 锁定）+ 重跑这段保留锁定条目
        entries = await cdp.js(
            "({rows: document.querySelectorAll('#gw-entries .entry-row').length,"
            " heads: [...document.querySelectorAll('#gw-entries .entry-head')].map(e=>e.textContent.trim()),"
            " adds: document.querySelectorAll('#gw-entries .entry-add').length,"
            " edits: document.querySelectorAll('#gw-entries .entry-edit').length,"
            " dels: document.querySelectorAll('#gw-entries .entry-delete').length,"
            " segs: [...document.getElementById('gw-segment').options].map(o=>o.value)})")
        check("G6 §3.3 条目表：段路径表头 + 一行一条（改 / 删 / 锁定）+ ＋加一条 + 段下拉",
              "PASS" if (int(entries["rows"]) > 0 and int(entries["adds"]) >= 3 and int(entries["edits"]) == int(entries["rows"])
                         and int(entries["dels"]) == int(entries["rows"])
                         and entries["segs"] == SEGMENT_LABELS) else "FAIL",
              f"条目行={entries['rows']}（改/删按钮各 {entries['edits']}/{entries['dels']}）；表头={entries['heads'][:4]}；"
              f"＋加一条={entries['adds']}；段下拉={entries['segs']}",
              clause="§3.3 每段可展开成条目表：一行一条 id·摘要·来源 + [编辑][删除][锁定]，段头 [＋加一条] 与 [重跑这段]",
              code="desktop/src/main.ts genwsRenderEntries / genwsAddEntry / genwsEditEntry / genwsDeleteEntry")

        # 改一条 → 锁定 → 加一条 → 重跑「设定核心」（模型拿示例包覆盖）→ 锁定条目原样保留
        await cdp.js(
            "window.prompt = () => '审计改写过的公理';"
            "document.querySelector('#gw-entries .entry-edit[data-ident=\"ax-1\"]').click();"
            "document.querySelector('#gw-entries .entry-lock[data-ident=\"ax-1\"]').click();"
            "document.querySelector('#gw-entries .entry-add[data-path=\"world.customs\"]').click()")
        before = await cdp.js(
            "({ax: [...document.querySelectorAll('#gw-entries .entry-row')].map(e=>e.textContent).find(t=>t.includes('ax-1')) || '',"
            " customs: [...document.querySelectorAll('#gw-entries .entry-head')].find(e=>e.textContent.includes('world.customs'))?.textContent || '',"
            " other: [...document.querySelectorAll('#gw-entries .entry-row')].map(e=>e.textContent).filter(t=>/^(src|cf|nv|hs|rc|en|ef|et)-/.test(t)).length,"
            " status: document.getElementById('gw-status').textContent,"
            " locked: document.querySelectorAll('#gw-entries .entry-lock:checked').length})")
        await cdp.select("gw-segment", "设定核心")
        await cdp.js("window.__confirmArgs=[]; document.getElementById('gw-rerun').click()")
        rerun_note = await wait_note(cdp, ("gw-rerun-note",), "重跑", 90)
        after = await cdp.js(
            "({ax: [...document.querySelectorAll('#gw-entries .entry-row')].map(e=>e.textContent).find(t=>t.includes('ax-1')) || '',"
            " other: [...document.querySelectorAll('#gw-entries .entry-row')].map(e=>e.textContent).filter(t=>/^(src|cf|nv|hs|rc|en|ef|et)-/.test(t)).length,"
            " status: document.getElementById('gw-status').textContent,"
            " errors: document.getElementById('gw-errors').textContent})")
        check("G7 §3.3/§八 改 + 锁定 + 重跑这段：锁定条目内容原样保留，其他段条目不动",
              "PASS" if ("审计改写过的公理" in str(before["ax"]) and "审计改写过的公理" in str(after["ax"])
                         and int(after["other"]) == int(before["other"])
                         and "锁定 1 条" in str(after["status"]) and not str(after["errors"]).strip()) else "FAIL",
              f"改后该行={str(before['ax'])[:70]!r}；重跑前状态条={str(before['status'])[:90]!r}；"
              f"重跑提示={str(rerun_note)[:70]!r}；重跑后该行={str(after['ax'])[:70]!r}（模型给的是示例包原文——锁定项没被覆盖）；"
              f"其他段条目行数 {before['other']}→{after['other']}；重跑后状态条={str(after['status'])[:90]!r}",
              clause="§3.3 锁定 = 重生成不覆盖（段级重跑同样保留）；§七 P2 验收：改一段后 [重跑这段]，其他段内容逐字段不变",
              code="desktop/src/main.ts genwsRerun / genwsToggleLock · isekai_core/world/generator.py fill_section/apply_locks")

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
        saved_pkg = json.loads((root / "packages" / "a2genws.json").read_text(encoding="utf-8")) if on_disk else {}
        locked_ax = [item for item in (saved_pkg.get("world") or {}).get("axioms") or [] if item.get("id") == "ax-1"]
        locked_kept = bool(locked_ax) and locked_ax[0].get("text") == "审计改写过的公理"
        check("G4-G5 §七 P1 验收：未保存时返回有提示；保存后包出现在管理页列表（真落盘）",
              "PASS" if ("未保存" in str(held["note"]) and held["still"]
                         and "已保存" in str(saved["note"]) and saved["in_list"] and on_disk and locked_kept) else "FAIL",
              f"返回时确认文案={str(held['note'])[:120]!r}（取消后仍在工作区={held['still']}）；"
              f"保存提示={saved['note']!r}；管理页列表含该包={saved['in_list']}；磁盘上={on_disk}；"
              f"落盘后锁定条目仍是审计改写的那一版={locked_kept}（{str(locked_ax[:1])[:80]}）",
              clause="§五 顶栏与返回：离开前未保存提示；§七 P1 验收：保存后包出现在管理页列表",
              code="desktop/src/main.ts genwsClose / genwsSave / world.package.save")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
        dump("genws")


async def section_broken() -> None:
    """非法候选：校验拦住、不落盘（§七 P2 验收第二条）。"""
    root = make_root("genws-bad")
    bad = example_package()
    bad["canon"] = []  # 抽掉实情条目：初始态与说法的引用悬空，最小内容也不达标
    proc, cdp, _targets = await boot_shell(
        root, 9932, {"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": json.dumps(bad, ensure_ascii=False)}
    )
    try:
        await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
        await cdp.js(STUB)
        await mgmt_call(cdp, "settings.set", llm={"api_key": FAKE_KEY, "model": "audit-fake"})
        await cdp.js("document.getElementById('pkg-open-workspace').click()")
        await asyncio.sleep(1.2)
        await cdp.js(
            "document.getElementById('gw-file').value='a2genws-bad.json';"
            "document.getElementById('gw-brief').value='审计非法候选';"
            "window.__confirmArgs=[];"
            "document.getElementById('gw-generate').click()")
        await asyncio.sleep(6.0)
        state = await cdp.js(
            "({note: document.getElementById('gw-generate-note').textContent,"
            " errors: document.getElementById('gw-errors').textContent,"
            " status: document.getElementById('gw-status').textContent,"
            " segs: [...document.querySelectorAll('#gw-segments li')].map(e=>e.textContent)})")
        await cdp.js("window.confirm=()=>true; document.getElementById('gw-save').click()")
        await asyncio.sleep(1.5)
        refused = await cdp.js("document.getElementById('gw-generate-note').textContent")
        on_disk = (root / "packages" / "a2genws-bad.json").exists()
        drafts = [item.name for item in (root / "packages").glob("*.draft.json")]
        check("G8 §七 P2 验收：非法产物被 validate_package 拦住且不落盘（失败留草稿）",
              "PASS" if (str(state["errors"]).strip() and "未通过校验" in str(state["note"])
                         and "校验没过" in str(refused) and not on_disk and drafts) else "FAIL",
              f"生成提示={str(state['note'])[:90]!r}；状态条={str(state['status'])[:80]!r}；段状态={state['segs']}；"
              f"错误清单={str(state['errors'])[:150]!r}；点保存被拒={str(refused)[:80]!r}；"
              f"磁盘上有该包={on_disk}（必须 False）；中间态草稿={drafts}",
              clause="§七 P2 验收：非法产物被 validate_package 拦住且不落盘",
              code="desktop/src/main.ts genwsGenerate / genwsSave · isekai_core/world/generator.py validate_package")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
        dump("genws")


async def section_card() -> None:
    """卡工作区（§4，P2 卡侧）：字段表 / 字段级重跑 / 锁定 / 联合校验 / 确认硬闸。"""
    from isekai_core.world.example import example_card

    root = make_root("cardws")
    package = example_package()
    (root / "packages" / "w-cw.json").write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
    card_reply = json.dumps(example_card(package), ensure_ascii=False)
    proc, cdp, _targets = await boot_shell(
        root, 9933, {"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": card_reply}
    )
    try:
        await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
        await cdp.js(STUB)
        await mgmt_call(cdp, "settings.set", llm={"api_key": FAKE_KEY, "model": "audit-fake"})
        await cdp.pane("manage")
        await cdp.js("document.getElementById('card-open-workspace').click()")
        await asyncio.sleep(1.5)
        opened = await cdp.js(
            "({open: !document.getElementById('pane-cardws').classList.contains('hidden'),"
            " manage_hidden: document.getElementById('pane-manage').classList.contains('hidden'),"
            " crumb: document.getElementById('cw-crumb').textContent,"
            " placeholder: document.getElementById('cw-fields').textContent,"
            " fields: [...document.getElementById('cw-field').options].map(o=>o.value),"
            " packages: [...document.getElementById('cw-package').options].map(o=>o.value)})")
        check("C1 §4.1 卡工作区：入口按钮进三列工作区 + 字段下拉 + 归属包下拉（无候选时给引导）",
              "PASS" if (opened["open"] and opened["manage_hidden"] and opened["crumb"].startswith("管理 ▸ 角色卡 ▸ 生成")
                         and len(opened["fields"]) == 6 and "w-cw.json" in opened["packages"]
                         and "先生成一份候选" in str(opened["placeholder"])) else "FAIL",
              f"工作区可见={opened['open']}／管理页隐藏={opened['manage_hidden']}；面包屑={opened['crumb']!r}；"
              f"字段下拉={opened['fields']}；归属包下拉={opened['packages']}；无候选提示={str(opened['placeholder'])[:60]!r}",
              clause="§4.1 布局：① 卡的设定 / ② 生成与校对（字段级）/ ③ 预览与确认；§4.2 必须先有包",
              code="desktop/index.html #pane-cardws · desktop/src/main.ts cwOpen / cwRenderFields")

        await cdp.select("cw-package", "w-cw.json")
        await cdp.js("document.getElementById('cw-file').value='a2cw.json';"
                     "document.getElementById('cw-brief').value='盐滩上记水位尺的年轻堤务吏';"
                     "document.getElementById('cw-name').value='审计卡';"
                     "window.__confirmArgs=[];"
                     "document.getElementById('cw-generate').click()")
        await asyncio.sleep(6.0)
        generated = await cdp.js(
            "({note: document.getElementById('cw-generate-note').textContent,"
            " status: document.getElementById('cw-status').textContent,"
            " rows: document.querySelectorAll('#cw-summary dt').length,"
            " fields: [...document.querySelectorAll('#cw-fields .entry-row')].map(e=>e.textContent),"
            " errors: document.getElementById('cw-errors').textContent})")
        check("C2 §4.1 填表 → 生成（FakeLLM 合法卡）→ 字段表与摘要出现、校验通过",
              "PASS" if (int(generated["rows"]) >= 8 and not str(generated["errors"]).strip()
                         and "校验通过" in str(generated["status"]) and len(generated["fields"]) == 6) else "FAIL",
              f"生成提示={str(generated['note'])[:90]!r}；状态条={str(generated['status'])[:80]!r}；摘要行={generated['rows']}；"
              f"字段表={[t[:46] for t in generated['fields']]}；错误清单={str(generated['errors'])[:80]!r}",
              clause="§4.1 ①→②→③：填设定 → 生成 → 校对；§4.2 字段级而非段级",
              code="desktop/src/main.ts cwGenerate / cwRenderFields / cwRenderSummary")

        # 锁定「身份与职业」→ 改它的 occupation → 重跑该字段组（模型返回示例卡原文）→ 用户改的那版必须留下
        await cdp.js(
            "window.prompt=()=> '审计改过的职业';"
            "document.querySelector('#cw-fields .cw-lock[data-field=\"identity\"]').click();"
            "document.querySelector('#cw-fields .cw-edit[data-leaf=\"identity.occupation\"]').click()")
        before = await cdp.js(
            "({row: [...document.querySelectorAll('#cw-fields .entry-row')].map(e=>e.textContent).find(t=>t.includes('身份与职业')) || '',"
            " status: document.getElementById('cw-status').textContent})")
        await cdp.select("cw-field", "身份与职业")
        await cdp.js("document.getElementById('cw-rerun').click()")
        rerun = await wait_note(cdp, ("cw-rerun-note",), "重跑", 90)
        after = await cdp.js(
            "({row: [...document.querySelectorAll('#cw-fields .entry-row')].map(e=>e.textContent).find(t=>t.includes('身份与职业')) || '',"
            " status: document.getElementById('cw-status').textContent,"
            " errors: document.getElementById('cw-errors').textContent})")
        check("C3 §4.2 字段级重跑 + 锁定：用户改过并锁定的字段不被模型覆盖，其他字段不动",
              "PASS" if ("审计改过的职业" in str(before["row"]) and "审计改过的职业" in str(after["row"])
                         and "锁定 1 个字段" in str(after["status"]) and not str(after["errors"]).strip()) else "FAIL",
              f"改后该行={str(before['row'])[:80]!r}；重跑提示={str(rerun)[:70]!r}；"
              f"重跑后该行={str(after['row'])[:80]!r}（模型给的是示例卡原文——锁定字段没被覆盖）；"
              f"状态条={str(after['status'])[:80]!r}",
              clause="§4.2 字段可 [锁定]（重跑不覆盖），粒度是「这个字段」；§七 P2 卡侧验收：字段级重跑、锁定与联合校验",
              code="desktop/src/main.ts cwRerun / cwToggleLock · isekai_core/world/generator.py fill_card/apply_field_locks")

        # 联合校验 + 确认硬闸（确认才写回文件）
        await cdp.js("document.getElementById('cw-validate').click()")
        validated = await wait_note(cdp, ("cw-validate-note",), "联合校验", 30)
        await cdp.js("window.__confirmArgs=[]; document.getElementById('cw-confirm').click()")
        confirmed = await wait_note(cdp, ("cw-generate-note",), "已确认", 45)
        saved_file = root / "packages" / "a2cw.json"
        saved = json.loads(saved_file.read_text(encoding="utf-8")) if saved_file.exists() else {}
        check("C4 §4.2/§七 联合校验 + 确认硬闸：确认后才写回，且写回的卡是已确认状态",
              "PASS" if ("联合校验通过" in str(validated) and "已确认" in str(confirmed)
                         and bool(saved) and (saved.get("meta") or {}).get("confirmed") is True
                         and ((saved.get("identity") or {}).get("occupation") == "审计改过的职业")) else "FAIL",
              f"联合校验提示={str(validated)[:70]!r}；确认提示={str(confirmed)[:70]!r}；"
              f"磁盘上有该卡={saved_file.exists()}；meta.confirmed={((saved.get('meta') or {}).get('confirmed'))}；"
              f"落盘的职业={((saved.get('identity') or {}).get('occupation'))!r}",
              clause="§4.2 确认是硬闸：world.card.confirm 才是最终版，未确认的卡进不了实例",
              code="desktop/src/main.ts cwValidate / cwConfirm · isekai_core/world/ops.py world.card.validate/confirm")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
        dump("cardws")


async def section_p3() -> None:
    """P3（§六）：进度可见 / 并排对比 / 从骨架长 + §3.4 提示词预览。"""
    root = make_root("genws-p3")
    reply = json.dumps(example_package(), ensure_ascii=False)
    skeleton = json.loads(json.dumps(example_package(), ensure_ascii=False))
    skeleton["canon"] = []  # 搞一份「未过校验的骨架」当从骨架长的起点
    (root / "packages" / "skel.json").write_text(json.dumps(skeleton, ensure_ascii=False), encoding="utf-8")
    proc, cdp, _targets = await boot_shell(
        root, 9934, {"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": reply}
    )
    try:
        await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
        await cdp.js(STUB)
        await mgmt_call(cdp, "settings.set", llm={"api_key": FAKE_KEY, "model": "audit-fake"})
        await cdp.pane("manage")
        await asyncio.sleep(1.0)
        await cdp.select("pkg-select", "skel.json")
        await cdp.js("document.getElementById('pkg-open-in-ws').click()")
        await asyncio.sleep(2.0)
        loaded = await cdp.js(
            "({open: !document.getElementById('pane-genws').classList.contains('hidden'),"
            " file: document.getElementById('gw-file').value,"
            " rows: document.querySelectorAll('#gw-entries .entry-row').length,"
            " note: document.getElementById('gw-generate-note').textContent,"
            " status: document.getElementById('gw-status').textContent})")
        check("P3-① §六「从骨架长」：把选中的包（未过校验的骨架）丢进工作区，条目表随之出来",
              "PASS" if (loaded["open"] and loaded["file"] == "skel.json" and int(loaded["rows"]) > 0
                         and "已载入 skel.json" in str(loaded["note"])) else "FAIL",
              f"工作区可见={loaded['open']}；文件名={loaded['file']!r}；条目行={loaded['rows']}；"
              f"提示={str(loaded['note'])[:90]!r}；状态条={str(loaded['status'])[:80]!r}",
              clause="§六 从骨架长：新建骨架的产物直接丢进工作区，再逐段 [重跑这段]",
              code="desktop/src/main.ts openPackageInWorkspace")

        # 生成① → 生成②（再生成一份）→ 上一份留作对比 → [采用上一份] 换回来
        await cdp.js("document.getElementById('gw-file').value='p3.json';"
                     "document.getElementById('gw-brief').value='一片灰潮沿岸的三城邦';"
                     "document.getElementById('gw-knob-genre').value='低魔海国';"
                     "window.__confirmArgs=[];"
                     "document.getElementById('gw-generate').click()")
        await asyncio.sleep(6.0)
        first = await cdp.js(
            "({status: document.getElementById('gw-status').textContent,"
            " rows: document.querySelectorAll('#gw-entries .entry-row').length})")
        # 第二份要能与第一份分辨：先改一条（不锁）→ 第二份会把没锁的改动覆盖掉 → 采用上一份能找回来
        await cdp.js("window.prompt=()=> '审计改过的公理';"
                     "document.querySelector('#gw-entries .entry-edit[data-ident=\"ax-1\"]').click();"
                     "document.getElementById('gw-generate').click()")
        await asyncio.sleep(6.0)
        second = await cdp.js(
            "({compare_visible: !document.getElementById('gw-compare-box').classList.contains('hidden'),"
            " compare: document.getElementById('gw-compare').textContent,"
            " ax: [...document.querySelectorAll('#gw-entries .entry-row')].map(e=>e.textContent).find(t=>t.includes('ax-1')) || '',"
            " status: document.getElementById('gw-status').textContent})")
        await cdp.js("document.getElementById('gw-adopt-previous').click()")
        await asyncio.sleep(0.6)
        adopted = await cdp.js(
            "({compare: document.getElementById('gw-compare').textContent,"
            " ax: [...document.querySelectorAll('#gw-entries .entry-row')].map(e=>e.textContent).find(t=>t.includes('ax-1')) || '',"
            " rows: document.querySelectorAll('#gw-entries .entry-row').length})")
        check("P3-② §六「并排对比」：[生成] 再跑一次留上一份 → 逐段条数对比 → [采用上一份] 换回第一份",
              "PASS" if (second["compare_visible"] and "这一份" in str(second["compare"])
                         and "上一份" in str(second["compare"])
                         and "审计改过的公理" not in str(second["ax"])       # 第二份：没锁的改动被模型覆盖
                         and "审计改过的公理" in str(adopted["ax"])) else "FAIL",       # 采用上一份：改动回来了
              f"第一次生成后条目行={first['rows']}；再生成后对比块可见={second['compare_visible']}：{str(second['compare'])[:150]!r}；"
              f"第二份里 ax-1={str(second['ax'])[:70]!r}（没锁的改动被模型覆盖）；"
              f"采用上一份后 ax-1={str(adopted['ax'])[:70]!r}（回到第一份，改动找回来了）、条目行={adopted['rows']}、"
              f"对比块={str(adopted['compare'])[:100]!r}",
              clause="§六 并排对比：同一 brief 生成两份取其一",
              code="desktop/src/main.ts genwsRenderCompare / genwsAdoptPrevious")

        # §3.4 提示词预览 + 核心进度快照
        await cdp.js("document.getElementById('gw-prompt-box').open=true;"
                     "document.getElementById('gw-prompt-box').dispatchEvent(new Event('toggle'))")
        await asyncio.sleep(2.0)
        prompt_ui = await cdp.js("document.getElementById('gw-prompt').textContent")
        snap = await mgmt_call(cdp, "world.generate.snapshot")
        progress = (snap.get("progress") or {})
        prompt_core = (snap.get("prompt") or {})
        check("P3-③ §3.4/§六 提示词预览：界面摊开的正是最近一次调用原文（旋钮句在其中）+ 核心进度快照可读",
              "PASS" if ("只输出一个 JSON 对象" in str(prompt_ui) and "体裁 低魔海国" in str(prompt_ui)
                         and "用户旋钮" in str(prompt_ui)
                         and progress.get("running") is False and str(prompt_core.get("label") or "")
                         and "体裁 低魔海国" in str(prompt_core.get("system"))) else "FAIL",
              f"界面提示词长度={len(str(prompt_ui))}（含旋钮句={'体裁 低魔海国' in str(prompt_ui)}、"
              f"含结构提示={'只输出一个 JSON 对象' in str(prompt_ui)}）；"
              f"核心快照 progress={progress}；prompt.label={str(prompt_core.get('label'))[:40]!r}",
              clause="§3.4 看这次给模型的提示词（只读）／§六 进度可见：生成期间显示正在生成第几段",
              code="desktop/src/main.ts genwsLoadPrompt · isekai_core/world/generator.py last_prompt/progress_snapshot · ops world.generate.snapshot")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
        dump("genws-p3")


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
    if which in ("broken", "all"):
        if not EXE.exists():
            check("broken", "SKIP", f"没有可执行件 {EXE}（先构建桌壳）")
        else:
            await section_broken()
    if which in ("card", "all"):
        if not EXE.exists():
            check("card", "SKIP", f"没有可执行件 {EXE}（先构建桌壳）")
        else:
            await section_card()
    if which in ("p3", "all"):
        if not EXE.exists():
            check("p3", "SKIP", f"没有可执行件 {EXE}（先构建桌壳）")
        else:
            await section_p3()
    print(f"[genws] 用时 {time.time() - started:.1f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(amain())
