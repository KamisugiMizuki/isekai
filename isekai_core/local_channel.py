"""进程内通道（CHANNEL_PLUGIN_SPEC §2.5「安卓内建」）。

安卓端保持同一套 UMP 语义，但**不强制复制桌面 WS**：这里给一条进程内传输——帧直接喂给同一个
`CoreServer._handler`，认证、去重、回执、错误与限额全走同一段代码，只是不占回环端口。

与 WS 路径的差别只在调度与编码（ANDROID_SPEC §3.1）：

- **按事件唤醒**：帧到达即用 `asyncio.Event` 唤醒读取方 / 取帧方，不做 10 ms 周期轮询；
- **同帧不做双向 JSON 编解码**：帧本来就允许是 dict（`ump.parse` 直接吃 dict，`ump.make` 产出
  dict），能传 dict 就传 dict；只有「核心已编码的出站线上文本」那一侧才解一次 JSON；
- **限额与 WS 路径同源同值**：单帧上限 `MAX_FRAME_BYTES` 在进程内同样拦（超限与库侧一致地
  按 1009 message too big 关掉这条连接，核心不进解析），文本 / 段数 / 附件限额则因为走的是
  同一段 `_handler` + `ump.parse(negotiated ...)` 天然同源。

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
from collections import deque
from typing import Any

from . import ump
from .channel import CoreServer
from .version import MAX_FRAME_BYTES

#: websockets 在收帧超限时报的关闭码（`CloseCode.MESSAGE_TOO_BIG`）：进程内照抄这个语义
MESSAGE_TOO_BIG = 1009


def _frame_bytes(frame: Any) -> int:
    """一帧按线上形态的字节数（dict 会编码一次——只为量长度，不做解码往返）。"""
    if isinstance(frame, str):
        return len(frame.encode("utf-8"))
    if isinstance(frame, (bytes, bytearray)):
        return len(bytes(frame))
    return len(json.dumps(frame, ensure_ascii=False).encode("utf-8"))


class LocalWS:
    """进程内传输的「ws」替身：`send` 收集出站帧，异步迭代从队列取入站帧（按事件唤醒）。"""

    def __init__(self, *, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        #: 出站帧：核心已 `json.dumps` 过的线上文本原样收下（`take()` 才解一次）
        self.sent: list[str] = []
        self.closed = False
        self.close_code = 0
        self.close_reason = ""
        #: 观测：因超过单帧上限而被拦下的帧数（与 WS 侧「库直接断开」同一处置）
        self.oversized = 0
        self.max_frame_bytes = int(max_frame_bytes)
        self._pending: deque[Any] = deque()
        #: 帧到达即置位（读方不等轮询）；关闭也置位，让读方看到关闭
        self._inbound = asyncio.Event()
        #: 出站帧到达即置位（取帧方不等轮询）
        self._outbound = asyncio.Event()

    # ---- websocket 的那半边 ----
    async def send(self, data: str) -> None:
        if self.closed:
            return
        text = str(data)
        if len(text.encode("utf-8")) > self.max_frame_bytes:
            self._fail_message_too_big()
            return
        self.sent.append(text)
        self._outbound.set()

    async def recv(self) -> Any:
        while True:
            if self._pending:
                return self._pending.popleft()
            if self.closed:
                raise asyncio.CancelledError("closed")
            self._inbound.clear()
            if self._pending or self.closed:  # 清事件与等待之间到达的帧不丢
                continue
            await self._inbound.wait()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed, self.close_code, self.close_reason = True, int(code), str(reason)
        self._inbound.set()
        self._outbound.set()

    def __aiter__(self) -> "LocalWS":
        return self

    async def __anext__(self) -> Any:
        while True:
            if self._pending:
                return self._pending.popleft()
            if self.closed:
                raise StopAsyncIteration
            self._inbound.clear()
            if self._pending or self.closed:
                continue
            await self._inbound.wait()

    # ---- 进程内的这半边 ----
    def feed(self, frame: dict[str, Any] | str) -> None:
        """喂一帧入站（dict 优先：进程内能传 dict 就不做 JSON 编解码往返）。

        超过单帧上限的帧按 WS 路径同一处置：**核心不进解析、不发 error 信封**，
        直接以 1009 关掉这条连接。
        """
        if self.closed:
            return
        data: Any = frame if isinstance(frame, (dict, str)) else json.dumps(frame, ensure_ascii=False)
        if _frame_bytes(data) > self.max_frame_bytes:
            self._fail_message_too_big()
            return
        self._pending.append(data)
        self._inbound.set()

    def take(self) -> list[dict[str, Any]]:
        """取走已收的出站帧（核心已编码的线上文本解一次；喂进 dict 的帧原样给）。"""
        out: list[dict[str, Any]] = []
        for item in self.sent:
            parsed = item
            if isinstance(item, str):
                try:
                    parsed = json.loads(item)
                except json.JSONDecodeError:
                    continue
            if isinstance(parsed, dict):
                out.append(parsed)
        self.sent.clear()
        return out

    async def wait_outbound(self) -> None:
        """等到有出站帧（或传输关闭）：帧到达即唤醒，不做周期轮询。"""
        while True:
            if self.sent or self.closed:
                return
            self._outbound.clear()
            if self.sent or self.closed:
                return
            await self._outbound.wait()

    def _fail_message_too_big(self) -> None:
        """超单帧上限：与 WS 库一致地 fail(1009)，连接不再可用（不发 error 信封）。"""
        self.oversized += 1
        self.closed = True
        self.close_code = MESSAGE_TOO_BIG
        self.close_reason = "message too big"
        self._inbound.set()
        self._outbound.set()


class InProcessChannel:
    """安卓侧可用的进程内通道：与桌面 WS 同一套 UMP 语义，收发都在进程内。"""

    def __init__(
        self,
        server: CoreServer,
        *,
        channel_id: str = "builtin-local",
        name: str = "进程内通道",
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        self.server = server
        self.channel_id = channel_id
        self.name = name
        self.ws = LocalWS(max_frame_bytes=max_frame_bytes)
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
        """送一帧 UMP（dict 直传，不编码），返回它引发的出站帧。"""
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
        """等到出站帧就返回；**帧到达即唤醒**，不做 10 ms 轮询（ANDROID_SPEC §3.1）。"""
        deadline = time.monotonic() + max(0.1, float(timeout))
        while True:
            frames = self.ws.take()
            if frames:
                return frames
            if self.ws.closed:
                return []  # 传输已关闭（含 1009 超限）：不会再有出站帧
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            try:
                await asyncio.wait_for(self.ws.wait_outbound(), timeout=remaining)
            except asyncio.TimeoutError:
                return []
