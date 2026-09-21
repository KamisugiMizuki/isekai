#!/usr/bin/env python
"""docs/MEMORY_SPEC.md 独立行为级审计（第二轮，独立探针）。

只新增本文件；不修改任何项目代码；不碰 data/isekai.db、config/config.yaml、packages/、logs/。
无真实联网（embedding 走本机 http.server 桩）、无真实 LLM（FakeLLM / 脚本模型）。
需要会话链路处走真 SessionService（真 SQLite、真入站 / 出站 / 固化）。

用法：.venv/Scripts/python.exe scripts/_audit2_mem.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM, LLMError  # noqa: E402
from isekai_core.runtime import memory as memory_mod  # noqa: E402
from isekai_core.runtime.service import RuntimeService  # noqa: E402
from isekai_core.session import SessionService  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.ump import Envelope  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402
from isekai_core.world import portable as portable_mod  # noqa: E402
from samples import DAY, sample_card, sample_package  # noqa: E402

NOW = 1.7e9
SVC = dict(instance_tokens_per_day=400_000, timeline_tokens_per_day=150_000, task_tokens_per_day=60_000)


# ---------- 本机 embedding 桩（真 HTTP） ----------


class EmbedStub:
    """本机 HTTP embedding 桩：可设维度 / 503 / 记录收到的 input，不发真实网络请求。"""

    def __init__(self, dim: int = 4, fail: bool = False) -> None:
        self.dim = dim
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                size = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(size) or b"{}")
                texts = [str(item) for item in (payload.get("input") or [])]
                stub.calls.append({
                    "model": payload.get("model"),
                    "input": texts,
                    "auth": self.headers.get("Authorization") or "",
                })
                if stub.fail:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps({
                    "data": [{"index": i, "embedding": stub.vector(t)} for i, t in enumerate(texts)],
                    "model": payload.get("model"),
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(str(text).encode("utf-8")).digest()
        values = [(digest[i % len(digest)] / 255.0) - 0.5 for i in range(self.dim)]
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return [value / norm for value in values]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---------- 夹具 ----------


@contextmanager
def env(**over: Any):
    """临时目录里的真 SQLite + RuntimeService；退出即删。"""
    root = Path(tempfile.mkdtemp(prefix="audit2mem-"))
    store = Store(root / "data" / "isekai.db")
    store.ensure_schema()
    cfg = load_config(root)
    # 探针不测「睡眠期等待窗口」本身：把窗口设为 0，避免每个回合真等 30–120 秒
    cfg = dataclasses.replace(
        cfg, runtime=dataclasses.replace(cfg.runtime, sleep_wait_min_s=0.0, sleep_wait_max_s=0.0)
    )
    world = RuntimeService(store, **{**SVC, **over})
    try:
        yield store, world, cfg, root
    finally:
        store.close()
        shutil.rmtree(root, ignore_errors=True)


def ready(store: Store, world: RuntimeService, *, days: int = 2, moment: int = DAY * 1500):
    package = sample_package(moment=moment)
    card = sample_card(package)
    info = create_instance(store, package, [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world.ensure_instance(info["id"], now_real=NOW)
    world.activate(info["id"], timeline_id, now_real=NOW)
    world.advance(info["id"], timeline_id, now_real=NOW + days * DAY, max_batches=60)
    return info, timeline_id, str(card["meta"]["card_id"])


def second_instance(store: Store, world: RuntimeService, *, days: int = 1):
    package = sample_package(moment=DAY * 1500)
    card = sample_card(package)
    info = create_instance(store, package, [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world.ensure_instance(info["id"], now_real=NOW)
    world.activate(info["id"], timeline_id, now_real=NOW)
    world.advance(info["id"], timeline_id, now_real=NOW + days * DAY, max_batches=60)
    return info, timeline_id, str(card["meta"]["card_id"])


def add_character(store: Store, world: RuntimeService, info: dict, timeline_id: str, name: str) -> str:
    card = sample_card(sample_package(), name=name)
    world.add_character(
        info["id"], timeline_id, card, now_real=NOW + 2 * DAY,
        joined_world=watermark(store, timeline_id), note=name,
    )
    return str(card["meta"]["card_id"])


def watermark(store: Store, timeline_id: str) -> int:
    return int(store.clock_get(timeline_id)["processed_world"])


def set_watermark(store: Store, timeline_id: str, value: int) -> None:
    """把线下水位直接设到指定世界秒（探针用：模拟「延迟数个世界日才提交」）。"""
    row = dict(store.clock_get(timeline_id))
    row["processed_world"] = int(value)
    store.clock_put(row)


def mem(store: Store, info: dict, timeline_id: str, character_id: str, ident: str, text: str, **over: Any):
    # 夹具按真实写入形状取当前水位（显式覆盖仍优先）
    stamp = watermark(store, timeline_id)
    row = {
        "id": ident, "instance_id": info["id"], "timeline_id": timeline_id, "character_id": character_id,
        "text": text, "kind": "fact", "sources": [{"kind": "claim", "ref": f"cl-{ident}"}],
        "strength": 0.6, "confidence": 0.8, "source_key": f"sk-{ident}",
        "happened_world": stamp, "learned_world": stamp, "recorded_world": stamp,
        "semantic_watermark": stamp, "decay_world": stamp,
    }
    row.update(over)
    return store.memory_add(row)


def ids(scope: list[dict]) -> list[str]:
    return [str(row["id"]) for row in scope]


def scope_ids(store: Store, info: dict, timeline_id: str, character_id: str, **kw) -> list[str]:
    return ids(store.memory_scope(info["id"], timeline_id, character_id, **kw))


def recall_ids(world: RuntimeService, info: dict, timeline_id: str, character_id: str, **kw) -> list[str]:
    return list(world.recall(info["id"], timeline_id, character_id, **kw)["ids"])


def memory_rows(store: Store, info: dict, timeline_id: str, character_id: str) -> list[dict]:
    return store.memory_scope(info["id"], timeline_id, character_id)


def cite_count(store: Store, timeline_id: str) -> int:
    return int(store._conn.execute(
        "SELECT COUNT(*) n FROM memory_citation WHERE timeline_id=?", (timeline_id,)).fetchone()["n"])


def raw(store: Store, sql: str, args: tuple = ()) -> list[Any]:
    return [tuple(row) for row in store._conn.execute(sql, args).fetchall()]


class ScriptLLM:
    """脚本化模型：可记录提示词、可在调用中触发钩子（模拟「等待期间世界侧发生的事」）。"""

    def __init__(self, replies: list[str], *, hook: Callable[[], None] | None = None) -> None:
        self.replies = list(replies)
        self.hook = hook
        self.calls: list[list[dict]] = []

    async def chat(self, messages, *, max_tokens=None, timeout=None, temperature=None) -> str:
        self.calls.append(messages)
        if self.hook is not None:
            self.hook()
        if not self.replies:
            raise RuntimeError("模型不可用")
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]

    def prompt_text(self) -> str:
        return "\n".join(str(item.get("content") or "") for call in self.calls for item in call)


def entry(text: str, ref: Any, **over: Any) -> str:
    row = {"text": text, "kind": "fact", "ref": str(ref), "strength": 0.6, "confidence": 0.8}
    row.update(over)
    return json.dumps([row], ensure_ascii=False)


def entries(rows: list[dict]) -> str:
    return json.dumps(rows, ensure_ascii=False)


def wire(store: Store, info: dict, timeline_id: str, character_id: str):
    """真会话链路所需的 session / channel / thread。"""
    session = store.session_ensure(info["id"], timeline_id, character_id)
    channel = store.channel_register(
        name="audit2", display_name="audit2", version="0", protocol="1", capabilities={}
    )[0]
    thread = store.thread_bind(channel["id"], f"dm-{character_id}", session["id"])
    return channel, thread


async def drive(
    store: Store,
    world: RuntimeService,
    cfg: Any,
    info: dict,
    timeline_id: str,
    character_id: str,
    texts: list[str],
    *,
    reply: str = "知道了",
    llm: Any = None,
    env_ids: list[str] | None = None,
) -> tuple[Any, list[dict], list[dict], dict, dict]:
    """走真 SessionService：入站 → 生成 → 固化回复 → 记忆记账；返回服务与已投递信封。"""
    channel, thread = wire(store, info, timeline_id, character_id)
    sent: list[dict] = []
    envelopes: list[dict] = []

    async def deliver(channel_id: str, thread_id: str, envelope: dict) -> bool:
        sent.append(envelope)
        return True

    service = SessionService(
        store=store, cfg=cfg, llm=llm or FakeLLM([reply]), deliver=deliver, runtime=world
    )
    for index, text in enumerate(texts):
        ident = (env_ids or [])[index] if env_ids and index < len(env_ids) else f"e2-{character_id}-{index}-{time.time_ns()}"
        envelope = Envelope(
            type="user_message", id=ident, ts=time.time(), payload={"text": text},
            thread_id=thread["thread_id"], binding_token=str(thread["binding_token"]),
        )
        envelopes.append(envelope)
        await service.accept(channel_id=channel["id"], thread_row=thread, env=envelope)
    for _ in range(50):
        if not service._tasks:
            break
        await asyncio.gather(*list(service._tasks), return_exceptions=True)
    return service, sent, envelopes, channel, thread


def run(coro):
    return asyncio.run(coro)


def delivered_text(sent: list[dict]) -> str:
    out: list[str] = []
    for envelope in sent:
        for part in ((envelope.get("payload") or {}).get("parts") or []):
            out.append(str(part.get("text") or ""))
    return chr(10).join(out)


# =====================================================================================
# 十一、行为验收
# =====================================================================================


def accept1() -> str:
    """验收1：同实例同线双角色互不可见；跨线、跨实例不串读。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, char_a = ready(store, world)
        char_b = add_character(store, world, info, timeline_id, "阿澈")
        mem(store, info, timeline_id, char_a, "mm-a", "堤禾记得巡夜人换了班。")
        mem(store, info, timeline_id, char_b, "mm-b", "阿澈记得巡夜人换了班。")
        other_info, other_tl, other_ch = second_instance(store, world)
        mem(store, other_info, other_tl, other_ch, "mm-x", "堤禾记得巡夜人换了班。")
        a_ids = recall_ids(world, info, timeline_id, char_a, topic="巡夜人")
        b_ids = recall_ids(world, info, timeline_id, char_b, topic="巡夜人")
        cross = recall_ids(world, other_info, other_tl, other_ch, topic="巡夜人")
        assert a_ids == ["mm-a"], f"本角色可见集合异常：{a_ids}"
        assert b_ids == ["mm-b"], f"同线另一角色串读：{b_ids}"
        assert cross == ["mm-x"], f"另一实例串读：{cross}"
        assert "mm-x" not in scope_ids(store, info, timeline_id, char_a), "跨实例作用域被击穿"
        return f"角色A={a_ids} 角色B={b_ids} 另一实例={cross}；另一实例的行不在本实例作用域"


def accept2() -> str:
    """验收2（前段）：分叉继承共同过去的记忆，不继承来源线后续记忆。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        assert mem(store, info, timeline_id, ch, "mm-old", "堤禾记得交接时的铜铃。") is not None
        commit = world.commit(info["id"], timeline_id, kind="manual", note="分叉点")
        assert mem(store, info, timeline_id, ch, "mm-new", "堤禾记得分叉之后的新事。") is not None
        fork = world.fork(info["id"], timeline_id, commit_id=commit["id"], name="分支", activate=True,
                          now_real=NOW + 3 * DAY)
        new_tl = str(fork["timeline"]["id"])
        inherited = scope_ids(store, info, new_tl, ch)
        assert inherited == ["mm-old"], f"分叉应只继承共同过去：{inherited}"
        assert "mm-new" not in recall_ids(world, info, new_tl, ch, topic="分叉之后"), "分叉继承了后续记忆"
        assert "mm-new" in scope_ids(store, info, timeline_id, ch), "原线自己的后续记忆丢失"
        return f"分叉线={inherited}；原线={scope_ids(store, info, timeline_id, ch)}"


def accept2b() -> str:
    """验收2（后段）/§七：回滚后旧向量即使物理存在也不得被命中返回。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        mem(store, info, timeline_id, ch, "mm-keep", "堤禾记得潮位刻线。", strength=0.9)
        commit = world.commit(info["id"], timeline_id, kind="manual", note="回滚点")
        mem(store, info, timeline_id, ch, "mm-gone", "堤禾记得回滚点之后的私事。", strength=0.9)
        store.memory_embedding_put(
            "mm-gone", instance_id=info["id"], timeline_id=timeline_id, model="stub",
            vector=[1.0, 0.0, 0.0, 0.0], content_hash="h",
        )
        world.rollback(info["id"], timeline_id, commit_id=commit["id"], now_real=NOW + 3 * DAY)
        assert "mm-gone" not in scope_ids(store, info, timeline_id, ch), "回滚未清掉后续记忆"
        # 物理上把旧向量塞回去（模拟「旧向量仍存在于缓存里」），不得被命中
        store._conn.execute(
            """INSERT OR REPLACE INTO memory_embedding(memory_id, instance_id, timeline_id, model, dim,
                                                      vector, source_version, created_at)
               VALUES('mm-gone',?,?, 'stub', 4, ?, 'h', ?)""",
            (info["id"], timeline_id, b"\x00" * 16, time.time()),
        )
        scores = store.memory_vector_scores(
            info["id"], timeline_id, ch, "回滚点之后的私事",
            query_vector=[1.0, 0.0, 0.0, 0.0], model="stub",
        )
        assert scores == {}, f"回滚后的旧向量仍被命中：{scores}"
        hit = recall_ids(world, info, timeline_id, ch, topic="回滚点之后的私事",
                         query_vector=[1.0, 0.0, 0.0, 0.0])
        assert "mm-gone" not in hit, f"召回返回了回滚前的条目：{hit}"
        return f"回滚后可见={scope_ids(store, info, timeline_id, ch)}；物理残留向量评分={scores}；召回={hit}"


def accept2c() -> str:
    """验收2 / §4.1：提取等待期间发生回滚 → 迟到结果不得写回。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        mem(store, info, timeline_id, ch, "mm-base", "堤禾记得旧钟楼。")
        commit = world.commit(info["id"], timeline_id, kind="manual", note="回滚点")
        world.queue_dialog_turn(
            info["id"], timeline_id, ch, world_seconds=watermark(store, timeline_id),
            user_ref="env-late", user_text="你昨晚去了水闸吗", reply_message_id="m-late", reply_text="我去了水闸",
        )
        task = [row for row in store.memory_tasks(info["id"], timeline_id)
                if str(row["source_ref"]).startswith("user:")][0]

        def rollback_now() -> None:
            world.rollback(info["id"], timeline_id, commit_id=commit["id"], now_real=NOW + 2 * DAY)

        llm = ScriptLLM([entry("她昨晚去过水闸。", task["id"])], hook=rollback_now)
        result = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        after = scope_ids(store, info, timeline_id, ch)
        leaked = [row for row in memory_rows(store, info, timeline_id, ch) if "水闸" in str(row["text"])]
        assert not leaked, (
            f"回滚发生在提取返回之前，迟到结果仍写回：{[(r['id'], r['text']) for r in leaked]}"
            f"（提取返回 {result}）"
        )
        return f"提取返回 {result}；回滚后可见={after}"


def accept3() -> str:
    """验收3：自述不变成亲历；传闻保留来源、高确信错误说法仍带来源。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        _svc, _sent, _envs, _chan, _thread = run(drive(
            store, world, _cfg, info, timeline_id, ch,
            ["你昨天是不是去水闸了"], reply="我昨天去过水闸了", env_ids=["e3-1"],
        ))
        tasks = store.memory_tasks(info["id"], timeline_id)
        reply_task = [row for row in tasks if str(row["source_ref"]).startswith("reply:")]
        assert reply_task, f"回复未登记为来源：{[r['source_ref'] for r in tasks]}"
        material = world._source_material(reply_task[0])
        assert material is not None, "回复来源不可达"
        label = memory_mod.source_label(material["sources"])
        assert "亲历" not in label, f"角色自述被标成亲历：{label}（sources={material['sources']}）"
        before_events = len(raw(store, "SELECT id FROM event WHERE instance_id=?", (info["id"],)))
        llm = ScriptLLM([entry("她被问到水闸，回答说去过。", reply_task[0]["id"], confidence=0.95)])
        run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 3 * DAY))
        after_events = len(raw(store, "SELECT id FROM event WHERE instance_id=?", (info["id"],)))
        saved = [row for row in memory_rows(store, info, timeline_id, ch) if "水闸" in str(row["text"])]
        assert saved, "提取未写入"
        source_kinds = {str(item.get("kind")) for item in json.loads(str(saved[0]["sources"]))}
        assert source_kinds == {"dialog"}, f"自述被升级成别的来源：{source_kinds}"
        assert after_events == before_events, "提取自述凭空造出了世界事件"
        # 传闻（已获知说法）保留「听说」来源
        knowledge = store.knowledge_window(info["id"], timeline_id, ch, until=10**15, limit=5)
        claim_label = "未取得说法来源"
        if knowledge:
            mem(store, info, timeline_id, ch, "mm-rumor", "听说堤长身故。", confidence=0.95,
                sources=[{"kind": "claim", "ref": str(knowledge[0]["id"]), "via": "驿站"}])
            row = store.memory_get("mm-rumor", instance_id=info["id"], timeline_id=timeline_id)
            claim_label = memory_mod.source_label(json.loads(str(row["sources"])))
            assert "听说" in claim_label, f"传闻来源丢失：{claim_label}"
        return (f"自述标签=「{label}」、写入来源 kind={source_kinds}、事件数 {before_events}→{after_events}；"
                f"传闻标签={claim_label} 且 confidence={saved[0]['confidence']}")


def accept4() -> str:
    """验收4：矛盾陈述不被去重吞掉、纠正留替代链、当前/过去可区分。"""
    query = "她把牌子交出去了吗"
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        mem(store, info, timeline_id, ch, "mm-old", "堤禾把通行牌交给了守卫。",
            strength=0.8, learned_world=1000, semantic_watermark=1000)
        mem(store, info, timeline_id, ch, "mm-fix", "堤禾没有把通行牌交给守卫。",
            strength=0.8, learned_world=2000, semantic_watermark=2000)
        rows = {str(row["id"]): row for row in memory_rows(store, info, timeline_id, ch)}
        assert len(rows) == 2, f"矛盾陈述被去重吞掉：{list(rows)}"
        assert rows["mm-old"]["superseded_by"] == "mm-fix", f"替代链缺失：{rows['mm-old']}"
        assert rows["mm-fix"]["supersedes"] == "mm-old", f"反向替代链缺失：{rows['mm-fix']}"
        dup = mem(store, info, timeline_id, ch, "mm-dup", "堤禾把通行牌交给了守卫。", strength=0.9)
        assert dup is None, "同事实去重失效"
        now_view = recall_ids(world, info, timeline_id, ch, topic=query)
        store_then = scope_ids(store, info, timeline_id, ch, until=1500)
        then_view = recall_ids(world, info, timeline_id, ch, topic=query, world_seconds=1500)
        assert now_view == ["mm-fix"], f"当前认知不是有效版本：{now_view}"
        assert then_view == ["mm-old"], (
            f"水位 1500（纠正之前）应读到当时的说法 mm-old，实际={then_view}；"
            f"store.memory_scope(until=1500)={store_then}（旧版本存在且带替代链，但被 state=archived + "
            f"service._drop_archived_unless_strong 的「逐字命中或向量过线」挡在召回之外）"
        )
        return f"条目数=2；替代链 mm-old→mm-fix；当前认知={now_view}；水位1500={then_view}"


def accept5() -> str:
    """验收5：缺配置 / 请求失败 / 维度改变 / 导入缺缓存 → 全文召回可用且不阻断。"""
    stub = EmbedStub(dim=4)
    try:
        with env(memory_embedding_model="stub", memory_embedding_base_url=stub.base_url,
                 memory_embedding_api_key="k") as (store, world, _cfg, _root):
            info, timeline_id, ch = ready(store, world)
            mem(store, info, timeline_id, ch, "mm-a", "堤禾记得潮位到了刻线。")
            mem(store, info, timeline_id, ch, "mm-b", "堤禾记得铜铃响了三次。")
            mem(store, info, timeline_id, ch, "mm-c", "堤禾记得渡口封了。")
            first = run(world.embed_memories(info["id"], timeline_id, now_real=NOW, limit=16))
            assert first.get("embedded") == 3, f"补齐异常：{first}"
            query = run(world.embed_query("潮位刻线", instance_id=info["id"], timeline_id=timeline_id))
            assert query, "查询向量获取失败"
            scores = store.memory_vector_scores(info["id"], timeline_id, ch, "潮位刻线",
                                                query_vector=query, model="stub")
            assert scores, "向量分数为空"
            stub.fail = True  # 请求失败：退化全文、不阻断
            assert run(world.embed_memories(info["id"], timeline_id, now_real=NOW, limit=16)).get("embedded") == 0
            assert recall_ids(world, info, timeline_id, ch, topic="潮位刻线"), "请求失败时全文召回不可用"
            stub.fail = False
            world_state = world.advance(info["id"], timeline_id, now_real=NOW + 4 * DAY, max_batches=30)
            assert world_state["state"] in {"current", "catching_up"}, "推进被 embedding 阻断"
            # 维度改变：旧向量退出混合召回
            big = EmbedStub(dim=8)
            try:
                world8 = RuntimeService(
                    store, **{**SVC, "memory_embedding_model": "stub",
                              "memory_embedding_base_url": big.base_url, "memory_embedding_api_key": "k"},
                )
                query8 = run(world8.embed_query("潮位刻线", instance_id=info["id"], timeline_id=timeline_id))
                assert query8 and len(query8) == 8
                stale_scores = store.memory_vector_scores(info["id"], timeline_id, ch, "潮位刻线",
                                                          query_vector=query8, model="stub")
                assert stale_scores == {}, f"维度改变后旧向量仍参与：{stale_scores}"
                assert recall_ids(world8, info, timeline_id, ch, topic="潮位刻线"), "维度改变后全文召回不可用"
            finally:
                big.close()
        # 导入件缺向量缓存：全文召回可用
        with env() as (store, world, _cfg, _root):
            info, timeline_id, ch = ready(store, world)
            mem(store, info, timeline_id, ch, "mm-d", "堤禾记得旧码头。")
            assert raw(store, "SELECT memory_id FROM memory_embedding WHERE timeline_id=?", (timeline_id,)) == []
            assert recall_ids(world, info, timeline_id, ch, topic="旧码头") == ["mm-d"]
    finally:
        stub.close()
    return "缺配置/503/维度改变/无缓存 → 全文召回均可用；向量残留评分 {}；世界推进未受阻"


def accept6() -> str:
    """验收6：同一源重复提取、同一轮重复召回、重放补算都不重复写或强化（真会话链路）。"""
    with env() as (store, world, cfg, _root):
        info, timeline_id, ch = ready(store, world)
        mem(store, info, timeline_id, ch, "mm-cite", "堤禾记得潮位刻线。", strength=0.5)
        svc, _sent, envs, _chan, thread = run(drive(
            store, world, cfg, info, timeline_id, ch,
            ["潮位刻线到了吗"], llm=FakeLLM(["到了刻线。"]), env_ids=["e6-1"],
        ))
        tasks = store.memory_tasks(info["id"], timeline_id)
        assert len(tasks) == 2, f"一轮应登记 2 条来源（输入 + 回复）：{[r['source_ref'] for r in tasks]}"
        assert svc._recalled == ["mm-cite"], f"本轮未召回预期条目：{svc._recalled}"
        after_cite = store.memory_get("mm-cite", instance_id=info["id"], timeline_id=timeline_id)
        assert abs(float(after_cite["strength"]) - 0.55) < 1e-9, f"强化异常：{after_cite['strength']}"
        # 重放同一信封：不重新生成、不重复登记、不重复强化
        again = run(drive(store, world, cfg, info, timeline_id, ch, ["潮位刻线到了吗"],
                          llm=FakeLLM(["到了刻线。"]), env_ids=["e6-1"]))
        _svc2, sent2, _e2, chan2, thread2 = again
        assert len(store.memory_tasks(info["id"], timeline_id)) == 2, "重放重复登记来源"
        cited = store.memory_get("mm-cite", instance_id=info["id"], timeline_id=timeline_id)
        assert abs(float(cited["strength"]) - 0.55) < 1e-9, f"重放导致重复强化：{cited['strength']}"
        # 提取幂等：两次提取不重复写入
        entry_text = "她听说刻线到了。"
        llm = ScriptLLM([
            entries([entry(entry_text, row["id"]) for row in tasks]),
        ])
        first = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        before = len(memory_rows(store, info, timeline_id, ch))
        second = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        after = len(memory_rows(store, info, timeline_id, ch))
        assert first["calls"] == 1 and second["calls"] == 0, f"重复提取又调了模型：{first} / {second}"
        assert after == before, f"重复提取重复写入：{before}→{after}"
        return (f"来源登记 2 条（重放不新增）；本轮召回 {svc._recalled} 强化 0.5→0.55；"
                f"重放后仍 0.55；两次提取 written={first['written']}/{second['written']}，条目数 {before}→{after}")


def accept7() -> str:
    """验收7：衰减按世界时长（小步=批量）、冻结不衰减、归档唤起受认知权限约束。"""
    with env(memory_decay_per_day=0.05) as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        base = watermark(store, timeline_id)
        mem(store, info, timeline_id, ch, "mm-1", "堤禾记得渡口。", strength=1.0, decay_world=base)
        mem(store, info, timeline_id, ch, "mm-2", "堤禾记得码头。", strength=1.0, decay_world=base)
        store.memory_decay(timeline_id=timeline_id, to_world=base + 10 * DAY, day_seconds=DAY, per_day=0.05)
        for step in range(1, 11):
            store.memory_decay(timeline_id=timeline_id, to_world=base + step * DAY, day_seconds=DAY, per_day=0.05)
        one = float(store.memory_get("mm-1", instance_id=info["id"], timeline_id=timeline_id)["strength"])
        many = float(store.memory_get("mm-2", instance_id=info["id"], timeline_id=timeline_id)["strength"])
        assert abs(one - many) < 1e-12, f"小步与批量不等价：{one} != {many}"
        expected = memory_mod.decayed_strength(
            1.0, from_world=base, to_world=base + 10 * DAY, day_seconds=DAY, per_day=0.05
        )
        assert abs(one - expected) < 1e-12, f"衰减曲线不符：{one} != {expected}"
        # 冻结期间不衰减
        world.freeze(info["id"], timeline_id, now_real=NOW + 3 * DAY)
        frozen_at = float(store.memory_get("mm-1", instance_id=info["id"], timeline_id=timeline_id)["strength"])
        world.advance(info["id"], timeline_id, now_real=NOW + 40 * DAY, max_batches=40)
        still = float(store.memory_get("mm-1", instance_id=info["id"], timeline_id=timeline_id)["strength"])
        assert abs(frozen_at - still) < 1e-12, f"冻结期间仍衰减：{frozen_at}→{still}"
        # 归档：普通召回不返回，强相关可唤起（仍同作用域）
        mem(store, info, timeline_id, ch, "mm-arch", "堤禾记得水闸下的刻字。", strength=0.05)
        arch = store.memory_get("mm-arch", instance_id=info["id"], timeline_id=timeline_id)
        assert str(arch["state"]) == "archived", f"低强度未归档：{arch['state']}"
        assert "mm-arch" not in recall_ids(world, info, timeline_id, ch, topic="今天天气"), "归档条目进了普通召回"
        assert "mm-arch" in recall_ids(world, info, timeline_id, ch, topic="水闸下的刻字"), "强相关检索未唤起归档条目"
        other = add_character(store, world, info, timeline_id, "阿澈")
        assert "mm-arch" not in recall_ids(world, info, timeline_id, other, topic="水闸下的刻字"), (
            "归档唤起到了别的角色"
        )
        return (f"10 世界日：一次={one:.6f} 分十步={many:.6f}（相等）；冻结 40 现实日强度 {frozen_at:.6f} 不变；"
                f"归档普通召回不返回、强相关命中且不跨角色")


def accept7b() -> str:
    """验收7 / §六：整理挂作息节律——无睡眠角色只在世界日界触发（行为级）。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        mem(store, info, timeline_id, ch, "mm-long",
            "堤禾记得那天在渡口等了很久，风把她的斗篷吹得鼓起来，她一路走一路在心里把要说的话来回过了好几遍，"
            "连开场白都换了三种说法，最后还是没能开口，只把手里那根断了的绳结反复搓了一遍又一遍。")
        row = store.memory_get("mm-long", instance_id=info["id"], timeline_id=timeline_id)
        assert len(str(row["text"])) > 60, "夹具文本不够长"
        set_watermark(store, timeline_id, DAY * 1502 + DAY // 2)  # 不在世界日界
        idle = ScriptLLM(["压短后的一句。"])
        out_idle = run(world.organize_memories(info["id"], timeline_id, llm=idle, now_real=NOW + 3 * DAY))
        assert idle.calls == [], f"非日界也整理了：{out_idle}"
        set_watermark(store, timeline_id, DAY * 1503)  # 世界日界
        edge = ScriptLLM(["她在渡口等了很久，终究没开口。"])
        out_edge = run(world.organize_memories(info["id"], timeline_id, llm=edge, now_real=NOW + 3 * DAY))
        assert len(edge.calls) == 1, f"日界未触发整理：{out_edge}"
        assert out_edge["organized"] == 1, f"整理未落库：{out_edge}"
        return f"非日界调用 0 次；世界日界调用 1 次并落库 {out_edge['items']}"


def accept8() -> str:
    """验收8：导出导入恢复原文/经历/记忆及引用；不含密钥；无记忆明细入口。"""
    secret = "sk-audit2-not-a-real-key-0123456789"
    with env(memory_embedding_model="stub", memory_embedding_base_url="http://127.0.0.1:9/v1",
             memory_embedding_api_key=secret) as (store, world, cfg, _root):
        info, timeline_id, ch = ready(store, world)
        mem(store, info, timeline_id, ch, "mm-exp", "堤禾记得旧码头的水位。", strength=0.7)
        run(drive(store, world, cfg, info, timeline_id, ch, ["水位怎么样"], env_ids=["e8-1"]))
        store.memory_cite("turn-8", "mm-exp", instance_id=info["id"], timeline_id=timeline_id,
                          character_id=ch, world_seconds=watermark(store, timeline_id))
        assert cite_count(store, timeline_id) >= 1, "夹具未落引用记录"
        container = portable_mod.build_container(store, info["id"])
        blob = json.dumps(container, ensure_ascii=False)
        assert "api_key" not in blob and "apiKey" not in blob, (
            f"导出件里出现了密钥字段：{blob[max(0, blob.find('api_key')) - 80:][:160]}"
        )
        assert secret not in blob, "导出件里出现了配置的 embedding 凭据"
        imported = portable_mod.import_instance(store, container, display_name="副本")
        new_id = str(imported["id"])
        new_tl = str(store.timeline_list(new_id)[0]["id"])
        new_memories = scope_ids(store, {"id": new_id}, new_tl, ch)
        dialogs = raw(store, "SELECT COUNT(*) n FROM message m JOIN session s ON s.id=m.session_id "
                            "WHERE s.instance_id=?", (new_id,))
        experiences = raw(store, "SELECT COUNT(*) n FROM experience WHERE instance_id=?", (new_id,))
        cites = int(store._conn.execute(
            "SELECT COUNT(*) n FROM memory_citation WHERE timeline_id IN "
            "(SELECT id FROM timeline WHERE instance_id=?)", (new_id,)).fetchone()["n"])
        assert new_memories, "导入后记忆未恢复"
        assert int(dialogs[0][0]) >= 1, f"导入后对话未恢复：{dialogs}"
        assert int(experiences[0][0]) >= 1, f"导入后经历未恢复：{experiences}"
        assert cites >= 1, (
            f"导入后引用记录（memory_citation）未恢复：{cites} 条；导出件的运行层快照键里没有引用记录"
            f"（store.runtime_dump 只导出 memories / memory_tasks）"
        )
        return f"导入后记忆={new_memories} 对话={int(dialogs[0][0])} 经历={int(experiences[0][0])} 引用={cites}"


def accept9() -> str:
    """验收9（合并批）：共享回复的多入一回按来源分别结算、不重复提取。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        stamp = watermark(store, timeline_id)
        for ref in ("env-m1", "env-m2"):
            world.queue_dialog_turn(info["id"], timeline_id, ch, world_seconds=stamp, user_ref=ref,
                                    user_text=f"第 {ref} 条", reply_message_id="m-merged", reply_text="一起回你")
        added_again = world.queue_dialog_turn(
            info["id"], timeline_id, ch, world_seconds=stamp, user_ref="env-m1",
            user_text="第 env-m1 条", reply_message_id="m-merged", reply_text="一起回你",
        )
        tasks = store.memory_tasks(info["id"], timeline_id)
        refs = sorted(str(row["source_ref"]) for row in tasks)
        assert refs == ["reply:m-merged", "user:env-m1", "user:env-m2"], f"合并批来源登记异常：{refs}"
        assert added_again == 0, f"重放又登记了新来源：{added_again}"
        llm = ScriptLLM([
            entry("她收到两条消息，回了一句。", [r["id"] for r in tasks if str(r["source_ref"]).startswith("user:")][0]),
        ])
        first = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY, limit=8))
        second = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY, limit=8))
        assert first["written"] == 1 and second["written"] == 0, f"合并批重复写入：{first} / {second}"
        return f"来源={refs}；重放新增={added_again}；两次提取 written={first['written']}/{second['written']}"


def accept9b() -> str:
    """验收9（后段）：冻结 / 归档后迟到任务不写入。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        stamp = watermark(store, timeline_id)
        world.queue_dialog_turn(info["id"], timeline_id, ch, world_seconds=stamp, user_ref="env-frz",
                                user_text="冻结前说的话", reply_message_id="m-frz", reply_text="冻结前的回复")
        before = len(memory_rows(store, info, timeline_id, ch))
        world.freeze(info["id"], timeline_id, now_real=NOW + 3 * DAY)
        task = store.memory_tasks(info["id"], timeline_id)
        assert task, "冻结前应留有待提取来源"
        llm = ScriptLLM([entry("她说了冻结前的话。", task[0]["id"])])
        frozen_out = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 3 * DAY))
        after_freeze = len(memory_rows(store, info, timeline_id, ch))
        world.archive_timeline(info["id"], timeline_id, now_real=NOW + 3 * DAY)
        world.queue_dialog_turn(info["id"], timeline_id, ch, world_seconds=stamp, user_ref="env-arch",
                                user_text="归档前说的话", reply_message_id="m-arch", reply_text="归档前的回复")
        arch_task = [row for row in store.memory_tasks(info["id"], timeline_id)
                     if "env-arch" in str(row["source_ref"])]
        assert arch_task, "归档前应留有待提取来源"
        arch_llm = ScriptLLM([entry("她在归档前说过话。", arch_task[0]["id"])])
        arch_out = run(world.extract_memories(info["id"], timeline_id, llm=arch_llm, now_real=NOW + 4 * DAY))
        after_archive = len(memory_rows(store, info, timeline_id, ch))
        freeze_leak = after_freeze - before
        archive_leak = after_archive - after_freeze
        assert not freeze_leak and not archive_leak, (
            f"冻结 / 归档后迟到任务仍写入：冻结 {before}→{after_freeze}（{frozen_out}）；"
            f"归档 {after_freeze}→{after_archive}（{arch_out}）；"
            f"extract_memories 未校验 timeline.state（app.py 只调度 active 线，但直接调用 / 调度恢复即穿透）"
        )
        return f"冻结前后条目数 {before}→{after_freeze}；归档前后 {after_freeze}→{after_archive}"


def accept10() -> str:
    """验收10：召回与重复引用不提高采信；同源重复不构成独立佐证。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        mem(store, info, timeline_id, ch, "mm-belief", "堤禾以为堤长还在。", strength=0.6, confidence=0.4)
        for turn in ("t-1", "t-2", "t-3"):
            world.cite_memories(info["id"], timeline_id, ch, turn_id=turn, memory_ids=["mm-belief"],
                                world_seconds=watermark(store, timeline_id))
        row = store.memory_get("mm-belief", instance_id=info["id"], timeline_id=timeline_id)
        assert abs(float(row["confidence"]) - 0.4) < 1e-9, f"召回/引用改变了采信：{row['confidence']}"
        assert abs(float(row["strength"]) - 0.75) < 1e-9, f"三轮强化异常：{row['strength']}"
        again = world.cite_memories(info["id"], timeline_id, ch, turn_id="t-1", memory_ids=["mm-belief"],
                                    world_seconds=watermark(store, timeline_id))
        after = store.memory_get("mm-belief", instance_id=info["id"], timeline_id=timeline_id)
        assert again == 0 and abs(float(after["strength"]) - 0.75) < 1e-9, "同轮重复引用被重复加权"
        # 同源重复不构成独立佐证：同一来源键只留一条
        mem(store, info, timeline_id, ch, "mm-s1", "她听说渡口封了。", source_key="mt-same")
        second = mem(store, info, timeline_id, ch, "mm-s2", "她听说渡口封了（重复来源）。", source_key="mt-same")
        assert second is None, "同源重复被当成独立条目写入"
        return f"三轮强化 strength={after['strength']}、confidence 保持 {after['confidence']}；同轮重复引用返回 {again}"


def accept11() -> str:
    """验收11：打算跨重启保留、未执行前不产生亲历、随口愿望不升级为义务。"""
    with env() as (store, world, _cfg, root):
        info, timeline_id, ch = ready(store, world)
        stamp = watermark(store, timeline_id)
        store.intent_put({
            "instance_id": info["id"], "timeline_id": timeline_id, "character_id": ch, "id": "in-audit",
            "object": "把通行牌还回去", "basis": "她答应过守卫", "strength": 0.7,
            "window_from": stamp, "window_to": stamp + 5 * DAY, "preconditions": "[]",
            "effect": "", "stage": "planned", "note": "", "source_world": stamp, "updated_world": stamp,
        })
        events_before = len(raw(store, "SELECT id FROM event WHERE instance_id=?", (info["id"],)))
        exp_before = len(raw(store, "SELECT id FROM experience WHERE instance_id=?", (info["id"],)))
        intents_before = {str(row["id"]): str(row["stage"]) for row in store.intent_list(info["id"], timeline_id, ch)}
        queued = world.queue_world_sources(info["id"], timeline_id)
        intent_tasks = [row for row in store.memory_tasks(info["id"], timeline_id)
                        if str(row["source_kind"]) == "intent" and str(row["source_ref"]) == "in-audit"]
        assert intent_tasks, f"打算未登记为来源（queue={queued}）"
        material = world._source_material(intent_tasks[0])
        assert material and "依据" in material["text"], f"打算材料未含对象/依据：{material}"
        llm = ScriptLLM([entry(material["text"], intent_tasks[0]["id"], kind="promise")])
        # 来源多于单批上限（8 条）时按批补齐：跑两轮，确保该打算进入被提取的材料批
        run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        saved = [row for row in memory_rows(store, info, timeline_id, ch) if str(row["kind"]) == "promise"]
        assert saved, "打算未入库"
        label = memory_mod.source_label(json.loads(str(saved[0]["sources"])))
        events_after = len(raw(store, "SELECT id FROM event WHERE instance_id=?", (info["id"],)))
        exp_after = len(raw(store, "SELECT id FROM experience WHERE instance_id=?", (info["id"],)))
        intents_after = {str(row["id"]): str(row["stage"]) for row in store.intent_list(info["id"], timeline_id, ch)}
        assert events_after == events_before and exp_after == exp_before, "记下打算产生了世界效果 / 亲历"
        assert intents_after == intents_before, f"随口愿望被升级为义务：{intents_before} → {intents_after}"
        assert intents_after.get("in-audit") == "planned", f"打算状态被改写：{intents_after}"
        # 「重启」等价读法：另开一条连接读同一个库文件（写入均已提交），确认持久化
        store2 = Store(root / "data" / "isekai.db")
        store2.ensure_schema()
        try:
            kept = [row for row in store2.intent_list(info["id"], timeline_id, ch) if str(row["id"]) == "in-audit"]
            memories = store2.memory_scope(info["id"], timeline_id, ch)
            assert len(kept) == 1 and str(kept[0]["stage"]) == "planned", f"重开库后打算丢失：{kept}"
            assert [row["id"] for row in memories] == [saved[0]["id"]], "重开库后记忆丢失"
        finally:
            store2.close()
        return (f"打算来源标签=「{label}」、kind=promise；事件 {events_before}→{events_after}、"
                f"经历 {exp_before}→{exp_after}、意图状态未被升级；重开库后打算与记忆仍在")


def accept12() -> str:
    """验收12：相关新经历能接回旧片段；回接仍受实例 / 线 / 角色隔离限制。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        other = add_character(store, world, info, timeline_id, "阿澈")
        mem(store, info, timeline_id, ch, "mm-topic", "堤禾说过铜铃挂在渡口。", learned_world=10)
        mem(store, info, timeline_id, other, "mm-other", "阿澈说过铜铃挂在自己家门口。", learned_world=10)
        current = watermark(store, timeline_id)
        world.queue_dialog_turn(info["id"], timeline_id, ch, world_seconds=current,
                                user_ref="env-back", user_text="铜铃还在渡口吗",
                                reply_message_id="m-back", reply_text="我记不清铜铃的位置了")
        task = [row for row in store.memory_tasks(info["id"], timeline_id)
                if str(row["source_ref"]).startswith("reply:")][0]
        llm = ScriptLLM([entry("她记不清铜铃挂在哪儿了。", task["id"])])
        run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        back = recall_ids(world, info, timeline_id, ch, topic="铜铃还在渡口吗")
        assert "mm-topic" in back, f"旧片段未被回接：{back}"
        assert any("记不清铜铃" in str(row["text"]) for row in memory_rows(store, info, timeline_id, ch)), "新经历未入库"
        other_view = recall_ids(world, info, timeline_id, other, topic="铜铃")
        assert "mm-other" not in back, f"本角色召回里出现了另一角色的条目：{back}"
        assert "mm-topic" not in other_view, f"另一角色召回里出现了本角色的条目：{other_view}"
        return f"回接命中={back}；另一角色召回={other_view}（各自只见自己的条目）"


def accept13() -> str:
    """验收13：共享预算耗尽 → 已固化对话不阻塞、向量待处理、全文召回可用、重试不绕过。"""
    with env(instance_tokens_per_day=1) as (store, world, cfg, _root):
        info, timeline_id, ch = ready(store, world)
        mem(store, info, timeline_id, ch, "mm-budget", "堤禾记得旧码头的水位。", strength=0.5)
        # 预算耗尽时，已固化对话照常
        _svc, sent, _envs, _chan, _thread = run(drive(
            store, world, cfg, info, timeline_id, ch, ["水位怎么样"], reply="水位还在涨。", env_ids=["e13-1"],
        ))
        assert "水位还在涨。" in delivered_text(sent), "预算耗尽阻断了已固化回复的投递"
        assert store.memory_get("mm-budget", instance_id=info["id"], timeline_id=timeline_id) is not None
        task = store.memory_tasks(info["id"], timeline_id)
        assert task, "已固化轮的来源未登记"
        # 提取：被预算挡住 → 待处理、不写入
        llm = ScriptLLM([entry("她说水位还在涨。", task[0]["id"])])
        out1 = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        out2 = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        assert out1["calls"] == 0 and out1["written"] == 0, f"预算耗尽仍调用了模型：{out1}"
        assert out1["pending"] == len(task) and out2["pending"] == len(task), f"待处理状态丢失：{out1} / {out2}"
        assert llm.calls == [], "预算耗尽仍发起了模型调用"
        assert len(memory_rows(store, info, timeline_id, ch)) == 1, "预算耗尽期间的重复重试产生了写入"
        # 向量：待嵌入 + 全文召回可用
        stub = EmbedStub(dim=4)
        try:
            world_e = RuntimeService(
                store, **{**SVC, "instance_tokens_per_day": 1, "memory_embedding_model": "stub",
                          "memory_embedding_base_url": stub.base_url, "memory_embedding_api_key": "k"},
            )
            emb = run(world_e.embed_memories(info["id"], timeline_id, now_real=NOW + 2 * DAY))
            assert emb.get("embedded") == 0 and emb.get("paused"), f"向量任务绕过预算：{emb}"
            assert stub.calls == [], "预算耗尽仍向 embedding 服务发了请求"
            assert recall_ids(world_e, info, timeline_id, ch, topic="旧码头的水位"), "全文召回不可用"
        finally:
            stub.close()
        cit = store.memory_cite("turn-13", "mm-budget", instance_id=info["id"], timeline_id=timeline_id,
                                character_id=ch, world_seconds=watermark(store, timeline_id))
        assert cit is True, "预算耗尽还妨碍了记账"
        return (f"回复照常投递；提取 calls={out1['calls']} written={out1['written']} pending={out1['pending']}；"
                f"向量 paused={emb.get('paused')} 且桩 0 次请求；全文召回可用")


def accept14() -> str:
    """验收14（前段）：立即提交与延迟数个世界日提交，在同一水位下强度等价、三时间戳不同。"""
    with env(memory_decay_per_day=0.02) as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        base = watermark(store, timeline_id)
        # A：来源当时提取（立即提交），再推进到 W2
        world.queue_dialog_turn(info["id"], timeline_id, ch, world_seconds=base, user_ref="env-now",
                                user_text="井里的水很甜", reply_message_id="m-now", reply_text="嗯，我知道")
        task_now = [row for row in store.memory_tasks(info["id"], timeline_id) if "env-now" in str(row["source_ref"])][0]
        out_now = run(world.extract_memories(info["id"], timeline_id,
                                             llm=ScriptLLM([entry("她听说井里的水很甜。", task_now["id"], strength=0.8)]),
                                             now_real=NOW + 2 * DAY))
        assert out_now["written"] == 1, f"立即提取未写入：{out_now}"
        row_now = [r for r in memory_rows(store, info, timeline_id, ch) if "井里的水很甜" in str(r["text"])][0]
        w2 = base + 3 * DAY
        store.memory_decay(timeline_id=timeline_id, to_world=w2, day_seconds=DAY, per_day=0.02)
        strength_now = float(store.memory_get(str(row_now["id"]), instance_id=info["id"],
                                              timeline_id=timeline_id)["strength"])
        # B：同类来源拖到 W2 才提取，强度按来源时刻结算
        world.advance(info["id"], timeline_id, now_real=NOW + 2 * DAY + 3 * DAY, max_batches=30)
        set_watermark(store, timeline_id, w2)
        world.queue_dialog_turn(info["id"], timeline_id, ch, world_seconds=base, user_ref="env-late14",
                                user_text="渡口的灯换了", reply_message_id="m-late14", reply_text="噢",
                                )
        task_late = [r for r in store.memory_tasks(info["id"], timeline_id) if "env-late14" in str(r["source_ref"])][0]
        out_late = run(world.extract_memories(info["id"], timeline_id,
                                              llm=ScriptLLM([entry("她听说渡口的灯换了。", task_late["id"], strength=0.8)]),
                                              now_real=NOW + 5 * DAY))
        assert out_late["written"] == 1, f"延迟提取未写入：{out_late}"
        row_late = [r for r in memory_rows(store, info, timeline_id, ch)
                    if "渡口的灯换了" in str(r["text"]) and str(r["id"]) != str(row_now["id"])][0]
        strength_late = float(row_late["strength"])
        assert abs(strength_now - strength_late) < 1e-9, (
            f"同一水位下强度不等价：立即提交 + 衰减到 W2 = {strength_now} vs 延迟提交 = {strength_late}"
        )
        assert int(row_late["happened_world"]) == base == int(row_late["learned_world"]), (
            f"来源三时间戳混淆：{({k: row_late[k] for k in ('happened_world', 'learned_world', 'recorded_world')})}"
        )
        assert int(row_late["recorded_world"]) == w2 > int(row_late["learned_world"]), "记录水位未按当前水位结算"
        return (f"立即={strength_now:.6f} 延迟={strength_late:.6f}（同水位等价）；"
                f"happened={row_late['happened_world']} learned={row_late['learned_world']} "
                f"recorded={row_late['recorded_world']}")


def accept14b() -> str:
    """验收14（后段）/§4.1：迟到提取不得覆盖期间已固化的纠正（走真提取路径）。"""
    with env(memory_decay_per_day=0.0) as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        base = watermark(store, timeline_id)
        world.queue_dialog_turn(info["id"], timeline_id, ch, world_seconds=base, user_ref="env-old14",
                                user_text="堤长的事有下文吗", reply_message_id="m-old14", reply_text="听人说堤长身故了")
        task = [row for row in store.memory_tasks(info["id"], timeline_id) if "env-old14" in str(row["source_ref"])][0]
        world.advance(info["id"], timeline_id, now_real=NOW + 10 * DAY, max_batches=30)
        fix_world = watermark(store, timeline_id)
        mem(store, info, timeline_id, ch, "mm-fix", "堤长没有身故，接任推举未毕。", strength=0.9, confidence=0.9,
            learned_world=fix_world, recorded_world=fix_world, semantic_watermark=fix_world, decay_world=fix_world)
        world.advance(info["id"], timeline_id, now_real=NOW + 30 * DAY, max_batches=60)
        llm = ScriptLLM([entry("堤长身故，接任推举未毕。", task["id"], strength=0.8)])
        out = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 30 * DAY))
        fix = store.memory_get("mm-fix", instance_id=info["id"], timeline_id=timeline_id)
        assert str(fix["superseded_by"] or "") == "", (
            f"迟到提取（来源水位 {base}）把 10 世界日后固化的纠正顶掉：mm-fix.superseded_by="
            f"{fix['superseded_by']} state={fix['state']}（提取返回 {out}；memory_add 拿当前水位当语义水位，"
            f"迟到条目因此被判成「更新」的纠正）"
        )
        return f"纠正在效（state={fix['state']}）；迟到提取 written={out['written']}"


# =====================================================================================
# 正文条款（含实现义务）
# =====================================================================================


def spec_extraction_validation() -> str:
    """§4.1：不合规提取结果整条丢弃（不拿猜测填空）；失败保留待处理。"""
    refs = {"mt-1"}
    assert memory_mod.parse_extraction('[{"text":"x","kind":"fact","ref":"nope"}]', refs) == [], "假来源未被丢弃"
    assert memory_mod.parse_extraction('[{"text":"x","kind":"rumor","ref":"mt-1"}]', refs) == [], "闭集外类型未被丢弃"
    assert memory_mod.parse_extraction("这不是 JSON", refs) == [], "非 JSON 未被丢弃"
    ok = memory_mod.parse_extraction(entry("堤长身故，接任推举未毕。", "mt-1"), refs)
    assert len(ok) == 1 and ok[0]["kind"] == "fact"
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        world.queue_dialog_turn(info["id"], timeline_id, ch, world_seconds=watermark(store, timeline_id),
                                user_ref="env-p", user_text="材料", reply_message_id="m-p", reply_text="回复")
        pending = store.memory_tasks(info["id"], timeline_id)
        run(world.extract_memories(info["id"], timeline_id, llm=ScriptLLM(["完全不是 JSON"]), now_real=NOW + 2 * DAY))
        states = {(str(r["source_ref"]), str(r["state"])) for r in store.memory_tasks(info["id"], timeline_id, state="pending")}
        done = {(str(r["source_ref"]), str(r["state"])) for r in store.memory_tasks(info["id"], timeline_id, state="done")}
        assert memory_rows(store, info, timeline_id, ch) == [], "不可解析的结果被当成记忆写入"
        assert len(states) == len(pending) and not done, f"提取失败未保留待处理：pending={states} done={done}"
        run(world.extract_memories(info["id"], timeline_id, llm=ScriptLLM(["[]"]), now_real=NOW + 2 * DAY))
        assert len(store.memory_tasks(info["id"], timeline_id, state="pending")) == 0, "「无可记内容」未被结算"
        return f"四类不合规结果全部丢弃；失败保留待处理 {len(states)} 条；[] 结算为 done"


def spec_prompt_scope() -> str:
    """§4.1/§7：提取提示只含该角色已接触的材料，不因为同库就读取别人的内容。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        other = add_character(store, world, info, timeline_id, "阿澈")
        secret = "西堤仓库的钥匙在阿澈手里"
        store.knowledge_put({
            "instance_id": info["id"], "timeline_id": timeline_id, "character_id": other, "id": "kn-secret",
            "world_seconds": watermark(store, timeline_id), "kind": "fact", "target": "仓库",
            "source": "亲见", "stance": "know", "text": secret,
        })
        mem(store, info, timeline_id, other, "mm-secret", secret)
        world.queue_dialog_turn(info["id"], timeline_id, ch, world_seconds=watermark(store, timeline_id),
                                user_ref="env-scope", user_text="你今天做了什么", reply_message_id="m-scope",
                                reply_text="我在堤上待着")
        llm = ScriptLLM([entry("她在堤上待着。", "mt-x")])
        tasks = store.memory_tasks(info["id"], timeline_id)
        my_task = [r for r in tasks if str(r["character_id"]) == ch]
        llm = ScriptLLM([entry("她在堤上待着。", my_task[0]["id"])])
        run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        prompt = llm.prompt_text()
        assert secret not in prompt, "提示词里出现了未向该角色披露的内容"
        assert "mm-secret" not in prompt and "kn-secret" not in prompt, "提示词里出现了别的角色的标识"
        assert "材料：" in prompt and str(my_task[0]["id"]) in prompt, f"提示词缺少本人材料：{prompt[:200]}"
        return "提示词含本人材料，未出现另一角色的私密记载 / 标识"


def spec_three_timestamps() -> str:
    """§三：发生 / 获知 / 记录三个时间戳不混成一个；查询水位按获知时间过滤。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        base = watermark(store, timeline_id)
        mem(store, info, timeline_id, ch, "mm-t1", "堤禾记得渡口封了。",
            happened_world=base - 5 * DAY, learned_world=base, recorded_world=base + DAY)
        columns = {row[1] for row in raw(store, "PRAGMA table_info(memory)")}
        assert {"happened_world", "learned_world", "recorded_world"} <= columns, f"三时间戳列缺失：{columns}"
        row = store.memory_get("mm-t1", instance_id=info["id"], timeline_id=timeline_id)
        assert row["happened_world"] == base - 5 * DAY and row["learned_world"] == base
        assert row["recorded_world"] == base + DAY, "三时间戳被压成一个"
        assert scope_ids(store, info, timeline_id, ch, until=base - 1) == [], "查询水位未按获知时间过滤"
        assert scope_ids(store, info, timeline_id, ch, until=base) == ["mm-t1"], "获知后应可见"
        return "happened/learned/recorded 三列独立且按获知时间过滤查询水位"


def spec_rank_and_brief() -> str:
    """§5.1：全文与向量先归一 / 秩融合（不直接乘不同量纲），同分稳定；简报按预算带线索。"""
    entries = [
        {"id": "e-1", "text": "铜铃响了三声", "learned_world": 0, "strength": 0.5},
        {"id": "e-2", "text": "渡口封了", "learned_world": 0, "strength": 0.5},
        {"id": "e-3", "text": "铜铃响过，渡口封了", "learned_world": 0, "strength": 0.5},
    ]
    ranked = memory_mod.rank(query="铜铃渡口", entries=entries, now_world=0, day_seconds=DAY)
    order = [row["id"] for row in ranked]
    assert order[0] == "e-3", f"双命中未排前：{order}"
    fused = memory_mod.rank(query="铜铃渡口", entries=entries, now_world=0, day_seconds=DAY,
                            vector_scores={"e-1": 0.9, "e-2": 0.1})
    fused_order = [row["id"] for row in fused]
    fusion_note = (
        "向量只覆盖部分条目时，未嵌入的 e-3 在融合里按 0.0 计（min-max 后等同该信号最差名次），"
        f"排序 {fused_order}"
    )
    tie = memory_mod.rank(query="", entries=entries[:2], now_world=0, day_seconds=DAY)
    assert [row["id"] for row in tie] == ["e-1", "e-2"], f"同分未按稳定标识排序：{[r['id'] for r in tie]}"
    low = memory_mod.pack_brief(
        [{"id": "m-1", "text": "她不太确定渡口是否封了。", "confidence": 0.4, "state": "active",
          "source_label": "听说", "learned_world": 0}], budget_tokens=900, limit=6)
    assert "不太确定" in low["text"] and "听说" in low["text"], f"简报丢了确信 / 来源线索：{low}"
    archived = memory_mod.pack_brief(
        [{"id": "m-2", "text": "水闸下的刻字。", "confidence": 0.9, "state": "archived",
          "source_label": "亲历", "learned_world": 0}], budget_tokens=900, limit=6)
    assert "模糊回想" in archived["text"], f"归档条目未带模糊表达：{archived}"
    huge = memory_mod.pack_brief(
        [{"id": f"m-{i}", "text": "很长的记忆" * 20, "confidence": 0.9, "state": "active",
          "source_label": "听说", "learned_world": 0} for i in range(6)], budget_tokens=200, limit=6)
    assert 0 < len(huge["ids"]) < 6, f"简报预算未生效：{huge['ids']}"
    return f"双命中优先={order}；同分稳定={[r['id'] for r in tie]}；低确信 / 归档线索齐全；预算 200 保留 {len(huge['ids'])} 行；{fusion_note}"


def spec_brief_not_shown() -> str:
    """§5.1 第 5 步 / §一：简报只进生成上下文，不展示给用户。"""
    with env() as (store, world, cfg, _root):
        info, timeline_id, ch = ready(store, world)
        mem(store, info, timeline_id, ch, "mm-brief", "堤禾记得旧码头的水位。")
        _svc, sent, _envs, _chan, _thread = run(drive(
            store, world, cfg, info, timeline_id, ch, ["旧码头的水位怎么样"], reply="水位还在涨。",
            env_ids=["e21-1"],
        ))
        session = store.session_ensure(info["id"], timeline_id, ch)
        context = world.turn_context(session, topic="旧码头的水位怎么样")
        assert "mm-brief" in context["memory_ids"], f"召回未进入本轮上下文：{context}"
        assert "旧码头的水位" in context["prompt"], "简报未注入生成上下文"
        shown = delivered_text(sent)
        assert "旧码头的水位" not in shown, f"简报泄漏到用户可见文本：{shown}"
        return f"上下文 memory_ids={context['memory_ids']}；投递正文={shown!r} 不含简报"


def spec_embedding_payload() -> str:
    """§5.2：向量化只发送该任务需要的文本（不上传整库 / 实例包 / 无关材料）。"""
    stub = EmbedStub(dim=4)
    try:
        with env(memory_embedding_model="stub", memory_embedding_base_url=stub.base_url,
                 memory_embedding_api_key="secret-key") as (store, world, cfg, _root):
            info, timeline_id, ch = ready(store, world)
            mem(store, info, timeline_id, ch, "mm-v1", "堤禾记得潮位刻线。")
            mem(store, info, timeline_id, ch, "mm-v2", "堤禾记得铜铃响了。")
            mem(store, info, timeline_id, ch, "mm-v3", "堤禾记得渡口封了。")
            out = run(world.embed_memories(info["id"], timeline_id, now_real=NOW, limit=3))
            assert out.get("embedded") == 3, f"补齐条数异常：{out}"
            sent_texts = [text for call in stub.calls for text in call["input"]]
            memory_texts = {str(row["text"]) for row in memory_rows(store, info, timeline_id, ch)}
            assert len(sent_texts) == 3, f"一次发送了 {len(sent_texts)} 条（应只发本批需要的最多 3 条）"
            assert set(sent_texts) <= memory_texts, f"发送了非记忆文本：{sent_texts}"
            package_blob = json.dumps(cfg.paths.root.as_posix())
            assert not any(package_blob in text for text in sent_texts), "发送了实例包 / 路径内容"
            assert stub.calls[0]["auth"].startswith("Bearer "), "凭据未按配置带上（独立配置项）"
            assert "secret-key" not in json.dumps(sent_texts), "凭据混进了正文"
            return f"首批发送 {len(sent_texts)} 条，均为本实例该线的记忆文本；未发送对话 / 经历 / 实例包"
    finally:
        stub.close()


def spec_embedding_fingerprint() -> str:
    """§5.2：模型指纹变化 → 旧向量退出混合召回并从源文本重建。"""
    stub = EmbedStub(dim=4)
    try:
        with env(memory_embedding_model="stub-a", memory_embedding_base_url=stub.base_url,
                 memory_embedding_api_key="k") as (store, world, _cfg, _root):
            info, timeline_id, ch = ready(store, world)
            mem(store, info, timeline_id, ch, "mm-f1", "堤禾记得潮位刻线。")
            mem(store, info, timeline_id, ch, "mm-f2", "堤禾记得铜铃响了。")
            run(world.embed_memories(info["id"], timeline_id, now_real=NOW, limit=8))
            rows = raw(store, "SELECT memory_id, model, dim FROM memory_embedding WHERE timeline_id=?", (timeline_id,))
            assert len(rows) == 2, f"指纹 A 落库异常：{rows}"
            switched = RuntimeService(
                store, **{**SVC, "memory_embedding_model": "stub-b",
                          "memory_embedding_base_url": stub.base_url, "memory_embedding_api_key": "k"},
            )
            query = run(switched.embed_query("潮位刻线", instance_id=info["id"], timeline_id=timeline_id))
            stale = store.memory_vector_scores(info["id"], timeline_id, ch, "潮位刻线",
                                               query_vector=query, model="stub-b")
            assert stale == {}, f"换模型后旧向量仍参与：{stale}"
            rebuilt = run(switched.embed_memories(info["id"], timeline_id, now_real=NOW, limit=8))
            assert rebuilt.get("embedded") == 2, f"未从源文本重建：{rebuilt}"
            rows2 = raw(store, "SELECT model, COUNT(*) n FROM memory_embedding WHERE timeline_id=? GROUP BY model",
                        (timeline_id,))
            assert dict(rows2).get("stub-b") == 2, f"重建后指纹不对：{rows2}"
            return f"换模型前 {len(rows)} 条；旧指纹评分={stale}；重建 {rebuilt}"
    finally:
        stub.close()


def spec_embedding_dim_rebuild() -> str:
    """§5.2 / 十一.5：同名模型换维度也要重建（旧向量退出混合召回，重建从源文本开始）。"""
    stub4 = EmbedStub(dim=4)
    stub8 = EmbedStub(dim=8)
    try:
        with env(memory_embedding_model="stub", memory_embedding_base_url=stub4.base_url,
                 memory_embedding_api_key="k") as (store, world, _cfg, _root):
            info, timeline_id, ch = ready(store, world)
            mem(store, info, timeline_id, ch, "mm-d1", "堤禾记得潮位刻线。")
            mem(store, info, timeline_id, ch, "mm-d2", "堤禾记得铜铃响了。")
            run(world.embed_memories(info["id"], timeline_id, now_real=NOW, limit=8))
            same_model_new_dim = RuntimeService(
                store, **{**SVC, "memory_embedding_model": "stub",
                          "memory_embedding_base_url": stub8.base_url, "memory_embedding_api_key": "k"},
            )
            pending = store.memory_missing_embeddings(info["id"], timeline_id, model="stub")
            out = run(same_model_new_dim.embed_memories(info["id"], timeline_id, now_real=NOW, limit=8))
            rows = dict(raw(store, "SELECT dim, COUNT(*) n FROM memory_embedding WHERE timeline_id=? GROUP BY dim",
                            (timeline_id,)))
            assert rows.get(8) == 2, (
                f"同名换维度未重建：dim 分布={rows}；embed_memories 返回 {out}；"
                f"memory_missing_embeddings(model='stub')（dim=0）返回 {len(pending)} 条 —— "
                f"服务端先按 model 查（dim=0 不过滤维度），查空即 return，第二段按真实维度补的重建永远走不到"
            )
            return f"维度分布={rows}；重建返回 {out}"
    finally:
        stub4.close()
        stub8.close()


def spec_no_notice_extraction() -> str:
    """§七：system_notice / 平台错误 / 管理诊断不得触发记忆提取。"""
    with env() as (store, world, cfg, _root):
        info, timeline_id, ch = ready(store, world)
        session = store.session_ensure(info["id"], timeline_id, ch)
        store.session_notice_put({
            "session_id": str(session["id"]), "instance_id": info["id"], "timeline_id": timeline_id,
            "kind": "boot", "message_id": "m-notice", "created_real": time.time(),
        })
        # 生成失败（平台错误）：不留下任何记忆来源
        _svc, _sent, _envs, _chan, _thread = run(drive(
            store, world, cfg, info, timeline_id, ch, ["在吗"],
            llm=FakeLLM(["x"], fail_with=LLMError("platform_down", "平台错误", retryable=True)),
            env_ids=["e25-1"],
        ))
        tasks = store.memory_tasks(info["id"], timeline_id)
        assert tasks == [], (
            f"通知 / 平台错误被登记成了记忆来源：{[(t['source_kind'], t['source_ref']) for t in tasks]}"
        )
        assert memory_rows(store, info, timeline_id, ch) == [], "通知 / 平台错误产生了记忆"
        # 诊断（管理面）不消费也不强化
        mem(store, info, timeline_id, ch, "mm-diag", "堤禾记得潮位刻线。", strength=0.5)
        world.budget_view(info["id"])
        world.commits(info["id"], timeline_id)
        world.view(info["id"], timeline_id, now_real=NOW + 2 * DAY)
        row = store.memory_get("mm-diag", instance_id=info["id"], timeline_id=timeline_id)
        assert abs(float(row["strength"]) - 0.5) < 1e-9, "技术诊断强化了记忆"
        return f"通知 / 失败轮未产生记忆来源；诊断后强度仍 {row['strength']}"


def spec_one_source_one_memory() -> str:
    """§4.1 / §三：同一来源的一次提取结果里，事实与承诺都应入库（同源幂等不等于一次只留一条）。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        world.queue_dialog_turn(info["id"], timeline_id, ch, world_seconds=watermark(store, timeline_id),
                                user_ref="env-multi", user_text="记得把牌子还回去",
                                reply_message_id="m-multi", reply_text="我会还回去的")
        task = [row for row in store.memory_tasks(info["id"], timeline_id) if "env-multi" in str(row["source_ref"])][0]
        llm = ScriptLLM([entries([
            {"text": "联络者提过把牌子还回去。", "kind": "fact", "ref": task["id"], "strength": 0.6, "confidence": 0.7},
            {"text": "她打算把牌子还回去。", "kind": "promise", "ref": task["id"], "strength": 0.7, "confidence": 0.8},
        ])])
        out = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        rows = memory_rows(store, info, timeline_id, ch)
        kinds = sorted(str(row["kind"]) for row in rows)
        assert out["written"] == 2 and kinds == ["fact", "promise"], (
            f"同一次提取里的第二条被当成「同源重复」丢掉：写入={out['written']} kinds={kinds}"
            f"（memory_add 按 source_key=任务 id 去重，一个来源最多一条）"
        )
        return f"写入 {out['written']} 条 kinds={kinds}"


def spec_organize_versioning() -> str:
    """§六：整理结果固化并版本化，只调表达、不重复改写同一段过去。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        original = ("堤禾记得那天在渡口等了很久，风把她的斗篷吹得鼓起来，她一路走一路在心里把要说的话来回过了"
                    "好几遍，连开场白都换了三种说法，最后还是没能开口，只把手里那根断了的绳结反复搓了一遍又一遍。")
        mem(store, info, timeline_id, ch, "mm-org", original, strength=0.6, source_key="mt-org")
        set_watermark(store, timeline_id, DAY * 1503)
        short = "她在渡口等了很久，终究没开口。"
        llm = ScriptLLM([short])
        out = run(world.organize_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 3 * DAY))
        assert out["organized"] == 1, f"整理未落库：{out}"
        rows = {str(row["id"]): row for row in memory_rows(store, info, timeline_id, ch)}
        new_id = str(out["items"][0]["to"])
        new_row = rows[new_id]
        assert str(new_row["id"]) != "mm-org"
        assert str(rows["mm-org"]["superseded_by"] or "") == new_id and int(new_row["version"]) > 1, (
            f"整理结果未与原文建立版本关系：原文 superseded_by={rows['mm-org']['superseded_by']} "
            f"state={rows['mm-org']['state']} version={rows['mm-org']['version']}；"
            f"整理条目 version={new_row['version']} supersedes={new_row['supersedes']}"
        )
        assert str(rows["mm-org"]["state"]) != "active", "整理后旧条目仍在普通召回里"
        hits = recall_ids(world, info, timeline_id, ch, topic="渡口等了很久")
        assert new_id in hits and "mm-org" not in hits, f"同一段过去被两份条目同时召回：{hits}"
        calls_after_first = len(llm.calls)
        run(world.organize_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 4 * DAY))
        assert len(llm.calls) == calls_after_first, (
            f"补算重复调用生成器改写同一段过去：{calls_after_first}→{len(llm.calls)}"
        )
        return f"整理条目={new_id}；原文 state={rows['mm-org']['state']} superseded_by={rows['mm-org']['superseded_by']}；召回={hits}"


def spec_no_memory_browse() -> str:
    """§一 / §7：用户界面不提供记忆浏览、逐条编辑或撤回披露的入口。"""
    ops = sorted(ROOT.joinpath("isekai_core/world/ops.py").read_text(encoding="utf-8").split("SYNC_OPS = frozenset")[1]
                 .split("ASYNC_OPS = frozenset")[0].split("}")[0].splitlines())
    op_names = [line.strip().strip('",') for line in ops if line.strip().startswith('"')]
    memory_ops = [name for name in op_names if "memor" in name or "disclos" in name]
    # 只禁「浏览 / 逐条编辑记忆」的入口；embedding 配置组（memory_embedding_*）是 §3.3 要求的设置面，
    # 先把这些键从文本里剔掉再找浏览标记，避免「配了向量化＝能看记忆」的误判。
    browse_markers = ("memory.list", "memory.get", "memory.browse", "memory.edit", "memories",
                      "记忆库", "查看记忆", "编辑记忆")
    desktop_hits = []
    for path in ROOT.joinpath("desktop").rglob("*"):
        if not path.is_file() or path.suffix not in {".ts", ".tsx", ".html"}:
            continue
        if "node_modules" in path.as_posix() or "target" in path.parts:
            continue
        text = re.sub(r"memory_embedding[a-z_]*", "", path.read_text(encoding="utf-8", errors="ignore"))
        if any(marker in text for marker in browse_markers):
            desktop_hits.append(str(path.relative_to(ROOT)))
    assert memory_ops == ["disclose.confirm", "disclose.list", "disclose.suggest"], \
        f"管理面出现了记忆明文入口：{memory_ops}"
    assert desktop_hits == [], f"界面出现了记忆相关入口：{desktop_hits}"
    extract = ROOT.joinpath("isekai_core/world/ops.py").read_text(encoding="utf-8")
    assert "不回传任何记忆内容" in extract, "提取入口的返回约定不见了"
    # 名字带 disclose 的第三条是 NARRATIVE_LAYER §9.4 的**披露候选**：它只挑出「对方讲过、用户已看过」
    # 的会话原文摆给人选，授权仍走 disclose.confirm。既然按名字它算披露族，就按实现核一遍：
    # 候选构造不许读记忆表、世界实情或认知切片（否则「候选」就成了旁路的浏览入口）。
    service_text = ROOT.joinpath("isekai_core/runtime/service.py").read_text(encoding="utf-8")
    body = service_text.split("def disclosure_candidates", 1)[1].split("\n    def ", 1)[0]
    forbidden = [name for name in ("memory_", "knowledge_", "claim_", "truth", "cognition")
                 if name in body]
    assert forbidden == [], f"披露候选读了不该读的表：{forbidden}"
    return (f"管理面记忆相关操作={memory_ops}（两条披露授权 + 一条候选挑选，候选只读已固化原文）；"
            f"桌面端无记忆字样；提取入口按约定不回传内容")


def spec_frozen_dialog_not_blocked() -> str:
    """§5.2 / 十一.5：一次未嵌入不改写已固化内容，且向量补齐失败不阻断会话链路。"""
    stub = EmbedStub(dim=4, fail=True)
    try:
        with env(memory_embedding_model="stub", memory_embedding_base_url=stub.base_url,
                 memory_embedding_api_key="k") as (store, world, cfg, _root):
            info, timeline_id, ch = ready(store, world)
            mem(store, info, timeline_id, ch, "mm-fail", "堤禾记得旧码头的水位。")
            out = run(world.embed_memories(info["id"], timeline_id, now_real=NOW, limit=8))
            assert out.get("embedded") == 0 and "error" in out, f"请求失败未如实上报：{out}"
            assert raw(store, "SELECT memory_id FROM memory_embedding WHERE timeline_id=?", (timeline_id,)) == [], (
                "请求失败仍写了向量"
            )
            _svc, sent, _envs, _chan, _thread = run(drive(
                store, world, cfg, info, timeline_id, ch, ["旧码头的水位"], reply="水位还在涨。",
                env_ids=["e30-1"],
            ))
            assert "水位还在涨。" in delivered_text(sent), "向量失败阻断了已固化回复的投递"
            assert recall_ids(world, info, timeline_id, ch, topic="旧码头的水位"), "全文召回不可用"
            return f"503 → {out}；随后一轮照常固化并投递；全文召回可用"
    finally:
        stub.close()


def spec_remote_notice() -> str:
    """§5.2：首次配置远程服务时说明会发送所需文本用于生成 / 向量化。"""
    surfaces = [
        ROOT / "isekai_core/cli.py", ROOT / "isekai_core/world_cli.py", ROOT / "README.md",
        ROOT / "config/README.md", ROOT / "config/config.example.yaml",
    ]
    surfaces += [p for p in ROOT.joinpath("desktop/src").rglob("*")
                 if p.is_file() and p.suffix in {".ts", ".tsx", ".html"}]
    needles = ("发送到远程", "会上传", "上传到远程", "上传到该", "会发送", "发送所需", "会把这部分文本",
               "远程 API 发送", "会传给", "发出请求即表示")
    hits = []
    for path in surfaces:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(needle in text for needle in needles):
            hits.append(str(path.relative_to(ROOT)))
    config_hint = (ROOT / "config/config.example.yaml").read_text(encoding="utf-8", errors="ignore")
    assert hits, (
        "面向用户的「首次配置远程 embedding 会发送所需文本」说明缺失："
        f"已检索 {len(surfaces)} 个界面 / 文档面（cli.py、world_cli.py、desktop/src、README、"
        f"config/README.md、config/config.example.yaml），均无此类提示；"
        f"config.example.yaml 里最接近的一句只是格式说明："
        f"「{config_hint.splitlines()[33].strip() if len(config_hint.splitlines()) > 33 else ''}」"
    )
    return f"首次配置说明出现在：{hits}"


def spec_partial_answer_settles() -> str:
    """§4.1 / 十一.13：提取结果里没提到的来源要被结算，不能每个周期都重新调模型处理同一来源。"""
    with env() as (store, world, _cfg, _root):
        info, timeline_id, ch = ready(store, world)
        world.queue_dialog_turn(info["id"], timeline_id, ch, world_seconds=watermark(store, timeline_id),
                                user_ref="env-part", user_text="你今天做了什么",
                                reply_message_id="m-part", reply_text="我在堤上待着")
        tasks = store.memory_tasks(info["id"], timeline_id)
        assert len(tasks) == 2, f"夹具来源数不对：{len(tasks)}"
        target = [row for row in tasks if str(row["source_ref"]).startswith("user:")][0]
        llm = ScriptLLM([entry("联络者问过她今天做了什么。", target["id"])])
        first = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        left = store.memory_tasks(info["id"], timeline_id)
        second = run(world.extract_memories(info["id"], timeline_id, llm=llm, now_real=NOW + 2 * DAY))
        prompts = [json.dumps(call, ensure_ascii=False) for call in llm.calls]
        assert len(llm.calls) == 1 and not left and second["calls"] == 0, (
            f"回答里没提到的来源未被结算：首轮写 {first['written']} 条，仍待处理 "
            f"{[(r['source_ref'], r['attempts']) for r in left]}；下一周期又调了一次模型 "
            f"（calls={len(llm.calls)}，两次提示词相同={prompts[0] == prompts[1] if len(prompts) > 1 else 'n/a'}）"
        )
        return f"首轮 written={first['written']}；待处理剩余={len(left)}；第二周期 calls={second['calls']}"


def spec_embedding_batch_rebuild() -> str:
    """§5.2：一批补齐没覆盖完的条目，补建路径要能落库（不能把整轮补齐打断）。"""
    stub = EmbedStub(dim=4)
    try:
        with env(memory_embedding_model="stub", memory_embedding_base_url=stub.base_url,
                 memory_embedding_api_key="k") as (store, world, _cfg, _root):
            info, timeline_id, ch = ready(store, world)
            for index, text in enumerate(("堤禾记得潮位刻线。", "堤禾记得铜铃响了三次。", "堤禾记得渡口的灯换了。")):
                mem(store, info, timeline_id, ch, f"mm-b{index}", text)
            out: Any = None
            error = ""
            try:
                out = run(world.embed_memories(info["id"], timeline_id, now_real=NOW, limit=2))
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
            rows = raw(store, "SELECT memory_id, dim FROM memory_embedding WHERE timeline_id=?", (timeline_id,))
            assert not error, (
                f"补齐一批后（limit=2，余 1 条）补建路径抛错：{error}；"
                f"落库 {len(rows)} 条；返回 {out}。service.py:1008-1015 调 memory_embedding_put("
                f"..., source_version=...) 但 store.py:2865 的参数名是 content_hash → TypeError"
            )
            assert len(rows) == 3, f"补齐未覆盖剩余条目：{rows}"
            return f"limit=2 时返回 {out}，最终落库 {len(rows)} 条"
    finally:
        stub.close()


CHECKS: list[tuple[str, str, Callable[[], str], str]] = [
    ("十一.1", "同实例同线双角色默认互不可见；跨实例不串读", accept1, "isekai_core/store.py:2605 memory_scope"),
    ("十一.2", "分叉继承共同过去、不继承后续记忆", accept2, "isekai_core/runtime/service.py:526 fork"),
    ("十一.2", "回滚后旧向量物理残留也不得被命中", accept2b, "isekai_core/store.py:2802 memory_vector_scores"),
    ("十一.2", "提取等待期间回滚 → 迟到结果不写回", accept2c, "isekai_core/runtime/service.py:866-895 写入段"),
    ("十一.3", "自述不变亲历、传闻留来源", accept3, "isekai_core/runtime/memory.py:272 source_label"),
    ("十一.4", "矛盾不被去重、纠正留替代链、当时水位可读旧版本", accept4, "isekai_core/store.py:2616 memory_add"),
    ("十一.5", "缺配置 / 失败 / 维度改变 / 无缓存 → 全文召回", accept5, "isekai_core/runtime/service.py:945 embed_memories"),
    ("十一.6", "重复提取 / 重复召回 / 重放不重复写或强化", accept6, "isekai_core/store.py:2759 memory_cite"),
    ("十一.7", "衰减小步=批量、冻结不衰减、归档唤起受限", accept7, "isekai_core/store.py:2836 memory_decay"),
    ("十一.7", "整理挂作息节律（无睡眠角色按世界日界）", accept7b, "isekai_core/runtime/service.py:1077-1083"),
    ("十一.8", "导出导入恢复三层内容与引用、无密钥、无明细入口", accept8, "isekai_core/world/portable.py:40 build_container"),
    ("十一.9", "合并批共享回复按来源结算、不重复提取", accept9, "isekai_core/runtime/service.py:640 queue_dialog_turn"),
    ("十一.9", "冻结 / 归档后迟到任务不写入", accept9b, "isekai_core/runtime/service.py:791 extract_memories"),
    ("十一.10", "召回与重复引用不提高采信、同源不算独立佐证", accept10, "isekai_core/store.py:2759 memory_cite"),
    ("十一.11", "打算跨重启保留、未执行不产生亲历 / 义务", accept11, "isekai_core/store.py:3104 intent_put"),
    ("十一.12", "旧话题可回接且仍受隔离限制", accept12, "isekai_core/runtime/service.py:1147 recall"),
    ("十一.13", "预算耗尽不阻塞、向量待处理、重试不绕过", accept13, "isekai_core/runtime/service.py:1247 reserve_call"),
    ("十一.14", "立即 / 延迟提交在同水位强度等价、三时间戳独立", accept14, "isekai_core/runtime/service.py:869-891"),
    ("十一.14", "迟到提取不得覆盖期间已固化的纠正", accept14b, "isekai_core/store.py:2635-2662 memory_add"),
    ("§4.1", "不合规提取结果整条丢弃、失败保留待处理", spec_extraction_validation, "isekai_core/runtime/memory.py:199"),
    ("§4.1", "提取提示只含该角色已接触材料", spec_prompt_scope, "isekai_core/runtime/service.py:719 _source_material"),
    ("§三", "发生 / 获知 / 记录三时间戳不混用", spec_three_timestamps, "isekai_core/store.py:2605 memory_scope"),
    ("§5.1", "归一 / 秩融合、同分稳定、简报预算与线索", spec_rank_and_brief, "isekai_core/runtime/memory.py:96 rank"),
    ("§5.1", "简报只进生成上下文、不展示给用户", spec_brief_not_shown, "isekai_core/runtime/service.py:898 turn_context"),
    ("§5.2", "向量化只发该任务需要的文本", spec_embedding_payload, "isekai_core/runtime/embedding.py:38 embed"),
    ("§5.2", "模型指纹变化 → 旧向量退出 + 从源文本重建", spec_embedding_fingerprint, "isekai_core/store.py:2898"),
    ("§5.2", "同名换维度 → 旧向量退出 + 从源文本重建", spec_embedding_dim_rebuild, "isekai_core/runtime/service.py:996-1016"),
    ("§5.2", "向量失败不改写已固化内容、不阻断链路", spec_frozen_dialog_not_blocked, "isekai_core/runtime/service.py:945"),
    ("§5.2", "首次配置远程服务时说明会发送文本", spec_remote_notice, "（界面 / 文档层）"),
    ("§七", "通知 / 平台错误 / 诊断不触发提取或强化", spec_no_notice_extraction, "isekai_core/session.py:449 _settle_memory"),
    ("§4.1", "一次提取的多条不同条目都应入库（同源幂等边界）", spec_one_source_one_memory, "isekai_core/store.py:2622 source_key 去重"),
    ("§4.1", "回答里未提到的来源要被结算（不重复调模型）", spec_partial_answer_settles, "isekai_core/runtime/service.py:855-865"),
    ("§5.2", "分批补齐的补建路径要能落库", spec_embedding_batch_rebuild, "isekai_core/runtime/service.py:1007-1016"),
    ("§六", "整理结果固化并版本化、不重复改写同一段过去", spec_organize_versioning, "isekai_core/runtime/service.py:1103-1118"),
    ("§一 / §七", "无记忆浏览 / 编辑入口", spec_no_memory_browse, "isekai_core/world/ops.py SYNC_OPS"),
]

COUNT = {"PASS": 0, "FAIL": 0, "DEFERRED": 0}


def report(status: str, clause: str, summary: str, evidence: str) -> None:
    COUNT[status] = COUNT.get(status, 0) + 1
    print(f"{status} [{clause}] {summary}\n    证据：{evidence}", flush=True)


def main() -> int:
    filters = [item for item in sys.argv[1:] if not item.startswith("-")]
    print(f"# MEMORY_SPEC 独立行为探针（第二轮）@ {ROOT}", flush=True)
    print("# 真 SQLite + 真 SessionService 链路；本机 embedding 桩；FakeLLM / 脚本模型", flush=True)
    if filters:
        print(f"# 过滤：{filters}", flush=True)
    for clause, summary, fn, site in CHECKS:
        if filters and not any(word in clause or word in summary for word in filters):
            continue
        try:
            evidence = fn()
        except AssertionError as exc:
            report("FAIL", clause, summary, f"{exc} ｜ 实现位置 {site}")
            continue
        except Exception:  # 探针自身异常如实上报
            tail = traceback.format_exc().strip().splitlines()[-1]
            report("FAIL", clause, summary, f"探针异常：{tail} ｜ 实现位置 {site}")
            continue
        report("PASS", clause, summary, f"{evidence} ｜ 实现位置 {site}")
    total = COUNT["PASS"] + COUNT["FAIL"] + COUNT["DEFERRED"]
    print(f"TOTAL {total} PASS {COUNT['PASS']} FAIL {COUNT['FAIL']} DEFERRED {COUNT['DEFERRED']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
