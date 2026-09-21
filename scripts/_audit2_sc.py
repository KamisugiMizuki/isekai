"""SESSION_CORE_SPEC 行为级审计探针（第二轮，独立于 scripts/_audit_session_core.py）。

真 WebSocket + 真 SQLite + 真 SessionService / RuntimeService / CoreServer，
只把 LLM 换成脚本替身。不启动核心进程，不碰 data/isekai.db、config/config.yaml、
packages/、logs/：全部存储落在 tempfile 临时目录。

用法（仓库根）：
  .venv/Scripts/python.exe scripts/_audit2_sc.py                  # 全部
  .venv/Scripts/python.exe scripts/_audit2_sc.py --only C16 C29   # 只跑指定条目
  .venv/Scripts/python.exe scripts/_audit2_sc.py --json out.json  # 另存 JSON
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from isekai_core import ump  # noqa: E402
from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.client import MgmtClient, UmpClient  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM, LLMError  # noqa: E402
from isekai_core.runtime import events as events_mod  # noqa: E402
from isekai_core.ump import Envelope  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402

#: 世界时刻：01:00（示例卡生活线 [0,25200) 是 sleep 块）
SLEEP_MOMENT = DAY * 1500 + 3600
#: 世界时刻：12:00（duty 块，清醒）
AWAKE_MOMENT = DAY * 1500 + 43200
#: 次日 12:00
NEXT_DAY_MOMENT = DAY * 1501 + 43200

REPRO = ".venv/Scripts/python.exe scripts/_audit2_sc.py --only "


# ------------------------------------------------------------------ LLM 替身


class ScriptedLLM(FakeLLM):
    """FakeLLM + 每次调用前的钩子：让「生成中世界发生变化」可确定地复现。"""

    def __init__(self, replies: list[str] | None = None, *, on_call: Callable[[int], Any] | None = None) -> None:
        super().__init__(replies or ["收到。"])
        self.on_call = on_call

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        index = len(self.calls)
        if self.on_call is not None:
            result = self.on_call(index)
            if asyncio.iscoroutine(result):
                await result
        return await super().chat(messages, **kwargs)


# ------------------------------------------------------------------ 夹具


class Chan:
    """UmpClient 的缓冲包装：可以按条件等信封，不满足的留着待查。"""

    def __init__(self, client: UmpClient) -> None:
        self.client = client
        self.buf: list[Envelope] = []
        self.threads: dict[str, dict[str, Any]] = {}

    async def wait(self, pred: Callable[[Envelope], bool], *, timeout: float = 10.0) -> Envelope:
        for index, env in enumerate(self.buf):
            if pred(env):
                return self.buf.pop(index)
        end = time.monotonic() + timeout
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("等待信封超时")
            env = await self.client.expect(lambda _env: True, timeout=remaining)
            if pred(env):
                return env
            self.buf.append(env)

    async def accepted(self, *, timeout: float = 8.0) -> Envelope:
        return await self.wait(lambda env: env.type == "accepted", timeout=timeout)

    async def reply(self, *, timeout: float = 12.0) -> Envelope:
        return await self.wait(lambda env: env.type == "reply", timeout=timeout)

    async def error(self, *, timeout: float = 8.0) -> Envelope:
        return await self.wait(lambda env: env.type == "error", timeout=timeout)

    async def drain(self, *, seconds: float = 0.4) -> None:
        """把这段时间里到达的信封收进缓冲（用于断言「什么都没来」）。"""
        end = time.monotonic() + seconds
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            try:
                self.buf.append(await self.client.expect(lambda _env: True, timeout=remaining))
            except TimeoutError:
                return

    def replies(self) -> list[Envelope]:
        return [env for env in self.buf if env.type == "reply"]

    def token(self, thread_id: str) -> str:
        return str((self.threads.get(thread_id) or {}).get("binding_token") or "")


class Core:
    def __init__(self, tmp: Path, fake: Any, runtime: Any, endpoint: str, mgmt: MgmtClient) -> None:
        self.tmp = tmp
        self.fake = fake
        self.runtime = runtime
        self.endpoint = endpoint
        self.mgmt = mgmt
        self.chans: list[Chan] = []

    @property
    def store(self) -> Any:
        return self.runtime.store

    @property
    def world(self) -> Any:
        return self.runtime.world

    async def send(self, chan: Chan, thread_id: str, text: str) -> str:
        env_id = await chan.client.send_user_message(
            thread_id=thread_id, binding_token=chan.token(thread_id), text=text
        )
        await chan.accepted()
        return env_id

    async def raw(self, chan: Chan, thread_id: str, text: str, env_id: str, *, token: str | None = None) -> None:
        await chan.client.send(
            ump.make("user_message", {"text": text}, thread_id=thread_id,
                     binding_token=token if token is not None else chan.token(thread_id), id=env_id)
        )

    async def retry(self, chan: Chan, thread_id: str, ref: str, *, kind: str | None = None) -> None:
        await chan.client.request_retry(thread_id=thread_id, binding_token=chan.token(thread_id), ref=ref, kind=kind)

    async def close(self) -> None:
        for chan in self.chans:
            try:
                await chan.client.close()
            except Exception:  # noqa: BLE001
                pass
        await self.runtime.service.shutdown()
        await self.runtime.server.close()
        self.runtime.store.close()


async def start_core(
    tmp: Path,
    *,
    replies: list[str] | None = None,
    on_call: Callable[[int], Any] | None = None,
    sleep: tuple[float, float] | None = None,
    merge_max: int | None = None,
    autocommit: bool | None = None,
) -> Core:
    cfg = load_config(tmp)
    run = cfg.runtime
    if sleep is not None:
        run = dataclasses.replace(run, sleep_wait_min_s=sleep[0], sleep_wait_max_s=sleep[1])
    if merge_max is not None:
        run = dataclasses.replace(run, merge_batch_max=merge_max)
    if autocommit is not None:
        run = dataclasses.replace(run, autocommit_enabled=autocommit)
    cfg.runtime = run
    fake = ScriptedLLM(replies, on_call=on_call)
    runtime = await build_runtime(cfg, llm=fake)
    endpoint = await runtime.server.start()
    mgmt = MgmtClient(endpoint, runtime.server.mgmt_token)
    await mgmt.connect()
    return Core(tmp, fake, runtime, endpoint, mgmt)


def make_instance(
    core: Core, *, moment: int = AWAKE_MOMENT, names: tuple[str, ...] = ("堤禾",)
) -> tuple[dict[str, Any], str, list[str], dict[str, Any]]:
    package = example_package("灰潮纪", moment=moment)
    cards = [example_card(package, name=name) for name in names]
    info = create_instance(core.store, package, cards)
    timeline_id = str(core.store.timeline_list(info["id"])[0]["id"])
    core.world.ensure_instance(info["id"], now_real=time.time())
    return info, timeline_id, [str(card["meta"]["card_id"]) for card in cards], package


async def bind_channel_async(
    core: Core,
    *,
    name: str,
    thread_id: str,
    session_id: str,
    capabilities: dict[str, Any] | None = None,
    credential: str | None = None,
) -> tuple[Chan, dict[str, Any], str]:
    """管理面登记通道 + 绑定 thread，再以真 WS 握手连上（rotate=False：重启后凭据不变）。"""
    issued = await core.mgmt.call(
        "channel.ensure", name=name, display_name=name, capabilities=capabilities or {}, rotate=False
    )
    cred = issued.get("credential") or credential
    if not cred:
        raise RuntimeError(f"通道 {name} 已有凭据且未轮换，需要调用方提供 credential")
    thread = (await core.mgmt.call("thread.bind", channel=name, thread_id=thread_id, session_id=session_id))["thread"]
    client = UmpClient(endpoint=core.endpoint, channel_id=name, name=name, credential=cred)
    ack = await client.connect()
    chan = Chan(client)
    chan.threads = {str(item["id"]): item for item in ack.get("threads") or []}
    core.chans.append(chan)
    return chan, thread, str(cred)


async def connect_chan(core: Core, *, name: str, credential: str) -> Chan:
    client = UmpClient(endpoint=core.endpoint, channel_id=name, name=name, credential=credential)
    ack = await client.connect()
    chan = Chan(client)
    chan.threads = {str(item["id"]): item for item in ack.get("threads") or []}
    core.chans.append(chan)
    return chan


def activate(core: Core, instance_id: str, timeline_id: str) -> None:
    """激活动作本身是同步运行层调用（§2.2 世界运行操作）。"""
    core.world.activate(instance_id, timeline_id, now_real=time.time())


def outbound_rows(store: Any, session_id: str) -> list[dict[str, Any]]:
    return [row for row in store.history_page(session_id, limit=500)["messages"] if row["role"] != "user"]


def inbound_row(core: Core, thread: dict[str, Any], env_id: str) -> dict[str, Any]:
    row = core.store.inbound_find(str(thread["channel_id"]), str(thread["thread_id"]), env_id)
    assert row is not None, f"入站行不存在：{env_id}"
    return row


def kill_character(core: Core, instance_id: str, timeline_id: str, character_id: str) -> dict[str, Any]:
    """用运行层自己的身故事件构造器落一条寿终事件（等价于寿终事件已固化的同一提交）。"""
    instance = core.store.instance_get(instance_id) or {}
    clock = core.world.clock_row(timeline_id)
    world = int(clock["processed_world"]) + 100
    card = core.world.card_of(instance, character_id, timeline_id=timeline_id, world_seconds=world)
    event = events_mod.death_event(
        card, instance_id=instance_id, timeline_id=timeline_id, world_seconds=world,
        calendar=core.world.calendar(instance), seed=core.world.seed_of(instance),
    )
    ok = core.store.apply_runtime_batch(
        timeline_id=timeline_id, generation=int(clock["generation"]), processed_world=world,
        catching_up=False, limited=False, events=[event],
    )
    assert ok, "身故事件未落库"
    return event


def advance_world(core: Core, instance_id: str, timeline_id: str, *, world_seconds: int, rate: int = 1000) -> int:
    """按倍率真实推进世界（走运行层 advance 路径）。"""
    now = time.time()
    core.world.set_rate(instance_id, timeline_id, rate=rate, now_real=now)
    core.world.advance(instance_id, timeline_id, now_real=now + world_seconds / float(rate))
    return int(core.world.clock_row(timeline_id)["processed_world"])


def add_material(core: Core, instance_id: str, timeline_id: str, character_id: str, *, key: str, text: str,
                 source: str = "src-1") -> int:
    world = int(core.world.clock_row(timeline_id)["processed_world"])
    core.store.knowledge_put({
        "instance_id": instance_id, "timeline_id": timeline_id, "character_id": character_id,
        "id": f"kn-{character_id}-{key}", "world_seconds": world, "kind": "claim",
        "target": f"cl-{key}", "source": source, "stance": "recorded", "text": text,
    })
    return world


def prompts_with(fake: Any, needle: str) -> list[list[dict[str, Any]]]:
    return [call for call in fake.calls if any(needle in str(msg.get("content") or "") for msg in call)]


def prompt_text(call: list[dict[str, Any]]) -> str:
    return "\n".join(str(msg.get("content") or "") for msg in call)


def R(status: str, clause: str, expected: str, observed: str, evidence: str, code_ref: str) -> dict[str, Any]:
    return {
        "status": status, "clause": clause, "expected": expected,
        "observed": observed, "evidence": evidence, "code_ref": code_ref,
    }


CHECKS: list[tuple[str, Callable[[Path], Awaitable[dict[str, Any]]]]] = []


def check(cid: str):
    def wrap(fn: Callable[[Path], Awaitable[dict[str, Any]]]):
        CHECKS.append((cid, fn))
        return fn

    return wrap


# ================================================================== 会话与绑定


@check("C01")
async def c01(tmp: Path) -> dict[str, Any]:
    """§2.1 / 附录B1：同一三元组复用同一会话；新窗口（新 thread）看到同一份持久历史。"""
    core = await start_core(tmp, replies=["收到。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        s1 = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                   character_id=chars[0]))["session"]
        s2 = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                   character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="w1", session_id=s1["id"])
        await core.send(chan, "w1", "在吗")
        await chan.reply()
        # 第二个窗口：同会话、另一个 thread
        chan2, thread2, _ = await bind_channel_async(core, name="desktop2", thread_id="w2", session_id=s1["id"])
        page = await core.mgmt.call("history.page", session_id=s1["id"], limit=50)
        texts = [row["text"] for row in page["messages"] if row["role"] == "user"]
        same = s1["id"] == s2["id"]
        shared = texts == ["在吗"]
        observed = f"session.ensure 两次 → {s1['id']} / {s2['id']}（相同={same}）；w2 视图分页含用户原文={shared}"
        return R(
            "PASS" if (same and shared) else "FAIL",
            "§2.1 会话身份 / 附录B1",
            "同三元组两次 ensure 复用同一会话；同会话新窗口分页能看到同一份历史",
            observed,
            f"复现：{REPRO}C01 ；sessions={s1['id']},{s2['id']}，page.texts={texts}",
            "isekai_core/store.py:1150 session_ensure / store.py:1455 history_page",
        )
    finally:
        await core.close()


@check("C02")
async def c02(tmp: Path) -> dict[str, Any]:
    """§2.2 / 附录B1：换通道 = 新 thread 绑到已有会话，沿用历史；旧绑定仍指向原会话。"""
    core = await start_core(tmp, replies=["收到。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        await core.send(chan, "dm-1", "换通道前的一句")
        await chan.reply()
        # 换通道：另一通道实例 + 新 thread，绑到同一会话
        chan2, thread2, _ = await bind_channel_async(core, name="telegram", thread_id="tg-1", session_id=session["id"])
        page = await core.mgmt.call("history.page", session_id=session["id"], limit=50)
        texts = [row["text"] for row in page["messages"] if row["role"] == "user"]
        old_binding = core.store.thread_get(str(thread["channel_id"]), "dm-1")
        observed = (
            f"新通道 thread2.session_id={thread2['session_id']}（原会话={session['id']}）；"
            f"历史={texts}；旧 thread 仍指向会话={old_binding['session_id'] == session['id']}"
        )
        ok = thread2["session_id"] == session["id"] and texts == ["换通道前的一句"] and old_binding["session_id"] == session["id"]
        return R(
            "PASS" if ok else "FAIL", "§2.2 更换通道 / 附录B1",
            "新 thread 绑到已有会话，沿用其历史；旧绑定不被改动",
            observed, f"复现：{REPRO}C02 ；page.texts={texts}",
            "isekai_core/store.py:1181 thread_bind / session.py:88 accept",
        )
    finally:
        await core.close()


@check("C03")
async def c03(tmp: Path) -> dict[str, Any]:
    """§2.2：重绑已有 thread → 原绑定版本失效、指向目标会话；旧历史留在原会话，不迁移。"""
    core = await start_core(tmp, replies=["收到。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core, names=("堤禾", "潮生"))
        activate(core, info["id"], tl)
        sa = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                   character_id=chars[0]))["session"]
        sb = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                   character_id=chars[1]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=sa["id"])
        await core.send(chan, "dm-1", "重绑前的历史")
        await chan.reply()
        old_token = chan.token("dm-1")
        v1 = int(thread["binding_version"])
        # 管理面重绑到会话 B
        again = (await core.mgmt.call("thread.bind", channel="builtin", thread_id="dm-1", session_id=sb["id"]))["thread"]
        v2 = int(again["binding_version"])
        # 旧令牌发消息：必须拒绝
        await core.raw(chan, "dm-1", "旧令牌的回声", "e-old-token", token=old_token)
        err = await chan.error()
        page_a = await core.mgmt.call("history.page", session_id=sa["id"], limit=50)
        page_b = await core.mgmt.call("history.page", session_id=sb["id"], limit=50)
        a_texts = [row["text"] for row in page_a["messages"]]
        b_texts = [row["text"] for row in page_b["messages"]]
        ok = (
            v2 == v1 + 1 and err.payload.get("code") in ("binding_expired",)
            and "重绑前的历史" in a_texts and "重绑前的历史" not in b_texts and b_texts == []
        )
        observed = (
            f"binding_version {v1}→{v2}；旧令牌错误码={err.payload.get('code')}；"
            f"原会话历史={a_texts}；目标会话历史={b_texts}"
        )
        return R(
            "PASS" if ok else "FAIL", "§2.2 重绑已有 thread / §2.3 令牌",
            "版本递增、原令牌失效、旧历史留在原会话不迁移",
            observed, f"复现：{REPRO}C03 ；err={err.payload}",
            "isekai_core/store.py:1181 thread_bind / channel.py:305 _on_user_message",
        )
    finally:
        await core.close()


@check("C04")
async def c04(tmp: Path) -> dict[str, Any]:
    """§2.3：未绑定 thread 与失效令牌都返回明确错误，且不落库。"""
    core = await start_core(tmp, replies=["收到。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        before = core.store.counts().get("messages", 0)
        await core.raw(chan, "nope", "未绑定 thread 的输入", "e-nope", token="bt-dummy-token")
        err1 = await chan.error()
        await core.raw(chan, "dm-1", "坏令牌的输入", "e-bad-token", token="bt-not-a-real-token")
        err2 = await chan.error()
        after = core.store.counts().get("messages", 0)
        rows = [core.store.inbound_find(str(thread["channel_id"]), "nope", "e-nope"),
                core.store.inbound_find(str(thread["channel_id"]), "dm-1", "e-bad-token")]
        ok = err1.payload.get("code") == "unknown_thread" and err2.payload.get("code") == "binding_expired" \
            and before == after and all(row is None for row in rows)
        observed = (
            f"未绑定→{err1.payload.get('code')}；坏令牌→{err2.payload.get('code')}；"
            f"message 行数 {before}→{after}"
        )
        return R(
            "PASS" if ok else "FAIL", "§2.3 消息路由",
            "未绑定返回 unknown_thread、令牌不符返回 binding_expired，且都不落库",
            observed, f"复现：{REPRO}C04 ；errors={err1.payload},{err2.payload}",
            "isekai_core/channel.py:296-312 _on_user_message",
        )
    finally:
        await core.close()


@check("C05")
async def c05(tmp: Path) -> dict[str, Any]:
    """§2.3：普通回复只回来源 thread，不广播到同会话其他绑定。"""
    core = await start_core(tmp, replies=["收到。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan_a, _, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        chan_b, _, _ = await bind_channel_async(core, name="mirror", thread_id="dm-2", session_id=session["id"])
        await core.send(chan_a, "dm-1", "只该回给 A")
        rep_a = await chan_a.reply()
        await chan_b.drain(seconds=0.6)
        b_replies = chan_b.replies()
        ok = rep_a.thread_id == "dm-1" and not b_replies
        observed = f"A 收到 reply thread={rep_a.thread_id}；B 缓冲 reply 数={len(b_replies)}"
        return R(
            "PASS" if ok else "FAIL", "§2.3 回复只回来源 thread",
            "回复只发来源 thread；同会话其他视图不实时收到推送",
            observed, f"复现：{REPRO}C05 ；B.buf={[e.type for e in chan_b.buf]}",
            "isekai_core/session.py:363-379 _generate / session.py:574 _send_batches",
        )
    finally:
        await core.close()


@check("C06")
async def c06(tmp: Path) -> dict[str, Any]:
    """§2.2 / §2.3：冻结线拒绝入站（不静默丢弃、不自动激活），激活后恢复。"""
    core = await start_core(tmp, replies=["收到。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        await core.mgmt.call("runtime.freeze", instance_id=info["id"], timeline_id=tl)
        await core.raw(chan, "dm-1", "冻结线上的输入", "e-frozen")
        err = await chan.error()
        frozen_rows = outbound_rows(core.store, session["id"])
        state_after_freeze = core.store.timeline_get(tl)["state"]
        await core.mgmt.call("runtime.activate", instance_id=info["id"], timeline_id=tl)
        await core.send(chan, "dm-1", "激活后的输入")
        rep = await chan.reply()
        ok = err.payload.get("code") == "state_blocked" and state_after_freeze == "frozen" \
            and not frozen_rows and rep.type == "reply"
        observed = (
            f"冻结态入站错误码={err.payload.get('code')}；冻结(未暗中激活)={state_after_freeze}；"
            f"冻结期间出站行={len(frozen_rows)}；激活后收到 reply"
        )
        return R(
            "PASS" if ok else "FAIL", "§2.2 冻结线 / §2.3 明确错误",
            "冻结线入站被明确拒绝且不暗中激活；激活后正常回复",
            observed, f"复现：{REPRO}C06 ；err={err.payload}",
            "isekai_core/session.py:117-126 accept",
        )
    finally:
        await core.close()


# ================================================================== 生成链路


@check("C07")
async def c07(tmp: Path) -> dict[str, Any]:
    """§4.2.1 / 附录B2：同请求重试查既有结果，不再次生成；同键异文拒绝。"""
    core = await start_core(tmp, replies=["收到。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        env_id = await core.send(chan, "dm-1", "同一条请求")
        rep = await chan.reply()
        calls = len(core.fake.calls)
        # 同键同文重发
        await core.raw(chan, "dm-1", "同一条请求", env_id)
        acc = await chan.accepted()
        calls_after = len(core.fake.calls)
        # 同键异文
        await core.raw(chan, "dm-1", "改了正文的同一条请求", env_id)
        err = await chan.error()
        outs = outbound_rows(core.store, session["id"])
        ok = (
            acc.payload.get("state") == "done"
            and acc.payload.get("message_id") == rep.payload.get("message_id")
            and calls == calls_after and len(outs) == 1
            and err.payload.get("code") == "conflict"
        )
        observed = (
            f"重发回执 state={acc.payload.get('state')} message_id=={rep.payload.get('message_id')}；"
            f"LLM 调用 {calls}→{calls_after}；出站行={len(outs)}；异文错误码={err.payload.get('code')}"
        )
        return R(
            "PASS" if ok else "FAIL", "§4.2.1 幂等接受 / 附录B2",
            "同键同文返回既有处理结果、不重新生成；同键异文报冲突",
            observed, f"复现：{REPRO}C07 ；env={env_id}",
            "isekai_core/session.py:146-157 accept / store.py:1214 inbound_put",
        )
    finally:
        await core.close()


@check("C08")
async def c08(tmp: Path) -> dict[str, Any]:
    """§4.2.6：一次逻辑轮次的固化共同发布（状态 + 唯一出站 + 投递资格），发布前不发送。"""
    core = await start_core(tmp, replies=["一次轮次的产物。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        core.fake.delay_s = 0.4
        env_id = await core.send(chan, "dm-1", "发布前请勿发送")
        row_queued = inbound_row(core, thread, env_id)
        outs_mid = outbound_rows(core.store, session["id"])
        mid_replies = chan.replies()
        rep = await chan.reply(timeout=8.0)
        row_done = inbound_row(core, thread, env_id)
        outs = outbound_rows(core.store, session["id"])
        deliveries = core.store.delivery_rows(int(outs[0]["seq"])) if outs else []
        ok = (
            row_queued["state"] in ("queued", "processing") and not outs_mid and not mid_replies
            and len(outs) == 1
            and row_done["state"] == "done"
            and row_done["reply_message_id"] == outs[0]["message_id"] == rep.payload.get("message_id")
            and len(deliveries) == 1
        )
        observed = (
            f"生成中输入态={row_queued['state']}、出站行={len(outs_mid)}、已发 reply={len(mid_replies)}；"
            f"完成后入站 state={row_done['state']} reply_message_id==出站 message_id（{outs[0]['message_id']}）；"
            f"投递行={len(deliveries)}"
        )
        return R(
            "PASS" if ok else "FAIL", "§4.2.6 提交 / 附录B2",
            "入站状态、唯一出站产物、投递资格同一事务发布；发布前不发送",
            observed, f"复现：{REPRO}C08 ；env={env_id}",
            "isekai_core/session.py:362-379 _generate / store.py:1369 commit_turn",
        )
    finally:
        await core.close()


@check("C09")
async def c09(tmp: Path) -> dict[str, Any]:
    """§4.2.7 / 附录B2：投递失败不记为成功；重连补投只重发已有产物，不重新生成。"""
    core = await start_core(tmp, replies=["已固化的产物。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, cred = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        core.fake.delay_s = 0.9
        env_id = await core.send(chan, "dm-1", "断线时生成")
        await asyncio.sleep(0.2)
        await chan.client.close()  # 通道断线：投递出口返回 False
        await asyncio.sleep(1.4)
        outs = outbound_rows(core.store, session["id"])
        states = [row["state"] for row in core.store.delivery_rows(int(outs[0]["seq"]))] if outs else []
        calls = len(core.fake.calls)
        # 重连：hello 后核心按有界策略补投
        chan2 = await connect_chan(core, name="builtin", credential=cred)
        try:
            rep = await chan2.reply(timeout=8.0)
        except TimeoutError:
            rep = None
        calls_after = len(core.fake.calls)
        ok = (
            len(outs) == 1 and states and all(state == "pending" for state in states)
            and rep is not None and rep.payload.get("message_id") == outs[0]["message_id"]
            and calls == calls_after
        )
        observed = (
            f"断线期间投递状态={states}；重连后收到 reply message_id=={outs[0]['message_id'] if outs else None}"
            f"（实收 {rep.payload.get('message_id') if rep else None}）；LLM 调用 {calls}→{calls_after}"
        )
        return R(
            "PASS" if ok else "FAIL", "§4.2.7 投递 / 附录B2",
            "投递失败保留 pending 不假称成功；补投只重发同一 message_id，不再生成",
            observed, f"复现：{REPRO}C09 ；env={env_id}",
            "isekai_core/session.py:605-616 _send_batches / channel.py:220 resend_pending",
        )
    finally:
        await core.close()


# ================================================================== 睡眠等待与合并


@check("C10")
async def c10(tmp: Path) -> dict[str, Any]:
    """§4.5 触发与等待：截止点一次确定并持久化，后续输入不重置、不叠加。"""
    core = await start_core(tmp, replies=["嗯……", "嗯……"], sleep=(1.6, 1.6))
    try:
        info, tl, chars, _ = make_instance(core, moment=SLEEP_MOMENT)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        started = time.monotonic()
        e1 = await core.send(chan, "dm-1", "第一条（她在睡）")
        wait1 = float(inbound_row(core, thread, e1)["wait_until"] or 0)
        await asyncio.sleep(0.6)
        e2 = await core.send(chan, "dm-1", "第二条（等待期间到达）")
        wait1_again = float(inbound_row(core, thread, e1)["wait_until"] or 0)
        rep = await chan.reply(timeout=10.0)
        elapsed = time.monotonic() - started
        ok = wait1 > 0 and wait1 == wait1_again and elapsed < 1.6 + 0.8
        observed = (
            f"第一条 wait_until={wait1:.3f}（第二条到达后仍={wait1_again:.3f}）；"
            f"从首条接受到回复耗时 {elapsed:.2f}s（单拍 1.6s；若叠加应≥3.2s）；reply={rep.payload.get('message_id')}"
        )
        return R(
            "PASS" if ok else "FAIL", "§4.5 触发与等待",
            "截止点以首次接受时间为基准、一次确定并持久化；后续输入不重置不叠加",
            observed, f"复现：{REPRO}C10 ；env1={e1} env2={e2}",
            "isekai_core/store.py:1264 inbound_claim_deadline / session.py:283",
        )
    finally:
        await core.close()


@check("C11")
async def c11(tmp: Path) -> dict[str, Any]:
    """§4.5 合并：批内共享一份固化回复，任一输入都查到同批结果；reply_to 关联最后一条。"""
    core = await start_core(tmp, replies=["（朦胧）……"], sleep=(1.2, 1.2))
    try:
        info, tl, chars, _ = make_instance(core, moment=SLEEP_MOMENT)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        e1 = await core.send(chan, "dm-1", "第一句")
        await asyncio.sleep(0.2)
        e2 = await core.send(chan, "dm-1", "第二句")
        rep = await chan.reply(timeout=10.0)
        r1, r2 = inbound_row(core, thread, e1), inbound_row(core, thread, e2)
        calls = len(core.fake.calls)
        prompt_calls = prompts_with(core.fake, "第一句")
        body = prompt_text(prompt_calls[-1]) if prompt_calls else ""
        # 批内任一输入查询同批状态
        await core.raw(chan, "dm-1", "第一句", e1)
        acc1 = await chan.accepted()
        await core.raw(chan, "dm-1", "第二句", e2)
        acc2 = await chan.accepted()
        ok = (
            r1["reply_message_id"] == r2["reply_message_id"] == rep.payload.get("message_id")
            and rep.payload.get("reply_to") == e2
            and rep.payload.get("covers") == [e1, e2]
            and calls == 1
            and acc1.payload.get("message_id") == acc2.payload.get("message_id") == rep.payload.get("message_id")
            and "第一句" in body and "第二句" in body
        )
        observed = (
            f"两条入站 reply_message_id={r1['reply_message_id']}；reply_to={rep.payload.get('reply_to')}；"
            f"covers={rep.payload.get('covers')}；LLM 调用={calls}；"
            f"查询两条入站回执 message_id={acc1.payload.get('message_id')}/{acc2.payload.get('message_id')}；"
            f"上下文同时含两条原文={('第一句' in body and '第二句' in body)}"
        )
        return R(
            "PASS" if ok else "FAIL", "§4.5 合并批",
            "批内共享一份固化回复、reply_to=最后一条、covers 保留全部输入、任一输入返回同一 message_id",
            observed, f"复现：{REPRO}C11 ；env1={e1} env2={e2}",
            "isekai_core/session.py:363-377 _generate / store.py:1369 commit_turn",
        )
    finally:
        await core.close()


@check("C12")
async def c12(tmp: Path) -> dict[str, Any]:
    """§4.5 / 附录B11：不跨通道或 thread 合并，各自成批投递。"""
    core = await start_core(tmp, replies=["回复一。", "回复二。"], sleep=(1.2, 1.2))
    try:
        info, tl, chars, _ = make_instance(core, moment=SLEEP_MOMENT)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan_a, thread_a, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        chan_b, thread_b, _ = await bind_channel_async(core, name="mirror", thread_id="dm-2", session_id=session["id"])
        e1 = await core.send(chan_a, "dm-1", "A 的话")
        await asyncio.sleep(0.3)
        e2 = await core.send(chan_b, "dm-2", "B 的话")
        rep_a = await chan_a.reply(timeout=10.0)
        rep_b = await chan_b.reply(timeout=10.0)
        ok = (
            rep_a.payload.get("covers") == [e1] and rep_b.payload.get("covers") == [e2]
            and rep_a.payload.get("message_id") != rep_b.payload.get("message_id")
            and rep_a.thread_id == "dm-1" and rep_b.thread_id == "dm-2"
        )
        observed = (
            f"A covers={rep_a.payload.get('covers')}（thread={rep_a.thread_id}）；"
            f"B covers={rep_b.payload.get('covers')}（thread={rep_b.thread_id}）"
        )
        return R(
            "PASS" if ok else "FAIL", "§4.5 合并范围 / 附录B11",
            "同一会话不同 thread 的输入不合并，各成一批且不互相投递",
            observed, f"复现：{REPRO}C12 ；envA={e1} envB={e2}",
            "isekai_core/session.py:382-399 _collect_batch",
        )
    finally:
        await core.close()


@check("C13")
async def c13(tmp: Path) -> dict[str, Any]:
    """§4.5 到期按真实状态表达：仍睡眠 → 朦胧；等待中自然醒来 → 按清醒状态。"""
    core = await start_core(tmp, replies=["嗯……", "醒了。"], sleep=(1.6, 1.6))
    try:
        info, tl, chars, _ = make_instance(core, moment=SLEEP_MOMENT)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        await core.send(chan, "dm-1", "睡着时问的")
        rep1 = await chan.reply(timeout=10.0)
        sleep_calls = prompts_with(core.fake, "睡着时问的")
        sleep_body = prompt_text(sleep_calls[-1]) if sleep_calls else ""
        # 第二条：等待期间把世界推过睡眠块末尾（08:00）
        await core.send(chan, "dm-1", "等一半就醒了的")
        await asyncio.sleep(0.4)
        moved = advance_world(core, info["id"], tl, world_seconds=30000)
        await chan.reply(timeout=10.0)
        wake_calls = prompts_with(core.fake, "等一半就醒了的")
        wake_body = prompt_text(wake_calls[-1]) if wake_calls else ""
        plan = core.store.plan_latest(info["id"], tl, chars[0])
        from isekai_core.runtime import life as life_mod
        window = life_mod.current_window(plan, moved)
        activity = str((window or {}).get("activity") or "")
        ok = "还在睡" in sleep_body and "已经醒了" in wake_body and activity == "duty"
        observed = (
            f"第一条上下文含「还在睡」={('还在睡' in sleep_body)}；"
            f"第二条（世界推到 {moved}，当前活动={activity!r}）上下文含「已经醒了」={('已经醒了' in wake_body)}"
        )
        return R(
            "PASS" if ok else "FAIL", "§4.5 到期按真实状态表达",
            "仍睡眠时给朦胧口吻约束，已自然醒来时按清醒状态、不否认世界推进",
            observed, f"复现：{REPRO}C13 ；world_after={moved}",
            "isekai_core/session.py:442-447 _sleep_hint",
        )
    finally:
        await core.close()


@check("C14")
async def c14(tmp: Path) -> dict[str, Any]:
    """§4.5 恢复与作废：等待中冻结 → 迟到任务不写入、不投递，批内各输入结算。"""
    core = await start_core(tmp, replies=["不该出现。"], sleep=(2.0, 2.0))
    try:
        info, tl, chars, _ = make_instance(core, moment=SLEEP_MOMENT)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        e1 = await core.send(chan, "dm-1", "等待中被冻结")
        await asyncio.sleep(0.3)
        await core.mgmt.call("runtime.freeze", instance_id=info["id"], timeline_id=tl)
        await asyncio.sleep(2.4)
        await chan.drain(seconds=0.3)
        row = inbound_row(core, thread, e1)
        outs = outbound_rows(core.store, session["id"])
        ok = row["state"] == "cancelled" and not outs and not chan.replies()
        observed = f"等待中冻结后：入站 state={row['state']} error_code={row['error_code']}；出站行={len(outs)}；reply 信封={len(chan.replies())}"
        return R(
            "PASS" if ok else "FAIL", "§4.5 恢复与作废 / §4.3",
            "等待任务在冻结后失效：不写回、不投递",
            observed, f"复现：{REPRO}C14 ；env={e1}",
            "isekai_core/session.py:291-298 _wait_and_collect / session.py:401-415 _stale_code",
        )
    finally:
        await core.close()


@check("C15")
async def c15(tmp: Path) -> dict[str, Any]:
    """§4.5 末条：非睡眠时段不额外设置等待。"""
    core = await start_core(tmp, replies=["在的。"], sleep=(2.0, 2.0))
    try:
        info, tl, chars, _ = make_instance(core, moment=AWAKE_MOMENT)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        started = time.monotonic()
        e1 = await core.send(chan, "dm-1", "清醒时问的")
        await chan.reply(timeout=6.0)
        elapsed = time.monotonic() - started
        wait_until = float(inbound_row(core, thread, e1)["wait_until"] or 0)
        ok = elapsed < 0.8 and wait_until == 0
        observed = f"清醒（12:00）时回复耗时 {elapsed:.2f}s；wait_until={wait_until}（等待区间 2.0s）"
        return R(
            "PASS" if ok else "FAIL", "§4.5 非睡眠时段",
            "非睡眠时段不额外等待，同会话仍守处理顺序",
            observed, f"复现：{REPRO}C15 ；env={e1}",
            "isekai_core/session.py:280-282 _wait_and_collect",
        )
    finally:
        await core.close()


@check("C16")
async def c16(tmp: Path) -> dict[str, Any]:
    """§4.3 / 附录B3：生成期间冻结 → 迟到结果不得写入或投递。"""
    holder: dict[str, Any] = {}

    async def freeze_mid(_index: int) -> None:
        core = holder.get("core")
        if core is not None:
            await core.mgmt.call("runtime.freeze", instance_id=holder["info"]["id"], timeline_id=holder["tl"])

    core = await start_core(tmp, replies=["这是一条迟到的回复。"], sleep=(0.0, 0.0), on_call=freeze_mid)
    holder["core"] = core
    try:
        info, tl, chars, _ = make_instance(core)
        holder["info"], holder["tl"] = info, tl
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        core.fake.delay_s = 0.0
        e1 = await core.send(chan, "dm-1", "生成期间世界被冻结")
        await asyncio.sleep(1.0)
        await chan.drain(seconds=0.3)
        row = inbound_row(core, thread, e1)
        outs = outbound_rows(core.store, session["id"])
        ok = row["state"] == "cancelled" and not outs
        observed = (
            f"入站 state={row['state']} error_code={row['error_code']}；"
            f"出站行={len(outs)} 正文={[row['text'] for row in outs]}；reply 信封={len(chan.replies())}"
        )
        return R(
            "PASS" if ok else "FAIL", "§4.3 迟到结果 / 附录B3",
            "冻结后未完成轮次的迟到结果不写入、不投向旧绑定",
            observed, f"复现：{REPRO}C16 ；env={e1}",
            "isekai_core/session.py:344-360 _generate（只核对 void 与绑定版本，未核对冻结/归档/世代）",
        )
    finally:
        await core.close()


@check("C17")
async def c17(tmp: Path) -> dict[str, Any]:
    """§4.3 崩溃恢复：已完成轮次恢复原产物；未完成者标记中断、显式重试不重复生成。"""
    core = await start_core(tmp, replies=["重试后的回复。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, cred = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        core.fake.delay_s = 1.5
        env_id = await core.send(chan, "dm-1", "崩溃前已接受")
        await asyncio.sleep(0.2)
        mid_state = inbound_row(core, thread, env_id)["state"]
        await core.close()  # 硬停：任务被取消，行停在 queued/processing
        core2 = await start_core(tmp, replies=["重试后的回复。"], sleep=(0.0, 0.0))
        core = None
        try:
            after_restart = core2.store.inbound_find(str(thread["channel_id"]), "dm-1", env_id)
            chan2 = await connect_chan(core2, name="builtin", credential=cred)
            await core2.retry(chan2, "dm-1", env_id)
            await chan2.accepted()
            rep = await chan2.reply(timeout=8.0)
            outs = outbound_rows(core2.store, session["id"])
            calls = len(core2.fake.calls)
            ok = (
                after_restart["state"] == "failed" and after_restart["error_code"] == "interrupted"
                and len(outs) == 1 and calls == 1
                and rep.payload.get("message_id") == outs[0]["message_id"]
            )
            observed = (
                f"中断标记 state={after_restart['state']}/error_code={after_restart['error_code']}"
                f"（崩溃前={mid_state}）；重试后出站行={len(outs)}、LLM 调用={calls}"
            )
            return R(
                "PASS" if ok else "FAIL", "§4.3 崩溃恢复",
                "未完成轮次标记中断并经显式重试恢复；不静默漏单、不执行两次",
                observed, f"复现：{REPRO}C17 ；env={env_id}",
                "isekai_core/store.py:1292 interrupt_open_turns / session.py:243-246 retry",
            )
        finally:
            await core2.close()
    finally:
        if core is not None:
            await core.close()


@check("C18")
async def c18(tmp: Path) -> dict[str, Any]:
    """§4.3 末条：已在后续成功轮次之后，不得把旧轮次重放成新回复。"""
    core = await start_core(tmp, replies=["第二条的回复。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        core.fake.fail_with = LLMError("llm_unavailable", "429", retryable=True)
        e1 = await core.send(chan, "dm-1", "第一条（生成失败）")
        await chan.error(timeout=6.0)
        failed = inbound_row(core, thread, e1)
        core.fake.fail_with = None
        e2 = await core.send(chan, "dm-1", "第二条（成功）")
        rep2 = await chan.reply(timeout=8.0)
        before = [row["text"] for row in outbound_rows(core.store, session["id"])]
        await core2_retry(core, chan, "dm-1", e1)
        await asyncio.sleep(0.8)
        after = [row["text"] for row in outbound_rows(core.store, session["id"])]
        ok = len(after) == 1  # 只应有第二条的回复
        observed = (
            f"第一条失败态={failed['state']}/{failed['error_code']}；第二条回复={rep2.payload.get('message_id')}；"
            f"出站正文 重试前={before} 重试后={after}"
        )
        return R(
            "PASS" if ok else "FAIL", "§4.3 重试不得插入旧轮次",
            "会话已有后续成功轮次时，旧失败轮次的显式重试应拒绝并提示用户重发",
            observed, f"复现：{REPRO}C18 ；env1={e1} env2={e2}",
            "isekai_core/session.py:243-246 retry（未检查后续成功轮次）",
        )
    finally:
        await core.close()


async def core2_retry(core: Core, chan: Chan, thread_id: str, ref: str) -> None:
    await core.retry(chan, thread_id, ref)
    try:
        await chan.accepted(timeout=4.0)
    except TimeoutError:
        pass


# ================================================================== 角色行动 / 上下文


@check("C19")
async def c19(tmp: Path) -> dict[str, Any]:
    """§4.4 / 附录B5：普通文本不产生世界效果，也不创建管理性时间线。"""
    core = await start_core(tmp, replies=["我这就去堤上，把通行牌的事办了。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])

        def snap() -> dict[str, int]:
            return {
                "events": len(core.store.event_window(info["id"], tl, until=10 ** 12, limit=500)),
                "timelines": len(core.store.timeline_list(info["id"])),
                "intents": len(core.store.intent_list(info["id"], tl)),
                "units": len(core.store.unit_list(info["id"], tl, chars[0])),
                "claims": len(core.store.claim_list(info["id"], tl)),
            }

        before = snap()
        await core.send(chan, "dm-1", "你把通行牌的事办一下吧，就说我托你办的")
        await chan.reply(timeout=8.0)
        after = snap()
        ok = before == after
        return R(
            "PASS" if ok else "FAIL", "§4.4 角色行动 / 附录B5",
            "对话生成不直接产生世界效果、不创建管理性时间线",
            f"轮次前后世界状态 {before} → {after}",
            f"复现：{REPRO}C19 ；reply='我这就去堤上，把通行牌的事办了。'",
            "isekai_core/session.py:449-481 _settle_memory（对话只产生记忆提取依据）",
        )
    finally:
        await core.close()


@check("C20")
async def c20(tmp: Path) -> dict[str, Any]:
    """§4.2.3 / 附录B4·B15：上下文同时带入活动、后果、环境投影与可知经历；不含实情与幕后设定。"""
    core = await start_core(tmp, replies=["嗯。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, package = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        world = int(core.world.clock_row(tl)["processed_world"])
        core.store.experience_add({
            "id": "ex-audit-1", "instance_id": info["id"], "timeline_id": tl, "character_id": chars[0],
            "world_seconds": world, "kind": "world", "summary": "她昨天在滩口把水位尺擦了一遍",
            "source_ref": "", "confidence": 0.8,
        })
        add_material(core, info["id"], tl, chars[0], key="ctx", text="北堤的通行牌这三天都停发了")
        await core.send(chan, "dm-1", "今天怎么样")
        await chan.reply(timeout=8.0)
        calls = prompts_with(core.fake, "今天怎么样")
        body = prompt_text(calls[-1]) if calls else ""
        secret = "崩塌前夜，堤长议会收到过一份未被采信的潮位告警。"
        creator = "她父亲的旧账本里有那份告警的一页抄件"
        checks = {
            "当前活动": "她此刻正在做的事" in body and "duty" in body,
            "环境投影": "她此刻能直接观察到的环境" in body and "潮位：2尺" in body,
            "角色可知经历": "她昨天在滩口把水位尺擦了一遍" in body,
            "已获知内容": "北堤的通行牌这三天都停发了" in body,
            "打算延续": "她自己惦记着的事" in body and "通行牌发放延误" in body,
            "不含实情": secret not in body,
            "不含幕后设定": creator not in body,
        }
        ok = all(checks.values())
        return R(
            "PASS" if ok else "FAIL", "§4.2.3 组装 / 附录B4·B15",
            "活动 / 环境 / 经历 / 已知 / 打算都进上下文；秘密实情与角色 creator 段不进上下文",
            "；".join(f"{key}={value}" for key, value in checks.items()),
            f"复现：{REPRO}C20 ；prompt 长度={len(body)}",
            "isekai_core/runtime/service.py:2965 system_prompt / runtime/cognition.py:171 render_prompt",
        )
    finally:
        await core.close()


# ================================================================== 世界源主动发言


@check("C21")
async def c21(tmp: Path) -> dict[str, Any]:
    """§5.1 / §5.4 / 附录B16：无合法素材不生成主动消息；只有他人素材也不算。"""
    core = await start_core(tmp, replies=["堤上风转了。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core, names=("堤禾", "潮生"))
        activate(core, info["id"], tl)
        knowledge_before = core.store.knowledge_window(info["id"], tl, chars[0], until=10 ** 12, limit=50)
        first = await tick(core, info["id"], tl, per_day=2)
        # 只给「另一个角色」的素材
        add_material(core, info["id"], tl, chars[1], key="other", text="潮生听说北堤的通行牌停发了")
        second = await tick(core, info["id"], tl, per_day=2)
        spoken_self = first.get("spoken")
        day = core.store.proactive_day_count(info["id"], tl, chars[0],
                                             world_day=int(core.world.calendar(core.store.instance_get(info["id"]))
                                                           .day_index(int(core.world.clock_row(tl)["processed_world"]))))
        other_day = core.store.proactive_day_count(
            info["id"], tl, chars[1],
            world_day=int(core.world.calendar(core.store.instance_get(info["id"])).day_index(
                int(core.world.clock_row(tl)["processed_world"]))))
        mine = second.get("skipped", {}).get(chars[0])
        others = [item for item in second.get("messages") or [] if item["character_id"] == chars[0]]
        # 生成类调用单独数：后验检查是同一轮的第二个调用，不是「又生成了一次」（NARRATIVE_LAYER §6.2）
        generated = len(prompts_with(core.fake, "告诉联络者"))
        ok = spoken_self == 0 and day == 0 and mine == "没有可用素材" and not others and other_day == 1             and generated == 1
        return R(
            "PASS" if ok else "FAIL", "§5.1 无素材不生成 / 附录B16",
            "无已获知素材时不生成；只有别人的素材时本角色仍不生成、不借用他人素材",
            f"自身素材为空={knowledge_before == []}；第一次 spoken={spoken_self}（{first.get('skipped')}）；"
            f"只给他人素材后本角色原因={mine}、本角色配额计数={day}、本角色消息={len(others)} 条"
            f"（另一角色计数={other_day}）；生成类调用={generated}",
            f"复现：{REPRO}C21 ",
            "isekai_core/runtime/proactive.py:19 candidates / service.py:1977 proactive_tick",
        )
    finally:
        await core.close()


@check("C22")
async def c22(tmp: Path) -> dict[str, Any]:
    """§5.2 配额与素材：每角色每线每世界日默认最多 2 条；同一素材不重复消费。"""
    core = await start_core(tmp, replies=["信报上有一条新消息。", "滩口的风向变了。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        # 三条**不同来源**的素材：同源的多条会被编进同一个叙事单元（见 tests/test_narrative.py），
        # 这里要验的是配额上限，所以让它们各自成单元。
        add_material(core, info["id"], tl, chars[0], key="m1", text="北堤的通行牌这三天都停发了", source="src-1")
        add_material(core, info["id"], tl, chars[0], key="m2", text="驿站新到一份灾年编年的补页", source="src-2")
        add_material(core, info["id"], tl, chars[0], key="m3", text="滩口的盐堆被潮水泡了", source="src-3")
        r1 = await tick(core, info["id"], tl, per_day=2)
        r2 = await tick(core, info["id"], tl, per_day=2)
        r3 = await tick(core, info["id"], tl, per_day=2)
        world_day = int(core.world.calendar(core.store.instance_get(info["id"])).day_index(
            int(core.world.clock_row(tl)["processed_world"])))
        day_count = core.store.proactive_day_count(info["id"], tl, chars[0], world_day=world_day)
        consumed = core.store.proactive_consumed(info["id"], tl, chars[0])
        generated = len(prompts_with(core.fake, "告诉联络者"))  # 两条消息 = 两次生成（后验检查另算）
        ok = (r1["spoken"], r2["spoken"], r3["spoken"]) == (1, 1, 0) and day_count == 2 \
            and r3["skipped"].get(chars[0]) == "今日额度用完" and generated == 2
        return R(
            "PASS" if ok else "FAIL", "§5.2 配额与素材消费",
            "每角色 / 每线 / 每世界日最多 2 条；第三条不生成；素材按固化条数计数、不重复消费",
            f"三次 tick spoken={r1['spoken']},{r2['spoken']},{r3['spoken']}（第三次原因={r3['skipped']}）；"
            f"当日计数={day_count}；已消费素材={sorted(consumed)}；生成类调用={generated}",
            f"复现：{REPRO}C22 ",
            "isekai_core/runtime/service.py:1983-1996 proactive_tick / store.py:724 proactive_day_count",
        )
    finally:
        await core.close()


@check("C23")
async def c23(tmp: Path) -> dict[str, Any]:
    """§5.3：主动消息 reply_to=null、固化时固定目标，目标变化不迁移旧任务；普通入站不暗中改目标。"""
    core = await start_core(tmp, replies=["信报上有一条新消息。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        add_material(core, info["id"], tl, chars[0], key="m1", text="北堤的通行牌这三天都停发了")
        result = await tick(core, info["id"], tl, per_day=2)
        message_id = str(result["messages"][0]["message_id"])
        row = core.store.outbound_by_message_id(message_id)
        target_before = core.store.thread_for_session(session["id"])
        # 普通入站不得暗中更换主动目标
        await core.send(chan, "dm-1", "在吗")
        await chan.reply(timeout=8.0)
        target_after = core.store.thread_for_session(session["id"])
        # 重绑目标 thread：既有主动消息仍保留固化时的目标与绑定版本
        again = (await core.mgmt.call("thread.bind", channel="builtin", thread_id="dm-2",
                                      session_id=session["id"]))["thread"]
        row_after = core.store.outbound_by_message_id(message_id)
        ok = (
            row["reply_to"] is None and str(row["channel_id"]) == str(thread["channel_id"])
            and str(row["thread_id"]) == "dm-1"
            and target_before["thread_id"] == target_after["thread_id"] == "dm-1"
            and row_after["thread_id"] == "dm-1" and int(row_after["binding_version"]) == int(row["binding_version"])
        )
        return R(
            "PASS" if ok else "FAIL", "§5.3 唯一目标与投递",
            "主动消息 reply_to=null；普通入站不改目标；重绑不改写已固化消息的目标与绑定版本",
            f"reply_to={row['reply_to']}；目标={row['thread_id']}（重绑后仍={row_after['thread_id']}，"
            f"binding_version {row['binding_version']}→{row_after['binding_version']}）；"
            f"普通入站前后目标={target_before['thread_id']}/{target_after['thread_id']}；新绑定={again['thread_id']}",
            f"复现：{REPRO}C23 ",
            "isekai_core/store.py:656 thread_for_session / service.py:2010 proactive_tick 固化",
        )
    finally:
        await core.close()


@check("C24")
async def c24(tmp: Path) -> dict[str, Any]:
    """§5.2 / §5.3 / 附录B6：离线积压与过期消息不得在重连时变成发送洪峰或新通知。"""
    core = await start_core(tmp, replies=["信报上有一条新消息。", "滩口的风向变了。", "盐堆被泡了。"],
                            sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, cred = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        for key, text in (("m1", "北堤的通行牌这三天都停发了"), ("m2", "驿站新到一份灾年编年的补页"),
                          ("m3", "滩口的盐堆被潮水泡了")):
            add_material(core, info["id"], tl, chars[0], key=key, text=text)
        for _ in range(3):
            await tick(core, info["id"], tl, per_day=5)
        pending = [row for row in outbound_rows(core.store, session["id"])]
        await chan.client.close()  # 通道离线
        advance_world(core, info["id"], tl, world_seconds=100000, rate=1000)  # 让积压素材过期
        chan2 = await connect_chan(core, name="builtin", credential=cred)
        await chan2.drain(seconds=1.2)
        resent = chan2.replies()
        ok = len(resent) <= 1
        return R(
            "PASS" if ok else "FAIL", "§5.2 积压 / §5.3 恢复 / 附录B6",
            "离线积压与过期主动消息不因重连变成补发洪峰（只按有界策略处理仍有效者）",
            f"离线前已固化待投递主动消息={len(pending)} 条；世界推后重连收到 reply={len(resent)} 条"
            f"（message_id={[env.payload.get('message_id') for env in resent]}）",
            f"复现：{REPRO}C24 ",
            "isekai_core/store.py:1441 pending_outbound / channel.py:220 resend_pending",
        )
    finally:
        await core.close()


# ================================================================== 初见（§5.6）


async def tick(core: Core, instance_id: str, timeline_id: str, *, per_day: int = 2) -> dict[str, Any]:
    """世界源主动发言：直接调运行层服务（管理面入口另见 C39——它当前抛 NameError）。"""
    return await core.world.proactive_tick(instance_id, timeline_id, llm=core.fake, per_day=per_day)


async def fc(core: Core, args: dict[str, Any]) -> dict[str, Any]:
    """初见独立开场：直接调运行层服务（管理面入口另见 C39）。"""
    return await core.world.first_contact(
        args["instance_id"], args["timeline_id"], args["character_id"],
        channel_id=args["channel_id"], thread_id=args["thread_id"], llm=core.fake,
    )


def first_contact_args(info: dict[str, Any], tl: str, character_id: str, thread: dict[str, Any],
                       thread_id: str) -> dict[str, Any]:
    return {
        "instance_id": info["id"], "timeline_id": tl, "character_id": character_id,
        "channel_id": str(thread["channel_id"]), "thread_id": thread_id,
    }


@check("C25")
async def c25(tmp: Path) -> dict[str, Any]:
    """§5.6：初见一次性（多窗口共用一份）；独立开场 reply_to=null，不占主动配额。"""
    core = await start_core(tmp, replies=["正好你在——风向变了。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="view-1", session_id=session["id"])
        args = first_contact_args(info, tl, chars[0], thread, "view-1")
        first = await fc(core, args)
        second = await fc(core, args)
        row = core.store.outbound_by_message_id(str(first["message_id"]))
        notice = core.store.first_contact_get(session["id"])
        ok = (first.get("spoken") is True and second.get("reused") is True
              and len(core.fake.calls) == 1 and row["reply_to"] is None
              and str(row["thread_id"]) == "view-1" and notice is not None
              and core.store.proactive_list(info["id"], tl) == [])
        return R(
            "PASS" if ok else "FAIL", "§5.6 初见一次性",
            "同一（实例,时间线,角色）只生成一次独立开场；reply_to=null；不占世界源主动配额",
            f"第一次 spoken={first.get('spoken')}；第二次 reused={second.get('reused')}；LLM 调用={len(core.fake.calls)}；"
            f"reply_to={row['reply_to']}；目标 thread={row['thread_id']}；主动配额记录={len(core.store.proactive_list(info['id'], tl))}",
            f"复现：{REPRO}C25 ",
            "isekai_core/runtime/service.py:1864 first_contact 一次性判定",
        )
    finally:
        await core.close()


@check("C26")
async def c26(tmp: Path) -> dict[str, Any]:
    """§5.6 与用户抢先发言：已有入站被接受后不再补发独立开场（免得双发）。"""
    core = await start_core(tmp, replies=["在的。", "又是我——风向变了。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="view-1", session_id=session["id"])
        await core.send(chan, "view-1", "我先说话了")
        await chan.reply(timeout=8.0)
        calls_before = len(core.fake.calls)
        result = await fc(core, first_contact_args(info, tl, chars[0], thread, "view-1"))
        outs = outbound_rows(core.store, session["id"])
        ok = result.get("spoken") is not True and len(core.fake.calls) == calls_before and len(outs) == 1
        return R(
            "PASS" if ok else "FAIL", "§5.6 与用户抢先发言 / 附录B10",
            "用户先发言后，初见意向并入首轮回复，不再生成独立开场（不双发）",
            f"用户先发言并已回复后调用初见：spoken={result.get('spoken')} reused={result.get('reused')}"
            f"；LLM 调用 {calls_before}→{len(core.fake.calls)}；会话出站行={len(outs)}",
            f"复现：{REPRO}C26 ",
            "isekai_core/runtime/service.py:1840 first_contact（无「已有入站」检查）",
        )
    finally:
        await core.close()


@check("C27")
async def c27(tmp: Path) -> dict[str, Any]:
    """§5.6 一次性与失败：生成失败不消费资格、可显式重试；已固化后不再重生成。"""
    core = await start_core(tmp, replies=["", "在的。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="view-1", session_id=session["id"])
        args = first_contact_args(info, tl, chars[0], thread, "view-1")
        failed = await fc(core, args)
        state_a = core.store.first_contact_get(session["id"])
        outs_a = outbound_rows(core.store, session["id"])
        retried = await fc(core, args)
        state_b = core.store.first_contact_get(session["id"])
        outs_b = outbound_rows(core.store, session["id"])
        third = await fc(core, args)
        outs_c = outbound_rows(core.store, session["id"])
        ok = (failed.get("spoken") is False and state_a is None and not outs_a
              and retried.get("spoken") is True and state_b is not None and len(outs_b) == 1
              and third.get("reused") is True and len(outs_c) == 1)
        return R(
            "PASS" if ok else "FAIL", "§5.6 一次性与失败 / 附录B10",
            "生成失败不留半成品、不消费资格，可显式重试；已固化后重试只复用原消息",
            f"失败次 spoken={failed.get('spoken')} 状态行={state_a is None} 出站={len(outs_a)}；"
            f"重试 spoken={retried.get('spoken')} 出站={len(outs_b)}；再调用 reused={third.get('reused')} 出站={len(outs_c)}",
            f"复现：{REPRO}C27 ",
            "isekai_core/runtime/service.py:1877 proactive_text_allowed 判定 + 1864 reused",
        )
    finally:
        await core.close()


@check("C28")
async def c28(tmp: Path) -> dict[str, Any]:
    """§5.6 回滚与继承 / 附录B10：回滚撤销开场正文，但「初见已完成」记号是控制状态，不倒退。"""
    core = await start_core(tmp, replies=["正好你在——风向变了。", "又是我——风向变了。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="view-1", session_id=session["id"])
        args = first_contact_args(info, tl, chars[0], thread, "view-1")
        base_commit = (await core.mgmt.call("runtime.commit", instance_id=info["id"], timeline_id=tl,
                                            note="初见前"))["commit"]
        first = await fc(core, args)
        events_before = len(core.store.event_window(info["id"], tl, until=10 ** 12, limit=500))
        await core.mgmt.call("runtime.rollback", instance_id=info["id"], timeline_id=tl,
                             commit_id=base_commit["id"], confirm=True)
        body_gone = len(outbound_rows(core.store, session["id"])) == 0
        mark_kept = core.store.first_contact_get(session["id"]) is not None
        again = await fc(core, args)
        outs_after = outbound_rows(core.store, session["id"])
        events_after = len(core.store.event_window(info["id"], tl, until=10 ** 12, limit=500))
        ok = mark_kept and again.get("reused") is True and len(outs_after) == 0 and events_before == events_after
        return R(
            "PASS" if ok else "FAIL", "§5.6 回滚与继承 / 附录B10",
            "回滚撤销开场正文，但初见记号不倒退：不再产生新开场（也不改事实）",
            f"开场正文随回滚消失={body_gone}；初见记号仍在={mark_kept}；回滚后再调用 spoken={again.get('spoken')} "
            f"reused={again.get('reused')}；会话出站行={len(outs_after)}；事件数 {events_before}→{events_after}",
            f"复现：{REPRO}C28 ",
            "isekai_core/store.py:1889 timeline_clear_state（回滚里删除 first_contact 行）",
        )
    finally:
        await core.close()



# ================================================================== 寿终后的联络收束（§5.7）


@check("C29")
async def c29(tmp: Path) -> dict[str, Any]:
    """§5.7 / 附录B9：归档后历史可查、入站明确拒绝、归档说明只给一次。"""
    core = await start_core(tmp, replies=["在的。", "不该出现。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        await core.send(chan, "dm-1", "在吗")
        await chan.reply(timeout=8.0)
        kill_character(core, info["id"], tl, chars[0])
        await core.raw(chan, "dm-1", "还在吗", "e-after-death")
        err1 = await chan.error()
        try:
            # 归档说明按 system_notice 投递（C30）：不能只等 reply
            notice_env = await chan.wait(
                lambda env: env.type == "system_notice" or bool(env.payload.get("message_id")),
                timeout=6.0,
            )
        except TimeoutError:
            notice_env = None
        notice = core.store.session_notice_get(session["id"], "archive")
        page = await core.mgmt.call("history.page", session_id=session["id"], limit=50)
        texts = [row["text"] for row in page["messages"]]
        await core.raw(chan, "dm-1", "再问一次", "e-after-death-2")
        err2 = await chan.error()
        notice2 = core.store.session_notice_get(session["id"], "archive")
        outs = outbound_rows(core.store, session["id"])
        ok = (
            err1.payload.get("code") == "state_blocked" and err2.payload.get("code") == "state_blocked"
            and notice is not None and notice2 is not None
            and str(notice2["message_id"]) == str(notice["message_id"])
            and notice_env is not None and notice_env.payload.get("message_id") == notice["message_id"]
            and "在吗" in texts and len(outs) == 2  # 原回复 + 一条归档说明
        )
        return R(
            "PASS" if ok else "FAIL", "§5.7 归档 / 附录B9",
            "入站明确拒绝、不静默丢弃；归档说明只固化一次；既有历史与记忆保留可查",
            f"两次入站错误码={err1.payload.get('code')}/{err2.payload.get('code')}；"
            f"归档说明 message_id={notice['message_id'] if notice else None}（第二次仍相同="
            f"{str(notice2['message_id']) == str(notice['message_id']) if notice and notice2 else None}）；"
            f"历史仍含早先往来={'在吗' in texts}；会话出站行={len(outs)}",
            f"复现：{REPRO}C29 ",
            "isekai_core/session.py:104-115 accept / session.py:159-193 _archive_notice",
        )
    finally:
        await core.close()


@check("C30")
async def c30(tmp: Path) -> dict[str, Any]:
    """§5.7：归档说明必须标记为联络系统 / 管理机制的说明，不能伪装成死者的新回复。"""
    core = await start_core(tmp, replies=["在的。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        await core.send(chan, "dm-1", "在吗")
        await chan.reply(timeout=8.0)
        kill_character(core, info["id"], tl, chars[0])
        await core.raw(chan, "dm-1", "还在吗", "e-after-death")
        await chan.error()
        try:
            notice_env = await chan.wait(
                lambda env: env.type in ("system_notice", "reply"), timeout=6.0
            )
        except TimeoutError:
            notice_env = None
        notice = core.store.session_notice_get(session["id"], "archive")
        row = core.store.outbound_by_message_id(str(notice["message_id"]))
        text = core.store.message_text(row)
        envelope_type = notice_env.type if notice_env else "（未收到）"
        ok = envelope_type == "system_notice" and str(row["role"]) != "character"
        return R(
            "PASS" if ok else "FAIL", "§5.7 收束说明标记",
            "归档说明以 system_notice 类型投递、消息行有联络系统 / 管理机制标记，不伪装成死者新回复",
            f"投递信封类型={envelope_type}；消息行 role={row['role']!r}；reply_to={row['reply_to']}；"
            f"正文={text!r}（含内部角色标识 {'cc-' in text}）",
            f"复现：{REPRO}C30 ",
            "isekai_core/session.py:165-177 _archive_notice（走 outbound_put → role='character'，"
            "投递走 session.py:574 _send_batches → type='reply'）",
        )
    finally:
        await core.close()


@check("C31")
async def c31(tmp: Path) -> dict[str, Any]:
    """§5.7 第一条：核心取消未固化生成、等待任务与主动候选。"""
    core = await start_core(tmp, replies=["不该出现的回复。"], sleep=(2.0, 2.0))
    try:
        info, tl, chars, _ = make_instance(core, moment=SLEEP_MOMENT)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        env_id = await core.send(chan, "dm-1", "等待期间角色寿终")
        await asyncio.sleep(0.3)
        kill_character(core, info["id"], tl, chars[0])
        await asyncio.sleep(2.4)
        await chan.drain(seconds=0.3)
        row = inbound_row(core, thread, env_id)
        outs = outbound_rows(core.store, session["id"])
        tick_result = await tick(core, info["id"], tl, per_day=2)
        ok = row["state"] == "cancelled" and not outs and tick_result.get("spoken") == 0
        return R(
            "PASS" if ok else "FAIL", "§5.7 未固化任务失效",
            "寿终后未固化的等待任务与主动候选一并失效：不写回、不投递、不再产生主动消息",
            f"等待中的入站 state={row['state']} error_code={row['error_code']}；出站行={len(outs)}（正文={[r['text'] for r in outs]}）；"
            f"归档后主动 tick={tick_result}",
            f"复现：{REPRO}C31 ",
            "isekai_core/session.py:401-415 _stale_code（不含归档判定）/ service.py:1962 proactive_tick 归档跳过",
        )
    finally:
        await core.close()


@check("C32")
async def c32(tmp: Path) -> dict[str, Any]:
    """§5.7 第二条：寿终前已固化但未投递的合法回复按普通投递恢复规则处理。"""
    core = await start_core(tmp, replies=["寿终前已固化的回复。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, cred = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        core.fake.delay_s = 0.9
        await core.send(chan, "dm-1", "断线期间生成")
        await asyncio.sleep(0.2)
        await chan.client.close()
        await asyncio.sleep(1.4)
        outs = outbound_rows(core.store, session["id"])
        states = [row["state"] for row in core.store.delivery_rows(int(outs[0]["seq"]))] if outs else []
        kill_character(core, info["id"], tl, chars[0])
        calls = len(core.fake.calls)
        chan2 = await connect_chan(core, name="builtin", credential=cred)
        try:
            rep = await chan2.reply(timeout=6.0)
        except TimeoutError:
            rep = None
        ok = (len(outs) == 1 and states == ["pending"] and rep is not None
              and rep.payload.get("message_id") == outs[0]["message_id"]
              and len(core.fake.calls) == calls)
        return R(
            "PASS" if ok else "FAIL", "§5.7 已固化回复的投递恢复",
            "寿终前已固化未投递的回复仍可按原投递资格恢复；不重新生成、不倒填新的生前回复",
            f"固化后投递状态={states}；归档后重连收到 reply={rep.payload.get('message_id') if rep else None}"
            f"（原 message_id={outs[0]['message_id'] if outs else None}）；LLM 调用 {calls}→{len(core.fake.calls)}",
            f"复现：{REPRO}C32 ",
            "isekai_core/store.py:1441 pending_outbound / channel.py:220 resend_pending",
        )
    finally:
        await core.close()


# ================================================================== 披露与隔离（§七）


async def two_characters(core: Core) -> dict[str, Any]:
    """建一个两角色实例 + 两个会话各自绑一个通道，返回常用句柄。"""
    info, tl, chars, _ = make_instance(core, names=("堤禾", "潮生"))
    activate(core, info["id"], tl)
    sa = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                               character_id=chars[0]))["session"]
    sb = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                               character_id=chars[1]))["session"]
    chan_a, thread_a, _ = await bind_channel_async(core, name="builtin", thread_id="dm-a", session_id=sa["id"])
    chan_b, thread_b, _ = await bind_channel_async(core, name="mirror", thread_id="dm-b", session_id=sb["id"])
    return {"info": info, "tl": tl, "chars": chars, "sa": sa, "sb": sb,
            "chan_a": chan_a, "thread_a": thread_a, "chan_b": chan_b, "thread_b": thread_b}


@check("C33")
async def c33(tmp: Path) -> dict[str, Any]:
    """§7.1：默认隔离（A 的私聊不进 B 的上下文）；含糊转述不产生授权。"""
    core = await start_core(tmp, replies=["嗯。", "我在听。"], sleep=(0.0, 0.0))
    try:
        h = await two_characters(core)
        secret = "我昨天把水位尺的绳子换成新的了"
        env_a = await core.send(h["chan_a"], "dm-a", secret)
        await h["chan_a"].reply(timeout=8.0)
        await core.send(h["chan_b"], "dm-b", "今天怎么样")
        await h["chan_b"].reply(timeout=8.0)
        calls = prompts_with(core.fake, "今天怎么样")
        body_b = prompt_text(calls[-1]) if calls else ""
        try:
            await core.mgmt.call("disclose.confirm", instance_id=h["info"]["id"], timeline_id=h["tl"],
                                 from_character=h["chars"][0], to_character=h["chars"][1], refs=[])
            vague_code = "ok"
        except Exception as exc:  # noqa: BLE001 - UmpError
            vague_code = getattr(exc, "code", "error")
        disclosures = core.world.disclosures(h["info"]["id"], h["tl"])
        ok = secret not in body_b and vague_code == "invalid_input" and not disclosures
        return R(
            "PASS" if ok else "FAIL", "§7.1 默认隔离与显式授权",
            "A 的私聊不进 B 的生成上下文；含糊 / 空范围转述不产生授权记录",
            f"B 的上下文含 A 私聊原文={secret in body_b}；空 refs 的披露确认结果={vague_code}；"
            f"授权记录数={len(disclosures)}",
            f"复现：{REPRO}C33 ；envA={env_a}",
            "isekai_core/runtime/service.py:920 turn_context 披露块 / runtime/disclosure.py:20 normalize_scope",
        )
    finally:
        await core.close()


@check("C34")
async def c34(tmp: Path) -> dict[str, Any]:
    """§7.1：明确披露后接收角色只看到该范围内的转述（不是亲历、也不多给）。"""
    core = await start_core(tmp, replies=["嗯。", "我在听。"], sleep=(0.0, 0.0))
    try:
        h = await two_characters(core)
        shared = "我昨天把水位尺的绳子换成新的了"
        withheld = "（这句不在披露范围）绳子是信使送来的"
        env_a = await core.send(h["chan_a"], "dm-a", shared)
        await h["chan_a"].reply(timeout=8.0)
        await core.send(h["chan_a"], "dm-a", withheld)
        await h["chan_a"].reply(timeout=8.0)
        granted = await core.mgmt.call("disclose.confirm", instance_id=h["info"]["id"], timeline_id=h["tl"],
                                       from_character=h["chars"][0], to_character=h["chars"][1], refs=[env_a],
                                       note="只给这一条")
        await core.send(h["chan_b"], "dm-b", "她跟你说了什么")
        await h["chan_b"].reply(timeout=8.0)
        calls = prompts_with(core.fake, "她跟你说了什么")
        body = prompt_text(calls[-1]) if calls else ""
        ok = ("联络者明确给你看过这些转述" in body and shared in body
              and withheld not in body and "不是你亲历的" in body)
        return R(
            "PASS" if ok else "FAIL", "§7.1 披露范围与转述",
            "接收角色只看到明确范围内的片段，且标明是转述、不是亲历；范围外内容不可见",
            f"披露记录 id={granted.get('id')}（复用={granted.get('reused')}，范围条数={len(granted.get('scope') or [])}）；"
            f"B 上下文含披露块={'联络者明确给你看过这些转述' in body}、含被披露原文={shared in body}、"
            f"含范围外原文={withheld in body}、标明非亲历={'不是你亲历的' in body}",
            f"复现：{REPRO}C34 ",
            "isekai_core/runtime/service.py:216 disclosed_fragments / runtime/disclosure.py:38 brief_block",
        )
    finally:
        await core.close()


@check("C35")
async def c35(tmp: Path) -> dict[str, Any]:
    """§7.2 / §7.1：回滚撤销授权与派生；从披露前提交分叉不假称原线已撤回。"""
    core = await start_core(tmp, replies=["嗯。", "我在听。", "又听见了。"], sleep=(0.0, 0.0))
    try:
        h = await two_characters(core)
        shared = "我昨天把水位尺的绳子换成新的了"
        env_a = await core.send(h["chan_a"], "dm-a", shared)
        await h["chan_a"].reply(timeout=8.0)
        base = (await core.mgmt.call("runtime.commit", instance_id=h["info"]["id"], timeline_id=h["tl"],
                                     note="披露前"))["commit"]
        await core.mgmt.call("disclose.confirm", instance_id=h["info"]["id"], timeline_id=h["tl"],
                             from_character=h["chars"][0], to_character=h["chars"][1], refs=[env_a])
        await core.send(h["chan_b"], "dm-b", "她跟你说了什么")
        await h["chan_b"].reply(timeout=8.0)
        calls = prompts_with(core.fake, "她跟你说了什么")
        body_with = prompt_text(calls[-1]) if calls else ""
        tasks_with = core.store.memory_tasks(h["info"]["id"], h["tl"])
        # 从披露前提交分叉：新线没有授权，原线仍有
        fork = await core.mgmt.call("runtime.fork", instance_id=h["info"]["id"], timeline_id=h["tl"],
                                    commit_id=base["id"], name="披露前分叉", activate=False)
        new_tl = str(fork["timeline"]["id"])
        original_kept = core.world.disclosures(h["info"]["id"], h["tl"])
        forked_has = core.world.disclosures(h["info"]["id"], new_tl)
        # 回滚原线到披露前
        await core.mgmt.call("runtime.rollback", instance_id=h["info"]["id"], timeline_id=h["tl"],
                             commit_id=base["id"], confirm=True)
        after_rollback = core.world.disclosures(h["info"]["id"], h["tl"])
        await core.send(h["chan_b"], "dm-b", "再说一次")
        await h["chan_b"].reply(timeout=8.0)
        calls2 = prompts_with(core.fake, "再说一次")
        body_after = prompt_text(calls2[-1]) if calls2 else ""
        tasks_after = core.store.memory_tasks(h["info"]["id"], h["tl"])
        ok = (len(original_kept) == 1 and not forked_has and not after_rollback
              and "联络者明确给你看过这些转述" in body_with
              and "联络者明确给你看过这些转述" not in body_after and len(tasks_after) <= len(tasks_with))
        return R(
            "PASS" if ok else "FAIL", "§7.2 撤回 / §7.1 授权版本化",
            "回滚同时撤销授权与派生认知；从旧提交分叉不撤销原线授权；两条路径不混为一谈",
            f"分叉后原线授权={len(original_kept)} 条、新线={len(forked_has)} 条；回滚后原线授权={len(after_rollback)} 条；"
            f"披露后 B 上下文含披露块={'联络者明确给你看过这些转述' in body_with}、回滚后={('联络者明确给你看过这些转述' in body_after)}；"
            f"提取任务 {len(tasks_with)}→{len(tasks_after)}",
            f"复现：{REPRO}C35 ",
            "isekai_core/store.py:1889 timeline_clear_state 删除 disclosure / runtime/service.py:570 rollback",
        )
    finally:
        await core.close()


# ================================================================== 分页 / 重启 / 接口可达性


@check("C36")
async def c36(tmp: Path) -> dict[str, Any]:
    """§3：分页历史只返回往来原文与处理 / 投递状态，不返回内部记忆、提示词或事件日志。"""
    core = await start_core(tmp, replies=["收到。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        await core.send(chan, "dm-1", "分页用的一句话")
        await chan.reply(timeout=8.0)
        page = await core.mgmt.call("history.page", session_id=session["id"], limit=50)
        keys: set[str] = set()
        for row in page["messages"]:
            keys |= set(row.keys())
        allowed = {"seq", "role", "text", "parts", "env_id", "message_id", "reply_message_id",
                   "reply_to", "batch_count", "state", "created_at"}
        extra = sorted(keys - allowed)
        leaky = sorted(key for key in keys if any(token in key.lower()
                                                 for token in ("memory", "prompt", "brief", "event", "system", "secret")))
        ok = not extra and not leaky and str(page.get("session", {}).get("id")) == session["id"]
        return R(
            "PASS" if ok else "FAIL", "§3 分页历史接口",
            "只回往来原文与处理 / 投递状态；不含记忆、提示词与事件日志字段",
            f"分页消息字段={sorted(keys)}；越界字段={extra + leaky}；顶层字段={sorted(page.keys())}",
            f"复现：{REPRO}C36 ",
            "isekai_core/channel.py:557 _public_message / store.py:1455 history_page",
        )
    finally:
        await core.close()


@check("C37")
async def c37(tmp: Path) -> dict[str, Any]:
    """§3 / 附录B8：分页游标绑定历史版本，回滚后旧游标与客户端缓存不得继续展示已撤销内容。"""
    core = await start_core(tmp, replies=["第一句回复。", "第二句回复。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        await core.send(chan, "dm-1", "第一句（保留）")
        await chan.reply(timeout=8.0)
        base = (await core.mgmt.call("runtime.commit", instance_id=info["id"], timeline_id=tl,
                                     note="第二句前"))["commit"]
        await core.send(chan, "dm-1", "第二句（将被回滚）")
        await chan.reply(timeout=8.0)
        page_before = await core.mgmt.call("history.page", session_id=session["id"], limit=50)
        cursor = page_before["next_before_seq"]
        await core.mgmt.call("runtime.rollback", instance_id=info["id"], timeline_id=tl,
                             commit_id=base["id"], confirm=True)
        page_after = await core.mgmt.call("history.page", session_id=session["id"], limit=50,
                                          before_seq=cursor)
        texts_after = [row["text"] for row in page_after["messages"]]
        stale_still_returned = any("第二句" in str(text or "") for text in texts_after)
        version_keys = sorted(key for key in page_after.keys()
                              if any(token in key.lower() for token in ("version", "generation", "watermark", "rev")))
        ok = (not stale_still_returned) and bool(version_keys)
        return R(
            "PASS" if ok else "FAIL", "§3 游标与历史版本 / 附录B8",
            "回滚后旧游标不返回已撤销内容，且分页响应带历史版本标识（客户端据此失效本地缓存）",
            f"回滚后旧游标仍返回已撤销消息={stale_still_returned}（服务端清得干净）；"
            f"分页响应字段={sorted(page_after.keys())}（版本 / 世代类字段={version_keys}）；旧游标={cursor}；"
            f"客户端缓存无判定依据：desktop/src/main.ts:411 loadHistory 把当页缓存进 state.messages，"
            f"续取只用 before_seq，且桌面端无回滚调用（main.ts 全文无 rollback）",
            f"复现：{REPRO}C37 ",
            "isekai_core/channel.py:539-553 history.page 响应（无版本 / 世代字段）",
        )
    finally:
        await core.close()


@check("C38")
async def c38(tmp: Path) -> dict[str, Any]:
    """§3 / 附录B8：关闭自动提交后重启仍能恢复历史。"""
    core = await start_core(tmp, replies=["第一句回复。"], sleep=(0.0, 0.0), autocommit=False)
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="dm-1", session_id=session["id"])
        await core.send(chan, "dm-1", "关掉自动提交也要留下的一句话")
        await chan.reply(timeout=8.0)
        commits_before = len(core.store.commit_list(info["id"], tl))
        await core.close()
        core2 = await start_core(tmp, replies=["又一句。"], sleep=(0.0, 0.0), autocommit=False)
        core = None
        try:
            page = await core2.mgmt.call("history.page", session_id=session["id"], limit=50)
            texts = [row["text"] for row in page["messages"]]
            ok = "关掉自动提交也要留下的一句话" in texts and len(core2.store.timeline_list(info["id"])) == 1
            return R(
                "PASS" if ok else "FAIL", "§3 持久化独立于自动提交 / 附录B8",
                "自动提交关闭时聊天与世界状态仍跨重启恢复；自动提交只决定可选回滚点",
                f"autocommit_enabled={core2.runtime.world.autocommit_enabled}；重启前提交数={commits_before}；"
                f"重启后历史={texts}",
                f"复现：{REPRO}C38 ",
                "isekai_core/app.py:116 build_runtime（重启后历史来自 SQLite）",
            )
        finally:
            await core2.close()
    finally:
        if core is not None:
            await core.close()


@check("C39")
async def c39(tmp: Path) -> dict[str, Any]:
    """§5.6 触发 / §5.2 主动发言：受信管理面入口必须真的可用。"""
    core = await start_core(tmp, replies=["正好你在——风向变了。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        session = (await core.mgmt.call("session.ensure", instance_id=info["id"], timeline_id=tl,
                                        character_id=chars[0]))["session"]
        chan, thread, _ = await bind_channel_async(core, name="builtin", thread_id="view-1", session_id=session["id"])
        results: dict[str, str] = {}
        for op, extra in (
            ("runtime.first_contact", {"instance_id": info["id"], "timeline_id": tl, "character_id": chars[0],
                                       "channel_id": str(thread["channel_id"]), "thread_id": "view-1"}),
            ("runtime.proactive", {"instance_id": info["id"], "timeline_id": tl, "per_day": 2}),
        ):
            try:
                outcome = await core.mgmt.call(op, **extra)
                results[op] = f"ok:{outcome}"
            except Exception as exc:  # noqa: BLE001 - UmpError
                results[op] = f"{getattr(exc, 'code', 'error')}: {exc}"
        first_row = core.store.first_contact_get(session["id"])
        ok = all(value.startswith("ok:") for value in results.values())
        return R(
            "PASS" if ok else "FAIL", "§5.6 触发 / §5.2 主动发言（管理面入口）",
            "管理面 runtime.first_contact / runtime.proactive 可正常调用（初见登记与主动发言的唯一入口）",
            f"调用结果={results}；初见状态行={first_row}",
            f"复现：{REPRO}C39 ",
            "isekai_core/world/ops.py:842 dispatch_async 签名无 runtime，:855/:865 引用未定义变量 runtime",
        )
    finally:
        await core.close()


@check("C40")
async def c40(tmp: Path) -> dict[str, Any]:
    """§5.2 节律：运行中的核心应有主动发言的调度入口（按生活节律择时），不能只留一个坏掉的 op。"""
    from isekai_core import app as app_mod

    core = await start_core(tmp, replies=["信报上有一条新消息。"], sleep=(0.0, 0.0))
    try:
        info, tl, chars, _ = make_instance(core)
        activate(core, info["id"], tl)
        add_material(core, info["id"], tl, chars[0], key="m1", text="北堤的通行牌这三天都停发了")
        stop = asyncio.Event()
        ticker = asyncio.create_task(app_mod._clock_tick(core.runtime, stop, interval=0.15))
        await asyncio.sleep(0.7)
        stop.set()
        await asyncio.gather(ticker, return_exceptions=True)
        logged = core.store.proactive_list(info["id"], tl)
        ok = len(logged) >= 1
        return R(
            "PASS" if ok else "FAIL", "§5.2 节律与调度 / §5.3 投递",
            "核心自身的周期任务里包含主动发言（有素材、清醒、有额度时按节律生成并投递）",
            f"跑了约 0.7s 的真核心时钟 tick（{app_mod._clock_tick.__name__}，interval=0.15）后，"
            f"proactive_log 记录={len(logged)} 条；素材已存在且角色清醒",
            f"复现：{REPRO}C40 ",
            "isekai_core/app.py:246 _clock_tick（catch_up / propose_intents / auto_commit / extract / embed，"
            "无 proactive_tick）",
        )
    finally:
        await core.close()


#: 无法在本探针里构成行为证据的条目（只换 LLM，不能验证措辞级行为）
DEFERRED_ITEMS: list[dict[str, Any]] = [
    {
        "status": "DEFERRED",
        "clause": "附录B12 / §5.1 表达取舍",
        "expected": "角色不愿讲的已知内容在反复追问下保持既有取舍：不按次数解锁、不泄漏未知实情、不临场编造掩饰事实",
        "observed": "本轮只把 LLM 换成脚本替身，无法验证措辞级行为；机制面可验证的部分（无「按提问次数解锁」的代码路径、"
                    "上下文不含未获知实情）由 C20 覆盖",
        "evidence": f"复现：{REPRO}C20 （机制面）；措辞面需真实模型",
        "code_ref": "isekai_core/runtime/cognition.py:35 knowledge_slice",
    },
    {
        "status": "DEFERRED",
        "clause": "附录B13 / §5.1 末条",
        "expected": "相关新经历与早先对话可自然回接；换话题后不出现任务催办、进度或补偿性线索",
        "observed": "属模型措辞行为；机制面已核：上下文不含用户任务清单 / 进度字段，"
                    "对话轮次不产生世界效果（C19）",
        "evidence": f"复现：{REPRO}C19 （机制面）",
        "code_ref": "isekai_core/runtime/cognition.py:171 render_prompt",
    },
    {
        "status": "DEFERRED",
        "clause": "附录B14 后半 / §4.4",
        "expected": "打算未实际执行前不产生「已做过」的表述",
        "observed": "属模型措辞行为；「打算跨轮延续」的机制面已核：打算进上下文（C20），重启后行仍在（C38 同库）",
        "evidence": f"复现：{REPRO}C20 ",
        "code_ref": "isekai_core/runtime/cognition.py:204-210（intents 进扮演定义）",
    },
    {
        "status": "DEFERRED",
        "clause": "附录B17 / §4.4 → EVENT_ENGINE_SPEC",
        "expected": "跨水位的角色故事单元能表达目标 / 阻碍 / 选择 / 代价与局部终态",
        "observed": "属事件引擎的表述验收（文本由事件引擎渲染后固化），会话层只投递已固化文本；"
                    "本轮范围不含事件引擎渲染质量",
        "evidence": f"复现：{REPRO}C08 （会话层只验收「固化 → 投递」这一段）",
        "code_ref": "isekai_core/runtime/service.py:2037 _proactive_prompt / runtime/render.py",
    },
    {
        "status": "DEFERRED",
        "clause": "§7.1 跨实例 / 跨线的用户叙述",
        "expected": "跨实例 / 跨线的用户叙述仍是当前会话中的转述，不触发跨域数据库查询",
        "observed": "披露接口按 (instance, timeline) 取作用域，代码里不存在跨域查询入口；"
                    "「不会发生某件事」无法用一次运行证明，归为接口面结论",
        "evidence": f"复现：{REPRO}C33 （同线隔离已实测）",
        "code_ref": "isekai_core/runtime/service.py:216 disclosed_fragments",
    },
    {
        "status": "DEFERRED",
        "clause": "§4.2.5 校验 / §4.2.4 生成",
        "expected": "检查输出完整性、认知边界、当前事实表述与来源；有界重试",
        "observed": "FakeLLM 只回脚本文本，无法产生「越界输出」来触发校验；「空回复不当成功、按可重试失败处理」"
                    "已由 C27 实测覆盖，认知边界由 C20（上下文侧）覆盖",
        "evidence": f"复现：{REPRO}C27 ",
        "code_ref": "isekai_core/session.py:334-342（空回复按失败）+ runtime 侧无输出校验层",
    },
]


async def run_one(cid: str, fn: Callable[[Path], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"scaudit-{cid}-", ignore_cleanup_errors=True) as raw:
        tmp = Path(raw)
        code_ref = ""
        try:
            result = await asyncio.wait_for(fn(tmp), timeout=180)
        except Exception as exc:  # 探针自身异常也要如实报出来
            return {
                "status": "ERROR",
                "clause": "(探针异常)",
                "expected": "-",
                "observed": f"{type(exc).__name__}: {exc}",
                "evidence": f"复现：{REPRO}{cid} ；\n" + traceback.format_exc()[-800:],
                "code_ref": code_ref,
            }
        return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    selected = [(cid, fn) for cid, fn in CHECKS if args.only is None or cid in args.only]
    results: list[dict[str, Any]] = []
    if args.only is None:
        for index, item in enumerate(DEFERRED_ITEMS, start=1):
            results.append({**item, "id": f"D{index}", "seconds": 0.0})
            print(f"[DEFER] D{index} {item['clause']}")
            print(f"       原因：{item['observed']}")
    for cid, fn in selected:
        started = time.monotonic()
        result = await run_one(cid, fn)
        result["id"] = cid
        result["seconds"] = round(time.monotonic() - started, 2)
        results.append(result)
        print(f"[{result['status']:>4}] {cid} {result['clause']}")
        print(f"       期望：{result['expected']}")
        print(f"       实测：{result['observed']}")
        if result["status"] != "PASS":
            print(f"       代码：{result['code_ref']}")
    summary = {status: sum(1 for item in results if item["status"] == status)
               for status in ("PASS", "FAIL", "DEFERRED", "ERROR")}
    print("汇总：" + ", ".join(f"{key}={value}" for key, value in summary.items()))
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if summary.get("FAIL", 0) == 0 and summary.get("ERROR", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))


