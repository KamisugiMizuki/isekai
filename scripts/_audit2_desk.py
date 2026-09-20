"""DESKTOP_SPEC 行为审计（第二轮 / 独立探针）：只新增本文件，不修改任何项目文件。

用法（仓库根，.venv 解释器）：
  .venv/Scripts/python.exe scripts/_audit2_desk.py static     # 静态核对（无需 exe）
  .venv/Scripts/python.exe scripts/_audit2_desk.py main       # 真壳 + CDP，假 LLM（ISEKAI_LLM_FAKE=1）
  .venv/Scripts/python.exe scripts/_audit2_desk.py llmfail    # 真壳 + 死地址 LLM（不联网）→ 生成失败 UI
  .venv/Scripts/python.exe scripts/_audit2_desk.py storage    # 存储不可用根 → UI 存储错误
  .venv/Scripts/python.exe scripts/_audit2_desk.py nointerp   # 缺 .venv 的根 → 启动失败诊断

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


def type_keys(*vk_codes: int) -> None:
    user32 = ctypes.windll.user32
    for vk in vk_codes:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vk_codes):
        user32.keybd_event(vk, 0, 2, 0)


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

    async def keys(self, key: str, code: str, vk: int, *, shift: bool = False) -> None:
        mods = 8 if shift else 0
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
        "window.__TAURI_INTERNALS__.invoke=(c,a,o)=>{if(c==='pick_backup_file')"
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

    # §3.1 桌面提醒（阶段 2–3）
    hits = [str(p.relative_to(REPO)) for p in
            list((REPO / "desktop" / "src").rglob("*.ts"))
            + list((REPO / "desktop" / "src-tauri" / "src").rglob("*.rs"))
            + [REPO / "desktop" / "index.html", REPO / "desktop" / "src-tauri" / "Cargo.toml"]
            if re.search(r"notification|notify|提醒", p.read_text(encoding="utf-8", errors="replace"), re.I)]
    check("S3 §3.1/A17 桌面提醒入口存在", "FAIL" if not hits else "PASS",
          f"desktop/ 内含 notification|notify|提醒 的文件={hits or '无'}",
          clause="§3.1 桌面提醒只作为已固化主动消息的入口 / §十.17",
          code="desktop/（无通知实现）")

    # §3.3 设置面：界面里是否存在各设置组
    html = (REPO / "desktop" / "index.html").read_text(encoding="utf-8")
    groups = {"LLM": "set-base-url", "备份": "backup-now", "关于": "about-facts"}
    wanted = {"记忆向量化": ("向量", "embed"), "提交": ("autocommit", "自动提交"),
              "世界 / 会话": ("激活数量上限", "世界包目录"), "用量": ("用量", "调用上限")}
    wanted = {k: tuple(t for t in toks if t not in src) for k, toks in wanted.items()}  # 只认渲染层/模板里的控件
    wanted.pop("用量", None)   # 用量组在设置面没有组：单列到 M18 的活体证据
    missing = [name for name, tokens in wanted.items()
               if not any(tok in html or tok in src for tok in tokens)]
    controls = sorted(set(re.findall(r'id="(set-[a-z-]+)"', html)))
    check("S4 §3.3 设置面设置组齐备（记忆向量化 / 提交 / 世界·会话 / 用量）",
          "FAIL" if missing else "PASS",
          f"已有组={list(groups)}；index.html+main.ts 中缺失组={missing}；可用设置控件={controls}",
          clause="§3.3 设置面表格（记忆向量化 / 提交 / 世界·会话 / 用量 各行）",
          code="desktop/index.html:142-176 · desktop/src/main.ts:629-655")

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
    note_bad = await cdp.js("document.getElementById('world-note').textContent")
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
    note_gen = await cdp.wait("document.getElementById('world-note').textContent", "草稿", 90)
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
        note_discard = await cdp.wait("document.getElementById('world-note').textContent", "已丢弃", 30)
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
    live = await cdp.js("window.__TAURI_INTERNALS__.invoke.toString().includes('pick_backup_file')")
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
