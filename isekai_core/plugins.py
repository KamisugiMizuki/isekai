"""插件宿主（CHANNEL_PLUGIN_SPEC §三）。

- **形态与安装（§3.1）**：独立进程 + 目录制 manifest；扫描 / 登记只读清单、**不运行代码**，
  用户明确启用后才启动，不经 shell 拼接执行。
- **承载（§2.5 / §3.2）**：子进程；stdin/stdout 为 UTF-8 NDJSON，stderr 为受限日志；
  管道绑定该通道身份——插件走的是和桌面 WS **同一段** `CoreServer._handler`（认证、去重、回执、
  错误、限额全同），只是传输换成 stdio。
- **生命周期（§3.2）**：启用（握手超时）→ 停用（有界退出 → 必要时结束进程树）→ 崩溃标记
  （不自动重启风暴）→ 手工更新（不删持久通道身份）→ 卸载（保留核心会话与角色历史）→ 核心退出全停。
- **安全（§3.4）**：默认只传必要环境变量 + 该插件自己的配置；不继承 LLM Key / 管理令牌 / 其他插件凭据。
  这不是沙箱（以当前用户权限运行），首次启用前由调用方明确提示。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from .channel import CoreServer
from .version import UMP_VERSION
from .log import get_logger

log = get_logger("isekai.plugins")

MANIFEST_FIELDS = ("id", "name", "version", "ump", "entry")
MANIFEST_NAME = "manifest.json"
#: 子进程只拿这些环境变量（§3.4）：降低误泄漏，不替代沙箱
ENV_ALLOWLIST = ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "LANG", "PYTHONUTF8", "PYTHONIOENCODING")
STDERR_KEEP_LINES = 200   # stderr 保留的最近行数（容量上限）
STDERR_LINE_CHARS = 500   # 单行截断
STDOUT_QUEUE_MAX = 200    # 出站帧排队上限：堵住输出不能拖死核心
HANDSHAKE_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 5.0


def scan(folder: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """扫描目录里的 manifest（§3.1）：只读清单，不运行代码，坏清单如实报错。"""
    root = Path(folder)
    if not root.is_dir():
        return []
    # 目录制（§3.1）：每个插件一个目录，清单在目录里；也认「目录本身就是插件」的平铺放法
    paths: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / MANIFEST_NAME).is_file():
            paths.append(child / MANIFEST_NAME)
        elif child.is_file() and child.suffix == ".json":
            paths.append(child)
    found: list[dict[str, Any]] = []
    for path in paths:
        item: dict[str, Any] = {
            "id": "",
            "name": "",
            "version": "",
            "ump": "",
            "entry": [],
            "directory": str(path.parent),
            "manifest": path.name,
            "errors": [],
        }
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            item["errors"].append(f"清单不是合法 JSON：{exc}")
            found.append(item)
            continue
        if not isinstance(raw, dict):
            item["errors"].append("清单顶层必须是对象")
            found.append(item)
            continue
        for field in MANIFEST_FIELDS:
            if raw.get(field) in (None, "", []):
                item["errors"].append(f"清单缺少 {field}")
        entry = raw.get("entry")
        if entry is not None:
            if not isinstance(entry, list) or not entry or not all(
                isinstance(part, str) and part for part in entry
            ):
                item["errors"].append("entry 必须是参数数组（字符串列表），不是 shell 串")
            else:
                item["entry"] = [str(part) for part in entry]
        for field in ("id", "name", "version", "ump"):
            if raw.get(field) not in (None, ""):
                item[field] = str(raw[field])
        # 启用前检查：入口可执行文件 / 脚本在工作目录里存在（不预判解释器是否存在，那是启用期的事）
        program = str(Path(item["entry"][0]).name) if item["entry"] else ""
        if program and program not in ("python", "python3", "py") and not (path.parent / program).exists():
            item["errors"].append(f"入口在工作目录里找不到：{program}")
        if item["entry"] and (path.parent / program).exists() and not os.access(path.parent / program, os.X_OK):
            item["errors"].append(f"入口没有执行权限：{program}")
        found.append(item)
    return found


class StdioWS:
    """子进程通道的「ws」替身：`send` 写 NDJSON 到 stdin；异步迭代读 stdout 行。"""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.closed = False
        self.close_code = 0
        self.send_lock = asyncio.Lock()
        self._stdout = proc.stdout
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=STDOUT_QUEUE_MAX)
        self._pump: asyncio.Task[Any] | None = asyncio.ensure_future(self._read_stdout())

    async def _read_stdout(self) -> None:
        assert self._stdout is not None
        try:
            while True:
                line = await self._stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    self._queue.put_nowait(text)
                except asyncio.QueueFull:  # 堵住的插件不拖死核心：丢帧并记账
                    log.warning("plugin stdout queue full, dropping frame")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读端异常只记账，不冒泡
            log.warning("plugin stdout read failed: %s", exc)
        finally:
            self._queue.put_nowait("")  # 唤醒迭代

    async def send(self, data: str) -> None:
        proc = self.proc
        if self.closed or proc.stdin is None or proc.returncode is not None:
            raise RuntimeError("plugin process is not running")
        async with self.send_lock:
            proc.stdin.write((str(data) + "\n").encode("utf-8"))
            try:
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RuntimeError("plugin stdin closed") from exc

    async def recv(self) -> str:
        item = await self._queue.get()
        if item == "":
            raise RuntimeError("plugin stdout closed")
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed, self.close_code = True, int(code)
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        try:
            if self.proc.stdin is not None and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
        except Exception:  # noqa: BLE001 - 关闭期异常无需上报
            pass

    def __aiter__(self) -> "StdioWS":
        return self

    async def __anext__(self) -> str:
        getter = asyncio.ensure_future(self._queue.get())
        try:
            item = await asyncio.wait_for(getter, timeout=1.0)
        except asyncio.TimeoutError:
            if self.proc.returncode is not None or self.closed:
                raise StopAsyncIteration from None
            getter.cancel()
            return await self.__anext__()
        if item == "":
            raise StopAsyncIteration
        return item


class PluginHost:
    """插件登记与运行（一个核心一份）。"""

    def __init__(self, *, cfg: Any, store: Any, server: CoreServer, folder: str | os.PathLike[str] | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.server = server
        self.folder = Path(folder) if folder is not None else Path(getattr(cfg.paths, "packages", ".")).parent / "plugins"
        self._running: dict[str, dict[str, Any]] = {}
        self._stderr: dict[str, list[str]] = {}

    # ---------- 登记 ----------

    def list_plugins(self) -> list[dict[str, Any]]:
        """登记表 + 扫描结果：已登记的以登记表为准（含 enabled / state）。"""
        registered = {str(row["id"]): row for row in self.store.plugin_list()}
        out: list[dict[str, Any]] = []
        for item in scan(self.folder):
            known = registered.get(str(item["id"])) if item["id"] else None
            out.append(
                {
                    **item,
                    "enabled": bool(known and known.get("enabled")),
                    "state": str(known.get("state") if known else "registered" if item["id"] else "invalid"),
                    "note": str(known.get("note") if known else ""),
                }
            )
        for ident, row in registered.items():
            if not any(item["id"] == ident for item in out):
                out.append({**row, "directory": str(row.get("path") or ""), "errors": ["清单已移走"], "entry": []})
        return out

    def _plugin_folder(self, plugin_id: str) -> Path:
        for item in scan(self.folder):
            if str(item["id"]) == str(plugin_id):
                return Path(str(item["directory"]))
        return self.folder

    def _env_for(self, plugin_id: str) -> dict[str, str]:
        """最小环境（§3.4）：只给必要变量与该插件自己的配置，不继承核心凭据。"""
        env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
        env["PYTHONUNBUFFERED"] = "1"
        env["ISEKAI_PLUGIN_ID"] = str(plugin_id)
        return env

    # ---------- 生命周期 ----------

    async def enable(self, plugin_id: str, *, timeout: float = HANDSHAKE_TIMEOUT_S) -> dict[str, Any]:
        """启用：启动 → 有限时间内完成认证握手 → 运行中（§3.2）。失败明确提示，不「试试看」。"""
        item = next((row for row in scan(self.folder) if str(row["id"]) == str(plugin_id)), None)
        if item is None:
            raise ValueError(f"没有这个插件：{plugin_id}")
        if item["errors"]:
            self.store.plugin_put({"id": plugin_id, "path": item["directory"], "enabled": 0,
                                   "state": "invalid", "note": "；".join(item["errors"]), "name": item["name"],
                                   "version": item["version"], "updated_at": time.time()})
            return {"enabled": False, "state": "invalid", "errors": item["errors"]}
        if plugin_id in self._running:
            return {"enabled": True, "state": "running", "note": "已在运行"}
        folder = Path(str(item["directory"]))
        # 认证材料由核心签发，只经受信通路交这一个插件（§2.5）：换的是它自己那条通道身份的凭据
        try:
            _row, credential = self.store.channel_register(
                name=str(plugin_id),
                display_name=str(item["name"] or plugin_id),
                version=str(item["version"] or "0"),
                protocol=UMP_VERSION,
                capabilities={},
                rotate=True,  # 每次启用换新凭据：旧凭据不能拿回来复用
            )
        except Exception as exc:  # noqa: BLE001 - 通道登记失败就是启用失败，明确报出来
            note = f"通道登记失败：{exc}"
            self.store.plugin_put({"id": plugin_id, "path": item["directory"], "enabled": 0, "state": "failed",
                                   "name": item["name"], "version": item["version"], "note": note,
                                   "updated_at": time.time()})
            return {"enabled": False, "state": "failed", "note": note}
        env = self._env_for(plugin_id)
        env["ISEKAI_PLUGIN_CREDENTIAL"] = str(credential or "")
        proc = await asyncio.create_subprocess_exec(
            *[str(part) for part in item["entry"]],
            cwd=str(folder),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        ws = StdioWS(proc)
        task = asyncio.ensure_future(self.server._handler(ws))  # noqa: SLF001 - 同一段握手 / 分发路径
        stderr_task = asyncio.ensure_future(self._pump_stderr(plugin_id, proc))
        self._running[plugin_id] = {"proc": proc, "ws": ws, "task": task, "stderr": stderr_task}
        self.store.plugin_put({"id": plugin_id, "path": item["directory"], "enabled": 1, "state": "starting",
                               "name": item["name"], "version": item["version"], "note": "",
                               "updated_at": time.time()})
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if proc.returncode is not None:
                note = f"进程启动后立刻退出（code={proc.returncode}）" + self._stderr_tail(plugin_id)
                self._forget(plugin_id, state="failed", note=note)
                return {"enabled": False, "state": "failed", "note": note}
            row = self.store.channel_by_name(str(plugin_id))  # 通道按名字查：id 是核心签发的 ci-…
            if row is not None and str(row.get("capabilities") or "{}") not in ("", "{}"):
                self.store.plugin_put({"id": plugin_id, "path": item["directory"], "enabled": 1, "state": "running",
                                       "name": item["name"], "version": item["version"], "note": "",
                                       "updated_at": time.time()})
                return {"enabled": True, "state": "running", "channel": str(plugin_id)}
        note = f"握手超时（{timeout:.0f}s）" + self._stderr_tail(plugin_id)
        await self.disable(plugin_id, note=note)
        return {"enabled": False, "state": "failed", "note": note}

    async def disable(self, plugin_id: str, *, note: str = "") -> dict[str, Any]:
        """停用：停接新消息 → 关连接 → 有界等待退出 → 必要时结束进程树（§3.2）。"""
        entry = self._running.get(str(plugin_id))
        if entry is None:
            row = self.store.plugin_get(str(plugin_id)) or {}
            self.store.plugin_put({**row, "id": plugin_id, "enabled": 0, "state": "stopped",
                                   "note": note or str(row.get("note") or ""), "updated_at": time.time()})
            return {"enabled": False, "state": "stopped"}
        proc: asyncio.subprocess.Process = entry["proc"]
        await entry["ws"].close(code=1001, reason="disabled")
        try:
            await asyncio.wait_for(proc.wait(), timeout=STOP_TIMEOUT_S)
            stopped = "clean"
        except asyncio.TimeoutError:
            await self._kill_tree(proc)
            stopped = "killed"
        if entry.get("stderr") is not None:
            with_suppress = entry["stderr"]
            with_suppress.cancel()
        if entry.get("task") is not None:
            entry["task"].cancel()
        self._running.pop(str(plugin_id), None)
        tail = self._stderr_tail(str(plugin_id))
        row = self.store.plugin_get(str(plugin_id)) or {}
        self.store.plugin_put({**row, "id": str(plugin_id), "enabled": 0, "state": "stopped",
                               "note": (note or f"已停用（{stopped}）") + tail,
                               "updated_at": time.time()})
        return {"enabled": False, "state": "stopped", "how": stopped, "stderr": self._stderr.get(str(plugin_id), [])[-3:]}

    async def uninstall(self, plugin_id: str) -> dict[str, Any]:
        """卸载：停用并移除登记 / 绑定；**保留核心会话与角色历史**（§3.2）。"""
        await self.disable(plugin_id, note="卸载")
        self.store.plugin_forget(str(plugin_id))
        dropped = self.store.channel_forget(str(plugin_id))  # 登记与绑定一起移除（§3.2）
        return {
            "uninstalled": str(plugin_id),
            "dropped": dropped,
            "kept": "核心会话 / 消息 / 角色历史不动",
        }

    async def stop_all(self) -> int:
        """核心退出：所有插件及所属子进程一并停止，不留后台孤儿（§3.2）。"""
        stopped = 0
        for plugin_id in list(self._running):
            await self.disable(plugin_id, note="核心退出")
            stopped += 1
        return stopped

    # ---------- 内部 ----------

    async def _kill_tree(self, proc: asyncio.subprocess.Process) -> None:
        """结束所属进程树：插件自己拉起的子进程也不留（§3.2）。"""
        if proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            log.warning("kill tree failed pid=%s: %s", proc.pid, exc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("plugin process did not exit pid=%s", proc.pid)

    async def _pump_stderr(self, plugin_id: str, proc: asyncio.subprocess.Process) -> None:
        """stderr 受限日志：有容量上限、逐行截断，不把核心日志刷爆（§3.2 / §六）。"""
        keep: list[str] = self._stderr.setdefault(str(plugin_id), [])
        assert proc.stderr is not None
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                keep.append(text[:STDERR_LINE_CHARS])
                del keep[: max(0, len(keep) - STDERR_KEEP_LINES)]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("plugin stderr pump failed: %s", exc)

    def _stderr_tail(self, plugin_id: str, lines: int = 2) -> str:
        kept = self._stderr.get(str(plugin_id)) or []
        if not kept:
            return ""
        return "；stderr：" + " / ".join(kept[-lines:])

    def _forget(self, plugin_id: str, *, state: str, note: str) -> None:
        entry = self._running.pop(str(plugin_id), None)
        if entry is not None:
            if entry.get("stderr") is not None:
                entry["stderr"].cancel()
            if entry.get("task") is not None:
                entry["task"].cancel()
        row = self.store.plugin_get(str(plugin_id)) or {}
        self.store.plugin_put({**row, "id": str(plugin_id), "enabled": 0, "state": state, "note": note,
                               "updated_at": time.time()})


HOST: PluginHost | None = None


def install(host: PluginHost | None) -> None:
    """核心启动时挂上宿主（管理面 op 从这里取）。"""
    global HOST
    HOST = host
