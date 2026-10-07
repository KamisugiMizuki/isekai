"""LLMClient 的请求重试行为（httpx MockTransport，不联网）。

判据：
1. 400/422 且本次带了采样参数（如 V4 思考模式对 temperature 的约束）→ 去掉它重试一次；
2. 空内容的重试要带上**翻倍后的预算**（payload 里真是新值，不是只改了局部变量）；
3. 服务端的原话要带进错误信息（诊断时不靠猜）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from isekai_core.config import LLMConfig
from isekai_core.llm import LLMClient, LLMError


def _wire(client: LLMClient, handler) -> list[httpx.Request]:
    """把 client 的传输层换成 MockTransport，记录每个请求。"""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def fake_http() -> httpx.AsyncClient:
        if client._client is None:
            client._client = httpx.AsyncClient(
                base_url=client.cfg.base_url.rstrip("/"),
                transport=httpx.MockTransport(wrapped),
            )
        return client._client

    client._http = fake_http  # type: ignore[method-assign]
    return seen


def _cfg(**over) -> LLMConfig:
    values = dict(
        base_url="https://api.example.com/v1",
        model="m",
        api_key="k",
        timeout_s=5,
        max_tokens=64,
        temperature=0.8,
    )
    values.update(over)
    return LLMConfig(**values)


async def test_rejected_sampling_parameter_retries_without_temperature() -> None:
    client = LLMClient(_cfg())
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "temperature" in body:
            return httpx.Response(422, json={"error": {"message": "temperature not allowed"}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "可用"}, "finish_reason": "stop"}]}
        )

    _wire(client, handler)
    reply = await client.chat([{"role": "user", "content": "hi"}])

    assert reply == "可用"
    assert len(bodies) == 2
    assert "temperature" in bodies[0] and "temperature" not in bodies[1], "第一次被拒后要去掉温度重试"


async def test_empty_completion_retry_uses_doubled_budget() -> None:
    client = LLMClient(_cfg())
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    _wire(client, handler)
    reply = await client.chat([{"role": "user", "content": "hi"}])

    assert reply == "ok"
    assert [item["max_tokens"] for item in bodies] == [64, 128], "空内容重试要带上翻倍后的预算"
    assert bodies[1].get("thinking") == {"type": "disabled"}, "烧空的重试要降为直接输出（thinking disabled）"


async def test_thinking_switch_when_asked_and_absent_by_default() -> None:
    """判断点显式传 thinking="disabled" 时请求体带开关；不传就不带（对话保持服务端默认）。"""
    client = LLMClient(_cfg())
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    _wire(client, handler)
    await client.chat([{"role": "user", "content": "hi"}], thinking="disabled")
    await client.chat([{"role": "user", "content": "hi"}])

    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert "thinking" not in bodies[1]


async def test_empty_completion_gives_up_after_disabled_retry() -> None:
    """降档重试只做一次：仍然空就如实失败，不无限重试。"""
    client = LLMClient(_cfg())
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        )

    _wire(client, handler)
    with pytest.raises(LLMError) as info:
        await client.chat([{"role": "user", "content": "hi"}])

    assert info.value.code == "empty_completion"
    assert len(bodies) == 2 and bodies[1].get("thinking") == {"type": "disabled"}


async def test_max_tokens_is_sent_as_integer() -> None:
    """max_tokens 必须是真整数：服务端（Rust 反序列化）拒绝 `65536.0` 这类浮点。

    配置层可能把 max_tokens 存成浮点（历史配置就是），出网口要统一 int 化。
    """
    client = LLMClient(_cfg(max_tokens=65536.0))
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    _wire(client, handler)
    await client.chat([{"role": "user", "content": "hi"}])

    assert bodies[0]["max_tokens"] == 65536
    assert isinstance(bodies[0]["max_tokens"], int), "浮点 max_tokens 会在服务端反序列化失败（422）"
    assert "65536.0" not in json.dumps(bodies[0]), "请求体里不能出现浮点字面量"


async def test_rejection_detail_keeps_server_message() -> None:
    client = LLMClient(_cfg())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "invalid max_tokens value"}})

    _wire(client, handler)
    with pytest.raises(LLMError) as info:
        await client.chat([{"role": "user", "content": "hi"}])

    assert info.value.code == "llm_rejected"
    assert "HTTP 400" in info.value.message and "invalid max_tokens value" in info.value.message
