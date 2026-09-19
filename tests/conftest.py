"""测试脚手架：在临时根目录里跑一个真实核心（真 WebSocket、真 SQLite），只把 LLM 换掉。"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.client import MgmtClient, UmpClient  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM, LLMError  # noqa: E402


@dataclass
class Harness:
    cfg: Any
    runtime: Any
    fake: FakeLLM
    endpoint: str

    @property
    def store(self) -> Any:
        return self.runtime.store

    @property
    def bootstrap(self) -> str:
        return self.runtime.server.bootstrap_token

    @property
    def mgmt_token(self) -> str:
        return self.runtime.server.mgmt_token


@asynccontextmanager
async def running_core(
    tmp_path: Path,
    *,
    replies: list[str] | None = None,
    fail_with: LLMError | None = None,
) -> AsyncIterator[Harness]:
    cfg = load_config(tmp_path)
    fake = FakeLLM(replies or ["收到。"], fail_with=fail_with)
    runtime = await build_runtime(cfg, llm=fake)
    endpoint = await runtime.server.start()
    harness = Harness(cfg=cfg, runtime=runtime, fake=fake, endpoint=endpoint)
    try:
        yield harness
    finally:
        await runtime.service.shutdown()
        await runtime.server.close()
        runtime.store.close()


async def open_mgmt(harness: Harness) -> MgmtClient:
    """管理凭据一次性：一个核心进程只开一个管理连接。"""
    mgmt = MgmtClient(harness.endpoint, harness.mgmt_token)
    await mgmt.connect()
    return mgmt


async def bind_thread(
    harness: Harness,
    mgmt: MgmtClient,
    *,
    channel_id: str,
    thread_id: str,
    instance: str = "ph-instance",
    timeline: str = "main",
    character: str = "ph-character",
    capabilities: dict[str, Any] | None = None,
    status: bool = True,
    segments: bool = True,
    max_text_len: int | None = None,
    max_parts: int | None = None,
) -> tuple[UmpClient, dict[str, Any]]:
    """管理面登记通道 + 建会话 + 绑定 thread，返回已握手的通道客户端与其绑定行。"""
    issued = await mgmt.call("channel.ensure", name=channel_id, capabilities=capabilities or {})
    credential = issued["credential"]
    session = (
        await mgmt.call(
            "session.ensure", instance_id=instance, timeline_id=timeline, character_id=character
        )
    )["session"]
    thread = (
        await mgmt.call("thread.bind", channel=channel_id, thread_id=thread_id, session_id=session["id"])
    )["thread"]

    client = UmpClient(
        endpoint=harness.endpoint,
        channel_id=channel_id,
        name=channel_id,
        credential=credential,
        status=status,
        segments=segments,
        **({"max_text_len": max_text_len} if max_text_len else {}),
        **({"max_parts": max_parts} if max_parts else {}),
    )
    ack = await client.connect()
    return client, {"thread": thread, "session": session, "ack": ack, "credential": credential}


@pytest.fixture
def anyio_backend() -> str:  # 兼容标记；本项目只用 asyncio
    return "asyncio"
