"""通道宿主：本地回环 WebSocket + UMP 会话 + 受信管理面。

- 首个信封决定连接角色：`hello` = 通道客户端（UMP），管理认证帧 = 壳的管理连接。
- 认证材料不写 URL、不进日志：引导凭据一次性，之后改用核心签发的持久凭据。
- 管理面只对持管理凭据的连接开放；通道不能靠自报名称取得管理权限。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import Server, serve
from websockets.exceptions import ConnectionClosed

from . import ump
from .config import Config, SettingsError, mask_api_key, save_llm_settings, SETTABLE_SECTIONS, save_section_settings
from .log import get_logger
from .session import SessionService
from .store import Store
from .runtime.service import RuntimeStateError
from .ump import Envelope, Err, Stage, UmpError
from .world import ops as world_ops
from .version import (
    APP_VERSION,
    DATA_FORMAT_VERSION,
    HANDSHAKE_TIMEOUT_S,
    MAX_FRAME_BYTES,
    PROTOCOL_ERROR_LIMIT,
    RULES_VERSION,
    UMP_VERSION,
)

log = get_logger("isekai.channel")


@dataclass
class _Conn:
    ws: Any
    role: str
    channel_id: str | None = None
    name: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    generation: int = 0
    errors: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: 限速窗口（固定窗口计数；§3.2）
    window_start: float = 0.0
    window_count: int = 0


def negotiate(client_caps: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """能力交集与限额：取双方支持范围，均为有效正值。"""
    return {
        "segments": bool(client_caps.get("segments", False)),
        "status": bool(client_caps.get("status", False)),
        "max_text_len": min(int(client_caps.get("max_text_len", cfg.max_text_len)), cfg.max_text_len),
        "max_parts": min(int(client_caps.get("max_parts", cfg.max_parts)), cfg.max_parts),
    }


class CoreServer:
    def __init__(
        self,
        *,
        cfg: Config,
        store: Store,
        service: SessionService,
        state: str = "ready",
        bootstrap_token: str | None = None,
        mgmt_token: str | None = None,
        generation: int = 1,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.service = service
        self.state = state
        self.generation = generation
        self.bootstrap_token = bootstrap_token or f"bs-{secrets.token_urlsafe(24)}"
        self.mgmt_token = mgmt_token or f"mg-{secrets.token_urlsafe(24)}"
        self._bootstrap_used = False
        self._mgmt_used = False
        self._server: Server | None = None
        self._conns: dict[str, _Conn] = {}
        self.endpoint: str | None = None

    # ---------- 生命周期 ----------

    async def start(self) -> str:
        self._server = await serve(
            self._handler,
            self.cfg.host,
            self.cfg.port,
            max_size=MAX_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
        )
        sockets = getattr(self._server, "sockets", None) or []
        if sockets:
            port = sockets[0].getsockname()[1]
        else:  # 理论上不会走到；保底用配置值
            port = self.cfg.port
        self.endpoint = f"ws://{self.cfg.host}:{port}"
        log.info("core listening on %s state=%s", self.endpoint, self.state)
        return self.endpoint

    async def close(self) -> None:
        for conn in list(self._conns.values()):
            try:
                await conn.ws.close(code=1001, reason="core shutdown")
            except Exception:  # noqa: BLE001 - 关闭期异常无需上报
                pass
        self._conns.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ---------- 发送 ----------

    async def _send_conn(self, conn: _Conn, envelope: dict[str, Any]) -> bool:
        async with conn.send_lock:
            try:
                await conn.ws.send(json.dumps(envelope, ensure_ascii=False))
                return True
            except (ConnectionClosed, RuntimeError):
                return False

    async def deliver(self, channel_id: str, thread_id: str, envelope: dict[str, Any]) -> bool:
        """会话核心的投递出口。目标离线时返回 False，消息保留未投递状态。"""
        conn = self._conns.get(channel_id)
        if conn is None:
            return False
        if envelope.get("type") == "status" and not conn.capabilities.get("status"):
            return True  # 未协商该能力的通道不接收操作状态
        return await self._send_conn(conn, envelope)

    # ---------- 连接处理 ----------

    async def _handler(self, ws: Any) -> None:
        conn = _Conn(ws=ws, role="")
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_TIMEOUT_S)
        except (asyncio.TimeoutError, ConnectionClosed):
            await _safe_close(ws, 1008, "handshake timeout")
            return

        mgmt: dict[str, Any] | None = None
        try:
            envelope = ump.parse(first, direction="c2s", max_text_len=self.cfg.max_text_len)
        except UmpError as first_error:
            mgmt = _parse_mgmt(first)
            if mgmt is None:
                await self._send_raw(ws, ump.error_envelope(first_error))
                await _safe_close(ws, 1008, "expected hello")
                return
        if mgmt is not None:
            # 管理帧不是 UMP 信封：在 except 之外进循环，免得后续异常被挂上无关的 __context__（日志里看着像协议故障）
            await self._mgmt_loop(ws, mgmt)
            return

        if envelope.type != "hello":
            await self._send_raw(
                ws, ump.error_envelope(UmpError(Err.PROTOCOL, "首帧必须是 hello", close=True))
            )
            await _safe_close(ws, 1008, "expected hello")
            return

        if not await self._channel_handshake(conn, envelope):
            return
        await self._channel_loop(conn)

    async def _send_raw(self, ws: Any, envelope: dict[str, Any]) -> None:
        try:
            await ws.send(json.dumps(envelope, ensure_ascii=False))
        except (ConnectionClosed, RuntimeError):
            pass

    async def _channel_handshake(self, conn: _Conn, envelope: Envelope) -> bool:
        try:
            hello = ump.parse_hello(envelope.payload)
            channel_row, credential = self._authorize(hello)
            # 在线连接数上限（§3.2）：只拒新连接，不动已在线的；重连（同通道标识）不算新增
            if channel_row["id"] not in self._conns and len(self._conns) >= int(self.cfg.max_connections):
                await self._send_raw(
                    conn.ws,
                    ump.error_envelope(
                        UmpError(
                            Err.OVERLOADED,
                            f"在线连接数已达上限（{int(self.cfg.max_connections)}），稍后重试",
                            retryable=True,
                            stage=Stage.RECEIVE,
                        )
                    ),
                )
                await _safe_close(conn.ws, 1013, "connection limit")
                log.warning("connection refused: limit=%s", self.cfg.max_connections)
                return False
            caps = negotiate(hello["capabilities"], self.cfg)
            self.store.channel_set_handshake(channel_row["id"], UMP_VERSION, caps)
        except UmpError as exc:
            await self._send_raw(conn.ws, ump.error_envelope(exc))
            await _safe_close(conn.ws, 1008, exc.code)
            return False

        conn.role = "channel"
        conn.channel_id = channel_row["id"]
        conn.name = hello["channel"]["id"]
        conn.capabilities = caps
        conn.generation = self.generation
        self._conns[conn.channel_id] = conn

        payload: dict[str, Any] = {
            "channel_instance": channel_row["id"],
            "name": hello["channel"]["name"],
            "negotiated": caps,
            "protocol": UMP_VERSION,
            "state": self.state,
            # 握手回带该通道已有的 thread 令牌：重连不必再问管理面（§2.2）
            "threads": [
                {
                    "id": row["thread_id"],
                    "binding_version": row["binding_version"],
                    "binding_token": row["binding_token"],
                }
                for row in self.store.thread_list(channel_row["id"])
            ],
        }
        if credential is not None:
            payload["credential"] = credential
        await self._send_conn(
            conn, ump.make("hello_ack", payload, id=ump.new_id("s"))
        )
        log.info("channel connected id=%s name=%s", channel_row["id"], hello["channel"]["id"])

        for thread in self.store.thread_list(channel_row["id"]):
            await self.service.resend_pending(channel_row["id"], thread["thread_id"])
        return True

    def _authorize(self, hello: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        name = hello["channel"]["id"]
        if hello["bootstrap"] is not None:
            if self._bootstrap_used or not hmac.compare_digest(hello["bootstrap"], self.bootstrap_token):
                raise UmpError(
                    Err.AUTH_FAILED, "引导凭据无效或已使用", retryable=False, close=True, stage=Stage.AUTH
                )
            self._bootstrap_used = True
            row, credential = self.store.channel_register(
                name=name,
                display_name=hello["channel"]["name"],
                version=hello["channel"]["version"],
                protocol=UMP_VERSION,
                capabilities=hello["capabilities"],
            )
            return row, credential
        row = self.store.channel_verify_credential(name, hello["credential"] or "")
        if row is None:
            raise UmpError(Err.AUTH_FAILED, "通道凭据无效", retryable=False, close=True, stage=Stage.AUTH)
        self.store.channel_touch(row["id"])
        return row, None

    async def _channel_loop(self, conn: _Conn) -> None:
        try:
            async for raw in conn.ws:
                try:
                    envelope = ump.parse(
                        raw,
                        direction="c2s",
                        max_text_len=int(conn.capabilities.get("max_text_len") or self.cfg.max_text_len),
                    )
                except UmpError as exc:
                    conn.errors += 1
                    await self._send_conn(conn, ump.error_envelope(exc))
                    if conn.errors >= PROTOCOL_ERROR_LIMIT:
                        await _safe_close(conn.ws, 1008, "too many protocol errors")
                        return
                    continue
                if not await self._rate_ok(conn, envelope):
                    continue
                await self._dispatch(conn, envelope)
        except ConnectionClosed:
            pass
        finally:
            if conn.channel_id and self._conns.get(conn.channel_id) is conn:
                del self._conns[conn.channel_id]

    async def _rate_ok(self, conn: _Conn, envelope: Envelope) -> bool:
        """每连接固定窗口限速（§3.2）：超限回 `rate_limited`（可重试）并丢掉这一帧的处理权，
        持续超限（> 2 倍）断开这条连接——违规只影响它自己，不牵动核心与其他通道。

        ponytail: 固定窗口计数，不是令牌桶；够用在这种「客户端不该压测核心」的闸上。
        """
        now = time.monotonic()
        if now - conn.window_start >= float(self.cfg.rate_limit_window_s):
            conn.window_start, conn.window_count = now, 0
        conn.window_count += 1
        limit = int(self.cfg.rate_limit_msgs)
        if conn.window_count <= limit:
            return True
        closing = conn.window_count > limit * 2
        await self._send_conn(
            conn,
            ump.error_envelope(
                UmpError(
                    Err.RATE_LIMITED,
                    "消息速率超限，稍后重试" if not closing else "消息速率持续超限，连接即将断开",
                    retryable=True,
                    stage=Stage.RECEIVE,
                ),
                thread_id=envelope.thread_id,
                ref=envelope.id,
            ),
        )
        if closing:
            log.warning("connection closed by rate limit: channel=%s", conn.channel_id)
            await _safe_close(conn.ws, 1008, "rate limit")
        return False

    async def _dispatch(self, conn: _Conn, envelope: Envelope) -> None:
        try:
            if envelope.type == "ping":
                await self._send_conn(
                    conn,
                    ump.make("pong", {}, thread_id=envelope.thread_id, id=ump.new_id("s")),
                )
                return
            if envelope.type == "pong":
                return
            if self.state != "ready" and envelope.type != "delivery":
                raise UmpError(
                    Err.STATE_BLOCKED,
                    f"核心状态 {self.state}，暂不接受新消息",
                    retryable=True,
                    stage=Stage.RECEIVE,
                )
            if envelope.type == "user_message":
                await self._on_user_message(conn, envelope)
            elif envelope.type == "retry":
                await self._on_retry(conn, envelope)
            elif envelope.type == "delivery":
                self._on_delivery(conn, envelope)
            else:
                raise UmpError(Err.PROTOCOL, f"核心不接受 {envelope.type} 类型", retryable=False)
        except UmpError as exc:
            await self._send_conn(
                conn,
                ump.error_envelope(exc, thread_id=envelope.thread_id, ref=envelope.id),
            )

    async def _on_user_message(self, conn: _Conn, envelope: Envelope) -> None:
        thread_id = envelope.thread_id or ""
        thread = self.store.thread_get(conn.channel_id or "", thread_id)
        if thread is None:
            raise UmpError(Err.UNKNOWN_THREAD, "thread 未绑定到任何会话", retryable=False, stage=Stage.RECEIVE)
        if envelope.binding_token != thread["binding_token"]:
            raise UmpError(
                Err.BINDING_EXPIRED, "绑定令牌已失效，需重新取得绑定", retryable=False, stage=Stage.RECEIVE
            )
        result = await self.service.accept(
            channel_id=conn.channel_id or "", thread_row=thread, env=envelope
        )
        await self._send_conn(
            conn,
            ump.make(
                "accepted",
                result,
                thread_id=thread_id,
                binding_token=thread["binding_token"],
                id=ump.new_id("s"),
            ),
        )

    async def _on_retry(self, conn: _Conn, envelope: Envelope) -> None:
        thread_id = envelope.thread_id or ""
        thread = self.store.thread_get(conn.channel_id or "", thread_id)
        if thread is None:
            raise UmpError(Err.UNKNOWN_THREAD, "thread 未绑定到任何会话", retryable=False, stage=Stage.RECEIVE)
        if envelope.binding_token != thread["binding_token"]:
            raise UmpError(Err.BINDING_EXPIRED, "绑定令牌已失效", retryable=False, stage=Stage.RECEIVE)
        result = await self.service.retry(
            channel_id=conn.channel_id or "",
            thread_id=thread_id,
            ref=envelope.payload["ref"],
            kind=envelope.payload.get("kind"),
        )
        await self._send_conn(
            conn,
            ump.make(
                "accepted",
                result,
                thread_id=thread_id,
                binding_token=thread["binding_token"],
                id=ump.new_id("s"),
            ),
        )

    def _on_delivery(self, conn: _Conn, envelope: Envelope) -> None:
        message_id = envelope.payload["message_id"]
        msg = self.store.outbound_by_message_id(message_id)
        if msg is None or msg["channel_id"] != conn.channel_id:
            raise UmpError(Err.NOT_FOUND, "找不到对应的出站消息", retryable=False, stage=Stage.DELIVERY)
        if envelope.binding_token != msg["binding_token"]:
            # 旧回执不能作用于新绑定（§2.3）
            raise UmpError(
                Err.BINDING_EXPIRED, "回执的绑定令牌与固化时不一致", retryable=False, stage=Stage.DELIVERY
            )
        index = envelope.payload.get("batch_index", 0)
        batches = json.loads(msg["parts"] or "[]")
        if index >= len(batches):
            raise UmpError(Err.PROTOCOL, "batch_index 超出范围", retryable=False, stage=Stage.DELIVERY)
        rollup = self.store.delivery_set(msg["seq"], index, envelope.payload["state"])
        log.info(
            "delivery msg=%s batch=%s state=%s rollup=%s",
            message_id,
            index,
            envelope.payload["state"],
            rollup,
        )

    # ---------- 管理面 ----------

    async def _mgmt_loop(self, ws: Any, first: dict[str, Any]) -> None:
        if first.get("op") != "auth":
            await self._send_raw(ws, _mgmt_reply(first, ok=False, error={"code": Err.AUTH_REQUIRED, "message": "需要管理认证"}))
            await _safe_close(ws, 1008, "auth required")
            return
        token = (first.get("args") or {}).get("token")
        if self._mgmt_used or not isinstance(token, str) or not hmac.compare_digest(token, self.mgmt_token):
            await self._send_raw(ws, _mgmt_reply(first, ok=False, error={"code": Err.AUTH_FAILED, "message": "管理凭据无效或已使用"}))
            await _safe_close(ws, 1008, "auth failed")
            return
        self._mgmt_used = True
        await self._send_raw(ws, _mgmt_reply(first, ok=True, result={"state": self.state}))
        try:
            async for raw in ws:
                frame = _parse_mgmt(raw)
                if frame is None:
                    await self._send_raw(ws, {"mgmt": "1", "ok": False, "error": {"code": Err.BAD_FRAME, "message": "非法管理帧"}})
                    continue
                try:
                    op = str(frame.get("op"))
                    if op == "settings.set":
                        result = await self._settings_set(dict(frame.get("args") or {}))
                    elif op == "thread.bind":
                        result = await self._thread_bind(dict(frame.get("args") or {}))
                    elif op in world_ops.ASYNC_OPS:
                        result = await world_ops.dispatch_async(
                            self.cfg,
                            self.service.llm,
                            op,
                            dict(frame.get("args") or {}),
                            store=self.store,
                            runtime=getattr(self.service, "runtime", None),
                        )
                    else:
                        result = self._mgmt_call(op, dict(frame.get("args") or {}))
                    await self._send_raw(ws, _mgmt_reply(frame, ok=True, result=result))
                except UmpError as exc:
                    await self._send_raw(ws, _mgmt_reply(frame, ok=False, error=exc.to_payload()))
                except Exception as exc:  # noqa: BLE001 —— 单个操作出错不得拖垮管理连接
                    log.exception("mgmt op failed op=%s", frame.get("op"))
                    await self._send_raw(
                        ws,
                        _mgmt_reply(
                            frame,
                            ok=False,
                            error={"code": Err.INTERNAL, "message": f"操作内部错误：{type(exc).__name__}", "retryable": False},
                        ),
                    )
        except ConnectionClosed:
            pass

    def connected_channels(self) -> list[str]:
        """当前在线的通道实例（诊断 / 管理面状态）。"""
        return sorted(self._conns)

    # ---------- 设置面 ----------

    async def _thread_bind(self, args: dict[str, Any]) -> dict[str, Any]:
        """管理面绑定 / 重绑：换代表令并通知在线通道（§2.2 binding 通知）。

        换代要让在线通道知道旧的作废了：先给旧绑定发 `state="revoked"`（换通道时发旧通道，
        同通道时就是它自己），再发新的 `state="active"`。只发 active 的话，客户端会一直拿着
        旧令牌，直到下一次发送才吃到 `binding_expired`。
        """
        previous: dict[str, Any] | None = None
        try:
            channel = self.store.channel_by_name(str(args.get("channel") or ""))
            if channel is not None:
                previous = self.store.thread_get(channel["id"], str(args.get("thread_id") or ""))
        except Exception:  # noqa: BLE001 —— 读不到旧绑定（或参数不是名）不影响绑定本身
            previous = None
        result = self._mgmt_call("thread.bind", args)
        row = result["thread"]
        if previous is not None and int(previous["binding_version"]) != int(row["binding_version"]):
            old_conn = self._conns.get(str(previous["channel_id"]))
            if old_conn is not None:
                await self._send_conn(
                    old_conn,
                    ump.make(
                        "binding",
                        {
                            "thread_id": previous["thread_id"],
                            "binding_version": previous["binding_version"],
                            "binding_token": previous["binding_token"],
                            "state": "revoked",
                        },
                        thread_id=previous["thread_id"],
                        id=ump.new_id("s"),
                    ),
                )
        conn = self._conns.get(row["channel_id"])
        if conn is not None:
            await self._send_conn(
                conn,
                ump.make(
                    "binding",
                    {
                        "thread_id": row["thread_id"],
                        "binding_version": row["binding_version"],
                        "binding_token": row["binding_token"],
                        "state": "active",
                    },
                    thread_id=row["thread_id"],
                    id=ump.new_id("s"),
                ),
            )
        return result

    def _settings_get(self) -> dict[str, Any]:
        cfg = self.cfg
        return {
            "llm": {
                "base_url": cfg.llm.base_url,
                "model": cfg.llm.model,
                "api_key": mask_api_key(cfg.llm.api_key),
                "api_key_set": bool(cfg.llm.api_key),
                "timeout_s": cfg.llm.timeout_s,
                "max_tokens": cfg.llm.max_tokens,
                "temperature": cfg.llm.temperature,
            },
            "memory": {
                "mode": "separate" if cfg.runtime.memory_embedding_model else "chat",
                "base_url": cfg.runtime.memory_embedding_base_url,
                "model": cfg.runtime.memory_embedding_model,
                "api_key": mask_api_key(cfg.runtime.memory_embedding_api_key),
                "api_key_set": bool(cfg.runtime.memory_embedding_api_key),
                "ready": bool(cfg.runtime.memory_embedding_model and cfg.runtime.memory_embedding_base_url),
            },
            "commit": {
                "auto_enabled": bool(cfg.runtime.autocommit_enabled),
                "minutes": int(cfg.runtime.autocommit_minutes),
                "events": int(cfg.runtime.autocommit_events),
            },
            "backup": {
                "dir": cfg.backup.dir,
                "interval_hours": int(cfg.backup.interval_hours),
                "keep": int(cfg.backup.keep),
            },
            "core": {
                "host": cfg.host,
                "max_text_len": cfg.max_text_len,
                "max_parts": cfg.max_parts,
                "context_history_max": cfg.context_history_max,
                "config_file": str(cfg.paths.config_file),
            },
        }

    async def _settings_set(self, args: dict[str, Any]) -> dict[str, Any]:
        """写入本地配置并即时生效；校验失败保留原值、错误不回显 Key（§3.3 白名单见 config.SETTABLE_SECTIONS）。"""
        llm_updates = args.get("llm")
        sections = {
            name: args.get(name) for name in SETTABLE_SECTIONS if isinstance(args.get(name), dict)
        }
        # 认得的段之外一律点名拒绝（含开发者专用段，如 runtime）
        unknown = [
            name
            for name, value in args.items()
            if name != "llm" and isinstance(value, dict) and name not in SETTABLE_SECTIONS
        ]
        if unknown:
            raise UmpError(Err.PROTOCOL, f"不开放的设置段：{unknown[0]}", retryable=False)
        if not isinstance(llm_updates, dict) and not sections:
            raise UmpError(
                Err.PROTOCOL, "settings.set 需要 llm 或 memory / commit / backup 段", retryable=False
            )
        reloaded = self.cfg
        try:
            if isinstance(llm_updates, dict):
                reloaded = save_llm_settings(self.cfg, llm_updates)
                self.cfg.llm = reloaded.llm  # 与 session 共用同一个 Config 对象
                await self.service.llm.aclose()  # base_url / key 可能变化，丢弃缓存的连接
                self.service.llm.cfg = reloaded.llm
                log.info(
                    "settings updated: model=%s base_url=%s key=%s",
                    reloaded.llm.model,
                    reloaded.llm.base_url,
                    bool(reloaded.llm.api_key),
                )
            for name in sections:
                reloaded = save_section_settings(reloaded, name, sections[name])
        except SettingsError as exc:
            raise UmpError(Err.PROTOCOL, str(exc), retryable=False) from exc
        if sections:
            # 运行层服务持有自己的运行时副本（RuntimeService 与 SessionService 都可能有）：一并写回，
            # 否则配置改了不生效（设计探针口径：runtime 键同时落在 RuntimeConfig 与实例属性上）
            targets = [self.service, getattr(self.service, "runtime", None)]
            for field_name, value in vars(reloaded.runtime).items():
                for target in targets:
                    if target is not None and hasattr(target, field_name):
                        setattr(target, field_name, value)
            self.cfg.runtime = reloaded.runtime
            self.cfg.backup = reloaded.backup
            log.info("settings updated: sections=%s", ",".join(sorted(sections)))
        return self._settings_get()

    def _mgmt_call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "status":
            return {
                "app": APP_VERSION,
                "data_format": DATA_FORMAT_VERSION,
                "rules": RULES_VERSION,
                "ump": UMP_VERSION,
                "state": self.state,
                "endpoint": self.endpoint,
                "counts": self.store.counts(),
                "channels_connected": self.connected_channels(),
                "sessions": self.store.session_list(),
                "placeholder": dict(self.cfg.placeholder),  # 阶段 0 占位会话三元组
            }
        if op == "session.ensure":
            instance_id = str(args.get("instance_id") or "")
            timeline_id = str(args.get("timeline_id") or "")
            character_id = str(args.get("character_id") or "")
            runtime = getattr(self.service, "runtime", None)
            instance = self.store.instance_get(instance_id)
            if runtime is not None and instance is not None:
                # 会话创建先查成员资格（§3.7 第 1 条）：撤销过的补入角色与未装配标识都挡在这里
                try:
                    runtime.assert_member(instance, timeline_id, character_id)
                except RuntimeStateError as exc:
                    raise UmpError(Err.STATE_BLOCKED, str(exc), retryable=False, stage=Stage.RECEIVE) from exc
            row = self.store.session_ensure(instance_id, timeline_id, character_id)
            return {"session": row}
        if op == "session.list":
            return {"sessions": self.store.session_list()}
        if op == "channel.ensure":
            # 受信管理通路签发通道凭据（认证材料不经 URL / 日志 / 插件环境）
            name = str(args.get("name") or "").strip()
            if not name:
                raise UmpError(Err.PROTOCOL, "channel.ensure 需要 name", retryable=False)
            row, credential = self.store.channel_register(
                name=name,
                display_name=str(args.get("display_name") or name),
                version=str(args.get("version") or "0"),
                protocol=UMP_VERSION,
                capabilities=dict(args.get("capabilities") or {}),
                rotate=bool(args.get("rotate", False)),
            )
            return {"channel": row, "credential": credential}
        if op == "thread.bind":
            channel = self.store.channel_by_name(str(args.get("channel") or ""))
            if channel is None:
                raise UmpError(Err.NOT_FOUND, "通道未登记", retryable=False)
            session = self.store.session_get(str(args.get("session_id") or ""))
            if session is None:
                raise UmpError(Err.NOT_FOUND, "会话不存在", retryable=False)
            row = self.store.thread_bind(channel["id"], str(args.get("thread_id") or ""), session["id"])
            return {"thread": row}
        if op == "thread.list":
            return {"threads": self.store.thread_list(args.get("channel_id"))}
        if op == "settings.get":
            return self._settings_get()
        if op in world_ops.SYNC_OPS:
            return world_ops.dispatch(self.cfg, self.store, op, args, runtime=getattr(self.service, "runtime", None))
        if op == "history.page":
            session = self.store.session_get(str(args.get("session_id") or ""))
            if session is None:
                raise UmpError(Err.NOT_FOUND, "会话不存在", retryable=False)
            page = self.store.history_page(
                session["id"],
                limit=int(args.get("limit") or 50),
                before_seq=args.get("before_seq"),
            )
            return {
                "session": session,
                "messages": [_public_message(row) for row in page["messages"]],
                "has_more": page["has_more"],
                "next_before_seq": page["next_before_seq"],
                # 历史版本（回滚会让它倒退）：客户端拿它作废本地缓存与旧游标（§3 验收 8）
                "revision": page["revision"],
            }
        raise UmpError(Err.UNSUPPORTED_TYPE, f"未知管理操作 {op}", retryable=False)


def _public_message(row: dict[str, Any]) -> dict[str, Any]:
    """管理面可见的消息面：只含往来原文与处理 / 投递状态。"""
    parts = row.get("parts")
    return {
        "seq": row["seq"],
        "role": row["role"],
        "text": row["text"],
        "parts": json.loads(parts) if parts else None,
        "env_id": row["env_id"],
        "message_id": row["message_id"],
        "reply_message_id": row.get("reply_message_id"),
        "reply_to": row["reply_to"],
        "batch_count": row["batch_count"],
        "state": row["state"],
        "created_at": row["created_at"],
    }


def _parse_mgmt(raw: str | bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(data, dict) and data.get("mgmt") == "1":
        return data
    return None


def _mgmt_reply(frame: dict[str, Any], *, ok: bool, result: dict[str, Any] | None = None,
                error: dict[str, Any] | None = None) -> dict[str, Any]:
    reply: dict[str, Any] = {"mgmt": "1", "op": frame.get("op"), "ok": ok}
    if frame.get("id") is not None:
        reply["id"] = frame["id"]
    if result is not None:
        reply["result"] = result
    if error is not None:
        reply["error"] = error
    return reply


async def _safe_close(ws: Any, code: int, reason: str) -> None:
    try:
        await ws.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - 关闭失败无需上报
        pass
