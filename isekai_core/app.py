"""核心进程装配：所有权检查、运行时构建、就绪握手。"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .channel import CoreServer
from .config import Config
from .llm import FakeLLM, LLMClient
from .log import get_logger
from .runtime.service import RuntimeService
from .session import SessionService
from .store import Store
from .version import APP_VERSION, DATA_FORMAT_VERSION, RULES_VERSION

log = get_logger("isekai.app")


class OwnershipError(RuntimeError):
    """同一数据目录已有活跃的核心写入者。"""


class PersistenceError(RuntimeError):
    """存储不可用：拒绝推进，等待恢复（DESKTOP_SPEC §2）。"""


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
            if isinstance(pid, int) and pid != os.getpid():
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
    world: Any = None  # 世界运行层（阶段 2 起）


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
    try:
        store.write_probe()  # 存储不可用时不进入正常运行（壳显示存储错误）
    except (sqlite3.Error, OSError) as exc:
        store.close()
        raise PersistenceError(str(exc)) from exc
    interrupted = store.interrupt_open_turns()
    if interrupted:
        log.warning("上次进程留下 %s 条未完成轮次：已标记中断，可显式重试", interrupted)
    llm = llm or build_llm(cfg)
    holder: dict[str, CoreServer] = {}

    async def deliver(channel_id: str, thread_id: str, envelope: dict[str, Any]) -> bool:
        server = holder.get("server")
        return bool(server and await server.deliver(channel_id, thread_id, envelope))

    service = SessionService(store=store, cfg=cfg, llm=llm, deliver=deliver)
    world = RuntimeService(
        store,
        rate_max=cfg.runtime.rate_max,
        max_active_timelines=cfg.runtime.max_active_timelines,
        catch_up_batches=cfg.runtime.catch_up_batches,
        catch_up_lag_seconds=cfg.runtime.catch_up_lag_seconds,
        render_calls_per_day=cfg.runtime.render_calls_per_day,
    )
    service.runtime = world  # 会话层经运行层构造扮演定义
    for row in store.instance_list():
        world.ensure_instance(row["id"], now_real=time.time())
    server = CoreServer(cfg=cfg, store=store, service=service, state=state, generation=generation)
    holder["server"] = server
    return Runtime(
        cfg=cfg,
        store=store,
        llm=llm,
        service=service,
        server=server,
        ownership=ownership or Ownership(cfg.paths.lock),
        world=world,
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


async def _watch_parent(parent_pid: int, stop: asyncio.Event) -> None:
    """父进程（壳）消失即自行退出：硬杀壳时不留孤儿写入者（DESKTOP_SPEC §2）。"""
    while not stop.is_set():
        await asyncio.sleep(5)
        if not pid_alive(parent_pid):
            log.warning("父进程 %s 已退出：核心随之停止", parent_pid)
            stop.set()
            return


async def run_core(cfg: Config, *, print_ready: bool = True, parent_pid: int | None = None) -> None:
    ownership = Ownership(cfg.paths.lock)
    stop = asyncio.Event()
    if parent_pid is not None:
        asyncio.create_task(_watch_parent(parent_pid, stop))

    runtime: Runtime | None = None
    while runtime is None and not stop.is_set():
        try:
            ownership.acquire()  # data 目录 / 锁文件不可写也算存储问题
            runtime = await build_runtime(cfg, ownership=ownership)
        except OwnershipError:
            raise  # 另一个核心在写：不是存储问题，按 already_running 退出
        except (PersistenceError, OSError) as exc:
            log.error("存储不可用：%s", exc)
            if print_ready:
                # 仍给就绪握手（状态=persistence_blocked）：壳据此显示存储错误并停止新工作
                blocked = {
                    "event": "ready",
                    "state": "persistence_blocked",
                    "endpoint": None,
                    "app": APP_VERSION,
                    "data_format": DATA_FORMAT_VERSION,
                    "rules": RULES_VERSION,
                    "error": str(exc),
                    "pid": os.getpid(),
                    "data_dir": str(cfg.paths.data),
                }
                sys.stdout.buffer.write((json.dumps(blocked, ensure_ascii=False) + "\n").encode("utf-8"))
                sys.stdout.buffer.flush()
                print_ready = False
            await asyncio.sleep(10)  # 有界重试：恢复写入后继续
    if runtime is None:
        ownership.release()
        return

    try:
        await runtime.server.start()
        if print_ready:
            # 就绪握手固定 UTF-8：不经控制台代码页，壳按字节读
            line = json.dumps(ready_line(runtime), ensure_ascii=False) + "\n"
            sys.stdout.buffer.write(line.encode("utf-8"))
            sys.stdout.buffer.flush()
        log.info("core ready state=%s", runtime.server.state)
        if runtime.world is not None:
            # 恢复：只对中断前激活的线补算，冻结线不补（§2.6）
            resumed = runtime.world.catch_up_all(now_real=time.time())
            if resumed:
                log.info("resumed timelines=%s", ",".join(resumed))
            ticker = asyncio.create_task(_clock_tick(runtime, stop))
        await stop.wait()
    finally:
        stop.set()
        await runtime.service.shutdown()
        await runtime.server.close()
        await runtime.llm.aclose()
        runtime.store.close()
        ownership.release()
        log.info("core stopped")


async def _clock_tick(runtime: Runtime, stop: asyncio.Event, *, interval: float = 5.0) -> None:
    """世界时钟自己走：周期性推进激活线（冻结线跳过，单线失败不影响其他线）。"""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        if runtime.world is None:
            continue
        try:
            runtime.world.catch_up_all(now_real=time.time(), max_batches=4)
        except Exception:  # 推进失败不该让核心退出
            log.exception("clock tick failed")
        # 角色自主提案：只在激活线上、按现实日预算（§11.3 / §2.8）
        try:
            for instance_id, timeline_id in runtime.world.active_timelines():
                await runtime.world.propose_intents(
                    instance_id, timeline_id, llm=runtime.llm, now_real=time.time()
                )
        except Exception:
            log.exception("intent proposal pass failed")
        # 证据充分后提取记忆：有界、按现实日预算、失败留待处理（§4.1）
        try:
            for instance_id, timeline_id in runtime.world.active_timelines():
                runtime.world.queue_world_sources(instance_id, timeline_id)
                await runtime.world.extract_memories(
                    instance_id, timeline_id, llm=runtime.llm, now_real=time.time(), limit=6
                )
        except Exception:
            log.exception("memory extraction pass failed")
