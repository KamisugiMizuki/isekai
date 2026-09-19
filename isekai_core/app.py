"""核心进程装配：所有权检查、运行时构建、就绪握手。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .channel import CoreServer
from .config import Config
from .llm import FakeLLM, LLMClient
from .log import get_logger
from .session import SessionService
from .store import Store
from .version import APP_VERSION, DATA_FORMAT_VERSION, RULES_VERSION

log = get_logger("isekai.app")


class OwnershipError(RuntimeError):
    """同一数据目录已有活跃的核心写入者。"""


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Ownership:
    """同一数据目录只允许一个核心写入者；陈旧锁可接管（DESKTOP_SPEC §2.1）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._held = False

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            info = self._read()
            pid = info.get("pid")
            if isinstance(pid, int) and pid != os.getpid() and pid_alive(pid):
                raise OwnershipError(f"另一个核心进程正在使用该数据目录（pid={pid}）")
            log.warning("接管陈旧锁文件 pid=%s", pid)
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "started_at": time.time(), "app": APP_VERSION}),
            encoding="utf-8",
        )
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        if self._read().get("pid") == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self._held = False


@dataclass
class Runtime:
    cfg: Config
    store: Store
    llm: Any
    service: SessionService
    server: CoreServer
    ownership: Ownership


def build_llm(cfg: Config) -> Any:
    if os.environ.get("ISEKAI_LLM_FAKE") == "1":
        # 开发 / 测试开关：不联网，直接返回脚本化文本
        return FakeLLM([os.environ.get("ISEKAI_LLM_FAKE_REPLY", "（占位回复）")])
    return LLMClient(cfg.llm)


async def build_runtime(
    cfg: Config,
    *,
    state: str = "ready",
    generation: int = 1,
    ownership: Ownership | None = None,
    llm: Any = None,
) -> Runtime:
    store = Store(cfg.paths.db)
    store.ensure_schema()
    interrupted = store.interrupt_open_turns()
    if interrupted:
        log.warning("上次进程留下 %s 条未完成轮次：已标记中断，可显式重试", interrupted)
    llm = llm or build_llm(cfg)
    holder: dict[str, CoreServer] = {}

    async def deliver(channel_id: str, thread_id: str, envelope: dict[str, Any]) -> bool:
        server = holder.get("server")
        return bool(server and await server.deliver(channel_id, thread_id, envelope))

    service = SessionService(store=store, cfg=cfg, llm=llm, deliver=deliver)
    server = CoreServer(cfg=cfg, store=store, service=service, state=state, generation=generation)
    holder["server"] = server
    return Runtime(
        cfg=cfg,
        store=store,
        llm=llm,
        service=service,
        server=server,
        ownership=ownership or Ownership(cfg.paths.lock),
    )


def ready_line(runtime: Runtime) -> dict[str, Any]:
    """就绪握手（stdout 单行 JSON）：端点 + 一次性凭据，经受信启动通路交壳。"""
    return {
        "event": "ready",
        "app": APP_VERSION,
        "data_format": DATA_FORMAT_VERSION,
        "rules": RULES_VERSION,
        "state": runtime.server.state,
        "endpoint": runtime.server.endpoint,
        "bootstrap": runtime.server.bootstrap_token,
        "mgmt": runtime.server.mgmt_token,
        "pid": os.getpid(),
        "data_dir": str(runtime.cfg.paths.data),
    }


async def run_core(cfg: Config, *, print_ready: bool = True) -> None:
    ownership = Ownership(cfg.paths.lock)
    ownership.acquire()
    runtime = await build_runtime(cfg, ownership=ownership)
    try:
        await runtime.server.start()
        if print_ready:
            # 就绪握手固定 UTF-8：不经控制台代码页，壳按字节读
            line = json.dumps(ready_line(runtime), ensure_ascii=False) + "\n"
            sys.stdout.buffer.write(line.encode("utf-8"))
            sys.stdout.buffer.flush()
        log.info("core ready state=%s", runtime.server.state)
        await asyncio.Event().wait()
    finally:
        await runtime.service.shutdown()
        await runtime.server.close()
        await runtime.llm.aclose()
        runtime.store.close()
        ownership.release()
        log.info("core stopped")
