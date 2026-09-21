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
from . import plugins as plugins_mod
from .log import get_logger
from .runtime.service import RuntimeService
from .world import ops as world_ops
from .session import SessionService
from .runtime.service import from_config as runtime_service_from_config
from .store import Store
from .world import instances
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
    world = runtime_service_from_config(cfg, store)
    service.runtime = world  # 会话层经运行层构造扮演定义
    for row in store.instance_list():
        world.ensure_instance(row["id"], now_real=time.time())
    server = CoreServer(cfg=cfg, store=store, service=service, state=state, generation=generation)
    holder["server"] = server
    # 插件宿主（CHANNEL_PLUGIN_SPEC §三）：登记表在库里，进程按需起停；没挂上时相关 op 明确拒绝
    plugins_mod.install(plugins_mod.PluginHost(cfg=cfg, store=store, server=server))
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
        # 启动后补做到期检查（§五）：先把「该备份的」补上，再对外就绪
        try:
            _backup_if_due(cfg, runtime.store, note="启动后补做")
        except Exception:
            log.exception("startup backup failed")
        blocked_ids = [
            str(row["id"])
            for row in runtime.store.instance_list()
            if runtime.world is not None and str(instances.compatibility(row)[0]) == "blocked"
        ]
        if blocked_ids:
            # 兼容性阻断没有「部分可用」的说法：核心整体进入只读状态，壳据此给恢复入口（§7.6 / DESKTOP_SPEC）
            runtime.server.state = "compatibility_blocked"
            log.error("compatibility blocked instances=%s", blocked_ids)
        if print_ready:
            # 就绪握手固定 UTF-8：不经控制台代码页，壳按字节读
            line = json.dumps(ready_line(runtime), ensure_ascii=False) + "\n"
            sys.stdout.buffer.write(line.encode("utf-8"))
            sys.stdout.buffer.flush()
        log.info("core ready state=%s", runtime.server.state)
        resumed_plugins = 0
        if plugins_mod.HOST is not None:
            for row in runtime.store.plugin_list():
                if int(row.get("enabled") or 0):
                    result = await plugins_mod.HOST.enable(str(row["id"]))
                    resumed_plugins += 1 if result.get("enabled") else 0
            if resumed_plugins:
                log.info("plugins resumed=%s", resumed_plugins)
        if runtime.world is not None:
            # 恢复：只对中断前激活的线补算，冻结线不补（§2.6）
            resumed = runtime.world.catch_up_all(now_real=time.time())
            if resumed:
                log.info("resumed timelines=%s", ",".join(resumed))
            ticker = asyncio.create_task(_clock_tick(runtime, stop))
        await stop.wait()
    finally:
        stop.set()
        if plugins_mod.HOST is not None:
            try:
                stopped_plugins = await plugins_mod.HOST.stop_all()
                if stopped_plugins:
                    log.info("plugins stopped=%s", stopped_plugins)
            except Exception:  # noqa: BLE001 - 退出期异常不挡收尾
                log.exception("stopping plugins failed")
        world_runtime = getattr(runtime, "world", None)
        campaign = getattr(world_runtime, "campaign", None)
        if campaign is not None:
            try:
                closed = await campaign.close_rule_sessions()
                if closed:
                    log.info("rule sessions stopped=%s", closed)
            except Exception:  # noqa: BLE001 - 退出期异常不挡收尾
                log.exception("stopping rule sessions failed")
        await runtime.service.shutdown()
        await runtime.server.close()
        await runtime.llm.aclose()
        runtime.store.close()
        ownership.release()
        log.info("core stopped")


def _backup_due(cfg: Any, store: Any) -> bool:
    """备份到期判断（DESKTOP_SPEC §五）：没有备份或距上次成功超过 interval_hours；0 = 只在退出前补做。"""
    backup_cfg = getattr(cfg, "backup", None)
    hours = int(getattr(backup_cfg, "interval_hours", 24) or 0)
    if hours <= 0:
        return False
    folder = Path(store.path).parent / str(getattr(backup_cfg, "dir", "backups") or "backups")
    latest = max((item.stat().st_mtime for item in folder.glob("isekai-*.db")), default=0.0)
    return (time.time() - latest) >= hours * 3600


def _backup_if_due(cfg: Any, store: Any, *, note: str) -> None:
    if not _backup_due(cfg, store):
        return
    result = world_ops.backup_once(cfg, store, note=note)
    log.info("backup due-check %s: ok=%s", note, result.get("ok"))


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
        # 兼容性阻断的实例不进这一轮的推进与派生任务（§7.6：blocked 只读，等用户确认转换）
        active = [
            pair for pair in runtime.world.active_timelines() if runtime.world.compatible(pair[0])
        ]
        try:
            runtime.world.catch_up_all(now_real=time.time(), max_batches=4)
        except Exception:  # 推进失败不该让核心退出
            log.exception("clock tick failed")
        # 角色自主提案：只在激活线上、按现实日预算（§11.3 / §2.8）
        try:
            for instance_id, timeline_id in active:
                await runtime.world.propose_intents(
                    instance_id, timeline_id, llm=runtime.llm, now_real=time.time()
                )
        except Exception:
            log.exception("intent proposal pass failed")
        # 自动提交（§5.1）：现实间隔或新增事件数到阈值，可配置可关
        try:
            for instance_id, timeline_id in active:
                runtime.world.maybe_auto_commit(instance_id, timeline_id, now_real=time.time())
        except Exception:
            log.exception("auto commit pass failed")
        # 证据充分后提取记忆：有界、按现实日预算、失败留待处理（§4.1）
        try:
            for instance_id, timeline_id in active:
                runtime.world.queue_world_sources(instance_id, timeline_id)
                await runtime.world.extract_memories(
                    instance_id, timeline_id, llm=runtime.llm, now_real=time.time(), limit=6
                )
                # 积压汇总：世界时间跑得比现实预算快，没有这一步积压只会越长越大（§4.1）
                await runtime.world.compact_backlog(
                    instance_id, timeline_id, llm=runtime.llm, now_real=time.time(), batch=40, limit=1
                )
                await runtime.world.embed_memories(instance_id, timeline_id, now_real=time.time(), limit=8)
        except Exception:
            log.exception("memory extraction pass failed")
        # 备份到期检查（§五）：运行期间定时检查，失败只记日志，不挡推进
        try:
            _backup_if_due(runtime.cfg, runtime.store, note="运行期到期检查")
        except Exception:
            log.exception("backup due-check failed")
        # 世界源主动发言（§5.2 / §5.3）：按节律与配额从她已获知的素材里挑一条固化，并当场投给唯一目标
        try:
            for instance_id, timeline_id in active:
                spoken = await runtime.world.proactive_tick(
                    instance_id,
                    timeline_id,
                    llm=runtime.llm,
                    max_text_len=int(runtime.cfg.max_text_len),
                    max_parts=int(runtime.cfg.max_parts),
                )
                for item in spoken.get("messages") or []:
                    row = runtime.store.outbound_by_message_id(str(item.get("message_id") or ""))
                    if row is not None:
                        await runtime.service.flush_proactive(str(row["session_id"]))
        except Exception:
            log.exception("proactive pass failed")
