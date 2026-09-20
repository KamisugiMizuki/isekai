"""进程内通道（CHANNEL_PLUGIN_SPEC §2.5「安卓内建」）。

安卓端保持同一套 UMP 语义，但**不强制复制桌面 WS**：这里给一条进程内传输——帧直接喂给同一个
`CoreServer._handler`，认证、去重、回执、错误与限额全走同一段代码，只是不占回环端口。

用法（安卓侧 / 测试）：

    channel = InProcessChannel(server, channel_id="builtin-local", name="安卓内建")
    await channel.connect(bootstrap=core.bootstrap_token)      # 握手：认证材料仍由受信通路给
    await channel.send(ump.make("user_message", {"text": "在吗"}, thread_id="t", binding_token=tok))
    reply = await channel.mgmt("world.package.validate", {"package_path": "..."})
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from . import ump
from .channel import CoreServer

_WAKE = object()


class LocalWS:
    """进程内传输的「ws」替身：`send` 收集出站帧，异步迭代从队列取入站帧。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self.close_code = 0
        self.close_reason = ""
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = asyncio.Event()

    # ---- websocket 的那半边 ----
    async def send(self, data: str) -> None:
        if not self.closed:
            self.sent.append(str(data))

    async def recv(self) -> str:
        item = await self._queue.get()
        if item is _WAKE:
            raise asyncio.CancelledError("closed")
        return str(item)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed, self.close_code, self.close_reason = True, int(code), str(reason)
        self._closed.set()
        self._queue.put_nowait(_WAKE)

    def __aiter__(self) -> "LocalWS":
        return self

    async def __anext__(self) -> str:
        if self.closed and self._queue.empty():
            raise StopAsyncIteration
        getter = asyncio.ensure_future(self._queue.get())
        closed = asyncio.ensure_future(self._closed.wait())
        done, pending = await asyncio.wait({getter, closed}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if getter in done and getter.result() is not _WAKE and not self.closed:
            return str(getter.result())
        raise StopAsyncIteration

    # ---- 进程内的这半边 ----
    def feed(self, frame: dict[str, Any] | str) -> None:
        self._queue.put_nowait(
            frame if isinstance(frame, str) else json.dumps(frame, ensure_ascii=False)
        )

    def take(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in self.sent:
            try:
                parsed = json.loads(item)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        self.sent.clear()
        return out


class InProcessChannel:
    """安卓侧可用的进程内通道：与桌面 WS 同一套 UMP 语义，收发都在进程内。"""

    def __init__(
        self, server: CoreServer, *, channel_id: str = "builtin-local", name: str = "进程内通道"
    ) -> None:
        self.server = server
        self.channel_id = channel_id
        self.name = name
        self.ws = LocalWS()
        self._task: asyncio.Task[Any] | None = None

    async def connect(
        self,
        *,
        bootstrap: str | None = None,
        credential: str | None = None,
        capabilities: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """起处理器 + 发 hello；返回握手回帧（失败时是 error 信封）。"""
        self._task = asyncio.create_task(self.server._handler(self.ws))  # noqa: SLF001 - 同一段握手 / 分发路径
        auth = {"bootstrap": bootstrap} if bootstrap else {"credential": credential}
        hello = ump.make(
            "hello",
            {
                "channel": {"id": self.channel_id, "name": self.name, "version": "0.1.0"},
                "capabilities": {"segments": True, "status": True, **(capabilities or {})},
                "auth": auth,
            },
        )
        self.ws.feed(hello)
        frames = await self._await_frame(timeout=timeout)
        return frames[0] if frames else {}

    async def send(self, envelope: dict[str, Any], *, timeout: float = 10.0) -> list[dict[str, Any]]:
        """送一帧 UMP，返回它引发的出站帧。"""
        self.ws.feed(envelope)
        return await self._await_frame(timeout=timeout)

    async def connect_mgmt(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """以**管理角色**起连接：管理帧协议的连接首帧必须是 auth（与壳同一条路径）。"""
        self._task = asyncio.create_task(self.server._handler(self.ws))  # noqa: SLF001
        frame = {
            "mgmt": "1",
            "op": "auth",
            "args": {"token": self.server.mgmt_token},
            "id": "r-0",
        }
        self.ws.feed(frame)
        frames = await self._await_frame(timeout=timeout)
        return frames[0] if frames else {}

    async def mgmt(
        self, op: str, args: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        """管理面调用（先 connect_mgmt）；返回 reply 帧。"""
        frame = {"mgmt": "1", "op": op, "args": args or {}, "id": ump.new_id("m")}
        self.ws.feed(frame)
        frames = await self._await_frame(timeout=timeout)
        return frames[0] if frames else {}

    async def close(self) -> None:
        await self.ws.close(code=1000, reason="client closed")
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=2.0)
            self._task = None

    async def _await_frame(self, *, timeout: float) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            frames = self.ws.take()
            if frames:
                return frames
        return []
