"""通道客户端：内建聊天（桌面壳）与开发 CLI 共用的 UMP 实现。

- `UmpClient`：hello 握手（引导凭据 / 持久凭据）、收发信封、回执与重试。
- `MgmtClient`：受信管理面（会话选择、绑定、历史分页）。两者可用同一端点。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from . import ump
from .ump import Envelope, UmpError
from .version import APP_VERSION, DEFAULT_MAX_PARTS, DEFAULT_MAX_TEXT_LEN


@dataclass
class UmpClient:
    endpoint: str
    channel_id: str = "builtin"
    name: str = "内建聊天窗口"
    version: str = APP_VERSION
    credential: str | None = None
    bootstrap: str | None = None
    segments: bool = True
    status: bool = True
    attachments: bool = False
    streaming: bool = False
    max_text_len: int = DEFAULT_MAX_TEXT_LEN
    max_parts: int = DEFAULT_MAX_PARTS

    #: 握手结果（含核心签发的持久凭据）
    hello_ack: dict[str, Any] | None = None
    negotiated: dict[str, Any] = field(default_factory=dict)
    _ws: Any = None
    _pump: asyncio.Task[Any] | None = None
    _queue: asyncio.Queue[Envelope] = field(default_factory=asyncio.Queue)

    async def connect(self, *, timeout: float = 15.0) -> dict[str, Any]:
        self._ws = await connect(self.endpoint, max_size=1 << 20, ping_interval=20, ping_timeout=20)
        auth: dict[str, str] = {}
        if self.credential:
            auth["credential"] = self.credential
        elif self.bootstrap:
            auth["bootstrap"] = self.bootstrap
        hello = ump.make(
            "hello",
            {
                "channel": {"id": self.channel_id, "name": self.name, "version": self.version},
                "capabilities": {
                    "segments": self.segments,
                    "status": self.status,
                    "attachments": self.attachments,
                    "streaming": self.streaming,
                    "max_text_len": self.max_text_len,
                    "max_parts": self.max_parts,
                },
                "auth": auth,
            },
        )
        await self._ws.send(json.dumps(hello, ensure_ascii=False))
        self._pump = asyncio.create_task(self._read_loop())
        ack = await self.expect(lambda env: env.type in ("hello_ack", "error"), timeout=timeout)
        if ack.type == "error":
            raise UmpError(
                ack.payload.get("code", ump.Err.AUTH_FAILED),
                ack.payload.get("message", "握手失败"),
                retryable=bool(ack.payload.get("retryable")),
            )
        self.hello_ack = ack.payload
        self.negotiated = ack.payload.get("negotiated", {})
        return ack.payload

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    await self._queue.put(ump.parse(raw, direction="s2c"))
                except UmpError:
                    continue
        except ConnectionClosed:
            pass

    async def expect(
        self,
        predicate: Callable[[Envelope], bool],
        *,
        timeout: float | None = 60.0,
        collect: list[Envelope] | None = None,
    ) -> Envelope:
        """等待满足条件的信封；不满足的照原样放进 collect（供调用方自行处理）。"""
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - asyncio.get_running_loop().time())
            try:
                envelope = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise TimeoutError("等待信封超时") from exc
            if predicate(envelope):
                return envelope
            if collect is not None:
                collect.append(envelope)

    async def send_user_message(
        self,
        *,
        thread_id: str,
        binding_token: str,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        payload: dict[str, Any] = {"text": text}
        if attachments:
            payload["attachments"] = attachments
        env = ump.make(
            "user_message",
            payload,
            thread_id=thread_id,
            binding_token=binding_token,
            id=ump.new_id("e"),
        )
        await self.send(env)
        return env["id"]

    async def request_retry(self, *, thread_id: str, binding_token: str, ref: str, kind: str | None = None) -> None:
        payload: dict[str, Any] = {"ref": ref}
        if kind:
            payload["kind"] = kind
        await self.send(ump.make("retry", payload, thread_id=thread_id, binding_token=binding_token))

    async def report_delivery(
        self,
        *,
        thread_id: str,
        binding_token: str,
        message_id: str,
        batch_index: int,
        state: str,
    ) -> None:
        await self.send(
            ump.make(
                "delivery",
                {"message_id": message_id, "batch_index": batch_index, "state": state},
                thread_id=thread_id,
                binding_token=binding_token,
            )
        )

    async def send(self, envelope: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(envelope, ensure_ascii=False))

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


class MgmtClient:
    """受信管理面客户端（仅壳与开发工具使用）。"""

    def __init__(self, endpoint: str, token: str) -> None:
        self.endpoint = endpoint
        self.token = token
        self._ws: Any = None
        self._counter = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pump: asyncio.Task[Any] | None = None
        self.info: dict[str, Any] = {}

    async def connect(self, *, timeout: float = 15.0) -> dict[str, Any]:
        self._ws = await connect(self.endpoint, max_size=1 << 20, ping_interval=20, ping_timeout=20)
        auth = {"mgmt": "1", "op": "auth", "id": "r-0", "args": {"token": self.token}}
        await self._ws.send(json.dumps(auth, ensure_ascii=False))
        reply = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=timeout))
        if not reply.get("ok"):
            raise UmpError(
                (reply.get("error") or {}).get("code", ump.Err.AUTH_FAILED),
                (reply.get("error") or {}).get("message", "管理认证失败"),
            )
        self.info = reply.get("result") or {}
        self._pump = asyncio.create_task(self._read_loop())
        return self.info

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                frame = json.loads(raw)
                future = self._pending.pop(str(frame.get("id")), None)
                if future is not None and not future.done():
                    future.set_result(frame)
        except ConnectionClosed:
            pass

    async def call(self, op: str, *, timeout: float = 30.0, **args: Any) -> dict[str, Any]:
        """管理调用。生成类操作会等模型返回，调用方给足 timeout（桌面端同款参数）。"""
        self._counter += 1
        ref = f"r-{self._counter}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[ref] = future
        await self._ws.send(json.dumps({"mgmt": "1", "op": op, "id": ref, "args": args}, ensure_ascii=False))
        frame = await asyncio.wait_for(future, timeout=timeout)
        if not frame.get("ok"):
            error = frame.get("error") or {}
            raise UmpError(error.get("code", "mgmt_error"), error.get("message", "管理操作失败"))
        return frame.get("result") or {}

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
