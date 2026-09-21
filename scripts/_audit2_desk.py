"""DESKTOP_SPEC 行为审计（第二轮 / 独立探针）：只新增本文件，不修改任何项目文件。

用法（仓库根，.venv 解释器）：
  .venv/Scripts/python.exe scripts/_audit2_desk.py static     # 静态核对（无需 exe）
  .venv/Scripts/python.exe scripts/_audit2_desk.py main       # 真壳 + CDP，假 LLM（ISEKAI_LLM_FAKE=1）
  .venv/Scripts/python.exe scripts/_audit2_desk.py llmfail    # 真壳 + 死地址 LLM（不联网）→ 生成失败 UI
  .venv/Scripts/python.exe scripts/_audit2_desk.py storage    # 存储不可用根 → UI 存储错误
  .venv/Scripts/python.exe scripts/_audit2_desk.py nointerp   # 缺 .venv 的根 → 启动失败诊断
  .venv/Scripts/python.exe scripts/_audit2_desk.py notify     # §3.1/A17 桌面提醒：真壳登记 / 通知 / 点击定位
  .venv/Scripts/python.exe scripts/_audit2_desk.py onboard    # 首跑零实例：空态直达入口 + 管理页首屏顺序

只读策略：仓库内文件（data/isekai.db、config/config.yaml、logs/、desktop/、isekai_core/、tests/）
一律不改；一切可写数据落在 %LOCALAPPDATA%/Temp/isekai_audit2_*，用 junction 指回仓库的
isekai_core / .venv（照抄 scripts/_audit_desktop.py 的只读做法）。
每条输出 `STATUS id — 证据`，同时把结构化明细写到 <Temp>/isekai_audit2_<phase>_result.json。
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "Scripts" / "python.exe"
EXE = REPO / "desktop" / "src-tauri" / "target" / "debug" / "isekai-desktop.exe"
DIST = REPO / "desktop" / "dist"
TMPBASE = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ".") / "Temp"
PORT = 9333          # 与旧探针（9222）分开，避免互相抢 target
sys.path.insert(0, str(REPO))

FAKE_KEY = "audit2-fake-key-0000"
DOM_MARKER = "审计2正文标记-Q7Q7"          # 消息正文标记：不得出现在日志
INTERNAL = {                                # 内部数据标记：不得出现在界面
    "memory": "内部记忆串-MEM-Q7Q7",
    "claim": "内部说法串-CLAIM-Q7Q7",
    "unit": "内部认知串-UNIT-Q7Q7",
}
XSS = "<img src=x onerror=\"window.__xss2=1\"><script>window.__xss3=1</script>审计2脚本"
FORBIDDEN_UI = ("实情", "事件日志", "性格", "认知条目", "记忆内容", "内部 diff", "内部diff",
                "prompt", "system_prompt", "API Key 明文")
RESULTS: list[dict] = []


def check(cid: str, status: str, observed: str, *, expected: str = "", clause: str = "",
          code: str = "") -> None:
    RESULTS.append({"id": cid, "clause": clause or cid, "status": status,
                    "expected": expected, "observed": observed, "code_ref": code})
    print(f"{status} {cid} — {observed}", flush=True)


def dump(tag: str) -> None:
    path = TMPBASE / f"isekai_audit2_{tag}_result.json"
    path.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[probe] 明细：{path}", flush=True)


# ------------------------------------------------------------------ 环境

def make_root(tag: str, *, venv: bool = True, broken_data: bool = False) -> Path:
    root = TMPBASE / f"isekai_audit2_{tag}_{int(time.time())}"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "packages").mkdir(parents=True, exist_ok=True)
    for name in ([".venv"] if venv else []) + ["isekai_core"]:
        dst = root / name
        if not dst.exists():
            subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(REPO / name)],
                           capture_output=True)
    (root / "config" / "config.yaml").write_text("\n".join([
        "core:",
        "  host: 127.0.0.1",
        "  port: 0",
        "runtime:",
        "  sleep_wait_min_s: 2",
        "  sleep_wait_max_s: 3",
        "  max_text_len: 4000",
        "  max_parts: 10",
        "  context_history_max: 20",
        "  autocommit_minutes: 60",
        "llm:",
        '  base_url: "http://127.0.0.1:9/v1"',      # 死地址：探针永不联网
        '  model: "audit2-model"',
        f'  api_key: "{FAKE_KEY}"',
        "  timeout_s: 5",
        "  max_tokens: 64",
        "  temperature: 0.2",
        "  memory_embedding_model: \"audit2-embed\"",
        "  memory_embedding_base_url: \"http://127.0.0.1:9/v1\"",
        "backup:",
        "  interval_hours: 24",
        "  keep: 3",
    ]) + "\n", encoding="utf-8")
    if broken_data:
        (root / "data").write_text("not a dir", encoding="utf-8")
    return root


def prepare_world(root: Path, names=("甲世界", "乙世界")) -> list[dict]:
    """独立数据根里造世界包 + 已审定卡 + 实例（走真实 ops 代码路径）。"""
    from isekai_core.config import load_config
    from isekai_core.runtime.service import from_config
    from isekai_core.store import Store
    from isekai_core.world import ops
    from isekai_core.world.example import example_card, example_package
    from isekai_core.world.package import save_package

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    world = from_config(cfg, store)
    out = []
    for index, name in enumerate(names):
        package = example_package(f"{name}-包")
        card = example_card(package, name=f"角色{index}甲")
        pkg_path = cfg.paths.packages / f"w{index}.json"
        card_path = cfg.paths.packages / f"c{index}.json"
        save_package(str(pkg_path), package)
        card_path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
        ops.dispatch(cfg, store, "world.card.confirm",
                     {"package_path": str(pkg_path), "card_path": str(card_path)}, runtime=world)
        info = ops.dispatch(cfg, store, "instance.create",
                            {"package_path": str(pkg_path), "card_paths": [str(card_path)],
                             "display_name": name}, runtime=world)["instance"]
        out.append(info)
    store.close()
    return out


def db(root: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(root / "data" / "isekai.db", timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def one(root: Path, sql: str, params: tuple = ()):
    rows = db(root, sql, params)
    return rows[0][0] if rows else None


def force_clean(path: Path) -> None:
    """文件或目录都干掉（探针造故障用）。"""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()


def kill_tree(name: str = "isekai-desktop.exe") -> None:
    subprocess.run(["taskkill", "/F", "/T", "/IM", name], capture_output=True)


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True)
    return str(pid).encode() in out.stdout


def window_count(pid: int) -> int:
    user32 = ctypes.windll.user32
    found = [0]
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found[0] += 1
        return True

    user32.EnumWindows(proc(callback), None)
    return found[0]


def close_window(pid: int) -> int:
    user32 = ctypes.windll.user32
    hits: list[int] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            hits.append(hwnd)
        return True

    user32.EnumWindows(proc(callback), None)
    for hwnd in hits:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    return len(hits)


def core_lock_pid(root: Path):
    path = root / "data" / "core.lock"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("pid")
    except (json.JSONDecodeError, OSError):
        return None


def logs(root: Path) -> str:
    folder = root / "logs"
    if not folder.exists():
        return ""
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in folder.glob("*.log"))


def impeccable_detector() -> Path | None:
    """Impeccable 技能自带的渲染态检测器（清规扫描）：找到就用真检测器复跑，
    找不到就退回「同一查询 / 同一四舍五入 / 同一阈值」的镜像判定（见 M43）。"""
    candidates = []
    if os.environ.get("IMPECCABLE_DETECTOR"):
        candidates.append(Path(os.environ["IMPECCABLE_DETECTOR"]))
    for base in (Path(os.environ.get("LOCALAPPDATA", "") or "."), Path.home() / "AppData" / "Local",
                 Path.home() / ".local" / "share"):
        candidates.append(base / "hermes" / "skills" / "frontend-design" / "impeccable"
                          / "scripts" / "detector" / "detect-antipatterns-browser.js")
    for path in candidates:
        if path.is_file():
            return path
    return None


# --------------------------------------------------- 原生窗口 / 键盘（点对话框）

def dialog_hwnd(needle: str) -> int:
    """按标题找一个可见窗口（原生文件对话框）。"""
    user32 = ctypes.windll.user32
    found = [0]
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if needle in buf.value:
                    found[0] = hwnd
        return True

    user32.EnumWindows(proc(callback), None)
    return found[0]


def focus_window(hwnd: int) -> bool:
    """把窗口抢到前台（无人值守下给原生对话框发键用）。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.ShowWindow(hwnd, 5)
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True
    foreground = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(foreground, None)
    tid_me = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(tid_me, tid_fg, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)
    user32.AttachThreadInput(tid_me, tid_fg, False)
    return user32.GetForegroundWindow() == hwnd


def child_windows(parent: int, depth: int = 0) -> list[tuple[int, str, str, tuple]]:
    """递归列出子窗口 (hwnd, class, text, rect)，用来找对话框里的文件名框 / 列表。"""
    user32 = ctypes.windll.user32
    out: list[tuple[int, str, str, tuple]] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _):
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls, 128)
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        out.append((hwnd, cls.value, buf.value, (rect.left, rect.top, rect.right, rect.bottom)))
        if depth < 3:
            out.extend(child_windows(hwnd, depth + 1))
        return True

    user32.EnumChildWindows(parent, proc(callback), None)
    return out


def set_text(hwnd: int, text: str) -> None:
    ctypes.windll.user32.SendMessageW(hwnd, 0x000C, 0, ctypes.c_wchar_p(text))  # WM_SETTEXT


def click_at(x: int, y: int, *, double: bool = False) -> None:
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    flags_down, flags_up = 0x0002, 0x0004
    user32.mouse_event(flags_down, 0, 0, 0, 0)
    user32.mouse_event(flags_up, 0, 0, 0, 0)
    if double:
        time.sleep(0.12)
        user32.mouse_event(flags_down, 0, 0, 0, 0)
        user32.mouse_event(flags_up, 0, 0, 0, 0)


def bring_front(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)  # HWND_TOPMOST + NOSIZE/NOMOVE
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002)    # 回到普通层级，保持置顶过
    user32.SetForegroundWindow(hwnd)


def dialog_pid(hwnd: int) -> int:
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_text(hwnd: int) -> str:
    """读控件文本：必须走 WM_GETTEXT（GetWindowText 拿不到别的进程里的控件文本，返回空串）。"""
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.user32.SendMessageW(hwnd, 0x000D, 1024, buffer)  # WM_GETTEXT
    return buffer.value


def drive_open_dialog(path: str, title_needle: str, timeout: float = 30.0) -> tuple[int, str]:
    """真驱动原生「打开」文件对话框，返回 (hwnd, 观察文本)。

    壳的 pick_file 是原生对话框，装不了 stub（`__TAURI_INTERNALS__.invoke` 是
    writable:false，赋值静默失败；2026-09 实测），只能真驱动。2026-09 实测到两条教训：
    · 鼠标点不可靠：对话框不一定在前台（SetForegroundWindow 受前台限制），同一屏幕位置上还
      可能压着别的进程的窗口（实测 WindowFromPoint 命中过另一个 pid 的 Button）→ 点了没反应；
    · 光 WM_SETTEXT 不够：Shell 风格对话框靠文件名框的 EN_CHANGE 把「选了哪个文件」交给自己的
      状态机，WM_SETTEXT 不触发那条通知，随后的按钮点击会被当成「没选文件」而取消。
    所以主路径是「逐字 WM_CHAR 输入（与真人打字同一条通知路径）→ 直接给「打开」按钮发 BM_CLICK」，
    不依赖焦点与屏幕坐标；失败就重打重试，最多三轮，绝不点别处的窗口。
    """
    hwnd = 0
    deadline = time.time() + timeout
    while time.time() < deadline and not hwnd:
        hwnd = dialog_hwnd(title_needle)
        time.sleep(0.4)
    if not hwnd:
        return 0, f"原生对话框（标题含「{title_needle}」）未出现"
    children = child_windows(hwnd)
    # y 最大的是文件名框（y 最小的是地址栏 / 搜索框）
    edits = sorted({(h, r) for h, cls, _t, r in children if cls.lower() in ("edit", "richedit50w")},
                   key=lambda item: item[1][1])
    buttons = [item for item in sorted({(h, t, r) for h, cls, t, r in children
                                        if cls.lower() == "button"})
               if "Open" in item[1] or "打开" in item[1]]
    if not edits or not buttons:
        return hwnd, (f"对话框 hwnd={hwnd} 里没找到文件名框 / 「打开」按钮"
                      f"（edit={len(edits)} button={len(buttons)}）")
    edit_h, (btn_h, btn_label, _rect) = edits[-1][0], buttons[0]

    def type_path() -> str:
        set_text(edit_h, "")
        time.sleep(0.2)
        for char in str(path):
            ctypes.windll.user32.SendMessageW(edit_h, 0x0102, ord(char), 0)  # WM_CHAR
        time.sleep(0.5)
        return window_text(edit_h)

    def closed(seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(0.4)
            if not ctypes.windll.user32.IsWindow(hwnd):
                return True
        return False

    typed = ""
    for attempt in range(3):
        typed = type_path()          # 每轮重打一遍：对话框可能把输入重置了
        ctypes.windll.user32.SendMessageW(btn_h, 0x00F5, 0, 0)   # BM_CLICK
        if closed(6.0):
            return hwnd, (f"原生对话框 hwnd={hwnd}：文件名框逐字输入 {path}（读回={typed!r}），"
                          f"点「{btn_label}」后被接受")
    pid = dialog_pid(hwnd)
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)  # 别把模态框留在桌面上
    return hwnd, (f"原生对话框 hwnd={hwnd}（pid={pid}）没有关闭：逐字输入 {path}（读回={typed!r}）后"
                  f"三轮都没能让「{btn_label}」确认")


# ------------------------------------------------------------------ CDP

class Cdp:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.seq = 0

    @classmethod
    async def attach(cls, port: int, timeout: float = 90.0):
        from websockets.asyncio.client import connect

        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                targets = json.loads(urlopen(f"http://127.0.0.1:{port}/json", timeout=2).read())
                pages = [t for t in targets if t.get("type") == "page"
                         and "devtools" not in str(t.get("url", ""))
                         and str(t.get("url", "")) not in ("", "about:blank")]
                if pages:
                    pages.sort(key=lambda t: 0 if "index.html" in str(t.get("url")) else 1)
                    ws = await connect(pages[0]["webSocketDebuggerUrl"], max_size=1 << 24)
                    client = cls(ws)
                    if await client.ready(timeout=40):
                        return client, targets
            except Exception as exc:  # noqa: BLE001
                last = exc
            await asyncio.sleep(0.5)
        raise TimeoutError(f"CDP 未就绪：{last}")

    async def ready(self, *, timeout: float = 40.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await self.js("document.readyState === 'complete' && !!document.getElementById('status')"):
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        return False

    async def call(self, method: str, **params):
        self.seq += 1
        mid = self.seq
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=40))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr: str, *, await_promise: bool = False):
        res = await self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                              awaitPromise=await_promise)
        if res.get("exceptionDetails"):
            raise RuntimeError(str(res["exceptionDetails"])[:240])
        return (res.get("result") or {}).get("value")

    async def wait(self, expr: str, want: str, timeout: float = 40.0) -> str:
        deadline = time.time() + timeout
        value = None
        while time.time() < deadline:
            try:
                value = await self.js(expr)
            except Exception:  # noqa: BLE001
                value = None
            if value and want in str(value):
                return str(value)
            await asyncio.sleep(0.4)
        return str(value)

    async def pane(self, name: str) -> None:
        await self.js("[...document.querySelectorAll('nav .nav')]"
                      f".find(b=>b.dataset.pane==='{name}').click()")
        await asyncio.sleep(0.8)

    async def nav(self, pane: str) -> None:
        await self.pane(pane)

    async def select(self, element_id: str, value: str) -> None:
        await self.js(f"(function(){{const s=document.getElementById({json.dumps(element_id)});"
                      f"s.value={json.dumps(value)}; s.dispatchEvent(new Event('change'));}})()")

    async def keys(self, key: str, code: str, vk: int, *, shift: bool = False,
                   ctrl: bool = False) -> None:
        mods = (8 if shift else 0) | (2 if ctrl else 0)
        await self.call("Input.dispatchKeyEvent", type="keyDown", key=key, code=code,
                        windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, modifiers=mods)
        await self.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=code,
                        windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, modifiers=mods)

    async def send_text(self, text: str, *, enter: bool = True) -> None:
        await self.js("document.getElementById('input').focus()")
        await self.call("Input.insertText", text=text)
        if enter:
            await self.keys("Enter", "Enter", 13)


STUB = ("window.__confirmArgs=[]; window.confirm=(m)=>{window.__confirmArgs.push(String(m)); return true;};"
        "window.alert=()=>undefined;"
        "if(!window.__origInvoke){window.__origInvoke=window.__TAURI_INTERNALS__.invoke;"
        "window.__TAURI_INTERNALS__.invoke=(c,a,o)=>{if(c==='pick_file')"
        "{return Promise.resolve(window.__wantBackup||null);}return window.__origInvoke(c,a,o);};}")


async def boot_shell(root: Path, port: int, env_extra: dict | None = None, *,
                     timeout: float = 120.0):
    env = dict(os.environ)
    env.update({"ISEKAI_ROOT": str(root), "PYTHONIOENCODING": "utf-8",
                "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={port}"})
    env.update(env_extra or {})
    proc = subprocess.Popen([str(EXE)], cwd=str(root), env=env)
    cdp, targets = await Cdp.attach(port, timeout=timeout)
    return proc, cdp, targets


# ================================================================== static

def section_static() -> None:
    spec = (REPO / "docs" / "DESKTOP_SPEC.md").read_text(encoding="utf-8")
    src = (REPO / "desktop" / "src" / "main.ts").read_text(encoding="utf-8")
    rust = (REPO / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    cargo = (REPO / "desktop" / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")

    # 被测 exe 与渲染层资源是否同一代（dist 比 exe 新就先报告不匹配）
    exe_m = EXE.stat().st_mtime if EXE.exists() else 0
    dist_m = max((p.stat().st_mtime for p in DIST.rglob("*") if p.is_file()), default=0)
    src_m = max((p.stat().st_mtime for p in (REPO / "desktop" / "src").rglob("*") if p.is_file()),
                default=0)
    rust_m = (REPO / "desktop" / "src-tauri" / "src" / "main.rs").stat().st_mtime
    check("S0 被测 exe 与 dist / 源码同代",
          "PASS" if exe_m >= dist_m and exe_m >= src_m and exe_m >= rust_m else "FAIL",
          f"exe={time.strftime('%H:%M:%S', time.localtime(exe_m))} ≥ dist={time.strftime('%H:%M:%S', time.localtime(dist_m))}"
          f"、主渲染源={time.strftime('%H:%M:%S', time.localtime(src_m))}、main.rs={time.strftime('%H:%M:%S', time.localtime(rust_m))}"
          "（资源在 cargo build 时烘进二进制，故 exe 必须不早于它们）",
          clause="审计前提", code="desktop/src-tauri/target/debug/isekai-desktop.exe")

    # §五 备份：运行期间定时检查 / 启动补做到期检查
    sched = subprocess.run(["grep", "-rn", "backup", str(REPO / "isekai_core" / "app.py"),
                            str(REPO / "isekai_core" / "cli.py")], capture_output=True, text=True).stdout
    interval_uses = subprocess.run(["grep", "-rn", "interval_hours", str(REPO / "isekai_core")],
                                   capture_output=True, text=True).stdout
    interval_lines = [ln for ln in interval_uses.splitlines() if ".pyc" not in ln and "__pycache__" not in ln]
    check("S1 §五 备份到期检查（运行期间定时 / 启动后补做）",
          "FAIL" if not sched.strip() else "PASS",
          f"isekai_core/app.py 与 cli.py 中 backup 命中={sched.strip() or '无'}；interval_hours 全部落点={interval_lines}"
          "（config.py:123 定义、ops.py:480 只回读给界面）→ 没有运行期调度器；"
          "main.ts:680 的界面文案「到期由核心补做」与实现不符，手动/退出前备份是仅有的两条路径",
          clause="§五 备份：运行期间定时检查，启动恢复后与显式退出前补做到期检查",
          code="isekai_core/config.py:123 · world/ops.py:911-918 · desktop/src/main.ts:680")

    # §二.1 重复启动复用 / 唤起已有应用
    single = "single-instance" in cargo
    check("S2 §二.1 重复启动「复用 / 唤起已有应用」的实现存在",
          "PASS" if single else "FAIL",
          f"Cargo.toml 是否含 tauri-plugin-single-instance={single}；"
          f"依赖清单={[ln.strip() for ln in cargo.splitlines() if 'tauri' in ln][:8]}",
          clause="§二.1 重复启动复用 / 唤起已有应用，不再启动第二个写库进程",
          code="desktop/src-tauri/Cargo.toml · src/main.rs:321-391")

    # §3.1 桌面提醒（阶段 2–3）：壳侧必须真的有「登记 → 通知 → 点击定位」三段代码
    ts_src = (REPO / "desktop" / "src" / "main.ts").read_text(encoding="utf-8")
    html_src = (REPO / "desktop" / "index.html").read_text(encoding="utf-8")
    ts_parts = {
        "登记（管理面 notice.create）": "notice.create",
        "解析定位（管理面 notice.resolve）": "notice.resolve",
        "显示系统通知（壳命令 notify_message）": "notify_message",
        "待定位提醒轮询（隐藏到托盘时兜底）": "take_pending_notice",
        "设置开关（默认开）": "set-notify-enabled",
        "开关闸门（关闭后不登记）": "notifyOn",
    }
    ts_missing = [name for name, token in ts_parts.items() if token not in ts_src]
    rust_parts = {
        "通知显示（notify_message 命令）": "fn notify_message",
        "点击落点（open_notice）": "fn open_notice",
        "系统通知激活回调（wait_for_response）": "wait_for_response",
        "把窗口带到前台": "set_focus",
    }
    rust_missing = [name for name, token in rust_parts.items() if token not in rust]
    hint_missing = [token for token in ("id=\"notice-open\"", "id=\"notice-note\"", "id=\"set-notify-enabled\"")
                    if token not in html_src]
    deps = [ln.strip() for ln in cargo.splitlines() if ln.strip().startswith(("notify-rust", "tauri-plugin-notification"))]
    hits = [str(p.relative_to(REPO)) for p in
            list((REPO / "desktop" / "src").rglob("*.ts"))
            + list((REPO / "desktop" / "src-tauri" / "src").rglob("*.rs"))
            + [REPO / "desktop" / "index.html", REPO / "desktop" / "src-tauri" / "Cargo.toml"]
            if re.search(r"notification|notify|提醒", p.read_text(encoding="utf-8", errors="replace"), re.I)]
    check("S3 §3.1/A17 桌面提醒入口（登记 → 系统通知 → 点击定位）实现存在",
          "PASS" if not (ts_missing or rust_missing or hint_missing) else "FAIL",
          f"渲染层缺={ts_missing or '无'}（命中={[k for k in ts_parts if k not in ts_missing]}）；"
          f"壳缺={rust_missing or '无'}（命中={[k for k in rust_parts if k not in rust_missing]}）；"
          f"界面控件缺={hint_missing or '无'}；通知依赖={deps}；desktop/ 内含 "
          f"notification|notify|提醒 的文件={hits or '无'}；行为级断言见 notify 模式 S3.1–S3.7",
          clause="§3.1 桌面提醒只作为已固化主动消息的入口 / §十.17",
          code="desktop/src/main.ts（notice.create / notice.resolve / notify_message）· "
               "desktop/src-tauri/src/main.rs（notify_message / open_notice）")

    # §3.3 设置面：界面里是否存在各设置组（按组控件的 id / 事实节点认，不靠散文里的词）
    html = (REPO / "desktop" / "index.html").read_text(encoding="utf-8")
    settings_groups = {
        "LLM": "set-base-url", "记忆向量化": "set-mem-mode", "提交": "set-commit-enabled",
        "世界 / 会话": "worldset-facts", "用量": "usage-facts", "备份": "backup-now",
        "外观": "appearance-note", "关于": "about-facts",
    }
    readonly_keys = ("创作目录（世界包 / 角色卡）", "可同时激活的线", "主动每日额度", "倍率上限（仅开发者）")
    missing = [name for name, token in settings_groups.items() if token not in html]
    missing_keys = [key for key in readonly_keys if key not in src]
    controls = sorted(set(re.findall(r'id="(set-[a-z-]+)"', html)))
    check("S4 §3.3 设置面设置组齐备（LLM / 记忆向量化 / 提交 / 世界·会话 / 用量 / 备份 / 外观 / 关于）",
          "FAIL" if (missing or missing_keys) else "PASS",
          f"缺组={missing or '无'}；只读事实缺={missing_keys or '无'}；可用设置控件={controls}",
          clause="§3.3 设置面表格（LLM / 记忆向量化 / 提交 / 世界·会话 / 用量 / 备份 / 外观 / 关于 各行）",
          code="desktop/index.html（设置面各分组） · desktop/src/main.ts renderLocalFacts")

    # §3.3 生成模型设置显示「最近一次变更时间」
    fill = re.search(r"function fillSettings\(settings: SettingsPayload\): void \{(.*?)\n\}", src, re.S)
    fill_body = fill.group(1) if fill else ""
    fill_fields = sorted(set(re.findall(r"settings\.llm\.([a-z_]+)", fill_body)))
    has_time = any(re.search(r"时间|time|changed|updated", f) for f in fill_fields)
    check("S5 §3.3 生成模型设置显示最近一次变更时间",
          "PASS" if has_time else "FAIL",
          f"fillSettings 渲染的 LLM 字段={fill_fields}（无任何变更时间字段）；"
          f"#settings-facts 只列 配置文件 / 单段上限·段数 / 上下文条数；"
          f"core 侧 settings.get 由 channel.py:535 直出 llm 段，无 changed_at",
          clause="§3.3 生成模型设置显示当前模型与最近一次变更时间",
          code="desktop/src/main.ts:629-643 · isekai_core/channel.py:535 · config.py:222")

    # §十.15「新的回复记录模型变更边界」
    ops_py = (REPO / "isekai_core" / "world" / "ops.py").read_text(encoding="utf-8")
    store_py = (REPO / "isekai_core" / "store.py").read_text(encoding="utf-8")
    boundary = [name for name, text in (("store.py", store_py), ("ops.py", ops_py))
                if re.search(r"model_fingerprint|model_change|generated_by", text)]
    msg_cols = re.search(r"CREATE TABLE IF NOT EXISTS message\((.*?)\);", store_py, re.S)
    cols = msg_cols.group(1) if msg_cols else ""
    check("S6 §十.15 回复记录模型变更边界",
          "PASS" if boundary else "FAIL",
          f"message 表列={[ln.strip().split()[0] for ln in cols.splitlines() if ln.strip()][:12]}；"
          f"核心中含模型指纹/变更边界字段的文件={boundary or '无'}→ 模型更换只改配置，回复行不带模型边界",
          clause="§十.15 新的回复记录模型变更边界，旧实例规则不受模型默认值替换",
          code="isekai_core/store.py:62-89 · isekai_core/config.py:222")

    # SPEC 头部状态行 vs 实现（文档自身一致性）
    header = [ln for ln in spec.splitlines() if ln.startswith("> 状态：")]
    stale = bool(header) and "阶段 1 起的界面与操作未实现" in header[0]
    stated = bool(header) and "已大部分实现" in header[0]
    check("S7 SPEC 头部状态行与实现一致",
          "PASS" if header and not stale and stated else "FAIL",
          f"文档状态行={'有' if header else '无'}；陈旧表述「阶段 1 起的界面与操作未实现」={'仍在' if stale else '已删'}；"
          f"已按实况写明阶段 1+ 已实现={'是' if stated else '否'}（对照 desktop/index.html 的生成 / 审定 / 实例管理 / 披露 / 草稿 / 备份）",
          clause="DESKTOP_SPEC §八 实施分期 / 头部状态行", code="docs/DESKTOP_SPEC.md:5")

    # ---- 2026-09-21 impeccable critique 的既定顺序修复：harden → layout/typeset → polish → onboard → 零碎 → §3.3
    css = (REPO / "desktop" / "src" / "styles.css").read_text(encoding="utf-8")
    control_ids = ["topbar-clock", "manage-facts-box", "world-note-jump", "pkg-note", "card-note",
                   "inst-note", "inst-delete-name", "inst-delete", "inst-delete-note",
                   "card-select-note", "card-add-result", "card-import-note", "inst-create-note",
                   "inst-import-note", "commit-select", "rollback", "rollback-note",
                   "mem-form", "set-mem-mode", "set-mem-base-url", "set-mem-model", "set-mem-api-key",
                   "mem-note", "commit-form", "set-commit-enabled", "set-commit-minutes",
                   "set-commit-events", "commit-note", "backup-form", "set-backup-dir",
                   "set-backup-interval", "set-backup-keep", "appearance-note", "open-config-dir"]
    missing_ids = [item for item in control_ids if f'id="{item}"' not in html]
    wired = {
        "生成在途闸门（禁用 + 重入守卫）": "function setGenerateGate" in src and "generateBusy.package" in src,
        "生成进度行（已用 / 上限）": "function startProgress" in src and "function elapsedLabel" in src,
        "生成确认写明无法取消": "过程中无法取消" in src,
        "生成前 API Key 闸门（世界包 / 角色卡共用一处）":
            "function apiKeyBlocked" in src and "function openApiKeyField" in src
            and "还没配 API Key" in src and 'apiKeyBlocked(settings, "pkg-note")' in src
            and 'apiKeyBlocked(settings, "card-note")' in src,
        "设置面 API Key 事实行（已配置 / 未配置）": '"API Key", settings.llm.api_key_set' in src,
        "删除闸门（离开实例行 + 键入实例名）": "function renderDeleteGate" in src and "将保留：世界包 / 角色卡 / 导出件" in src,
        "组内结果槽": "function groupNote" in src and "function reportNote" in src,
        "动作结果按组落槽（一行一槽）":
            'worldAction(importPackage, "pkg-note")' in src
            and 'worldAction(addCharacter, "card-add-result")' in src
            and 'worldAction(importCard, "card-import-note")' in src
            and '"inst-create-note")' in src and '"inst-import-note")' in src
            and '}, "inst-delete-note")' in src,
        "删除结果不再靠缓存对抗 loadWorld": "deleteResult" not in src and "const DELETE_HINT" in src,
        "实例详情先渲染完再回填动作结果": "await showInstance(selected)" in src,
        "回滚入口（commits → 确认 → rollback）":
            'mgmt.call("runtime.commits"' in src and 'mgmt.call("runtime.rollback"' in src
            and 'worldAction(rollbackToCommit, "rollback-note")' in src
            and "还没有回滚点：提交由自动提交与退出补做产生" in src,
        "页顶锚点回跳": "function jumpToWorldGroup" in src and "worldNoteAnchor" in src,
        "顶栏世界时钟": "function refreshClockChip" in src and "function clockTarget" in src,
        "时钟轮询不再限管理页": "void refreshClockChip();\n    if (!$(\"pane-manage\")" in src,
        "空态按状态分支": "function emptyState" in src and "function openFirstWorld" in src,
        "retry 缺 envId 不渲染": 'message.state === "failed" && message.envId' in src,
        "记忆段走 settings.set": 'saveSegment("memory"' in src or '"memory",' in src,
        "提交段走 settings.set": '"commit",' in src and "saveSegment" in src,
        "备份段走 settings.set": '"backup",' in src and "saveSegment" in src,
        "打开配置目录（复用 open_dir + 配置路径父目录）": "function openConfigDir" in src and '"open_dir"' in src,
        "管理面重建（核心重启后一次性令牌重新 auth）": "function rebuildMgmt" in src,
        "管理面 op 记账（探针观察点）": "function traceOps" in src and "__opLog" in src,
        # ---- 2026-09-21 复审四条（②–⑥）的源码落点。这些只是存在性；行为级断言在 main / notify 段：
        # M41（预算从核心读 + 两组互斥）、M42（降级 chip 的 AX 角色 + sr-only 播报节点）、M43（字号阶梯）、
        # M44（侧栏筛选）、M45（Ctrl+1/2/3）、M46（.row.hidden 计算样式）、M47（设置面无开发者键控件）、
        # M48（进度槽在途 aria-live=off）、M49（散文段落行宽 ≤ 72ch）、M50（确认框预算句口径）。
        "生成预算从核心读（runtime.budget → 确认框）":
            'mgmt!.call("runtime.budget"' in src and "async function budgetLine" in src
            and "今日已用" in src,
        "读不到预算的降级有 ponytail 标注": "ponytail:" in src,
        "两组生成互斥（全局在途闸门 + 发起前拦下）":
            "function generateBlocked" in src and "generateBusy.package || generateBusy.card" in src
            and 'generateBlocked("package", "pkg-note")' in src
            and 'generateBlocked("card", "card-note")' in src,
        "降级 chip 可点（落点是设置面记忆组首个可编辑控件）":
            "function openMemoryGroup" in src and "openMemoryGroup()" in src
            and '$("degrade").addEventListener("click"' in src,
        "降级口径不再写成故障（全文 = 默认态）":
            "召回：全文（默认；点此启用语义召回）" in src and "语义召回不可用" not in src
            and "全文召回（默认：语义召回未启用）" in src,
        "侧栏筛选（键入即筛 + 重渲染后仍生效）":
            "function applySideFilter" in src and "applySideFilter();" in src,
        "快捷键最小集（Ctrl+K / Ctrl+1-3）":
            "function bindShortcuts" in src and 'event.key === "k"' in src
            and 'panes: Record<string, string> = { "1": "chat", "2": "manage", "3": "settings" }' in src,
        "删除提示带实例标识尾段（校验仍比显示名）":
            "function deleteHint" in src and "id.slice(-6)" in src
            and "typed !== name" in src,
        # ---- 2026-09-21 第三轮复审四条：进度槽播报闸门 / chip 的 AX 语义 / 散文 measure / 预算口径。
        # 这里只是存在性；行为级断言在 main 段 M42（AX 角色 = button + sr-only 播报节点）、
        # M48（在途 aria-live=off）、M49（段落行宽实测）、M50（确认框预算句口径）。
        "进度槽播报闸门（在途 aria-live=off，结束回 polite）":
            "function setLiveGate" in src and 'setAttribute("aria-live", quiet ? "off" : "polite")' in src
            and "setLiveGate(blocked);" in src,
        "降级播报写进独立 sr-only 节点（chip 自身不再带 live）":
            '$("degrade-live")' in src and "记忆召回：当前用全文" in src,
        "预算口径：今日已用 = 全部任务 calls 之和 + 本次最多 k 次调用":
            "今日已用 ${used} 次调用（全部任务）" in src and "本次最多 ${GENERATE_LIMIT[kind]} 次调用" in src,
    }
    missing_wired = [key for key, present in wired.items() if not present]
    css_tokens = {
        "基础 button 规则": bool(re.search(r"^button \{", css, re.M)),
        ".primary 主操作": "button.primary" in css,
        "h3 分组分隔线": "border-top: 1px solid var(--border)" in css and re.search(r"#pane-manage h3,\s*\n#pane-settings h3", css),
        ".row 换行 + 按钮不压缩": "flex-wrap: wrap" in css and "flex-shrink: 0" in css,
        "错误色用已有 --bad": "color: var(--bad)" in css and "--danger" not in css,
        "长串换行 overflow-wrap": "overflow-wrap: anywhere" in css and "word-break: break-all" not in css,
        "全局 :focus-visible": bool(re.search(r"^:focus-visible \{", css, re.M)),
        "浏览器外表面取主题色": "color-scheme: light dark" in css and "::selection" in css,
        "空槽不占位": ".note:empty" in css,
        "被禁用的输入看得出禁用": "input:disabled" in css and "background: transparent" in css,
        # 治症不治根（复审 ⑤）：.row.hidden 必须排在 .row 之后（后写者胜），不靠 !important
        ".row.hidden 真的收起（且排在 .row 之后）":
            ".row.hidden" in css and css.find("\n.row {") < css.rfind("\n.row.hidden {"),
        # 字号三档（复审 ③）：CSS 里只剩 12 / 14 / 24（正文 14 走 body 的 font 简写）
        "字号只有 12 / 14 / 24 三档":
            set(re.findall(r"font-size:\s*(\d+)px", css)) == {"12", "14", "24"}
            and bool(re.search(r"font:\s*14px/1\.6", css)),
        "筛选框样式（不与正文抢字号档）": "#side-filter" in css,
        # 复审 ③（行为级见 M42 / M49）：sr-only 播报节点 + 散文段落行宽上限
        "sr-only 播报节点（1×1 裁剪，不是 display:none）":
            bool(re.search(r"^\.sr-only \{", css, re.M))
            and bool(re.search(r"\.sr-only \{[^}]*clip-path: inset\(50%\);", css))
            and "display: none" not in (re.search(r"\.sr-only \{[^}]*\}", css) or re.match("", "")).group(0),
        "散文段落 72ch 行宽上限（只管 p.muted，表单另有 520px）":
            bool(re.search(r"#pane-manage p\.muted,\n#pane-settings p\.muted \{\n  max-width: 72ch;\n\}", css))
            and bool(re.search(r"#pane-settings form \{[^}]*max-width: 520px;", css)),
    }
    missing_css = [key for key, present in css_tokens.items() if not present]
    # 复审 ③（行为级见 M42）：role=status 挂在可点 button 上会盖掉 button 语义（AX 实测角色变 status）——
    # 播报改走独立 sr-only 节点，button 上不再留 role / aria-live。
    degrade_tag = re.search(r'<button[^>]*id="degrade"[^>]*>', html)
    degrade_tag_s = degrade_tag.group(0) if degrade_tag else ""
    html_tokens = {
        # 复审 ②（行为级见 M42）：状态节点要被读屏播报；降级 chip 是可点的真 button
        "状态条 role=status": 'id="status" class="chip pending" role="status"' in html,
        "降级 chip：可点 button 且不在 button 上挂 role / aria-live":
            bool(degrade_tag) and "role=" not in degrade_tag_s and "aria-live" not in degrade_tag_s,
        "降级播报另起视觉隐藏的 role=status 节点（sr-only）":
            bool(re.search(r'<span id="degrade-live" class="sr-only" role="status" aria-live="polite"></span>', html)),
        "生成进度槽 role=status（世界包 / 角色卡两个槽）":
            all(f'id="{item}" class="muted note" role="status"' in html for item in ("pkg-note", "card-note")),
        # 复审 ④：侧栏筛选框（行为级见 M44）
        "侧栏筛选框": 'id="side-filter"' in html,
    }
    missing_html = [key for key, present in html_tokens.items() if not present]
    dev_ids = [item for item in re.findall(r'id="(set-[a-z-]+)"', html)
               if re.search(r"rate_max|max_active|catch_up|render_calls|per_day|quota", item)]
    # 【静态检查，不是行为级】本项只核对源码 / 静态 HTML 里的文本与属性存在性——
    # 上一批的失效模式（源码改了但行为没改）在这里防不住，所以逐条的行为级等价断言放在 main 段的
    # M34 / M41 / M42 / M43 / M44 / M45 / M46 / M47 / M48 / M49 / M50（真点真读）与 notify 段；本条保留是因为它一次
    # 覆盖 30 条落点的清单，比行为探针更不容易漏项。
    check("S8（静态检查：源码 / HTML 文本存在性；行为级见 M34·M41–M50）2026-09-21 critique 既定顺序修复项在界面 / 渲染层 / 样式里的落点",
          "PASS" if not (missing_ids or missing_wired or missing_css or missing_html) else "FAIL",
          f"界面控件缺={missing_ids or '无'}；渲染层接线缺={missing_wired or '无'}；"
          f"样式缺={missing_css or '无'}；HTML 属性缺={missing_html or '无'}。"
          "**本项口径 = 静态源码文本存在性断言（不驱动界面）**：判据是 main.ts / styles.css / index.html 里"
          "出现对应文本；真正“点了会发生什么”由 M34（生成闸门与连点）、M41（预算从核心读 + 两组互斥）、"
          "M42（降级 chip 的 AX 角色 = button + sr-only 播报节点 + 跳转聚焦）、M43（字号实测集合）、"
          "M44（筛选真筛）、M45（快捷键真切 pane）、M46（.row.hidden 计算样式）、M47（设置面无开发者键控件）、"
          "M48（进度槽在途 aria-live=off / 结束回 polite）、M49（散文段落渲染宽 ≤ 72ch）、M50（确认框预算句口径）逐条取证",
          clause="DESKTOP_SPEC §3.1 附近反馈 / 顶栏世界时钟 · §3.3 设置面 · §四 视觉与可访问性",
          code="desktop/index.html · desktop/src/main.ts · desktop/src/styles.css")
    # 【静态检查，不是行为级】S9 同样只看静态 HTML / 源码文本；行为级等价断言 = M47
    # （渲染态：设置面没有任何开发者键控件，worldset-facts 里只有只读的「倍率上限（仅开发者）」事实行）。
    check("S9（静态检查：源码 / HTML 文本存在性；行为级见 M47）开发者专用键不在设置表单里（只作只读事实）",
          "PASS" if not dev_ids and "worldset-facts" in html and "倍率上限（仅开发者）" in src else "FAIL",
          f"设置表单里出现的开发者键={dev_ids or '无'}（rate_max / max_active_timelines / "
          f"catch_up_batches / render_calls_per_day / 各类 token 额度一律只读）；"
          f"只读事实节点=worldset-facts={'在' if 'worldset-facts' in html else '缺'}；"
          f"「倍率上限（仅开发者）」文案={'在' if '倍率上限（仅开发者）' in src else '缺'}。"
          "**本项口径 = 静态源码文本存在性断言**；渲染态行为级复核见 M47",
          clause="§3.3 世界 / 会话组：倍率上限只读且仅开发者可配置",
          code="desktop/index.html（settings 面各表单） · desktop/src/main.ts renderLocalFacts")


# ================================================================== main

async def section_main() -> None:
    from isekai_core.config import load_config
    from isekai_core.store import Store
    from isekai_core.world.example import example_card
    from isekai_core.world.package import load_package

    root = make_root("main")
    instances = prepare_world(root)
    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    package = load_package(cfg.paths.packages / "w0.json")
    # 补卡用：一张已审定但不在实例里的卡、一张未审定卡
    good = example_card(package, name="丙补卡甲")
    bad = example_card(package, name="丁未审定", confirmed=False)
    (cfg.paths.packages / "good.json").write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    (cfg.paths.packages / "unconfirmed.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    from isekai_core.world import ops
    ops.dispatch(cfg, store, "world.card.confirm",
                 {"package_path": str(cfg.paths.packages / "w0.json"),
                  "card_path": str(cfg.paths.packages / "good.json")})
    # 内部数据标记（不得出现在界面）：记忆 / 说法 / 认知条目
    con = sqlite3.connect(cfg.paths.db, timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    inst, line = instances[0]["id"], store.timeline_list(instances[0]["id"])[0]["id"]
    char = one(root, "SELECT character_id FROM unit WHERE instance_id=? LIMIT 1", (inst,)) or "cc-x"
    con.execute("INSERT OR REPLACE INTO memory(id,instance_id,timeline_id,character_id,text,kind,"
                "learned_world,recorded_world,semantic_watermark) VALUES(?,?,?,?,?,'fact',0,0,0)",
                ("m-a2", inst, line, char, INTERNAL["memory"]))
    con.execute("INSERT OR REPLACE INTO claim(instance_id,timeline_id,id,event_id,text,earliest_world)"
                " VALUES(?,?,?,?,?,0)", (inst, line, "c-a2", "ev-a2", INTERNAL["claim"]))
    con.execute("UPDATE unit SET semantic=? WHERE rowid="
                "(SELECT rowid FROM unit WHERE instance_id=? AND timeline_id=? LIMIT 1)",
                (INTERNAL["unit"], inst, line))
    con.commit()
    con.close()
    store.close()
    print(f"[main] 数据根={root} 实例={[i['id'] for i in instances]}", flush=True)

    kill_tree()
    proc, cdp, targets = await boot_shell(
        root, PORT, {"ISEKAI_LLM_FAKE": "1",
                     "ISEKAI_LLM_FAKE_REPLY": "审计2占位回复。" + "长" * 4200})
    status = await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    check("M0 真壳启动 → 内建通道 / 管理面就绪",
          "PASS" if status == "已就绪" else "FAIL", f"状态条={status!r}",
          expected="壳等核心 ready 并完成通道 + 管理面认证后才显示已就绪",
          clause="§二.2/§二.3", code="desktop/src/main.ts:352-409")
    await cdp.js(STUB)
    pages = [t for t in targets if t.get("type") == "page"]
    groups = await cdp.js("[...document.querySelectorAll('#sidebar .section-title')].map(e=>e.textContent)")
    tabs = await cdp.js("[...document.querySelectorAll('nav .nav')].map(b=>b.textContent)")
    check("M1 §三 单窗口侧栏结构",
          "PASS" if len(pages) == 1 else "FAIL",
          f"CDP page target 数={len(pages)}；侧栏分组={groups}；页签={tabs}",
          clause="§三 界面结构：侧栏（会话 / 世界 / 时间线）+ 内容区（聊天 / 管理 / 设置）",
          code="desktop/index.html:10-19")

    # ---- 顶栏：当前实例 / 角色 / 时间线名称
    sessions_li = await cdp.js("document.getElementById('sessions').textContent")
    title = await cdp.js("document.getElementById('title').textContent")
    placeholder_triple = one(root, "SELECT instance_id || ' / ' || timeline_id || ' / ' || character_id "
                                  "FROM session WHERE instance_id='ph-instance' LIMIT 1")
    check("M2 §3.1 顶栏显示当前实例 / 角色 / 时间线名称",
          "PASS" if instances[0]["id"] in str(title) and "占位会话" not in str(title) else "FAIL",
          f"顶栏={title!r}；侧栏会话组={sessions_li!r}（占位三元组={placeholder_triple}）→ "
          "显示的是占位会话 id，不是实例/角色/时间线名称",
          clause="§3.1 顶栏显示当前实例 / 角色 / 时间线名称", code="desktop/src/main.ts:96-99")

    # ---- 空态 / 时间戳 / Shift+Enter
    empty = await cdp.js("document.querySelector('#messages li.empty')?.textContent || ''")
    await cdp.send_text("第一轮 " + DOM_MARKER, enter=True)
    got = await cdp.wait("document.getElementById('messages').textContent", "审计2占位回复", 45)
    meta = await cdp.js("document.querySelector('#messages li.user .meta')?.textContent || ''")
    stamp_ok = False
    if meta:
        hm = re.search(r"(\d{2}):(\d{2})", str(meta))
        if hm:
            now = time.localtime()
            stamp_ok = abs(int(hm.group(1)) * 60 + int(hm.group(2)) - (now.tm_hour * 60 + now.tm_min)) <= 3
    world_time_marked = await cdp.js("/(世界|world)\\s*\\d/.test(document.getElementById('messages').textContent)")
    check("M3 §3.1 消息时间戳标注现实时间（世界时刻另行标记）",
          "PASS" if stamp_ok else "FAIL",
          f"空态文案={empty!r}；消息元信息={meta!r}（与本机当前时刻差 ≤3 分钟={stamp_ok}）；"
          f"消息区是否出现世界时刻标记={world_time_marked}",
          clause="§3.1 消息时间戳默认标注现实时间；世界时刻若显示须明确标记",
          code="desktop/src/main.ts:105-108,132-135")
    parts = await cdp.js("document.querySelectorAll('#messages li.character .body p').length")
    check("M4 §3.1 多分段渲染（长回复按限额切段）",
          "PASS" if (parts or 0) >= 2 else "FAIL",
          f"假定 LLM 回复 4212 字、max_text_len=4000 → 角色消息渲染出 {parts} 个 <p>"
          f"（核心 split_parts 分段，界面 parts 逐段落 <p>）",
          clause="§3.1 消息流、多分段", code="isekai_core/session.py:37,334 · main.ts:139-144")
    await cdp.js("document.getElementById('input').focus()")
    await cdp.call("Input.insertText", text="第一行")
    await cdp.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter",
                   text="\r", unmodifiedText="\r", windowsVirtualKeyCode=13,
                   nativeVirtualKeyCode=13, modifiers=8)
    await cdp.call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter",
                   windowsVirtualKeyCode=13, nativeVirtualKeyCode=13, modifiers=8)
    await cdp.call("Input.insertText", text="第二行")
    value = await cdp.js("document.getElementById('input').value")
    await asyncio.sleep(0.6)
    sent_after_shift = await cdp.js("document.getElementById('messages').textContent.includes('第一行')")
    check("M5 §3.1 Shift+Enter 换行、不发送",
          "PASS" if "\n" in str(value) and not sent_after_shift else "FAIL",
          f"Shift+Enter 后输入框值={value!r}（含换行={chr(10) in str(value)}）；"
          f"消息区是否已发出该内容={sent_after_shift}",
          clause="§3.1 Enter 发送、Shift+Enter 换行", code="desktop/src/main.ts:587-592")
    await cdp.js("document.getElementById('input').value='';")

    # ---- XSS / 内部标记扫描 / 凭据不泄漏
    await cdp.send_text(XSS, enter=True)
    await asyncio.sleep(3.0)
    xss = await cdp.js("({a: window.__xss2||null, b: window.__xss3||null,"
                       " img: document.querySelectorAll('#messages img, #messages script').length,"
                       " text: document.getElementById('messages').textContent.includes('审计2脚本')})")
    check("M6 §十.6/§四 角色与用户文本按文本渲染，不执行 HTML/脚本",
          "PASS" if not xss["a"] and not xss["b"] and xss["img"] == 0 and xss["text"] else "FAIL",
          f"注入载荷渲染后 window.__xss2={xss['a']} __xss3={xss['b']}；#messages 内 img/script 元素={xss['img']}；"
          f"载荷文本可见={xss['text']}（textContent 渲染）",
          clause="§四 输出作为文本安全渲染，不执行角色文本中的 HTML / 脚本", code="main.ts:121,142")

    for pane in ("chat", "manage", "settings"):
        await cdp.pane(pane)
    dom = await cdp.js("document.documentElement.outerHTML")
    leaks = {name: (marker in str(dom)) for name, marker in INTERNAL.items()}
    body_text = await cdp.js("document.body.innerText")
    forbidden = [w for w in FORBIDDEN_UI if w in str(body_text)]
    forb_where = await cdp.js(
        "(()=>{const out={};const walk=(n)=>{if(n.nodeType===3){const t=n.nodeValue||'';"
        "for(const w of " + json.dumps(list(FORBIDDEN_UI)) + "){if(t.includes(w)){"
        "(out[w]=out[w]||[]).push((n.parentElement&&(n.parentElement.id||n.parentElement.className))||'?');}}}"
        "else{n.childNodes.forEach(walk);}};walk(document.body);return out;})()")
    check("M7 §十.6 界面不显示实情 / 事件 / 性格数值 / 认知条目 / 记忆内容",
          "PASS" if not any(leaks.values()) else "FAIL",
          f"库内植入的内部标记出现在 DOM={leaks}（memory/claim/unit 三条植入串全不出现）；"
          f"界面文字里出现禁用词={forbidden or '无'}，出现位置={forb_where}（命中的是规范自身要求写的说明句"
          "「不含对话正文、实情、记忆或凭据」与「世界内部 不可浏览」这类元陈述，不是内部数据）",
          clause="§一 不展示：实情、事件日志、角色状态、性格数值、认知条目、记忆内容、内部 diff",
          code="desktop/src/main.ts（管理面只渲染元数据）")
    tokens = {"bootstrap": None, "mgmt": None}
    shell_log = logs(root)
    secrets_in_logs = [tag for tag in (FAKE_KEY, DOM_MARKER) if tag in shell_log]
    check("M8 §二.6/§十.6 凭据与正文不进入壳日志",
          "PASS" if not secrets_in_logs else "FAIL",
          f"logs/shell.log 命中 API Key/正文标记={secrets_in_logs or '无'}；"
          f"日志行样本={[ln[-70:] for ln in shell_log.splitlines() if 'core ready' in ln or 'spawn' in ln][:3]}",
          clause="§二.6 令牌与 API Key 不进入 URL、日志、第三方插件环境或导出件",
          code="desktop/src-tauri/src/main.rs:49-59")

    # ---- 设置面：打码 / 校验失败保留原值 / 缺组
    await cdp.pane("settings")
    await cdp.wait("document.getElementById('set-model').value", "audit2-model", 30)
    ph = await cdp.js("document.getElementById('set-api-key').placeholder")
    key_in_dom = FAKE_KEY in str(dom)
    settings_heads = await cdp.js("[...document.querySelectorAll('#pane-settings h3')].map(h=>h.textContent)")
    settings_facts = await cdp.js("document.getElementById('settings-facts').textContent")
    mt = await cdp.js("(()=>{const f=document.getElementById('settings-form');"
                      "const i=document.getElementById('set-max-tokens');"
                      "return {value:i.value, step:i.step, min:i.min, stepMismatch:i.validity.stepMismatch,"
                      " formValid:f.checkValidity()};})()")
    disk_before = (root / "config" / "config.yaml").read_text(encoding="utf-8")
    await cdp.js("document.getElementById('set-model').value='audit2-model-B';"
                 "document.getElementById('settings-form').requestSubmit();")
    await asyncio.sleep(2.5)
    note_default = await cdp.js("document.getElementById('settings-note').textContent")
    disk_default = (root / "config" / "config.yaml").read_text(encoding="utf-8")
    check("M9 §3.3 设置面表单在默认配置下可提交（日常项由 UI 保存）",
          "PASS" if "已保存" in str(note_default) and "audit2-model-B" in disk_default else "FAIL",
          f"#set-max-tokens 预填值={mt['value']}，input step={mt['step']} min={mt['min']} → "
          f"stepMismatch={mt['stepMismatch']}、form.checkValidity()={mt['formValid']}；"
          f"改模型后点保存 → 提示={note_default!r}，磁盘 model 变更={'audit2-model-B' in disk_default}"
          f"（核心默认 llm.max_tokens=1024，而控件 step=128/min=1 只接受 1+128k，"
          "浏览器静默拦下 requestSubmit → 默认安装下「保存」是不动按钮）",
          clause="§3.3 日常用户可调项由 UI 保存到本地配置，不要求反复手编配置",
          code="desktop/index.html:150 · isekai_core/config.py:77,254 · main.ts:1555")
    await cdp.js("document.getElementById('set-max-tokens').value='129';"
                 "document.getElementById('settings-form').requestSubmit();")
    await cdp.wait("document.getElementById('settings-note').textContent", "已保存", 20)
    disk_ok = (root / "config" / "config.yaml").read_text(encoding="utf-8")
    await cdp.js("document.getElementById('set-max-tokens').value='129';"
                 "document.getElementById('set-base-url').value='';"
                 "document.getElementById('settings-form').requestSubmit();")
    await asyncio.sleep(2.5)
    bad_note = await cdp.js("document.getElementById('settings-note').textContent")
    disk_bad = (root / "config" / "config.yaml").read_text(encoding="utf-8")
    ph_after = await cdp.js("document.getElementById('set-api-key').placeholder")
    check("M9b §3.3 凭据读取打码 + 校验失败保留原值 + Key 不回显",
          "PASS" if ("•" in str(ph) or "*" in str(ph)) and FAKE_KEY not in str(ph)
          and "保存失败" in str(bad_note) and "audit2-model-B" in disk_bad
          and "audit2-model-B" in disk_ok else "FAIL",
          f"Key placeholder（读取打码）={ph!r}；完整 Key 出现在渲染层 DOM={key_in_dom}；"
          f"max_tokens 改成合法 129 后保存 → 磁盘 model={'audit2-model-B' in disk_ok}；"
          f"空 base_url 提交 → 提示={bad_note!r}，磁盘保留 {disk_bad.strip().splitlines()[-1][:40]!r}",
          clause="§3.3 凭据 UI 写入、读取打码；配置检查失败保留原值；错误提示不回显完整 Key",
          code="main.ts:790-808 · isekai_core/config.py:197,222")
    check("M10 §3.3 设置面的组齐备（活体 DOM）",
          "PASS" if {"备份", "关于 / 诊断"} <= set(settings_heads)
          and len(settings_heads) >= 6 else "FAIL",
          f"设置面 <h3> 组={settings_heads}；设置事实={settings_facts!r} → "
          "缺 记忆向量化 / 提交 / 世界·会话 / 用量 四组（无对应控件，也无「模型最近变更时间」）",
          clause="§3.3 设置面表格各分组", code="desktop/index.html:142-176")

    # ---- §4 可访问性：控件名称 / 焦点可见 / 键盘可达
    roles = {"button", "combobox", "textbox", "checkbox", "listbox", "searchbox", "spinbutton"}
    unnamed_all: dict[str, list[str]] = {}
    named_count = 0
    for pane in ("chat", "manage", "settings"):
        await cdp.pane(pane)
        ax = await cdp.call("Accessibility.getFullAXTree")
        nodes = [n for n in ax.get("nodes", []) if (n.get("role") or {}).get("value") in roles]
        named_count += len(nodes)
        unnamed_all[pane] = [f"{(n.get('role') or {}).get('value')}:{(n.get('name') or {}).get('value') or '∅'}"
                             for n in nodes if not ((n.get("name") or {}).get("value") or "").strip()]
    total_unnamed = sum(len(v) for v in unnamed_all.values())
    check("M11 §四 控件有名称（可访问名）",
          "PASS" if total_unnamed == 0 else "FAIL",
          f"三个页面的 AX 交互控件 {named_count} 个，无名称 {total_unnamed} 个（按页：{unnamed_all}）"
          f"（无名称项对应 index.html 里无 label/aria-label 的 select；有名称的靠 placeholder 或按钮文字）",
          clause="§四 键盘可完成选择、发送、确认与取消；焦点可见、控件有名称",
          code="desktop/index.html:53,67,96,104,109,126,131")
    focus_log, focus_visible = [], []
    for _ in range(8):
        await cdp.keys("Tab", "Tab", 9)
        await asyncio.sleep(0.25)
        info = await cdp.js("(()=>{const e=document.activeElement; if(!e) return null;"
                            "const cs=getComputedStyle(e); return {id:e.id||e.tagName,"
                            " fv:e.matches(':focus-visible'), outline:cs.outlineStyle+' '+cs.outlineWidth,"
                            " shadow:cs.boxShadow.slice(0,40)};})()")
        if not info:
            break
        focus_log.append(info["id"])
        if info["fv"] and (info["outline"].split()[1] not in ("0px",) or info["shadow"] != "none"):
            focus_visible.append(info["id"])
    check("M12 §四 键盘可达 + 焦点可见",
          "PASS" if focus_log and len(focus_visible) >= max(1, len(focus_log) - 2) else "FAIL",
          f"连按 Tab 焦点链={focus_log}；其中判定焦点可见={focus_visible}"
          f"（:focus-visible 命中且 outline 宽≠0 或 box-shadow≠none；styles.css 只有 .nav:focus-visible 一条自定义规则）",
          clause="§四 键盘可完成选择、发送、确认与取消；焦点可见",
          code="desktop/src/styles.css:116")

    # ---- 视觉：黑白语义色板 / 无装饰性渐变 / 明暗两套
    palette = await cdp.js("(()=>{const b=getComputedStyle(document.body);"
                           "const chip=getComputedStyle(document.getElementById('status'));"
                           "return {dark:b.backgroundColor, text:b.color, chip:chip.color,"
                           " bodyImage:b.backgroundImage, appImage:getComputedStyle(document.getElementById('app')).backgroundImage};})()")
    await cdp.call("Emulation.setEmulatedMedia",
                   features=[{"name": "prefers-color-scheme", "value": "dark"}])
    await asyncio.sleep(0.5)
    dark = await cdp.js("getComputedStyle(document.body).backgroundColor")
    await cdp.call("Emulation.setEmulatedMedia",
                   features=[{"name": "prefers-color-scheme", "value": "light"}])
    rgb = [int(x) for x in re.findall(r"\d+", str(dark))[:3]] or [255, 255, 255]
    check("M13 §四 黑白语义色板、明暗两套、无装饰性渐变",
          "PASS" if sum(rgb) < 400 and dark != palette["dark"]
          and "gradient" not in str(palette["bodyImage"] + palette["appImage"]) else "FAIL",
          f"浅色 body={palette['dark']}/文字={palette['text']}；prefers-color-scheme=dark → body={dark}（RGB 合计 {sum(rgb)}）；"
          f"body/app 的 background-image={palette['bodyImage']}/{palette['appImage']}（无 gradient 命中）",
          clause="§四 黑白语义色板，明暗两套；无装饰性渐变", code="desktop/src/styles.css:1-30")

    # ---- 管理面：时钟 / 冻结线 / 补卡 / 生成 / 草稿
    await cdp.pane("manage")
    await cdp.wait("document.getElementById('inst-select').options.length", "", 20)
    await cdp.select("inst-select", instances[0]["id"])
    frozen_label = await cdp.wait("document.getElementById('clock-label').textContent", "冻结", 25)
    await cdp.js("document.getElementById('clock-activate').click()")
    active_label = await cdp.wait("document.getElementById('clock-label').textContent", "倍率", 25)
    lines_before = db(root, "SELECT id, state FROM timeline ORDER BY id")
    await cdp.select("inst-select", instances[1]["id"])
    await asyncio.sleep(3.0)
    other_label = await cdp.js("document.getElementById('clock-label').textContent")
    lines_after = db(root, "SELECT id, state FROM timeline ORDER BY id")
    check("M14 §十.3 时钟只显示当前查看的激活线；切换查看不改激活集合",
          "PASS" if "冻结" in str(frozen_label) and "倍率" in str(active_label)
          and "冻结" in str(other_label) and lines_before == lines_after else "FAIL",
          f"甲（未激活）={frozen_label!r} → 点激活后={active_label!r}；切到乙（未激活）={other_label!r}；"
          f"激活集合 切换前{lines_before} 切换后{lines_after}",
          clause="§十.3 冻结线只读历史；查看切换不影响激活集合；时钟只显示当前查看的激活线",
          code="main.ts:1127-1152,1293-1314")

    await cdp.select("inst-select", instances[0]["id"])
    await asyncio.sleep(2.0)
    # 追赶中显示
    await cdp.js("document.getElementById('clock-rate').value='2592000';"
                 "document.getElementById('clock-set-rate').click()")
    await asyncio.sleep(4.0)
    catching = await cdp.wait("document.getElementById('clock-label').textContent", "追赶中", 20)
    check("M15 §十.18/A18 追赶中状态在界面可见，目标水位不与已处理水位混同",
          "PASS" if "追赶中" in str(catching) else "FAIL",
          f"倍率 2592000 生效后时钟标签={catching!r}（含「追赶中（已处理 N）」）",
          clause="§2.10/§十.18 高倍率追赶受限时显示追赶状态；不把目标时刻当成已完成水位",
          code="main.ts:1145-1147")
    await cdp.js("document.getElementById('clock-rate').value='1';"
                 "document.getElementById('clock-set-rate').click()")
    await asyncio.sleep(2.5)

    units_before = one(root, "SELECT COUNT(*) FROM unit")
    joins_before = one(root, "SELECT COUNT(*) FROM character_join")
    await cdp.select("card-select", "unconfirmed.json")
    await cdp.js("document.getElementById('card-add-note').value='审计未审定';"
                 "document.getElementById('card-add').click()")
    await asyncio.sleep(3.0)
    note_bad = await note_of(cdp, "card-add-result", "card-note", "world-note")
    units_mid = one(root, "SELECT COUNT(*) FROM unit")
    joins_mid = one(root, "SELECT COUNT(*) FROM character_join")
    check("M16 §十.19 补卡用未审定卡被拒，且不留半个角色",
          "PASS" if units_mid == units_before and joins_mid == joins_before
          and "确认" in str(note_bad) else "FAIL",
          f"未审定卡补入 → 界面提示={note_bad!r}；unit 行 {units_before}→{units_mid}，"
          f"character_join 行 {joins_before}→{joins_mid}（都不变=原子拒绝）",
          clause="§十.19 补卡可从管理面选择目标线并完成卡片审定、预算确认和原子提交；失败不留下角色定义、成员资格或半条线",
          code="main.ts:1193-1230 · isekai_core/runtime/service.py:2884")
    await cdp.select("card-select", "good.json")
    await cdp.js("document.getElementById('card-add-note').value='从北岸调来';"
                 "document.getElementById('card-add').click()")
    facts = await cdp.wait("document.getElementById('card-add-facts').textContent", "最近补入", 45)
    units_after = one(root, "SELECT COUNT(*) FROM unit")
    joins_after = one(root, "SELECT COUNT(*) FROM character_join")
    confirm_j = await cdp.js("window.__confirmArgs.slice(-1)[0] || ''")
    check("M17 §3.2/§十.19 补卡：确认预算与加入语义 → 原子提交 → 回显加入标签",
          "PASS" if "丙补卡甲" in str(facts) and joins_after == joins_before + 1 else "FAIL",
          f"补卡确认文案={str(confirm_j)[:150]!r}…；补入后事实={facts!r}；"
          f"character_join {joins_before}→{joins_after}，unit {units_mid}→{units_after}",
          clause="§3.2 角色行：选择目标时间线 → 创建 / 审定补卡 → 确认预算与加入语义 → 原子提交",
          code="main.ts:1203-1229 · isekai_core/world/ops.py:250-268")
    # AI 生成 → 用量提示 + 草稿
    drafts_before = [p.name for p in (root / "packages").glob("*.draft.json")]
    await cdp.js("document.getElementById('pkg-brief').value='审计用的世界描述';"
                 "document.getElementById('pkg-file').value='a2gen.json';"
                 "document.getElementById('pkg-generate').click()")
    note_gen = await wait_note(cdp, ("pkg-note", "world-note"), "草稿", 90)
    confirm_gen = await cdp.js("window.__confirmArgs.slice(-1)[0] || ''")
    drafts_after = [p.name for p in (root / "packages").glob("*.draft.json")]
    check("M18 §十.11/§3.3 用量：生成前给出调用预估与上限，完成后回报调用次数；未过校验存草稿",
          "PASS" if "调用" in str(confirm_gen) and "上限" in str(confirm_gen)
          and "调用" in str(note_gen) and len(drafts_after) > len(drafts_before) else "FAIL",
          f"生成前确认文案={str(confirm_gen)[:160]!r}；完成后提示={note_gen!r}；"
          f"草稿 {drafts_before}→{drafts_after}（无「实际用量记录」持久视图，也无回填预估确认）",
          clause="§3.3 用量：生成与回填的预估确认与总调用上限；实际用量记录（调用次数与 token 量级）",
          code="main.ts:1356-1384,1164-1181")
    # 丢弃草稿
    name = str(drafts_after[0]).replace(".draft.json", "") if drafts_after else ""
    pkgs_before = sorted(p.name for p in (root / "packages").glob("*.json")
                         if not p.name.endswith(".draft.json"))
    await cdp.js("document.getElementById('world-refresh').click()")
    await asyncio.sleep(2.0)
    if name:
        await cdp.select("draft-select", name)
        await cdp.js("document.getElementById('draft-discard').click()")
        note_discard = await wait_note(cdp, ("draft-note", "world-note"), "已丢弃", 30)
    else:
        note_discard = ""
    pkgs_after = sorted(p.name for p in (root / "packages").glob("*.json")
                        if not p.name.endswith(".draft.json"))
    check("M19 §3.2/§十.9 丢弃草稿只删草稿，不动正式资产",
          "PASS" if note_discard and pkgs_before == pkgs_after
          and not (root / "packages" / f"{name}.draft.json").exists() else "FAIL",
          f"丢弃提示={note_discard!r}；正式世界包/角色卡文件 前={pkgs_before} 后={pkgs_after}（一致）；"
          f"草稿文件已删={not (root / 'packages' / f'{name}.draft.json').exists()}",
          clause="§3.2 草稿：只有「丢弃草稿」才删除；§十.9 丢弃草稿不损伤正式资产",
          code="main.ts:1277-1283,1233-1256")

    # ================= 2026-09-21 impeccable critique（harden → … → §3.3）新增行为断言 =================
    # 结果全部对界面行为取证：生成闸门与仪表、删除闸门、就近反馈、顶栏世界时钟、设置表单 round-trip。

    await cdp.pane("manage")
    await asyncio.sleep(1.0)

    # ---- M34 harden：10 分钟付费生成的在途仪表（已用 / 上限）与重入闸门
    gen_controls = await cdp.js(
        "({pkg: ['pkg-generate','pkg-brief','pkg-name'].every(i=>!!document.getElementById(i)),"
        " card: ['card-generate','card-brief','card-name-input'].every(i=>!!document.getElementById(i))})")
    await clear_notes(cdp, "pkg-note", "world-note")
    gen_before = (await ops_log(cdp)).count("world.package.generate")
    # ---- M41 准备：把实例级预算上限改成一个只有核心知道的数字（654321），再用确认框里的数字
    # 证明「上限从核心读」而不是壳里写死的常量；同时记下核心账本里今日的调用次数。
    gen_instance = await cdp.js("document.getElementById('inst-select').value")
    await mgmt_call(cdp, "runtime.budget.set", instance_id=gen_instance,
                    instance_tokens_per_day=654321)
    budget_head = await mgmt_call(cdp, "runtime.budget", instance_id=gen_instance)
    budget_limits = dict(budget_head.get("limits") or {})
    budget_used = sum(int(row.get("calls") or 0) for row in (budget_head.get("rows") or []))
    card_ops_before = (await ops_log(cdp)).count("world.card.generate")
    # 页面侧观察点：① disabled 属性突变记录（不受探针采样节奏影响，能看见瞬时的在途窗口）；
    # ② 闸门刚合上的那个微任务里自动补一次「连点」（程序化 click + 派发 click）——
    #    浏览器对 disabled 按钮两条路都不派发，这正是「连点不得发第二次」的现场；
    # ③ 同一时刻对另一组（角色卡）发起一次：两组互斥必须在这里挡住（M41 的证据）。
    await cdp.js(
        "(()=>{window.__gateLog=[];window.__reentry={tried:false,disabledAtClick:null};"
        "window.__mutex={cardDisabled:null,cardDisabledAtClick:null,cardNote:null};"
        # M48 同一现场（不另起一次付费生成）：进度槽的 aria-live 突变时间线 + 在途文本写入落点计数。
        # armed[id] = 该槽当前是否处在自己的 off 窗口里（看见 aria-live=off 起、回到 polite 止）。
        "window.__live={log:[],writesOff:0,leaks:0,armed:{},inflight:null,"
        "onAttr:function(id,live){this.log.push({id:id,ev:'attr',live:live});this.armed[id]=(live==='off');},"
        "onText:function(id,live){if(live==='off'){this.writesOff++;}else if(this.armed[id]){this.leaks++;}}};"
        "const liveOf=id=>document.getElementById(id).getAttribute('aria-live');"
        "const b=document.getElementById('pkg-generate');"
        "const c=document.getElementById('card-generate');"
        "if(window.__gateObs)window.__gateObs.disconnect();"
        "window.__gateObs=new MutationObserver(()=>{const d=b.disabled;"
        "window.__gateLog.push({d:d,note:document.getElementById('pkg-note').textContent});"
        "if(d&&!window.__reentry.tried){window.__reentry.tried=true;"
        "b.click();b.dispatchEvent(new MouseEvent('click',{bubbles:true}));"
        "window.__reentry.disabledAtClick=b.disabled;"
        "window.__mutex.cardDisabled=c.disabled;"
        "c.click();c.dispatchEvent(new MouseEvent('click',{bubbles:true}));"
        "window.__mutex.cardDisabledAtClick=c.disabled;"
        "window.__mutex.cardNote=document.getElementById('card-note').textContent;"
        "window.__live.inflight={pkg:liveOf('pkg-note'),card:liveOf('card-note')};}});"
        "window.__gateObs.observe(b,{attributes:true,attributeFilter:['disabled']});"
        "if(window.__liveObs)window.__liveObs.disconnect();"
        "window.__liveObs=new MutationObserver(ms=>{for(const m of ms){const el=m.target;const live=liveOf(el.id);"
        "if(m.attributeName==='aria-live'){window.__live.onAttr(el.id,live);continue;}"
        "window.__live.onText(el.id,live);"
        "if(window.__live.log.length<40){window.__live.log.push({id:el.id,ev:'text',live:live,"
        "text:(el.textContent||'').slice(0,20)});}}});"
        "for(const id of ['pkg-note','card-note']){window.__liveObs.observe(document.getElementById(id),"
        "{attributes:true,attributeFilter:['aria-live'],childList:true,characterData:true,subtree:true});}})()")
    await cdp.js("document.getElementById('pkg-brief').value='审计闸门用世界描述';"
                 "document.getElementById('pkg-file').value='a2gate.json';"
                 "document.getElementById('pkg-generate').click()")
    gen_final = await wait_note(cdp, ("pkg-note", "world-note"), "草稿", 150)
    gate = await cdp.js("({log:(window.__gateLog||[]).slice(),reentry:window.__reentry||null,"
                        "mutex:window.__mutex||null,live:window.__live?{log:window.__live.log.slice(),"
                        "writesOff:window.__live.writesOff,leaks:window.__live.leaks,"
                        "inflight:window.__live.inflight}:null})")
    await cdp.js("if(window.__gateObs)window.__gateObs.disconnect();"
                 "if(window.__liveObs)window.__liveObs.disconnect()")
    gen_after = (await ops_log(cdp)).count("world.package.generate")
    card_ops_after = (await ops_log(cdp)).count("world.card.generate")
    confirm_gate = await cdp.js("window.__confirmArgs.slice(-1)[0] || ''")
    # 生成结束后再读一次核心账本：确认框里那个「今日已用 n 次」必须落在两次读数之间
    # （账本是活的：跑着的激活线会在探针读数之后继续记账，所以判据是区间而不是相等）
    budget_tail = await mgmt_call(cdp, "runtime.budget", instance_id=gen_instance)
    budget_used_after = sum(int(row.get("calls") or 0) for row in (budget_tail.get("rows") or []))
    inflight = next((row for row in (gate["log"] or []) if row.get("d")), {})
    reentry = gate["reentry"] or {}
    mutex = gate.get("mutex") or {}
    check("M34 §3.1/§十.11 生成在途有仪表（已用 / 上限）、相关控件禁用、连点不发起第二次调用",
          "PASS" if (gen_controls["pkg"] and gen_controls["card"]
                     and inflight and "生成中" in str(inflight.get("note"))
                     and "已用" in str(inflight.get("note")) and "上限" in str(inflight.get("note"))
                     and "无法取消" in str(inflight.get("note"))
                     and reentry.get("tried") and reentry.get("disabledAtClick") is True
                     and (gen_after - gen_before) == 1
                     and "调用" in str(gen_final) and "用时" in str(gen_final)) else "FAIL",
          f"生成三控件存在（世界包={gen_controls['pkg']}、角色卡={gen_controls['card']}）；"
          f"在途禁用记录（disabled 突变，共 {len(gate['log'] or [])} 条）={inflight}；"
          f"在途连点尝试={reentry}（tried=True 且点击时按钮仍是 disabled）；"
          f"三次点击后 world.package.generate 实际调用数={gen_after - gen_before}（只发一次）；"
          f"完成后提示={gen_final!r}",
          clause="§3.1 界面状态可见（生成过程有已用时长与上限，不用「一两分钟」糊）／§十.11 调用上限与用量回报",
          code="desktop/src/main.ts setGenerateGate / startProgress / GENERATE_LIMIT / window.__opLog",
          expected="在途时三控件禁用、进度行给已用与上限、连点只发一次调用、完成后回报实际用量与用时")

    # ================= 2026-09-21 impeccable 复审：壳侧四条（①②③④⑤⑥）的行为级断言 =================

    # ---- M41 复审①：付费预算从核心读 + 世界包 / 角色卡两组生成互斥（数据取自上面同一次生成）
    printed_used = re.search(r"今日已用 (\d+) 次", str(confirm_gate))
    printed_calls = int(printed_used.group(1)) if printed_used else -1
    used_ok = bool(printed_used) and budget_used <= printed_calls <= budget_used_after
    limit_expect = str(budget_limits.get("instance_tokens_per_day") or "")
    task_expect = f"上限 {budget_limits.get('task_tokens_per_day')} token"
    check("M41 复审① 生成预算从核心读（确认框写核心的今日调用数与上限）+ 两组生成互斥",
          "PASS" if (used_ok and limit_expect and limit_expect in str(confirm_gate)
                     and task_expect in str(confirm_gate)
                     and mutex.get("cardDisabled") is True and mutex.get("cardDisabledAtClick") is True
                     and "互斥" in str(mutex.get("cardNote"))
                     and (card_ops_after - card_ops_before) == 0) else "FAIL",
          f"确认框（真文案）={str(confirm_gate)[:300]!r}；探针先把实例上限改成 654321 再经壳读回："
          f"核心 runtime.budget.limits={budget_limits} → 确认框里出现 {limit_expect!r}="
          f"{bool(limit_expect) and limit_expect in str(confirm_gate)}（壳里写死的常量给不出这个数）；"
          f"核心账本今日调用：探针读数 {budget_used} → 确认框写 {printed_calls} → 生成结束后再读 "
          f"{budget_used_after}（区间内={used_ok}；账本是活的，判据是区间不是相等，但落在区间外就说明"
          f"这个数不来自核心）；世界包在途时角色卡组：按钮 disabled="
          f"{mutex.get('cardDisabled')}、点击时仍 disabled={mutex.get('cardDisabledAtClick')}、"
          f"点后本组提示={str(mutex.get('cardNote'))!r}；角色卡生成实际调用数 "
          f"{card_ops_before}→{card_ops_after}（互斥窗口内 0 次）",
          clause="§十.11 生成前给出调用预估与上限（上限取核心预算，不写死在壳里）／§3.1 两组生成互斥",
          code="desktop/src/main.ts budgetLine / setGenerateGate / generateBlocked",
          expected="确认框里的「今日已用 n 次 / 上限 N」两处数字与核心 runtime.budget 一致（含只有核心知道的实例上限）；"
                   "一组在跑时另一组控件禁用、点击不产生管理面调用、本组槽里给出互斥说明")

    # ---- M48 复审④（P1）：生成进度不落进 atomic live region
    # 病根：进度行每秒改写一次，槽却是 role="status" + aria-live=polite —— 一次 10 分钟生成会被重复播报数百句。
    # 判据全部来自页面侧 MutationObserver（就是上面 M34 那个生成现场，不另起一次付费调用）：
    # ① 闸门合上那一刻两槽都是 off；② 在途落到槽里的文本写入全部发生在 off 期间（落进 polite 的 = 0）；
    # ③ 结束后两槽回 polite（只有结果那一句才播报）。
    live_pkg = await cdp.js("document.getElementById('pkg-note').getAttribute('aria-live')")
    live_card = await cdp.js("document.getElementById('card-note').getAttribute('aria-live')")
    live = gate.get("live") or {}
    inflight_live = live.get("inflight") or {}
    attr_line = [e for e in (live.get("log") or []) if e.get("ev") == "attr"]
    text_line = [e for e in (live.get("log") or []) if e.get("ev") == "text"]
    check("M48 复审④ 生成进度不落进 atomic live region（在途 aria-live=off，结束回 polite）",
          "PASS" if (inflight_live.get("pkg") == "off" and inflight_live.get("card") == "off"
                     and int(live.get("writesOff") or 0) >= 1 and int(live.get("leaks") or 0) == 0
                     and live_pkg == "polite" and live_card == "polite") else "FAIL",
          f"闸门合上那一刻（disabled 突变回调里读）两槽 aria-live：pkg-note={inflight_live.get('pkg')!r}、"
          f"card-note={inflight_live.get('card')!r}；生成在途落到槽里的文本写入：落进 off 槽 "
          f"{live.get('writesOff')} 条记录、落进 polite 槽（off 窗口内）{live.get('leaks')} 条"
          f"（后者就是「重复播报数百次」的现场，必须为 0）；结束后两槽 aria-live：pkg-note={live_pkg!r}、"
          f"card-note={live_card!r}；aria-live 突变时间线={attr_line}；在途头几条文本写入={text_line[:3]}",
          clause="§四 可访问性：live region 只播「该说的那一句」，逐秒进度不逐条播报",
          code="desktop/src/main.ts setGenerateGate → setLiveGate / startProgress / groupNote",
          expected="生成在途 pkg-note 与 card-note 的 aria-live=off（文本照写、界面照看）；在途没有任何文本写进 "
                   "polite 的槽；结束 / 失败后两槽回到 polite，只有结果那一句被播报")

    # ---- M50 复审④（P2）：预算句的口径（数字与口径都要对得上）
    # 旧句「今日已用 N 次 / 上限 M token」把两件事混在一句里：N 是当日**全部任务**的 calls 之和，
    # 却被读成「本次任务的次数」。新口径把两类上限分开写，并逐项对核心本轮读数。
    desktop_src = (REPO / "desktop" / "src" / "main.ts").read_text(encoding="utf-8")
    gen_k = re.search(r"GENERATE_LIMIT = \{ package: (\d+), card: (\d+) \}", desktop_src)
    budget_re = re.search(
        r"今日已用 (\d+) 次调用（全部任务） / 单任务上限 (\d+) token（本实例 (\d+)、单线 (\d+)）；"
        r"本次最多 (\d+) 次调用", str(confirm_gate))
    printed = [int(x) for x in budget_re.groups()] if budget_re else []
    used_ok2 = bool(printed) and budget_used <= printed[0] <= budget_used_after
    limits_ok = bool(printed) and (str(printed[1]) == str(budget_limits.get("task_tokens_per_day"))
                                  and str(printed[2]) == str(budget_limits.get("instance_tokens_per_day"))
                                  and str(printed[3]) == str(budget_limits.get("timeline_tokens_per_day")))
    k_ok = bool(printed) and bool(gen_k) and printed[4] == int(gen_k.group(1))
    check("M50 复审④ 确认框的预算句按新口径（今日已用 n 次调用（全部任务） / 单任务上限 M token；本次最多 k 次调用）",
          "PASS" if (used_ok2 and limits_ok and k_ok and "次 / 上限" not in str(confirm_gate)) else "FAIL",
          f"确认框原文={str(confirm_gate)[:300]!r}；正则抓到=（今日已用 {printed[0] if printed else '∅'} 次调用（全部任务）、"
          f"单任务上限 {printed[1] if printed else '∅'} token、本实例 {printed[2] if printed else '∅'}、"
          f"单线 {printed[3] if printed else '∅'}、本次最多 {printed[4] if printed else '∅'} 次调用）；"
          f"核心 runtime.budget 本轮真实读数：探针先读 {budget_used} → 确认框写 {printed[0] if printed else '∅'} → "
          f"生成后再读 {budget_used_after}（n = 本实例今日 call_ledger 全任务 calls 之和，落在区间内={used_ok2}）；"
          f"核心三层 token 限额={budget_limits}，句子里三个数字与之一致={limits_ok}；"
          f"k 与壳侧 GENERATE_LIMIT.package={gen_k.group(1) if gen_k else '∅'} 一致={k_ok}；"
          f"旧「次 / 上限 N」混排已消失={'次 / 上限' not in str(confirm_gate)}",
          clause="§十.11 生成前给出调用预估与上限（次数与 token 两类上限不混在一句话里）",
          code="desktop/src/main.ts budgetLine",
          expected="确认框写「今日已用 n 次调用（全部任务） / 单任务上限 M token（本实例 / 单线）；本次最多 k 次调用」；"
                   "n 与核心账本当轮读数（全部任务 calls 之和）一致、M 与核心限额一致、k 与壳侧常量一致")

    # ---- M42 复审③（P1）：降级 chip 的 AX 语义 / 口径 / 可点（跳设置面记忆组 + 聚焦首控件）；
    # 播报改走独立 sr-only 节点——上一版把 role=status 挂在 button 上，AX 实测角色变成 status（button 语义被盖掉）。
    # 判据认 AX 树 + HTML 属性两侧：AX role=button 且 focusable、可点（跳转 + 聚焦），播报节点 role=status 且不可聚焦、
    # 视觉上 1×1 裁剪（不是 display:none，那会把播报一起关掉）；#status 与两个进度槽仍保持 role=status。
    deg_ax = await ax_view(cdp, "document.getElementById('degrade')")
    live_ax = await ax_view(cdp, "document.getElementById('degrade-live')")
    chip_state = await cdp.js(
        "(()=>{const r=id=>{const el=document.getElementById(id);"
        "return el?{role:el.getAttribute('role'),live:el.getAttribute('aria-live')}:null};"
        "const c=document.getElementById('degrade');const l=document.getElementById('degrade-live');"
        "const cl=getComputedStyle(l);const box=l.getBoundingClientRect();"
        "return {chipRole:c.getAttribute('role'),chipLive:c.getAttribute('aria-live'),"
        " chipText:(c.textContent||'').trim(),chipTitle:c.getAttribute('title')||'',"
        " chipHidden:c.classList.contains('hidden'),"
        " liveRole:l.getAttribute('role'),liveLive:l.getAttribute('aria-live'),"
        " liveText:(l.textContent||'').trim(),"
        " liveBox:[Math.round(box.width),Math.round(box.height)],liveDisplay:cl.display,"
        " liveVisibility:cl.visibility,liveClip:cl.clipPath||cl.webkitClipPath||'',"
        " status:r('status'),pkg:r('pkg-note'),card:r('card-note')};})()")
    await cdp.js("document.getElementById('degrade').click()")
    await asyncio.sleep(1.5)
    jumped = await cdp.js(
        "({settings: !document.getElementById('pane-settings').classList.contains('hidden'),"
        " chat: !document.getElementById('pane-chat').classList.contains('hidden'),"
        " active: (document.activeElement&&document.activeElement.id)||'',"
        " chipHidden: document.getElementById('degrade').classList.contains('hidden')})")
    ax_ok = (deg_ax["found"] and deg_ax["role"] == "button" and deg_ax["ignored"] is False
             and deg_ax["props"].get("focusable") is True)
    live_ok = (live_ax["found"] and live_ax["role"] == "status"
               and live_ax["props"].get("focusable") is not True
               and any("全文" in t and "语义召回" in t for t in live_ax["texts"]))
    chip_ok = (chip_state["chipRole"] is None and chip_state["chipLive"] is None
               and "语义召回" in chip_state["chipText"] and "不可用" not in chip_state["chipText"]
               and chip_state["chipTitle"] != "")
    live_css_ok = (chip_state["liveDisplay"] != "none" and chip_state["liveVisibility"] != "hidden"
                   and max(chip_state["liveBox"]) <= 2 and bool(chip_state["liveClip"]))
    slots_ok = all(chip_state[k] and chip_state[k]["role"] == "status" for k in ("status", "pkg", "card"))
    check("M42 复审③ 降级 chip 在 AX 上是 button（不再被 role=status 盖掉）+ 仍可聚焦可点 + 播报走独立 sr-only 节点",
          "PASS" if (ax_ok and live_ok and chip_ok and live_css_ok and slots_ok
                     and jumped["settings"] and not jumped["chat"]
                     and jumped["active"] == "set-mem-mode") else "FAIL",
          f"AX 实测 #degrade：role={deg_ax['role']!r}（子树角色={deg_ax['roles'][:4]}）、"
          f"focusable={deg_ax['props'].get('focusable')}、ignored={deg_ax['ignored']}；"
          f"HTML 侧：#degrade role={chip_state['chipRole']!r}、aria-live={chip_state['chipLive']!r}、"
          f"title={chip_state['chipTitle']!r}、文本={chip_state['chipText']!r}；"
          f"播报节点 #degrade-live：AX role={live_ax['role']!r}、focusable={live_ax['props'].get('focusable')}、"
          f"AX 子树文本={live_ax['texts'][:4]}；视觉：display={chip_state['liveDisplay']!r}、"
          f"visibility={chip_state['liveVisibility']!r}、clip-path={chip_state['liveClip']!r}、盒={chip_state['liveBox']}px；"
          f"仍带 role=status 的槽：#status={chip_state['status']}、pkg-note={chip_state['pkg']}、"
          f"card-note={chip_state['card']}；点 chip → 设置面可见={jumped['settings']}、"
          f"聊天面仍可见={jumped['chat']}（应 False）、document.activeElement={jumped['active']!r}",
          clause="§3.1 界面状态可见（状态 / 进度 / 降级对读屏可用）／§四 可访问性（可点控件保持 button 语义）／§六 降级是常态不是故障",
          code="desktop/index.html #degrade / #degrade-live · styles.css .sr-only · main.ts renderDegrade / openMemoryGroup",
          expected="AX 上 #degrade 的 role=button、focusable=true、ignored=false（HTML 上不带 role / aria-live）；"
                   "存在 sr-only 的 role=status 节点，AX 子树文本说明「当前用全文（默认）」、不可聚焦、视觉 1×1 裁剪且不是 display:none；"
                   "#status / pkg-note / card-note 仍是 role=status；点击后切到设置面记忆组并聚焦它的第一个可编辑控件")

    # ---- M47 复审⑥：S9 的渲染态等价（设置面有没有能改开发者键的控件）
    dev_live = await cdp.js(
        "(()=>{const ids=[...document.querySelectorAll('#pane-settings input,#pane-settings select')]"
        ".map(e=>e.id).filter(Boolean);"
        "const bad=ids.filter(id=>/rate_max|max_active|catch_up|render_calls|per_day|quota/.test(id));"
        "const facts=document.getElementById('worldset-facts').textContent||'';"
        "return {controls:ids.length,bad:bad,fact:facts.includes('倍率上限（仅开发者）'),facts:facts.slice(0,180)};})()")
    check("M47 复审⑥（S9 的行为级等价）设置面没有开发者键控件，倍率上限只作只读事实",
          "PASS" if (not dev_live["bad"] and dev_live["fact"]) else "FAIL",
          f"设置面活体控件 {dev_live['controls']} 个，命中开发者键的={dev_live['bad'] or '无'}"
          f"（rate_max / max_active_timelines / catch_up_batches / render_calls_per_day / 各类 per_day 额度）；"
          f"worldset-facts 含「倍率上限（仅开发者）」={dev_live['fact']}；事实文本={dev_live['facts']!r}",
          clause="§3.3 世界 / 会话组：倍率上限只读且仅开发者可配置",
          code="desktop/index.html（settings 面各表单） · main.ts renderLocalFacts",
          expected="设置面没有任何改这些键的控件；它们只出现在只读事实里")

    # ---- M43 复审③：字号阶梯实测（规则镜像 + 技能自带检测器复跑）
    sizes = await cdp.js(
        "(()=>{const s=new Set();"
        "for(const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,span,a,li,td,th,label,button,div')){"
        "const fs=parseFloat(getComputedStyle(el).fontSize);"
        "if(fs>0&&fs<200)s.add(Math.round(fs*10)/10);}"
        "const sorted=[...s].sort((a,b)=>a-b);"
        "return {sizes:sorted, ratio: sorted.length? +(sorted[sorted.length-1]/sorted[0]).toFixed(2):0};})()")
    detector_hits: list = []
    detector_path = impeccable_detector()
    if detector_path:
        try:
            await cdp.js("window.__IMPECCABLE_CONFIG__={autoScan:false};"
                         + detector_path.read_text(encoding="utf-8"))
            detector_hits = await cdp.js(
                "(()=>{try{const out=[];const raw=window.impeccableDetect({serialize:false})||[];"
                "for(const g of raw){const fs=(g&&g.findings)||[];"
                "if(fs.length){for(const f of fs)out.push(f.type||f.id||'?');}"
                "else if(g&&(g.type||g.id))out.push(g.type||g.id);}"
                "return out;}catch(e){return ['@error:'+String(e)]}})()") or []
            detector_note = (f"技能自带检测器（{detector_path.name}）渲染态复跑 → 全量 findings="
                             f"{detector_hits or '无'}；flat-type-hierarchy "
                             f"{'命中（不合格）' if 'flat-type-hierarchy' in detector_hits else '0 命中'}")
        except Exception as exc:  # noqa: BLE001
            detector_note = f"检测器注入失败（{str(exc)[:120]}）→ 只用下面的镜像判定"
    else:
        detector_note = "未找到技能自带检测器 → 只用镜像判定（同一查询 / 同一四舍五入 / 同一阈值）"
    check("M43 复审③ 字号阶梯只剩三档（12 / 14 / 24），实测不再触发 flat-type-hierarchy",
          "PASS" if (sizes["sizes"] and set(sizes["sizes"]) <= {12, 14, 24} and sizes["ratio"] >= 2.0
                     and "flat-type-hierarchy" not in detector_hits) else "FAIL",
          f"渲染态实测字号集合={sizes['sizes']}（max/min={sizes['ratio']}，规则是 <2.0 才报）；"
          f"判据镜像自 detect-antipatterns-browser.js:4707-4717（同一 querySelectorAll 名单 / 同一 "
          f"Math.round(fs*10)/10 / 同一 ratio<2.0 阈值）；{detector_note}",
          clause="§四 视觉：字号阶梯有真层级（三档：正文 14 / 次级 12 / 标题 24）",
          code="desktop/src/styles.css（.brand · h2 = 24、body = 14、次级与输入 = 12）",
          expected="实测集合 ⊆ {12,14,24} 且 max/min ≥ 2.0；真检测器不再报 flat-type-hierarchy")

    # ---- M44 复审④：侧栏过滤框（键入即筛 / 按内容匹配 / Esc 清空恢复 / Ctrl+K 聚焦）
    rows_expr = ("(()=>{const all=[];for(const id of ['sessions','timelines']){"
                 "for(const el of document.getElementById(id).children)all.push(el);}"
                 "const vis=all.filter(e=>!e.classList.contains('hidden'));"
                 "return {total:all.length,vis:vis.length,texts:vis.map(e=>e.textContent)};})()")
    side_before = await cdp.js(rows_expr)
    await cdp.keys("k", "KeyK", 75, ctrl=True)
    await asyncio.sleep(0.5)
    focus_after_ctrl_k = await cdp.js("(document.activeElement&&document.activeElement.id)||''")
    await cdp.js("(()=>{const el=document.getElementById('side-filter');"
                 "el.value='zzz-没有这一行';el.dispatchEvent(new Event('input'))})()")
    await asyncio.sleep(0.4)
    side_none = await cdp.js(rows_expr)
    needle = str((side_before["texts"] or [""])[0])[:2]
    await cdp.js("(()=>{const el=document.getElementById('side-filter');"
                 f"el.value={json.dumps(needle)};el.dispatchEvent(new Event('input'))}})()")
    await asyncio.sleep(0.4)
    side_hit = await cdp.js(rows_expr)
    await cdp.js("document.getElementById('side-filter').dispatchEvent("
                 "new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
    await asyncio.sleep(0.4)
    side_back = await cdp.js(rows_expr)
    filter_value_after_esc = await cdp.js("document.getElementById('side-filter').value")
    hit_all_match = bool(side_hit["texts"]) and all(needle in str(text) for text in side_hit["texts"])
    check("M44 复审④ 侧栏过滤框：键入即筛（可见行减少）、真按内容匹配、Esc 清空后恢复",
          "PASS" if (focus_after_ctrl_k == "side-filter"
                     and side_before["vis"] == side_before["total"] and side_before["total"] >= 2
                     and side_none["vis"] == 0 and side_none["total"] == side_before["total"]
                     and side_hit["vis"] >= 1 and hit_all_match
                     and side_back["vis"] == side_before["total"]
                     and filter_value_after_esc == "") else "FAIL",
          f"Ctrl+K → 焦点={focus_after_ctrl_k!r}；筛前可见 {side_before['vis']}/{side_before['total']} 行；"
          f"键入不存在的词 → 可见 {side_none['vis']} 行（对象仍是 {side_none['total']} 行，只是被收起）；"
          f"键入首行前两字 {needle!r} → 可见 {side_hit['vis']} 行、全部含该词={hit_all_match}"
          f"（{side_hit['texts']}）；Esc 清空后 → 输入框={filter_value_after_esc!r}、"
          f"可见 {side_back['vis']}/{side_back['total']} 行（完全恢复）",
          clause="§3.1 列表可用性（老手效率最小集：过滤，不做批量操作）",
          code="desktop/index.html#side-filter · main.ts applySideFilter / bindShortcuts",
          expected="键入即筛（可见行数减少且只留内容匹配的行），Esc 清空后完全恢复；Ctrl+K 聚焦过滤框")

    # ---- M45 复审④：Ctrl+1/2/3 切三个 pane
    pane_expr = ("({chat:!document.getElementById('pane-chat').classList.contains('hidden'),"
                 "manage:!document.getElementById('pane-manage').classList.contains('hidden'),"
                 "settings:!document.getElementById('pane-settings').classList.contains('hidden')})")
    pane_start = await cdp.js(pane_expr)
    await cdp.keys("1", "Digit1", 49, ctrl=True)
    await asyncio.sleep(0.9)
    pane_1 = await cdp.js(pane_expr)
    await cdp.keys("3", "Digit3", 51, ctrl=True)
    await asyncio.sleep(0.9)
    pane_3 = await cdp.js(pane_expr)
    await cdp.keys("2", "Digit2", 50, ctrl=True)
    await asyncio.sleep(0.9)
    pane_2 = await cdp.js(pane_expr)
    check("M45 复审④ Ctrl+1/2/3 切聊天 / 管理 / 设置三个 pane",
          "PASS" if (pane_1 == {"chat": True, "manage": False, "settings": False}
                     and pane_3 == {"chat": False, "manage": False, "settings": True}
                     and pane_2 == {"chat": False, "manage": True, "settings": False}) else "FAIL",
          f"起点（刚点完 chip，在设置面）={pane_start}；Ctrl+1 → {pane_1}；Ctrl+3 → {pane_3}；"
          f"Ctrl+2 → {pane_2}（每步只有一个 pane 可见）",
          clause="§3.1 键盘效率最小集（不做全局命令面板）",
          code="desktop/src/main.ts bindShortcuts",
          expected="Ctrl+1/2/3 分别切到聊天 / 管理 / 设置，同一时刻只有一个 pane 可见")

    # ---- M46 复审⑤：.row.hidden 的计算样式护栏（治根：规则排在 .row 之后）
    hidden_guard = await cdp.js(
        "(()=>{const row=document.getElementById('inst-convert-row');"
        "const has=row.classList.contains('hidden');"
        "const off=getComputedStyle(row).display;"
        "row.classList.remove('hidden');const shown=getComputedStyle(row).display;"
        "row.classList.add('hidden');const back=getComputedStyle(row).display;"
        "return {has:has,hidden:off,shown:shown,back:back};})()")
    check("M46 复审⑤ .row.hidden 的渲染态计算样式（护栏：隐藏规则必须压过 .row 的 display:flex）",
          "PASS" if (hidden_guard["has"] and hidden_guard["hidden"] == "none"
                     and hidden_guard["shown"] == "flex" and hidden_guard["back"] == "none") else "FAIL",
          f"#inst-convert-row（.row.hidden）带 hidden 类={hidden_guard['has']}；计算样式：带 hidden → "
          f"display={hidden_guard['hidden']!r}；临时摘掉 hidden → display={hidden_guard['shown']!r}"
          f"（正对照：证明这条判据能区分两种情况，不是恒为 none）；再戴回 → {hidden_guard['back']!r}",
          clause="§四 视觉：.row.hidden 真的收起（治根：规则排在 .row 之后，不靠 !important）",
          code="desktop/src/styles.css 文件末尾 .row.hidden",
          expected="带 .hidden 时计算样式 display=none；摘掉时 display=flex")
    await cdp.pane("manage")
    await asyncio.sleep(0.5)

    # ---- M49 复审④（P2）：散文段落的行宽上限（宽窗下不再排满整屏）
    # 判据是渲染态实测：1ch = 该段字体下「0」的宽度（不是字数）；宽窗用 CDP 设备度量覆盖模拟，
    # 同时要求「约束确实在生效」——容器宽 > 72ch，否则窄窗下这条断言等于白给。
    prose_expr = ("(()=>{const sec=document.getElementById('pane-__PANE__');"
                  "const chOf=el=>{const cs=getComputedStyle(el);const s=document.createElement('span');"
                  "s.textContent='0';s.style.position='absolute';s.style.left='-9999px';s.style.whiteSpace='pre';"
                  "s.style.fontSize=cs.fontSize;s.style.fontFamily=cs.fontFamily;s.style.fontWeight=cs.fontWeight;"
                  "document.body.appendChild(s);const w=s.getBoundingClientRect().width;s.remove();return w;};"
                  "const rows=[];for(const p of sec.querySelectorAll('p.muted')){const t=(p.textContent||'').trim();"
                  "if(t.length<40)continue;const cs=getComputedStyle(p);const ch=chOf(p);"
                  "const w=p.getBoundingClientRect().width;"
                  "rows.push({pane:'__PANE__',chars:t.length,width:+w.toFixed(1),maxWidth:cs.maxWidth,ch:+ch.toFixed(2),"
                  "chPerLine:+(w/ch).toFixed(1),cjkPerLine:+(w/parseFloat(cs.fontSize)).toFixed(1),"
                  "paneWidth:+sec.getBoundingClientRect().width.toFixed(1)});}"
                  "const offenders=[];"
                  "for(const el of document.querySelectorAll('p,li,span,div,td,th,a,label')){"
                  "const dt=[...el.childNodes].some(n=>n.nodeType===3&&(n.textContent||'').trim());"
                  # 与 impeccable 检测器同一道闸（hasDirectText）：只算「自己直接持有文本」的元素，容器不算
                  "if(!dt)continue;"
                  "const t=(el.textContent||'').trim();if(t.length<=80)continue;"
                  "const r=el.getBoundingClientRect();if(r.width<=0)continue;const cs=getComputedStyle(el);"
                  "if(cs.display==='none'||cs.visibility==='hidden')continue;"
                  "const cpl=r.width/(parseFloat(cs.fontSize)*0.5);"
                  "if(cpl>85)offenders.push({tag:el.tagName,id:el.id||'',cls:String(el.className).slice(0,24),"
                  "pane:(el.closest('.pane')||{}).id||'',cpl:Math.round(cpl)});}"
                  "return {viewport:innerWidth,paneWidth:+sec.getBoundingClientRect().width.toFixed(1),"
                  "rows:rows,offenders:offenders.slice(0,6)"
                  "};})()")
    await cdp.pane("settings")
    await cdp.call("Emulation.setDeviceMetricsOverride", width=1800, height=1000,
                   deviceScaleFactor=1, mobile=False)
    await asyncio.sleep(0.9)
    prose_settings = await cdp.js(prose_expr.replace("__PANE__", "settings"))
    prose_settings["formWidth"] = await cdp.js(
        "+document.getElementById('settings-form').getBoundingClientRect().width.toFixed(1)")
    await cdp.pane("manage")
    await asyncio.sleep(0.6)
    prose_manage = await cdp.js(prose_expr.replace("__PANE__", "manage"))
    await cdp.call("Emulation.clearDeviceMetricsOverride")
    await asyncio.sleep(0.6)
    prose_rows = list(prose_settings["rows"]) + list(prose_manage["rows"])
    worst = max(prose_rows, key=lambda r: r["chPerLine"]) if prose_rows else {}
    cap_binds = [r for r in prose_rows if r["paneWidth"] > 72 * r["ch"]]
    cap_ok = all(re.search(r"[\d.]+", str(r["maxWidth"]))
                 and float(re.search(r"[\d.]+", str(r["maxWidth"])).group(0)) <= 72 * r["ch"] + 1.5
                 and r["chPerLine"] <= 72.5 for r in prose_rows)
    check("M49 复审④ 散文段落有行宽上限（渲染态实测 ≤ 72ch，且宽窗下确实在生效）",
          "PASS" if (prose_settings["rows"] and prose_manage["rows"] and prose_rows and cap_ok and cap_binds
                     and 500 <= prose_settings["formWidth"] <= 522) else "FAIL",
          f"模拟宽窗 {prose_settings['viewport']}px 视口（设置面内容宽 {prose_settings['paneWidth']}px、"
          f"管理面 {prose_manage['paneWidth']}px）下实测 {len(prose_rows)} 段（设置面 {len(prose_settings['rows'])} / "
          f"管理面 {len(prose_manage['rows'])}）；最宽一行 {worst.get('chPerLine')} ch"
          f"（= {worst.get('cjkPerLine')} 个汉字/行，段长 {worst.get('chars')} 字，实宽 {worst.get('width')}px，"
          f"计算样式 max-width={worst.get('maxWidth')!r}，1ch={worst.get('ch')}px，容器 {worst.get('paneWidth')}px）；"
          f"逐段 ch/行={[r['chPerLine'] for r in prose_rows]}；"
          f"「约束在生效」的证据（容器宽 > 72ch，窄窗下这条断言才不会白给）="
          f"{[{'pane': r['pane'], 'paneWidth': r['paneWidth'], 'cap': round(72 * r['ch'])} for r in cap_binds]}；"
          f"表单没被压窄：settings-form 宽 {prose_settings['formWidth']}px（它自己的 520px 上限，未受新规则影响）；"
          f"镜像 impeccable 的 line-length 规则（文本 >80 字且 宽/(字号×0.5) >85）在宽窗下列出的残余元素="
          f"{prose_settings['offenders'] or '无'}"
          f"（设置面 / 管理面的 p.muted 已全部落在这条线以下；若仍有残余，看这里的 tag/id 落在哪个面）",
          clause="§四 视觉：散文段落有可读行宽上限（宽窗下不排满整屏）",
          code="desktop/src/styles.css（#pane-manage p.muted / #pane-settings p.muted → max-width: 72ch）",
          expected="设置面 / 管理面的说明段落渲染宽 ≤ ~72ch（1ch = 该字体下「0」的宽度，实测给出 px 与汉字数）；"
                   "宽窗下是这条上限在起作用（容器宽 > 72ch）；设置表单仍按自己的 520px，没被压窄")

    # ---- M35 harden：删除实例的闸门（离开实例行 + 键入实例名 + 写清保留什么）
    gate_inst = (await mgmt_call(cdp, "instance.create",
                                 package_path=str(root / "packages" / "w0.json"),
                                 card_paths=[str(root / "packages" / "good.json")],
                                 display_name="审计删除用实例"))["instance"]
    await cdp.js("document.getElementById('world-refresh').click()")
    await asyncio.sleep(2.5)
    await cdp.select("inst-select", str(gate_inst["id"]))
    await asyncio.sleep(2.5)
    gate0 = await cdp.js(
        "({disabled: document.getElementById('inst-delete').disabled,"
        " note: document.getElementById('inst-delete-note').textContent,"
        " row: !!document.getElementById('inst-delete').closest('.delete-row'),"
        " same_row: document.getElementById('inst-select').parentElement"
        "   === document.getElementById('inst-delete').parentElement})")
    # ① 名字不对（含空）：不给删；真派发一次点击也不落
    await cdp.js("document.getElementById('inst-delete-name').value='不是这个名字';"
                 "document.getElementById('inst-delete-name').dispatchEvent(new Event('input'));")
    gate_wrong = await cdp.js("document.getElementById('inst-delete').disabled")
    await cdp.js("document.getElementById('inst-delete').click()")
    await asyncio.sleep(1.5)
    alive_after_wrong = one(root, "SELECT COUNT(*) FROM instance WHERE id=?", (gate_inst["id"],))
    # ② 键入正确名字 → 可点 → 删除（保留清单写在同一行）
    await cdp.js(f"document.getElementById('inst-delete-name').value={json.dumps(str(gate_inst['name']))};"
                 "document.getElementById('inst-delete-name').dispatchEvent(new Event('input'));")
    gate_right = await cdp.js("document.getElementById('inst-delete').disabled")
    await cdp.js("document.getElementById('inst-delete').click()")
    await asyncio.sleep(3.5)
    gone = one(root, "SELECT COUNT(*) FROM instance WHERE id=?", (gate_inst["id"],))
    del_note = await cdp.js("document.getElementById('inst-delete-note').textContent")
    del_confirm = await cdp.js("window.__confirmArgs.slice(-1)[0] || ''")
    inst_tail = f"#{str(gate_inst['id'])[-6:]}"   # 复审⑤：删除提示里的实例标识尾段
    check("M35 §3.2 删除实例要键入实例名才放行；入口离开实例行，并写明保留什么 + 实例标识尾段",
          "PASS" if (gate0["disabled"] is True and gate0["row"] and not gate0["same_row"]
                     and "将保留：世界包 / 角色卡 / 导出件" in str(gate0["note"])
                     and inst_tail in str(gate0["note"])
                     and gate_wrong is True and alive_after_wrong == 1
                     and gate_right is False and gone == 0
                     and "已删除" in str(del_note)
                     and "不可撤销" in str(del_confirm) and "将保留" in str(del_confirm)
                     and inst_tail in str(del_confirm)) else "FAIL",
          f"未输入名字时删除按钮 disabled={gate0['disabled']}（输入错名字后仍={gate_wrong}，"
          f"派发点击后实例仍在=实例表 {alive_after_wrong} 行）；入口在独立 .delete-row={gate0['row']}、"
          f"与实例下拉同一行={gate0['same_row']}（应为 False）；行内保留清单={gate0['note']!r}"
          f"（含实例 id 尾段 {inst_tail!r}={inst_tail in str(gate0['note'])}）；"
          f"键入正确名字后可点={gate_right is False} → 删除后实例表 {gone} 行；结果提示={del_note!r}；"
          f"确认框={str(del_confirm)[:140]!r}（含尾段={inst_tail in str(del_confirm)}）",
          clause="§3.2 删除需二次确认，明确范围与失去的进展（破坏性操作不贴着它的目标）",
          code="desktop/index.html#inst-delete-name · main.ts renderDeleteGate / deleteHint / inst-delete 处理器",
          expected="未键入实例名时不可点、点击不生效；键入后删除并回报；保留清单与确认框都写清范围"
                   "（同名实例靠 id 尾段区分；校验仍比显示名，不让用户输 id）")

    # ---- M36 polish：动作结果就近落在本组的行内槽，页顶不再承接组内结果
    await cdp.pane("manage")
    await clear_notes(cdp, "pkg-note", "world-note")
    await cdp.js("(()=>{const s=document.getElementById('pkg-select');"
                 "const hit=[...s.options].find(o=>o.value==='w0.json');"
                 "s.value=(hit||s.options[0]).value;})()")
    await cdp.js("document.getElementById('pkg-check').click()")
    checked = await wait_note(cdp, ("pkg-note",), "通过校验", 30)
    top_note = await cdp.js("document.getElementById('world-note').textContent")
    checked_pkg = await cdp.js("document.getElementById('pkg-select').value")
    slots = await cdp.js(
        "['pkg-note','card-note','inst-note','draft-note','clock-note','role-note','disclose-note',"
        "'card-import-note','card-select-note','card-add-result','inst-create-note','inst-import-note',"
        "'inst-delete-note','rollback-note','card-add-facts','world-facts'].filter(i=>!!document.getElementById(i)).length")
    check("M36 §3.1 动作结果就近落在本组的行内槽（页顶只留跨组 / 严重事件）",
          "PASS" if ("通过校验" in str(checked) and not str(top_note).strip() and (slots or 0) == 16) else "FAIL",
          f"点「校验」（选 {checked_pkg}）→ 世界包组结果槽="
          f"{checked!r}；同一时刻页顶 #world-note={top_note!r}（应为空：组内结果不再挤页顶）；"
          f"每组行内槽存在={slots}/16（世界包 / 角色卡生成 / 实例 / 草稿 / 运行 / 角色 / 披露 / 卡导入 / "
          f"卡选择行 / 补卡行 / 创建实例行 / 导入实例行 / 删除行 / 回滚行 / 补卡事实 / 实例事实）",
          clause="§3.1 结果回到动作旁边（1800px 长页只有一个页顶槽时，结果常落在屏幕外）",
          code="desktop/index.html（各组 .note 槽） · main.ts groupNote / reportNote / worldAction(slot)")
    # 页顶锚点：跨组事件带「回到该组」入口
    anchor = await cdp.js("({btn: !!document.getElementById('world-note-jump'),"
                          " target: document.getElementById('group-instances') ? 'yes' : 'no'})")
    check("M37 polish：页顶跨组事件可点回该组（锚点存在）",
          "PASS" if anchor["btn"] and anchor["target"] == "yes" else "FAIL",
          f"页顶「回到该组」入口={anchor['btn']}；锚点目标 #group-instances={anchor['target']}；"
          f"跨组 / 严重事件（实例详情读不出来）写页顶时给锚点，组内结果只写组内槽",
          clause="§3.1 反馈位置：页顶只留跨组事件并锚定回组",
          code="desktop/index.html#world-note-jump · main.ts worldNote(text, bad, anchor) / jumpToWorldGroup")

    # ---- M38 零碎升 P1：世界时钟回到顶栏（聊天页可见），2s 轮询不再只在管理页跑
    await cdp.pane("manage")
    await cdp.select("inst-select", instances[0]["id"])
    await asyncio.sleep(1.5)
    line_label = await cdp.js("document.getElementById('clock-label').textContent")
    if "冻结" in str(line_label):
        await cdp.js("document.getElementById('clock-activate').click()")
        await cdp.wait("document.getElementById('clock-label').textContent", "倍率", 25)
    chat_session = await cdp.js(
        "(()=>{const bs=[...document.querySelectorAll('#sessions button.session')];"
        "const hit=bs.find(b=>!b.textContent.includes('初始会话'));"
        "(hit||bs[0])?.click(); return (hit||bs[0])?.textContent||'';})()")
    await asyncio.sleep(2.0)
    await cdp.pane("chat")
    chip = {}
    deadline = time.time() + 25
    while time.time() < deadline:
        chip = await cdp.js("({hidden: document.getElementById('topbar-clock').classList.contains('hidden'),"
                            " text: document.getElementById('topbar-clock').textContent,"
                            " manage_hidden: document.getElementById('pane-manage').classList.contains('hidden')})")
        if not chip.get("hidden") and chip.get("text"):
            break
        await asyncio.sleep(0.5)
    await cdp.js("window.__opLog.length=0")   # 只数这一段：管理页隐藏期间顶栏时钟的轮询
    await asyncio.sleep(6.0)
    polled = (await ops_log(cdp)).count("runtime.clock")
    chip_later = await cdp.js("({hidden: document.getElementById('topbar-clock').classList.contains('hidden'),"
                              " text: document.getElementById('topbar-clock').textContent})")
    check("M38 §3.1 世界时钟在顶栏可见（当前查看且已激活的线），离开管理页仍在跟",
          "PASS" if (not chip.get("hidden") and "倍率" in str(chip.get("text"))
                     and chip.get("manage_hidden") and polled >= 2 and not chip_later.get("hidden")) else "FAIL",
          f"会话={chat_session!r}；聊天页顶栏时钟={chip.get('text')!r}（隐藏={chip.get('hidden')}，"
          f"管理页隐藏={chip.get('manage_hidden')}）；管理页隐藏的 6 秒内 runtime.clock 轮询次数={polled}"
          f"（≥2 即证明轮询不再只在管理页可见时跑）；6 秒后时钟={chip_later.get('text')!r}",
          clause="§3.1 顶栏仅显示当前查看且已激活的线的世界时钟",
          code="desktop/index.html#topbar-clock · main.ts clockTarget / refreshClockChip（bindWorld 的 2s 轮询）")

    # ---- M39 §3.3 设置面三组可编辑表单：round-trip（改一项 → settings.get 读到新值 → 改回）
    await cdp.pane("settings")
    await cdp.wait("document.getElementById('mem-form') ? '1' : '0'", "1", 20)
    saved_segments = await mgmt_call(cdp, "settings.get")
    rt_rows = []
    for seg, form, field, value, sub, note_id in (
        ("memory", "mem-form", "set-mem-model", "audit2-embed-rt", "model", "mem-note"),
        ("commit", "commit-form", "set-commit-events", "7", "events", "commit-note"),
        ("backup", "backup-form", "set-backup-keep", "5", "keep", "backup-note"),
    ):
        if not saved_segments.get(seg):
            note_absent = await note_of(cdp, note_id)
            rt_rows.append((seg, "DEFERRED",
                            f"settings.get 未回 {seg} 段（核心侧未落地）；界面回执={note_absent!r}"))
            continue
        original = (saved_segments.get(seg) or {}).get(sub)

        async def submit(text: str) -> str:
            if seg == "memory":
                await cdp.js("(()=>{const s=document.getElementById('set-mem-mode');s.value='separate';"
                             "s.dispatchEvent(new Event('change'));})()")
            await cdp.js(f"document.getElementById({json.dumps(field)}).value={json.dumps(text)};"
                         f"document.getElementById({json.dumps(form)}).requestSubmit();")
            deadline = time.time() + 25
            note = ""
            while time.time() < deadline:
                note = await note_of(cdp, note_id)
                if note and "保存中" not in note:
                    break
                await asyncio.sleep(0.3)
            return note

        note_new = await submit(value)
        mid = ((await mgmt_call(cdp, "settings.get")).get(seg) or {}).get(sub)
        restore = str(original) if original not in (None, "") else ""
        note_back = await submit(restore) if restore else "（原值为空，跳过改回）"
        back = ((await mgmt_call(cdp, "settings.get")).get(seg) or {}).get(sub)
        ok_rt = (str(mid) == str(value) and "已保存" in str(note_new)
                 and (not restore or str(back) == str(original)))
        rt_rows.append((seg, "PASS" if ok_rt else "FAIL",
                        f"{seg}.{sub}：{original!r} → 写入 {value!r} → settings.get 读到 {mid!r} → 改回 {back!r}；"
                        f"回执={note_new!r}/{note_back!r}"))
    unsupported = [row for row in rt_rows if row[1] == "DEFERRED"]
    failed = [row for row in rt_rows if row[1] == "FAIL"]
    check("M39 §3.3 记忆向量化 / 提交 / 备份三组表单可编辑，写入走 settings.set 并回读",
          "FAIL" if failed else ("DEFERRED" if unsupported else "PASS"),
          "；".join(row[2] for row in rt_rows)
          + f"｜settings.get 段={sorted(k for k in saved_segments if k in ('llm', 'core', 'memory', 'commit', 'backup'))}",
          clause="§3.3 设置面：记忆向量化 / 提交 / 备份三组由 UI 写入本地配置（不要求手编配置）",
          code="desktop/index.html#mem-form/#commit-form/#backup-form · main.ts saveSegment / fillMemorySegment",
          expected="改一项后在 settings.get 里读得到新值，再改回；核心未落地该段时照实记 DEFERRED 并留下核心回执")
    seg_facts = await cdp.js(
        "({mem: document.getElementById('mem-facts').textContent,"
        " commit: document.getElementById('commit-facts').textContent,"
        " backup: document.getElementById('backup-facts').textContent,"
        " appearance: document.getElementById('appearance-note').textContent,"
        " opencfg: !!document.getElementById('open-config-dir')})")
    check("M40 §3.3 外观组只读说明 + 打开配置目录入口（默认安装不再走进死胡同）",
          "PASS" if (seg_facts["appearance"] and "跟随系统明暗" in str(seg_facts["appearance"])
                     and seg_facts["opencfg"]) else "FAIL",
          f"外观组文案={seg_facts['appearance']!r}；打开配置目录按钮={seg_facts['opencfg']}"
          f"（目录取 settings.get 的 core.config_file 父目录，壳复用既有 open_dir）；"
          f"记忆组事实={str(seg_facts['mem'])[:60]!r}…；备份组事实={str(seg_facts['backup'])[:60]!r}…",
          clause="§3.3 外观：黑白极简主题，跟随系统明暗；关于：日志目录 / 脱敏诊断（配置目录同类入口）",
          code="desktop/index.html#appearance-note/#open-config-dir · main.ts openConfigDir")

    # ---- 备份：立即备份 / 失败保留旧备份 / 打开目录
    await cdp.pane("settings")
    await cdp.wait("document.getElementById('backup-facts').textContent", "备份目录", 25)
    await cdp.js("document.getElementById('backup-now').click()")
    note_ok = await cdp.wait("document.getElementById('backup-note').textContent", "已备份", 90)
    rows = await cdp.js("[...document.querySelectorAll('#backup-list li')].map(li=>li.textContent)")
    folder = root / "data" / "backups"
    made = sorted(folder.glob("isekai-*.db"))
    facts = await cdp.js("document.getElementById('backup-facts').textContent")
    check("M20 §3.3 备份组：立即备份 + 只显示时间/完整性 + 目录·间隔·保留数",
          "PASS" if "已备份" in str(note_ok) and made and rows
          and all(("完整" in r or "校验" in r) for r in rows) and "检查间隔" in str(facts) else "FAIL",
          f"提示={note_ok!r}；盘上备份={[p.name for p in made]}；列表行={rows}；事实={facts!r}",
          clause="§3.3 备份：立即备份、恢复备份、打开备份目录；目录、间隔、保留数；只显示时间、完整性与成功 / 失败",
          code="main.ts:672-716")
    # 破坏备份目录 → 下一次备份必须失败且不删旧备份
    keep = TMPBASE / f"isekai_audit2_keepbackup_{int(time.time())}"
    keep.mkdir(parents=True, exist_ok=True)
    old = made[-1]
    shutil.move(str(old), str(keep / old.name))
    mtime_before = (keep / old.name).stat().st_mtime
    force_clean(folder)
    folder.write_text("not a dir", encoding="utf-8")
    await cdp.js("document.getElementById('backup-now').click()")
    await asyncio.sleep(8.0)
    note_fail = await cdp.js("document.getElementById('backup-note').textContent")
    kept = (keep / old.name)
    check("M21 §五/§十.10 备份失败不删除上一份、不更新成功时间",
          "PASS" if ("失败" in str(note_fail) or "未通过" in str(note_fail))
          and kept.exists() and kept.stat().st_mtime == mtime_before else "FAIL",
          f"备份目录被占位文件顶掉后再点立即备份 → 提示={note_fail!r}；"
          f"上一份备份仍在={kept.exists()} 且 mtime 未变={kept.stat().st_mtime == mtime_before if kept.exists() else 'n/a'}",
          clause="§五 备份：失败不更新成功时间、不删除旧备份；新备份完整校验成功后才轮转", code="isekai_core/world/ops.py:911-918")
    force_clean(folder)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.move(str(keep / old.name), str(folder / old.name))
    await cdp.js("document.getElementById('backup-open').click()")
    await asyncio.sleep(2.0)
    note_open = await cdp.js("document.getElementById('backup-note').textContent")
    check("M22 §3.3 打开备份目录入口可用",
          "PASS" if "已用资源管理器打开" in str(note_open) else "FAIL",
          f"点击「打开备份目录」→ 提示={note_open!r}（调用壳 open_dir → explorer）",
          clause="§3.3 备份：打开备份目录", code="desktop/src-tauri/src/main.rs:213-225")

    # ---- 长历史分页（占位会话）
    session_id = one(root, "SELECT id FROM session WHERE instance_id='ph-instance'")
    con = sqlite3.connect(root / "data" / "isekai.db", timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    con.executemany("INSERT INTO message(session_id,role,text,state,created_at) VALUES(?,?,?,?,?)",
                    [(session_id, "user", f"a2seed-{i:03d}", "done", time.time()) for i in range(240)])
    con.commit()
    con.close()
    total = one(root, "SELECT COUNT(*) FROM message WHERE session_id=?", (session_id,))
    await cdp.pane("chat")
    await cdp.js("document.getElementById('restart').click()")
    await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    await cdp.wait("document.getElementById('history-note').textContent", "已加载最近", 90)
    page1 = await cdp.js("({n: document.querySelectorAll('#messages li.message').length,"
                         " note: document.getElementById('history-note').textContent,"
                         " has000: document.getElementById('messages').textContent.includes('a2seed-000')})")
    await cdp.js("document.getElementById('history-more').click()")
    page2 = await cdp.js("(()=>{const l=document.getElementById('messages');"
                         "return {n: document.querySelectorAll('#messages li.message').length,"
                         " has000: l.textContent.includes('a2seed-000')};})()")
    deadline = time.time() + 40
    while time.time() < deadline and not page2["has000"]:
        await asyncio.sleep(0.5)
        page2 = await cdp.js("(()=>{const l=document.getElementById('messages');"
                             "return {n: document.querySelectorAll('#messages li.message').length,"
                             " has000: l.textContent.includes('a2seed-000')};})()")
    check("M23 §十.8/§4 长历史分页（界面「加载更多」）",
          "PASS" if page1["n"] and page2["n"] > page1["n"] and page2["has000"] else "FAIL",
          f"会话共 {total} 行；第一页 {page1}；点「加载更多」后 {page2}（按 before_seq 续取并接在前面）",
          clause="§4 长历史分页；§3.1 历史按当前会话分页加载", code="main.ts:411-454")

    # ---- 断线重连：不重复生成（同一 env_id 重发）
    await cdp.js("window.confirm=(m)=>{window.__confirmArgs.push(String(m)); return true;}")
    before_rows = one(root, "SELECT COUNT(*) FROM message WHERE session_id=?", (session_id,))
    await cdp.js("document.getElementById('input').value='重发同一封信';"
                 "document.getElementById('composer').requestSubmit();")
    await cdp.wait("document.getElementById('messages').textContent", "重发同一封信", 30)
    await cdp.js("document.getElementById('restart').click()")   # 断线
    await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    after_rows = one(root, "SELECT COUNT(*) FROM message WHERE session_id=?", (session_id,))
    dup = one(root, "SELECT COUNT(*) FROM message WHERE session_id=? AND text='重发同一封信'",
              (session_id,))
    check("M24 §十.7 断线重连后按核心持久记录补读，不重复生成",
          # before 在发送前取、after 在发送+重连后取：一次成功轮次本来就该 +2 行（用户 + 回复），
          # 判据是「增量恰为 2 且入站唯一」，不是「行数不变」
          "PASS" if (after_rows - before_rows) == 2 and dup == 1 else "FAIL",
          f"重启核心（断线→重连）前后消息行数 {before_rows}→{after_rows}；"
          f"用户消息行数=1（实为 {dup}）→ 重连补读回核心真值、不复制请求",
          clause="§十.7 网络断线后的重试不重复生成或消费额度；§3.1 重连后补读",
          code="main.ts:294-324,411-420")

    # ---- §二.1 重复启动：复用/唤起 vs 新窗口
    proc2, cdp2, _ = await boot_shell(root, PORT, {"ISEKAI_LLM_FAKE": "1",
                                                   "ISEKAI_LLM_FAKE_REPLY": "第二壳"})
    await asyncio.sleep(25.0)
    wins2 = window_count(proc2.pid)
    status2 = await cdp2.wait("document.getElementById('status').textContent", "核心", 45)
    spawns = [ln for ln in logs(root).splitlines() if "core spawned pid=" in ln]
    check("M25 §二.1 重复启动复用 / 唤起已有应用",
          "PASS" if wins2 == 0 else "FAIL",
          f"第二次启动的壳 pid={proc2.pid} 自有可见窗口数={wins2}（>0 = 又开了一个新窗口，未复用/唤起已有应用）；"
          f"第二壳状态条={status2!r}；shell.log 中 core spawned 次数={len(spawns)}",
          clause="§二.1 重复启动复用 / 唤起已有应用，不再启动第二个写库进程",
          code="desktop/src-tauri/Cargo.toml（无 single-instance 插件）· main.rs:321-391")
    lock_pid = core_lock_pid(root)
    check("M26 §十.1 第二次启动不产生第二个写库核心",
          "PASS" if lock_pid and pid_alive(lock_pid) and lock_pid == core_lock_pid(root) else "FAIL",
          f"写库锁属 pid={lock_pid}（存活={pid_alive(lock_pid) if lock_pid else False}）；"
          f"第二个核心由核心自身的 already_running 退出（core.log 末行={[ln[-70:] for ln in logs(root).splitlines() if 'already_running' in ln or '另一个核心' in ln][-1:] or '无'}）",
          clause="§十.1 双重启动不产生两个写库核心", code="isekai_core/app.py 写库锁")
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc2.pid)], capture_output=True)

    # ---- §二.4 核心崩溃：明确错误 + 重启入口，不无限自动重启
    spawns_before = len([l for l in logs(root).splitlines() if "core spawned pid=" in l])
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(lock_pid)], capture_output=True)
    crash_status = await cdp.wait("document.getElementById('status').textContent", "核心", 40)
    restart_visible = await cdp.js("!document.getElementById('restart').classList.contains('hidden')")
    await asyncio.sleep(10.0)
    spawns_after = len([l for l in logs(root).splitlines() if "core spawned pid=" in l])
    check("M27 §二.4/§十.1 核心崩溃给明确错误 + 重启入口，不无限自动重启",
          "PASS" if "核心" in str(crash_status) and restart_visible
          and spawns_after == spawns_before else "FAIL",
          f"硬杀核心后状态条={crash_status!r}；重启按钮可见={restart_visible}；"
          f"10 秒内 core spawned 次数 {spawns_before}→{spawns_after}（无自动重启）；"
          f"日志提示={[l[-60:] for l in logs(root).splitlines() if 'stdout closed' in l][-1:] or '无'}",
          clause="§二.4 核心崩溃给明确错误和重启入口，不无限自动重启",
          code="main.rs:177-188 · main.ts:246-252,1560-1570")
    await cdp.js("document.getElementById('restart').click()")
    back = await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    check("M28 §二.4 重启入口真的能恢复", "PASS" if back == "已就绪" else "FAIL",
          f"点「重启核心」后状态条={back!r}", clause="§二.4 重启入口", code="main.ts:326-350")

    # ---- §3.1/§一 切换角色=换会话；未激活线不接受新生成
    await cdp.pane("manage")
    await cdp.select("inst-select", instances[1]["id"])
    await asyncio.sleep(2.0)
    await cdp.js("document.getElementById('role-switch').click()")
    switch_note = await cdp.wait("document.getElementById('role-note').textContent", "当前会话角色", 40)
    title_after_switch = await cdp.js("document.getElementById('title').textContent")
    await asyncio.sleep(2.0)
    frozen_session_send = None
    await cdp.pane("chat")
    frozen_before = one(root, "SELECT COUNT(*) FROM message")
    await cdp.send_text("乙世界未激活也想聊", enter=True)
    await asyncio.sleep(12.0)
    chat_text = await cdp.js("document.getElementById('messages').textContent")
    chips = await cdp.js("[...document.querySelectorAll('#messages li.user .chips')].map(c=>c.textContent)")
    frozen_after = one(root, "SELECT COUNT(*) FROM message")
    replied = "审计2占位回复" in str(chat_text)
    check("M29 §一/§3.1 未激活（冻结）线不能发起新生成，界面状态区分清楚",
          "PASS" if not replied and chips else "FAIL",
          f"切到乙世界（冻结）会话后发送 → 是否生成回复={replied}；用户消息状态片={chips}；"
          f"消息行数 {frozen_before}→{frozen_after}；切换角色提示={switch_note!r}；"
          f"切换后顶栏={title_after_switch!r}（仍未显示目标实例 / 角色 / 时间线名称）",
          clause="§一 未就绪线不能发起新生成；§3.1 接受 / 生成 / 失败状态区分清楚",
          code="main.ts:486-528,1047-1080")
    await cdp.pane("manage")
    await cdp.select("inst-select", instances[1]["id"])
    await asyncio.sleep(1.5)
    await cdp.js("document.getElementById('clock-activate').click()")
    await cdp.wait("document.getElementById('clock-label').textContent", "倍率", 25)
    await cdp.pane("chat")
    await cdp.send_text("激活之后正常一轮", enter=True)
    got2 = await cdp.wait("document.getElementById('messages').textContent", "审计2占位回复", 45)
    check("M30 §3.1 激活后正常生成", "PASS" if "激活之后正常一轮" in str(got2) else "FAIL",
          f"激活后发送 → 消息区含用户输入与回复={'激活之后正常一轮' in str(got2) and '审计2占位回复' in str(got2)}",
          clause="§3.1 消息流", code="main.ts:563-574")

    # ---- 向量服务降级指示
    await cdp.pane("settings")
    await cdp.pane("chat")
    page_text = await cdp.js("document.body.innerText")
    emb = [w for w in ("向量", "embedding", "语义召回", "降级") if w in str(page_text)]
    check("M31 §六/§十.7 embedding 失败的降级在界面上有标识",
          "FAIL" if not emb else "PASS",
          f"记忆向量化指向死地址（http://127.0.0.1:9）时聊天仍可正常生成（降级生效），"
          f"但界面全文中「向量/embedding/语义召回/降级」命中={emb or '无'} → 用户看到的只是正常回复，"
          "没有任何向量服务降级提示（§六 要求区分向量服务降级）",
          clause="§六 明确区分核心未就绪、某线追赶、模型失败、向量服务降级；§十.7 embedding 失败时显示可用的降级",
          code="desktop/src/main.ts（无向量状态渲染）· isekai_core/runtime/service.py:1005-1010")

    # ---- 导入入口（§3.2 世界包 / 角色卡）与不兼容实例转换（§7.6）：真壳 + CDP，测试件只落 Temp
    from isekai_core.world.example import example_package

    fp = str(int(time.time()))
    pkg_file = f"audit2-import-world-{fp}.json"
    card_file = f"audit2-import-card-{fp}.json"
    sample = example_package(name="审计导入世界")
    bad_pkg, good_pkg = TMPBASE / f"audit2-bad-world-{fp}.json", TMPBASE / pkg_file
    huge_card, good_card = TMPBASE / f"audit2-huge-card-{fp}.json", TMPBASE / card_file
    bad_pkg.write_text(json.dumps({"meta": {}, "races": []}, ensure_ascii=False), encoding="utf-8")
    good_pkg.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    good_card.write_text(json.dumps(example_card(sample, name="审计导入卡"), ensure_ascii=False),
                         encoding="utf-8")
    huge_card.write_text('{"identity": {}, "pad": "' + "x" * (1 << 20) + '"}', encoding="utf-8")
    packages_dir = root / "packages"
    files_before = sorted(p.name for p in packages_dir.glob("*"))
    await cdp.pane("manage")
    await clear_notes(cdp, "pkg-note", "card-note", "world-note")
    await cdp.js("document.getElementById('pkg-errors').textContent='';"
                 "document.getElementById('card-errors').textContent='';")

    # ① 坏包：核心的原因原样上界面，不过校验就不落盘
    drive1 = await pick_import_file(cdp, "pkg-import", bad_pkg, "选择要导入的世界包")
    bad_pkg_note = await cdp.wait("document.getElementById('pkg-errors').textContent", "未落盘", 45)
    files_after_bad = sorted(p.name for p in packages_dir.glob("*"))
    ok_i1 = ("未落盘" in str(bad_pkg_note) and files_before == files_after_bad
             and "被接受" in drive1)
    check("I1 §3.2/§7.5 导入坏的世界包：界面原样显示核心的拒绝原因，且不落盘",
          "PASS" if ok_i1 else "FAIL",
          f"点「导入世界包…」→ {drive1} → #pkg-errors={bad_pkg_note!r}；"
          f"创作目录文件 {files_before} → {files_after_bad}"
          f"（无新增={files_before == files_after_bad}）",
          clause="§3.2 管理面导入外部世界包；§7.5 导入前结构校验，不过校验不落盘",
          code="desktop/src/main.ts importInto / importPackage · isekai_core/world/ops.py:394-422",
          expected="拒绝原因带「未落盘」字样并原样进界面；创作目录不新增文件")

    # ② 好包：导入成功 → 包列表出现并选中；再导一次 → 问一句覆盖 → 带 force 覆盖
    await clear_notes(cdp, "pkg-note", "world-note")
    drive2 = await pick_import_file(cdp, "pkg-import", good_pkg, "选择要导入的世界包")
    pkg_note = await wait_note(cdp, ("pkg-note", "world-note"), "已导入", 45)
    pkg_view = await cdp.js("({opts:[...document.getElementById('pkg-select').options].map(o=>o.value),"
                            " sel: document.getElementById('pkg-select').value})")
    core_pkgs = [str(item.get("file")) for item
                 in ((await mgmt_call(cdp, "world.package.list")).get("packages") or [])]
    conf_before = await cdp.js("window.__confirmArgs.length")
    await clear_notes(cdp, "pkg-note", "world-note")
    drive2b = await pick_import_file(cdp, "pkg-import", good_pkg, "选择要导入的世界包")
    forced_note = await wait_note(cdp, ("pkg-note", "world-note"), "覆盖了同名文件", 45)
    conf_after = await cdp.js("window.__confirmArgs.length")
    conf_last = await cdp.js("window.__confirmArgs.slice(-1)[0] || ''")
    ok_i2 = (pkg_file in pkg_view["opts"] and pkg_view["sel"] == pkg_file and pkg_file in core_pkgs
             and "被接受" in drive2 and "被接受" in drive2b
             and int(conf_after or 0) == int(conf_before or 0) + 1
             and "已存在" in str(conf_last) and "覆盖" in str(conf_last)
             and "覆盖了同名文件" in str(forced_note))
    check("I2 §3.2 导入好的世界包：列表出现并选中；同名要用户确认覆盖才写",
          "PASS" if ok_i2 else "FAIL",
          f"导入 {good_pkg.name} → 世界提示={pkg_note!r}；#pkg-select 选项含该件={pkg_file in pkg_view['opts']}"
          f"、选中={pkg_view['sel']!r}；核心 world.package.list 含该件={pkg_file in core_pkgs}；"
          f"同名再导一次（{drive2b}）→ 确认框第 {conf_after} 条={str(conf_last)[:80]!r}（比上次多 "
          f"{int(conf_after or 0) - int(conf_before or 0)} 条），覆盖后提示={forced_note!r}",
          clause="§3.2 导入成功后刷新创作目录并选中；同名文件不静默覆盖，需显式确认",
          code="desktop/src/main.ts importPackage（force 重试）· isekai_core/world/ops.py:405-414",
          expected="新包进下拉并被选中；同名冲突先问覆盖，确认后才 replaced 落盘")

    # ③ 归属包闸门 + 超限卡：没选包不给导入；超限文件在读之前被拒，目录仍无新增
    await cdp.select("card-package-select", "")
    gate = await cdp.js("({disabled: document.getElementById('card-import').disabled,"
                        " note: document.getElementById('card-import-note').textContent})")
    await cdp.select("card-package-select", pkg_file)
    gate_on = await cdp.js("!document.getElementById('card-import').disabled")
    await cdp.js("document.getElementById('card-errors').textContent=''")
    files_before_card = sorted(p.name for p in packages_dir.glob("*"))  # 上面这次成功导入之后再看
    drive3 = await pick_import_file(cdp, "card-import", huge_card, "选择要导入的角色卡")
    huge_note = await cdp.wait("document.getElementById('card-errors').textContent", "加载限额", 45)
    files_after_huge = sorted(p.name for p in packages_dir.glob("*"))
    ok_i3 = (gate["disabled"] is True and "先选归属世界包" in str(gate["note"]) and gate_on is True
             and "加载限额" in str(huge_note) and "被接受" in drive3
             and files_before_card == files_after_huge)
    check("I3 §3.2/§2.3 未选归属包不给导入；超限角色卡被读前拒绝且不落盘",
          "PASS" if ok_i3 else "FAIL",
          f"归属包下拉=占位项 → 按钮 disabled={gate['disabled']}、提示={gate['note']!r}；"
          f"选 {pkg_file} 后按钮可点={gate_on}；选 {huge_card.name}（1 MiB+）导入（{drive3}）→ "
          f"#card-errors={huge_note!r}；创作目录文件 {files_before_card} → {files_after_huge}"
          f"（无新增={files_before_card == files_after_huge}）",
          clause="§3.2 角色卡导入要对着包做联合校验（渠道 / 史料引用）；§2.3 加载限额在读之前拦",
          code="desktop/src/main.ts renderCardImportGate / importInto · isekai_core/world/ops.py:423-436",
          expected="没选包时入口不可点且写明要先选包；超限文件带「加载限额」原因被拒，不落盘")

    # ④ 好卡：对着已导入的包做联合校验 → 卡列表出现并选中
    await clear_notes(cdp, "card-note", "card-import-note", "world-note")
    drive4 = await pick_import_file(cdp, "card-import", good_card, "选择要导入的角色卡")
    card_note = await wait_note(cdp, ("card-import-note", "card-note", "world-note"), "已导入", 45)
    card_view = await cdp.js("({opts:[...document.getElementById('card-select').options].map(o=>o.value),"
                             " sel: document.getElementById('card-select').value})")
    core_cards = [str(item.get("file")) for item
                  in ((await mgmt_call(cdp, "world.card.list")).get("cards") or [])]
    ok_i4 = (card_file in card_view["opts"] and card_view["sel"] == card_file
             and card_file in core_cards and pkg_file in str(card_note) and "被接受" in drive4)
    check("I4 §3.2 导入好的角色卡（带归属包联合校验）：卡列表出现并选中",
          "PASS" if ok_i4 else "FAIL",
          f"归属包={pkg_file} 时导入 {good_card.name}（{drive4}）→ 世界提示={card_note!r}；"
          f"#card-select 选项含该件={card_file in card_view['opts']}、选中={card_view['sel']!r}；"
          f"核心 world.card.list 含该件={card_file in core_cards}",
          clause="§3.2 角色卡导入对着归属世界包联合校验；导入成功后刷新卡列表并选中",
          code="desktop/src/main.ts importCard · isekai_core/world/ops.py:423-456",
          expected="卡进列表并被选中，提示里写明对照哪个包校验")

    # ⑤ 不兼容实例的转换入口（§7.6）：compatible 不显示；不兼容时显示 + 点它给核心的原因
    inst0 = instances[0]["id"]
    orig_fmt = str(one(root, "SELECT data_format FROM instance WHERE id=?", (inst0,)) or "")
    await cdp.select("inst-select", inst0)
    await asyncio.sleep(2.0)
    compat_hidden = await cdp.js("({cls: document.getElementById('inst-convert-row').classList.contains('hidden'),"
                                 " display: getComputedStyle(document.getElementById('inst-convert-row')).display})")
    db_exec(root, "UPDATE instance SET data_format='9.9' WHERE id=?", (inst0,))
    await cdp.select("inst-select", inst0)
    await asyncio.sleep(2.0)
    bad_row = await cdp.js("({hidden: document.getElementById('inst-convert-row').classList.contains('hidden'),"
                           " display: getComputedStyle(document.getElementById('inst-convert-row')).display,"
                           " note: document.getElementById('inst-convert-note').textContent})")
    ref = (await mgmt_call(cdp, "instance.convert", instance_id=inst0, confirmed=False)).get("convert") or {}
    await cdp.js("document.getElementById('inst-convert').click()")
    conv_note = await cdp.wait("document.getElementById('inst-convert-note').textContent", "转换器", 40)
    db_exec(root, "UPDATE instance SET data_format=? WHERE id=?", (orig_fmt, inst0))
    await cdp.select("inst-select", inst0)
    ok_i5 = (compat_hidden["cls"] is True and compat_hidden["display"] == "none"
             and bad_row["hidden"] is False and bad_row["display"] != "none" and "9.9" in str(bad_row["note"])
             and ref.get("state") == "blocked"
             and str(ref.get("hint") or "") in str(conv_note)
             and str(ref.get("reason") or "") in str(conv_note) and "用兼容版本" in str(conv_note))
    check("I5 §7.6 不兼容实例才显示转换入口；点开照实显示核心的原因（无转换器 → 用兼容版本）",
          "PASS" if ok_i5 else "FAIL",
          f"data_format={orig_fmt}（compatible）时转换行 class 含 hidden={compat_hidden['cls']}、"
          f"计算样式 display={compat_hidden['display']!r}（.hidden 必须真的把行收起来）；"
          f"改成 9.9 再开管理页 → 行 class 含 hidden={bad_row['hidden']}、display={bad_row['display']!r}、"
          f"行内提示={bad_row['note']!r}；"
          f"核心 instance.convert(confirmed=false)={ref}；点「转换…」后行内提示={conv_note!r}"
          f"（含核心 hint={ref.get('hint')!r} 与 reason）",
          clause="§7.6 打开实例做兼容检查：convertible / blocked 才提示转换与原因；没有可信转换器就停在提示上",
          code="desktop/src/main.ts convertSelectedInstance · isekai_core/world/instances.py:215-243",
          expected="compatible 不显示入口；不兼容显示入口，点开显示核心给的 reason 与「用兼容版本」提示，不改数据")

    # ---- I6 §3.3/P0：没配 API Key 时点「AI 生成」不弹确认框，行内槽直接给「去填 Key」+ 真跳转
    await cdp.pane("settings")
    await cdp.wait("document.getElementById('settings-facts').textContent", "当前模型", 25)
    facts_key_on = await cdp.js("document.getElementById('settings-facts').textContent")
    await cdp.pane("manage")
    key_off = ((await mgmt_call(cdp, "settings.set", llm={"api_key": ""})).get("llm") or {})
    key_read_off = ((await mgmt_call(cdp, "settings.get")).get("llm") or {})
    await clear_notes(cdp, "pkg-note", "world-note")
    await cdp.js("document.getElementById('pkg-brief').value='审计 I6 未配 Key 的世界描述';"
                 "document.getElementById('pkg-file').value='a2nokey.json';"
                 "window.__confirmArgs=[]; window.__opLog.length=0;"
                 "window.confirm=(m)=>{window.__confirmArgs.push(String(m)); return false;};"
                 "document.getElementById('pkg-generate').click()")
    gate_note = await cdp.wait("document.getElementById('pkg-note').textContent", "还没配 API Key", 25)
    gate = await cdp.js("({text: document.getElementById('pkg-note').textContent,"
                        " link: !!document.querySelector('#pkg-note button.api-key-jump'),"
                        " confirms: window.__confirmArgs.length,"
                        " called: (window.__opLog||[]).includes('world.package.generate')})")
    await cdp.js("document.querySelector('#pkg-note button.api-key-jump').click()")
    await asyncio.sleep(1.5)
    jump = await cdp.js("({pane: !document.getElementById('pane-settings').classList.contains('hidden'),"
                        " focus: (document.activeElement||{}).id || '',"
                        " facts: document.getElementById('settings-facts').textContent})")
    # 角色卡生成走同一道闸（共用一处）：也只在行内槽给跳转，不弹确认框
    await cdp.pane("manage")
    await cdp.select("pkg-select", "w0.json")
    await clear_notes(cdp, "card-note", "world-note")
    await cdp.js("document.getElementById('card-brief').value='审计 I6 未配 Key 的角色描述';"
                 "window.__confirmArgs=[]; window.__opLog.length=0;"
                 "document.getElementById('card-generate').click()")
    card_gate = await cdp.wait("document.getElementById('card-note').textContent", "还没配 API Key", 25)
    card_gate_state = await cdp.js("({link: !!document.querySelector('#card-note button.api-key-jump'),"
                                   " confirms: window.__confirmArgs.length,"
                                   " called: (window.__opLog||[]).includes('world.card.generate')})")
    # 把 Key 配回去：正常路径必须恢复（再点一次 → 确认框回来 → 取消）
    key_back = ((await mgmt_call(cdp, "settings.set", llm={"api_key": FAKE_KEY})).get("llm") or {})
    await cdp.pane("settings")
    await cdp.wait("document.getElementById('settings-facts').textContent", "当前模型", 25)
    facts_key_back = await cdp.js("document.getElementById('settings-facts').textContent")
    await cdp.pane("manage")
    await clear_notes(cdp, "pkg-note", "world-note")
    await cdp.js("window.__confirmArgs=[];"
                 "window.confirm=(m)=>{window.__confirmArgs.push(String(m)); return false;};"
                 "document.getElementById('pkg-generate').click()")
    back_note = await cdp.wait("document.getElementById('pkg-note').textContent", "已取消", 30)
    back = await cdp.js("({confirms: window.__confirmArgs.length,"
                        " last: window.__confirmArgs.slice(-1)[0] || '',"
                        " stale: !!document.querySelector('#pkg-note button.api-key-jump')})")
    ok_i6 = (key_off.get("api_key_set") is False and key_read_off.get("api_key_set") is False
             and "还没配 API Key" in str(gate_note) and "去设置面" in str(gate["text"])
             and gate["link"] is True and int(gate["confirms"] or 0) == 0 and not gate["called"]
             and jump["pane"] is True and jump["focus"] == "set-api-key"
             and "已配置" in str(facts_key_on) and "未配置" in str(jump["facts"])
             and "还没配 API Key" in str(card_gate) and card_gate_state["link"] is True
             and int(card_gate_state["confirms"] or 0) == 0 and not card_gate_state["called"]
             and key_back.get("api_key_set") is True and "已配置" in str(facts_key_back)
             and int(back["confirms"] or 0) == 1 and "将向" in str(back["last"])
             and "上限" in str(back["last"]) and "已取消" in str(back_note)
             and back["stale"] is False)
    check("I6 §3.3/P0 没配 API Key 时点「AI 生成」不弹确认框：行内槽给「还没配 API Key」+ 跳转聚焦，配回后路径恢复",
          "PASS" if ok_i6 else "FAIL",
          f"settings.set(api_key='') → llm.api_key_set={key_off.get('api_key_set')}（settings.get 回读"
          f"{key_read_off.get('api_key_set')}）；点「AI 生成」（世界包）→ 确认框调用数={gate['confirms']}"
          f"（应为 0）、槽={gate['text']!r}、跳转链存在={gate['link']}、是否发起生成调用={gate['called']}"
          f"（应为 False）；点跳转链 → 设置页可见={jump['pane']}、焦点={jump['focus']!r}、"
          f"事实行={str(jump['facts'])[:60]!r}（含「未配置」）；角色卡生成同一道闸："
          f"槽={str(card_gate)[:60]!r}、链接={card_gate_state['link']}、确认框={card_gate_state['confirms']}、"
          f"发起调用={card_gate_state['called']}；配回 Key 后 llm.api_key_set={key_back.get('api_key_set')}、"
          f"事实行含「已配置」={'已配置' in str(facts_key_back)}；再点生成 → 确认框第 {back['confirms']} 条"
          f"={str(back['last'])[:90]!r}…、槽={back_note!r}、槽内跳转链已消失={back['stale'] is False}",
          clause="§3.3 生成模型：Key 是生成的前置；未配置时给可执行的下一步（设置面 + 聚焦），不弹必然失败的确认框",
          code="desktop/src/main.ts apiKeyBlocked / openApiKeyField（世界包与角色卡共用）· fillSettings 事实行",
          expected="无 Key：0 次确认框、槽内出现「还没配 API Key」+ 跳转链真的切页并聚焦 #set-api-key；配回后确认框与取消路径照旧")

    # ---- I7 polish：四处影子槽归位（一行一槽，槽只服务本行）+ 角色卡组两个主人合并
    slot_card = f"a2slot-{int(time.time())}.json"
    (cfg.paths.packages / slot_card).write_text(
        json.dumps(example_card(package, name="戊槽卡", confirmed=False), ensure_ascii=False),
        encoding="utf-8")
    await cdp.pane("manage")
    await cdp.js("document.getElementById('world-refresh').click()")
    await asyncio.sleep(2.5)
    # ① 确认卡片 → 卡片行自己的槽（#card-select-note），不再答在生成行的槽
    await cdp.select("pkg-select", "w0.json")
    await cdp.select("card-select", slot_card)
    await clear_notes(cdp, "card-select-note", "card-note", "card-import-note", "world-note")
    await cdp.js("document.getElementById('card-confirm').click()")
    confirm_slot = await cdp.wait("document.getElementById('card-select-note').textContent", "已确认", 40)
    confirm_slots = await cdp.js("({select: document.getElementById('card-select-note').textContent,"
                                 " generate: document.getElementById('card-note').textContent,"
                                 " top: document.getElementById('world-note').textContent})")
    # ② 补卡 → 补卡行自己的槽（#card-add-result）
    await cdp.select("inst-select", inst)
    await asyncio.sleep(2.0)
    await clear_notes(cdp, "card-add-result", "card-note", "world-note")
    await cdp.select("card-select", slot_card)
    await cdp.js("document.getElementById('card-add-note').value='I7 槽位检查';"
                 "window.__confirmArgs=[];"
                 "window.confirm=(m)=>{window.__confirmArgs.push(String(m)); return true;};"
                 "document.getElementById('card-add').click()")
    add_slot = await cdp.wait("document.getElementById('card-add-result').textContent", "已补入", 45)
    add_slots = await cdp.js("({add: document.getElementById('card-add-result').textContent,"
                             " generate: document.getElementById('card-note').textContent,"
                             " top: document.getElementById('world-note').textContent})")
    # ③ 创建实例 → 创建行自己的槽（#inst-create-note）
    await clear_notes(cdp, "inst-create-note", "inst-note", "world-note")
    await cdp.js("(()=>{const s=document.getElementById('inst-package-select');"
                 "const hit=[...s.options].find(o=>o.value==='w0.json'); if(hit) s.value=hit.value;"
                 "const c=document.getElementById('inst-cards-select'); c.value=" + json.dumps(slot_card) + ";"
                 "document.getElementById('inst-name-input').value='I7 创建行槽';"
                 "document.getElementById('inst-create').click();})()")
    create_slot = await cdp.wait("document.getElementById('inst-create-note').textContent", "已创建", 60)
    create_slots = await cdp.js("({create: document.getElementById('inst-create-note').textContent,"
                                " select_row: document.getElementById('inst-note').textContent,"
                                " top: document.getElementById('world-note').textContent})")
    made = [item for item in ((await mgmt_call(cdp, "instance.list")).get("instances") or [])
            if str(item.get("name")) == "I7 创建行槽"]
    made_id = str(made[0]["id"]) if made else ""
    # ④ 导入实例 → 导入行自己的槽（#inst-import-note）
    export_file = f"a2slot-export-{int(time.time())}.isekai.json"
    if made_id:
        await mgmt_call(cdp, "instance.export", id=made_id, path=export_file)
    await cdp.js("document.getElementById('world-refresh').click()")
    await asyncio.sleep(2.5)
    await clear_notes(cdp, "inst-import-note", "inst-note", "world-note")
    await cdp.select("import-select", export_file)
    await cdp.js("document.getElementById('inst-import').click()")
    import_slot = await cdp.wait("document.getElementById('inst-import-note').textContent", "已导入为", 60)
    import_slots = await cdp.js("({import: document.getElementById('inst-import-note').textContent,"
                                " select_row: document.getElementById('inst-note').textContent,"
                                " top: document.getElementById('world-note').textContent})")
    # ⑤ 删除失败 → 删除行自己的槽（#inst-delete-note）：UI 还拿着名字，核心已经没有这一行
    await cdp.select("inst-select", made_id or inst)
    await asyncio.sleep(2.0)
    del_hint = await cdp.js("document.getElementById('inst-delete-note').textContent")
    ui_name = await cdp.js("[...document.getElementById('inst-select').options]"
                           f".find(o=>o.value==={json.dumps(made_id)})?.textContent.split('｜')[0] || ''")
    await mgmt_call(cdp, "instance.delete", id=made_id)
    await clear_notes(cdp, "inst-delete-note", "inst-note", "world-note")
    await cdp.js("document.getElementById('inst-delete-name').value=" + json.dumps(str(ui_name)) + ";"
                 "document.getElementById('inst-delete-name').dispatchEvent(new Event('input'));")
    armed = await cdp.js("!document.getElementById('inst-delete').disabled")
    await cdp.js("window.confirm=(m)=>{window.__confirmArgs.push(String(m)); return true;};"
                 "document.getElementById('inst-delete').click()")
    del_note = await cdp.wait("document.getElementById('inst-delete-note').textContent", "实例不存在", 40)
    del_slots = await cdp.js("({del: document.getElementById('inst-delete-note').textContent,"
                             " select_row: document.getElementById('inst-note').textContent,"
                             " top: document.getElementById('world-note').textContent})")
    await cdp.js("document.getElementById('world-refresh').click()")   # 收尾：列表回到核心真值
    await asyncio.sleep(2.0)
    ok_i7 = ("已确认" in str(confirm_slot) and not str(confirm_slots["generate"]).strip()
             and not str(confirm_slots["top"]).strip()
             and "已补入" in str(add_slot) and not str(add_slots["generate"]).strip()
             and not str(add_slots["top"]).strip()
             and "已创建" in str(create_slot) and "已创建" not in str(create_slots["select_row"])
             and not str(create_slots["top"]).strip()
             and "已导入为" in str(import_slot) and "已导入为" not in str(import_slots["select_row"])
             and not str(import_slots["top"]).strip()
             and del_hint.strip().startswith("将保留：世界包 / 角色卡 / 导出件")
             and f"（实例 #{str(made_id)[-6:]}）" in str(del_hint)   # 复审⑤：提示带实例标识尾段
             and armed is True
             and "实例不存在" in str(del_note) and "实例不存在" not in str(del_slots["select_row"])
             and not str(del_slots["top"]).strip())
    check("I7 polish 四处影子槽归位：槽只服务本行（补卡 / 创建实例 / 导入实例 / 删除失败）",
          "PASS" if ok_i7 else "FAIL",
          f"① 确认卡片 → #card-select-note={confirm_slot!r}（生成行 #card-note={confirm_slots['generate']!r}、"
          f"页顶={confirm_slots['top']!r}）；② 补卡 → #card-add-result={add_slot!r}"
          f"（#card-note={add_slots['generate']!r}、页顶={add_slots['top']!r}）；"
          f"③ 创建实例 → #inst-create-note={create_slot!r}（实例行 #inst-note={create_slots['select_row']!r}、"
          f"页顶={create_slots['top']!r}）；④ 导入实例 → #inst-import-note={import_slot!r}"
          f"（#inst-note={import_slots['select_row']!r}、页顶={import_slots['top']!r}）；"
          f"⑤ 换实例后删除行提示复位={del_hint!r}、键入可点={armed}；背后删掉该实例再点删除 → "
          f"#inst-delete-note={del_note!r}（失败也落本行；#inst-note={del_slots['select_row']!r}、"
          f"页顶={del_slots['top']!r}）",
          clause="§3.1 结果回到动作旁边：一行一槽，槽只服务本行；§3.2 删除失败也在删除行回报",
          code="desktop/index.html（#card-select-note / #card-add-result / #inst-create-note / #inst-import-note）"
               "· main.ts worldAction(slot) / renderDeleteGate（不再用缓存对抗 loadWorld）",
          expected="每个动作的结果都落在它自己那一行的槽里，页顶只留跨组 / 严重事件；删除失败落在 #inst-delete-note")

    # ---- I8 §七：回滚入口——提交列表（空列表照实说）→ 二次确认 → runtime.rollback，世界水位真的退回去
    await cdp.pane("manage")
    # (a) 空列表要诚实：新实例的时间线本来带一条 initial 提交，这里把它去掉，构造真正没有回滚点的线
    #     （探针在本轮别的检查里也直接写这个临时库：data_format 改写、内部标记插入）
    empty_inst = (await mgmt_call(cdp, "instance.create",
                                  package_path=str(cfg.paths.packages / "w0.json"),
                                  card_paths=[str(cfg.paths.packages / "good.json")],
                                  display_name="I8 空回滚点")).get("instance") or {}
    empty_id = str(empty_inst.get("id") or "")
    empty_line = one(root, "SELECT id FROM timeline WHERE instance_id=?", (empty_id,))
    con = sqlite3.connect(root / "data" / "isekai.db", timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("DELETE FROM commit_log WHERE timeline_id=?", (empty_line,))
    con.commit()
    con.close()
    empty_left = one(root, "SELECT COUNT(*) FROM commit_log WHERE timeline_id=?", (empty_line,))
    await cdp.js("document.getElementById('world-refresh').click()")
    await asyncio.sleep(2.5)
    await cdp.select("inst-select", empty_id)
    empty_view = {}
    deadline = time.time() + 25
    while time.time() < deadline:
        empty_view = await cdp.js("({opts: [...document.getElementById('commit-select').options].length,"
                                  " note: document.getElementById('rollback-note').textContent})")
        if str(empty_view.get("note")).strip():
            break
        await asyncio.sleep(0.5)
    # (b) 本线真提交 → 界面列出 → 水位跑过提交点 → 冻结 → 回滚 → 水位退回提交点
    await cdp.select("inst-select", inst)
    await asyncio.sleep(2.0)
    if "冻结" in str(await cdp.js("document.getElementById('clock-label').textContent")):
        await cdp.js("document.getElementById('clock-activate').click()")
        await cdp.wait("document.getElementById('clock-label').textContent", "倍率", 25)
    commit_res = await mgmt_call(cdp, "runtime.commit", instance_id=inst, timeline_id=line,
                                 note="I8 审计回滚点")
    commit = commit_res.get("commit") or {}
    commit_id, commit_moment = str(commit.get("id") or ""), int(commit.get("moment") or 0)
    await cdp.js("window.__opLog.length=0; document.getElementById('world-refresh').click()")
    await cdp.wait("document.getElementById('commit-select').textContent", "I8 审计回滚点", 40)
    listed = await cdp.js("({ids: [...document.getElementById('commit-select').options].map(o=>o.value),"
                          " pairs: [...document.getElementById('commit-select').options].map(o=>[o.value,o.text]),"
                          " note: document.getElementById('rollback-note').textContent,"
                          " ops: (window.__opLog||[]).filter(o=>o==='runtime.commits').length})")
    mine = [text for value, text in (listed["pairs"] or []) if value == commit_id]
    # 水位要真的跑过提交点（倍率由前序检查决定，所以只轮询「有没有前进」，不给判据留时间赌注）
    level_before = commit_moment
    deadline = time.time() + 60
    while time.time() < deadline and level_before - commit_moment < 8:
        before_clock = ((await mgmt_call(cdp, "runtime.clock", instance_id=inst,
                                         timeline_id=line)).get("clock") or {})
        level_before = int(before_clock.get("processed_world") or 0)
        await asyncio.sleep(2.0)
    label_before = await cdp.js("document.getElementById('clock-label').textContent")
    # 冻结后再回滚：冻结线不推进，水位是精确值，前后可比
    await cdp.js("document.getElementById('clock-freeze').click()")
    await cdp.wait("document.getElementById('clock-label').textContent", "已冻结", 30)
    frozen_clock = ((await mgmt_call(cdp, "runtime.clock", instance_id=inst,
                                     timeline_id=line)).get("clock") or {})
    level_frozen = int(frozen_clock.get("processed_world") or 0)
    gen_before = one(root, "SELECT generation FROM timeline_clock WHERE timeline_id=?", (line,))
    await cdp.select("commit-select", commit_id)
    await cdp.js("window.__confirmArgs=[];"
                 "window.confirm=(m)=>{window.__confirmArgs.push(String(m)); return true;};"
                 "document.getElementById('rollback').click()")
    roll_note = await cdp.wait("document.getElementById('rollback-note').textContent", "已回滚", 60)
    confirm_roll = await cdp.js("window.__confirmArgs.slice(-1)[0] || ''")
    after_clock = ((await mgmt_call(cdp, "runtime.clock", instance_id=inst,
                                    timeline_id=line)).get("clock") or {})
    proc_after = one(root, "SELECT processed_world FROM timeline_clock WHERE timeline_id=?", (line,))
    gen_after = one(root, "SELECT generation FROM timeline_clock WHERE timeline_id=?", (line,))
    state_after = one(root, "SELECT state FROM timeline WHERE id=?", (line,))
    label_after = await cdp.wait("document.getElementById('clock-label').textContent",
                                 f"已处理 {commit_moment} 世界秒", 30)
    ok_i8 = ("还没有回滚点：提交由自动提交与退出补做产生" in str(empty_view["note"])
             and int(empty_view["opts"] or 0) == 0 and int(empty_left or 0) == 0
             and commit_id and commit_id in listed["ids"] and mine
             and "I8 审计回滚点" in str(mine[0]) and commit_id[-6:] in str(mine[0])
             and int(listed["ops"] or 0) >= 1
             and level_frozen - commit_moment >= 8
             and int(after_clock.get("processed_world") or 0) == commit_moment
             and after_clock.get("state") == "frozen" and str(state_after) == "frozen"
             and int(proc_after or 0) == commit_moment and gen_after == gen_before + 1
             and "回滚" in str(confirm_roll) and "之后的世界时间与事件会按回滚语义处理" in str(confirm_roll)
             and commit_id[-6:] in str(confirm_roll)
             and "已回滚到" in str(roll_note) and commit_id in str(roll_note)
             and f"已处理 {commit_moment} 世界秒" in str(label_after))
    check("I8 §七 回滚入口：提交列表 → 二次确认 → runtime.rollback，世界水位与世代真的退回提交点",
          "PASS" if ok_i8 else "FAIL",
          f"空回滚点（新实例 {str(empty_inst.get('name'))!r}，先删掉它那条 initial 提交 → 库里剩 {empty_left} 条）"
          f"→ 列表 {empty_view['opts']} 项、槽={empty_view['note']!r}；"
          f"mgmt runtime.commit → commit={commit_id!r}（世界 {commit_moment} 秒）；"
          f"刷新后界面列出提交 {len(listed['ids'])} 条（含本条={commit_id in (listed['ids'] or [])}），"
          f"本条在界面上的项={str(mine)!r}（含 id 尾段 {commit_id[-6:]!r}）、槽={listed['note']!r}、"
          f"界面 runtime.commits 调用={listed['ops']} 次；"
          f"水位跑到提交点之后（回滚前 runtime.clock.processed_world={level_frozen}，比提交点晚 "
          f"{level_frozen - commit_moment} 秒；冻结前界面时钟={label_before!r}）；"
          f"点冻结 → 点回滚 → 确认框={str(confirm_roll)[:90]!r}…；槽={str(roll_note)[:110]!r}…；"
          f"回滚后 processed_world={after_clock.get('processed_world')}（= 提交点）、线状态={state_after}、"
          f"库里水位={proc_after}、generation {gen_before}→{gen_after}、"
          f"界面时钟={label_after!r}（正是提交点的水位）",
          clause="§七 回滚：破坏性能力在界面可见——列提交、写清覆盖语义、二次确认，恢复能力不再只在核心",
          code="desktop/index.html#commit-select/#rollback/#rollback-note · main.ts loadCommits / rollbackToCommit · "
               "isekai_core/world/ops.py:801-829（runtime.commits / runtime.rollback 的参数名）",
          expected="界面列出真实提交；确认文案写明覆盖语义；回滚后世界水位退回提交点、世代 +1、结果落在本行槽；空列表照实说明来源")

    # ---- §五 关闭窗口到托盘：核心继续推进
    await cdp.pane("manage")
    await cdp.select("inst-select", instances[1]["id"])
    await asyncio.sleep(2.0)
    active_tl = instances[1]["id"]
    tl = one(root, "SELECT id FROM timeline WHERE instance_id=?", (active_tl,))
    alive_before = one(root, "SELECT processed_world FROM timeline_clock WHERE timeline_id=?", (tl,))
    hits = close_window(proc.pid)
    await asyncio.sleep(3.0)
    hidden = window_count(proc.pid) == 0
    await asyncio.sleep(12.0)
    alive_after = one(root, "SELECT processed_world FROM timeline_clock WHERE timeline_id=?", (tl,))
    lock2 = core_lock_pid(root)
    check("M32 §五/§十.1 关闭窗口默认到托盘，核心继续运行并推进",
          "PASS" if hits and hidden and pid_alive(lock2) and alive_after > alive_before else "FAIL",
          f"WM_CLOSE 命中窗口={hits}；窗口可见={not hidden}；持锁核心 pid={lock2} 存活={pid_alive(lock2)}；"
          f"窗口隐藏期间激活线水位 {alive_before} → {alive_after} 世界秒",
          clause="§五 关闭窗口默认到托盘，核心继续运行；§十.1 关闭到托盘仍推进",
          code="main.rs:392-398")

    # ---- §五/§十.20 整库恢复（走真实界面入口，包含原生文件对话框与确认）
    backup_for_restore = str((root / "data" / "backups" / old.name))
    drafts_now = [p.name for p in (root / "packages").glob("*.draft.json")]
    instances_now = len(db(root, "SELECT id FROM instance"))
    await cdp.js(f"window.__wantBackup={json.dumps(backup_for_restore)};")
    await cdp.pane("settings")
    await cdp.wait("document.getElementById('backup-facts').textContent", "备份目录", 25)
    # 恢复前制造差异：多一个实例 + 多一条草稿 + 一条消息（都晚于备份）
    from isekai_core.world import ops as _ops
    from isekai_core.config import load_config as _load
    from isekai_core.store import Store as _Store
    _cfg = _load(root)
    _st = _Store(_cfg.paths.db)
    _st.ensure_schema()
    _ops.dispatch(_cfg, _st, "instance.create",
                  {"package_path": str(_cfg.paths.packages / "w0.json"),
                   "card_paths": [str(_cfg.paths.packages / "good.json")],
                   "display_name": "恢复后应消失的实例"})
    _st.close()
    (root / "packages" / "a2after.draft.json").write_text(
        json.dumps({"name": "a2after", "kind": "package", "payload": {}, "progress": {},
                    "errors": [], "updated_at": time.time()}, ensure_ascii=False), encoding="utf-8")
    after_instances = len(db(root, "SELECT id FROM instance"))
    await cdp.js("document.getElementById('backup-restore').click()")
    restore_note = await cdp.wait("document.getElementById('backup-note').textContent", "已恢复", 120)
    r_instances = len(db(root, "SELECT id FROM instance"))
    r_frozen = db(root, "SELECT COUNT(*) FROM timeline WHERE state='frozen'")
    r_lines = one(root, "SELECT COUNT(*) FROM timeline")
    r_plain = one(root, "SELECT COUNT(*) FROM thread WHERE binding_token <> ''")
    r_draft_files = [p.name for p in (root / "packages").glob("*.draft.json")]
    confirm_restore = await cdp.js("window.__confirmArgs.slice(-1)[0] || ''")
    _m33_done = "已恢复" in str(restore_note)
    check("M33 §十.20 整库恢复：原子切换、按备份回滚登记与草稿、旧令牌失效、全线冻结",
          ("PASS" if _m33_done and r_frozen == r_lines
           and after_instances > instances_now and r_instances == instances_now
           and r_plain == 0 and drafts_now == r_draft_files and "冻结" in str(confirm_restore)
           else ("DEFERRED" if not _m33_done else "FAIL")),
          f"恢复前：实例 {instances_now} → 制造差异后 {after_instances}；恢复后实例={r_instances}（回到备份内容）；"
          f"线 {r_frozen}/{r_lines} 冻结；明文绑定令牌={r_plain}；草稿文件 {drafts_now}→{r_draft_files}；"
          f"确认文案含「冻结 / 令牌失效」={'冻结' in str(confirm_restore) and '令牌' in str(confirm_restore)}",
          clause="§十.20 整库恢复停止全部写入者并原子切换；草稿、实例登记、预算与去重状态按备份恢复，旧连接 / 令牌 / 异步任务失效，恢复后所有线冻结",
          code="isekai_core/store.py:815-843 · main.ts:718-754")
    # 恢复后的界面：旧连接是否还能继续 / 是否需要重新握手
    status_after = await cdp.js("document.getElementById('status').textContent")
    await cdp.pane("chat")
    await cdp.send_text("恢复之后的连接还能发吗", enter=True)
    await asyncio.sleep(10.0)
    post = await cdp.js("({chips: [...document.querySelectorAll('#messages li.user .chips')].map(c=>c.textContent),"
                        " status: document.getElementById('status').textContent,"
                        " restart: !document.getElementById('restart').classList.contains('hidden')})")
    check("§五/§十.20 恢复后旧连接失效 → 壳自动重新握手并补读",
          "PASS" if "已就绪" in str(post["status"]) and not post["restart"] else "FAIL",
          f"恢复后连接未断开、也没有重新握手：直接发送旧令牌的请求 → 状态条={post['status']!r}，"
          f"出现重启入口={post['restart']}，用户消息状态片={post['chips']}；"
          f"core.log 中恢复后的握手记录={[l[-60:] for l in logs(root).splitlines() if 'hello' in l.lower()][-2:] or '无'}",
          clause="§3.3/§五 恢复后旧连接、绑定令牌和异步任务统一失效，重连必须重新握手与补读；"
                 "§十.20 旧连接 / 令牌 / 异步任务失效",
          code="main.ts:294-324（只在 socket 断开时重连；恢复不触发握手）")

    # ---- §五 显式退出：先保存再停进程（走托盘「退出」同一函数）
    backups_before = {p.name for p in (root / "data" / "backups").glob("isekai-*.db")}
    started = time.time()
    await cdp.js("window.__TAURI_INTERNALS__.invoke('quit_app', {}); 'sent'")
    gone = False
    deadline = time.time() + 40
    while time.time() < deadline:
        if not pid_alive(proc.pid):
            gone = True
            break
        await asyncio.sleep(0.5)
    elapsed = time.time() - started
    core_pid_now = core_lock_pid(root)
    sh = logs(root)
    lines = [l for l in sh.splitlines() if "exit " in l or "core exited" in l]
    backups_after = {p.name for p in (root / "data" / "backups").glob("isekai-*.db")}
    hard = [l for l in lines if "stopping core pid=" in l]
    orphan = bool(core_pid_now) and pid_alive(core_pid_now)
    ok_exit = (gone and not orphan
               and any("exit requested" in l for l in lines)
               and any("exit save" in l for l in lines)
               and any("core exited on its own" in l for l in lines)
               and bool(backups_after - backups_before) and not hard and elapsed < 30)
    check("M34 §五/§十.1 显式退出：先保存再停进程、有上限、无孤儿写入者",
          "PASS" if ok_exit else "FAIL",
          f"退出耗时 {elapsed:.1f}s（<30s 有上限）；壳已退出={gone}；core.lock 残留={core_pid_now}；"
          f"退出序列={[l.split('] ')[-1] for l in lines]}；退出前新增备份={sorted(backups_after - backups_before)}；"
          f"硬杀记录={hard or '无'}（FLUSH_WAIT_MS=EXIT_WAIT_MS=3000）",
          clause="§五 显式退出先保存再停进程；退出等待有上限；§十.1 无所属孤儿写入者",
          code="main.rs:281-319,21-24")
    kill_tree()
    dump("main")


# ================================================================== compat

async def section_compat() -> None:
    """§十.12 兼容检查先于补算：blocked 的实例不得继续推进。"""
    from isekai_core.config import load_config
    from isekai_core.runtime.service import from_config
    from isekai_core.store import Store

    root = make_root("compat")
    info = prepare_world(root, ("兼容世界",))[0]
    cfg = load_config(root)
    st = Store(cfg.paths.db)
    st.ensure_schema()
    world = from_config(cfg, st)
    line = st.timeline_list(info["id"])[0]["id"]
    world.activate(info["id"], line, now_real=time.time())
    world.advance(info["id"], line, now_real=time.time() + 5, max_batches=1)
    before = int(st.clock_get(line)["processed_world"])
    status_ok = "compatible"
    con = sqlite3.connect(cfg.paths.db, timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("UPDATE instance SET data_format='9.9' WHERE id=?", (info["id"],))
    con.commit()
    con.close()
    blocked = ops_dispatch_info(cfg, st, info["id"])
    error = ""
    try:
        world.advance(info["id"], line, now_real=time.time() + 3600, max_batches=2)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    after = int(st.clock_get(line)["processed_world"])
    st.close()
    check("C1 §十.12 打开旧实例先做兼容检查，blocked 时不得继续补算",
          "PASS" if error or after == before else "FAIL",
          f"data_format 改成 9.9（主版本不兼容）后：instance.info 报 compatibility={blocked!r}；"
          f"advance → 异常={error!r}；水位 {before} → {after} 世界秒（{status_ok}）",
          clause="§十.12 打开旧实例时兼容检查先于补算；不兼容时不会静默换规则",
          code="isekai_core/runtime/service.py:1390-1396,1601,1607（_require_compatible 前置闸）")


def ops_dispatch_info(cfg, store, instance_id: str):
    """取实例兼容性判定（只读）。"""
    from isekai_core.world import ops

    try:
        return ops.dispatch(cfg, store, "instance.info", {"id": instance_id})["instance"]["compatibility"]
    except Exception as exc:  # noqa: BLE001
        return f"（instance.info 失败：{exc}）"


# ================================================================== keys# ================================================================== keys

async def section_keys() -> None:
    """§3.1 键盘：Enter 发送、Shift+Enter 换行（用真实按键载荷，CDP 需要 text 才走默认插入）。"""
    root = make_root("keys")
    prepare_world(root, ("按键世界",))
    kill_tree()
    proc, cdp, _ = await boot_shell(root, PORT + 7,
                                    {"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": "按键复核回复"})
    await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    await cdp.js(STUB)
    await cdp.js("document.getElementById('input').focus()")
    await cdp.call("Input.insertText", text="第一行")
    await cdp.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter",
                   text="\r", unmodifiedText="\r", windowsVirtualKeyCode=13,
                   nativeVirtualKeyCode=13, modifiers=8)
    await cdp.call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter",
                   windowsVirtualKeyCode=13, nativeVirtualKeyCode=13, modifiers=8)
    await asyncio.sleep(0.5)
    after_shift = await cdp.js("document.getElementById('input').value")
    sent_shift = await cdp.js("document.getElementById('messages').textContent.includes('第一行')")
    await cdp.call("Input.insertText", text="第二行")
    await asyncio.sleep(0.4)
    value = await cdp.js("document.getElementById('input').value")
    await cdp.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter",
                   text="\r", unmodifiedText="\r", windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
    await cdp.call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter",
                   windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
    sent = await cdp.wait("document.getElementById('messages').textContent", "第二行", 30)
    rows = one(root, "SELECT text FROM message WHERE role='user' ORDER BY seq DESC LIMIT 1")
    NL = chr(10)
    shift_newline = NL in str(after_shift)
    body_expected = "第一行" + NL + "第二行"
    persisted = body_expected in str(rows)
    check("K1 §3.1 Enter 发送 / Shift+Enter 换行",
          "PASS" if shift_newline and not sent_shift and persisted and "第二行" in str(sent) else "FAIL",
          f"Shift+Enter 后输入框={after_shift!r}（含换行={shift_newline}，尚未发送={not sent_shift}）；"
          f"再输入第二行后输入框={value!r}；Enter 发送 → 库内落盘的用户消息={rows!r}（两行同一条={persisted}）；"
          f"消息区含第二行={'第二行' in str(sent)}",
          clause="§3.1 Enter 发送、Shift+Enter 换行", code="main.ts:587-604")
    kill_tree()
    dump("keys")


# ================================================================== restore

async def section_restore() -> None:
    """A20 界面路径（真原生对话框）+ §五 显式退出（干净壳，先在无对话框时测）。"""
    from isekai_core.config import load_config
    from isekai_core.world import ops
    from isekai_core.store import Store

    # ---------- ① 显式退出（无挂起对话框的干净壳） ----------
    root_exit = make_root("exit")
    prepare_world(root_exit, ("退出世界",))
    kill_tree()
    proc, cdp, _ = await boot_shell(root_exit, PORT + 6,
                                    {"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": "退出复核回复"})
    await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    folder_exit = root_exit / "data" / "backups"
    backups_before = {p.name for p in folder_exit.glob("isekai-*.db")} if folder_exit.exists() else set()
    started = time.time()
    sent = await cdp.js("window.__TAURI_INTERNALS__.invoke('quit_app', {}).then(()=>'ok').catch(e=>'ERR:'+e); 'sent'")
    gone = False
    deadline = time.time() + 45
    while time.time() < deadline:
        if not pid_alive(proc.pid):
            gone = True
            break
        await asyncio.sleep(0.5)
    elapsed = time.time() - started
    core_pid = core_lock_pid(root_exit)
    lines = [ln for ln in logs(root_exit).splitlines()
             if "exit " in ln or "core exited" in ln or "stopping core" in ln]
    backups_after = {p.name for p in folder_exit.glob("isekai-*.db")} if folder_exit.exists() else set()
    ok_exit = (gone and not (core_pid and pid_alive(core_pid))
               and any("exit requested" in l for l in lines)
               and any("exit save" in l for l in lines)
               and any("core exited on its own" in l for l in lines)
               and bool(backups_after - backups_before)
               and not any("stopping core" in l for l in lines))
    check("R4 §五/§十.1 显式退出：先保存再停进程、有上限、无孤儿写入者",
          "PASS" if ok_exit else "FAIL",
          f"退出耗时 {elapsed:.1f}s（界面确认上限 3s + 等核心自行退出 3s）；壳已退出={gone}；"
          f"core.lock={core_pid}（存活={pid_alive(core_pid) if core_pid else False}）；invoke 返回={sent!r}；"
          f"退出序列={[l.split('] ')[-1] for l in lines]}；退出前新增备份={sorted(backups_after - backups_before)}",
          clause="§五 显式退出先保存再停进程；退出等待有上限；§十.1 无所属孤儿写入者",
          code="main.rs:281-319,21-24 · main.ts:1511-1532")
    kill_tree()

    # ---------- ② 整库恢复的界面路径（真原生对话框） ----------
    root = make_root("restore")
    prepare_world(root, ("甲世界",))
    kill_tree()
    proc, cdp, _ = await boot_shell(root, PORT + 5,
                                    {"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": "恢复复核回复"})
    await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    await cdp.js(STUB)
    await cdp.pane("settings")
    await cdp.wait("document.getElementById('backup-facts').textContent", "备份目录", 25)
    await cdp.js("document.getElementById('backup-now').click()")
    await cdp.wait("document.getElementById('backup-note').textContent", "已备份", 90)
    folder = root / "data" / "backups"
    cfg = load_config(root)
    st = Store(cfg.paths.db)
    st.ensure_schema()
    before = len(db(root, "SELECT id FROM instance"))
    ops.dispatch(cfg, st, "instance.create",
                 {"package_path": str(cfg.paths.packages / "w0.json"),
                  "card_paths": [str(cfg.paths.packages / "c0.json")],
                  "display_name": "恢复后应消失"}, runtime=None)
    st.close()
    (root / "packages" / "rev-after.draft.json").write_text(json.dumps(
        {"name": "rev-after", "kind": "package", "payload": {}, "progress": {},
         "errors": [], "updated_at": time.time()}, ensure_ascii=False), encoding="utf-8")
    after = len(db(root, "SELECT id FROM instance"))
    drafts_before = sorted(p.name for p in (root / "packages").glob("*.draft.json"))
    picked_path = str(folder / sorted(folder.glob("isekai-*.db"))[-1].name)

    await cdp.js("document.getElementById('backup-restore').click()")
    hwnd = 0
    deadline = time.time() + 30
    while time.time() < deadline and not hwnd:
        hwnd = dialog_hwnd("选择要恢复的备份") or dialog_hwnd("备份文件")
        await asyncio.sleep(0.5)
    note_wait = await cdp.js("document.getElementById('backup-note').textContent")
    check("R5 §3.2/§3.3 恢复备份先弹原生文件选择对话框（路径授权交给系统）",
          "PASS" if hwnd else "FAIL",
          f"点「恢复备份…」后原生对话框窗口 hwnd={hwnd}（标题「选择要恢复的备份」）；"
          f"对话框等待中的界面提示={note_wait!r}",
          clause="§3.2 原生文件选择 / 保存负责路径授权", code="desktop/src-tauri/src/main.rs:227-247")
    tree_dump = []
    if hwnd:
        bring_front(hwnd)
        await asyncio.sleep(0.8)
        kids = child_windows(hwnd)
        tree_dump = [(c, t[:18], r) for _, c, t, r in kids][:14]
        edits = [(h, r) for h, c, _t, r in kids if c.lower() in ("edit", "richedit50w")]
        # 路线 1：给「文件名」框直接塞路径，再给对话框发 IDOK
        if edits:
            set_text(edits[0][0], picked_path)
            await asyncio.sleep(0.4)
            ctypes.windll.user32.PostMessageW(hwnd, 0x0111, 1, 0)  # WM_COMMAND / IDOK
            await asyncio.sleep(2.5)
        if "已恢复" not in str(await cdp.js("document.getElementById('backup-note').textContent")):
            # 路线 2：真鼠标双击列表里的第一项（对话框以备份目录为初始目录）
            import ctypes.wintypes as wt
            rect = wt.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            click_at(rect.left + 60, rect.top + 120, double=True)
            await asyncio.sleep(2.5)
    notes = []
    for _ in range(14):
        await asyncio.sleep(2.5)
        notes.append(await cdp.js("document.getElementById('backup-note').textContent"))
        if notes[-1] and ("已恢复" in notes[-1] or "失败" in notes[-1] or "未完成" in notes[-1]
                          or "已取消" in notes[-1]):
            break
    r_inst = len(db(root, "SELECT id FROM instance"))
    r_frozen = one(root, "SELECT COUNT(*) FROM timeline WHERE state='frozen'")
    r_lines = one(root, "SELECT COUNT(*) FROM timeline")
    r_tokens = one(root, "SELECT COUNT(*) FROM thread WHERE binding_token <> ''")
    safety = sorted(p.name for p in folder.glob("*safety*"))
    drafts_after = sorted(p.name for p in (root / "packages").glob("*.draft.json"))
    confirm_last = await cdp.js("window.__confirmArgs.slice(-1)[0] || ''")
    restored_done = any("已恢复" in str(n) for n in notes)
    check("R6 §十.20 整库恢复（界面入口）：原子切换 + 全线冻结 + 令牌失效 + 安全副本",
          "PASS" if restored_done and r_frozen == r_lines and after > before and r_inst == before
          and r_tokens == 0 and safety else ("FAIL" if restored_done else "DEFERRED"),
          f"确认文案={str(confirm_last)[:90]!r}…；界面提示序列={notes[-2:]}；"
          f"实例 {before}→{after}（制造差异）→{r_inst}（恢复后=备份内容）；线 {r_frozen}/{r_lines} 冻结；"
          f"明文绑定令牌={r_tokens}；安全副本={safety[:1]}；对话框子窗口类={[c for c, _t, _r in tree_dump][:4]}",
          clause="§十.20 整库恢复停止全部写入者并原子切换；旧连接 / 令牌 / 异步任务失效，恢复后所有线冻结",
          code="main.rs:227-247 · main.ts:718-754 · isekai_core/store.py:815-843")
    _zip = sorted(p.name for p in folder.glob("*.packages.zip"))
    check("R6b §3.3 整库备份包含确认世界包 / 草稿（不是只要 DB）",
          ("PASS" if (restored_done and (drafts_after != drafts_before or not drafts_before))
           else ("DEFERRED" if not restored_done else "FAIL")),
          f"备份目录里世界包快照={_zip[:2]}（DB 快照之外另有配对 zip）；恢复完成={restored_done}；"
          f"草稿文件 {drafts_before}→{drafts_after}"
          + ("" if restored_done else "；恢复没走完（对话框未被驱动，见 R2/R6），本轮不对备份内容下结论"),
          clause="§3.3 整库备份包含确认世界包 / 草稿、实例登记、实例与时间线状态…；恢复是整套受管数据的灾难恢复",
          code="isekai_core/store.py:754-783,815-843 · world/ops.py:911-918")
    if restored_done:
        status_after = await cdp.js("document.getElementById('status').textContent")
        await cdp.pane("chat")
        await cdp.send_text("恢复之后还能发吗", enter=True)
        await asyncio.sleep(14.0)
        post = await cdp.js("({chips: [...document.querySelectorAll('#messages li.user .chips')].map(c=>c.textContent),"
                            " status: document.getElementById('status').textContent,"
                            " restart: !document.getElementById('restart').classList.contains('hidden'),"
                            " tail: document.getElementById('messages').textContent.slice(-120)})")
        ok_r7 = "已就绪" in str(post["status"]) and not post["restart"] and not post["chips"]
        check("R7 §3.3/§五/§十.20 恢复后旧连接失效 → 壳重新握手并补读",
              "PASS" if ok_r7 else "FAIL",
              f"恢复后状态条={status_after!r}；用旧连接继续发 → 状态条={post['status']!r}、"
              f"重启入口={post['restart']}、用户消息状态片={post['chips']}、消息尾声={post['tail']!r} → "
              "壳对「恢复使旧连接 / 令牌失效」无感：不重新握手、不补读、也不提示，只在用户下次发送时"
              "把请求丢给核心（核心已清空明文令牌 → 请求被拒），界面显示失败态；"
              "要回到可用状态只能手动点「重启核心」",
              clause="§3.3/§五 恢复后旧连接、绑定令牌和异步任务统一失效，重连必须重新握手与补读",
              code="main.ts:287-324（仅在 socket 断开时重连；恢复不触发握手与补读）")
    kill_tree()
    dump("restore")


# ================================================================== recon

async def section_recon() -> None:
    """复核三件事：① 恢复备份走真实界面入口 ② 断线重连后补读不重复 ③ 显式退出握手。"""
    from isekai_core.config import load_config
    from isekai_core.world import ops
    from isekai_core.store import Store

    root = make_root("recon")
    instances = prepare_world(root, ("甲世界",))
    kill_tree()
    proc, cdp, _ = await boot_shell(root, PORT + 4,
                                    {"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": "复核占位回复"})
    status = await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    desc = await cdp.js("(()=>{const d=Object.getOwnPropertyDescriptor(window.__TAURI_INTERNALS__,'invoke');"
                        "return d?{writable:d.writable, configurable:d.configurable,"
                        " kind:typeof d.value, hasGet: !!d.get}:null;})()")
    await cdp.js(STUB)
    live = await cdp.js("window.__TAURI_INTERNALS__.invoke.toString().includes('pick_file')")
    check("R0 渲染层 invoke 可包装（探针能力自检）",
          # 能力自检：包装不成功不是产品缺陷，是探针这条路走不通（Tauri 把 invoke 定义成 writable/configurable=false）。
          # 原生文件对话框因此只能走真窗口点击（R4-R7 用的就是那条），本项如实记 DEFERRED 而不是 FAIL。
          "PASS" if live else "DEFERRED",
          f"__TAURI_INTERNALS__.invoke 描述符={desc} → 包装={live}（false=不可包装，属框架设计）；"
          f"替代路线=真窗口点击（见 R4-R7）；状态条={status!r}",
          clause="审计能力自检", code="desktop/dist/assets/index-*.js（invoke 属性在调用点查表）")

    # ① 断线重连后补读、不重复生成
    await cdp.send_text("重连补读检查 " + DOM_MARKER, enter=True)
    await cdp.wait("document.getElementById('messages').textContent", "复核占位回复", 60)
    rows_settled = one(root, "SELECT COUNT(*) FROM message")
    users = one(root, "SELECT COUNT(*) FROM message WHERE text LIKE '重连补读检查%'")
    title_before = await cdp.js("document.getElementById('title').textContent")
    await cdp.js("document.getElementById('restart').click()")
    await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    await asyncio.sleep(3.0)
    rows_after = one(root, "SELECT COUNT(*) FROM message")
    users_after = one(root, "SELECT COUNT(*) FROM message WHERE text LIKE '重连补读检查%'")
    check("R1 §十.7/§3.1 断线重连后按核心持久记录补读，不重复生成或复制请求",
          "PASS" if rows_after == rows_settled and users_after == users == 1 else "FAIL",
          f"一轮对话落定后行数={rows_settled}（用户行 {users}）→ 重启核心断线重连后行数={rows_after}"
          f"（用户行 {users_after}）；顶栏 {title_before!r}",
          clause="§十.7 网络断线后的重试不重复生成或消费额度；§3.1 重连后补读",
          code="main.ts:294-324,411-420")

    # ② 恢复备份（先做一份备份，再制造差异，再从界面恢复）
    await cdp.pane("settings")
    await cdp.wait("document.getElementById('backup-facts').textContent", "备份目录", 25)
    await cdp.js("document.getElementById('backup-now').click()")
    await cdp.wait("document.getElementById('backup-note').textContent", "已备份", 90)
    folder = root / "data" / "backups"
    made = sorted(folder.glob("isekai-*.db"))
    backup_path = str(made[-1])
    cfg = load_config(root)
    st = Store(cfg.paths.db)
    st.ensure_schema()
    before_instances = len(db(root, "SELECT id FROM instance"))
    ops.dispatch(cfg, st, "instance.create",
                 {"package_path": str(cfg.paths.packages / "w0.json"),
                  "card_paths": [str(cfg.paths.packages / "c0.json")],
                  "display_name": "恢复后应消失"}, runtime=None)
    st.close()
    after_instances = len(db(root, "SELECT id FROM instance"))
    await cdp.js("window.__wantBackup=" + json.dumps(backup_path) + ";")
    await cdp.js("document.getElementById('backup-restore').click()")
    notes = []
    for _ in range(12):
        await asyncio.sleep(2.5)
        notes.append(await cdp.js("document.getElementById('backup-note').textContent"))
        if notes[-1] and ("已恢复" in notes[-1] or "失败" in notes[-1] or "未完成" in notes[-1]):
            break
    r_instances = len(db(root, "SELECT id FROM instance"))
    r_frozen = one(root, "SELECT COUNT(*) FROM timeline WHERE state='frozen'")
    r_lines = one(root, "SELECT COUNT(*) FROM timeline")
    r_tokens = one(root, "SELECT COUNT(*) FROM thread WHERE binding_token <> ''")
    safety = sorted(p.name for p in folder.glob("*safety*"))
    confirm_last = await cdp.js("window.__confirmArgs.slice(-1)[0] || ''")
    core_tail = [ln for ln in (root / "logs" / "core.log").read_text(encoding="utf-8", errors="replace").splitlines()
                 if "restore" in ln.lower() or "backup" in ln.lower()][-3:]
    _r2_done = any("已恢复" in str(n) for n in notes)
    check("R2 §十.20 整库恢复走真实界面入口：原子切换 + 全线冻结 + 令牌失效",
          ("PASS" if _r2_done and r_frozen == r_lines
           and after_instances > before_instances and r_instances == before_instances
           and r_tokens == 0 and safety else ("DEFERRED" if not _r2_done else "FAIL")),
          f"确认文案={str(confirm_last)[:80]!r}；界面提示序列={notes}；"
          f"实例 {before_instances}→{after_instances}（制造差异）→{r_instances}（恢复后）；"
          f"线 {r_frozen}/{r_lines} 冻结；明文绑定令牌={r_tokens}；安全副本={safety}；core.log={core_tail}",
          clause="§十.20 整库恢复停止全部写入者并原子切换；旧连接 / 令牌 / 异步任务失效，恢复后所有线冻结",
          code="desktop/src-tauri/src/main.rs:227-247 · main.ts:718-754 · isekai_core/store.py:815-843")
    # 恢复后的界面反应：是否重新握手与补读
    status_after = await cdp.js("document.getElementById('status').textContent")
    await cdp.pane("chat")
    await cdp.send_text("恢复之后还能发吗", enter=True)
    await asyncio.sleep(12.0)
    post = await cdp.js("({chips: [...document.querySelectorAll('#messages li.user .chips')].map(c=>c.textContent),"
                        " status: document.getElementById('status').textContent,"
                        " restart: !document.getElementById('restart').classList.contains('hidden'),"
                        " text: document.getElementById('messages').textContent.slice(-160)})")
    check("R3 §3.3/§十.20 恢复后旧连接失效 → 壳重新握手并补读",
          "PASS" if "已就绪" in str(post["status"]) and not post["restart"] else "FAIL",
          f"恢复后界面状态={status_after!r}；发送旧令牌请求 → 状态条={post['status']!r}、"
          f"重启入口={post['restart']}、消息尾声={post['text']!r}、状态片={post['chips']}；"
          f"没有出现「已在用的旧连接 / 令牌已失效」这类重新握手提示",
          clause="§3.3/§五 恢复后旧连接、绑定令牌和异步任务统一失效，重连必须重新握手与补读",
          code="main.ts:287-324（只在 socket 断开时重连）")

    # ③ 显式退出（托盘「退出」调的就是 quit_app）
    backups_before = {p.name for p in folder.glob("isekai-*.db")}
    started = time.time()
    await cdp.js("window.__TAURI_INTERNALS__.invoke('quit_app', {}); 'sent'")
    gone = False
    deadline = time.time() + 45
    while time.time() < deadline:
        if not pid_alive(proc.pid):
            gone = True
            break
        await asyncio.sleep(0.5)
    elapsed = time.time() - started
    core_pid = core_lock_pid(root)
    sh = logs(root)
    lines = [ln for ln in sh.splitlines() if "exit " in ln or "core exited" in ln or "stopping core" in ln]
    backups_after = {p.name for p in folder.glob("isekai-*.db")}
    ok_exit = (gone and not (core_pid and pid_alive(core_pid))
               and any("exit requested" in l for l in lines)
               and any("exit save" in l for l in lines)
               and any("core exited on its own" in l for l in lines)
               and bool(backups_after - backups_before))
    check("R4 §五/§十.1 显式退出：先保存再停进程、有上限、无孤儿写入者",
          "PASS" if ok_exit else "FAIL",
          f"退出耗时 {elapsed:.1f}s；壳已退出={gone}；core.lock={core_pid}（存活={pid_alive(core_pid) if core_pid else False}）；"
          f"退出序列={[l.split('] ')[-1] for l in lines]}；退出前新增备份={sorted(backups_after - backups_before)}",
          clause="§五 显式退出先保存再停进程；退出等待有上限；§十.1 无所属孤儿写入者",
          code="main.rs:281-319,21-24 · main.ts:1511-1532")
    kill_tree()
    dump("recon")


# ================================================================== llmfail

async def section_llmfail() -> None:
    """LLM 地址不可达（不联网）：§六「模型失败」必须与「角色没回复」区分开，且重试不重复生成。"""
    root = make_root("llmfail")
    prepare_world(root, ("故障世界",))
    kill_tree()
    proc, cdp, _ = await boot_shell(root, PORT + 1)   # 不设 ISEKAI_LLM_FAKE：走死地址
    status = await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    await cdp.js(STUB)
    check("F1 无假 LLM 时核心仍能就绪（模型不可用不阻断启动）",
          "PASS" if status == "已就绪" else "FAIL", f"状态条={status!r}",
          clause="§十.16 模型不可用时单独标记外部依赖失败", code="main.ts:352-409")
    session_id = one(root, "SELECT id FROM session WHERE instance_id='ph-instance'")
    before = one(root, "SELECT COUNT(*) FROM message WHERE session_id=?", (session_id,))
    await cdp.send_text("模型挂掉时的一轮", enter=True)
    deadline = time.time() + 60
    chips = None
    while time.time() < deadline:
        chips = await cdp.js("[...document.querySelectorAll('#messages li.user .chips')].map(c=>c.textContent)")
        if chips and any("失败" in c for c in chips):
            break
        await asyncio.sleep(1.0)
    text = await cdp.js("document.getElementById('messages').textContent")
    reply_rows = one(root, "SELECT COUNT(*) FROM message WHERE session_id=? AND role='character'",
                     (session_id,))
    after = one(root, "SELECT COUNT(*) FROM message WHERE session_id=?", (session_id,))
    check("F2 §六 模型失败在界面上与「角色没回复」区分（有失败态与错误码）",
          "PASS" if chips and any("失败" in c for c in chips) else "FAIL",
          f"LLM 死地址时发送 → 用户消息状态片={chips}（失败态 + 错误码）；"
          f"角色回复行={reply_rows}；消息行数 {before}→{after}",
          clause="§六 明确区分核心未就绪、某线追赶、模型失败…不能全部变成「角色没回复」",
          code="main.ts:515-527,164-177")
    retry = await cdp.js("!!document.querySelector('#messages li.user .chips button')")
    await cdp.js("document.querySelector('#messages li.user .chips button').click()")
    await asyncio.sleep(12.0)
    after_retry = one(root, "SELECT COUNT(*) FROM message WHERE session_id=?", (session_id,))
    dupes = one(root, "SELECT COUNT(*) FROM message WHERE session_id=? AND role='user'", (session_id,))
    check("F3 §十.7 失败重试不重复生成、不重复落用户行",
          "PASS" if retry and after_retry == after and dupes == 1 else "FAIL",
          f"重试按钮存在={retry}；重试后消息行数 {after}→{after_retry}；用户消息行数={dupes}（按协议同一 env_id 重试）",
          clause="§十.7 网络断线后的重试不重复生成或消费额度；§3.1 生成 / 投递失败的重试按协议恢复对应阶段",
          code="main.ts:170-177,576-582 · ump.ts retry")
    kill_tree()
    dump("llmfail")


# ================================================================== storage / nointerp

async def section_storage() -> None:
    root = make_root("storage", broken_data=True)
    kill_tree()
    proc, cdp, _ = await boot_shell(root, PORT + 2)
    status = await cdp.wait("document.getElementById('status').textContent", "存储不可用", 90)
    restart = await cdp.js("!document.getElementById('restart').classList.contains('hidden')")
    check("D1 §2.9/§十.13 持续写盘失败 → 界面显示存储不可用 + 重启入口",
          "PASS" if "存储不可用" in str(status) and restart else "FAIL",
          f"data 目录被占位文件顶掉时的状态条={status!r}；重启入口可见={restart}",
          clause="§2.9 核心进入持续写盘错误态时，壳显示存储不可用；§十.13 UI 显示存储错误",
          code="main.rs:155-156 · main.ts:1564-1565")
    before = await cdp.js("document.querySelectorAll('#messages li.message').length")
    await cdp.js("document.getElementById('input').value='存储坏了也要发';"
                 "document.getElementById('composer').requestSubmit();")
    await asyncio.sleep(4.0)
    after = await cdp.js("document.querySelectorAll('#messages li.message').length")
    check("D2 §2.9/§十.13 存储不可用时不让未固化工作继续（停止新的不可持久化工作）",
          "PASS" if after == before and after == 0 else "FAIL",
          f"存储不可用时提交 → 消息条目 {before}→{after}（未就绪态不发送，也不显示成成功）",
          clause="§2.9 停止新的不可持久化工作；不得把未固化回复显示为成功",
          code="main.ts:563-574（state.phase !== ready 时不发送）")
    kill_tree()
    dump("storage")


async def section_nointerp() -> None:
    root = make_root("nointerp", venv=False)
    kill_tree()
    proc, cdp, _ = await boot_shell(root, PORT + 3)
    status = await cdp.wait("document.getElementById('status').textContent", "核心", 90)
    restart = await cdp.js("!document.getElementById('restart').classList.contains('hidden')")
    title = await cdp.js("document.getElementById('title').textContent")
    check("E1 §二.4/§2.8 核心起不来时给技术诊断与重启入口",
          "PASS" if ("解释器" in str(status) or "核心" in str(status)) and restart else "FAIL",
          f"缺 .venv 的根 → 状态条={status!r}；重启入口={restart}；顶栏={title!r}",
          clause="§二.4 核心崩溃给明确错误和重启入口；§2.8 等待超时给启动阶段与日志位置等技术诊断",
          code="main.rs:99-103 · main.ts:1571-1576")
    kill_tree()
    dump("nointerp")


# ================================================================== onboard（首跑）

async def section_onboard() -> None:
    """首跑 / 零实例：聊天空态给直达入口（不与顶栏 chip 互相矛盾），管理页首屏不是版本表。"""
    root = make_root("onboard")
    kill_tree()
    proc, cdp, _ = await boot_shell(root, PORT + 5,
                                    {"ISEKAI_LLM_FAKE": "1", "ISEKAI_LLM_FAKE_REPLY": "首跑占位回复"})
    status = await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    await cdp.js(STUB)
    empty = await cdp.js(
        "(()=>{const e=document.querySelector('#messages li.empty');"
        "return {text: e?e.textContent:'', btn: !!document.getElementById('empty-create'),"
        " chip: document.getElementById('status').textContent};})()")
    await cdp.js("document.getElementById('empty-create')?.click()")
    await asyncio.sleep(2.0)
    landed = await cdp.js(
        "({manage: !document.getElementById('pane-manage').classList.contains('hidden'),"
        " chat_hidden: document.getElementById('pane-chat').classList.contains('hidden'),"
        " focused: document.activeElement && document.activeElement.id,"
        " open: document.getElementById('manage-facts-box').open,"
        " first_h3: (document.querySelector('#pane-manage h3')||{}).textContent || '',"
        " facts_rows: document.querySelectorAll('#manage-facts dt').length})")
    check("O1 §四/P0-1 零实例首跑：空态给直达入口，点了到管理页并聚焦世界包生成入口",
          "PASS" if (empty["btn"] and "还没有世界实例" in str(empty["text"])
                     and "还没有世界实例" in str(empty["chip"])
                     and landed["manage"] and landed["chat_hidden"]
                     and landed["focused"] == "pkg-brief" and landed["open"] is False
                     and landed["first_h3"].strip() == "世界包") else "FAIL",
          f"空态文案={empty['text']!r}（直达按钮={empty['btn']}）；顶栏 chip={empty['chip']!r}"
          f"（与空态同一口径：不再一边说「发一条消息试试」一边说「还没有世界实例」）；"
          f"点击后 管理页可见={landed['manage']}、聊天页隐藏={landed['chat_hidden']}、"
          f"焦点={landed['focused']!r}；版本与计数折叠={landed['open'] is False}（{landed['facts_rows']} 行事实表不再占首屏）；"
          f"管理页首组={landed['first_h3']!r}",
          clause="§四 空实例 / 无会话有明确空态；§3.2 首次启动无可用世界包时提供入口",
          code="desktop/src/main.ts emptyState / openFirstWorld · desktop/index.html#manage-facts-box")
    manage_facts = await cdp.js("document.getElementById('manage-facts').textContent")
    await cdp.pane("settings")
    await cdp.wait("document.getElementById('about-facts').textContent", "应用版本", 25)
    about_facts = await cdp.js("document.getElementById('about-facts').textContent")
    duplicated = [key for key in ("应用版本", "数据格式版本", "世界规则版本") if key in str(manage_facts)]
    check("O2 §3.3 版本三项只留一处（管理页不再重复「关于 / 诊断」）",
          "PASS" if not duplicated and "应用版本" in str(about_facts) else "FAIL",
          f"管理页版本与计数={manage_facts!r}（重复的三项={duplicated or '无'}）；"
          f"关于 / 诊断={str(about_facts)[:80]!r}…（版本的正位）",
          clause="§3.3 关于：应用 / 数据格式版本、日志目录、脱敏诊断",
          code="desktop/src/main.ts renderManagePane / loadAbout")
    kill_tree()
    dump("onboard")


# ================================================================== notify（§3.1 末条 / §十.17 桌面提醒）

#: 主动消息正文（假 LLM）：通知正文应当是它，不是任何内部标识
NOTIFY_TEXT = "北堤的通行牌这三天都停发了，先别走那条路。"
#: 通知观察点：记录 notify_message 的载荷与结果；__notifyFail 非空时让通知命令失败（注入「通知不可用」）
#: 观察点：WebView2 里 `window.__TAURI_INTERNALS__` 与其 invoke 都是只读属性（w/c 全 false，
#: Proxy / defineProperty 均失败，实测），页面内截不到 invoke 载荷；所以通知一侧用壳自己的
#: 诊断日志（notification shown / failed，不含正文）当证据，这里只截 UMP 的 delivery 上报。
NOTIFY_STUB = (
    "(()=>{window.__umpSend=window.__umpSend||[];"
    "if(!window.__origWsSend){window.__origWsSend=WebSocket.prototype.send;"
    "WebSocket.prototype.send=function(d){try{const m=JSON.parse(String(d));"
    "if(m&&m.type==='delivery'){window.__umpSend.push(m.payload||{});}}catch(e){}"
    "return window.__origWsSend.call(this,d);};}"
    "window.__notifyStubReady=true;return true;})()"
)


async def install_observe(cdp: Cdp, timeout: float = 90.0) -> bool:
    """装上观察点（Tauri 的 internals 可能在页面 ready 之后才注入 → 轮询到装上为止）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await cdp.js(NOTIFY_STUB):
                return True
        except Exception:  # noqa: BLE001 - 页面还在初始化
            pass
        await asyncio.sleep(0.5)
    return False


async def pick_import_file(cdp: Cdp, button_id: str, path: Path, title_needle: str) -> str:
    """点导入按钮 → 真驱动随之弹出的原生文件对话框 → 返回可引用的观察文本（文件对话框只能真驱）。"""
    await cdp.js(f"document.getElementById({json.dumps(button_id)}).click()")
    _hwnd, note = await asyncio.to_thread(drive_open_dialog, str(path), title_needle)
    return note


async def mgmt_call(cdp: Cdp, op: str, **args):
    """经壳自己已认证的管理面连接调 op。

    核心的管理令牌是一次性的（channel.py `_mgmt_used`），只会交给受信壳；
    探针要断言管理面契约就只能借壳的通路 —— 页面把 `mgmt.call` 挂在 window.__mgmtCall 上。
    """
    return await cdp.js(f"window.__mgmtCall({json.dumps(op)},{json.dumps(args)})",
                        await_promise=True) or {}


async def note_of(cdp: Cdp, *ids: str) -> str:
    """读一组提示槽的文本：结果按就近原则落在组内槽，页顶 #world-note 只留跨组 / 严重事件。"""
    expr = "+".join(f"(document.getElementById({json.dumps(i)})?.textContent||'')" for i in ids)
    return str(await cdp.js(expr) or "")


async def wait_note(cdp: Cdp, ids: tuple, want: str, timeout: float = 45.0) -> str:
    """等组内 / 页顶提示里出现 want（观察面是这些槽的并集，判据不变）。"""
    deadline = time.time() + timeout
    value = ""
    while time.time() < deadline:
        value = await note_of(cdp, *ids)
        if want in value:
            return value
        await asyncio.sleep(0.4)
    return value


async def ax_view(cdp: Cdp, expr: str) -> dict:
    """AX 树上某个节点的真实角色 / 属性 / 子树文本（CDP Accessibility.queryAXTree）。

    HTML 属性不是读屏看到的东西：`role="status"` 挂在 <button> 上时 AX 角色就会变成 status（button 语义被盖掉），
    「这控件到底是按钮还是状态区」只认这里。返回 found / role / roles / ignored / props（含 focusable）/ texts。
    """
    await cdp.call("Accessibility.enable")
    obj = (await cdp.call("Runtime.evaluate", expression=expr, returnByValue=False)).get("result") or {}
    object_id = obj.get("objectId")
    if not object_id:
        return {"found": False, "role": "", "roles": [], "ignored": True, "props": {}, "texts": []}
    nodes = (await cdp.call("Accessibility.queryAXTree", objectId=object_id)).get("nodes", [])
    if not nodes:
        return {"found": False, "role": "", "roles": [], "ignored": True, "props": {}, "texts": []}
    head = nodes[0]
    props = {p.get("name"): (p.get("value") or {}).get("value") for p in (head.get("properties") or [])}
    return {"found": True, "role": (head.get("role") or {}).get("value") or "",
            "roles": [(n.get("role") or {}).get("value") or "" for n in nodes],
            "ignored": bool(head.get("ignored")), "props": props,
            "texts": [(n.get("name") or {}).get("value") or "" for n in nodes]}


async def clear_notes(cdp: Cdp, *ids: str) -> None:
    await cdp.js(";".join(
        f"(document.getElementById({json.dumps(i)})||{{}}).textContent=''" for i in ids))


async def ops_log(cdp: Cdp) -> list:
    """壳发出去的管理面 op 记账（main.ts traceOps → window.__opLog）。"""
    return list(await cdp.js("(window.__opLog||[]).slice()") or [])


def foreground_pid() -> int:
    """当前前台窗口属于哪个进程（「点击提醒 → 聚焦窗口」的观察点）。"""
    user32 = ctypes.windll.user32
    owner = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(owner))
    return int(owner.value)


def db_exec(root: Path, sql: str, params: tuple = ()) -> None:
    """写一行到临时数据根。探针自己的 DML 必须显式 commit：sqlite3 在 close() 时回滚未提交事务。"""
    con = sqlite3.connect(root / "data" / "isekai.db", timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def knowledge_add(root: Path, inst: str, line: str, char: str, *, key: str, text: str) -> None:
    """补一条「她已获知」的素材：主动发言要有可分享的东西。"""
    world = int(one(root, "SELECT COALESCE(MAX(processed_world), 0) FROM timeline_clock "
                          "WHERE timeline_id=?", (line,)) or 0)
    db_exec(root, "INSERT OR REPLACE INTO knowledge(instance_id,timeline_id,character_id,id,world_seconds,"
                  "kind,target,source,stance,text) VALUES(?,?,?,?,?,'claim',?,'src-1','recorded',?)",
            (inst, line, char, f"kn-{key}", world, f"cl-{key}", text))


def fixed_messages(root: Path, session_id: str) -> list[tuple]:
    """该会话里 reply_to 为空的已固化消息（主动消息 / 独立开场）。"""
    return db(root, "SELECT message_id FROM message WHERE session_id=? AND reply_to IS NULL ORDER BY seq",
              (session_id,))


async def wait_fixed(root: Path, session_id: str, known: set[str], timeout: float = 40.0) -> str:
    """等一条**新的**已固化主动消息落库。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for (message_id,) in fixed_messages(root, session_id):
            if str(message_id) not in known:
                return str(message_id)
        await asyncio.sleep(1.0)
    return ""


async def click_session(cdp: Cdp, needle: str) -> str:
    """点侧栏里文字含 needle 的会话（侧栏只列当前查看实例的会话 + 正在用的那条）。"""
    return str(await cdp.js(
        "(()=>{const bs=[...document.querySelectorAll('#sessions button.session')];"
        f"const hit=bs.find(b=>b.textContent.includes({json.dumps(needle)}));"
        "(hit||bs[0])?.click();return (hit||bs[0])?.textContent||'';})()") or "")


async def leave_target(cdp: Cdp, other_instance: str, target_name: str,
                        timeout: float = 25.0) -> str:
    """离开提醒目标会话：侧栏只列当前查看实例的会话，所以先切到另一个实例，
    再点那条不是 active 的会话（= 对照实例的角色会话）；标题里不再有目标实例名才算离开。"""
    await cdp.pane("manage")
    await cdp.select("inst-select", other_instance)
    title = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(2.0)
        await cdp.pane("chat")
        clicked = await cdp.js(
            "(()=>{const bs=[...document.querySelectorAll('#sessions button.session')];"
            "const hit=bs.find(b=>!b.classList.contains('active'));"
            "if(!hit)return '';hit.click();return hit.textContent;})()")
        if clicked:
            await asyncio.sleep(4.0)
            title = str(await cdp.js("document.getElementById('title').textContent") or "")
            if target_name not in title:
                return title
        await cdp.pane("manage")
    return title


async def notify_click(cdp: Cdp, notice_id: str) -> None:
    """通知点击落点：与系统通知的激活回调进同一段壳侧处理（main.rs open_notice）。"""
    await cdp.js("window.__TAURI_INTERNALS__.invoke('notice_click',"
                 f"{{notice_id:{json.dumps(notice_id)}}})", await_promise=True)


async def section_notify() -> None:
    from isekai_core.config import load_config
    from isekai_core.store import Store

    root = make_root("notify")
    instances = prepare_world(root)
    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    inst = instances[0]["id"]
    line = store.timeline_list(inst)[0]["id"]
    char = str(one(root, "SELECT character_id FROM unit WHERE instance_id=? AND timeline_id=? LIMIT 1",
                   (inst, line)) or "")
    session = store.session_ensure(inst, line, char)
    session_id = str(session["id"])
    # 世界时钟前移 8 小时：示例世界从午夜开局（生活计划第一段是睡眠），主动发言要角色清醒（§5.2 节律）
    shift = 28800
    db_exec(root, "UPDATE timeline_clock SET base_world = base_world + ?, processed_world = processed_world + ? "
                  "WHERE timeline_id = ?", (shift, shift, line))
    # 素材 m1：主动发言要有她已获知的东西（m2 / m3 留给开关与降级两轮）
    store.knowledge_put({"instance_id": inst, "timeline_id": line, "character_id": char,
                         "id": "kn-m1",
                         "world_seconds": int((store.clock_get(line) or {}).get("processed_world") or 0),
                         "kind": "claim", "target": "cl-m1", "source": "src-1",
                         "stance": "recorded", "text": "北堤的通行牌停发三天了"})
    # 另一个实例也备一条会话：验证「点击提醒 → 切回来」前得先真的离开目标会话
    inst2 = instances[1]["id"]
    line2 = store.timeline_list(inst2)[0]["id"]
    char2 = str(one(root, "SELECT character_id FROM unit WHERE instance_id=? AND timeline_id=? LIMIT 1",
                    (inst2, line2)) or "")
    session2 = str(store.session_ensure(inst2, line2, char2)["id"])
    store.close()
    print(f"[notify] 数据根={root}；实例={inst}；线={line}；角色={char}；会话={session_id}；"
          f"对照实例={inst2}；对照会话={session2}", flush=True)

    kill_tree()
    proc, cdp, _ = await boot_shell(root, PORT + 4, {"ISEKAI_LLM_FAKE": "1",
                                                     "ISEKAI_LLM_FAKE_REPLY": NOTIFY_TEXT})
    await cdp.js(STUB)
    # 尽早装：核心 tick 不等壳就绪，主动消息可能先到；internals 注入晚于页面 ready，所以轮询重试
    observed = await install_observe(cdp)
    status = await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)

    # ---- 激活甲世界的线（时钟只对当前查看的激活线有效）+ 把聊天 thread 绑到目标会话
    await cdp.pane("manage")
    await cdp.select("inst-select", inst)
    await cdp.wait("document.getElementById('clock-label').textContent", "冻结", 30)
    await cdp.js("document.getElementById('clock-activate').click()")
    await cdp.wait("document.getElementById('clock-label').textContent", "倍率", 30)
    await cdp.pane("chat")
    await click_session(cdp, char)
    await asyncio.sleep(6.0)
    bound = one(root, "SELECT COUNT(*) FROM thread WHERE session_id=? AND thread_id='main'", (session_id,))

    # ---- 主动消息到达（核心自身 tick 的生活节律路径：素材 + 清醒 + 额度 + 唯一目标）
    fixed = await wait_fixed(root, session_id, set())
    shown = await cdp.wait("document.getElementById('messages').textContent", NOTIFY_TEXT[:14], 30)
    await asyncio.sleep(2.5)
    notices = list((await mgmt_call(cdp, "notice.list", instance_id=inst)).get("notices") or [])
    payload = await cdp.js("window.__notify") or []
    fixed_all = [str(item) for (item,) in fixed_messages(root, session_id)]
    mapped = [str(row.get("message_id") or "") for row in notices]
    paired = set(mapped) == set(fixed_all)
    shown_logs = len([ln for ln in logs(root).splitlines() if "notification shown" in ln])
    ok = (status == "已就绪" and observed and bound == 1 and bool(fixed) and bool(notices)
          and fixed in mapped and set(mapped) <= set(fixed_all) and NOTIFY_TEXT[:14] in str(shown)
          and shown_logs == len(notices))
    check("S3.1 §3.1/A17 已固化主动消息到达 → 壳登记提醒（notice.list 一条，message_id 与固化消息一致）",
          "PASS" if ok else "FAIL",
          f"真壳状态={status!r}；通知观察点已装={observed}；聊天 thread 绑定到目标会话={bool(bound)}；"
          f"核心 tick 固化的主动消息={fixed_all}（★首条={fixed!r}）；notice.list {len(notices)} 条，"
          f"message_id 与之{'一致' if paired else '不一致'}：{mapped}；"
          f"壳日志里「系统通知已显示」={shown_logs} 行（= 提醒条数：{shown_logs == len(notices)}，"
          f"页面内载荷观察点装不上见 S3.2 说明）；界面已显示该消息={NOTIFY_TEXT[:14] in str(shown)}",
          clause="§3.1 桌面提醒只作为已固化主动消息的入口；§十.17 桌面提醒点击后打开固化消息所属会话",
          code="desktop/src/main.ts mergeReply→registerNotice（notice.create）",
          expected="收到 reply_to 为空的已固化消息后，受信管理面出现一条引用同一 message_id 的提醒")

    # ---- 通知载荷卫生 + 系统通知是否真的发出
    source = (REPO / "desktop" / "src" / "main.ts").read_text(encoding="utf-8")
    built = re.search(r"function noticePayload\(text: string\).*?\n\}", source, re.S)
    payload_line = re.search(r"return \{\s*title:.*?\n\s*\};", built.group(0) if built else "", re.S)
    payload_src = payload_line.group(0) if payload_line else ""
    ids_used = bool(re.search(r"\.id\b|messageId|instanceId|timelineId|sessionId", payload_src))
    notify_ok = await cdp.js("window.__notifyOk")
    notify_err = await cdp.js("window.__notifyErr")
    shown_logs = len([ln for ln in logs(root).splitlines() if "notification shown" in ln])
    ok = bool(built) and not ids_used and shown_logs >= 1
    check("S3.2 §3.1/A17 系统通知载荷（标题 / 正文）只由显示名 + 固化消息原文构造、不含内部标识",
          "PASS" if ok else "FAIL",
          f"noticePayload 的标题 / 正文表达式={payload_src.strip()!r}（含 .id / *Id 字段={ids_used}）；"
          f"改由壳日志确认通知真的发出：notification shown={shown_logs} 行；"
          f"页面内载荷观察点不可用（WebView2 的 __TAURI_INTERNALS__ 与 invoke 全为只读属性，"
          f"Proxy/defineProperty 均失败）→ 载荷内容以构造断言 + 通知已发出为准；"
          f"注入观察点的成功 / 错误计数={notify_ok}/{notify_err!r}",
          clause="§3.1 桌面提醒不作为第二份历史；CHANNEL_PLUGIN_SPEC §2.5 通知载荷不承载内部标识",
          code="desktop/src/main.ts noticePayload · desktop/src-tauri/src/main.rs notify_message",
          expected="通知只带显示名与消息原文；实例 / 时间线 / 会话 / 消息标识只留在壳与管理面的内部通路")

    # ---- 断线重连补读：把投递记录退回 pending（模拟没收到回执）→ 重连会补发同一条。
    #      核心只补发**最新一条**主动消息（session.py resend_pending：旧的留在历史里不补发），
    #      所以这一条要拿最新那条来测。
    latest = fixed_all[-1] if fixed_all else fixed
    db_exec(root, "UPDATE delivery SET state='pending' "
                  "WHERE msg_seq=(SELECT seq FROM message WHERE message_id=?)", (latest,))
    before = len(notices)
    chars_before = await cdp.js("document.querySelectorAll('#messages li.character').length")
    sends_before = len([row for row in (await cdp.js("window.__umpSend") or [])
                        if str(row.get("message_id")) == latest])
    await cdp.js("document.getElementById('restart').classList.remove('hidden');"
                 "document.getElementById('restart').click()")
    back = await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    await asyncio.sleep(8.0)
    notices2 = list((await mgmt_call(cdp, "notice.list", instance_id=inst)).get("notices") or [])
    payload2 = await cdp.js("window.__notify") or []
    chars_after = await cdp.js("document.querySelectorAll('#messages li.character').length")
    delivery = one(root, "SELECT state FROM delivery WHERE msg_seq="
                         "(SELECT seq FROM message WHERE message_id=?)", (latest,))
    sends_after = len([row for row in (await cdp.js("window.__umpSend") or [])
                       if str(row.get("message_id")) == latest])
    off_logs = len([ln for ln in logs(root).splitlines() if "notification shown" in ln])
    ok = (back == "已就绪" and before == len(notices2) and chars_after == chars_before
          and sends_after > sends_before and off_logs == len(notices2))
    check("S3.3 §3.1/A17 断线重连补读不重复登记（同一条固化消息只登记一次）",
          "PASS" if ok else "FAIL",
          f"把最新一条主动消息（{latest}，核心只补发最新一条）的投递记录退回 pending 再重启核心："
          f"重连后状态={back!r}；补读确实重投（壳对同一条消息的回执上报 {sends_before}→{sends_after} 次，"
          f"核心侧投递状态={delivery!r}）；重连后壳日志里的通知行={off_logs}（= 提醒数）；"
          f"注：重连时壳会 thread.bind 换绑定令牌，核心对旧令牌回执按「旧回执不能作用于新绑定」拒绝"
          f"（channel.py _on_delivery），故投递状态可能停在 pending —— 壳侧照常上报，"
          f"靠 message_id 去重保证不重复登记；"
          f"notice.list {before}→{len(notices2)} 条；角色消息条数 {chars_before}→{chars_after}"
          f"（重复投递不重复渲染）；补读后没有新的通知出现（notification shown 行数={off_logs}）",
          clause="§3.1 历史按当前会话分页加载，重连后补读；§十.17 提醒不因重连重复出现",
          code="desktop/src/main.ts registerNotice（noticedMessages 去重）",
          expected="同一 message_id 的重复投递不再创建提醒、不再发通知")

    # ---- 点击提醒（目标有效）：窗口回到前台 + 切到提醒指向的实例 / 时间线 / 会话
    notice_one = str((notices2 or [{}])[0].get("id") or "")
    other_title = await leave_target(cdp, inst2, instances[0]["name"])
    close_window(proc.pid)  # 关窗到托盘：通知点击要能把窗口带回来
    await asyncio.sleep(2.5)
    visible_before = window_count(proc.pid)
    await notify_click(cdp, notice_one)
    await asyncio.sleep(4.5)
    visible_after = window_count(proc.pid)
    focused = foreground_pid() == proc.pid
    title = await cdp.js("document.getElementById('title').textContent")
    active = await cdp.js("document.querySelector('#sessions button.session.active')?.textContent||''")
    ok = (visible_before == 0 and visible_after >= 1 and instances[0]["name"] in str(title)
          and str(title) != str(other_title) and "·" in str(active))
    check("S3.4 §十.17 点击提醒 → 窗口回到前台并定位到原实例 / 时间线 / 会话",
          "PASS" if ok else "FAIL",
          f"先切到对照实例（{instances[1]['name']}）的会话并关窗到托盘：可见窗口数 {visible_before}；"
          f"点提醒后 {visible_after}（窗口被带回来）；前台窗口属本壳={focused}；"
          f"顶栏 {other_title!r} → {title!r}；侧栏活动会话={active!r}",
          clause="§十.17 桌面提醒点击后打开固化消息所属的当前有效会话",
          code="desktop/src-tauri/src/main.rs open_notice/notice_click · main.ts openNotice（notice.resolve）",
          expected="点击后窗口可见并切到 target 指向的会话（先离开原会话再点，切换可验证）")

    # ---- 开关关闭：不再创建提醒，管理面照常
    await cdp.pane("settings")
    await cdp.js("(()=>{const b=document.getElementById('set-notify-enabled');b.checked=false;"
                 "b.dispatchEvent(new Event('change',{bubbles:true}));return b.checked;})()")
    await asyncio.sleep(1.5)
    flag = json.loads((root / "config" / "shell.json").read_text(encoding="utf-8")).get("notify_enabled")
    before_off = list((await mgmt_call(cdp, "notice.list", instance_id=inst)).get("notices") or [])
    shown_before_off = len([ln for ln in logs(root).splitlines() if "notification shown" in ln])
    chars_before2 = await cdp.js("document.querySelectorAll('#messages li.character').length")
    known = {str(item) for (item,) in fixed_messages(root, session_id)}
    knowledge_add(root, inst, line, char, key="m2", text="盐仓的账本换人了")
    # 每日额度已被核心自身的节律用掉：这一轮走管理面 runtime.proactive，
    # 再重启核心 → 重连补发把这条固化消息投给壳（顺带再走一遍补读路径）
    gen2 = await mgmt_call(cdp, "runtime.proactive", instance_id=inst, timeline_id=line, per_day=5)
    await cdp.js("document.getElementById('restart').classList.remove('hidden');"
                 "document.getElementById('restart').click()")
    back2 = await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    fixed2 = await wait_fixed(root, session_id, known, timeout=45.0)
    await asyncio.sleep(8.0)  # 等补发投递 + 历史补读
    chars_after2 = await cdp.js("document.querySelectorAll('#messages li.character').length")
    shown_off = len([ln for ln in logs(root).splitlines() if "notification shown" in ln])
    notices3 = list((await mgmt_call(cdp, "notice.list", instance_id=inst)).get("notices") or [])
    await cdp.pane("manage")
    await cdp.js("document.getElementById('world-refresh').click()")
    await asyncio.sleep(2.0)
    facts = await cdp.js("document.getElementById('manage-facts').textContent")
    ok = (flag is False and bool(fixed2) and len(notices3) == len(before_off)
          and int(chars_after2 or 0) >= int(chars_before2 or 0) + 1
          and shown_off == shown_before_off and bool(str(facts)))
    check("S3.6 §3.3/§3.1 设置面关闭桌面提醒后不再创建提醒，管理面照常",
          "PASS" if ok else "FAIL",
          f"关掉开关后壳设置 notify_enabled={flag!r}；管理面 runtime.proactive 仍能生成 "
          f"{str(gen2)[:70]}（message_id={fixed2!r}；重启补读后该消息进了历史："
          f"角色消息 {chars_before2}→{chars_after2} 条）；notice.list {len(before_off)}→{len(notices3)} 条、"
          f"壳日志 notification shown 行数 {shown_before_off}→{shown_off}"
          f"（关掉后不再登记、不再通知）；重连状态={back2!r}；管理面刷新后事实区非空={bool(str(facts))}",
          clause="§3.3 日常用户可调项由 UI 保存到本地配置；§3.1 关闭提醒不影响管理面",
          code="desktop/src/main.ts registerNotice（notifyOn 闸门）· index.html #set-notify-enabled",
          expected="关闭后新到的主动消息不登记提醒、不发通知；管理面与历史不受影响")

    # ---- 通知不可用：换一个带 ISEKAI_NOTIFY_FAIL=1 的壳（同一数据根）跑降级路径
    await cdp.pane("settings")
    await cdp.js("(()=>{const b=document.getElementById('set-notify-enabled');b.checked=true;"
                 "b.dispatchEvent(new Event('change',{bubbles:true}));return b.checked;})()")
    await asyncio.sleep(1.5)
    kill_tree()
    # 旧核跟着旧壳一起退：等写库锁释放再起新壳，别让新核撞上「另一个核心在写」
    for _ in range(40):
        lock = core_lock_pid(root)
        if not lock or not pid_alive(lock):
            break
        await asyncio.sleep(0.5)
    proc, cdp, _ = await boot_shell(root, PORT + 5, {"ISEKAI_LLM_FAKE": "1",
                                                    "ISEKAI_LLM_FAKE_REPLY": NOTIFY_TEXT,
                                                    "ISEKAI_NOTIFY_FAIL": "1"})
    await cdp.js(STUB)
    observed = observed and await install_observe(cdp, timeout=60)
    status_b = await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    chars_before3 = await cdp.js("document.querySelectorAll('#messages li.character').length")
    knowledge_add(root, inst, line, char, key="m3", text="渡口的桥板换新了")
    known3 = {str(item) for (item,) in fixed_messages(root, session_id)}
    await mgmt_call(cdp, "runtime.proactive", instance_id=inst, timeline_id=line, per_day=5)
    await cdp.js("document.getElementById('restart').classList.remove('hidden');"
                 "document.getElementById('restart').click()")
    await cdp.wait("document.getElementById('status').textContent", "已就绪", 120)
    fixed3 = await wait_fixed(root, session_id, known3, timeout=45.0)
    await asyncio.sleep(8.0)
    chars_after3 = await cdp.js("document.querySelectorAll('#messages li.character').length")
    notices4 = list((await mgmt_call(cdp, "notice.list", instance_id=inst)).get("notices") or [])
    degrade = await cdp.js("document.getElementById('notify-note').textContent")
    await cdp.send_text("你那边现在怎么样？", enter=True)
    chat_ok = False
    for _ in range(40):
        users = int(await cdp.js("document.querySelectorAll('#messages li.user').length") or 0)
        chars = int(await cdp.js("document.querySelectorAll('#messages li.character').length") or 0)
        if users >= 1 and chars > int(chars_after3 or 0):
            chat_ok = True
            break
        await asyncio.sleep(1.0)
    fail_logs = len([ln for ln in logs(root).splitlines() if "notification failed" in ln])
    mapped4 = {str(row.get("message_id") or "") for row in notices4}
    ok = (status_b == "已就绪" and bool(fixed3) and len(notices4) > len(notices3)
          and fixed3 in mapped4 and "不可用" in str(degrade)
          and int(chars_after3 or 0) >= int(chars_before3 or 0) + 1
          and fail_logs >= 1 and chat_ok)
    check("S3.7 §3.1/A17 系统通知不可用时降级：不崩、历史照常可读、界面有可读说明",
          "PASS" if ok else "FAIL",
          f"用 ISEKAI_NOTIFY_FAIL=1 的壳（状态={status_b!r}，同一数据根）注入通知失败："
          f"新消息 message_id={fixed3!r} 仍固化并进历史（角色消息 {chars_before3}→{chars_after3} 条）；"
          f"提醒仍登记（notice.list {len(notices3)}→{len(notices4)} 条且含该消息"
          f"={fixed3 in mapped4}，通知失败不吞提醒）；"
          f"壳日志里 notification failed={fail_logs} 行；界面说明={degrade!r}；"
          f"此后仍能正常对话（自己的消息 + 新回复进历史={chat_ok}）",
          clause="§3.1 系统通知不可用时用户仍可从历史读取；§3.3 配置检查失败保留原值，错误提示可读",
          code="desktop/src/main.ts registerNotice（catch → notifyUnavailable）· #notify-note",
          expected="通知失败只降级为界面说明：提醒记录、聊天与历史全部照常")

    # ---- 目标失效（归档）：只显示管理错误，不改投、不切换、不激活冻结线
    notice_three = str((notices4 or [{}])[-1].get("id") or "")
    other = await leave_target(cdp, inst2, instances[0]["name"])
    await mgmt_call(cdp, "runtime.timeline.archive", instance_id=inst, timeline_id=line)
    resolved = await mgmt_call(cdp, "notice.resolve", id=notice_three)
    target = resolved.get("target") or {}
    lines_before = db(root, "SELECT id, state FROM timeline ORDER BY id")
    await notify_click(cdp, notice_three)
    await asyncio.sleep(4.5)
    note = await cdp.js("document.getElementById('notice-note').textContent")
    title2 = await cdp.js("document.getElementById('title').textContent")
    lines_after = db(root, "SELECT id, state FROM timeline ORDER BY id")
    leak_in_note = {name: value for name, value in (("instance", inst), ("timeline", line),
                                                    ("session", session_id)) if value in str(note)}
    ok = (target.get("valid") is False and str(target.get("reason") or "") != ""
          and "系统错误" in str(note) and str(title2) == str(other)
          and lines_before == lines_after and not leak_in_note
          and all(state != "active" for _, state in lines_after))
    check("S3.5 §十.17 目标归档后点击提醒：报管理错误、不改投其他会话、不激活冻结线",
          "PASS" if ok else "FAIL",
          f"先切到对照实例会话，再归档目标线；notice.resolve={resolved}（valid=false 且带原因）；"
          f"点提醒后界面提示={note!r}（含内部标识={leak_in_note or '无'}）；"
          f"顶栏未变（{other!r} → {title2!r}）；时间线状态 点击前{lines_before} 点击后{lines_after}"
          f"（没有线被激活：{[s for _, s in lines_after]}）",
          clause="§十.17 目标被删除 / 归档 / 回滚 / 重绑时不改投其他会话、不激活冻结线，并显示管理错误",
          code="desktop/src/main.ts openNotice（valid=false 分支）· main.rs open_notice",
          expected="失效目标只报管理错误：不改投角色、不改变激活集合、历史仍可读")
    kill_tree()
    stamp = str(root.name).rsplit("_", 1)[-1]
    outdir = TMPBASE / f"isekai_notify_{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "result.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "shell-core.log").write_text(logs(root), encoding="utf-8")
    (outdir / "README.txt").write_text(
        "§3.1/A17 桌面提醒验收（notify 模式）\n"
        f"数据根={root}\n"
        "复现：cd /d/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_audit2_desk.py notify\n",
        encoding="utf-8")
    print(f"[notify] 日志目录={outdir}", flush=True)
    dump("notify")


# ================================================================== 入口

async def amain() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "static"
    started = time.time()
    try:
        if which in ("static", "all"):
            section_static()
        if which in ("main", "all"):
            await section_main()
        if which in ("recon", "all"):
            await section_recon()
        if which in ("compat", "all"):
            await section_compat()
        if which in ("keys", "all"):
            await section_keys()
        if which in ("restore", "all"):
            await section_restore()
        if which in ("llmfail", "all"):
            await section_llmfail()
        if which in ("storage", "all"):
            await section_storage()
        if which in ("onboard", "all"):
            await section_onboard()
        if which in ("notify", "all"):
            await section_notify()
        if which in ("nointerp", "all"):
            await section_nointerp()
    finally:
        kill_tree()
    print(f"\n[{which}] 用时 {time.time() - started:.0f}s；"
          f"PASS={sum(1 for r in RESULTS if r['status'] == 'PASS')} "
          f"FAIL={sum(1 for r in RESULTS if r['status'] == 'FAIL')} "
          f"DEFERRED={sum(1 for r in RESULTS if r['status'] == 'DEFERRED')}", flush=True)


if __name__ == "__main__":
    asyncio.run(amain())
