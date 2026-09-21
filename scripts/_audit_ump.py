#!/usr/bin/env python
"""UMP 行为审计探针（只读：不改代码 / 测试 / data；临时库）。

依据：`docs/worldruntime/SESSION_CORE_SPEC.md` 的 UMP 条款 + `docs/worldruntime/CHANNEL_PLUGIN_SPEC.md` 中
属「通道宿主 / 协议」的部分（信封校验、认证与持久身份、幂等去重、投递回执、
错误模型、重连补投、能力协商与限额）。

真 WebSocket 回环 + 真 SQLite（临时目录），不启动核心进程、不占用固定端口；
每个条目独立建通道 / thread，互不影响。按设计后置的条款标 DEFERRED，不算 FAIL。

用法：.venv/Scripts/python.exe scripts/_audit_ump.py [--only CODE]
输出：每行 `<STATUS> <摘要> — 证据`；末行 `TOTAL n PASS p FAIL f DEFERRED d`。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from websockets.asyncio.client import connect as ws_connect  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

from conftest import bind_thread, open_mgmt, running_core  # noqa: E402
from isekai_core import ump  # noqa: E402
from isekai_core.client import MgmtClient, UmpClient  # noqa: E402
from isekai_core.llm import LLMError  # noqa: E402
from isekai_core.ump import UmpError  # noqa: E402

SHORT = "收到。"
#: 6 行、每行 20 字符：用于分段 / 能力不兼容条目
LONG = "\n".join(f"第{i}行：" + "世界内容" * 4 for i in range(1, 7))
PROBE = "scripts/_audit_ump.py"

RESULTS: list[tuple[str, str, str, str]] = []


def emit(status: str, code: str, summary: str, evidence: str) -> None:
    RESULTS.append((status, code, summary, evidence))
    print(f"{status} {code} {summary} — {evidence}", flush=True)


class Ctx:
    """两个真核心：h 短回复；hb 长回复（分段 / 限额条目用）。"""

    def __init__(self, h: Any, hb: Any, mgmt: Any, mgmt_b: Any) -> None:
        self.h, self.hb, self.mgmt, self.mgmt_b = h, hb, mgmt, mgmt_b


# ---------------------------------------------------------------- 基础工具


async def send(ws: Any, env: dict[str, Any]) -> None:
    await ws.send(json.dumps(env, ensure_ascii=False))


async def recv(ws: Any, timeout: float = 8.0) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def recv_all(ws: Any, seconds: float = 2.0) -> list[dict[str, Any]]:
    """裸 WS：收走这段时间内到达的全部帧（不经客户端解析，保留原始线上形态）。"""
    out: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while True:
        budget = end - loop.time()
        if budget <= 0:
            return out
        try:
            out.append(await recv(ws, budget))
        except (asyncio.TimeoutError, TimeoutError):
            continue


def err_code(env: dict[str, Any]) -> str:
    assert env.get("type") == "error", f"期望 error 信封：{json.dumps(env, ensure_ascii=False)[:200]}"
    return str(env["payload"]["code"])


async def expect_error(ws: Any, code: str, frame: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
    if frame is not None:
        await send(ws, frame)
    env = await recv(ws, timeout)
    assert env.get("type") == "error", f"期望 error，得到 {json.dumps(env, ensure_ascii=False)[:200]}"
    assert env["payload"]["code"] == code, f"期望 {code}，得到 {env['payload']}"
    return env


async def expect_closed(ws: Any, timeout: float = 6.0) -> int | None:
    """等待服务端断开；返回收到的关闭码（若有）。"""
    try:
        await asyncio.wait_for(ws.recv(), timeout)
    except ConnectionClosed as exc:
        return exc.rcvd.code if exc.rcvd is not None else None
    raise AssertionError("连接未被关闭")


async def hello(ws: Any, name: str, *, credential: str | None = None, bootstrap: str | None = None,
                caps: dict[str, Any] | None = None) -> dict[str, Any]:
    auth = {"credential": credential} if credential else {"bootstrap": bootstrap}
    await send(
        ws,
        ump.make(
            "hello",
            {
                "channel": {"id": name, "name": name, "version": "0.1.0"},
                "capabilities": caps or {"segments": True, "status": True, "max_text_len": 4000, "max_parts": 10},
                "auth": auth,
            },
        ),
    )
    return await recv(ws)


async def credential_for(ctx: Ctx, name: str, *, mgmt: Any = None, caps: dict[str, Any] | None = None) -> str:
    issued = await (mgmt or ctx.mgmt).call("channel.ensure", name=name, capabilities=caps or {})
    return str(issued["credential"])


async def channel_ws(ctx: Ctx, name: str, *, credential: str | None = None, bootstrap: str | None = None,
                     caps: dict[str, Any] | None = None, endpoint: str | None = None) -> tuple[Any, dict[str, Any]]:
    ws = await ws_connect(endpoint or ctx.h.endpoint, max_size=1 << 20)
    ack = await hello(ws, name, credential=credential, bootstrap=bootstrap, caps=caps)
    assert ack["type"] == "hello_ack", f"握手失败：{json.dumps(ack, ensure_ascii=False)[:200]}"
    return ws, ack


async def replies(client: UmpClient, count: int, *, timeout: float = 20.0,
                  collect: list[Any] | None = None) -> list[Any]:
    got: list[Any] = []
    while len(got) < count:
        got.append(await client.expect(lambda e: e.type == "reply", timeout=timeout, collect=collect))
    return got


async def drain(client: UmpClient, seconds: float = 1.2) -> list[Any]:
    """收走这段时间内到达的全部信封：用于对回包顺序不敏感的断言。"""
    out: list[Any] = []
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while True:
        budget = end - loop.time()
        if budget <= 0:
            return out
        try:
            await client.expect(lambda e: False, timeout=min(0.4, budget), collect=out)
        except TimeoutError:
            continue


def pick(frames: Iterable[Any], env_type: str) -> Any:
    found = [e for e in frames if e.type == env_type]
    assert found, f"未收到 {env_type} 信封（实收 {[e.type for e in frames]}）"
    return found[-1]


async def barrier(client: UmpClient, seconds: float = 1.0) -> list[Any]:
    """顺序屏障：核心按帧序处理，pong 回来即说明之前的帧（如回执）已处理完。"""
    await client.send(ump.make("ping", {}))
    frames = await drain(client, seconds)
    assert any(e.type == "pong" for e in frames), "心跳未回：无法确认前序帧已处理"
    return frames


async def expect_none(client: UmpClient, env_type: str, seconds: float = 1.2) -> None:
    frames = await drain(client, seconds)
    assert not [e for e in frames if e.type == env_type], f"不该收到 {env_type} 信封"


@asynccontextmanager
async def slow(harness: Any, seconds: float):
    harness.fake.delay_s = seconds
    try:
        yield
    finally:
        harness.fake.delay_s = 0.0


def ci_id(harness: Any, name: str) -> str:
    row = harness.store.channel_by_name(name)
    assert row is not None, f"通道 {name} 未登记"
    return str(row["id"])


def flat_parts(row: dict[str, Any]) -> str:
    if row.get("parts"):
        return "\n".join(text for batch in json.loads(row["parts"]) for text in batch)
    return str(row.get("text") or "")


# ---------------------------------------------------------------- 条目


# 「1.0」之外的协议版本被拒并断连（CHANNEL_PLUGIN_SPEC §2.1 / §9）
async def e1(ctx: Ctx) -> str:
    ws = await ws_connect(ctx.h.endpoint)
    await send(ws, {"ump": "2.0", "type": "hello", "id": "e-v", "ts": 0.0, "payload": {}})
    env = await recv(ws)
    assert err_code(env) == ump.Err.PROTOCOL, env
    code = await expect_closed(ws)
    return f"ump=2.0 → error protocol_error（stage={env['payload']['stage']}）并关闭连接（close={code}）"


# 未知 type 报协议错误，不拿自由文本执行管理操作（§2.1）
async def e2(ctx: Ctx) -> str:
    cred = await credential_for(ctx, "audit-e2")
    ws, _ = await channel_ws(ctx, "audit-e2", credential=cred)
    before = ctx.h.store.counts()
    env = await expect_error(ws, ump.Err.UNSUPPORTED_TYPE,
                             ump.make("make_me_admin", {"op": "channel.ensure", "name": "audit-e2"}))
    assert ctx.h.store.counts() == before, "未知类型产生了副作用"
    assert "result" not in env and "ok" not in env, "不该返回管理面结果"
    await ws.close()
    return "type=make_me_admin → unsupported_type；通道数 / 消息数不变，无管理面结果回包"


# 坏帧不拖死连接 + 心跳（§2.1 / §2.3 / §六）
async def e3(ctx: Ctx) -> str:
    cred = await credential_for(ctx, "audit-e3")
    ws, _ = await channel_ws(ctx, "audit-e3", credential=cred)
    await ws.send("这不是 JSON {{")
    await expect_error(ws, ump.Err.BAD_FRAME)
    await send(ws, ump.make("ping", {}))
    pong = await recv(ws)
    assert pong["type"] == "pong", pong
    assert "thread" not in pong, "连接级信封不应要求 thread"
    await ws.close()
    return "非 JSON 帧 → bad_frame；同连接后续 ping 仍得 pong（连接级信封带 thread 非必需）"


# 字段类型 / 必填 / 枚举在解析期校验（§2.1 / §2.3）
async def e4(ctx: Ctx) -> str:
    cred = await credential_for(ctx, "audit-e4")
    ws, _ = await channel_ws(ctx, "audit-e4", credential=cred)
    cases: list[dict[str, Any]] = [
        {"ump": "1.0", "type": "ping", "id": "e-1", "ts": "now", "payload": {}},      # ts 非数字
        {"ump": "1.0", "type": "ping", "id": "e-2", "ts": 1.0, "payload": []},         # payload 非对象
        {"ump": "1.0", "type": "ping", "ts": 1.0, "payload": {}},                      # id 缺失
        ump.make("delivery", {"message_id": "m-1", "batch_index": 0, "state": "read"},
                 thread_id="dm-x", binding_token="bt-x"),                              # 枚举非法
    ]
    for frame in cases:
        await expect_error(ws, ump.Err.PROTOCOL, frame)
    await ws.close()
    return "ts 非数字 / payload 非对象 / id 缺失 / delivery.state 非法 均报 protocol_error（解析期拦截）"


# 方向强制：通道不能发核心 → 通道的类型（§2.3）
async def e5(ctx: Ctx) -> str:
    cred = await credential_for(ctx, "audit-e5")
    ws, _ = await channel_ws(ctx, "audit-e5", credential=cred)
    frame = ump.make("reply", {"message_id": "m-fake", "parts": [{"text": "我代表核心"}], "batch_count": 1}, thread_id="dm-x")
    await expect_error(ws, ump.Err.PROTOCOL, frame)
    await ws.close()
    return "通道侧发 reply → protocol_error（不会当成核心出站回复被接纳）"


# 消息 / 回执 / 重试必须带 thread 与 binding_token（§2.1 / §2.2）
async def e6(ctx: Ctx) -> str:
    cred = await credential_for(ctx, "audit-e6")
    ws, _ = await channel_ws(ctx, "audit-e6", credential=cred)
    cases = [
        ump.make("user_message", {"text": "hi", "binding_token": "bt-x"}),                      # 缺 thread
        ump.make("user_message", {"text": "hi"}, thread_id="dm-x"),                             # 缺 token
        ump.make("retry", {"ref": "e-1"}, thread_id="dm-x"),                                    # retry 缺 token
        ump.make("delivery", {"message_id": "m-1", "state": "accepted"}, thread_id="dm-x"),     # delivery 缺 token
    ]
    for frame in cases:
        await expect_error(ws, ump.Err.PROTOCOL, frame)
    await ws.close()
    return "user_message 缺 thread / 缺 token、retry 缺 token、delivery 缺 token 均报 protocol_error（不套用默认绑定）"


# UMP 不承载世界实例 / 时间线 / 角色内部标识（§一 UMP 只承载消息流）
async def e7(ctx: Ctx) -> str:
    ins, tl, ch = "ph-audit-inst", "tl-audit-7f", "ch-audit-9c"
    cred = await credential_for(ctx, "audit-noscope")
    session = (await ctx.mgmt.call("session.ensure", instance_id=ins, timeline_id=tl, character_id=ch))["session"]
    thread = (await ctx.mgmt.call("thread.bind", channel="audit-noscope", thread_id="dm-ns",
                                  session_id=session["id"]))["thread"]
    ws, ack = await channel_ws(ctx, "audit-noscope", credential=cred)
    await send(ws, ump.make("user_message", {"text": "在吗"}, thread_id="dm-ns",
                            binding_token=thread["binding_token"]))
    reply = await recv(ws)
    while reply["type"] != "reply":
        reply = await recv(ws)
    wire = json.dumps([ack, reply], ensure_ascii=False)
    leaked = [value for value in (ins, tl, ch) if value in wire]
    assert not leaked, f"UMP 线上出现内部标识：{leaked}"
    await ws.close()
    return "hello_ack 与 reply 信封均不含实例 / 时间线 / 角色标识（只有通道实例 id 与不透明令牌）"


# 引导凭据一次性（§2.5）
async def a1(ctx: Ctx) -> str:
    first = UmpClient(endpoint=ctx.h.endpoint, channel_id="audit-boot", name="audit-boot", bootstrap=ctx.h.bootstrap)
    ack = await first.connect()
    credential = ack.get("credential")
    assert credential and ack["state"] == "ready", ack
    await first.close()
    second = UmpClient(endpoint=ctx.h.endpoint, channel_id="audit-boot", name="audit-boot", bootstrap=ctx.h.bootstrap)
    try:
        await second.connect()
    except UmpError as exc:
        assert exc.code == ump.Err.AUTH_FAILED, exc.code
    else:
        raise AssertionError("引导凭据被重复使用")
    finally:
        await second.close()
    return "首连签发持久凭据；同一引导凭据复连 → auth_failed（消费后不可复用）"


# 错误凭据被拒（§2.2 / §2.5）
async def a2(ctx: Ctx) -> str:
    forged = UmpClient(endpoint=ctx.h.endpoint, channel_id="audit-forged", name="audit-forged",
                       credential="cr-forged")
    try:
        await forged.connect()
    except UmpError as exc:
        assert exc.code == ump.Err.AUTH_FAILED, exc.code
    else:
        raise AssertionError("伪造凭据通过了认证")
    finally:
        await forged.close()
    assert ctx.h.store.channel_by_name("audit-forged") is None, "认证失败不得登记通道实例"
    return "credential=cr-forged → auth_failed，且不留通道登记记录"


# 持久身份：重连同一通道实例，不重复签发（§2.2）
async def a3(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-persist", thread_id="dm-p")
    ack1 = info["ack"]
    again = UmpClient(endpoint=ctx.h.endpoint, channel_id="audit-persist", name="audit-persist",
                      credential=info["credential"])
    ack2 = await again.connect()
    try:
        assert ack2["channel_instance"] == ack1["channel_instance"], (ack1, ack2)
        assert "credential" not in ack2, "重连不该再签发凭据"
    finally:
        await again.close()
        await client.close()
    return f"持久凭据重连 → 同一 channel_instance={ack1['channel_instance']}，不重复签发"


# 自报通道身份 / 未持管理凭据都不能调用管理面（§2.2 / §五）
async def a4(ctx: Ctx) -> str:
    client, _info = await bind_thread(ctx.h, ctx.mgmt, channel_id="builtin", thread_id="dm-builtin")
    try:
        await client.send({"mgmt": "1", "op": "status", "id": "r-1", "args": {}})
        env = await client.expect(lambda e: e.type == "error")
        assert env.payload["code"] in (ump.Err.BAD_FRAME, ump.Err.PROTOCOL), env.payload
    finally:
        await client.close()
    forged = MgmtClient(ctx.h.endpoint, "mg-forged")
    try:
        await forged.connect()
    except UmpError as exc:
        assert exc.code == ump.Err.AUTH_FAILED, exc.code
    else:
        raise AssertionError("伪造管理令牌通过了认证")
    finally:
        await forged.close()
    return "hello 自报 channel.id=builtin 后发管理帧 → 错error（非管理结果）；伪造 mgmt 令牌 → auth_failed"


# 握手持久记录协商结果；断线不清除；重连不必再问管理面（§2.2）
async def a5(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-keep", thread_id="dm-keep",
                                     max_text_len=500)
    token = info["thread"]["binding_token"]
    await client.close()
    row = ctx.h.store.channel_get(str(info["thread"]["channel_id"]))
    assert row and row["protocol"] == "1.0" and json.loads(row["capabilities"])["max_text_len"] == 500, row
    again = UmpClient(endpoint=ctx.h.endpoint, channel_id="audit-keep", name="audit-keep",
                      credential=info["credential"], max_text_len=500)
    ack = await again.connect()
    try:
        threads = {t["id"]: t for t in ack["threads"]}
        assert threads["dm-keep"]["binding_token"] == token, ack["threads"]
        assert ack["negotiated"]["max_text_len"] == 500, ack["negotiated"]
    finally:
        await again.close()
    return f"断线后协商结果仍在库（protocol=1.0, max_text_len=500）；重连 hello_ack 回带 thread 令牌 {token}"


# 能力声明变化 → 重新握手重新确认交集（§2.2）
async def a6(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-nego", thread_id="dm-nego",
                                     max_text_len=50, max_parts=2)
    assert info["ack"]["negotiated"] == {"segments": True, "status": True, "max_text_len": 50, "max_parts": 2}, info["ack"]
    await client.close()
    again = UmpClient(endpoint=ctx.h.endpoint, channel_id="audit-nego", name="audit-nego",
                      credential=info["credential"], max_text_len=300, max_parts=7, segments=False)
    ack = await again.connect()
    try:
        assert ack["negotiated"] == {"segments": False, "status": True, "max_text_len": 300, "max_parts": 7}, ack["negotiated"]
    finally:
        await again.close()
    row = ctx.h.store.channel_by_name("audit-nego")
    assert json.loads(row["capabilities"])["max_text_len"] == 300, row
    return "50/2 → 300/7+segments=False：重连重新协商并覆盖持久记录（不沿用旧交集）"


# 认证 / 协议永久错误：拒绝 + 关闭，不降级为无认证（§六）
async def a7(ctx: Ctx) -> str:
    ws = await ws_connect(ctx.h.endpoint)
    env = await hello(ws, "audit-noauth", credential="cr-nope")
    assert err_code(env) == ump.Err.AUTH_FAILED, env
    assert env["payload"]["retryable"] is False and env["payload"]["stage"] == ump.Stage.AUTH, env["payload"]
    code = await expect_closed(ws)
    return f"错误凭据 → auth_failed(retryable=False, stage=auth) 并关闭连接（close={code}）；不降级为无认证"


# 幂等：同键同文重发只查既有结果，不重复生成（§2.2）
async def i1(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-idem", thread_id="dm-idem")
    token = info["thread"]["binding_token"]
    env = ump.make("user_message", {"text": "重复发送"}, thread_id="dm-idem", binding_token=token, id="e-dup")
    try:
        await client.send(env)
        first_round = await drain(client, 3.0)
        first = pick(first_round, "reply")
        assert pick(first_round, "accepted").payload["state"] in ("queued", "processing", "done")
        calls = len(ctx.h.fake.calls)
        rows = ctx.h.store.counts()["messages"]
        await client.send(env)  # 传输重发
        second_round = await drain(client, 3.0)
        accepted = pick(second_round, "accepted")
        again = [e for e in second_round if e.type == "reply"]
        inbound = ctx.h.store.inbound_find(ci_id(ctx.h, "audit-idem"), "dm-idem", "e-dup")
        facts = (
            f"重发后 accepted.state={accepted.payload['state']!r} message_id={accepted.payload['message_id']!r}，"
            f"重发 reply 信封 {len(again)} 条，入站行 message_id={inbound['message_id']!r} "
            f"reply_message_id={inbound['reply_message_id']!r}，"
            f"模型调用 {len(ctx.h.fake.calls)}（前 {calls}），消息行数 {ctx.h.store.counts()['messages']}（前 {rows}）"
        )
        assert accepted.payload["state"] == "done", facts
        assert accepted.payload["message_id"] == first.payload["message_id"], facts
        assert again and again[-1].payload["message_id"] == first.payload["message_id"], facts
        assert again[-1].payload["parts"] == first.payload["parts"], facts
        assert len(ctx.h.fake.calls) == calls and ctx.h.store.counts()["messages"] == rows, facts
    finally:
        await client.close()
    return f"重发同 id/env → accepted.state=done + 同一 message_id={first.payload['message_id']}，模型调用数与消息行数不变"


# 幂等：同键异文报冲突（§2.2）
async def i2(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-conflict", thread_id="dm-cf")
    token = info["thread"]["binding_token"]
    try:
        await client.send(ump.make("user_message", {"text": "原文"}, thread_id="dm-cf", binding_token=token, id="e-cf"))
        await client.expect(lambda e: e.type == "accepted")
        await replies(client, 1)
        calls = len(ctx.h.fake.calls)
        rows = ctx.h.store.counts()["messages"]
        await client.send(ump.make("user_message", {"text": "换了正文"}, thread_id="dm-cf", binding_token=token, id="e-cf"))
        env = await client.expect(lambda e: e.type == "error")
        assert env.payload["code"] == ump.Err.CONFLICT, env.payload
        assert len(ctx.h.fake.calls) == calls and ctx.h.store.counts()["messages"] == rows
    finally:
        await client.close()
    return "同 id 换正文 → conflict（既不静默丢弃也不二次执行，模型调用与消息行数不变）"


# 路由键 = 已认证通道实例 + thread，不同通道同 thread/id 不串线（§2.2）
async def i3(ctx: Ctx) -> str:
    a, info_a = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-iso-a", thread_id="dm-42")
    b, info_b = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-iso-b", thread_id="dm-42")
    try:
        rows = ctx.h.store.counts()["messages"]
        shared = {"text": "同一个 thread 与 id", "id": "e-shared"}
        await a.send(ump.make("user_message", {"text": shared["text"]}, thread_id="dm-42",
                             binding_token=info_a["thread"]["binding_token"], id=shared["id"]))
        await b.send(ump.make("user_message", {"text": shared["text"]}, thread_id="dm-42",
                             binding_token=info_b["thread"]["binding_token"], id=shared["id"]))
        ra = (await replies(a, 1))[0]
        rb = (await replies(b, 1))[0]
        assert ra.payload["message_id"] != rb.payload["message_id"], (ra.payload, rb.payload)
        assert ra.payload["covers"] == ["e-shared"] and rb.payload["covers"] == ["e-shared"]
        assert ctx.h.store.counts()["messages"] == rows + 4, "两通道未各自落库"
    finally:
        await a.close()
        await b.close()
    return f"同 thread/id 两通道各得独立回复（{ra.payload['message_id']} / {rb.payload['message_id']}），各自入站 + 出站共 4 行"


# 持久化后才确认接收（§2.2 / SESSION_CORE §4.2）
async def i4(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-ack", thread_id="dm-ack")
    token = info["thread"]["binding_token"]
    cid = ci_id(ctx.h, "audit-ack")
    try:
        await client.send(ump.make("user_message", {"text": "持久化了吗"}, thread_id="dm-ack",
                                   binding_token=token, id="e-ack"))
        accepted = await client.expect(lambda e: e.type == "accepted")
        row = ctx.h.store.inbound_find(cid, "dm-ack", "e-ack")
        assert row is not None, "收到 accepted 时入站行尚不存在"
        assert row["state"] in ("queued", "processing", "done"), row["state"]
        assert accepted.payload["ref"] == "e-ack", accepted.payload
        await replies(client, 1)
    finally:
        await client.close()
    return f"accepted 到达时库里已有该行（state={row['state']}），确认在持久化之后"


# 同一会话多 thread 串行（核心接受顺序），回复只回来源 thread（§2.2 / SESSION_CORE §3）
async def i5(ctx: Ctx) -> str:
    kw = {"instance": "ph-audit-serial", "timeline": "main", "character": "ph-audit-c"}
    wa, info_a = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-serial", thread_id="dm-a", **kw)
    wb, info_b = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-serial2", thread_id="dm-b", **kw)
    stamps: list[float] = []
    original = ctx.h.fake.chat

    async def timed(messages: Any, **kwargs: Any) -> Any:
        stamps.append(asyncio.get_running_loop().time())
        return await original(messages, **kwargs)

    try:
        ctx.h.fake.chat = timed  # 生成调用打点：同一会话的两轮不得在时间上重叠
        async with slow(ctx.h, 0.5):
            await wa.send(ump.make("user_message", {"text": "第一条"}, thread_id="dm-a",
                                   binding_token=info_a["thread"]["binding_token"], id="e-s1"))
            await wb.send(ump.make("user_message", {"text": "第二条"}, thread_id="dm-b",
                                   binding_token=info_b["thread"]["binding_token"], id="e-s2"))
            ra = await wa.expect(lambda e: e.type == "reply", timeout=20)
            rb = await wb.expect(lambda e: e.type == "reply", timeout=20)
    finally:
        ctx.h.fake.chat = original
        await wa.close()
        await wb.close()
    order = [ra.thread_id, rb.thread_id]
    assert order == ["dm-a", "dm-b"], f"回复未按接受顺序到达：{order}"
    assert len(stamps) == 2, f"本条应有两次生成调用，实际 {len(stamps)}"
    gap = stamps[1] - stamps[0]
    assert gap >= 0.5, f"同一会话两轮生成在时间上重叠（间隔 {gap:.2f}s < 单轮耗时 0.5s）"
    hist = ctx.h.store.context_window(str(info_a["session"]["id"]), 20)
    roles = [r["role"] for r in hist]
    texts = [flat_parts(r) for r in hist]
    assert roles == ["user", "user", "character", "character"], (roles, texts)
    assert texts == ["第一条", "第二条", SHORT, SHORT], texts
    return (f"两条入站按接受顺序串行（两轮生成互不重叠，间隔 {gap:.2f}s ≥ 单轮 0.5s），"
            f"回复分别只回 {order[0]} / {order[1]}；会话历史 4 行与两条入站 + 各自回复一致（无覆盖）")


# 未绑定 thread 返回明确错误，不自动选默认角色（§2.3 / SESSION_CORE §2.3）
async def b1(ctx: Ctx) -> str:
    client, _info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-unknown", thread_id="dm-1")
    try:
        rows = ctx.h.store.counts()["messages"]
        await client.send_user_message(thread_id="dm-nothing", binding_token="bt-x", text="有人吗")
        env = await client.expect(lambda e: e.type == "error")
        assert env.payload["code"] == ump.Err.UNKNOWN_THREAD, env.payload
        assert ctx.h.store.counts()["messages"] == rows, "未绑定 thread 的消息被落库"
    finally:
        await client.close()
    return "未绑定 thread → unknown_thread，且不落库、不改投其它会话"


# 旧令牌的未接收消息被拒（§2.2）
async def b2(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-stale", thread_id="dm-stale")
    stale = info["thread"]["binding_token"]
    try:
        await ctx.mgmt.call("thread.bind", channel="audit-stale", thread_id="dm-stale",
                            session_id=info["session"]["id"])
        rows = ctx.h.store.counts()["messages"]
        await client.send_user_message(thread_id="dm-stale", binding_token=stale, text="旧令牌")
        env = await client.expect(lambda e: e.type == "error")
        assert env.payload["code"] == ump.Err.BINDING_EXPIRED, env.payload
        assert ctx.h.store.counts()["messages"] == rows, "旧令牌消息被意外接受 / 排队"
    finally:
        await client.close()
    return f"换代后旧令牌 {stale} → binding_expired，未接受未排队（消息行数不变）"


# 重绑使迟到结果作废；重发返回取消，不重新灌入新状态（§2.2 / SESSION_CORE §4.3）
async def b3(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-void", thread_id="dm-void")
    token = info["thread"]["binding_token"]
    cid = ci_id(ctx.h, "audit-void")
    try:
        async with slow(ctx.h, 0.6):
            await client.send(ump.make("user_message", {"text": "正在生成时被重绑"}, thread_id="dm-void",
                                       binding_token=token, id="e-void"))
            await asyncio.sleep(0.2)
            rebound = await ctx.mgmt.call("thread.bind", channel="audit-void", thread_id="dm-void",
                                          session_id=info["session"]["id"])
            await asyncio.sleep(1.6)
        row = ctx.h.store.inbound_find(cid, "dm-void", "e-void")
        assert row is not None and row["state"] == "cancelled", row
        calls, rows = len(ctx.h.fake.calls), ctx.h.store.counts()["messages"]
        await drain(client, 0.6)  # 清掉第一阶段遗留的 accepted / status
        await client.send(ump.make("user_message", {"text": "正在生成时被重绑"}, thread_id="dm-void",
                                   binding_token=rebound["thread"]["binding_token"], id="e-void"))
        resent = await drain(client, 2.0)
        accepted = pick(resent, "accepted")
        assert accepted.payload["state"] == "cancelled", accepted.payload
        assert not [e for e in resent if e.type == "reply"], "作废输入被重新执行并产出回复"
        assert len(ctx.h.fake.calls) == calls and ctx.h.store.counts()["messages"] == rows, "作废输入被重新执行"
    finally:
        await client.close()
    return "重绑后迟到结果标 cancelled 不投递；同 id 重发返回 state=cancelled，不重跑模型也不新增行"


# 回执：按原出站标识更新原记录；unknown 不等于成功、迟到回执不倒退（§2.3）
async def d1(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-dlv", thread_id="dm-dlv")
    token = info["thread"]["binding_token"]
    try:
        await client.send_user_message(thread_id="dm-dlv", binding_token=token, text="在吗")
        reply = (await replies(client, 1))[0]
        mid = reply.payload["message_id"]
        seq = ctx.h.store.outbound_by_message_id(mid)["seq"]
        assert reply.payload["batch_count"] == 1
        await client.report_delivery(thread_id="dm-dlv", binding_token=token, message_id=mid, batch_index=0, state="unknown")
        frames = await barrier(client)
        assert not [e for e in frames if e.type == "error"], [e.payload for e in frames if e.type == "error"]
        rollup_unknown = ctx.h.store.delivery_rollup(seq)
        assert rollup_unknown == "unknown", rollup_unknown
        await client.report_delivery(thread_id="dm-dlv", binding_token=token, message_id=mid, batch_index=0, state="accepted")
        await barrier(client)
        assert ctx.h.store.delivery_rollup(seq) == "delivered", "全部批次 accepted 才 delivered"
        await client.report_delivery(thread_id="dm-dlv", binding_token=token, message_id=mid, batch_index=0, state="unknown")
        await barrier(client)
        assert ctx.h.store.delivery_rollup(seq) == "delivered", "迟到 unknown 倒退了已确认状态"
    finally:
        await client.close()
    return "回执写入原投递记录：unknown→unknown（不称成功）、accepted→delivered、迟到 unknown 不倒退"


# 换绑后的令牌不能作用于旧投递记录（§2.3）
async def d2(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-dlv2", thread_id="dm-dlv2")
    token = info["thread"]["binding_token"]
    try:
        await client.send_user_message(thread_id="dm-dlv2", binding_token=token, text="在吗")
        reply = (await replies(client, 1))[0]
        mid = reply.payload["message_id"]
        seq = ctx.h.store.outbound_by_message_id(mid)["seq"]
        before = ctx.h.store.delivery_rollup(seq)
        rebound = await ctx.mgmt.call("thread.bind", channel="audit-dlv2", thread_id="dm-dlv2",
                                      session_id=info["session"]["id"])
        await client.report_delivery(thread_id="dm-dlv2", binding_token=rebound["thread"]["binding_token"],
                                     message_id=mid, batch_index=0, state="accepted")
        env = await client.expect(lambda e: e.type == "error")
        assert env.payload["code"] == ump.Err.BINDING_EXPIRED, env.payload
        assert ctx.h.store.delivery_rollup(seq) == before, "旧记录被新令牌改动"
    finally:
        await client.close()
    return f"换代后的令牌补回执 → binding_expired，原记录 rollup 仍为 {before}"


# 重连补投：生成完成后断线，重连补投同一固化回复、不重跑模型（§2.3 / §2.2 / SESSION_CORE §4.2）
async def r1(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-resend", thread_id="dm-rs")
    token = info["thread"]["binding_token"]
    cid = ci_id(ctx.h, "audit-resend")
    async with slow(ctx.h, 0.7):
        await client.send_user_message(thread_id="dm-rs", binding_token=token, text="断线期间生成")
        await client.close()          # 生成尚未完成时断线
        await asyncio.sleep(1.8)      # 生成完成；无连接 → 投递保持未确认
    pending = ctx.h.store.pending_outbound(cid, "dm-rs", limit=5)
    assert len(pending) == 1, f"应有 1 条待投递固化回复，实际 {len(pending)}"
    mid = pending[0]["message_id"]
    calls = len(ctx.h.fake.calls)
    rows = ctx.h.store.counts()["messages"]
    again = UmpClient(endpoint=ctx.h.endpoint, channel_id="audit-resend", name="audit-resend",
                      credential=info["credential"])
    ack = await again.connect()
    try:
        assert ack["state"] == "ready"
        env = await again.expect(lambda e: e.type == "reply", timeout=10)
        assert env.payload["message_id"] == mid, (mid, env.payload)
        assert len(ctx.h.fake.calls) == calls, "补投重新调用了模型"
        assert ctx.h.store.counts()["messages"] == rows, "补投新增了消息行"
    finally:
        await again.close()
    return f"重连后补投同一 message_id={mid}（正文沿用固化产物），模型调用数与消息行数不变"


# 显式 retry(outbound)：只重发固化结果，回执以 accepted 枚举回给通道（§2.3）
async def r2(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-retry", thread_id="dm-rt")
    token = info["thread"]["binding_token"]
    cid = ci_id(ctx.h, "audit-retry")
    async with slow(ctx.h, 0.7):
        await client.send_user_message(thread_id="dm-rt", binding_token=token, text="重发这条")
        await client.close()
        await asyncio.sleep(1.8)
    mid = ctx.h.store.pending_outbound(cid, "dm-rt", limit=5)[0]["message_id"]
    calls = len(ctx.h.fake.calls)
    ws, _ack = await channel_ws(ctx, "audit-retry", credential=info["credential"])
    try:
        redelivered = [f for f in await recv_all(ws, 2.5) if f.get("type") == "reply"]  # 重连本就会补投一次
        assert redelivered and redelivered[-1]["payload"]["message_id"] == mid, (mid, redelivered)
        calls_after = len(ctx.h.fake.calls)
        await send(ws, ump.make("retry", {"ref": mid, "kind": "outbound"}, thread_id="dm-rt", binding_token=token))
        after = await recv_all(ws, 2.5)  # 裸帧：不经客户端解析，保留线上原始形态
    finally:
        await ws.close()
    kinds = [f.get("type") for f in after]
    accepted = [f for f in after if f.get("type") == "accepted"]
    resent = [f for f in after if f.get("type") == "reply"]
    facts = (
        f"retry(outbound) 后核心回帧 {kinds}；accepted.state={[f['payload'].get('state') for f in accepted]!r}"
        f"（ump.ACCEPT_STATES={sorted(ump.ACCEPT_STATES)}）；"
        f"reply.message_id={[f['payload'].get('message_id') for f in resent]!r}（原 {mid}）；"
        f"模型调用 {len(ctx.h.fake.calls)}（重连后 {calls_after} / retry 前 {calls}）"
    )
    assert resent and all(f["payload"]["message_id"] == mid for f in resent), facts
    assert len(ctx.h.fake.calls) == calls_after == calls, facts
    assert accepted, facts
    assert accepted[-1]["payload"]["state"] in ump.ACCEPT_STATES, facts
    return (f"retry(kind=outbound, ref={mid}) → 只重发同一 message_id 的固化结果（模型未再被调用），"
            f"accepted.state={accepted[-1]['payload']['state']!r} 属合法枚举")


# 错误模型：code / message / retryable / ref / stage 齐备且脱敏（§六）
async def x1(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-errshape", thread_id="dm-err")
    token = info["thread"]["binding_token"]
    try:
        await client.send_user_message(thread_id="dm-none", binding_token="bt-x", text="错误信封形状")
        env = await client.expect(lambda e: e.type == "error")
        payload = env.payload
        assert set(payload) <= {"code", "message", "retryable", "ref", "stage"}, payload
        assert payload["code"] == ump.Err.UNKNOWN_THREAD and isinstance(payload["message"], str) and payload["message"]
        assert payload["retryable"] is False and payload["stage"] == ump.Stage.RECEIVE, payload
        blob = json.dumps(env.raw, ensure_ascii=False)
        assert "你是 isekai 核心进程的占位对话端" not in blob, "错误里泄漏了提示词"
        # 内部异常也不得外泄细节（脱敏）
        ctx.h.fake.fail_with = RuntimeError("secret-internal-detail: 数据库连接串")
        try:
            await client.send_user_message(thread_id="dm-err", binding_token=token, text="内部错误")
            env2 = await client.expect(lambda e: e.type == "error", timeout=15)
        finally:
            ctx.h.fake.fail_with = None
        assert env2.payload["code"] == ump.Err.INTERNAL, env2.payload
        assert "secret-internal-detail" not in json.dumps(env2.raw, ensure_ascii=False), "内部细节外泄"
    finally:
        await client.close()
    return "错误信封仅含 code/message/retryable/ref/stage（stage 属固定枚举）；提示词与内部异常细节均不外泄"


# 生成失败保留输入与失败状态；显式 retry 恢复同一轮次（§2.3 / §六 / SESSION_CORE §4.3）
async def x2(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-genfail", thread_id="dm-gf")
    token = info["thread"]["binding_token"]
    cid = ci_id(ctx.h, "audit-genfail")
    try:
        ctx.h.fake.fail_with = LLMError("llm_unavailable", "HTTP 503", retryable=True)
        try:
            await client.send(ump.make("user_message", {"text": "会失败的一轮"}, thread_id="dm-gf",
                                       binding_token=token, id="e-gf"))
            env = await client.expect(lambda e: e.type == "error", timeout=15)
        finally:
            ctx.h.fake.fail_with = None
        assert env.payload["code"] == "llm_unavailable" and env.payload["retryable"] is True, env.payload
        assert env.payload["stage"] == ump.Stage.GENERATE and env.payload["ref"] == "e-gf", env.payload
        row = ctx.h.store.inbound_find(cid, "dm-gf", "e-gf")
        assert row["state"] == "failed" and row["error_code"] == "llm_unavailable", row
        calls = len(ctx.h.fake.calls)
        await client.request_retry(thread_id="dm-gf", binding_token=token, ref="e-gf", kind="input")
        accepted = await client.expect(lambda e: e.type == "accepted")
        assert accepted.payload["state"] in ("processing", "queued"), accepted.payload
        reply = (await replies(client, 1))[0]
        assert reply.payload["reply_to"] == "e-gf", reply.payload
        assert len(ctx.h.fake.calls) == calls + 1, "retry 未恢复同一轮次"
        again = ctx.h.store.inbound_find(cid, "dm-gf", "e-gf")
        assert again["seq"] == row["seq"] and again["state"] == "done", again
    finally:
        await client.close()
    return "生成失败 → error(llm_unavailable, retryable, stage=generate)，输入保留 failed；retry 在同一行 seq 上恢复并成功"


# 空回复不算成功（§六 / SESSION_CORE §4.2）
async def x3(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-empty", thread_id="dm-em")
    token = info["thread"]["binding_token"]
    cid = ci_id(ctx.h, "audit-empty")
    try:
        ctx.h.fake.replies.append("")
        try:
            await client.send(ump.make("user_message", {"text": "空回复"}, thread_id="dm-em",
                                       binding_token=token, id="e-em"))
            env = await client.expect(lambda e: e.type == "error", timeout=15)
        finally:
            ctx.h.fake.replies.append(SHORT)
        assert env.payload["code"] == ump.Err.GENERATION_FAILED and env.payload["retryable"] is True, env.payload
        assert env.payload["stage"] == ump.Stage.GENERATE, env.payload
        row = ctx.h.store.inbound_find(cid, "dm-em", "e-em")
        assert row["state"] == "failed", row
        await expect_none(client, "reply", 0.8)
    finally:
        await client.close()
    return "空文本 → generation_failed（可重试，stage=generate），输入标 failed，不产生空回复"


# 连接卫生：连续协议错误达上限断连（§3.2 / §六）
async def x4(ctx: Ctx) -> str:
    cred = await credential_for(ctx, "audit-errlimit")
    ws, _ = await channel_ws(ctx, "audit-errlimit", credential=cred)
    for i in range(5):
        await send(ws, {"ump": "1.0", "type": "ping", "id": f"e-bad{i}"})
    seen = 0
    for _ in range(5):
        env = await recv(ws)
        if env.get("type") == "error":
            seen += 1
    assert seen == 5, f"应有 5 条错误回包，实际 {seen}"
    await expect_closed(ws)
    return "5 次协议错误（PROTOCOL_ERROR_LIMIT=5）后连接被关闭，坏帧不会被无限接纳"


# 连接卫生：超大帧直接断开（版 version.py:34 MAX_FRAME_BYTES）
async def x5(ctx: Ctx) -> str:
    cred = await credential_for(ctx, "audit-bigframe")
    ws, _ = await channel_ws(ctx, "audit-bigframe", credential=cred)
    await ws.send("x" * ((1 << 20) + 64))
    code = await expect_closed(ws, timeout=8)
    return f"1 MiB+ 帧 → 连接被断开（close={code}），不进解析"


# 连接卫生：握手有有限超时（version.py:36 HANDSHAKE_TIMEOUT_S=10）
async def x6(ctx: Ctx) -> str:
    ws = await ws_connect(ctx.h.endpoint)
    started = asyncio.get_running_loop().time()
    code = await expect_closed(ws, timeout=14)
    elapsed = asyncio.get_running_loop().time() - started
    assert 8.0 <= elapsed <= 13.5, f"握手超时耗时 {elapsed:.1f}s，不符 10s 有界超时"
    return f"不发首帧的连接在 {elapsed:.1f}s 后由核心关闭（close={code}），不无限占用"


# 能力协商：限额取双方交集且为有效正值（§2.4）
async def c1(ctx: Ctx) -> str:
    small, info_s = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-c1a", thread_id="dm-c1a",
                                      max_text_len=50, max_parts=3)
    big, info_b = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-c1b", thread_id="dm-c1b",
                                    max_text_len=999999, max_parts=999999)
    try:
        assert info_s["ack"]["negotiated"]["max_text_len"] == 50, info_s["ack"]["negotiated"]
        assert info_s["ack"]["negotiated"]["max_parts"] == 3, info_s["ack"]["negotiated"]
        assert info_b["ack"]["negotiated"]["max_text_len"] == 4000, info_b["ack"]["negotiated"]
        assert info_b["ack"]["negotiated"]["max_parts"] == 10, info_b["ack"]["negotiated"]
    finally:
        await small.close()
        await big.close()
    return "客户端 50/3 → 50/3；客户端 999999 → 核心上界 4000/10（min 交集，全为有效正值）"


# 超出协商长度的入站被拒且不落库（§2.4 / §2.1）
async def c2(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-c2", thread_id="dm-c2", max_text_len=30)
    token = info["thread"]["binding_token"]
    try:
        rows = ctx.h.store.counts()["messages"]
        await client.send_user_message(thread_id="dm-c2", binding_token=token, text="字" * 31)
        env = await client.expect(lambda e: e.type == "error")
        assert env.payload["code"] == ump.Err.PROTOCOL, env.payload
        assert ctx.h.store.counts()["messages"] == rows, "超限输入被落库"
    finally:
        await client.close()
    return "协商 max_text_len=30 时发 31 字 → protocol_error；不落库、不进入生成"


# 分段：超长回复完整、有序、不丢尾（§2.4）
async def c3(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.hb, ctx.mgmt_b, channel_id="audit-batch", thread_id="dm-batch",
                                     segments=False, max_text_len=30, max_parts=5)
    token = info["thread"]["binding_token"]
    try:
        await client.send_user_message(thread_id="dm-batch", binding_token=token, text="分段看看")
        seen: list[Any] = []
        first = await client.expect(lambda e: e.type == "reply", timeout=15, collect=seen)
        got = [first]
        while len(got) < first.payload["batch_count"]:
            got.append(await client.expect(lambda e: e.type == "reply", timeout=15, collect=seen))
        indices = [e.payload["batch_index"] for e in got]
        counts = {e.payload["batch_count"] for e in got}
        ids = {e.payload["message_id"] for e in got}
        parts = [p["text"] for e in got for p in e.payload["parts"]]
        assert indices == list(range(len(got))), indices
        assert counts == {len(got)} and len(ids) == 1, (counts, ids)
        assert len(got) > 1, "长回复未被分批"
        assert all(len(p) <= 30 for p in parts), [len(p) for p in parts]
        assert "".join(parts) == LONG.replace("\n", ""), "分批丢失 / 重排了正文"
    finally:
        await client.close()
    return f"segments=False 时每批一段、共 {len(got)} 批有序（indices={indices}，batch_count 一致），拼接后正文完整无丢尾"


# 新协商能力不足以承载既有分段：只报能力不兼容，不重排 / 裁剪 / 重生成（§2.4）
async def c4(ctx: Ctx) -> str:
    client, info = await bind_thread(ctx.hb, ctx.mgmt_b, channel_id="audit-incompat", thread_id="dm-in",
                                     segments=True, max_text_len=4000)
    token = info["thread"]["binding_token"]
    try:
        await client.send_user_message(thread_id="dm-in", binding_token=token, text="换能力")
        reply = (await replies(client, 1))[0]
        mid = reply.payload["message_id"]
        before = ctx.hb.store.outbound_by_message_id(mid)["parts"]
        await client.close()
        again = UmpClient(endpoint=ctx.hb.endpoint, channel_id="audit-incompat", name="audit-incompat",
                          credential=info["credential"], max_text_len=20, segments=True)
        await again.connect()
        try:
            env = await again.expect(lambda e: e.type == "error", timeout=10)
            assert env.payload["code"] == ump.Err.UNSUPPORTED_CAPABILITY, env.payload
            assert env.payload["stage"] == ump.Stage.DELIVERY, env.payload
        finally:
            await again.close()
        after = ctx.hb.store.outbound_by_message_id(mid)["parts"]
        assert after == before, "既有分段计划被重排 / 裁剪"
        rollup = ctx.hb.store.delivery_rollup(ctx.hb.store.outbound_by_message_id(mid)["seq"])
        assert rollup == "incompatible", rollup
    finally:
        pass
    return f"限额缩到 20 后重连 → unsupported_capability（stage=delivery），固化分段与历史未改，rollup={rollup}"


# 操作状态只发给声明该能力的通道（§2.3 / §2.4）
async def c5(ctx: Ctx) -> str:
    off, info_off = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-stat-off", thread_id="dm-so",
                                      status=False)
    on, info_on = await bind_thread(ctx.h, ctx.mgmt, channel_id="audit-stat-on", thread_id="dm-sn",
                                    status=True)
    try:
        async with slow(ctx.h, 0.4):
            await off.send_user_message(thread_id="dm-so", binding_token=info_off["thread"]["binding_token"],
                                        text="不支持状态")
            collected: list[Any] = []
            await off.expect(lambda e: e.type == "reply", timeout=15, collect=collected)
            assert all(e.type != "status" for e in collected), [e.type for e in collected]
            await on.send_user_message(thread_id="dm-sn", binding_token=info_on["thread"]["binding_token"],
                                       text="支持状态")
            got: list[Any] = []
            await on.expect(lambda e: e.type == "reply", timeout=15, collect=got)
            states = [e.payload["state"] for e in got if e.type == "status"]
            assert "thinking" in states, states
    finally:
        await off.close()
        await on.close()
    return f"status=False 通道只收 accepted/reply；status=True 通道收到 thinking/idle（实测 states={states}）"


# ---------------------------------------------------------------- 登记

CheckFn = Callable[[Ctx], Awaitable[str]]
CHECKS: list[tuple[str, str, str, CheckFn]] = [
    ("E1", "信封：非 1.x 协议版本被拒并断连", "isekai_core/ump.py:255-257 + channel.py:143-156", e1),
    ("E2", "信封：未知 type 报 unsupported_type 且无副作用", "isekai_core/ump.py:259-262", e2),
    ("E3", "信封：坏帧不拖死连接；ping/pong 心跳", "channel.py:246-262 · ump.py:24-28, 60-65", e3),
    ("E4", "信封：字段类型 / 必填 / 枚举解析期校验", "isekai_core/ump.py:238-291, 177-235", e4),
    ("E5", "信封：方向强制（通道不能发核心类型）", "isekai_core/ump.py:260-264", e5),
    ("E6", "信封：thread 与 binding_token 必填", "isekai_core/ump.py:271-284", e6),
    ("E7", "信封：不承载实例 / 时间线 / 角色内部标识", "channel.py:197-217 · session.py:375-388", e7),
    ("A1", "认证：引导凭据一次性", "channel.py:226-239 · store.py:754-813", a1),
    ("A2", "认证：错误凭据被拒且不登记实例", "channel.py:240-243 · store.py:815-821", a2),
    ("A3", "身份：持久凭据重连同一通道实例", "channel.py:213-217 · store.py:829-835", a3),
    ("A4", "权限：自报 builtin / 伪造管理令牌都不能进管理面", "channel.py:340-345, 372-383", a4),
    ("A5", "身份：握手回带 thread 令牌；断线不清协商结果", "channel.py:202-221 · store.py:829-835", a5),
    ("A6", "协商：能力变化重新握手重新确认交集", "channel.py:52-59, 179-195", a6),
    ("A7", "认证：永久错误拒绝 + 关闭，不降级无认证", "channel.py:185-188, 224-244", a7),
    ("I1", "幂等：同键同文重发只查既有结果", "session.py:92-119 · ump.py:283-284", i1),
    ("I2", "幂等：同键异文报 conflict", "session.py:101-108 · store.py:919-922", i2),
    ("I3", "幂等：不同通道同 thread/id 不串线", "channel.py:300-308 · store.py:903-931", i3),
    ("I4", "接受：持久化之后才确认接收", "session.py:92-112", i4),
    ("I5", "顺序：同一会话多 thread 串行、回复只回来源 thread", "session.py:161-170, 375-388", i5),
    ("B1", "绑定：未绑定 thread 报明确错误", "channel.py:300-308 · session.py:81-108", b1),
    ("B2", "绑定：旧令牌的未接收消息被拒", "channel.py:305-308", b2),
    ("B3", "绑定：重绑使迟到结果作废；重发返回取消", "session.py:210-224, 87-90 · store.py:1545-1558", b3),
    ("D1", "回执：按原出站标识更新；unknown 不倒退已确认", "channel.py:347-368 · store.py:1059-1092", d1),
    ("D2", "回执：换代令牌不能作用于旧投递记录", "channel.py:352-356", d2),
    ("R1", "重连补投：同一固化回复、不重跑模型", "channel.py:220-221 · session.py:418-424", r1),
    ("R2", "重试：outbound retry 只重发固化结果", "session.py:121-137, 358-400", r2),
    ("X1", "错误模型：五字段齐备且脱敏", "ump.py:67-95, 331-340 · session.py:430-432", x1),
    ("X2", "错误模型：生成失败保留输入状态，retry 恢复同轮次", "session.py:182-199, 139-152", x2),
    ("X3", "错误模型：空回复不算成功", "session.py:201-208 · llm.py:107-112", x3),
    ("X4", "连接卫生：协议错误达上限断连", "channel.py:255-261 · version.py:35", x4),
    ("X5", "连接卫生：超大帧直接断开", "channel.py:90-97 · version.py:34", x5),
    ("X6", "连接卫生：握手有限超时", "version.py:36 · channel.py:143-146", x6),
    ("C1", "协商：限额取交集且为有效正值", "channel.py:52-59 · ump.py:136-139, 157-160", c1),
    ("C2", "协商：超限入站被拒且不落库", "channel.py:250-254 · ump.py:180-185", c2),
    ("C3", "分段：超长回复完整有序分批不丢尾", "session.py:30-61, 358-400", c3),
    ("C4", "分段：能力变更只报不兼容，不重排 / 裁剪 / 重生成", "session.py:365-374, 402-416", c4),
    ("C5", "能力：status 只发给声明支持者", "channel.py:129-136 · session.py:426-428", c5),
]

#: 按设计后置 / 未实现（SPEC 分期条款），不计 FAIL
DEFERRED: list[tuple[str, str, str]] = [
    ("P1", "插件宿主：子进程 + stdio NDJSON、manifest、生命周期与隔离", 
     "CHANNEL_PLUGIN_SPEC §三 / §七「随外部通道需求后置」；代码中无 manifest 扫描、stdio 承载与进程生命周期实现（isekai_core 无插件模块）"),
    ("P2", "主动消息投递：reply_to=null、唯一目标、素材配额与不补发历史洪峰",
     "CHANNEL_PLUGIN_SPEC §2.3 主动消息 / SESSION_CORE_SPEC §5.3；会话核心只固化对入站的回复（session.py:226-240 covers 恒为入站 id），无主动产物与投递路径"),
    ("P3", "合并回复（多入一回）：一条回复覆盖批内多条输入、批内任一条查同批结果",
     "CHANNEL_PLUGIN_SPEC §2.3 合并回复 / SESSION_CORE_SPEC §4.5；session.py:233 covers 恒为单条，无睡眠等待与批合并（§4.5 属阶段 3）"),
    ("P4", "初次联络独立开场：reply_to=null 的单次例外与一次性资格",
     "SESSION_CORE_SPEC §5.6（随实例 / 角色卡接入）；无开场登记与固化路径"),
    ("P5", "system_notice 生成：归档说明 / 最后联络，且不进角色记忆提取",
     "CHANNEL_PLUGIN_SPEC §2.3 与 §十.10 / SESSION_CORE_SPEC §5.7；ump.py:41,233-235 已能解析与分类，但无生成方"),
    ("P6", "安卓内建通道与进程内传输（不强制复制桌面 WS）",
     "CHANNEL_PLUGIN_SPEC §2.5「实际路线待评估」+ §七 更后置"),
]


async def main() -> int:
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))

    root_a, root_b = tempfile.mkdtemp(prefix="audit-ump-a-"), tempfile.mkdtemp(prefix="audit-ump-b-")
    async with running_core(Path(root_a), replies=[SHORT]) as h:
        async with running_core(Path(root_b), replies=[LONG]) as hb:
            ctx = Ctx(h, hb, await open_mgmt(h), await open_mgmt(hb))
            print(f"# UMP 行为探针：核心 A={h.endpoint} 核心 B={hb.endpoint}（临时库 {root_a} / {root_b}）", flush=True)
            for code, summary, impl, fn in CHECKS:
                if only and code not in only and "ALL" not in only:
                    continue
                try:
                    detail = await fn(ctx)
                except Exception as exc:  # noqa: BLE001 —— 逐条隔离，单项失败不影响其余条目
                    emit("FAIL", code, summary,
                         f"{type(exc).__name__}: {exc}；最小复现：{sys.executable} {PROBE} --only {code}；可疑处 {impl}")
                else:
                    emit("PASS", code, summary, f"{detail}；实现 {impl}")
            if not only:
                for code, summary, why in DEFERRED:
                    emit("DEFERRED", code, summary, why)
            await ctx.mgmt.close()
            await ctx.mgmt_b.close()

    passed = sum(1 for row in RESULTS if row[0] == "PASS")
    failed = sum(1 for row in RESULTS if row[0] == "FAIL")
    deferred = sum(1 for row in RESULTS if row[0] == "DEFERRED")
    print(f"TOTAL {passed + failed + deferred} PASS {passed} FAIL {failed} DEFERRED {deferred}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
