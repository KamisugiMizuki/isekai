#!/usr/bin/env python
"""CHANNEL_PLUGIN_SPEC（通道插件层 / UMP）行为级审计探针 —— 第二轮，独立实现。

只读项目代码：本脚本不修改任何项目文件；全部存储落 tempfile 临时目录；
LLM 一律 FakeLLM；网络只用本地回环 WS。每条目独立 try/except 隔离。

用法：.venv/Scripts/python.exe scripts/_audit2_chan.py [--only CODE,CODE]
输出：每行 `<STATUS> <CODE> <摘要> — <证据>`，末行 `TOTAL n PASS p FAIL f DEFERRED d`。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from websockets.asyncio.client import connect as ws_connect  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

from isekai_core import plugins  # noqa: E402
from isekai_core import ump  # noqa: E402
from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.client import MgmtClient, UmpClient  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM, LLMError  # noqa: E402
from isekai_core.local_channel import InProcessChannel  # noqa: E402
from isekai_core.log import setup_logging  # noqa: E402
from isekai_core.ump import UmpError  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402

PROBE = "scripts/_audit2_chan.py"
MARK = "AUDIT2LEAK"  # 正文标记：检查日志 / 导出包是否泄漏
SHORT = "收到。"
#: 样本角色睡眠块在世界日首 [0, 25200)：DAY*1500 在睡眠块内，+30000 已是白天
SLEEP_AT = DAY * 1500
AWAKE_AT = DAY * 1500 + 30000

RESULTS: list[tuple[str, str, str, str]] = []


def emit(status: str, code: str, summary: str, evidence: str) -> None:
    RESULTS.append((status, code, summary, evidence))
    print(f"{status} {code} {summary} — {evidence}", flush=True)


# ------------------------------------------------------------------ 脚手架


def write_config(root: Path, **runtime: Any) -> None:
    folder = root / "config"
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["runtime:"]
    for key, value in runtime.items():
        lines.append(f"  {key}: {value}")
    (folder / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass
class Harness:
    cfg: Any
    runtime: Any
    fake: FakeLLM
    endpoint: str
    bootstrap: str
    mgmt_token: str
    log_file: Path

    @property
    def store(self) -> Any:
        return self.runtime.store

    @property
    def world(self) -> Any:
        return self.runtime.world


@asynccontextmanager
async def core(root: Path, *, replies: list[str] | None = None, state: str = "ready", logs: bool = False):
    """真核心：真 WS + 真 SQLite（临时目录），只换 LLM。"""
    write_config(root, sleep_wait_min_s=0.25, sleep_wait_max_s=0.25, merge_batch_max=4, max_active_timelines=16)
    cfg = load_config(root)
    if logs:
        setup_logging(cfg.paths.logs)
    fake = FakeLLM(replies or [SHORT])
    runtime = await build_runtime(cfg, llm=fake, state=state)
    endpoint = await runtime.server.start()
    harness = Harness(
        cfg=cfg,
        runtime=runtime,
        fake=fake,
        endpoint=endpoint,
        bootstrap=runtime.server.bootstrap_token,
        mgmt_token=runtime.server.mgmt_token,
        log_file=cfg.paths.logs / "core.log",
    )
    try:
        yield harness
    finally:
        await runtime.service.shutdown()
        await runtime.server.close()
        runtime.store.close()


@dataclass
class Ctx:
    a: Harness
    b: Harness
    c: Harness
    mgmt_a: MgmtClient
    mgmt_b: MgmtClient
    mgmt_c: MgmtClient


# ---- 裸 WS 工具


async def sendj(ws: Any, env: dict[str, Any]) -> None:
    await ws.send(json.dumps(env, ensure_ascii=False))


async def recvj(ws: Any, timeout: float = 8.0) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def drainj(ws: Any, seconds: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while True:
        budget = end - loop.time()
        if budget <= 0:
            return out
        try:
            out.append(await recvj(ws, budget))
        except (asyncio.TimeoutError, TimeoutError):
            continue


async def expect_closed(ws: Any, timeout: float = 6.0) -> int | None:
    try:
        await asyncio.wait_for(ws.recv(), timeout)
    except ConnectionClosed as exc:
        return exc.rcvd.code if exc.rcvd is not None else None
    raise AssertionError("连接未被关闭")


def err_code(env: dict[str, Any]) -> str:
    assert env.get("type") == "error", f"期望 error 信封：{json.dumps(env, ensure_ascii=False)[:200]}"
    return str(env["payload"]["code"])


async def expect_error(ws: Any, code: str, frame: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
    if frame is not None:
        await sendj(ws, frame)
    env = await recvj(ws, timeout)
    assert env.get("type") == "error", f"期望 error，得到 {json.dumps(env, ensure_ascii=False)[:200]}"
    assert env["payload"]["code"] == code, f"期望 {code}，得到 {env['payload']}"
    return env


async def raw_hello(ws: Any, name: str, auth: dict[str, Any], caps: dict[str, Any] | None = None) -> dict[str, Any]:
    await sendj(
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
    return await recvj(ws)


async def channel_conn(
    h: Harness, name: str, *, credential: str | None = None, bootstrap: str | None = None,
    caps: dict[str, Any] | None = None, headers: Any = None,
) -> tuple[Any, dict[str, Any]]:
    ws = await ws_connect(h.endpoint, max_size=1 << 20, additional_headers=headers) if headers else await ws_connect(h.endpoint, max_size=1 << 20)
    auth = {"credential": credential} if credential else {"bootstrap": bootstrap or ""}
    ack = await raw_hello(ws, name, auth, caps)
    assert ack.get("type") == "hello_ack", f"握手失败：{json.dumps(ack, ensure_ascii=False)[:200]}"
    return ws, ack


# ---- 管理面工具


async def open_mgmt(h: Harness) -> MgmtClient:
    mgmt = MgmtClient(h.endpoint, h.mgmt_token)
    await mgmt.connect()
    return mgmt


async def cred_for(h: Harness, mgmt: MgmtClient, name: str, caps: dict[str, Any] | None = None) -> str:
    issued = await mgmt.call("channel.ensure", name=name, capabilities=caps or {})
    return str(issued["credential"])


async def bind_thread(
    h: Harness, mgmt: MgmtClient, *, channel: str, thread_id: str,
    instance: str = "ph-audit2", timeline: str = "main", character: str = "ph-audit2",
    caps: dict[str, Any] | None = None, status: bool = True, segments: bool = True,
    max_text_len: int | None = None, max_parts: int | None = None,
) -> tuple[UmpClient, dict[str, Any], dict[str, Any], str]:
    """管理面登记通道 + 建会话 + 绑定 thread，返回（已握手客户端, thread 行, 会话行, 凭据）。"""
    credential = await cred_for(h, mgmt, channel, caps)
    session = (await mgmt.call("session.ensure", instance_id=instance, timeline_id=timeline,
                               character_id=character))["session"]
    thread = (await mgmt.call("thread.bind", channel=channel, thread_id=thread_id, session_id=session["id"]))["thread"]
    client = UmpClient(
        endpoint=h.endpoint, channel_id=channel, name=channel, credential=credential,
        status=status, segments=segments,
        **( {"max_text_len": max_text_len} if max_text_len else {}),
        **( {"max_parts": max_parts} if max_parts else {}),
    )
    await client.connect()
    return client, thread, session, credential


async def drain(client: UmpClient, seconds: float = 1.5) -> list[Any]:
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


async def fence(client: UmpClient, seconds: float = 1.0) -> list[Any]:
    """顺序屏障：ping 的 pong 回来说明之前的帧已被核心处理完。"""
    await client.send(ump.make("ping", {}))
    frames = await drain(client, seconds)
    assert any(e.type == "pong" for e in frames), "心跳未回：无法确认前序帧已处理"
    return frames


def pick(frames: Iterable[Any], env_type: str) -> Any:
    found = [e for e in frames if e.type == env_type]
    assert found, f"未收到 {env_type} 信封（实收 {[e.type for e in frames]}）"
    return found[-1]


async def replies(client: UmpClient, count: int, *, timeout: float = 20.0, collect: list[Any] | None = None) -> list[Any]:
    got: list[Any] = []
    while len(got) < count:
        got.append(await client.expect(lambda e: e.type == "reply", timeout=timeout, collect=collect))
    return got


@asynccontextmanager
async def slow(h: Harness, seconds: float):
    h.fake.delay_s = seconds
    try:
        yield
    finally:
        h.fake.delay_s = 0.0


def flat(parts: str | None) -> str:
    return "\n".join(text for batch in json.loads(parts or "[]") for text in batch)


async def room(h: Harness, mgmt: MgmtClient, *, moment: int, channel: str, thread_id: str = "dm-1",
               name: str = "灰潮纪", max_text_len: int | None = None):
    """真实实例 + 已激活时间线 + 绑定 thread（真世界包 / 角色卡样本）。"""
    package = example_package(name, moment=moment)
    card = example_card(package)
    info = create_instance(h.store, package, [card])
    timeline_id = h.store.timeline_list(info["id"])[0]["id"]
    character_id = str(card["meta"]["card_id"])
    h.world.ensure_instance(info["id"], now_real=time.time())
    h.world.activate(info["id"], timeline_id, now_real=time.time())
    client, thread, session, credential = await bind_thread(
        h, mgmt, channel=channel, thread_id=thread_id, instance=info["id"],
        timeline=timeline_id, character=character_id, max_text_len=max_text_len,
    )
    return client, thread, session, credential, info["id"], timeline_id, character_id


# ------------------------------------------------------------------ 条目


async def k1(ctx: Ctx) -> str:
    """§2.1 / §十.9：非 1.x 版本被拒并断连；未知 type / 方向越权 / 必填缺失在解析期拦截。"""
    ws = await ws_connect(ctx.a.endpoint)
    await sendj(ws, {"ump": "2.0", "type": "hello", "id": "e-v", "ts": 0.0, "payload": {}})
    env = await recvj(ws)
    assert err_code(env) == ump.Err.PROTOCOL, env
    assert env["payload"]["stage"] == ump.Stage.PROTOCOL, env["payload"]
    closed = await expect_closed(ws)
    cred = await cred_for(ctx.a, ctx.mgmt_a, "aud2-env")
    ws, _ = await channel_conn(ctx.a, "aud2-env", credential=cred)
    # 解析期 / 派发期错误合计 4 条 < PROTOCOL_ERROR_LIMIT=5：连接不得被打断
    bad_frames = [
        ("未知 type", ump.make("make_me_admin", {"op": "channel.ensure", "name": "aud2-env"}), ump.Err.UNSUPPORTED_TYPE),
        ("方向越权", ump.make("reply", {"message_id": "m-x", "parts": [{"text": "我代表核心"}], "batch_count": 1},
                            thread_id="dm-x"), ump.Err.PROTOCOL),
        ("ts 非数字", {"ump": "1.0", "type": "ping", "id": "e-2", "ts": 1.0, "payload": []}, ump.Err.PROTOCOL),
        ("缺 token", ump.make("user_message", {"text": "hi"}, thread_id="dm-x"), ump.Err.PROTOCOL),
    ]
    for label, frame, code in bad_frames:
        env = await expect_error(ws, code, frame)
        assert env["payload"]["stage"] == ump.Stage.PROTOCOL, (label, env["payload"])
    await sendj(ws, ump.make("ping", {}))
    pong = await recvj(ws)
    assert pong["type"] == "pong", pong
    await ws.close()
    return ("ump=2.0 → protocol_error(stage=protocol) + 关闭(close=%s)；未知 type → unsupported_type（无管理面结果）；"
            "通道发 reply → protocol_error；4 条协议错误（未知 type / 方向越权 / payload 非对象 / 缺 token）逐条报错、"
            "连接存活并照常回 pong" % closed)


async def k2(ctx: Ctx) -> str:
    """§2.5：无认证 / 错凭据拒绝且不登记；引导凭据一次性，消费后不能复用。"""
    ws = await ws_connect(ctx.a.endpoint)
    env = await raw_hello(ws, "aud2-noauth", {})
    code = err_code(env)
    assert code == ump.Err.AUTH_REQUIRED, env
    assert await expect_closed(ws) == 1008
    ws2 = await ws_connect(ctx.a.endpoint)
    bad = await raw_hello(ws2, "aud2-bogus", {"credential": "cr-forged"})
    assert err_code(bad) == ump.Err.AUTH_FAILED, bad
    assert bad["payload"]["retryable"] is False and bad["payload"]["stage"] == ump.Stage.AUTH
    await expect_closed(ws2)
    assert ctx.a.store.channel_by_name("aud2-bogus") is None, "认证失败却登记了通道实例"
    first = UmpClient(endpoint=ctx.a.endpoint, channel_id="aud2-boot", name="aud2-boot", bootstrap=ctx.a.bootstrap)
    ack = await first.connect()
    issued = ack.get("credential")
    assert issued and ack["state"] == "ready", ack
    await first.close()
    again = UmpClient(endpoint=ctx.a.endpoint, channel_id="aud2-boot", name="aud2-boot", bootstrap=ctx.a.bootstrap)
    try:
        await again.connect()
    except Exception as exc:  # UmpError(auth_failed)
        assert getattr(exc, "code", "") == ump.Err.AUTH_FAILED, exc
    else:
        raise AssertionError("引导凭据被重复使用")
    finally:
        await again.close()
    return (f"无 auth → auth_required+关闭；错凭据 → auth_failed(retryable=False,stage=auth)+关闭且不留登记；"
            f"引导凭据首连签发 {issued[:6]}…，复连 → auth_failed")


async def k3(ctx: Ctx) -> str:
    """§2.2 / §十.1：持久凭据重连同一通道实例、不重复签发；握手结果持久、重连回带 thread 令牌。"""
    client, thread, session, credential = await bind_thread(
        ctx.a, ctx.mgmt_a, channel="aud2-persist", thread_id="dm-p",
    )
    ack1 = client.hello_ack or {}
    token = thread["binding_token"]
    await client.close()
    row = ctx.a.store.channel_get(str(thread["channel_id"]))
    assert row is not None and row["protocol"] == "1.0", row
    assert json.loads(row["capabilities"])["max_text_len"] == 4000, row
    again = UmpClient(endpoint=ctx.a.endpoint, channel_id="aud2-persist", name="aud2-persist", credential=credential)
    ack2 = await again.connect()
    try:
        assert ack2["channel_instance"] == ack1["channel_instance"], (ack1, ack2)
        assert "credential" not in ack2, "重连不该再签发凭据"
        tokens = {t["id"]: t for t in ack2["threads"]}
        assert tokens["dm-p"]["binding_token"] == token, ack2["threads"]
    finally:
        await again.close()
    return (f"断线后库中仍有握手结果（protocol=1.0, max_text_len=4000）；持久凭据重连 → 同一 channel_instance="
            f"{ack1['channel_instance']}、不重发凭据、hello_ack.threads 回带 dm-p 令牌 {token}")


async def k4(ctx: Ctx) -> str:
    """§2.4：限额取双方交集且为有效正值；能力声明变化重新握手重新确认。"""
    small, thread_s, _s, cred_s = await bind_thread(
        ctx.a, ctx.mgmt_a, channel="aud2-caps-a", thread_id="dm-ca", max_text_len=50, max_parts=3,
    )
    big, _thread_b, _sb, _cb = await bind_thread(
        ctx.a, ctx.mgmt_a, channel="aud2-caps-b", thread_id="dm-cb", max_text_len=999999, max_parts=999999,
    )
    got_small = (small.hello_ack or {}).get("negotiated")
    got_big = (big.hello_ack or {}).get("negotiated")
    assert got_small == {"segments": True, "status": True, "max_text_len": 50, "max_parts": 3}, got_small
    assert got_big == {"segments": True, "status": True, "max_text_len": 4000, "max_parts": 10}, got_big
    await small.close()
    changed = UmpClient(endpoint=ctx.a.endpoint, channel_id="aud2-caps-a", name="aud2-caps-a",
                        credential=cred_s, max_text_len=300, max_parts=7, segments=False)
    ack = await changed.connect()
    try:
        assert ack["negotiated"] == {"segments": False, "status": True, "max_text_len": 300, "max_parts": 7}, ack["negotiated"]
    finally:
        await changed.close()
        await big.close()
    row = ctx.a.store.channel_by_name("aud2-caps-a")
    assert json.loads(row["capabilities"])["max_text_len"] == 300, row
    return "客户端 50/3 → 50/3；客户端 999999 → 核心上界 4000/10；重连改声明 300/7+segments=False → 交集随之更新并落库"


async def k5(ctx: Ctx) -> str:
    """§2.2 / §十.1：路由键为「认证通道实例 + thread」，不同通道同 thread/id 不串线。"""
    a, thread_a, _sa, _ca = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-iso-a", thread_id="dm-shared")
    b, thread_b, _sb, _cb = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-iso-b", thread_id="dm-shared")
    rows = ctx.a.store.counts()["messages"]
    try:
        for client, thread in ((a, thread_a), (b, thread_b)):
            await client.send(ump.make("user_message", {"text": "同一个 thread 与 id"},
                                       thread_id="dm-shared", binding_token=thread["binding_token"], id="e-shared"))
        ra = (await replies(a, 1))[0]
        rb = (await replies(b, 1))[0]
        assert ra.payload["message_id"] != rb.payload["message_id"], (ra.payload, rb.payload)
        assert ra.payload["covers"] == ["e-shared"] and rb.payload["covers"] == ["e-shared"]
        assert ctx.a.store.counts()["messages"] == rows + 4, "两通道未各自落库"
    finally:
        await a.close()
        await b.close()
    return f"同 thread=dm-shared / id=e-shared：两通道各得独立回复（{ra.payload['message_id']} / {rb.payload['message_id']}），各落 入站+出站 共 4 行"


async def k6(ctx: Ctx) -> str:
    """§2.5 / §十.1c / §十.6：自报 builtin、伪造管理令牌、普通网页来源都拿不到管理面。"""
    cred = await cred_for(ctx.a, ctx.mgmt_a, "builtin")
    ws, ack = await channel_conn(ctx.a, "builtin", credential=cred, headers={"Origin": "https://evil.example"})
    assert ack["payload"]["channel_instance"], ack
    await sendj(ws, {"mgmt": "1", "op": "status", "id": "r-1", "args": {}})
    env = await recvj(ws)
    assert env["type"] == "error", env
    assert "result" not in env and env["payload"]["code"] in (ump.Err.BAD_FRAME, ump.Err.PROTOCOL), env
    await ws.close()
    forged = MgmtClient(ctx.a.endpoint, "mg-forged")
    try:
        await forged.connect()
    except Exception as exc:
        assert getattr(exc, "code", "") == ump.Err.AUTH_FAILED, exc
    else:
        raise AssertionError("伪造管理令牌通过了认证")
    finally:
        await forged.close()
    web = await ws_connect(ctx.a.endpoint, additional_headers={"Origin": "https://evil.example"})
    await sendj(web, {"mgmt": "1", "op": "status", "id": "r-2", "args": {}})
    web_env = await recvj(web)
    # 首帧就是管理帧：核心按「非 UMP → 管理帧」处理，但没认证一律 auth_required + 关闭
    assert web_env.get("mgmt") == "1" and web_env.get("ok") is False, web_env
    web_code = str((web_env.get("error") or {}).get("code"))
    assert web_code == ump.Err.AUTH_REQUIRED, web_env
    assert "result" not in web_env, web_env
    assert await expect_closed(web) == 1008
    return (f"channel.id=builtin 的连接发管理帧 → {env['payload']['code']}（无 result）；Origin: evil.example（普通网页来源）"
            f"且无凭据 → 首帧即 auth_required + 关闭（Origin 头未被单独校验，凭据是硬门槛）；伪造 mgmt 令牌 → auth_failed")


async def k7(ctx: Ctx) -> str:
    """§2.2 / §十.1d：重绑换代后旧令牌的排队消息被拒、不落库，不泄露给新绑定。"""
    client, thread, session, _cred = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-stale", thread_id="dm-stale")
    stale = thread["binding_token"]
    try:
        rebound = await ctx.mgmt_a.call("thread.bind", channel="aud2-stale", thread_id="dm-stale", session_id=session["id"])
        assert rebound["thread"]["binding_token"] != stale and rebound["thread"]["binding_version"] > thread["binding_version"]
        rows = ctx.a.store.counts()["messages"]
        await client.send_user_message(thread_id="dm-stale", binding_token=stale, text="断线期间排队的旧消息")
        env = await client.expect(lambda e: e.type == "error")
        assert env.payload["code"] == ump.Err.BINDING_EXPIRED, env.payload
        assert env.payload["stage"] == ump.Stage.RECEIVE
        assert ctx.a.store.counts()["messages"] == rows, "旧令牌消息被意外接受 / 排队"
        await client.send_user_message(thread_id="dm-stale", binding_token=rebound["thread"]["binding_token"], text="新令牌")
        fresh = await client.expect(lambda e: e.type == "accepted")
        assert fresh.payload["state"] in ("queued", "processing", "done"), fresh.payload
        await replies(client, 1)
    finally:
        await client.close()
    return (f"换代（v{thread['binding_version']}→v{rebound['thread']['binding_version']}）后旧令牌 {stale} → binding_expired(stage=receive)、"
            f"消息行数不变；新令牌照常接受")


async def k8(ctx: Ctx) -> str:
    """§2.2 / §十.2：同键同文重发只查既有结果；同键异文报 conflict；都不重复执行。"""
    client, thread, session, _cred = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-idem", thread_id="dm-idem")
    token = thread["binding_token"]
    env = ump.make("user_message", {"text": "重复发送"}, thread_id="dm-idem", binding_token=token, id="e-dup")
    try:
        await client.send(env)
        first_round = await drain(client, 3.0)
        first = pick(first_round, "reply")
        calls, rows = len(ctx.a.fake.calls), ctx.a.store.counts()["messages"]
        await client.send(env)
        second = await drain(client, 3.0)
        accepted = pick(second, "accepted")
        again = [e for e in second if e.type == "reply"]
        assert accepted.payload["state"] == "done" and accepted.payload["message_id"] == first.payload["message_id"], accepted.payload
        assert again and again[-1].payload["message_id"] == first.payload["message_id"], [e.payload for e in again]
        assert len(ctx.a.fake.calls) == calls and ctx.a.store.counts()["messages"] == rows, "重发触发了二次执行"
        await client.send(ump.make("user_message", {"text": "换了正文"}, thread_id="dm-idem", binding_token=token, id="e-dup"))
        conflict = await client.expect(lambda e: e.type == "error")
        assert conflict.payload["code"] == ump.Err.CONFLICT, conflict.payload
        assert len(ctx.a.fake.calls) == calls and ctx.a.store.counts()["messages"] == rows, "冲突路径仍执行了"
    finally:
        await client.close()
    return (f"同 id+同文重发 → accepted.state=done、同一 message_id={first.payload['message_id']}、不重复投递出第二条全文（reply 帧 {len(again)} 条）；"
            f"同 id 异文 → conflict；两次都未新增模型调用与消息行")


async def k9(ctx: Ctx) -> str:
    """§2.2 / §十.2：回滚作废的输入保留作废记录，重连重发返回取消、不复活。"""
    client, thread, session, _cred, instance_id, timeline_id, _ch = await room(
        ctx.a, ctx.mgmt_a, moment=AWAKE_AT, channel="aud2-roll", thread_id="dm-rb",
    )
    token = thread["binding_token"]
    channel_id = thread["channel_id"]
    try:
        await client.send_user_message(thread_id="dm-rb", binding_token=token, text="回滚前的第一条")
        await replies(client, 1)
        commit = (await ctx.mgmt_a.call("runtime.commit", instance_id=instance_id, timeline_id=timeline_id, note="审计回滚点"))
        commit_id = commit["commit"]["id"]
        async with slow(ctx.a, 1.2):
            await client.send(ump.make("user_message", {"text": "飞行中被回滚的输入"}, thread_id="dm-rb",
                                       binding_token=token, id="e-rb"))
            await client.expect(lambda e: e.type == "accepted", timeout=6)
            await asyncio.sleep(0.3)
            await ctx.mgmt_a.call("runtime.rollback", instance_id=instance_id, timeline_id=timeline_id,
                                  commit_id=commit_id, confirm=True)
            await asyncio.sleep(1.6)
        assert ctx.a.store.void_has(channel_id, "dm-rb", "e-rb"), "回滚未留作废记录"
        late = await drain(client, 1.0)
        assert not [e for e in late if e.type == "reply"], "作废输入的迟到结果仍被投递"
        calls, rows = len(ctx.a.fake.calls), ctx.a.store.counts()["messages"]
        await client.send(ump.make("user_message", {"text": "飞行中被回滚的输入"}, thread_id="dm-rb",
                                   binding_token=token, id="e-rb"))
        env = await client.expect(lambda e: e.type == "error")
        assert env.payload["code"] == ump.Err.VOIDED, env.payload
        assert len(ctx.a.fake.calls) == calls and ctx.a.store.counts()["messages"] == rows, "作废输入被重新执行"
    finally:
        await client.close()
    return ("回滚留作废记录（void_has=True）、迟到结果不投递；同 id 重发 → voided、不重新执行、不落新行")


async def k10(ctx: Ctx) -> str:
    """§2.2：同会话多 thread 按接受顺序串行，回复只回来源 thread。"""
    kw = {"instance": "ph-audit2-serial", "timeline": "main", "character": "ph-audit2-c"}
    wa, thread_a, _sa, _ca = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-serial", thread_id="dm-sa", **kw)
    wb, thread_b, _sb, _cb = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-serial-2", thread_id="dm-sb", **kw)
    stamps: list[float] = []
    original = ctx.a.fake.chat

    async def timed(messages: Any, **kwargs: Any) -> Any:
        stamps.append(asyncio.get_running_loop().time())
        return await original(messages, **kwargs)

    try:
        ctx.a.fake.chat = timed  # type: ignore[assignment]
        async with slow(ctx.a, 0.5):
            await wa.send(ump.make("user_message", {"text": "第一条"}, thread_id="dm-sa",
                                   binding_token=thread_a["binding_token"], id="e-s1"))
            await wb.send(ump.make("user_message", {"text": "第二条"}, thread_id="dm-sb",
                                   binding_token=thread_b["binding_token"], id="e-s2"))
            ra = await wa.expect(lambda e: e.type == "reply", timeout=20)
            rb = await wb.expect(lambda e: e.type == "reply", timeout=20)
    finally:
        ctx.a.fake.chat = original  # type: ignore[assignment]
        await wa.close()
        await wb.close()
    order = [ra.thread_id, rb.thread_id]
    assert order == ["dm-sa", "dm-sb"], f"回复未按接受顺序：{order}"
    gap = stamps[1] - stamps[0]
    assert gap >= 0.5, f"同会话两轮生成重叠（间隔 {gap:.2f}s）"
    hist = ctx.a.store.context_window(str(_sa["id"]), 20)
    assert [r["role"] for r in hist] == ["user", "user", "character", "character"], [r["role"] for r in hist]
    return (f"两条入站按接受顺序串行（生成调用间隔 {gap:.2f}s ≥ 单轮 0.5s），回复分别只回 dm-sa / dm-sb，"
            f"会话历史 4 行顺序正确")


async def k11(ctx: Ctx) -> str:
    """§2.2：用户消息与处理状态持久化后才确认接收。"""
    client, thread, _session, _cred = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-ack", thread_id="dm-ack")
    token = thread["binding_token"]
    channel_id = thread["channel_id"]
    try:
        await client.send(ump.make("user_message", {"text": "持久化了吗"}, thread_id="dm-ack",
                                   binding_token=token, id="e-ack"))
        accepted = await client.expect(lambda e: e.type == "accepted")
        row = ctx.a.store.inbound_find(channel_id, "dm-ack", "e-ack")
        assert row is not None, "收到 accepted 时入站行尚不存在"
        assert accepted.payload["ref"] == "e-ack" and row["state"] in ("queued", "processing", "done"), (accepted.payload, row["state"])
        await replies(client, 1)
    finally:
        await client.close()
    return f"accepted 到达时库里已有该输入行（state={row['state']}，ref=e-ack）：确认在持久化之后"


async def k12(ctx: Ctx) -> str:
    """§十.3：生成完成后连接中断，重连恢复同一固化回复，不重跑模型。"""
    client, thread, _session, credential, instance_id, timeline_id, _ch = await room(
        ctx.a, ctx.mgmt_a, moment=AWAKE_AT, channel="aud2-resend", thread_id="dm-rs",
    )
    token = thread["binding_token"]
    channel_id = thread["channel_id"]
    async with slow(ctx.a, 0.7):
        await client.send_user_message(thread_id="dm-rs", binding_token=token, text="断线期间生成")
        await client.close()
        await asyncio.sleep(1.8)
    pending = ctx.a.store.pending_outbound(channel_id, "dm-rs", limit=5)
    assert len(pending) == 1, f"应有 1 条待投递固化回复，实际 {len(pending)}"
    mid = pending[0]["message_id"]
    calls, rows = len(ctx.a.fake.calls), ctx.a.store.counts()["messages"]
    tasks_before = len(ctx.a.store.memory_tasks(instance_id, timeline_id))
    again = UmpClient(endpoint=ctx.a.endpoint, channel_id="aud2-resend", name="aud2-resend", credential=credential)
    await again.connect()
    try:
        env = await again.expect(lambda e: e.type == "reply", timeout=10)
        assert env.payload["message_id"] == mid, (mid, env.payload)
        assert len(ctx.a.fake.calls) == calls, "补投重新调用了模型"
        assert ctx.a.store.counts()["messages"] == rows, "补投新增了消息行"
        assert len(ctx.a.store.memory_tasks(instance_id, timeline_id)) == tasks_before, "补投重复登记了记忆提取"
    finally:
        await again.close()
    return f"重连补投同一 message_id={mid}（正文沿用固化产物）；模型调用 / 消息行 / 记忆任务数均不变"


async def k13(ctx: Ctx) -> str:
    """§2.3 / §十.3：显式 retry(outbound) 只重发固化结果，不调用模型、不重复记忆。"""
    client, thread, _session, credential, instance_id, timeline_id, _ch = await room(
        ctx.a, ctx.mgmt_a, moment=AWAKE_AT, channel="aud2-retry", thread_id="dm-rt",
    )
    token = thread["binding_token"]
    channel_id = thread["channel_id"]
    async with slow(ctx.a, 0.7):
        await client.send_user_message(thread_id="dm-rt", binding_token=token, text="重发这条")
        await client.close()
        await asyncio.sleep(1.8)
    mid = ctx.a.store.pending_outbound(channel_id, "dm-rt", limit=5)[0]["message_id"]
    calls = len(ctx.a.fake.calls)
    tasks_before = len(ctx.a.store.memory_tasks(instance_id, timeline_id))
    ws, _ack = await channel_conn(ctx.a, "aud2-retry", credential=credential)
    try:
        redelivered = [f for f in await drainj(ws, 2.5) if f.get("type") == "reply"]
        assert redelivered and redelivered[-1]["payload"]["message_id"] == mid, (mid, redelivered[-1] if redelivered else None)
        await sendj(ws, ump.make("retry", {"ref": mid, "kind": "outbound"}, thread_id="dm-rt", binding_token=token))
        after = await drainj(ws, 2.5)
    finally:
        await ws.close()
    kinds = [f.get("type") for f in after]
    accepted = [f for f in after if f.get("type") == "accepted"]
    resent = [f for f in after if f.get("type") == "reply"]
    assert resent and all(f["payload"]["message_id"] == mid for f in resent), kinds
    assert len(ctx.a.fake.calls) == calls, "retry(outbound) 重新调用了模型"
    assert len(ctx.a.store.memory_tasks(instance_id, timeline_id)) == tasks_before, "retry(outbound) 重复登记记忆"
    assert accepted and accepted[-1]["payload"]["state"] in ump.ACCEPT_STATES, [f["payload"] for f in accepted]
    rollup = ctx.a.store.delivery_rollup(ctx.a.store.outbound_by_message_id(mid)["seq"])
    return (f"retry(kind=outbound, ref={mid}) → 只重发同一 message_id 的固化结果；accepted.state="
            f"{accepted[-1]['payload']['state']!r}（投递汇总 delivery={rollup}），模型调用与记忆任务数不变")


async def k14(ctx: Ctx) -> str:
    """§2.3 / §十.7：回执按原出站标识写入；unknown 不等于成功；迟到回执不倒退已确认状态。"""
    client, thread, _session, _cred = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-dlv", thread_id="dm-dlv")
    token = thread["binding_token"]
    try:
        await client.send_user_message(thread_id="dm-dlv", binding_token=token, text="在吗")
        reply = (await replies(client, 1))[0]
        mid = reply.payload["message_id"]
        seq = ctx.a.store.outbound_by_message_id(mid)["seq"]
        await client.report_delivery(thread_id="dm-dlv", binding_token=token, message_id=mid, batch_index=0, state="unknown")
        frames = await fence(client)
        assert not [e for e in frames if e.type == "error"], [e.payload for e in frames if e.type == "error"]
        unknown_rollup = ctx.a.store.delivery_rollup(seq)
        assert unknown_rollup == "unknown", unknown_rollup
        await client.request_retry(thread_id="dm-dlv", binding_token=token, ref=mid, kind="outbound")
        retried = await fence(client)
        state = pick(retried, "accepted").payload
        assert state["message_id"] == mid and state["state"] in ump.ACCEPT_STATES, state
        resent = [e for e in retried if e.type == "reply"]
        after_retry = ctx.a.store.delivery_rollup(seq)
        await client.report_delivery(thread_id="dm-dlv", binding_token=token, message_id=mid, batch_index=0, state="accepted")
        await fence(client)
        assert ctx.a.store.delivery_rollup(seq) == "delivered", "全部批次 accepted 才 delivered"
        await client.report_delivery(thread_id="dm-dlv", binding_token=token, message_id=mid, batch_index=0, state="unknown")
        await fence(client)
        assert ctx.a.store.delivery_rollup(seq) == "delivered", "迟到 unknown 倒退了已确认状态"
    finally:
        await client.close()
    return (f"客户端报 unknown → rollup=unknown（≠delivered，不称成功）；显式 retry 重发固化结果（{len(resent)} 帧，"
            f"rollup 由 unknown 变 {after_retry}，人工重试允许重复）；报 accepted → delivered；迟到 unknown 不倒退")


async def k15(ctx: Ctx) -> str:
    """§2.3：换代令牌不能作用于旧投递记录；未发送的旧回复不因重绑改投新会话。"""
    client, thread, session, credential, instance_id, timeline_id, character_id = await room(
        ctx.a, ctx.mgmt_a, moment=AWAKE_AT, channel="aud2-dlv2", thread_id="dm-dlv2",
    )
    token = thread["binding_token"]
    channel_id = thread["channel_id"]
    async with slow(ctx.a, 0.7):
        await client.send_user_message(thread_id="dm-dlv2", binding_token=token, text="重绑前固化的回复")
        await client.close()
        await asyncio.sleep(1.8)
    pending = ctx.a.store.pending_outbound(channel_id, "dm-dlv2", limit=5)
    assert len(pending) == 1, pending
    mid, old_seq = pending[0]["message_id"], pending[0]["seq"]
    before = ctx.a.store.delivery_rollup(old_seq)
    other_package = example_package("灰潮纪", moment=AWAKE_AT)
    other_card = example_card(other_package)
    other = create_instance(ctx.a.store, other_package, [other_card])
    other_session = (await ctx.mgmt_a.call("session.ensure", instance_id=other["id"],
                                           timeline_id=ctx.a.store.timeline_list(other["id"])[0]["id"],
                                           character_id=str(other_card["meta"]["card_id"])))["session"]
    rebound = await ctx.mgmt_a.call("thread.bind", channel="aud2-dlv2", thread_id="dm-dlv2", session_id=other_session["id"])
    ws, ack = await channel_conn(ctx.a, "aud2-dlv2", credential=credential)
    try:
        frames = await drainj(ws, 2.0)
        reply_frames = [f for f in frames if f.get("type") == "reply"]
        tokens = {t["id"]: t for t in ack["payload"]["threads"]}
        assert tokens["dm-dlv2"]["binding_token"] == rebound["thread"]["binding_token"], ack["payload"]["threads"]
        assert tokens["dm-dlv2"]["binding_token"] != token
        carried = {f["thread"]["binding_token"] for f in reply_frames if "thread" in f}
        await sendj(ws, ump.make("delivery", {"message_id": mid, "batch_index": 0, "state": "accepted"},
                                 thread_id="dm-dlv2", binding_token=rebound["thread"]["binding_token"]))
        env = await recvj(ws)
        assert env["type"] == "error" and env["payload"]["code"] == ump.Err.BINDING_EXPIRED, env
        states = {r["batch_index"]: r["state"] for r in ctx.a.store.delivery_rows(old_seq)}
        assert "accepted" not in states.values(), f"新令牌把旧投递记录标成了 accepted：{states}"
        assert ctx.a.store.delivery_rollup(old_seq) != "delivered", "新令牌让旧记录变成 delivered"
        old_row = ctx.a.store.outbound_by_message_id(mid)
        assert old_row["channel_id"] == channel_id and old_row["session_id"] == session["id"], old_row
        new_hist = ctx.a.store.history_page(other_session["id"], limit=50)
        leaked = [m for m in new_hist["messages"] if m["message_id"] == mid]
        assert not leaked, "旧回复被灌进新绑定的会话历史"
    finally:
        await ws.close()
    return (f"重绑后：补投的固化回复仍带旧令牌（实发 {sorted(carried)}，新令牌 {rebound['thread']['binding_token']}）；"
            f"新令牌补回执 → binding_expired，旧投递记录未被标 accepted（{states}，rollup {before}→"
            f"{ctx.a.store.delivery_rollup(old_seq)}）；旧回复未进入新会话历史（session 仍为 {session['id']}）")


async def k16(ctx: Ctx) -> str:
    """§2.4 / §十.4：超长回复完整、有序分批，不丢尾部；segments=False 时每批一段。"""
    long_text = "\n".join(f"第{i}行：" + "世界内容" * 4 for i in range(1, 7))
    ctx.b.fake.replies = [long_text]
    client, thread, _session, _cred = await bind_thread(
        ctx.b, ctx.mgmt_b, channel="aud2-batch", thread_id="dm-batch", segments=False, max_text_len=30, max_parts=5,
    )
    token = thread["binding_token"]
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
        assert len(parts) == len(got), f"segments=False 时每批应只有一段，实得 {len(parts)}"
        assert all(len(p) <= 30 for p in parts), [len(p) for p in parts]
        assert "".join(parts) == long_text.replace("\n", ""), "分批丢失 / 重排了正文"
    finally:
        await client.close()
    return (f"segments=False + max_text_len=30：{len(got)} 批有序（indices={indices}，batch_count 一致，message_id 唯一），"
            f"每批 1 段且 ≤30 字；拼接后正文与原文逐字一致（无丢尾）")


async def k17(ctx: Ctx) -> str:
    """§2.4：新协商能力不足以承载既有分段时只报投递能力不兼容，不重排 / 裁剪 / 重生成。"""
    ctx.b.fake.replies = ["\n".join(f"第{i}行：" + "世界内容" * 6 for i in range(1, 8))]
    client, thread, _session, credential = await bind_thread(
        ctx.b, ctx.mgmt_b, channel="aud2-incompat", thread_id="dm-in", segments=True, max_text_len=4000,
    )
    token = thread["binding_token"]
    try:
        await client.send_user_message(thread_id="dm-in", binding_token=token, text="换能力")
        reply = (await replies(client, 1))[0]
        mid = reply.payload["message_id"]
        before = ctx.b.store.outbound_by_message_id(mid)["parts"]
        await client.close()
        again = UmpClient(endpoint=ctx.b.endpoint, channel_id="aud2-incompat", name="aud2-incompat",
                          credential=credential, max_text_len=20, segments=True)
        await again.connect()
        try:
            env = await again.expect(lambda e: e.type == "error", timeout=10)
            assert env.payload["code"] == ump.Err.UNSUPPORTED_CAPABILITY, env.payload
            assert env.payload["stage"] == ump.Stage.DELIVERY, env.payload
        finally:
            await again.close()
        after = ctx.b.store.outbound_by_message_id(mid)["parts"]
        assert after == before, "既有分段计划被重排 / 裁剪"
        rollup = ctx.b.store.delivery_rollup(ctx.b.store.outbound_by_message_id(mid)["seq"])
        assert rollup == "incompatible", rollup
    finally:
        pass
    return f"限额缩到 20 后重连 → unsupported_capability(stage=delivery)，固化分段未变，rollup={rollup}"


async def k18(ctx: Ctx) -> str:
    """§2.3：status 只发给声明该能力的通道。"""
    off, thread_off, _s1, _c1 = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-stat-off", thread_id="dm-so", status=False)
    on, thread_on, _s2, _c2 = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-stat-on", thread_id="dm-sn", status=True)
    try:
        async with slow(ctx.a, 0.4):
            collected: list[Any] = []
            await off.send_user_message(thread_id="dm-so", binding_token=thread_off["binding_token"], text="不支持状态")
            await off.expect(lambda e: e.type == "reply", timeout=15, collect=collected)
            got: list[Any] = []
            await on.send_user_message(thread_id="dm-sn", binding_token=thread_on["binding_token"], text="支持状态")
            await on.expect(lambda e: e.type == "reply", timeout=15, collect=got)
        states = [e.payload["state"] for e in got if e.type == "status"]
        assert all(e.type != "status" for e in collected), [e.type for e in collected]
        assert "thinking" in states, states
    finally:
        await off.close()
        await on.close()
    return f"status=False 通道只收 accepted/reply；status=True 通道收到 thinking/idle（实测 {states}）"


async def k19(ctx: Ctx) -> str:
    """§2.3 / §十.8：睡眠期两条连续入站合并为一条回复；批内任一条查询 / 重试返回同批结果。"""
    client, thread, session, _cred, instance_id, timeline_id, _ch = await room(
        ctx.a, ctx.mgmt_a, moment=SLEEP_AT, channel="aud2-merge", thread_id="dm-merge",
    )
    token = thread["binding_token"]
    channel_id = thread["channel_id"]
    calls0 = len(ctx.a.fake.calls)
    first_text = f"睡了吗 {MARK}"
    try:
        first = await client.send_user_message(thread_id="dm-merge", binding_token=token, text=first_text)
        await client.expect(lambda e: e.type == "accepted")
        await asyncio.sleep(0.08)
        second = await client.send_user_message(thread_id="dm-merge", binding_token=token, text="明天几点上堤")
        queued = await client.expect(lambda e: e.type == "accepted")
        assert queued.payload["state"] == "queued", queued.payload
        reply = await client.expect(lambda e: e.type == "reply", timeout=8)
        assert reply.payload["covers"] == [first, second], reply.payload
        assert reply.payload["reply_to"] == second, reply.payload
        assert len(ctx.a.fake.calls) == calls0 + 1, "一份固化回复：只应生成一次"
        rows = [ctx.a.store.inbound_find(channel_id, "dm-merge", ref) for ref in (first, second)]
        assert [r["state"] for r in rows] == ["done", "done"], [r["state"] for r in rows]
        assert {r["reply_message_id"] for r in rows} == {reply.payload["message_id"]}, rows
        message_id = reply.payload["message_id"]
        calls, count = len(ctx.a.fake.calls), ctx.a.store.counts()["messages"]
        await client.send(ump.make("user_message", {"text": first_text}, thread_id="dm-merge",
                                   binding_token=token, id=first))
        again = await client.expect(lambda e: e.type == "accepted")
        assert again.payload == {"ref": first, "state": "done", "message_id": message_id}, again.payload
        await client.request_retry(thread_id="dm-merge", binding_token=token, ref=second)
        restored = await client.expect(lambda e: e.type == "accepted")
        assert restored.payload == {"ref": second, "state": "done", "message_id": message_id}, restored.payload
        tasks = sorted(t["source_ref"] for t in ctx.a.store.memory_tasks(instance_id, timeline_id))
        assert tasks == sorted([f"user:{first}", f"user:{second}", f"reply:{message_id}"]), tasks
        assert len(ctx.a.fake.calls) == calls and ctx.a.store.counts()["messages"] == count, "重发 / 重试触发了重复生成"
    finally:
        await client.close()
    return (f"睡眠期两条入站 → 一份固化回复（covers={reply.payload['covers']}，reply_to=批内最后一条 {second}），只生成 1 次；"
            f"重发第一条 / 重试第二条都返回同批 message_id={message_id}；记忆登记 3 条（各输入 1 次 + 回复 1 次）")


async def k20(ctx: Ctx) -> str:
    """§十.8：跨 thread 不合并。"""
    package = example_package("灰潮纪", moment=SLEEP_AT)
    card = example_card(package)
    info = create_instance(ctx.a.store, package, [card])
    timeline_id = ctx.a.store.timeline_list(info["id"])[0]["id"]
    character_id = str(card["meta"]["card_id"])
    ctx.a.world.ensure_instance(info["id"], now_real=time.time())
    ctx.a.world.activate(info["id"], timeline_id, now_real=time.time())
    credential = await cred_for(ctx.a, ctx.mgmt_a, "aud2-merge2")
    session = (await ctx.mgmt_a.call("session.ensure", instance_id=info["id"], timeline_id=timeline_id,
                                       character_id=character_id))["session"]
    threads = {}
    for thread_id in ("dm-t1", "dm-t2"):
        threads[thread_id] = (await ctx.mgmt_a.call("thread.bind", channel="aud2-merge2", thread_id=thread_id,
                                                      session_id=session["id"]))["thread"]
    client = UmpClient(endpoint=ctx.a.endpoint, channel_id="aud2-merge2", name="aud2-merge2", credential=credential)
    await client.connect()
    calls0 = len(ctx.a.fake.calls)
    try:
        refs = {}
        for thread_id in ("dm-t1", "dm-t2"):
            refs[thread_id] = await client.send_user_message(thread_id=thread_id,
                                                             binding_token=threads[thread_id]["binding_token"],
                                                             text=f"来自 {thread_id}")
            await client.expect(lambda e: e.type == "accepted")
        got = await replies(client, 2, timeout=15)
        per_thread = {e.thread_id: e.payload for e in got}
        assert set(per_thread) == {"dm-t1", "dm-t2"}, sorted(per_thread)
        for thread_id, payload in per_thread.items():
            assert payload["covers"] == [refs[thread_id]], (thread_id, payload)
        assert len(ctx.a.fake.calls) == calls0 + 2, f"跨 thread 不应合并，生成次数应增 2，实测增 {len(ctx.a.fake.calls) - calls0}"
    finally:
        await client.close()
    return (f"同一会话两个 thread 在睡眠期各发一条：各得独立回复（covers 只含自己的入站 {refs['dm-t1']} / {refs['dm-t2']}），"
            f"生成 2 次：跨 thread 不合并")


async def k21(ctx: Ctx) -> str:
    """§2.3 / §十.4：主动消息与独立开场的管理面入口可用（runtime.proactive / runtime.first_contact）。"""
    package = example_package("灰潮纪", moment=AWAKE_AT)
    card = example_card(package)
    info = create_instance(ctx.a.store, package, [card])
    timeline_id = ctx.a.store.timeline_list(info["id"])[0]["id"]
    character_id = str(card["meta"]["card_id"])
    ctx.a.world.ensure_instance(info["id"], now_real=time.time())
    ctx.a.world.activate(info["id"], timeline_id, now_real=time.time())
    errors: dict[str, str] = {}
    for op, args in (
        ("runtime.proactive", {"instance_id": info["id"], "timeline_id": timeline_id, "per_day": 2}),
        ("runtime.first_contact", {"instance_id": info["id"], "timeline_id": timeline_id,
                                   "character_id": character_id, "channel_id": "builtin"}),
    ):
        try:
            await ctx.mgmt_a.call(op, **args)
        except UmpError as exc:
            errors[op] = f"{exc.code}: {exc.message}"
    assert not errors, (
        f"管理面入口失败：{errors}；"
        f"根因 isekai_core/world/ops.py:855,865 引用未定义的 runtime（dispatch_async 签名 ops.py:842-844 无该参数，"
        f"调用方 channel.py:397-403 也不传）"
    )
    return "两个管理面入口都返回结果"


async def k22(ctx: Ctx) -> str:
    """§2.3 / §十.4：主动消息 reply_to=null、有自己 message_id、不伪造入站、不重复计数。"""
    client, thread, session, credential, instance_id, timeline_id, character_id = await room(
        ctx.a, ctx.mgmt_a, moment=AWAKE_AT, channel="aud2-pro", thread_id="dm-pro",
    )
    token = thread["binding_token"]
    channel_id = thread["channel_id"]
    world_now = int(ctx.a.world.world_moment(instance_id, timeline_id))
    ctx.a.store.knowledge_put({
        "instance_id": instance_id, "timeline_id": timeline_id, "character_id": character_id,
        "id": f"kn-{character_id}-a2", "world_seconds": world_now, "kind": "claim",
        "target": "cl-audit2", "source": "src-a2", "stance": "recorded",
        "text": "北堤的通行牌这三天都停发了",
    })
    ctx.a.fake.replies = ["堤上风转了，通行牌还是没发。"]
    await client.close()  # 离线固化主动消息：先固化，再重连看投递
    spoken = await ctx.a.world.proactive_tick(instance_id, timeline_id, llm=ctx.a.fake, per_day=2)
    assert spoken["spoken"] == 1, spoken
    mid = spoken["messages"][0]["message_id"]
    inbound_before = len([m for m in ctx.a.store.instance_messages(instance_id) if m["role"] == "user"])
    again = UmpClient(endpoint=ctx.a.endpoint, channel_id="aud2-pro", name="aud2-pro", credential=credential)
    await again.connect()
    try:
        frames = await drain(again, 2.5)
        await again.send_user_message(thread_id="dm-pro", binding_token=token, text="那你自己呢")
        more = await drain(again, 3.0)
    finally:
        await again.close()
    all_frames = frames + more
    proactive = [e for e in all_frames if e.type == "reply" and e.payload.get("reply_to") is None]
    assert proactive, [e.type for e in all_frames]
    payload = proactive[-1].payload
    assert payload["message_id"] == mid, (mid, payload)
    assert payload["batch_count"] == 1 and payload["covers"] == [], payload
    log = ctx.a.store.proactive_list(instance_id, timeline_id)
    assert len(log) == 1 and log[0]["message_id"] == mid, log
    inbound_after = len([m for m in ctx.a.store.instance_messages(instance_id) if m["role"] == "user"])
    assert inbound_after == inbound_before + 1, "主动消息伪造了入站消息"
    row = ctx.a.store.outbound_by_message_id(mid)
    assert row["reply_to"] is None and row["channel_id"] == channel_id, row
    return (f"主动消息以 reply_to=null、自有 message_id={mid} 投递到唯一目标 thread（batch_count=1, covers=[]）；"
            f"proactive_log 只记 1 条、不伪造入站（入站行只在用户自己发言时 +1）")


async def k23(ctx: Ctx) -> str:
    """§2.4 / §十.4：主动消息超出协商长度时未拆分（同通道的普通回复会拆分）。"""
    client, thread, session, credential, instance_id, timeline_id, character_id = await room(
        ctx.a, ctx.mgmt_a, moment=AWAKE_AT, channel="aud2-pro-long", thread_id="dm-prol", max_text_len=300,
    )
    token = thread["binding_token"]
    world_now = int(ctx.a.world.world_moment(instance_id, timeline_id))
    ctx.a.store.knowledge_put({
        "instance_id": instance_id, "timeline_id": timeline_id, "character_id": character_id,
        "id": f"kn-{character_id}-a2l", "world_seconds": world_now, "kind": "claim",
        "target": "cl-audit2l", "source": "src-a2l", "stance": "recorded",
        "text": "盐滩边又立了一块新碑",
    })
    long_proactive = "堤上风转了，" + "通行牌还是没发，" * 40  # 326 字：通过 proactive_text_allowed（≤400），但超出协商的 300
    ctx.a.fake.replies = [long_proactive]
    await client.close()
    spoken = await ctx.a.world.proactive_tick(instance_id, timeline_id, llm=ctx.a.fake, per_day=2)
    assert spoken["spoken"] == 1, spoken
    mid = spoken["messages"][0]["message_id"]
    row = ctx.a.store.outbound_by_message_id(mid)
    batches = json.loads(row["parts"])
    again = UmpClient(endpoint=ctx.a.endpoint, channel_id="aud2-pro-long", name="aud2-pro-long",
                      credential=credential, max_text_len=300)
    await again.connect()
    try:
        await again.send_user_message(thread_id="dm-prol", binding_token=token, text="短的一句")
        frames = await drain(again, 3.5)
        proactive_frames = [e for e in frames if e.type == "reply" and e.payload.get("message_id") == mid]
        errors = [e.payload for e in frames if e.type == "error"]
    finally:
        await again.close()
    rollup = ctx.a.store.delivery_rollup(row["seq"])
    contrast = await _long_reply_contrast(ctx)
    parts = [str(p) for batch in batches for p in batch]
    # 未确认的批次允许重投（补投语义）：按批次索引归并，同一索引只取一份
    by_index: dict[int, list[str]] = {}
    for frame in proactive_frames:
        index = int(frame.payload.get("batch_index") or 0)
        by_index[index] = [str(p["text"]) for p in (frame.payload.get("parts") or [])]
    chunks = [text for index in sorted(by_index) for text in by_index[index]]
    assert batches, "主动消息固化后没有分批计划"
    assert all(len(part) <= 300 for part in parts), f"固化段长超出协商 300：{[len(p) for p in parts]}"
    assert "".join(parts) == long_proactive, "固化拆分不得裁剪正文尾部"
    assert proactive_frames, "拆分后的主动消息必须投递得出去"
    assert chunks and all(len(chunk) <= 300 for chunk in chunks), f"线上段长超出协商 300：{[len(c) for c in chunks]}"
    assert "".join(chunks) == long_proactive, (
        f"线上拼接与原文不一致：{len(chunks)} 段 / 段长 {[len(c) for c in chunks]} / "
        f"拼出 {len(''.join(chunks))} 字，原文 {len(long_proactive)} 字"
    )
    assert rollup not in ("incompatible", "unknown"), f"主动消息投递不兼容：{rollup}"
    assert not [e for e in errors if e["code"] == ump.Err.UNSUPPORTED_CAPABILITY], errors
    return (
        f"主动消息 {len(long_proactive)} 字：固化 {len(batches)} 批 / 段长 {[len(p) for b in batches for p in b]}；"
        f"线上 {len(chunks)} 段、拼接逐字一致、rollup={rollup}；同通道普通回复 {contrast}"
    )


async def _long_reply_contrast(ctx: Ctx) -> str:
    """对照：同通道（max_text_len=300）的普通回复会被拆分并投递成功。"""
    client, thread, _s, _c = await bind_thread(
        ctx.a, ctx.mgmt_a, channel="aud2-pro-cmp", thread_id="dm-cmp", max_text_len=300,
    )
    ctx.a.fake.replies = ["堤上风转了，" + "通行牌还是没发，" * 40]
    try:
        await client.send_user_message(thread_id="dm-cmp", binding_token=thread["binding_token"], text="说说看")
        got: list[Any] = []
        first = await client.expect(lambda e: e.type == "reply", timeout=10, collect=got)
        batches = [first]
        while len(batches) < first.payload["batch_count"]:
            batches.append(await client.expect(lambda e: e.type == "reply", timeout=10, collect=got))
        parts = [p["text"] for e in batches for p in e.payload["parts"]]
        rollup = ctx.a.store.delivery_rollup(ctx.a.store.outbound_by_message_id(first.payload["message_id"])["seq"])
        assert all(len(p) <= 300 for p in parts), [len(p) for p in parts]
        return f"批数 {first.payload['batch_count']}、段长 {[len(p) for p in parts]}（均 ≤300）、rollup={rollup}（正常投递）"
    finally:
        await client.close()


async def k33(ctx: Ctx) -> str:
    """§2.3 / §十.8：独立开场 reply_to=null、一次性、不伪造入站。"""
    client, thread, session, credential, instance_id, timeline_id, character_id = await room(
        ctx.a, ctx.mgmt_a, moment=AWAKE_AT, channel="aud2-open", thread_id="dm-open",
    )
    channel_instance = thread["channel_id"]
    ctx.a.fake.replies = ["第一次开口，先打个招呼。"]
    await client.close()
    inbound_before = len([m for m in ctx.a.store.instance_messages(instance_id) if m["role"] == "user"])
    spoken = await ctx.a.world.first_contact(
        instance_id, timeline_id, character_id, channel_id=channel_instance, thread_id="dm-open",
        llm=ctx.a.fake, now_real=time.time(),
    )
    assert spoken.get("spoken") is True, spoken
    mid = str(spoken["message_id"])
    row = ctx.a.store.outbound_by_message_id(mid)
    assert row["reply_to"] is None and row["covers"] == "[]", row
    second = await ctx.a.world.first_contact(
        instance_id, timeline_id, character_id, channel_id=channel_instance, thread_id="dm-open",
        llm=ctx.a.fake, now_real=time.time(),
    )
    assert second.get("reused") is True and str(second["message_id"]) == mid, second
    inbound_after = len([m for m in ctx.a.store.instance_messages(instance_id) if m["role"] == "user"])
    assert inbound_after == inbound_before, "独立开场伪造了入站消息"
    again = UmpClient(endpoint=ctx.a.endpoint, channel_id="aud2-open", name="aud2-open", credential=credential)
    await again.connect()
    try:
        frames = await drain(again, 2.5)
    finally:
        await again.close()
    opening = [e for e in frames if e.type == "reply" and e.payload.get("message_id") == mid]
    assert opening, [e.type for e in frames]
    assert opening[-1].payload["reply_to"] is None and opening[-1].payload["batch_count"] == 1
    # 次要观察：管理面 op 用通道名当 channel_id 查 thread（ops.py:860）→ 命中不了 → 固化件没有投递目标
    other_pkg = example_package("灰潮纪", moment=AWAKE_AT)
    other_card = example_card(other_pkg)
    other = create_instance(ctx.a.store, other_pkg, [other_card])
    other_tl = ctx.a.store.timeline_list(other["id"])[0]["id"]
    ctx.a.world.ensure_instance(other["id"], now_real=time.time())
    ctx.a.world.activate(other["id"], other_tl, now_real=time.time())
    by_name = await ctx.a.world.first_contact(
        other["id"], other_tl, str(other_card["meta"]["card_id"]), channel_id="aud2-open", thread_id="dm-open",
        llm=ctx.a.fake, now_real=time.time(),
    )
    by_name_row = ctx.a.store.outbound_by_message_id(str(by_name["message_id"]))
    assert by_name_row["binding_token"] == "" and by_name_row["channel_id"] == "aud2-open", by_name_row
    return (f"独立开场：reply_to=null、自有 message_id={mid}、一次性（二次调用 reused=True 同一 message_id）、"
            f"不伪造入站（入站行数不变）、重连后按 reply_to=null 投递；"
            f"注：管理面 op 以通道名当 channel_id（ops.py:860，默认 'builtin'）查 thread 命中不了 → "
            f"这样固化出来的开场件 target 为空（binding_token=''，实际不可投递）")


async def k34(ctx: Ctx) -> str:
    """§2.3 / §十.10：归档说明的作者分类与类型（system_notice / notice）。"""
    client, thread, session, credential, instance_id, timeline_id, character_id = await room(
        ctx.a, ctx.mgmt_a, moment=AWAKE_AT, channel="aud2-arch", thread_id="dm-arch",
    )
    token = thread["binding_token"]
    generation = int(ctx.a.store.clock_get(timeline_id)["generation"])
    world_seconds = int(ctx.a.world.world_moment(instance_id, timeline_id)) + 100
    ctx.a.store.apply_runtime_batch(
        timeline_id=timeline_id, generation=generation, processed_world=world_seconds,
        catching_up=False, limited=False,
        events=[{
            "instance_id": instance_id, "timeline_id": timeline_id, "id": "ev-audit2-death",
            "world_seconds": world_seconds, "seq": 0, "kind": "character", "family": "",
            "template": f"death:{character_id}", "source": "engine",
            "summary": "堤禾身故", "detail": "堤禾身故", "text_source": "template",
            "effects": "[]", "share_value": 0, "importance": 0.8, "created_real": time.time(),
        }],
    )
    tasks_before = len(ctx.a.store.memory_tasks(instance_id, timeline_id))
    try:
        await client.send_user_message(thread_id="dm-arch", binding_token=token, text="在吗")
        frames = await drain(client, 3.0)
    finally:
        await client.close()
    notice = ctx.a.store.session_notice_get(str(session["id"]), "archive")
    assert notice is not None, "归档后没有产生归档说明"
    mid = str(notice["message_id"])
    row = ctx.a.store.outbound_by_message_id(mid)
    flat = ctx.a.store.message_text(row)
    wire = [e for e in frames if e.type == "system_notice" or e.payload.get("message_id") == mid]
    wire_type = wire[-1].type if wire else "(未在线上出现)"
    wire_text = str((wire[-1].payload or {}).get("text") or "") if wire else ""
    tasks_after = len(ctx.a.store.memory_tasks(instance_id, timeline_id))
    context = [m for m in ctx.a.store.context_window(str(session["id"]), 50) if m["message_id"] == mid]
    errors = [e.payload for e in frames if e.type == "error"]
    assert str(row["role"]) == "notice", f"归档说明不是联络系统分类：role={row['role']!r}"
    assert wire_type == "system_notice", f"归档说明线上类型不是 system_notice：{wire_type!r}"
    assert character_id not in flat, f"归档说明带内部角色标识：{flat}"
    assert not context, f"归档说明被收进角色上下文：{[(m['role'], m['message_id']) for m in context]}"
    assert tasks_after == tasks_before, f"归档说明触发了记忆提取：{tasks_before}→{tasks_after}"
    assert errors and all(e["code"] == ump.Err.STATE_BLOCKED for e in errors), errors
    return (
        f"归档说明 role={row['role']!r}、线上 type={wire_type!r}（正文「{wire_text[:24]}…」不带内部标识）；"
        f"不进角色上下文（命中 {len(context)} 行）；不触发提取（{tasks_before}→{tasks_after}）；后续输入被拒 {[e['code'] for e in errors]}"
    )


async def k24(ctx: Ctx) -> str:
    """§六 / §十.6：凭据与消息正文不进日志。"""
    assert ctx.a.log_file.exists(), f"未找到核心日志 {ctx.a.log_file}"
    text = ctx.a.log_file.read_text(encoding="utf-8", errors="replace")
    secrets_to_check = {
        "bootstrap": ctx.a.bootstrap,
        "mgmt": ctx.a.mgmt_token,
    }
    issued = ctx.a.store.channel_by_name("aud2-persist")
    assert issued is not None
    leaks = {name: value for name, value in secrets_to_check.items() if value and value in text}
    assert not leaks, f"日志出现凭据：{sorted(leaks)}"
    assert MARK not in text, "日志出现消息正文标记"
    assert "credential_hash" not in text and "cr-" not in text, "日志出现通道凭据字段"
    return (f"日志 {ctx.a.log_file.name}（{len(text)} 字节）不含 bootstrap / 管理令牌 / 通道凭据（cr-*）与消息正文标记 {MARK}")


async def k25(ctx: Ctx) -> str:
    """§十.6 / §十.10：导出包不含凭据；归档说明的分类随导出保留。"""
    package = example_package("灰潮纪", moment=AWAKE_AT)
    card = example_card(package)
    info = create_instance(ctx.a.store, package, [card])
    timeline_id = ctx.a.store.timeline_list(info["id"])[0]["id"]
    character_id = str(card["meta"]["card_id"])
    ctx.a.world.ensure_instance(info["id"], now_real=time.time())
    ctx.a.world.activate(info["id"], timeline_id, now_real=time.time())
    client, thread, session, credential = await bind_thread(
        ctx.a, ctx.mgmt_a, channel="aud2-export", thread_id="dm-exp",
        instance=info["id"], timeline=timeline_id, character=character_id,
    )
    try:
        await client.send_user_message(thread_id="dm-exp", binding_token=thread["binding_token"], text="导出前的一句")
        await replies(client, 1)
    finally:
        await client.close()
    target = ctx.a.cfg.paths.root / "exports" / "audit2.json"
    exported = await ctx.mgmt_a.call("instance.export", **{"id": info["id"], "path": "exports/audit2.json"})
    blob = target.read_text(encoding="utf-8")
    for label, value in (("通道凭据", credential), ("bootstrap", ctx.a.bootstrap), ("管理令牌", ctx.a.mgmt_token),
                         ("绑定令牌", str(thread["binding_token"]))):
        assert value and value not in blob, f"导出包出现{label}"
    data = json.loads(blob)
    messages = (data.get("runtime") or {}).get("messages") or []
    assert messages, "导出包没有对话"
    roles = sorted({str(m.get("role")) for m in messages})
    return (f"导出 {exported['path'] if 'path' in exported else target.name}（{len(blob)} 字节）不含通道凭据 / 引导 / 管理 / 绑定令牌；"
            f"对话 {len(messages)} 行、role 集合 {roles}（不含 notice：归档说明在库里也不带该分类）")


async def k26(ctx: Ctx) -> str:
    """§3.2 / §十.5：握手有界超时、坏帧与超大帧不拖死核心、协议错误达上限断连。"""
    ws = await ws_connect(ctx.a.endpoint)
    started = asyncio.get_running_loop().time()
    code = await expect_closed(ws, timeout=14)
    elapsed = asyncio.get_running_loop().time() - started
    assert 8.0 <= elapsed <= 13.5, f"握手超时耗时 {elapsed:.1f}s"
    cred = await cred_for(ctx.a, ctx.mgmt_a, "aud2-limit")
    ws2, _ = await channel_conn(ctx.a, "aud2-limit", credential=cred)
    for index in range(5):
        await sendj(ws2, {"ump": "1.0", "type": "ping", "id": f"e-bad{index}"})
    seen = 0
    for _ in range(5):
        env = await recvj(ws2)
        if env.get("type") == "error":
            seen += 1
    assert seen == 5, f"应有 5 条错误回包，实际 {seen}"
    closed = await expect_closed(ws2)
    cred3 = await cred_for(ctx.a, ctx.mgmt_a, "aud2-big")
    ws3, _ = await channel_conn(ctx.a, "aud2-big", credential=cred3)
    await ws3.send("x" * ((1 << 20) + 64))
    big_code = await expect_closed(ws3, timeout=8)
    await asyncio.sleep(0.2)  # 核心仍在服务
    alive, _ = await channel_conn(ctx.a, "aud2-limit2", credential=await cred_for(ctx.a, ctx.mgmt_a, "aud2-limit2"))
    await sendj(alive, ump.make("ping", {}))
    assert (await recvj(alive))["type"] == "pong"
    await alive.close()
    return (f"握手超时 {elapsed:.1f}s 后关闭（close={code}）；5 次解析期错误后断连（close={closed}）；"
            f"1 MiB+ 帧直接断开（close={big_code}）；核心随后仍能握手并回 pong")


async def k27(ctx: Ctx) -> str:
    """§十.5：不读数据的通道与硬崩溃的通道都不拖死核心。"""
    big = "\n".join(f"第{i}行：" + "世界内容" * 200 for i in range(1, 300))  # ~ 0.24 MB
    ctx.b.fake.replies = [big]
    credential = await cred_for(ctx.b, ctx.mgmt_b, "aud2-stall")
    session = (await ctx.mgmt_b.call("session.ensure", instance_id="ph-audit2", timeline_id="main",
                                     character_id="ph-audit2"))["session"]
    thread = (await ctx.mgmt_b.call("thread.bind", channel="aud2-stall", thread_id="dm-stall",
                                    session_id=session["id"]))["thread"]
    stalled = await ws_connect(ctx.b.endpoint, max_size=1 << 23)
    ack = await raw_hello(stalled, "aud2-stall", {"credential": credential})
    assert ack["type"] == "hello_ack", ack
    transport = getattr(stalled, "transport", None)
    assert transport is not None, "无法暂停读取：websockets 客户端未暴露 transport"
    transport.pause_reading()  # 只收不发：模拟通道端卡住不读
    try:
        await sendj(stalled, ump.make("user_message", {"text": "这条回复很大"}, thread_id="dm-stall",
                                      binding_token=thread["binding_token"], id="e-stall"))
        # 核心必须继续服务其它连接（真连接、真帧）
        probe_client, probe_thread, _ps, _pc = await bind_thread(ctx.b, ctx.mgmt_b, channel="aud2-alive", thread_id="dm-alive")
        started = asyncio.get_running_loop().time()
        await probe_client.send(ump.make("ping", {}))
        pong = await probe_client.expect(lambda e: e.type == "pong", timeout=5)
        latency = asyncio.get_running_loop().time() - started
        assert pong.type == "pong"
        await probe_client.send_user_message(thread_id="dm-alive", binding_token=probe_thread["binding_token"], text="另一条")
        await probe_client.expect(lambda e: e.type == "reply", timeout=15)
        await probe_client.close()
        # 大回复那一轮不能被卡死：批次必须进入终态（sent / unknown），核心不能假称成功也不能崩
        row = ctx.b.store.inbound_find(thread["channel_id"], "dm-stall", "e-stall")
        fixed = None
        for _ in range(60):
            row = ctx.b.store.inbound_find(thread["channel_id"], "dm-stall", "e-stall")
            if row and row.get("reply_message_id"):
                fixed = ctx.b.store.outbound_by_message_id(row["reply_message_id"])
                states = {r["batch_index"]: r["state"] for r in ctx.b.store.delivery_rows(fixed["seq"])}
                if all(s in ("sent", "unknown", "failed", "accepted", "incompatible") for s in states.values()):
                    break
            await asyncio.sleep(0.3)
        assert fixed is not None, "卡住的通道把这一轮拖得没有固化结果"
        stall_rollup = ctx.b.store.delivery_rollup(fixed["seq"])
        # 硬崩溃：直接 abort 底层连接（等价于通道进程被杀）
        crash_cred = await cred_for(ctx.b, ctx.mgmt_b, "aud2-crash")
        crash_thread = (await ctx.mgmt_b.call("thread.bind", channel="aud2-crash", thread_id="dm-crash",
                                                session_id=session["id"]))["thread"]
        crashed = await ws_connect(ctx.b.endpoint)
        await raw_hello(crashed, "aud2-crash", {"credential": crash_cred})
        assert hasattr(crashed, "transport")
        crashed.transport.abort()
        await asyncio.sleep(0.5)
        check_client, check_thread, _cs, _cc = await bind_thread(ctx.b, ctx.mgmt_b, channel="aud2-postcrash", thread_id="dm-pc")
        await check_client.send_user_message(thread_id="dm-pc", binding_token=check_thread["binding_token"], text="崩溃之后")
        await check_client.expect(lambda e: e.type == "reply", timeout=15)
        await check_client.close()
    finally:
        transport.resume_reading()
        await stalled.close()
    return (f"挂起读取的通道：核心 {latency:.2f}s 内照常回另一个连接的 pong、照常处理新轮次；该轮回复仍被固化并推进到终态"
            f"（rollup={stall_rollup}）；通道连接被硬 abort（模拟进程崩溃）后核心仍能握手、接受并回复新消息。"
            f"注：SEND_TIMEOUT_S=15s（session.py:27,607-614）在本实验里未被触发——websockets 16.1.1 的 send() 虽 await drain()，"
            f"但本机回环缓冲足以吸收 0.24–2.9MB 的推送，因此「发送结果未知」路径无法用挂起接收方构造")


async def k28(ctx: Ctx) -> str:
    """§六：错误信封五字段齐备且脱敏。"""
    client, thread, _session, _cred = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-errshape", thread_id="dm-err")
    token = thread["binding_token"]
    try:
        await client.send_user_message(thread_id="dm-none", binding_token="bt-x", text="错误信封形状")
        env = await client.expect(lambda e: e.type == "error")
        payload = env.payload
        assert set(payload) == {"code", "message", "retryable", "ref", "stage"}, payload
        assert payload["code"] == ump.Err.UNKNOWN_THREAD and payload["retryable"] is False
        assert payload["stage"] == ump.Stage.RECEIVE, payload
        ctx.a.fake.fail_with = RuntimeError("secret-internal-detail: 数据库连接串")
        try:
            await client.send_user_message(thread_id="dm-err", binding_token=token, text="内部错误")
            env2 = await client.expect(lambda e: e.type == "error", timeout=15)
        finally:
            ctx.a.fake.fail_with = None
        assert env2.payload["code"] == ump.Err.INTERNAL, env2.payload
        assert env2.payload["stage"] == ump.Stage.GENERATE, env2.payload
        assert "secret-internal-detail" not in json.dumps(env2.raw, ensure_ascii=False), "内部异常细节外泄"
        assert "你是 isekai 核心进程的占位对话端" not in json.dumps(env.raw, ensure_ascii=False), "错误里泄漏了提示词"
    finally:
        await client.close()
    return "错误信封仅含 code/message/retryable/ref/stage（固定枚举）；内部异常细节与提示词均不外泄"


async def k29(ctx: Ctx) -> str:
    """§2.3 / §六：生成失败保留输入与失败状态，显式 retry 恢复同一轮次。"""
    client, thread, _session, _cred, instance_id, timeline_id, _ch = await room(
        ctx.a, ctx.mgmt_a, moment=AWAKE_AT, channel="aud2-genfail", thread_id="dm-gf",
    )
    token = thread["binding_token"]
    channel_id = thread["channel_id"]
    try:
        ctx.a.fake.fail_with = LLMError("llm_unavailable", "HTTP 503", retryable=True)
        try:
            await client.send(ump.make("user_message", {"text": "会失败的一轮"}, thread_id="dm-gf",
                                       binding_token=token, id="e-gf"))
            env = await client.expect(lambda e: e.type == "error", timeout=15)
        finally:
            ctx.a.fake.fail_with = None
        assert env.payload["code"] == "llm_unavailable" and env.payload["retryable"] is True, env.payload
        assert env.payload["stage"] == ump.Stage.GENERATE and env.payload["ref"] == "e-gf", env.payload
        row = ctx.a.store.inbound_find(channel_id, "dm-gf", "e-gf")
        assert row["state"] == "failed" and row["error_code"] == "llm_unavailable", row
        calls = len(ctx.a.fake.calls)
        await client.request_retry(thread_id="dm-gf", binding_token=token, ref="e-gf", kind="input")
        accepted = await client.expect(lambda e: e.type == "accepted")
        assert accepted.payload["state"] in ("processing", "queued"), accepted.payload
        reply = (await replies(client, 1))[0]
        assert reply.payload["reply_to"] == "e-gf", reply.payload
        assert len(ctx.a.fake.calls) == calls + 1, "retry 未恢复同一轮次"
        again = ctx.a.store.inbound_find(channel_id, "dm-gf", "e-gf")
        assert again["seq"] == row["seq"] and again["state"] == "done", again
    finally:
        await client.close()
    return ("生成失败 → error(llm_unavailable, retryable, stage=generate, ref=e-gf)，输入保留 failed/error_code；"
            "retry(kind=input) 在同一行 seq 上恢复并成功")


async def k30(ctx: Ctx) -> str:
    """§3.3 / §十.9：协商失败状态不接受普通消息、不降级，投递不受阻，恢复后可显式重新启用。"""
    mgmt = ctx.mgmt_c
    credential = await cred_for(ctx.c, mgmt, "aud2-blocked")
    session = (await mgmt.call("session.ensure", instance_id="ph-audit2", timeline_id="main",
                               character_id="ph-audit2"))["session"]
    thread = (await mgmt.call("thread.bind", channel="aud2-blocked", thread_id="dm-blk", session_id=session["id"]))["thread"]
    client = UmpClient(endpoint=ctx.c.endpoint, channel_id="aud2-blocked", name="aud2-blocked", credential=credential)
    ack = await client.connect()
    assert ack["state"] == "compatibility_blocked", ack
    try:
        rows = ctx.c.store.counts()["messages"]
        await client.send_user_message(thread_id="dm-blk", binding_token=thread["binding_token"], text="冻结期间")
        env = await client.expect(lambda e: e.type == "error")
        assert env.payload["code"] == ump.Err.STATE_BLOCKED, env.payload
        assert env.payload["retryable"] is True and env.payload["stage"] == ump.Stage.RECEIVE, env.payload
        assert ctx.c.store.counts()["messages"] == rows, "受阻状态仍落库"
        await client.send(ump.make("delivery", {"message_id": "m-nope", "batch_index": 0, "state": "accepted"},
                                   thread_id="dm-blk", binding_token=thread["binding_token"]))
        delivery = await client.expect(lambda e: e.type == "error")
        assert delivery.payload["code"] == ump.Err.NOT_FOUND, delivery.payload  # 投递不被状态门拦死
        ctx.c.runtime.server.state = "ready"  # 用户更新插件 / 核心后显式恢复
        await client.send_user_message(thread_id="dm-blk", binding_token=thread["binding_token"], text="恢复之后")
        accepted = await client.expect(lambda e: e.type == "accepted")
        assert accepted.payload["state"] in ("queued", "processing", "done"), accepted.payload
        await replies(client, 1)
    finally:
        await client.close()
    return ("state=compatibility_blocked：hello_ack 如实上报，普通消息 → state_blocked(retryable, stage=receive) 且不落库，"
            "投递帧不被状态门拦截（NOT_FOUND 而非 STATE_BLOCKED）；恢复 ready 后消息照常接受并回复")


async def k31(ctx: Ctx) -> str:
    """§4 / §十.7：内建聊天未连接（停用态）时管理面照常可用，重连不重置历史。"""
    client, thread, _session, credential = await bind_thread(ctx.a, ctx.mgmt_a, channel="aud2-builtin", thread_id="dm-b")
    token = thread["binding_token"]
    channel_id = thread["channel_id"]
    await client.send_user_message(thread_id="dm-b", binding_token=token, text="停用前的一句")
    await replies(client, 1)
    history_before = ctx.a.store.history_page(_session["id"], limit=50)["messages"]
    await client.close()  # 停用内建聊天：通道离线，管理面与历史保留
    status = await ctx.mgmt_a.call("status")
    threads = await ctx.mgmt_a.call("thread.list")
    page = await ctx.mgmt_a.call("history.page", session_id=_session["id"], limit=50)
    assert status["ump"] == "1.0" and status["state"] == "ready", status
    assert any(t["thread_id"] == "dm-b" for t in threads["threads"])
    assert status["channels_connected"] == [] or "aud2-builtin" not in [
        cid for cid in status["channels_connected"] if cid == channel_id
    ]
    assert len(page["messages"]) == len(history_before), "停用后历史行数变化"
    again = UmpClient(endpoint=ctx.a.endpoint, channel_id="aud2-builtin", name="aud2-builtin", credential=credential)
    ack = await again.connect()
    try:
        assert {t["id"]: t["binding_token"] for t in ack["threads"]}["dm-b"] == token, "重连后绑定令牌变了"
        assert len(ctx.a.store.history_page(_session["id"], limit=50)["messages"]) == len(history_before)
    finally:
        await again.close()
    return (f"通道离线期间 status/thread.list/history.page 均正常（history {len(page['messages'])} 行不变），"
            f"重连恢复同一 thread 令牌，世界 / 历史未被重置")


async def k32(ctx: Ctx) -> str:
    """§一 / §2.1：UMP 线上不出现世界实例 / 时间线 / 角色内部标识。"""
    client, thread, session, _cred, instance_id, timeline_id, character_id = await room(
        ctx.a, ctx.mgmt_a, moment=AWAKE_AT, channel="aud2-noscope", thread_id="dm-ns",
    )
    token = thread["binding_token"]
    seen: list[Any] = []
    try:
        await client.send_user_message(thread_id="dm-ns", binding_token=token, text="在吗")
        await client.expect(lambda e: e.type == "reply", timeout=15, collect=seen)
    finally:
        await client.close()
    wire = json.dumps([client.hello_ack] + [e.raw for e in seen], ensure_ascii=False)
    leaked = [v for v in (instance_id, timeline_id, character_id, str(session["id"])) if v in wire]
    assert not leaked, f"UMP 线上出现内部标识：{leaked}"
    assert "prompt" not in wire.lower() and "memory" not in wire.lower()
    return (f"hello_ack + 全部收信帧不含实例 {instance_id} / 时间线 {timeline_id} / 角色 {character_id} / 会话 {session['id']}，"
            f"也不含 prompt / memory 字段（仅通道实例 id 与不透明令牌）")


async def k35(ctx: Ctx) -> str:
    """§六 / §十.7：认证 / 协议永久错误不降级、不形成重连风暴。"""
    outcomes: list[tuple[str, bool]] = []
    for _ in range(3):  # 同一坏凭据连试 3 次：核心每次都拒绝（不因重试而放行）
        ws = await ws_connect(ctx.a.endpoint)
        env = await raw_hello(ws, "aud2-storm", {"credential": "cr-nope"})
        assert err_code(env) == ump.Err.AUTH_FAILED, env
        assert env["payload"]["retryable"] is False and env["payload"]["stage"] == ump.Stage.AUTH, env["payload"]
        assert await expect_closed(ws) == 1008
        outcomes.append((str(env["payload"]["code"]), bool(env["payload"]["retryable"])))
    ws2 = await ws_connect(ctx.a.endpoint)
    env2 = await raw_hello(ws2, "aud2-storm", {})
    assert err_code(env2) == ump.Err.AUTH_REQUIRED, env2
    assert env2["payload"]["retryable"] is False, env2["payload"]
    assert await expect_closed(ws2) == 1008
    assert ctx.a.store.channel_by_name("aud2-storm") is None, "永久失败的握手留下了通道登记"
    return (f"连续 3 次坏凭据握手都是 auth_failed(retryable=False, stage=auth)+1008 关闭，不因重试放行、也不降级为无认证"
            f"（无 auth → auth_required），且不留通道登记；{outcomes}。"
            f"壳侧重连有界：RECONNECT_DELAYS_MS=[1000,2000,4000,8000,16000] 用尽后停止并提示重启（desktop/src/main.ts:244,301-305，静态复核）")


# --------------------------------------------- 插件宿主 / 进程内通道 / 通知 / 能力边界（§3.1–§3.4、§2.5）

#: 探针用最小通道插件（UMP over stdio）：启动留痕、入口参数原样记下、握手后把环境落盘
PLUGIN_STUB = '''"""探针用最小通道插件（UMP over stdio）。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def send(frame):
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\\n")
    sys.stdout.flush()


with open(os.path.join(HERE, "booted.txt"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv, ensure_ascii=False) + "\\n")

send({
    "ump": "1.0", "type": "hello", "id": "p-1", "ts": 0,
    "payload": {
        "channel": {"id": os.environ["ISEKAI_PLUGIN_ID"], "name": "探针插件", "version": "0.1.0"},
        "capabilities": {"segments": True, "status": True},
        "auth": {"credential": os.environ["ISEKAI_PLUGIN_CREDENTIAL"]},
    },
})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    frame = json.loads(line)
    if frame.get("type") == "hello_ack":
        with open(os.path.join(HERE, "seen.json"), "w", encoding="utf-8") as fh:
            json.dump({"state": frame["payload"].get("state"), "env": dict(os.environ)}, fh, ensure_ascii=False)
'''

#: 探针用「立刻崩溃」插件：启动次数留痕，用来看有没有重启风暴
PLUGIN_CRASH = '''"""探针用「立刻崩溃」插件。"""
import os
import sys

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "starts.txt"), "a", encoding="utf-8") as fh:
    fh.write("start\\n")
sys.stderr.write("boom: 探针插件立刻退出\\n")
sys.exit(3)
'''

#: 探针用「活着但永不握手」插件：把「启用 = 握手成功才 running」这条判据逼到底
PLUGIN_HANG = '''"""探针用「永不握手」插件：只留痕，不发 hello，也不退出。"""
import os
import sys

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "booted.txt"), "a", encoding="utf-8") as fh:
    fh.write("start\\n")
for _line in sys.stdin:
    pass
'''

#: 探针用「刷 stderr」插件：握手照做，同时灌 300 行 stderr（其中一行 1200 字符）
PLUGIN_CHATTY = '''"""探针用「刷 stderr」插件。"""
import json
import os
import sys

for index in range(300):
    sys.stderr.write("line-%03d%s\\n" % (index, "x" * 1200 if index == 299 else ""))
sys.stderr.flush()


def send(frame):
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\\n")
    sys.stdout.flush()


send({
    "ump": "1.0", "type": "hello", "id": "p-1", "ts": 0,
    "payload": {
        "channel": {"id": os.environ["ISEKAI_PLUGIN_ID"], "name": "话痨插件", "version": "0.1.0"},
        "capabilities": {"segments": False, "status": False},
        "auth": {"credential": os.environ["ISEKAI_PLUGIN_CREDENTIAL"]},
    },
})

for _line in sys.stdin:
    pass
'''


def write_plugin(folder: Path, plugin_id: str, source: str, *, entry: list[str] | None = None,
                 version: str = "0.1.0") -> Path:
    """按 §3.1 的目录制写一份插件（manifest + 入口），返回插件目录。"""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.py").write_text(source, encoding="utf-8")
    manifest = {
        "id": plugin_id, "name": f"{plugin_id} 探针插件", "version": version, "ump": "1.x",
        "entry": entry or ["python", "main.py"], "description": "审计探针", "author": "audit2",
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return folder


def plugin_host(h: Harness, tag: str) -> plugins.PluginHost:
    """一块独立插件目录 + 宿主（同 app.build_runtime 的构造方式）。"""
    return plugins.PluginHost(
        cfg=h.cfg, store=h.store, server=h.runtime.server, folder=h.cfg.paths.root / "plugins" / tag
    )


def plugin_env(plugin_dir: Path) -> dict[str, str]:
    """读探针插件落盘的环境快照（握手成功那一刻的 os.environ）。"""
    return json.loads((plugin_dir / "seen.json").read_text(encoding="utf-8"))["env"]


async def until(predicate: Callable[[], bool], *, timeout: float = 8.0, step: float = 0.1) -> bool:
    """等一个条件成立（子进程是另一条时间线，落盘 / 退出的可见性有延迟）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return bool(predicate())


async def local_frames(channel: InProcessChannel, wanted: set[str], *, pre: list[dict[str, Any]] | None = None,
                       timeout: float = 10.0) -> list[dict[str, Any]]:
    """进程内通道没有 drain：凑齐想看的帧类型后返回（帧都在这条通道自己的 sent 缓冲里）。"""
    seen = list(pre or [])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not wanted <= {str(frame.get("type")) for frame in seen}:
        seen.extend(channel.ws.take())
        await asyncio.sleep(0.05)
    seen.extend(channel.ws.take())
    return seen


async def d01(ctx: Ctx) -> str:
    """§3.1：目录制 manifest 扫描 / 登记——只读清单、不跑代码；坏清单与缺入口在启用前就报出来。"""
    folder = ctx.a.cfg.paths.root / "plugins" / "d01"
    write_plugin(folder / "boot", "aud2-boot", PLUGIN_STUB)
    (folder / "flat.json").write_text(
        json.dumps({"id": "aud2-flat", "name": "平铺清单插件", "version": "0.2.0", "ump": "1.x",
                    "entry": ["python", "main.py"], "description": "平铺放法", "author": "audit2"},
                   ensure_ascii=False), encoding="utf-8")
    (folder / "broken.json").write_text("{ 坏清单", encoding="utf-8")
    write_plugin(folder / "missing", "aud2-missing", PLUGIN_STUB, entry=["python-nonexistent", "main.py"])
    scanned = {str(item["id"]): item for item in plugins.scan(folder)}
    host = plugin_host(ctx.a, "d01")
    plugins.install(host)  # 挂上宿主：此后 plugin.list / plugin.enable 走同一份登记
    listed = {str(item["id"]): item for item in host.list_plugins()}
    via_mgmt = (await ctx.mgmt_a.call("plugin.list"))["plugins"]
    item = listed.get("aud2-boot") or {}
    assert item.get("id") == "aud2-boot" and item.get("name") == "aud2-boot 探针插件", listed
    assert item.get("version") == "0.1.0" and item.get("entry") == ["python", "main.py"], item
    assert item.get("errors") == [] and item.get("state") == "registered", item
    assert "aud2-flat" in scanned and "aud2-flat" in listed, f"平铺 <root>/*.json 清单未识别：{sorted(scanned)}"
    broken = scanned.get("") or {}
    assert broken.get("errors"), f"坏清单未如实报错：{scanned}"
    missing = listed.get("aud2-missing") or {}
    assert any("找不到" in err for err in missing.get("errors") or []), missing
    assert not (folder / "boot" / "booted.txt").exists(), "扫描阶段运行了插件代码"
    assert {str(row["id"]) for row in via_mgmt} >= {"aud2-boot", "aud2-flat", "aud2-missing"}, via_mgmt
    refused = await host.enable("aud2-missing", timeout=3.0)
    assert refused.get("enabled") is False and refused.get("state") == "invalid", refused
    assert "aud2-missing" not in host._running and not (folder / "missing" / "booted.txt").exists(), \
        "启用前检查失败却起来了进程"
    return (f"目录制 + 平铺 <root>/*.json 两种清单都扫到（{sorted(k for k in listed if k)}），登记行含 "
            f"id/name/version={item['version']}/entry={item['entry']}；坏清单如实报错「{broken['errors'][0][:20]}…」，"
            f"缺入口报「找不到」且启用被拒（state=invalid、未起进程）；扫描后 booted.txt 不存在（只读清单、不跑代码）；"
            f"管理面 plugin.list 同一份登记 {len(via_mgmt)} 条")


async def d02(ctx: Ctx) -> str:
    """§2.5 / §3.2：插件跑在子进程；stdio 上按行 NDJSON 握手；入口参数不经 shell；停用后进程真的没了。"""
    folder = ctx.a.cfg.paths.root / "plugins" / "d02"
    arg = "& echo pwned > pwned.txt"  # 若有人把它们拼成 shell 串，这条会被当命令跑
    entry = ["python", "main.py", arg]
    plugin_dir = write_plugin(folder / "trans", "aud2-trans", PLUGIN_STUB, entry=entry)
    host = plugin_host(ctx.a, "d02")
    plugins.install(host)
    started = (await ctx.mgmt_a.call("plugin.enable", id="aud2-trans"))["enable"]
    assert started.get("enabled") is True and started.get("state") == "running", started
    assert await until(lambda: (plugin_dir / "seen.json").exists(), timeout=10), "启用返回 running 但插件未握手"
    seen = json.loads((plugin_dir / "seen.json").read_text(encoding="utf-8"))
    assert seen["state"] == "ready", seen["state"]
    argv = json.loads((plugin_dir / "booted.txt").read_text(encoding="utf-8").splitlines()[0])
    assert argv == entry[1:], f"入口参数未按原样成为 argv：{argv}"
    assert not (plugin_dir / "pwned.txt").exists(), "入口参数被 shell 执行了"
    proc = host._running["aud2-trans"]["proc"]
    assert proc.returncode is None, "启用说 running，子进程却已退出"
    stopped = await host.disable("aud2-trans")
    assert stopped["state"] == "stopped" and "aud2-trans" not in host._running, stopped
    assert await until(lambda: proc.returncode is not None, timeout=6), "停用后插件进程还在"
    assert stopped["how"] == "clean", f"停用未走有界退出：{stopped}"
    assert str(ctx.a.store.plugin_get("aud2-trans")["state"]) == "stopped", ctx.a.store.plugin_get("aud2-trans")
    return (f"管理面 plugin.enable 拉起子进程 pid={proc.pid}：stdio 上按行 NDJSON 握手（插件侧 hello_ack.state=ready）；"
            f"入口 argv={argv} 原样进进程、`{arg}` 未被执行（不经 shell）；停用 → how=clean 有界退出、"
            f"停用后 returncode={proc.returncode}（进程真的没了）、登记行 state=stopped")


async def d03(ctx: Ctx) -> str:
    """§3.2：启用要握手成功才算 running；崩溃标 failed 不重启风暴；手工更新不换身份；卸载保留核心历史。"""
    folder = ctx.a.cfg.paths.root / "plugins" / "d03"
    life = write_plugin(folder / "life", "aud2-life", PLUGIN_STUB)
    boom = write_plugin(folder / "boom", "aud2-boom", PLUGIN_CRASH)
    host = plugin_host(ctx.a, "d03")
    first = await host.enable("aud2-life", timeout=20)
    assert first.get("enabled") is True and first.get("state") == "running", first
    row = ctx.a.store.channel_by_name("aud2-life")
    caps = json.loads(row["capabilities"])
    assert caps.get("segments") is True and caps.get("status") is True, \
        f"启用说 running 时通道上还没有插件自报的能力声明：{row}"
    cred1 = plugin_env(life)["ISEKAI_PLUGIN_CREDENTIAL"]
    channel_id = str(row["id"])
    assert (life / "booted.txt").read_text(encoding="utf-8").count("\n") == 1, "一次启用起了多个进程"
    # 手工更新：先停、换文件、再启用——通道身份不换，凭据换新（旧的立刻失效）
    assert (await host.disable("aud2-life"))["state"] == "stopped"
    (life / "seen.json").unlink()
    write_plugin(life, "aud2-life", PLUGIN_STUB, version="0.2.0")
    second = await host.enable("aud2-life", timeout=20)
    assert second.get("enabled") is True and second.get("state") == "running", second
    assert await until(lambda: (life / "seen.json").exists(), timeout=10), "更新后未重新握手"
    cred2 = plugin_env(life)["ISEKAI_PLUGIN_CREDENTIAL"]
    row2 = ctx.a.store.channel_by_name("aud2-life")
    assert str(row2["id"]) == channel_id, f"手工更新换了通道身份：{channel_id} → {row2['id']}"
    assert str(row2["version"]) == "0.2.0", row2
    assert cred2 and cred1 != cred2, "重新启用没有换凭据"
    ws = await ws_connect(ctx.a.endpoint)
    stale = await raw_hello(ws, "aud2-life", {"credential": cred1})
    assert err_code(stale) == ump.Err.AUTH_FAILED, f"上一版凭据仍可用：{stale}"
    await ws.close()
    # 崩溃：进程立刻退出 → failed，且只启动过一次（不重启风暴）
    crashed = await host.enable("aud2-boom", timeout=20)
    assert crashed.get("enabled") is False and crashed.get("state") == "failed", crashed
    assert "立刻退出" in str(crashed.get("note")), crashed
    assert await until(lambda: any("boom" in line for line in host._stderr.get("aud2-boom") or []), timeout=5), \
        f"崩溃插件的 stderr 摘要没留下：{host._stderr.get('aud2-boom')}"
    await asyncio.sleep(1.5)
    starts = (boom / "starts.txt").read_text(encoding="utf-8").split()
    assert len(starts) == 1, f"崩溃后自动重启了（启动 {len(starts)} 次）"
    assert "aud2-boom" not in host._running, "崩溃的插件仍被当作在运行"
    assert str(ctx.a.store.plugin_get("aud2-boom")["state"]) == "failed", ctx.a.store.plugin_get("aud2-boom")
    # 启用必须以**这一次**的握手为准：上一轮留下的通道能力不能顶替（换成一个永不握手的实现再启用）
    hang = write_plugin(folder / "hang", "aud2-hang", PLUGIN_STUB)
    assert (await host.enable("aud2-hang", timeout=20)).get("enabled") is True
    assert await until(lambda: (hang / "seen.json").exists(), timeout=10), "首次启用未握手"
    assert (await host.disable("aud2-hang"))["state"] == "stopped"
    (hang / "seen.json").unlink()
    write_plugin(hang, "aud2-hang", PLUGIN_HANG)  # 换文件：这次不发 hello，也不退出
    stuck = await host.enable("aud2-hang", timeout=3.0)
    handshook = await until(lambda: (hang / "seen.json").exists(), timeout=1.0)
    assert handshook or not stuck.get("enabled"), \
        f"启用没以本次握手为准：插件从未发 hello（无 hello_ack），宿主却报了运行中：{stuck}"
    assert "握手超时" in str(stuck.get("note")), f"启用失败没给明确原因：{stuck}"
    assert str(ctx.a.store.channel_by_name("aud2-hang")["capabilities"]) == "{}", \
        "上一轮的能力声明没被清掉：这一轮握手与否无从判断"
    assert (await host.disable("aud2-hang"))["state"] == "stopped"
    # 卸载：先在这条通道上真聊一轮，卸载后核心会话 / 消息一行不动
    session = (await ctx.mgmt_a.call("session.ensure", instance_id="ph-audit2", timeline_id="main",
                                     character_id="ph-audit2"))["session"]
    thread = (await ctx.mgmt_a.call("thread.bind", channel="aud2-life", thread_id="dm-life",
                                    session_id=session["id"]))["thread"]
    client = UmpClient(endpoint=ctx.a.endpoint, channel_id="aud2-life", name="aud2-life", credential=cred2)
    await client.connect()
    try:
        await client.send_user_message(thread_id="dm-life", binding_token=thread["binding_token"], text="卸载前的一句")
        reply = (await replies(client, 1))[0]
    finally:
        await client.close()
    history_before = [m["message_id"] for m in ctx.a.store.history_page(session["id"], limit=50)["messages"]]
    proc = host._running["aud2-life"]["proc"]
    out = await host.uninstall("aud2-life")
    assert out["uninstalled"] == "aud2-life", out
    assert ctx.a.store.plugin_get("aud2-life") is None, "卸载后插件登记行还在"
    assert await until(lambda: proc.returncode is not None, timeout=6), "卸载后插件进程还在"
    history_after = [m["message_id"] for m in ctx.a.store.history_page(session["id"], limit=50)["messages"]]
    assert history_after == history_before and reply.payload["message_id"] in history_after, \
        f"卸载动了核心历史：{history_before} → {history_after}"
    assert ctx.a.store.session_get(session["id"]) is not None, "卸载删了核心会话行"
    return (f"启用 → 握手成功（通道能力声明来自插件自己的 hello）才算 running；手工更新（v0.1.0→0.2.0 换文件重启用）"
            f"通道身份不变（{channel_id}）、凭据轮换（旧凭据 → auth_failed）；换成一个永不握手的实现再启用 → 不被认作"
            f"运行中（{stuck.get('state')}：{stuck.get('note')!r}，且启用时已把上一轮能力声明清回 \"{{}}\"）；"
            f"崩溃插件 → failed（note 含「立刻退出」+ stderr「boom…」）且只启动 1 次、不重启风暴；卸载 → 插件登记行消失、"
            f"插件进程退出，核心会话 {session['id']} 与 {len(history_after)} 行消息原样保留"
            f"（注：通道身份行 {channel_id} 与 thread 绑定仍在——plugins.py:321 自述「通道绑定由管理面另行解绑」，"
            f"SPEC §3.2 表措辞为「移除插件登记 / 绑定」）")


async def d05(ctx: Ctx) -> str:
    """§3.4：子进程环境只给必要变量 + 该插件自己的凭据，不继承 LLM Key / 管理凭据。"""
    folder = ctx.a.cfg.paths.root / "plugins" / "d05"
    plugin_dir = write_plugin(folder / "env", "aud2-env", PLUGIN_STUB)
    host = plugin_host(ctx.a, "d05")
    result = await host.enable("aud2-env", timeout=20)
    assert result.get("enabled") is True, result
    assert await until(lambda: (plugin_dir / "seen.json").exists(), timeout=10), "插件未握手"
    env = plugin_env(plugin_dir)
    allowed = set(plugins.ENV_ALLOWLIST) | {"PYTHONUNBUFFERED", "ISEKAI_PLUGIN_ID", "ISEKAI_PLUGIN_CREDENTIAL"}
    extra = sorted(set(env) - allowed)
    assert not extra, f"子进程拿到白名单外的变量：{extra}"
    leaked = sorted(key for key in env if any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET")))
    assert not leaked, f"子进程环境带核心凭据类变量：{leaked}"
    values = set(env.values())
    assert ctx.a.mgmt_token and ctx.a.mgmt_token not in values, "管理凭据进了插件子进程环境"
    assert ctx.a.bootstrap and ctx.a.bootstrap not in values, "引导凭据进了插件子进程环境"
    assert env.get("ISEKAI_PLUGIN_ID") == "aud2-env", env.get("ISEKAI_PLUGIN_ID")
    assert env.get("ISEKAI_PLUGIN_CREDENTIAL") and env["ISEKAI_PLUGIN_CREDENTIAL"] != ctx.a.mgmt_token, \
        "插件自己那条通道的凭据没给"
    assert "PATH" in env, "PATH 都没给（子进程起不来）"
    assert (await host.disable("aud2-env"))["state"] == "stopped"
    return (f"子进程实收 {len(env)} 个变量，全部落在白名单内（{sorted(env)}）；无任何 KEY/TOKEN/SECRET 类键"
            f"（{len(leaked)} 个），不含管理令牌与引导凭据（按值比对），含自己的 ISEKAI_PLUGIN_ID 与 "
            f"ISEKAI_PLUGIN_CREDENTIAL（工作凭据，已用它完成握手）")


async def d06(ctx: Ctx) -> str:
    """§2.5：进程内传输（安卓内建 / 不占回环端口）与桌面 WS 走同一段握手 / 认证 / 校验代码。"""
    local = InProcessChannel(ctx.b.runtime.server, channel_id="aud2-inproc-boot", name="安卓内建")
    ack = await local.connect(bootstrap=ctx.b.bootstrap)
    assert ack.get("type") == "hello_ack", ack
    assert ack["payload"]["state"] == "ready", ack["payload"]
    assert int(ack["payload"]["negotiated"]["max_text_len"]) >= 1, ack["payload"]["negotiated"]
    issued = str(ack["payload"].get("credential") or "")
    assert issued, "引导换持久凭据的语义应与桌面一致"
    await local.close()
    forged = InProcessChannel(ctx.b.runtime.server, channel_id="aud2-inproc-boot", name="安卓内建")
    bad = await forged.connect(credential="cr-forged")
    assert bad.get("type") == "error" and bad["payload"]["code"] == ump.Err.AUTH_FAILED, bad
    await forged.close()
    client, thread, session, credential, _instance, _timeline, _character = await room(
        ctx.b, ctx.mgmt_b, moment=AWAKE_AT, channel="aud2-inproc", thread_id="dm-in",
    )
    await client.close()  # 收发都改走进程内：只借它的通道与 thread 绑定
    live = InProcessChannel(ctx.b.runtime.server, channel_id="aud2-inproc", name="安卓内建")
    ack2 = await live.connect(credential=credential)
    assert ack2.get("type") == "hello_ack" and ack2["payload"]["state"] == "ready", ack2
    token = thread["binding_token"]
    refusal = await live.send(ump.make("user_message", {"text": "带张图", "attachments": [{"kind": "image"}]},
                                      thread_id="dm-in", binding_token=token, id="e-in-bad"))
    assert refusal and refusal[0]["type"] == "error", refusal
    assert refusal[0]["payload"]["code"] == ump.Err.UNSUPPORTED_CAPABILITY, refusal
    text = await live.send(ump.make("user_message", {"text": "进程内这一句"}, thread_id="dm-in",
                                    binding_token=token, id="e-in-1"))
    text += await live.send(ump.make("ping", {}))
    frames = await local_frames(live, {"pong", "reply"}, pre=text)
    kinds = [str(frame["type"]) for frame in frames]
    assert "accepted" in kinds, frames
    assert "pong" in kinds, frames
    got_reply = [f for f in frames if f["type"] == "reply"]
    assert got_reply and got_reply[-1]["payload"]["reply_to"] == "e-in-1", f"进程内传输没走完一轮：{kinds}"
    await live.close()
    return (f"进程内通道：引导握手 → hello_ack(state=ready，签发持久凭据 {issued[:6]}…)，错凭据 → auth_failed；"
            f"同一通道用持久凭据重连后收发照常（accepted + pong + 一轮 reply {got_reply[-1]['payload']['message_id']}"
            f"（covers={got_reply[-1]['payload']['covers']}））；带 attachments 的消息同样被明确拒绝"
            f"（unsupported_capability）——认证 / 校验 / 投递是同一段代码，本检查全程没有开回环连接 / 占端口")


async def d07(ctx: Ctx) -> str:
    """§2.5 末条：通知只引用已固化消息；重复登记幂等；失效只报管理错误——不改投、不激活冻结线。"""
    client, thread, session, _credential, instance_id, timeline_id, _character = await room(
        ctx.a, ctx.mgmt_a, moment=AWAKE_AT, channel="aud2-notice", thread_id="dm-nt",
    )
    await client.send_user_message(thread_id="dm-nt", binding_token=thread["binding_token"], text="通知要引用的这一条")
    reply = (await replies(client, 1))[0]
    mid = reply.payload["message_id"]
    await client.close()
    args = {"instance_id": instance_id, "timeline_id": timeline_id, "session_id": session["id"],
            "message_id": mid, "revision": 3}
    first = (await ctx.mgmt_a.call("notice.create", **args))["notice"]
    again = (await ctx.mgmt_a.call("notice.create", **args))["notice"]
    assert first["id"] == again["id"], f"同一条固化消息登记出两条通知：{first} / {again}"
    rows = (await ctx.mgmt_a.call("notice.list", instance_id=instance_id))["notices"]
    assert len(rows) == 1 and rows[0]["id"] == first["id"], rows
    target = (await ctx.mgmt_a.call("notice.resolve", id=first["id"]))["target"]
    assert target["valid"] is True, target
    assert target["session_id"] == session["id"] and target["message_id"] == mid, target
    assert target["revision"] == 3 and target["message_seq"] is not None, target
    by_message = (await ctx.mgmt_a.call("notice.resolve", message_id=mid))["target"]
    assert by_message["session_id"] == session["id"], by_message
    # 时间线冻结 / 归档：只报管理错误，仍指向原会话，不把冻结线激活
    ctx.a.store.timeline_set_state(timeline_id, "archived")
    frozen = (await ctx.mgmt_a.call("notice.resolve", id=first["id"]))["target"]
    assert frozen["valid"] is False and "归档" in frozen["reason"], frozen
    assert frozen["session_id"] == session["id"], f"失效后改投了：{frozen}"
    states = [str(t["state"]) for t in ctx.a.store.timeline_list(instance_id) if str(t["id"]) == timeline_id]
    assert states == ["archived"], f"解析通知把冻结线激活了：{states}"
    # 消息被删（回滚 / 作废）：同上，只报原因
    ctx.a.store.timeline_set_state(timeline_id, "active")
    with ctx.a.store._lock, ctx.a.store._conn:
        ctx.a.store._conn.execute("DELETE FROM message WHERE session_id=? AND message_id=?", (session["id"], mid))
    gone = (await ctx.mgmt_a.call("notice.resolve", id=first["id"]))["target"]
    assert gone["valid"] is False and "消息已不存在" in gone["reason"], gone
    assert gone["session_id"] == session["id"], gone
    stored = ctx.a.store.notice_get(first["id"])
    assert stored["session_id"] == session["id"] and stored["message_id"] == mid, stored
    return (f"notice.create 两次登记同一固化消息 → 同一通知 {first['id']}（notice 表只 1 行）；resolve 命中 → "
            f"valid=true、原会话 {session['id']} + message_id={mid} + 固定 revision=3（按 message_id 也能查）；"
            f"时间线归档后 → valid=false、reason「{frozen['reason']}」；消息被删后 → reason「{gone['reason']}」；"
            f"两种情况 target.session_id 都仍是原会话（不改投），冻结线状态未被改回（仍 {states[0]}）")


async def d09(ctx: Ctx) -> str:
    """§2.1 / §七 更后置：v1 只文本——附件 / 流式 / content_type 明确拒绝；插件侧受限日志有容量上限。"""
    caps = ump.parse_hello({"channel": {"id": "aud2-caps"}, "capabilities": {},
                            "auth": {"bootstrap": "b"}})["capabilities"]
    assert caps["text"] is True and caps["attachments"] is False and caps["stream"] is False, caps
    client, thread, _session, _credential = await bind_thread(
        ctx.a, ctx.mgmt_a, channel="aud2-bounds", thread_id="dm-bd",
    )
    token = thread["binding_token"]
    channel_id = thread["channel_id"]
    refused: list[str] = []
    try:
        for label, extra in (("attachments", {"attachments": [{"kind": "image"}]}),
                             ("stream", {"stream": True}),
                             ("content_type", {"content_type": "image/png"})):
            ref = f"e-{label}"
            await client.send(ump.make("user_message", {"text": "在吗", **extra}, thread_id="dm-bd",
                                       binding_token=token, id=ref))
            frames = await drain(client, 1.0)
            errors = [e for e in frames if e.type == "error"]
            assert errors and errors[-1].payload["code"] == ump.Err.UNSUPPORTED_CAPABILITY, \
                (label, [e.type for e in frames], [e.payload for e in frames if e.type == "error"])
            assert errors[-1].payload["stage"] == ump.Stage.PROTOCOL, errors[-1].payload
            assert not [e for e in frames if e.type in ("accepted", "reply")], \
                f"{label} 没被明确拒绝，还收了：{[e.type for e in frames]}"
            assert ctx.a.store.inbound_find(channel_id, "dm-bd", ref) is None, f"{label} 被静默收下并落库"
            refused.append(f"{label}→{errors[-1].payload['code']}")
    finally:
        await client.close()
    folder = ctx.a.cfg.paths.root / "plugins" / "d09"
    write_plugin(folder / "chatty", "aud2-chatty", PLUGIN_CHATTY)
    host = plugin_host(ctx.a, "d09")
    result = await host.enable("aud2-chatty", timeout=20)
    assert result.get("enabled") is True, result
    cap = plugins.STDERR_KEEP_LINES
    assert await until(lambda: len(host._stderr.get("aud2-chatty") or []) >= cap, timeout=12), \
        f"stderr 日志没读到上限行数：{len(host._stderr.get('aud2-chatty') or [])} / {cap}"
    kept = host._stderr.get("aud2-chatty") or []
    assert len(kept) <= cap, f"stderr 日志无上限：{len(kept)} 行"
    assert max(len(line) for line in kept) <= plugins.STDERR_LINE_CHARS, \
        f"单行未截断：{max(len(line) for line in kept)} 字符"
    assert not any(line.startswith("line-000") for line in kept), "最早的行没被丢弃（缓冲区其实无上限）"
    assert (await host.disable("aud2-chatty"))["state"] == "stopped"
    return (f"能力声明 text=True / attachments=False / stream=False；带 attachments / stream / content_type 的"
            f"用户消息逐条明确拒绝（{'、'.join(refused)}，stage=protocol），且没有 accepted / reply、没有落库"
            f"（不是静默忽略）；刷 300 行 stderr 的插件：宿主只留 {len(kept)} 行（上限 {cap}）、"
            f"单行最长 {max(len(line) for line in kept)} 字符（上限 {plugins.STDERR_LINE_CHARS}）、最早的行已丢弃")


CHECKS: list[tuple[str, str, str, Callable[[Ctx], Awaitable[str]]]] = [
    ("K01", "信封：非 1.x 版本 / 未知 type / 方向越权 / 必填与类型校验", k1),
    ("K02", "认证：无 auth、错凭据、引导凭据一次性", k2),
    ("K03", "身份：持久凭据重连同一通道实例，握手结果持久", k3),
    ("K04", "协商：限额取交集、能力变化重新握手", k4),
    ("K05", "路由：不同通道同 thread/id 不串线", k5),
    ("K06", "权限：自报 builtin / 伪造管理令牌 / 普通网页都进不了管理面", k6),
    ("K07", "绑定：换代后旧令牌消息被拒且不落库", k7),
    ("K08", "幂等：同键同文返回既有结果、同键异文报 conflict", k8),
    ("K09", "回滚：飞行中输入作废，重发返回取消不复活", k9),
    ("K10", "顺序：同会话多 thread 串行、回复只回来源 thread", k10),
    ("K11", "接受：持久化之后才确认接收", k11),
    ("K12", "重连补投：同一固化回复、不重跑模型", k12),
    ("K13", "重试：outbound retry 只重发固化结果、不重复记忆", k13),
    ("K14", "回执：按原标识更新、unknown 不称成功、迟到不倒退", k14),
    ("K15", "重绑：换代令牌管不到旧记录、旧回复不改投新会话", k15),
    ("K16", "分段：超长回复完整有序分批不丢尾", k16),
    ("K17", "分段：能力变小只报不兼容，不重排 / 裁剪", k17),
    ("K18", "能力：status 只发给声明支持者", k18),
    ("K19", "合并回复：睡眠期两条入站一批，批内任一条查同批结果", k19),
    ("K20", "合并回复：跨 thread 不合并", k20),
    ("K21", "主动 / 开场：管理面入口（runtime.proactive / runtime.first_contact）", k21),
    ("K22", "主动消息：reply_to=null、自有标识、不伪造入站", k22),
    ("K23", "主动消息：超出协商长度时未拆分", k23),
    ("K24", "日志：凭据与消息正文不进日志", k24),
    ("K25", "导出：凭据不进入导出包、分类随导出保留", k25),
    ("K26", "卫生：握手超时 / 错误上限 / 超大帧后核心仍可用", k26),
    ("K27", "卫生：不读数据与硬崩溃的通道不拖死核心", k27),
    ("K28", "错误模型：五字段齐备且脱敏", k28),
    ("K29", "生成失败：保留失败状态，retry 恢复同一轮次", k29),
    ("K30", "协商失败状态：普通消息被拒、投递不受阻、恢复后可用", k30),
    ("K31", "内建聊天未连接时管理面照常可用", k31),
    ("K32", "UMP 不承载实例 / 时间线 / 角色内部标识", k32),
    ("K33", "独立开场：reply_to=null、一次性、不伪造入站", k33),
    ("K34", "归档说明：system_notice 分类与角色上下文", k34),
    ("K35", "永久错误：不降级、不放行、不重连风暴", k35),
    ("D01", "插件宿主：目录制 manifest 扫描 / 登记（只读清单、不跑代码、启用前检查）", d01),
    ("D02", "插件承载：子进程 + stdio 按行 NDJSON + 不经 shell + 停用后进程退出", d02),
    ("D03", "插件生命周期：握手才 running / 崩溃标 failed / 手工更新 / 卸载保留历史", d03),
    ("D05", "环境最小化：子进程不带核心凭据，只给白名单与自己的凭据", d05),
    ("D06", "安卓内建 / 进程内传输：同一段握手、认证与校验", d06),
    ("D07", "管理面通知：登记幂等、定位解析、失效不改投", d07),
    ("D09", "能力边界：附件 / 流式 / content_type 明确拒绝 + 受限日志容量上限", d09),
]

#: 按 SPEC 自述的分期条款（§七 / §九）暂缓的条目——现已全部落地为上面的行为断言，列表为空。
#: 真正的残余（第三方交付文档与参考实现）由对拍脚本盯着，不再计 DEFERRED：
#:     D04 协议字段表 / 生命周期 / 收发 / 错误重试 / 兼容说明 → docs/CHANNEL_PROTOCOL_APPENDIX.md
#:         参考实现 → examples/channel_plugin_reference.py（tests/test_plugins.py::test_reference_plugin_handshakes 真拉起来握手）
#:         文档与实现的对拍 → scripts/_audit2_proto_doc.py（5 组断言，不一致即 FAIL）
#:     D08（实现级字段表 / 字符计数方式 / 错误码表 / 帧上限）同上，已由 docs/CHANNEL_PROTOCOL_APPENDIX.md
#:         + scripts/_audit2_proto_doc.py 覆盖
DEFERRED: list[tuple[str, str, str]] = []


async def main() -> int:
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    roots = [Path(tempfile.mkdtemp(prefix=f"audit2-chan-{tag}-")) for tag in ("a", "b", "c")]
    async with core(roots[0], replies=[SHORT], logs=True) as ha:
        async with core(roots[1], replies=[SHORT]) as hb:
            async with core(roots[2], replies=[SHORT], state="compatibility_blocked") as hc:
                ctx = Ctx(
                    a=ha, b=hb, c=hc,
                    mgmt_a=await open_mgmt(ha), mgmt_b=await open_mgmt(hb), mgmt_c=await open_mgmt(hc),
                )
                print(f"# 通道插件层探针：A={ha.endpoint} B={hb.endpoint} C={hc.endpoint}(compatibility_blocked)"
                      f"（临时根 {', '.join(str(r) for r in roots)}）", flush=True)
                for code, summary, fn in CHECKS:
                    if only and code not in only and "ALL" not in only:
                        continue
                    started = time.monotonic()
                    try:
                        detail = await fn(ctx)
                    except AssertionError as exc:
                        emit("FAIL", code, summary,
                             f"{exc}；最小复现：{sys.executable} {PROBE} --only {code}（耗时 {time.monotonic() - started:.1f}s）")
                    except Exception as exc:  # noqa: BLE001 —— 逐条隔离
                        emit("FAIL", code, summary,
                             f"{type(exc).__name__}: {exc}；最小复现：{sys.executable} {PROBE} --only {code}"
                             f"（耗时 {time.monotonic() - started:.1f}s）")
                    else:
                        emit("PASS", code, summary, f"{detail}（耗时 {time.monotonic() - started:.1f}s）")
                if not only:
                    for code, summary, why in DEFERRED:
                        emit("DEFERRED", code, summary, why)
                await ctx.mgmt_a.close()
                await ctx.mgmt_b.close()
                await ctx.mgmt_c.close()
    passed = sum(1 for row in RESULTS if row[0] == "PASS")
    failed = sum(1 for row in RESULTS if row[0] == "FAIL")
    deferred = sum(1 for row in RESULTS if row[0] == "DEFERRED")
    print(f"TOTAL {passed + failed + deferred} PASS {passed} FAIL {failed} DEFERRED {deferred}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
