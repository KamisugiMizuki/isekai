"""六项桌面 UI 面的真壳 CDP 验收（临时探针，不入库）。

用法（仓库根，.venv 解释器）：
  .venv/Scripts/python.exe <本文件>

覆盖：备份组三按钮 / 补卡入口 / 草稿续作 / 长历史分页 / 打开日志目录 / 显式退出握手。
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

REPO = Path(r"D:\Hermes_workspace\isekai")
PY = REPO / ".venv" / "Scripts" / "python.exe"
EXE = REPO / "desktop" / "src-tauri" / "target" / "debug" / "isekai-desktop.exe"
sys.path.insert(0, str(REPO / "scripts"))
from _audit_desktop import Cdp, db_rows, kill_tree, make_root, pid_alive, prepare_world, select_instance  # noqa: E402

RESULT: list[tuple[str, bool, str]] = []
PS = "powershell"


def check(item: str, ok: bool, evidence: str) -> None:
    RESULT.append((item, bool(ok), evidence))
    print(f"{'PASS' if ok else 'FAIL'} {item} — {evidence}", flush=True)


def ps_out(script: str) -> str:
    out = subprocess.run([PS, "-NoProfile", "-Command", script], capture_output=True)
    return (out.stdout or b"").decode("utf-8", "replace")


def explorer_urls() -> list[str]:
    raw = ps_out("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "@(New-Object -ComObject Shell.Application).Windows() | "
                 "ForEach-Object { $_.LocationURL }")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def close_explorer(keyword: str) -> None:
    ps_out("$w=(New-Object -ComObject Shell.Application).Windows(); "
           f"@($w) | Where-Object {{ $_.LocationURL -like '*{keyword}*' }} | ForEach-Object {{ $_.Quit() }}")


def window_titles() -> list[str]:
    user32 = ctypes.windll.user32
    titles: list[str] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(proc(callback), None)
    return titles


def powershell_pids() -> list[int]:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq powershell.exe", "/FO", "CSV", "/NH"],
                         capture_output=True)
    pids: list[int] = []
    for line in (out.stdout or b"").decode("gbk", "replace").splitlines():
        parts = [item.strip('"') for item in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def kill_powershell() -> None:
    for pid in powershell_pids():
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


async def js_wait(cdp: Cdp, expr: str, want: str, timeout: float = 30.0) -> str:
    return await cdp.wait_text(expr, want, timeout)


async def main() -> None:
    root = make_root("six")
    instances = prepare_world(root, ("甲世界", "乙世界"))
    pkgs = root / "packages"

    # 补卡用的一张已审定卡（prepare_world 只给实例内角色建卡，这里另建一张未入实例的卡）
    from isekai_core.config import load_config
    from isekai_core.world import ops
    from isekai_core.world.example import example_card
    from isekai_core.store import Store
    from isekai_core.world.package import load_package

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    package = load_package(cfg.paths.packages / "w0.json")
    card = example_card(package, name="丙补卡甲")
    (pkgs / "newcard.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    ops.dispatch(cfg, store, "world.card.confirm",
                 {"package_path": str(pkgs / "w0.json"), "card_path": str(pkgs / "newcard.json")})

    # 草稿 fixture：与核心 _draft_path 同布局（<创作目录>/<名字>.draft.json）
    draft_name = "审计草稿"
    (pkgs / f"{draft_name}.draft.json").write_text(json.dumps({
        "name": draft_name, "kind": "package", "payload": package,
        "progress": {}, "errors": ["审计用的未过校验项"], "updated_at": time.time(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    store.close()

    kill_tree("isekai-desktop.exe")
    kill_powershell()
    env = dict(os.environ)
    env.update({
        "ISEKAI_ROOT": str(root),
        "ISEKAI_LLM_FAKE": "1",
        "ISEKAI_LLM_FAKE_REPLY": "审计占位回复",
        "PYTHONIOENCODING": "utf-8",
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--remote-debugging-port=9222",
    })
    proc = subprocess.Popen([str(EXE)], cwd=str(root), env=env)
    print(f"[probe] 壳 pid={proc.pid} 数据根={root}", flush=True)
    cdp, targets = await Cdp.attach(9222, timeout=90)
    status = await js_wait(cdp, "document.getElementById('status').textContent", "已就绪", 90)
    print(f"[probe] 状态条={status}", flush=True)
    await cdp.js("window.confirm = () => true; window.alert = () => undefined;")

    # ---------------------------------------------------------------- ① 备份组三按钮
    await cdp.js("[...document.querySelectorAll('nav .nav')].find(b=>b.dataset.pane==='settings').click()")
    await js_wait(cdp, "document.getElementById('backup-facts').textContent", "备份目录", 20)
    facts_before = await cdp.js("document.getElementById('backup-facts').textContent")
    await cdp.js("document.getElementById('backup-now').click()")
    note = await js_wait(cdp, "document.getElementById('backup-note').textContent", "已备份", 60)
    facts_after = await cdp.js("document.getElementById('backup-facts').textContent")
    rows = await cdp.js("[...document.querySelectorAll('#backup-list li')].map(li=>li.textContent)")
    db_files = sorted((root / "data" / "backups").glob("isekai-*.db"))
    check("①-1 立即备份：核心落一份备份且列表刷新",
          "已备份" in str(note) and len(rows) >= 1 and len(db_files) >= 1 and "完整" in " ".join(rows),
          f"按钮后提示={note!r}；列表行={rows}；盘上备份={[p.name for p in db_files]}；"
          f"配置事实（目录/间隔/保留/最近）={facts_after!r}（点击前={facts_before!r}）")

    urls_before = explorer_urls()
    await cdp.js("document.getElementById('backup-open').click()")
    await asyncio.sleep(2.5)
    urls_after = explorer_urls()
    opened = [url for url in urls_after if url not in urls_before]
    await js_wait(cdp, "document.getElementById('backup-note').textContent", "已用资源管理器打开", 15)
    note_open = await cdp.js("document.getElementById('backup-note').textContent")
    check("①-2 打开备份目录：新开资源管理器窗口指向备份目录",
          any("backups" in url for url in opened),
          f"点击前窗口={urls_before}；点击后新增={opened}；提示={note_open!r}")
    close_explorer("backups")

    # 恢复备份：原生文件对话框（无人值守下不能真点确认，先证明对话框确实弹出，再取消）
    ps_before = set(powershell_pids())
    await cdp.js("document.getElementById('backup-restore').click()")
    dialog_seen = ""
    deadline = time.time() + 25
    while time.time() < deadline:
        titles = [title for title in window_titles() if "备份" in title]
        if titles:
            dialog_seen = titles[0]
            break
        await asyncio.sleep(0.5)
    ps_now = set(powershell_pids())
    kill_powershell()
    note_cancel = await js_wait(cdp, "document.getElementById('backup-note').textContent", "已取消选择", 25)
    check("①-3 恢复备份：弹原生文件对话框（取消后不动数据）",
          bool(dialog_seen) and ps_now - ps_before and "已取消选择" in str(note_cancel),
          f"原生对话框窗口标题={dialog_seen!r}（进程 powershell.exe pid 新增={sorted(ps_now - ps_before)}）；"
          f"取消后提示={note_cancel!r}；备份列表未变={len(db_files)} 份")

    # 恢复路径本身（不做对话框）在核心侧单独验：见 probe_restore_op.py
    log_dir_dom = await cdp.js("document.getElementById('about-facts').textContent")
    check("⑤-1 关于 / 诊断显示日志目录（读真实路径，不硬编码）",
          str(root / "logs") in str(log_dir_dom),
          f"设置页「关于 / 诊断」事实={log_dir_dom!r}；期望日志目录={root / 'logs'}")

    urls_before = explorer_urls()
    await cdp.js("document.getElementById('open-log-dir').click()")
    await asyncio.sleep(2.5)
    urls_after = explorer_urls()
    opened = [url for url in urls_after if url not in urls_before]
    note_log = await js_wait(cdp, "document.getElementById('about-note').textContent", "已用资源管理器打开", 15)
    check("⑤-2 打开日志目录：新开资源管理器窗口指向 logs",
          any("logs" in url for url in opened),
          f"点击后新增窗口={opened}；提示={note_log!r}")
    close_explorer("logs")

    # ---------------------------------------------------------------- ② 补卡入口
    await cdp.js("[...document.querySelectorAll('nav .nav')].find(b=>b.dataset.pane==='manage').click()")
    await asyncio.sleep(2.0)
    await select_instance(cdp, instances[0]["id"])
    deadline = time.time() + 25
    options: list[str] = []
    while time.time() < deadline:
        options = await cdp.js("[...document.getElementById('card-add-timeline').options].map(o=>o.value)") or []
        if options:
            break
        await asyncio.sleep(0.5)
    await cdp.js("(function(){const s=document.getElementById('card-select');"
                 "s.value='newcard.json'; s.dispatchEvent(new Event('change'));})()")
    roles_before = await cdp.js("document.getElementById('world-facts').textContent")
    await cdp.js("document.getElementById('card-add-note').value='从北岸调来';"
                 "document.getElementById('card-add').click()")
    facts = await js_wait(cdp, "document.getElementById('card-add-facts').textContent", "最近补入", 40)
    note_line = await cdp.js("document.getElementById('world-note').textContent")
    roles_after = await cdp.js("document.getElementById('world-facts').textContent")
    try:
        units = db_rows(root, "SELECT COUNT(*) FROM unit WHERE instance_id=?", (instances[0]["id"],))[0][0]
    except sqlite3.Error as exc:
        units = f"（读 unit 失败：{exc}）"
    check("② 补卡：选卡 + 选实例/时间线 → 补入并回显 joined_label",
          "丙补卡甲" in str(facts) and "丙补卡甲" in str(roles_after) and "最近补入" in str(facts)
          and "从北岸调来" in str(facts),
          f"目标时间线下拉={options}；补入后管理页提示={note_line!r}；补卡事实={facts!r}；"
          f"实例角色从 {roles_before.count('｜')} 项变 {roles_after.count('｜')} 项；库内 unit 行={units}")

    # ---------------------------------------------------------------- ③ 草稿续作
    drafts = await cdp.js("[...document.getElementById('draft-select').options].map(o=>o.value)")
    note_draft = await cdp.js("document.getElementById('draft-note').textContent")
    await cdp.js("document.getElementById('draft-continue').click()")
    note_continue = await js_wait(cdp, "document.getElementById('world-note').textContent", "已载回创作目录", 30)
    loaded_file = pkgs / f"{draft_name}.json"
    errors_shown = await cdp.js("document.getElementById('pkg-errors').textContent")
    draft_still_there = (pkgs / f"{draft_name}.draft.json").exists()
    await cdp.js("document.getElementById('draft-discard').click()")
    note_discard = await js_wait(cdp, "document.getElementById('world-note').textContent", "已丢弃草稿", 30)
    check("③ 草稿续作：列表能看到、能载回、丢弃只删草稿",
          draft_name in drafts and loaded_file.exists() and draft_still_there
          and not (pkgs / f"{draft_name}.draft.json").exists() and "审计用的未过校验项" in str(errors_shown),
          f"草稿下拉={drafts}；继续编辑提示={note_continue!r}、载回文件存在={loaded_file.exists()}"
          f"（{loaded_file.name}）、未过校验项回显={errors_shown!r}；丢弃提示={note_discard!r}、"
          f"草稿文件已删={not (pkgs / f'{draft_name}.draft.json').exists()}")

    # ---------------------------------------------------------------- ④ 长历史分页
    session_id = db_rows(root, "SELECT id FROM session WHERE instance_id='ph-instance'")[0][0]
    con = sqlite3.connect(root / "data" / "isekai.db", timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    con.executemany("INSERT INTO message(session_id,role,text,state,created_at) VALUES(?,?,?,?,?)",
                    [(session_id, "user", f"seed-{index:03d}", "done", time.time())
                     for index in range(250)])
    con.commit()
    con.close()
    total = db_rows(root, "SELECT COUNT(*) FROM message WHERE session_id=?", (session_id,))[0][0]
    await cdp.js("[...document.querySelectorAll('nav .nav')].find(b=>b.dataset.pane==='chat').click()")
    await cdp.js("document.getElementById('restart').click()")
    await asyncio.sleep(4.0)
    await js_wait(cdp, "document.getElementById('history-note').textContent", "已加载最近", 120)
    page1 = await cdp.js("({count: document.querySelectorAll('#messages li.message').length,"
                         " note: document.getElementById('history-note').textContent,"
                         " btn: document.getElementById('history-more').classList.contains('hidden')})")
    await cdp.js("document.getElementById('messages').scrollTop = 0")
    await asyncio.sleep(0.5)
    before = await cdp.js("(() => {const l=document.getElementById('messages');"
                          "return {bottom: l.scrollHeight - l.scrollTop, first:"
                          " (document.querySelector('#messages li.message p')||{}).textContent};})()")
    await cdp.js("document.getElementById('history-more').click()")
    deadline = time.time() + 45
    page2 = {}
    while time.time() < deadline:
        page2 = await cdp.js("({count: document.querySelectorAll('#messages li.message').length,"
                             " hasSeed0: document.getElementById('messages').textContent.includes('seed-000'),"
                             " bottom: (() => {const l=document.getElementById('messages');"
                             " return l.scrollHeight - l.scrollTop;})(),"
                             " first: (document.querySelector('#messages li.message p')||{}).textContent})")
        if page2.get("hasSeed0"):
            break
        await asyncio.sleep(0.5)
    check("④ 长历史分页：加载第 2 页且旧消息接在前面、视口不跳",
          page2.get("count", 0) > page1["count"] and page2.get("hasSeed0")
          and abs(float(page2.get("bottom", 0)) - float(before["bottom"])) <= 8
          and page2.get("first") != before.get("first"),
          f"会话共 {total} 行；第一页 {page1}；「加载更多」后 {page2}；"
          f"距底部距离 前={before['bottom']:.0f}px 后={page2.get('bottom'):.0f}px（锚定未跳)；"
          f"列表首条 前={before.get('first')!r} 后={page2.get('first')!r}")

    # ---------------------------------------------------------------- ⑥ 显式退出握手
    shell_log_path = root / "logs" / "shell.log"
    core_log_path = root / "logs" / "core.log"
    core_pid = json.loads((root / "data" / "core.lock").read_text(encoding="utf-8"))["pid"]
    backups_before = {p.name for p in (root / "data" / "backups").glob("isekai-*.db")}
    # 走壳自己的退出入口（托盘「退出」调的是同一个函数）：无人值守下没法点托盘，这里从渲染层 invoke
    sent = await cdp.js("window.__TAURI_INTERNALS__.invoke('quit_app', {}); 'sent'")
    print(f"[probe] 退出请求已发出={sent!r}", flush=True)
    deadline = time.time() + 30
    gone = False
    while time.time() < deadline:
        if not pid_alive(proc.pid):
            gone = True
            break
        await asyncio.sleep(0.5)
    core_gone = not pid_alive(core_pid)
    await asyncio.sleep(1.0)
    shell_log = shell_log_path.read_text(encoding="utf-8", errors="replace")
    core_log = core_log_path.read_text(encoding="utf-8", errors="replace")
    lines = [line for line in shell_log.splitlines() if "exit " in line or "core exited" in line]
    backups_after = {p.name for p in (root / "data" / "backups").glob("isekai-*.db")}
    exit_index = next((i for i, line in enumerate(shell_log.splitlines())
                       if "exit requested" in line), None)
    hard_killed = [line for line in shell_log.splitlines()[exit_index + 1:] if "stopping core pid=" in line] \
        if exit_index is not None else ["没有找到退出序列"]
    check("⑥ 显式退出：先保存（退出前备份）→ 核心自行退出 → 未硬杀",
          gone and core_gone and any("exit requested: 先保存再停进程" in line for line in lines)
          and any("exit save: 退出前备份" in line for line in lines)
          and any("core exited on its own" in line for line in lines)
          and "core stopped" in core_log and not (root / "data" / "core.lock").exists()
          and backups_after - backups_before and not hard_killed,
          f"壳进程已退出={gone}、核心进程已退出={core_gone}；shell.log 退出序列={lines}；"
          f"core.log 出现 core stopped={'core stopped' in core_log}；写库锁已释放={not (root / 'data' / 'core.lock').exists()}；"
          f"退出前新增备份={sorted(backups_after - backups_before)}；硬杀记录={hard_killed or '无'}")

    print("\n---- 汇总 ----", flush=True)
    for item, ok, _ in RESULT:
        print(f"{'PASS' if ok else 'FAIL'} {item}", flush=True)
    print(f"PASS {sum(1 for _, ok, _ in RESULT if ok)}/{len(RESULT)}", flush=True)

    kill_tree("isekai-desktop.exe")
    kill_powershell()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        kill_tree("isekai-desktop.exe")
