"""SESSION_CORE_SPEC 附录 B（行为验收）探针：临时库 + 假 LLM，不启核心进程、不碰 data/isekai.db。

每个条目一个自检函数，返回 (status, evidence)。status ∈ PASS / FAIL / DEFERRED / SKIP。
用法：`.venv/Scripts/python.exe scripts/_audit_session_core.py`
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from isekai_core import ump  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402
from isekai_core.runtime import intents as intents_mod  # noqa: E402
from isekai_core.runtime.service import RuntimeService, RuntimeStateError  # noqa: E402
from isekai_core.session import SessionService  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.ump import UmpError  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402

NOW = 1_700_000_000.0

#: 固化后尾部（投递 / 记账）跑完的等待，免得探针抢在任务收尾前关库
TAIL_SLEEP = 0.08


# ---------------------------------------------------------------- 脚手架


class Harness:
    """真 Store（临时库）+ 真 RuntimeService + 真 SessionService；只把 LLM 与投递出口换成替身。"""

    def __init__(self, root: Path, replies: list[str] | None = None) -> None:
        self.cfg = load_config(root)
        self.store = Store(self.cfg.paths.db)
        self.store.ensure_schema()
        self.world = RuntimeService(self.store, autocommit_enabled=False)
        self.llm = FakeLLM(replies or ["收到。"])
        self.sent: list[dict] = []
        self.deliver_ok = True
        self.service = SessionService(
            store=self.store, cfg=self.cfg, llm=self.llm, deliver=self._deliver, runtime=self.world
        )

    async def _deliver(self, channel_id: str, thread_id: str, envelope: dict) -> bool:
        self.sent.append({"channel": channel_id, "thread": thread_id,
                          "type": envelope.get("type"), "payload": envelope.get("payload")})
        return self.deliver_ok

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:  # 探针自己换过库句柄时不再报错
            pass


def make_world(h: Harness, *, cards: list[dict] | None = None, moment: int = DAY * 1500,
               name: str = "灰潮纪审计"):
    package = example_package(name, moment=moment)
    cards = cards if cards is not None else [example_card(package)]
    info = create_instance(h.store, package, cards)
    timeline_id = h.store.timeline_list(info["id"])[0]["id"]
    h.world.ensure_instance(info["id"], now_real=NOW)
    return package, info, timeline_id, cards


def bind(h: Harness, session_id: str, *, channel: str = "builtin", thread: str = "dm-1"):
    row, _cred = h.store.channel_register(
        name=channel, display_name=channel, version="0", protocol=ump.UMP_VERSION, capabilities={}
    )
    thread_row = h.store.thread_bind(row["id"], thread, session_id)
    return row["id"], thread_row


def envelope(h: Harness, text: str, *, thread_id: str, token: str, env_id: str):
    raw = ump.make("user_message", {"text": text}, thread_id=thread_id, binding_token=token, id=env_id)
    return ump.parse(raw, direction="c2s", max_text_len=h.cfg.max_text_len)


async def wait_state(store: Store, channel: str, thread: str, env_id: str, *, timeout: float = 4.0):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = store.inbound_find(channel, thread, env_id)
        if row is not None and row["state"] in ("done", "failed", "cancelled"):
            await asyncio.sleep(TAIL_SLEEP)  # 等固化的尾部（投递 / 记账）跑完
            return store.inbound_find(channel, thread, env_id)
        await asyncio.sleep(0.02)
    return row


def known_texts(h: Harness, info, timeline_id: str, character_id: str, *, moment: int, card=None) -> set[str]:
    """该角色此刻能合法看到的文本集合：用于区分「实情泄漏」与「不同事件同文碰撞」。"""
    snapshot = h.world.character_snapshot(info["id"], timeline_id, character_id, world_seconds=moment)
    legit = {str(row["text"] or "") for row in snapshot["knowledge"]}
    legit |= {str(row["summary"] or "") for row in snapshot["experiences"]}
    legit |= {str(row.get("value") or "") for row in snapshot["observations"]}
    if card:
        legit.add(str((card.get("background") or {}).get("self_knowledge") or ""))
        for entry in card.get("initial_knowledge") or []:
            legit.add(str(entry.get("claim") or ""))
    return {text for text in legit if text}


def unknown_truth_details(h: Harness, info, timeline_id: str, character_id: str, *, moment: int) -> list[str]:
    """她一无所知的那些事件的实情文本（`event.detail`）。"""
    known = h.store.knowledge_ids(info["id"], timeline_id, character_id)
    out: list[str] = []
    for event in h.store.event_window(info["id"], timeline_id, until=moment, limit=300):
        claims = h.store.claim_list(info["id"], timeline_id, event_id=str(event["id"]))
        if not claims or any(str(claim["id"]) in known for claim in claims):
            continue
        detail = str(event["detail"] or "").strip()
        if detail:
            out.append(detail)
    return out


def prompt_of(h: Harness, info, timeline_id: str, character_id: str, *, topic: str = "") -> str:
    return h.world.turn_context(
        {"instance_id": info["id"], "timeline_id": timeline_id, "character_id": character_id}, topic=topic
    )["prompt"]


def outbound_rows(store: Store, session_id: str) -> list[dict]:
    rows = store.history_page(session_id, limit=200)["messages"]
    return [row for row in rows if row["role"] != "user"]


def say(store: Store, info, timeline_id: str, character_id: str, *, env: str, text: str, reply: str):
    """固化一轮对话（不经 LLM）：返回 (回复 message_id, session)。"""
    session = store.session_ensure(info["id"], timeline_id, character_id)
    store.inbound_put(session_id=session["id"], channel_id="cli-dev", thread_id=f"t-{character_id}",
                      env_id=env, text=text, binding_version=1)
    fixed = store.outbound_put(session_id=session["id"], message_id=f"m-{env}", reply_to=env, covers=[env],
                               batches=[[reply]], target_channel="cli-dev", target_thread=f"t-{character_id}",
                               binding_version=1, binding_token="tok")
    return fixed["message_id"], session


def tables(store: Store) -> set[str]:
    return {row[0] for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def source_scan(*needles: str) -> dict[str, list[str]]:
    """在 isekai_core 里找关键字落在哪些文件（用于「有没有这条代码路径」的静态证据）。"""
    hits: dict[str, list[str]] = {}
    for path in (ROOT / "isekai_core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                hits.setdefault(needle, []).append(path.relative_to(ROOT).as_posix())
    return hits


# ---------------------------------------------------------------- 条目 1


async def check_01(tmp: Path):
    """1. 切换世界 / 角色 / 时间线或更换通道后，历史不迁移、不串读；同三元组复用同一历史。"""
    h = Harness(tmp)
    try:
        package, info, timeline_id, cards = make_world(h)
        first = str(cards[0]["meta"]["card_id"])
        second_card = example_card(package, name="堤砚")
        h.world.add_character(info["id"], timeline_id, second_card, now_real=NOW,
                              joined_world=int(h.store.clock_get(timeline_id)["processed_world"]))
        second = str(second_card["meta"]["card_id"])
        s_a = h.store.session_ensure(info["id"], timeline_id, first)
        s_a2 = h.store.session_ensure(info["id"], timeline_id, first)
        s_b = h.store.session_ensure(info["id"], timeline_id, second)
        # 换世界：另一实例的同名角色必须是另一条会话
        _p2, info2, tl2, cards2 = make_world(h, name="灰潮纪审计二")
        s_a_world2 = h.store.session_ensure(info2["id"], tl2, str(cards2[0]["meta"]["card_id"]))
        # 换时间线：同实例另一条线同样另立会话
        branch = h.world.fork(info["id"], timeline_id, commit_id=h.world.commit(info["id"], timeline_id)["id"])
        s_a_tl2 = h.store.session_ensure(info["id"], branch["timeline"]["id"], first)

        say(h.store, info, timeline_id, first, env="env-a", text="只有她知道的事", reply="A 的私聊")
        say(h.store, info, timeline_id, second, env="env-b", text="B 的话", reply="B 的回")
        page_a = h.store.history_page(s_a["id"], limit=50)["messages"]
        page_b = h.store.history_page(s_b["id"], limit=50)["messages"]
        leak = [row for row in page_b if "A 的私聊" in json.dumps(row, ensure_ascii=False)]

        channel, thread = bind(h, s_a["id"], thread="dm-1")
        before = [row["seq"] for row in h.store.history_page(s_a["id"], limit=50)["messages"]]
        h.store.thread_bind(channel, "dm-1", s_b["id"])  # 重绑：历史不迁移
        after_a = [row["seq"] for row in h.store.history_page(s_a["id"], limit=50)["messages"]]
        after_b = h.store.history_page(s_b["id"], limit=50)["messages"]
        ok = (
            s_a["id"] == s_a2["id"]
            and s_a["id"] not in (s_b["id"], s_a_world2["id"], s_a_tl2["id"])
            and not leak
            and before == after_a
            and not any("A 的私聊" in json.dumps(row, ensure_ascii=False) for row in after_b)
        )
        ev = (f"同三元组复用 session={s_a['id']}；跨角色/跨世界/跨线各自独立 "
              f"({s_b['id']},{s_a_world2['id']},{s_a_tl2['id']})；B 的历史无 A 内容；"
              f"重绑后 A 历史 seq 不变 {before}，B 未继承 {len(after_b)} 条中的 A 消息")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 2


async def check_02(tmp: Path):
    """2. 重试 / 重发 / 崩溃恢复不重复生成、写记忆或消费素材。"""
    h = Harness(tmp, replies=["第一句回复。"])
    try:
        _p, info, timeline_id, cards = make_world(h)
        char = str(cards[0]["meta"]["card_id"])
        session = h.store.session_ensure(info["id"], timeline_id, char)
        channel, thread = bind(h, session["id"])
        token = thread["binding_token"]
        db_path = h.cfg.paths.db

        def tasks():
            return len(h.store.memory_tasks(info["id"], timeline_id))

        env1 = envelope(h, "在吗", thread_id="dm-1", token=token, env_id="e-1")
        first = await h.service.accept(channel_id=channel, thread_row=thread, env=env1)
        row = await wait_state(h.store, channel, "dm-1", "e-1")
        calls_after_first = len(h.llm.calls)
        tasks_after_first = tasks()

        # 同键同文重发 + 同键异文
        again = await h.service.accept(channel_id=channel, thread_row=thread, env=env1)
        conflict = None
        try:
            await h.service.accept(
                channel_id=channel, thread_row=thread,
                env=envelope(h, "换了内容", thread_id="dm-1", token=token, env_id="e-1"),
            )
        except UmpError as exc:
            conflict = exc.code
        # 投递失败 → 只重发已有产物，不重新生成
        h.deliver_ok = False
        h.sent.clear()
        resent = await h.service.resend_pending(channel, "dm-1")
        h.deliver_ok = True
        calls_after_dup = len(h.llm.calls)
        tasks_after_dup = tasks()
        calls_before_crash = len(h.llm.calls)

        # 崩溃恢复：一条真正没跑完的轮次（慢 LLM + 进程中断）→ interrupt_open_turns → 显式重试
        h.llm.delay_s = 5.0
        calls_before_interrupt = len(h.llm.calls)
        env2 = envelope(h, "第二问", thread_id="dm-1", token=token, env_id="e-2")
        await h.service.accept(channel_id=channel, thread_row=thread, env=env2)
        await asyncio.sleep(0.15)
        processing = h.store.inbound_find(channel, "dm-1", "e-2")["state"]
        await h.service.shutdown()          # 进程中断：在途任务被丢掉，库停在 processing
        keep = h.store
        keep.close()

        h.store = Store(db_path)            # 「重启」：新进程用同一库
        h.store.ensure_schema()
        recovered = h.store.interrupt_open_turns()
        h.llm.delay_s = 0.0
        h.llm.replies = ["恢复后的回复。"]
        h.service = SessionService(store=h.store, cfg=h.cfg, llm=h.llm, deliver=h._deliver, runtime=h.world)
        h.world.store = h.store
        retried = await h.service.retry(channel_id=channel, thread_id="dm-1", ref="e-2")
        row2 = await wait_state(h.store, channel, "dm-1", "e-2")
        this_turn = {"user:e-2", f"reply:{row2['reply_message_id'] or row2['message_id']}"}
        tasks_after_crash = [t for t in h.store.memory_tasks(info["id"], timeline_id)
                             if str(t["source_ref"]) in this_turn]
        fixed2 = h.store.outbound_by_message_id(row2["reply_message_id"] or row2["message_id"] or "")
        conditions = {
            "首次接受即 queued": first["state"] == "queued",
            "首次轮次固化": row["state"] == "done",
            "重发返回既有结果": again["state"] == "done" and again["message_id"] == row["message_id"],
            "同键异文被拒": conflict == ump.Err.CONFLICT,
            "重发不重新生成": calls_after_first == calls_after_dup == 1,
            "来源登记不重复": tasks_after_first == tasks_after_dup == 2,
            "投递失败只重发": resent == 1,
            "崩溃重试不重复生成": len(h.llm.calls) - calls_before_interrupt == 2,  # 一次被中断的尝试 + 一次恢复
            "崩溃前在途": processing == "processing",
            "启动标记中断": recovered == 1,
            "显式重试可恢复": retried["state"] in ("queued", "processing", "done"),
            "重试后固化": row2["state"] == "done" and fixed2 is not None and fixed2["state"] == "fixed",
            "重试不重复登记来源": len(tasks_after_crash) == 2,
        }
        ok = all(conditions.values())
        bad = [name for name, value in conditions.items() if not value]
        ev = (f"同键同文 → state={again['state']} 同 message_id={row['message_id']}；同键异文 → {conflict}；"
              f"生成次数 {calls_after_first}→{calls_after_dup}（重发 / 断线不触发生成，resend={resent}，"
              f"崩溃重试后 {len(h.llm.calls)} 次）；待提取来源 {tasks_after_first}→{tasks_after_dup} 条；"
              f"崩溃时 state={processing} → 启动标记中断 {recovered} 条 → 显式重试 {retried['state']} → "
              f"重试后 state={row2['state']}、该输入待提取来源 {len(tasks_after_crash)} 条、新增生成 1 次；"
              f"未满足项={bad or '无'}")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 3


async def check_03(tmp: Path):
    """3. 慢生成期间世界推进不被旧快照覆盖；冻结 / 回滚 / 删除 / 重绑后迟到结果不写入、不错投。"""
    findings: list[str] = []
    ok = True

    # (a) 生成期间推进世界：水位不被旧快照写回
    h = Harness(tmp / "a", replies=["慢回复。"])
    try:
        h.llm.delay_s = 0.35
        _p, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        char = str(cards[0]["meta"]["card_id"])
        session = h.store.session_ensure(info["id"], timeline_id, char)
        channel, thread = bind(h, session["id"], thread="dm-1")
        await h.service.accept(channel_id=channel, thread_row=thread,
                               env=envelope(h, "在吗", thread_id="dm-1", token=thread["binding_token"], env_id="e-1"))
        h.world.advance(info["id"], timeline_id, now_real=NOW + 3 * DAY)   # 生成期间世界前进
        moved = int(h.store.clock_get(timeline_id)["processed_world"])
        await wait_state(h.store, channel, "dm-1", "e-1")
        after = int(h.store.clock_get(timeline_id)["processed_world"])
        task_world = [t for t in h.store.memory_tasks(info["id"], timeline_id) if t["source_kind"] == "dialog"]
        ok = ok and after == moved and h.store.inbound_find(channel, "dm-1", "e-1")["state"] == "done"
        findings.append(f"(a) 生成中推进到 {moved}，轮次提交后水位仍是 {after}（回退={moved - after}）")
    finally:
        h.close()

    # (b) 重绑 → 迟到结果不写入、不投递
    h = Harness(tmp / "b", replies=["慢回复。"])
    try:
        h.llm.delay_s = 0.35
        _p, info, timeline_id, cards = make_world(h)
        char = str(cards[0]["meta"]["card_id"])
        session = h.store.session_ensure(info["id"], timeline_id, char)
        channel, thread = bind(h, session["id"], thread="dm-1")
        await h.service.accept(channel_id=channel, thread_row=thread,
                               env=envelope(h, "在吗", thread_id="dm-1", token=thread["binding_token"], env_id="e-1"))
        h.store.thread_bind(channel, "dm-1", session["id"])  # 重绑（新绑定版本）
        row = await wait_state(h.store, channel, "dm-1", "e-1")
        fresh = [m for m in outbound_rows(h.store, session["id"]) if m["reply_to"] == "e-1"]
        ok = ok and row["state"] == "cancelled" and not fresh
        findings.append(f"(b) 重绑后迟到轮次 state={row['state']}，新增回复 {len(fresh)} 条")
    finally:
        h.close()

    # (c) 回滚 → 迟到结果不写入
    h = Harness(tmp / "c", replies=["慢回复。"])
    try:
        h.llm.delay_s = 0.35
        _p, info, timeline_id, cards = make_world(h)
        char = str(cards[0]["meta"]["card_id"])
        point = h.world.commit(info["id"], timeline_id, note="回滚点")
        session = h.store.session_ensure(info["id"], timeline_id, char)
        channel, thread = bind(h, session["id"], thread="dm-1")
        await h.service.accept(channel_id=channel, thread_row=thread,
                               env=envelope(h, "在吗", thread_id="dm-1", token=thread["binding_token"], env_id="e-1"))
        result = h.world.rollback(info["id"], timeline_id, commit_id=point["id"], now_real=NOW + 60)
        row = await wait_state(h.store, channel, "dm-1", "e-1")
        fresh = [m for m in outbound_rows(h.store, session["id"]) if m["reply_to"] == "e-1"]
        # 回滚会清走被截去的对话行：输入行消失也算「没写回」
        state_c = row["state"] if row else "行已随回滚清除"
        ok = ok and not fresh and (row is None or row["state"] in ("cancelled", "done"))
        findings.append(f"(c) 回滚作废 {result['voided_inputs']} 条飞行输入，迟到轮次 state={state_c}，"
                        f"新增回复 {len(fresh)} 条")
    finally:
        h.close()

    # (d) 冻结 → 迟到结果
    h = Harness(tmp / "d", replies=["慢回复。"])
    try:
        h.llm.delay_s = 0.35
        _p, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        char = str(cards[0]["meta"]["card_id"])
        session = h.store.session_ensure(info["id"], timeline_id, char)
        channel, thread = bind(h, session["id"], thread="dm-1")
        await h.service.accept(channel_id=channel, thread_row=thread,
                               env=envelope(h, "在吗", thread_id="dm-1", token=thread["binding_token"], env_id="e-1"))
        in_flight = h.store.inbound_find(channel, "dm-1", "e-1")["state"]
        h.world.freeze(info["id"], timeline_id, now_real=NOW + 1)
        row = await wait_state(h.store, channel, "dm-1", "e-1")
        fresh = [m for m in outbound_rows(h.store, session["id"]) if m["reply_to"] == "e-1"]
        ok = ok and row["state"] == "cancelled" and not fresh
        findings.append(f"(d) 冻结时该轮次 {in_flight}，冻结后迟到轮次 state={row['state']}，写入回复 {len(fresh)} 条，"
                        f"投递 {len([s for s in h.sent if s['type'] == 'reply'])} 条")
    finally:
        h.close()

    # (f) 冻结线收到新入站（§2.2：发新消息前须明确激活）
    h = Harness(tmp / "f", replies=["冻结线的回复。"])
    try:
        _p, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.freeze(info["id"], timeline_id, now_real=NOW + 1)
        char = str(cards[0]["meta"]["card_id"])
        session = h.store.session_ensure(info["id"], timeline_id, char)
        channel, thread = bind(h, session["id"], thread="dm-1")
        await h.service.accept(channel_id=channel, thread_row=thread,
                               env=envelope(h, "在吗", thread_id="dm-1", token=thread["binding_token"], env_id="e-1"))
        frozen_row = await wait_state(h.store, channel, "dm-1", "e-1")
        frozen_replies = [m for m in outbound_rows(h.store, session["id"]) if m["reply_to"] == "e-1"]
        ok = ok and (frozen_row is None or frozen_row["state"] == "rejected") and not frozen_replies
        findings.append(f"(f) 向冻结线发入站 → state={frozen_row['state'] if frozen_row else None}、"
                        f"新回复 {len(frozen_replies)} 条")
    finally:
        h.close()

    # (e) 删除线 → 迟到结果
    h = Harness(tmp / "e", replies=["慢回复。"])
    try:
        h.llm.delay_s = 0.35
        _p, info, timeline_id, cards = make_world(h)
        char = str(cards[0]["meta"]["card_id"])
        session = h.store.session_ensure(info["id"], timeline_id, char)
        channel, thread = bind(h, session["id"], thread="dm-1")
        await h.service.accept(channel_id=channel, thread_row=thread,
                               env=envelope(h, "在吗", thread_id="dm-1", token=thread["binding_token"], env_id="e-1"))
        task = list(h.service._tasks)[-1]
        h.store.timeline_delete(info["id"], timeline_id)
        row = await wait_state(h.store, channel, "dm-1", "e-1")
        await asyncio.sleep(0.6)   # 让在途生成真正跑完（慢 LLM 0.35s），再看迟到结果是否写回
        orphan = h.store._conn.execute(
            "SELECT COUNT(*) FROM message WHERE session_id=? AND role!='user'", (session["id"],)
        ).fetchone()[0]
        delivered = len([s for s in h.sent if s["type"] == "reply"])
        raised = "None"
        if task.done() and task.exception() is not None:
            raised = type(task.exception()).__name__
        ok = ok and (row is None or row["state"] in ("cancelled", "failed")) and orphan == 0 and delivered == 0
        findings.append(f"(e) 删除线后迟到轮次 state={row['state'] if row else None}，"
                        f"孤儿回复 {orphan} 条，投递 {delivered} 条，任务异常={raised}")
    finally:
        h.close()

    return ("PASS" if ok else "FAIL"), "；".join(findings)


# ---------------------------------------------------------------- 条目 4


async def check_04(tmp: Path):
    """4. 生成上下文不含不可见实情；有依据的传闻与明确推测不被强行改成全知真相。"""
    h = Harness(tmp)
    try:
        package, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 3 * DAY)
        char = str(cards[0]["meta"]["card_id"])
        moment = int(h.store.clock_get(timeline_id)["processed_world"])
        prompt = prompt_of(h, info, timeline_id, char, topic="告警")
        creator = str((cards[0].get("background") or {}).get("creator") or "")
        unknown_truth = unknown_truth_details(h, info, timeline_id, char, moment=moment)
        legit = known_texts(h, info, timeline_id, char, moment=moment, card=cards[0])
        leaked = [text for text in unknown_truth if text and text in prompt and text not in legit]
        collision = [text for text in unknown_truth if text and text in prompt and text in legit]
        rumor_marked = "｜" in prompt and ("听人说的" in prompt or "只是读到过" in prompt or "将信将疑" in prompt)
        ok = bool(creator) and creator not in prompt and not leaked and rumor_marked
        ev = (f"creator 幕后设定（{creator[:16]}…）{'未' if creator not in prompt else '已'}进上下文；"
              f"未获知事件的实情文本 {len(unknown_truth)} 条，真泄漏 {len(leaked)} 条"
              f"（另 {len(collision)} 条是不同事件同文碰撞，本就在她已知的条目里）；"
              f"说法带来源与确信标签={rumor_marked}")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 5


async def check_05(tmp: Path):
    """5. 对话促成的行动只有经规则校验才产生世界效果；普通文本不创建管理性时间线。"""
    h = Harness(tmp, replies=["我这就去把通行牌补上，顺便把堤志补完。"])
    try:
        _p, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 2 * DAY)
        char = str(cards[0]["meta"]["card_id"])
        session = h.store.session_ensure(info["id"], timeline_id, char)
        channel, thread = bind(h, session["id"], thread="dm-1")

        events_before = len(h.store.event_window(info["id"], timeline_id, until=10**12, limit=400))
        timelines_before = len(h.store.timeline_list(info["id"]))
        intents_before = len(h.store.intent_list(info["id"], timeline_id, char))
        instances_before = len(h.store.instance_list())

        await h.service.accept(channel_id=channel, thread_row=thread,
                               env=envelope(h, "你去把通行牌补上吧", thread_id="dm-1",
                                            token=thread["binding_token"], env_id="e-1"))
        row = await wait_state(h.store, channel, "dm-1", "e-1")

        events_after = len(h.store.event_window(info["id"], timeline_id, until=10**12, limit=400))
        timelines_after = len(h.store.timeline_list(info["id"]))
        intents_after = len(h.store.intent_list(info["id"], timeline_id, char))
        instances_after = len(h.store.instance_list())
        # 无受支持效果 → 只保留为意愿（纯函数层）
        rows = [{"id": "in-x", "stage": "adopted", "preconditions": "[]", "effect": "{}",
                 "window_from": 0, "window_to": 10**12}]
        decided = intents_mod.decide(rows[0], world_seconds=100, events_present=set(), active_effects=[])
        ok = (row["state"] == "done" and events_after == events_before and timelines_after == timelines_before
              and intents_after == intents_before and instances_after == instances_before
              and decided == "keep")
        ev = (f"一轮声称已去办事的回复固化后：事件 {events_before}→{events_after}，线 {timelines_before}→"
              f"{timelines_after}，实例 {instances_before}→{instances_after}，打算 {intents_before}→{intents_after}；"
              f"无受支持效果的打算 decide={decided}（不进事件）")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 6


async def check_06(tmp: Path):
    """6. 多 thread 共享主动配额且只向选定目标投递；离线与高倍率不补发历史洪峰。"""
    h = Harness(tmp)
    try:
        _p, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.set_rate(info["id"], timeline_id, rate=86400, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 5)   # 世界时间推进数日
        char = str(cards[0]["meta"]["card_id"])
        session = h.store.session_ensure(info["id"], timeline_id, char)
        bind(h, session["id"], thread="dm-1")
        days = int(h.store.clock_get(timeline_id)["processed_world"]) / DAY
        proactive = h.store._conn.execute(
            "SELECT COUNT(*) FROM message WHERE role IN ('character','notice') AND reply_to IS NULL"
        ).fetchone()[0]
        tables_now = tables(h.store)
        code = source_scan("proactive", "主动消息", "每日额度", "quota")
        task_names = source_scan("proactive_text")
        ok = False   # 未实现即 FAIL
        ev = (f"世界推进 {days:.1f} 日、无人说话时主动消息 0 条（reply_to IS NULL 计数={proactive}、"
              f"LLM 调用 {len(h.llm.calls)} 次）；库内无配额 / 投递目标 / 初见表"
              f"（{sorted(t for t in tables_now if 'quota' in t or 'proactive' in t or 'opening' in t)}）；"
              f"主动发言代码路径只出现在优先级清单 {task_names.get('proactive_text')}")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 7


async def check_07(tmp: Path):
    """7. 披露严格限于明确来源与接收角色；回滚撤销派生认知，从旧提交分叉不假称原线已撤回。"""
    h = Harness(tmp)
    try:
        package, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 2 * DAY)
        first = str(cards[0]["meta"]["card_id"])
        second_card = example_card(package, name="堤砚")
        h.world.add_character(info["id"], timeline_id, second_card, now_real=NOW + 2 * DAY,
                              joined_world=int(h.store.clock_get(timeline_id)["processed_world"]))
        second = str(second_card["meta"]["card_id"])
        third_card = example_card(package, name="第三个人")
        h.world.add_character(info["id"], timeline_id, third_card, now_real=NOW + 2 * DAY,
                              joined_world=int(h.store.clock_get(timeline_id)["processed_world"]))
        third = str(third_card["meta"]["card_id"])
        point = h.world.commit(info["id"], timeline_id, note="披露前")
        reply_id, _ = say(h.store, info, timeline_id, first, env="env-a", text="堤上的事",
                          reply="信报上抄到堤长身故，别的先别外传")
        before_grant = prompt_of(h, info, timeline_id, second, topic="堤长")
        grant = h.world.disclose(info["id"], timeline_id, from_character=first, to_character=second,
                                 refs=[reply_id], note="让堤砚知道")
        after_grant = prompt_of(h, info, timeline_id, second, topic="堤长")
        third_prompt = prompt_of(h, info, timeline_id, third, topic="堤长")
        first_prompt = prompt_of(h, info, timeline_id, first, topic="堤长")
        rejects = []
        for refs in ([], ["m-不存在"]):
            try:
                h.world.disclose(info["id"], timeline_id, from_character=first, to_character=second, refs=refs)
            except (RuntimeStateError, ValueError) as exc:
                rejects.append(str(exc)[:24])
        # 回滚撤销授权与派生；分叉保留原线
        h.store.memory_add({
            "id": "mm-b", "instance_id": info["id"], "timeline_id": timeline_id, "character_id": second,
            "text": "联络者转述了堤禾说过的话：信报上抄到堤长身故", "kind": "fragment",
            "sources": [{"kind": "dialog", "ref": reply_id, "source_role": "other_character"}],
            "happened_world": 0, "learned_world": 0, "recorded_world": 0, "semantic_watermark": 0,
            "strength": 0.6, "confidence": 0.8,
        })
        h.world.rollback(info["id"], timeline_id, commit_id=point["id"], now_real=NOW + 4 * DAY)
        after_rollback = h.world.disclosures(info["id"], timeline_id)
        memory_gone = h.store.memory_get("mm-b", instance_id=info["id"], timeline_id=timeline_id) is None
        rolled_prompt = prompt_of(h, info, timeline_id, second, topic="堤长")

        point2 = h.world.commit(info["id"], timeline_id, note="再披露点")
        reply2, _ = say(h.store, info, timeline_id, first, env="env-a2", text="问", reply="再答一次")
        h.world.disclose(info["id"], timeline_id, from_character=first, to_character=second, refs=[reply2])
        branch = h.world.fork(info["id"], timeline_id, commit_id=point2["id"], name="旧提交分支")
        original_kept = len(h.world.disclosures(info["id"], timeline_id)) == 1
        branch_empty = h.world.disclosures(info["id"], branch["timeline"]["id"]) == []

        ok = ("披露块" not in before_grant and "堤长身故" in after_grant and "转述" in after_grant
              and "堤长身故" not in third_prompt and "堤长身故" not in first_prompt
              and len(rejects) == 2 and after_rollback == [] and memory_gone
              and "堤长身故" not in rolled_prompt and original_kept and branch_empty)
        ev = (f"授权前 B 上下文无披露块，授权后出现「转述」（grantWorld={grant['granted_world']}）；"
              f"第三人/来源角色不可见；含糊与不存在引用 {rejects}；回滚后授权 {len(after_rollback)} 条、"
              f"派生记忆撤销={memory_gone}；分叉后原线保留={original_kept}、分支为空={branch_empty}")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 8


async def check_08(tmp: Path):
    """8. 关闭自动提交后重启仍能恢复历史；回滚后的旧分页游标不继续展示已撤销内容。"""
    h = Harness(tmp, replies=["存下来的回复。"])
    try:
        _p, info, timeline_id, cards = make_world(h)
        char = str(cards[0]["meta"]["card_id"])
        session = h.store.session_ensure(info["id"], timeline_id, char)
        channel, thread = bind(h, session["id"], thread="dm-1")
        for index in range(3):
            env_id = f"e-{index}"
            await h.service.accept(channel_id=channel, thread_row=thread,
                                   env=envelope(h, f"第{index}问", thread_id="dm-1",
                                                token=thread["binding_token"], env_id=env_id))
            await wait_state(h.store, channel, "dm-1", env_id)
        autocommit = h.world.autocommit_enabled
        commits = len(h.store.commit_list(info["id"], timeline_id))
        page = h.store.history_page(session["id"], limit=2)
        cursor = page["next_before_seq"]
        texts_before = [row["text"] for row in h.store.history_page(session["id"], limit=50)["messages"]]
        h.store.close()

        # 重启：同一库重开（不新建核心进程）
        reopened = Store(h.cfg.paths.db)
        reopened.ensure_schema()
        h.store = reopened                      # 探针后续与收尾都用新句柄
        texts_after = [row["text"] for row in reopened.history_page(session["id"], limit=50)["messages"]]
        # 回滚后的旧游标：走完全部分页也不该出现被撤销的内容
        world2 = RuntimeService(reopened, autocommit_enabled=False)
        point = world2.commit(info["id"], timeline_id, note="回滚点")
        say(reopened, info, timeline_id, char, env="env-revoked", text="撤销前的问", reply="撤销前的答")
        world2.rollback(info["id"], timeline_id, commit_id=point["id"], now_real=NOW + 90)
        # 从表头翻页：不该出现被撤销的内容
        full: list[dict] = []
        seq_cursor = None
        for _ in range(6):
            got = reopened.history_page(session["id"], limit=2, before_seq=seq_cursor)
            full.extend(got["messages"])
            if not got["has_more"] or got["next_before_seq"] is None:
                break
            seq_cursor = got["next_before_seq"]
        head_leak = [row for row in full if "撤销前的答" in json.dumps(row, ensure_ascii=False)]
        # 旧游标（回滚前取的）继续翻页
        stale = []
        before = cursor
        for _ in range(6):
            got = reopened.history_page(session["id"], limit=2, before_seq=before)
            stale.extend(got["messages"])
            if not got["has_more"] or got["next_before_seq"] is None:
                break
            before = got["next_before_seq"]
        revoke_leak = [row for row in stale if "撤销前的答" in json.dumps(row, ensure_ascii=False)]
        ok = (autocommit is False and commits == 1 and texts_after == texts_before
              and not revoke_leak and not head_leak and len(full) == len(texts_before))
        ev = (f"自动提交关闭（提交只有创建期 1 个）时重开库：历史 {len(texts_before)} 条逐字一致="
              f"{texts_after == texts_before} ✓；但回滚到本线提交点后对话只剩 {len(full)} 条（回滚点应有 "
              f"{len(texts_before)} 条）——快照里有 dialog 段，回滚只写回 runtime 段（service.py:596），"
              f"正文 §三「回滚恢复目标提交的对话」未落实；旧游标翻页 {len(stale)} 条、已撤销内容 "
              f"{len(revoke_leak)} 条")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 9


async def check_09(tmp: Path):
    """9. 角色归档后：历史可查、未固化任务失效、最多一次合法最后联络 / 归档说明、入站明确拒绝。"""
    h = Harness(tmp, replies=["我这就去堤上看看。"])
    try:
        package = example_package("灰潮纪寿终审计", moment=DAY * 1500)
        card = example_card(package)
        card["identity"]["died"] = DAY * 1502       # 固化死亡：寿终时刻落在推进范围内
        info = create_instance(h.store, package, [card])
        timeline_id = h.store.timeline_list(info["id"])[0]["id"]
        h.world.ensure_instance(info["id"], now_real=NOW)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 4 * DAY)
        char = str(card["meta"]["card_id"])
        deaths = [row for row in h.store.event_window(info["id"], timeline_id, until=10**12, limit=300)
                  if str(row["template"]).startswith("death:")]
        session = h.store.session_ensure(info["id"], timeline_id, char)
        channel, thread = bind(h, session["id"], thread="dm-1")
        await h.service.accept(channel_id=channel, thread_row=thread,
                               env=envelope(h, "你还好吗", thread_id="dm-1",
                                            token=thread["binding_token"], env_id="e-after-death"))
        row = await wait_state(h.store, channel, "dm-1", "e-after-death")
        replies = [m for m in outbound_rows(h.store, session["id"]) if m["reply_to"] == "e-after-death"]
        notices = h.store._conn.execute("SELECT COUNT(*) FROM message WHERE role='notice'").fetchone()[0]
        code = source_scan("system_notice", "role=\"notice\"", "归档", "archived")
        ok = (len(deaths) == 1 and row["state"] == "rejected")
        ev = (f"寿终事件 {len(deaths)} 条（{deaths[0]['world_seconds'] if deaths else '-'}）；"
              f"寿终后入站 state={row['state']}、新回复 {len(replies)} 条、归档说明 {notices} 条"
              f"（system_notice 生产代码只出现在 {code.get('system_notice')}）")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 10


async def check_10(tmp: Path):
    """10. 初见：多窗口只生成一次；用户抢先输入不双发；失败可重试；已固化未送达不重生成；回滚不复活。"""
    h = Harness(tmp)
    try:
        _p, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 2 * DAY)
        char = str(cards[0]["meta"]["card_id"])
        views = [h.store.session_ensure(info["id"], timeline_id, char) for _ in range(3)]
        h.world.advance(info["id"], timeline_id, now_real=NOW + 3 * DAY)
        openings = h.store._conn.execute(
            "SELECT COUNT(*) FROM message WHERE reply_to IS NULL AND role IN ('character','notice')"
        ).fetchone()[0]
        has_state = any("opening" in name or "first" in name for name in tables(h.store))
        code = source_scan("初见", "first_contact", "开场")
        ok = False
        ev = (f"三窗口打开同一会话（session={views[0]['id']}）后独立开场 {openings} 条、LLM 调用 {len(h.llm.calls)} 次；"
              f"库内无初见 / 开场状态表={has_state}；first_contact 代码只出现在 {code.get('first_contact')}"
              f"（角色卡字段与扮演定义，无开场生成 / 资格消费）")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 11


async def check_11(tmp: Path):
    """11. 睡眠等待：连续输入不延长等待；不同 thread 不合并或错投；批内每条输入都查到同批结果；不误称仍睡。"""
    h = Harness(tmp, replies=["嗯……（迷糊）"], )
    try:
        _p, info, timeline_id, cards = make_world(h)   # 初始水位 offset=0 → 睡眠块内（0..25200）
        char = str(cards[0]["meta"]["card_id"])
        session = h.store.session_ensure(info["id"], timeline_id, char)
        channel, thread = bind(h, session["id"], thread="dm-1")
        started = time.monotonic()
        for index in range(2):
            await h.service.accept(channel_id=channel, thread_row=thread,
                                   env=envelope(h, f"第{index}条", thread_id="dm-1",
                                                token=thread["binding_token"], env_id=f"e-{index}"))
            await wait_state(h.store, channel, "dm-1", f"e-{index}")
        after = time.monotonic() - started
        messages = h.store.history_page(session["id"], limit=50)["messages"]
        replies = [m for m in messages if m["role"] != "user"]
        covers = {m["message_id"]: (m["reply_to"] or "-") for m in replies}
        snapshot = h.world.character_snapshot(info["id"], timeline_id, char, world_seconds=int(
            h.store.clock_get(timeline_id)["processed_world"]))
        wait_cfg = source_scan("sleep_wait", "wait_seconds", "batch_deadline", "合并批次", "多入一回")
        merged = len(replies) == 1 and all(m["reply_to"] != "e-0" for m in replies)
        ok = merged
        ev = (f"该水位活动=「{snapshot['current_activity']}」（睡眠块内）连续两条输入 → {len(replies)} 条各自独立的回复"
              f"（reply_to={sorted(covers.values())}），耗时 {after:.2f}s 无等待窗；"
              f"同批共享 message_id={len(set(m['message_id'] for m in replies)) == 1}；"
              f"等待 / 合并代码路径 {list(wait_cfg)}")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 12


async def check_12(tmp: Path):
    """12. 角色不愿讲的已知内容在反复追问下保持既有取舍。"""
    h = Harness(tmp)
    try:
        _p, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 2 * DAY)
        char = str(cards[0]["meta"]["card_id"])
        prompts = [prompt_of(h, info, timeline_id, char, topic="堤长 粮 名册") for _ in range(3)]
        state_holder = any(k in " ".join(tables(h.store)) for k in ("refusal", "secret", "tone", "tradeoff"))
        code = source_scan("表达取舍", "不愿说", "拒绝", "refusal")
        ok = False
        ev = (f"同一追问三次得到逐字相同的上下文（{len(set(prompts))} 种），库内无取舍 / 口风状态={state_holder}，"
              f"表达取舍代码命中 {list(code)}；本 SPEC §十把「表达取舍的依据与稳定性」列为待模块设计项（残余）")
        return ("DEFERRED", ev)
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 13


async def check_13(tmp: Path):
    """13. 相关新经历与早先对话有合法共同指涉时可自然回接；换话题后不出现任务催办、进度或补偿性线索。"""
    h = Harness(tmp)
    try:
        _p, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 2 * DAY)
        char = str(cards[0]["meta"]["card_id"])
        say(h.store, info, timeline_id, char, env="env-m", text="堤志补到哪了",
            reply="我把春汛通行牌的延误抄进了抄存，等信报来对一遍")
        h.store.memory_add({
            "id": "mm-1", "instance_id": info["id"], "timeline_id": timeline_id, "character_id": char,
            "text": "她记着自己经手过春汛通行牌的发放延误", "kind": "fact",
            "sources": [{"kind": "dialog", "ref": "env-m"}], "happened_world": 0, "learned_world": 0,
            "recorded_world": 0, "semantic_watermark": 0, "strength": 0.9, "confidence": 0.9,
        })
        back = h.world.recall(info["id"], timeline_id, char, topic="春汛 通行牌")
        hit = [item for item in back["entries"] if "春汛通行牌" in str(item.get("text") or "")]
        off = prompt_of(h, info, timeline_id, char, topic="今天天气")
        nag = [word for word in ("待办", "催办", "进度", "已完成任务", "补偿") if word in off]
        ok = bool(hit) and not nag
        ev = (f"共同指涉召回命中 {len(hit)} 条（最近一条来源={json.loads(str(hit[0]['sources']))[0]['kind'] if hit else '-'}）；"
              f"换话题后的上下文含催办 / 进度词 {nag or '无'}")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 14


async def check_14(tmp: Path):
    """14. 角色自己的打算在话题切换与重启后延续；未实际执行前不产生「已做过」的表述。"""
    h = Harness(tmp)
    try:
        package = example_package("灰潮纪打算审计", moment=DAY * 1500)
        card = example_card(package)
        char = str(card["meta"]["card_id"])
        card["intents"] = [{
            "id": "in-1", "object": "把今年春汛的通行牌发放延误记进抄存，等信报来对一遍",
            "basis": "她自己经手的通行牌与信使交接记录", "strength": 0.7,
            "window": {"from": DAY * 1560, "to": DAY * 1590},   # 目标窗口远在推进范围之外
            "preconditions": ["cf-1"], "effect": {"kind": "public_notice", "target": "src-1", "expiry": "with_cause"},
        }]
        info = create_instance(h.store, package, [card])
        timeline_id = h.store.timeline_list(info["id"])[0]["id"]
        h.world.ensure_instance(info["id"], now_real=NOW)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 3 * DAY)   # 窗口未到：打算只能停在原位
        rows_before = h.store.intent_list(info["id"], timeline_id, char)
        prompt_before = prompt_of(h, info, timeline_id, char, topic="通行牌")
        events_before = h.store.event_window(info["id"], timeline_id, until=10**12, limit=300)
        # 重启：同一库上换一个运行层实例（进程中断恢复的等价形态）
        h.store.close()
        reopened = Store(h.cfg.paths.db)
        reopened.ensure_schema()
        world2 = RuntimeService(reopened, autocommit_enabled=False)
        h.store = reopened
        rows_after = reopened.intent_list(info["id"], timeline_id, char)
        session = reopened.session_ensure(info["id"], timeline_id, char)
        prompt_after = world2.turn_context(session, topic="通行牌")["prompt"]
        events = reopened.event_window(info["id"], timeline_id, until=10**12, limit=300)
        idle_events = [ev["id"] for ev in events if str(ev["template"]) in {str(r["id"]) for r in rows_after}]
        claim_words = [word for word in ("已做过", "已经做过", "已办完") if word in prompt_after]
        stated = str(rows_after[0]["stage"]) if rows_after else ""
        ok = (bool(rows_before) and len(rows_after) == len(rows_before) and stated in ("adopted", "waiting", "deferred")
              and str(rows_after[0]["object"]) in prompt_after
              and str(rows_after[0]["object"]) in prompt_before and not claim_words
              and not idle_events and len(events) == len(events_before))
        ev = (f"打算 {len(rows_before)} 条（stage={rows_before[0]['stage']}，窗口 DAY*1560 未到）→ 推进 3 日 + 重启后 "
              f"{len(rows_after)} 条（stage={stated}）；上下文话题切换到通行牌仍带「"
              f"{str(rows_after[0]['object'])[:16]}…」且只写成打算；未执行前无「已做过」说法={not claim_words}、"
              f"无对应行动事件（{len(idle_events)} 条，事件总数 {len(events)} 未变）")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 15


async def check_15(tmp: Path):
    """15. 上下文同时带入实际活动、有效后果、合法环境投影与可知经历；计划 / 未获知事件 / 实情不进上下文。"""
    h = Harness(tmp)
    try:
        _p, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 3 * DAY)
        char = str(cards[0]["meta"]["card_id"])
        moment = int(h.store.clock_get(timeline_id)["processed_world"])
        snapshot = h.world.character_snapshot(info["id"], timeline_id, char, world_seconds=moment)
        prompt = prompt_of(h, info, timeline_id, char, topic="潮位")
        activity = str(snapshot["current_activity"] or "")
        experiences = [str(row["summary"] or "") for row in snapshot["experiences"]] + \
                      [str(row["text"] or "") for row in snapshot["knowledge"]]
        observations = [f"{row['name']}：{row['value']}{row['unit']}" for row in snapshot["observations"]]
        effect_note = "受影响的后果" in prompt
        creator = str((cards[0].get("background") or {}).get("creator") or "")
        future = [str(row["detail"] or "") for row in h.store.event_window(
            info["id"], timeline_id, until=10**12, limit=300) if int(row["world_seconds"]) > moment]
        legit = known_texts(h, info, timeline_id, char, moment=moment, card=cards[0])
        unknown = unknown_truth_details(h, info, timeline_id, char, moment=moment)
        leaked_future = [text for text in future if text and text in prompt]
        leaked_unknown = [text for text in unknown if text and text in prompt and text not in legit]
        effects = list(snapshot["effects"])
        # 有效后果场景：用户引入一条指向她本人的活动受限，看上下文的活动是否带上后果
        draft = await h.world.draft_user_event(
            info["id"], timeline_id, intent="让堤务吏这一段只能守在滩口",
            payload={
                "when": "now",
                "effects": [{"kind": "activity_constraint", "target": char, "expiry": "until_cleared"}],
                "claims": [{"text": "堤务吏被限在滩口值守", "source_id": "src-1", "audience": "公开"}],
            },
            now_real=NOW + 3 * DAY,
        )
        injected = h.world.confirm_user_event(info["id"], str(draft["draft"]["draft_id"]), now_real=NOW + 3 * DAY)
        new_line = str(injected["timeline_id"])
        moment_eff = int(h.store.clock_get(new_line)["processed_world"])
        snapshot_eff = h.world.character_snapshot(info["id"], new_line, char, world_seconds=moment_eff)
        prompt_eff = h.world.turn_context(
            {"instance_id": info["id"], "timeline_id": new_line, "character_id": char}, topic="今天做什么"
        )["prompt"]
        effect_note_eff = "受影响的后果" in prompt_eff
        effects_eff = list(snapshot_eff["effects"])
        ok = (bool(activity) and activity in prompt and any(x and x in prompt for x in experiences)
              and (not observations or all(item in prompt for item in observations))
              and creator not in prompt and not leaked_future and not leaked_unknown
              and (not effects or effect_note) and (not effects_eff or effect_note_eff))
        ev = (f"活动「{activity[:20]}」进上下文={activity in prompt}，经历/知识条目进上下文="
              f"{any(x and x in prompt for x in experiences)}，环境投影 {len(observations)} 条进上下文="
              f"{all(item in prompt for item in observations)}，有效后果 {len(effects)} 条、"
              f"后果注释={effect_note}（注入活动受限后 {len(effects_eff)} 条有效后果、上下文注释="
              f"{effect_note_eff}，活动=「{str(snapshot_eff['current_activity'])[:24]}」）；"
              f"未来事件泄漏 {len(leaked_future)} 条、未获知实情泄漏 "
              f"{len(leaked_unknown)} 条（同文碰撞已排除）；creator 泄漏="
              f"{creator in prompt}")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 16


async def check_16(tmp: Path):
    """16. 自然开场：有近期合法素材可自然回接；只有角色未知事件或无素材时不泄漏 / 不补造。"""
    h = Harness(tmp)
    try:
        # (1) 有近期已知经历
        _p, info, timeline_id, cards = make_world(h, name="灰潮纪开场一")
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 3 * DAY)
        char = str(cards[0]["meta"]["card_id"])
        moment = int(h.store.clock_get(timeline_id)["processed_world"])
        rich = prompt_of(h, info, timeline_id, char, topic="今天怎么样")
        snapshot = h.world.character_snapshot(info["id"], timeline_id, char, world_seconds=moment)
        material = [str(row["summary"] or "") for row in snapshot["experiences"][:4]]
        has_material = any(text and text in rich for text in material)

        # (2) 只有角色未知事件
        legit = known_texts(h, info, timeline_id, char, moment=moment, card=cards[0])
        unknown_done = unknown_truth_details(h, info, timeline_id, char, moment=moment)
        no_leak = not [text for text in unknown_done if text and text in rich and text not in legit]

        # (3) 无可用素材：未推进的实例
        _p2, info2, tl2, cards2 = make_world(h, name="灰潮纪开场二")
        char2 = str(cards2[0]["meta"]["card_id"])
        bare = prompt_of(h, info2, tl2, char2, topic="今天怎么样")
        events2 = h.store.event_window(info2["id"], tl2, until=10**12, limit=10)
        moment2 = int(h.store.clock_get(tl2)["processed_world"])
        legit2 = known_texts(h, info2, tl2, char2, moment=moment2, card=cards2[0])
        bare_leak = [text for text in unknown_truth_details(h, info2, tl2, char2, moment=moment2)
                     if text in bare and text not in legit2]
        ok = has_material and no_leak and not bare_leak
        ev = (f"(a) 近期已知经历 {len(material)} 条进上下文={has_material}；"
              f"(b) 未获知事件实情 {len(unknown_done)} 条、泄漏 {0 if no_leak else 1}；"
              f"(c) 未推进实例（库内事件 {len(events2)} 条）上下文不含任何事件实情，已补造事件 {len(bare_leak)} 条")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 17


async def check_17(tmp: Path):
    """17. 一条跨水位的角色故事单元能表达目标、阻碍、选择、代价与局部终态；终态不关闭世界、不泄漏未知真相。"""
    h = Harness(tmp)
    try:
        package = example_package("灰潮纪故事审计", moment=DAY * 1500)
        card = example_card(package)
        char = str(card["meta"]["card_id"])
        card["intents"] = [{
            "id": "in-story", "object": "把春汛通行牌的延误记进抄存并等信报对一遍",
            "basis": "她自己经手的通行牌与信使交接记录", "strength": 0.7,
            "window": {"from": DAY * 1501, "to": DAY * 1503},
            "preconditions": ["cf-1"], "effect": {"kind": "public_notice", "target": "src-1", "expiry": "with_cause"},
        }]
        info = create_instance(h.store, package, [card])
        timeline_id = h.store.timeline_list(info["id"])[0]["id"]
        h.world.ensure_instance(info["id"], now_real=NOW)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 6 * DAY)
        rows = h.store.intent_list(info["id"], timeline_id, char)
        events = h.store.event_window(info["id"], timeline_id, until=10**12, limit=300)
        units = intents_mod.story_units(rows, events)
        events_at_story_end = len(events)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 9 * DAY)
        events_later = len(h.store.event_window(info["id"], timeline_id, until=10**12, limit=600))
        creator = str((card.get("background") or {}).get("creator") or "")
        unit = units[0] if units else {}
        terminal = str(unit.get("terminal") or "")
        ok = (bool(units) and bool(unit.get("object")) and bool(unit.get("basis"))
              and terminal in ("达成", "延期", "放弃", "持续中")
              and events_later > events_at_story_end and creator not in json.dumps(units, ensure_ascii=False))
        ev = (f"故事单元 terminal={terminal}、阻碍记录「{str(unit.get('obstacles'))[:18]}」、"
              f"关联事件 {len(unit.get('events') or [])} 条、unresolved={unit.get('unresolved')}；"
              f"终态后世界继续产生事件 {events_at_story_end}→{events_later}；单元视图不含 creator 幕后实情")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


# ---------------------------------------------------------------- 条目 18


async def check_18(tmp: Path):
    """18. 披露确认先于读取；授权失败本轮不用新权限；回滚撤销授权及派生回复 / 记忆。"""
    h = Harness(tmp)
    try:
        package, info, timeline_id, cards = make_world(h)
        h.world.activate(info["id"], timeline_id, now_real=NOW)
        h.world.advance(info["id"], timeline_id, now_real=NOW + 2 * DAY)
        first = str(cards[0]["meta"]["card_id"])
        second_card = example_card(package, name="堤砚")
        h.world.add_character(info["id"], timeline_id, second_card, now_real=NOW + 2 * DAY,
                              joined_world=int(h.store.clock_get(timeline_id)["processed_world"]))
        second = str(second_card["meta"]["card_id"])
        point = h.world.commit(info["id"], timeline_id, note="披露前")
        reply_id, second_session = say(h.store, info, timeline_id, first, env="env-a", text="堤上的事",
                                       reply="信报上抄到堤长身故")

        # 失败确认：不留下半授权，B 本轮继续按旧权限
        failed = None
        try:
            h.world.disclose(info["id"], timeline_id, from_character=first, to_character=second,
                             refs=["m-不存在"])
        except RuntimeStateError as exc:
            failed = str(exc)[:20]
        grants_after_failure = h.world.disclosures(info["id"], timeline_id)
        prompt_after_failure = prompt_of(h, info, timeline_id, second, topic="堤长")

        # 确认成功：授权先落库，随后才可读
        h.world.disclose(info["id"], timeline_id, from_character=first, to_character=second, refs=[reply_id])
        grants = h.world.disclosures(info["id"], timeline_id, to_character=second)
        prompt_after_grant = prompt_of(h, info, timeline_id, second, topic="堤长")

        # 派生：B 用过授权的一轮回复 + 派生记忆
        derived = say(h.store, info, timeline_id, second, env="env-b", text="他跟你说了什么",
                      reply="联络者转述过：信报上抄到堤长身故")
        h.store.memory_add({
            "id": "mm-b2", "instance_id": info["id"], "timeline_id": timeline_id, "character_id": second,
            "text": "联络者转述了堤禾说过的话：信报上抄到堤长身故", "kind": "fragment",
            "sources": [{"kind": "dialog", "ref": reply_id, "source_role": "other_character"}],
            "happened_world": 0, "learned_world": 0, "recorded_world": 0, "semantic_watermark": 0,
            "strength": 0.6, "confidence": 0.8,
        })
        h.world.rollback(info["id"], timeline_id, commit_id=point["id"], now_real=NOW + 4 * DAY)
        after_rollback = h.world.disclosures(info["id"], timeline_id)
        derived_gone = h.store.memory_get("mm-b2", instance_id=info["id"], timeline_id=timeline_id) is None
        derived_reply_gone = h.store.message_get(
            h.store._conn.execute("SELECT seq FROM message WHERE message_id=?", (derived[0],)).fetchone()[0]
        ) if h.store._conn.execute("SELECT 1 FROM message WHERE message_id=?", (derived[0],)).fetchone() else None

        ok = (failed is not None and grants_after_failure == []
              and "披露块" not in prompt_after_failure and "联络者明确给你看过这些转述" not in prompt_after_failure
              and len(grants) == 1 and "联络者明确给你看过这些转述" in prompt_after_grant
              and after_rollback == [] and derived_gone and derived_reply_gone is None)
        ev = (f"失败确认（{failed}）后授权 {len(grants_after_failure)} 条、B 上下文无披露块；"
              f"确认成功后授权 {len(grants)} 条且 B 才看到转述；回滚后授权 {len(after_rollback)} 条、"
              f"派生记忆撤销={derived_gone}、派生回复撤销={derived_reply_gone is None}")
        return ("PASS" if ok else "FAIL"), ev
    finally:
        h.close()


CHECKS = [
    check_01, check_02, check_03, check_04, check_05, check_06, check_07, check_08, check_09,
    check_10, check_11, check_12, check_13, check_14, check_15, check_16, check_17, check_18,
]


async def main() -> int:
    lines: list[str] = []
    tally = {"PASS": 0, "FAIL": 0, "DEFERRED": 0, "SKIP": 0}
    for index, check in enumerate(CHECKS, start=1):
        with tempfile.TemporaryDirectory(prefix="isekai-audit-") as raw:
            tmp = Path(raw)
            try:
                status, evidence = await check(tmp)
            except Exception:  # 探针自身异常也按 FAIL 报，附最小复现
                status, evidence = "FAIL", "探针异常：" + traceback.format_exc(limit=3).strip().replace("\n", " | ")[-300:]
        tally[status] = tally.get(status, 0) + 1
        lines.append(f"{status} {index} — {evidence}")
    lines.append(f"TOTAL {len(CHECKS)} PASS {tally['PASS']} FAIL {tally['FAIL']} DEFERRED {tally['DEFERRED']}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
