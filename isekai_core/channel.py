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
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import Server, serve
from websockets.exceptions import ConnectionClosed

from . import ump
from .config import Config
from .log import get_logger
from .session import SessionService
from .store import Store
from .ump import Envelope, Err, UmpError
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

        try:
            envelope = ump.parse(first, direction="c2s", max_text_len=self.cfg.max_text_len)
        except UmpError as first_error:
            mgmt = _parse_mgmt(first)
            if mgmt is not None:
                await self._mgmt_loop(ws, mgmt)
                return
            await self._send_raw(ws, ump.error_envelope(first_error))
            await _safe_close(ws, 1008, "expected hello")
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
                raise UmpError(Err.AUTH_FAILED, "引导凭据无效或已使用", retryable=False, close=True)
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
            raise UmpError(Err.AUTH_FAILED, "通道凭据无效", retryable=False, close=True)
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
                await self._dispatch(conn, envelope)
        except ConnectionClosed:
            pass
        finally:
            if conn.channel_id and self._conns.get(conn.channel_id) is conn:
                del self._conns[conn.channel_id]

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
                raise UmpError(Err.STATE_BLOCKED, f"核心状态 {self.state}，暂不接受新消息", retryable=True)
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
            raise UmpError(Err.UNKNOWN_THREAD, "thread 未绑定到任何会话", retryable=False)
        if envelope.binding_token != thread["binding_token"]:
            raise UmpError(Err.BINDING_EXPIRED, "绑定令牌已失效，需重新取得绑定", retryable=False)
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
            raise UmpError(Err.UNKNOWN_THREAD, "thread 未绑定到任何会话", retryable=False)
        if envelope.binding_token != thread["binding_token"]:
            raise UmpError(Err.BINDING_EXPIRED, "绑定令牌已失效", retryable=False)
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
            raise UmpError(Err.NOT_FOUND, "找不到对应的出站消息", retryable=False)
        index = envelope.payload.get("batch_index", 0)
        batches = json.loads(msg["parts"] or "[]")
        if index >= len(batches):
            raise UmpError(Err.PROTOCOL, "batch_index 超出范围", retryable=False)
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
                    result = self._mgmt_call(str(frame.get("op")), dict(frame.get("args") or {}))
                    await self._send_raw(ws, _mgmt_reply(frame, ok=True, result=result))
                except UmpError as exc:
                    await self._send_raw(ws, _mgmt_reply(frame, ok=False, error=exc.to_payload()))
        except ConnectionClosed:
            pass

    def connected_channels(self) -> list[str]:
        """当前在线的通道实例（诊断 / 管理面状态）。"""
        return sorted(self._conns)

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
            }
        if op == "session.ensure":
            row = self.store.session_ensure(
                str(args.get("instance_id") or ""),
                str(args.get("timeline_id") or ""),
                str(args.get("character_id") or ""),
            )
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
