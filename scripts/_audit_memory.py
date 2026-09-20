#!/usr/bin/env python
"""docs/MEMORY_SPEC.md 行为级审计探针（十一、行为验收 + 正文相关条款）。

只读项目代码、只在临时目录里建库；不打真实 embedding / 真实 LLM
（用本地 HTTP 桩 + 脚本化 FakeLLM）。不启动核心进程，不碰 data/isekai.db。

用法： .venv/Scripts/python.exe scripts/_audit_memory.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
import threading
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from isekai_core.runtime import memory as memory_mod  # noqa: E402
from isekai_core.runtime.service import RuntimeService  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.world import ops as ops_mod  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402
from isekai_core.world.portable import build_container, import_instance  # noqa: E402
from samples import DAY, sample_card, sample_package  # noqa: E402

NOW = 1.7e9
SVC = dict(
    instance_tokens_per_day=400_000,
    timeline_tokens_per_day=150_000,
    task_tokens_per_day=60_000,
    autocommit_enabled=False,
)

COUNT = {"PASS": 0, "FAIL": 0, "DEFERRED": 0}


# ---------- 桩：本地 embedding（OpenAI 兼容） ----------

class _Stub:
    DIM = 4
    fail = False
    calls: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            _Stub.calls.append({
                "path": self.path, "model": payload.get("model"),
                "input": payload.get("input") or [], "auth": self.headers.get("Authorization") or "",
            })
            if _Stub.fail:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"{}")
                return
            data = [
                {"index": index, "embedding": _Stub.vector(str(text), _Stub.DIM)}
                for index, text in enumerate(payload.get("input") or [])
            ]
            body = json.dumps({"data": data, "model": payload.get("model")}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    @staticmethod
    def vector(text: str, dim: int) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [(digest[index % len(digest)] / 255.0) - 0.5 for index in range(dim)]
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return [round(value / norm, 6) for value in values]

    def __init__(self, dim: int = 4) -> None:
        _Stub.DIM, _Stub.fail, _Stub.calls = dim, False, []
        self.server = HTTPServer(("127.0.0.1", 0), _Stub.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---------- 夹具 ----------

@contextmanager
def env(**over: Any):
    root = Path(tempfile.mkdtemp(prefix="audit-memory-"))
    store = Store(root / "data" / "isekai.db")
    store.ensure_schema()
    world = RuntimeService(store, **{**SVC, **over})
    try:
        yield store, world, root / "data" / "isekai.db"
    finally:
        store.close()
        shutil.rmtree(root, ignore_errors=True)


def ready(store: Store, world: RuntimeService, *, days: int = 2) -> tuple[dict, str, str]:
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


def mem(store: Store, info: dict, timeline_id: str, character_id: str, ident: str, text: str, **over: Any) -> Any:
    row = {
        "id": ident, "instance_id": info["id"], "timeline_id": timeline_id, "character_id": character_id,
        "text": text, "kind": "fact", "sources": [{"kind": "claim", "ref": f"cl-{ident}"}],
        "happened_world": 0, "learned_world": 0, "recorded_world": 0, "semantic_watermark": 0,
        "strength": 0.6, "confidence": 0.8,
    }
    row.update(over)
    return store.memory_add(row)


def say(store: Store, info: dict, timeline_id: str, character_id: str, *, env_id: str, world_seconds: int,
        text: str, reply: str) -> str:
    session = store.session_ensure(info["id"], timeline_id, character_id)
    store.inbound_put(session_id=session["id"], channel_id="cli-dev", thread_id=f"t-{character_id}",
                      env_id=env_id, text=text, binding_version=1)
    out = store.outbound_put(session_id=session["id"], message_id=f"m-{env_id}", reply_to=env_id, covers=[env_id],
                             batches=[[reply]], target_channel="cli-dev", target_thread=f"t-{character_id}",
                             binding_version=1, binding_token="tok")
    return str(out["message_id"])


def turn(store: Store, world: RuntimeService, info: dict, timeline_id: str, character_id: str, text: str, *,
         world_seconds: int, ref: str, reply_ref: str = "", reply: str = "知道了") -> int:
    return world.queue_dialog_turn(
        info["id"], timeline_id, character_id, world_seconds=world_seconds, user_ref=ref, user_text=text,
        reply_message_id=reply_ref or f"m-{ref}", reply_text=reply,
    )


class ScriptLLM:
    """脚本化假模型：记录调用（用于检查提示词里出现了什么）。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, *, max_tokens=None, timeout=None, temperature=None) -> str:
        self.calls.append(messages)
        if not self.replies:
            raise RuntimeError("模型不可用")
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


def entry(text: str, ref: Any, **over: Any) -> str:
    row = {"text": text, "kind": "fact", "ref": str(ref), "strength": 0.6, "confidence": 0.8}
    row.update(over)
    return json.dumps([row], ensure_ascii=False)


def watermark(store: Store, timeline_id: str) -> int:
    return int(store.clock_get(timeline_id)["processed_world"])


def sql_int(store: Store, sql: str, args: tuple = ()) -> int:
    return int(store._conn.execute(sql, args).fetchone()[0])


def ids(scope: list[dict]) -> list[str]:
    return [str(row["id"]) for row in scope]


# ---------- 逐条审计 ----------

def accept1() -> str:
    """验收1：同实例同线双角色默认互不可见；不同实例、不同线亦不串读。"""
    with env() as (store, world, _):
        info, tl, ch1 = ready(store, world)
        ch2 = add_character(store, world, info, tl, "堤砚")
        commit = world.commit(info["id"], tl, note="基线")
        mem(store, info, tl, ch1, "mm-a1", "她答应过替堤长压着那份信报", kind="promise", strength=0.9)
        branch = world.fork(info["id"], tl, commit_id=commit["id"], name="分线")
        tl2 = str(branch["timeline"]["id"])
        other_character = world.recall(info["id"], tl, ch2, topic="信报 堤长")
        other_line = world.recall(info["id"], tl2, ch1, topic="信报 堤长")
        info2, tl_2, ch_2 = ready(store, world, days=1)
        other_instance = world.recall(info2["id"], tl_2, ch_2, topic="信报 堤长")
        own = world.recall(info["id"], tl, ch1, topic="信报")
        assert own["ids"] == ["mm-a1"], f"本角色应召回到自己的条目：{own['ids']}"
        assert other_character["entries"] == [], f"同线另一角色读到 A 的记忆：{other_character['entries']}"
        assert other_line["entries"] == [], f"分叉线读到原线后续记忆：{other_line['entries']}"
        assert other_instance["entries"] == [], f"另一实例读到：{other_instance['entries']}"
        return (f"本角色 {own['ids']}；同线另一角色 {other_character['ids']}；分叉线 {other_line['ids']}；"
                f"另一实例 {other_instance['ids']}")


def accept2() -> str:
    """验收2：分叉继承共同过去、不继承后续；回滚后旧向量与迟到提取不复活未来内容。"""
    stub = _Stub()
    try:
        with env(memory_embedding_model="stub-embed", memory_embedding_base_url=stub.base_url,
                 memory_embedding_api_key="k") as (store, world, _):
            info, tl, ch = ready(store, world)
            mem(store, info, tl, ch, "mm-old", "潮位到了刻线", decay_world=watermark(store, tl))
            asyncio.run(world.embed_memories(info["id"], tl, now_real=NOW))
            commit = world.commit(info["id"], tl, note="回滚点")
            mem(store, info, tl, ch, "mm-new", "堤长身故，接任未毕", decay_world=watermark(store, tl))
            asyncio.run(world.embed_memories(info["id"], tl, now_real=NOW))
            emb_before = sql_int(store, "SELECT COUNT(*) FROM memory_embedding WHERE timeline_id=?", (tl,))

            branch = world.fork(info["id"], tl, commit_id=commit["id"], name="分叉")
            tl2 = str(branch["timeline"]["id"])
            fork_ids = ids(store.memory_scope(info["id"], tl2, ch))
            before = float(store.memory_get("mm-old", instance_id=info["id"], timeline_id=tl)["strength"])
            world.cite_memories(info["id"], tl2, ch, turn_id="m-fork", memory_ids=["mm-old"], world_seconds=0)
            after = float(store.memory_get("mm-old", instance_id=info["id"], timeline_id=tl)["strength"])
            fork_after = float(store.memory_get("mm-old", instance_id=info["id"], timeline_id=tl2)["strength"])

            world.rollback(info["id"], tl, commit_id=commit["id"], now_real=NOW + 6 * DAY)
            rolled = store.memory_get("mm-new", instance_id=info["id"], timeline_id=tl)
            kept = store.memory_get("mm-old", instance_id=info["id"], timeline_id=tl)
            emb_after = sql_int(store, "SELECT COUNT(*) FROM memory_embedding WHERE timeline_id=?", (tl,))
            scores = store.memory_vector_scores(info["id"], tl, ch, "堤长", query_vector=stub.vector("堤长", 4),
                                               model="stub-embed")
            recalled = world.recall(info["id"], tl, ch, topic="堤长 身故")

            assert fork_ids == ["mm-old"], f"分叉应只继承共同过去：{fork_ids}"
            assert after == before and fork_after > before, f"分叉与原线共享可变对象：{before}→{after}（分叉 {fork_after}）"
            assert rolled is None and kept is not None, f"回滚覆盖面：mm-new={rolled} mm-old={kept}"
            assert emb_after == 0, f"回滚后旧向量仍物理存在：{emb_before}→{emb_after}"
            assert "mm-new" not in scores, f"回滚后旧向量仍参与评分：{scores}"
            assert all(str(row["id"]) != "mm-new" for row in recalled["entries"]), "回滚后未来内容仍被召回"
            return (f"分叉={fork_ids}；原线强度 {before}→{after} / 分叉内 {fork_after}；"
                    f"回滚后 mm-new={rolled}、向量行 {emb_before}→{emb_after}、召回={recalled['ids']}")
    finally:
        stub.close()


def accept3() -> str:
    """验收3：随口说的往事不会变成亲历；传闻不变真相、高确信仍留来源。"""
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        turn(store, world, info, tl, ch, "你以前来过这儿吗", world_seconds=watermark(store, tl),
             ref="env-1", reply_ref="m-1")
        reply_task = [t for t in store.memory_tasks(info["id"], tl) if t["source_ref"].startswith("reply:")][0]
        events_before = sql_int(store, "SELECT COUNT(*) FROM event WHERE timeline_id=?", (tl,))
        llm = ScriptLLM([entry("她说自己小时候在堤上住过，见过潮位到刻线上面",
                               reply_task["id"], kind="fragment", confidence=0.9)])
        asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW))
        rows = store.memory_scope(info["id"], tl, ch)
        sources = json.loads(rows[0]["sources"])
        label = memory_mod.source_label(sources)
        events_after = sql_int(store, "SELECT COUNT(*) FROM event WHERE timeline_id=?", (tl,))
        rumor = mem(store, info, tl, ch, "mm-rumor", "听说堤长私吞了修堤粮",
                    sources=[{"kind": "claim", "ref": "cl-r", "via": "驿站"}], confidence=0.95)
        assert sources[0]["kind"] == "dialog" and sources[0]["source_role"] == "character", f"来源标错：{sources}"
        assert label != "亲历", f"她随口的往事被记成亲历：{label}"
        assert events_after == events_before, "记忆反写世界事件（说了某事 = 该事真实发生）"
        assert memory_mod.source_label(json.loads(rumor["sources"])) == "听说", "传闻没保住「听说」来源"
        assert float(rumor["confidence"]) == 0.95, f"高确信被抹平：{rumor['confidence']}"
        assert memory_mod.source_label([{"kind": "experience", "ref": "ex"}]) == "亲历"
        return (f"自述来源={sources} → 标签「{label}」；事件数 {events_before}→{events_after}；"
                f"传闻标签「听说」置信 0.95；亲历来源仍标「亲历」")


def accept4() -> str:
    """验收4：相似但矛盾的陈述不被去重吞掉；纠正留替代链，过去 / 当前分版。"""
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        mem(store, info, tl, ch, "mm-first", "堤长身故，接任推举未毕", strength=0.8, confidence=0.9)
        second = mem(store, info, tl, ch, "mm-second", "堤长没有身故，接任推举未毕", strength=0.7, confidence=0.7,
                     happened_world=DAY, learned_world=DAY, recorded_world=DAY, semantic_watermark=DAY)
        count = store.memory_count(info["id"], tl, ch)
        first = store.memory_get("mm-first", instance_id=info["id"], timeline_id=tl)
        now_view = [r["id"] for r in store.memory_scope(info["id"], tl, ch, until=DAY * 10)
                    if r["superseded_by"] is None]
        then_view = ids(store.memory_scope(info["id"], tl, ch, until=0))
        dup = mem(store, info, tl, ch, "mm-dup", "堤长身故，接任推举未毕", strength=0.9)
        assert count == 2, f"矛盾陈述被去重吞掉：{count}"
        assert second is not None and first["superseded_by"] == "mm-second", f"替代链缺失：{first}"
        assert first["state"] == "archived" and first["text"] == "堤长身故，接任推举未毕", "旧说法被静默覆写"
        assert now_view == ["mm-second"], f"当前认知应只剩有效版本：{now_view}"
        assert then_view == ["mm-first"], f"当时水位应读到当时的说法：{then_view}"
        assert dup is None, "同事实去重失效（重复条目被写入）"
        return (f"条目数={count}；替代链 mm-first→{first['superseded_by']}（state={first['state']}）；"
                f"当前认知={now_view}；水位 0 时={then_view}；同事实重复写入={dup}")


def accept5a() -> str:
    """验收5：embedding 缺配置 / 请求失败时退化为全文召回，不阻断世界推进。"""
    parts = []
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        mem(store, info, tl, ch, "mm-a", "潮位到了刻线")
        assert world.embedding_ready is False
        skipped = asyncio.run(world.embed_memories(info["id"], tl, now_real=NOW))
        recalled = world.recall(info["id"], tl, ch, topic="潮位")
        advanced = world.advance(info["id"], tl, now_real=NOW + 4 * DAY)
        assert skipped == {"embedded": 0, "skipped": "not_configured"}, skipped
        assert recalled["ids"] == ["mm-a"], f"全文召回失效：{recalled['ids']}"
        assert advanced["state"] == "current" and advanced["processed_world"] > watermark(store, tl) - DAY, advanced
        parts.append(f"缺配置 → {skipped}；全文召回 {recalled['ids']}；世界推进 {advanced['state']}@"
                     f"{advanced['processed_world']}")
    stub = _Stub()
    try:
        with env(memory_embedding_model="stub-embed", memory_embedding_base_url=stub.base_url,
                 memory_embedding_api_key="k") as (store, world, _):
            info, tl, ch = ready(store, world)
            mem(store, info, tl, ch, "mm-b", "船到了")
            _Stub.fail = True
            failed = asyncio.run(world.embed_memories(info["id"], tl, now_real=NOW))
            missing = store.memory_missing_embeddings(info["id"], tl, model="stub-embed")
            query = asyncio.run(world.embed_query("船", instance_id=info["id"], timeline_id=tl))
            recalled = world.recall(info["id"], tl, ch, topic="船")
            assert failed["embedded"] == 0 and "503" in failed["reason"], failed
            assert missing and query is None, f"待嵌入={len(missing)} 查询向量={query}"
            assert recalled["ids"] == ["mm-b"], f"全文召回失效：{recalled['ids']}"
            parts.append(f"桩 503 → {failed['reason']}；待嵌入 {len(missing)} 条；查询向量 {query}；"
                         f"全文召回 {recalled['ids']}")
    finally:
        stub.close()
    return "；".join(parts)


def accept5b() -> str:
    """验收5：查询向量维度改变时旧向量不参与混合召回，全文召回照常。"""
    stub = _Stub(dim=4)
    try:
        with env(memory_embedding_model="stub-embed", memory_embedding_base_url=stub.base_url,
                 memory_embedding_api_key="k") as (store, world, _):
            info, tl, ch = ready(store, world)
            mem(store, info, tl, ch, "mm-d", "潮位到了刻线")
            first = asyncio.run(world.embed_memories(info["id"], tl, now_real=NOW))
            _Stub.DIM = 8  # 模型名没变但维度变了（指纹里的维度部分）
            probe = stub.vector("潮位", 8)
            scores = store.memory_vector_scores(info["id"], tl, ch, "潮位", query_vector=probe, model="stub-embed")
            recalled = world.recall(info["id"], tl, ch, topic="潮位", query_vector=probe)
            again = asyncio.run(world.embed_memories(info["id"], tl, now_real=NOW))
            assert first["embedded"] == 1, first
            assert scores == {}, f"维度不符的旧向量仍参与评分：{scores}"
            assert recalled["ids"] == ["mm-d"], f"全文召回失效：{recalled['ids']}"
            return (f"落库 {first}；维度 4→8 的查询评分 {scores}（旧向量已退出）；全文召回 {recalled['ids']}；"
                    f"重建尝试 {again}")
    finally:
        stub.close()


def accept6() -> str:
    """验收6：同一源重复提取、同一轮重复召回、中断补算不重复写入或强化。"""
    with env(memory_decay_per_day=0.3) as (store, world, _):
        info, tl, ch = ready(store, world)
        turn(store, world, info, tl, ch, "你还好吗", world_seconds=watermark(store, tl), ref="env-A", reply_ref="m-A")
        tasks = store.memory_tasks(info["id"], tl)
        llm = ScriptLLM([json.dumps([
            {"text": "联络者问她好不好", "kind": "fact", "ref": tasks[0]["id"], "strength": 0.6, "confidence": 0.8},
            {"text": "她说她知道了", "kind": "fragment", "ref": tasks[1]["id"], "strength": 0.4, "confidence": 0.9},
        ], ensure_ascii=False)])
        first = asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW))
        count = store.memory_count(info["id"], tl, ch)
        before = {r["id"]: float(r["strength"]) for r in store.memory_scope(info["id"], tl, ch)}
        again = asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW))
        world.recall(info["id"], tl, ch, topic="她")
        world.recall(info["id"], tl, ch, topic="她")
        after_recall = {r["id"]: float(r["strength"]) for r in store.memory_scope(info["id"], tl, ch)}
        scope = ids(store.memory_scope(info["id"], tl, ch))
        cite1 = world.cite_memories(info["id"], tl, ch, turn_id="m-A", memory_ids=scope, world_seconds=0)
        cite2 = world.cite_memories(info["id"], tl, ch, turn_id="m-A", memory_ids=scope, world_seconds=0)
        after_cite = {r["id"]: float(r["strength"]) for r in store.memory_scope(info["id"], tl, ch)}
        assert first["written"] == 2 and count == 2, f"首次提取 {first} 条数 {count}"
        assert again["written"] == 0 and store.memory_count(info["id"], tl, ch) == 2, f"重复提取又写了：{again}"
        assert after_recall == before, f"重复召回提高了强度：{before} → {after_recall}"
        assert cite1 == 2, f"同轮强化 {(cite1, cite2)}"
        assert cite2 == 0, f"同一轮重复强化：{cite1} / {cite2}"
        assert all(after_cite[key] > before[key] for key in scope), f"实际采纳未强化：{after_cite}"
    steps = {}
    for label, planned in (("分步", [1, 2, 3, 4, 5]), ("一次", [5])):
        with env(memory_decay_per_day=0.3) as (store, world, _):
            info, tl, ch = ready(store, world)
            base = watermark(store, tl)
            mem(store, info, tl, ch, "mm-s", "潮位", strength=1.0)
            # memory_add 不写 decay_world（列默认 0），这里显式把衰减起点摆到记录水位
            store._conn.execute("UPDATE memory SET decay_world=? WHERE id=?", (base, "mm-s"))
            for step in planned:
                world.advance(info["id"], tl, now_real=NOW + (2 + step) * DAY, max_batches=20)
            steps[label] = float(store.memory_get("mm-s", instance_id=info["id"], timeline_id=tl)["strength"])
            if label == "一次":
                world.advance(info["id"], tl, now_real=NOW + 7 * DAY, max_batches=20)
                repeat = float(store.memory_get("mm-s", instance_id=info["id"], timeline_id=tl)["strength"])
    assert steps["分步"] > 0 and abs(steps["分步"] - steps["一次"]) < 1e-9, f"小步与批量补算不等价：{steps}"
    assert repeat == steps["一次"], f"同一水位重跑改动了强度：{steps['一次']} → {repeat}"
    return (f"重复提取写入 {first['written']}→{again['written']}；召回后强度不变；同轮强化 {cite1}→{cite2}；"
            f"补算等价 分步={steps['分步']:.12f} 一次={steps['一次']:.12f}（重跑 {repeat:.12f}）")


def accept7a() -> str:
    """验收7：高倍率衰减与冻结恢复都按世界时间结算。"""
    with env(memory_decay_per_day=0.05) as (store, world, _):
        info, tl, ch = ready(store, world)
        start = watermark(store, tl)
        mem(store, info, tl, ch, "mm-rate", "潮位记录", strength=1.0)
        store._conn.execute("UPDATE memory SET decay_world=? WHERE id=?", (start, "mm-rate"))
        world.set_rate(info["id"], tl, rate=10, now_real=NOW + 2 * DAY)
        result = world.advance(info["id"], tl, now_real=NOW + 3 * DAY, max_batches=40)
        end = watermark(store, tl)
        row = store.memory_get("mm-rate", instance_id=info["id"], timeline_id=tl)
        after = float(row["strength"])
        expected = memory_mod.decayed_strength(1.0, from_world=start, to_world=int(row["decay_world"]),
                                              day_seconds=DAY, per_day=0.05)
        assert end - start > 8 * DAY, f"高倍率没有推进世界时间：{(end - start) / DAY:.2f} 世界日"
        assert int(row["decay_world"]) == end, f"衰减没跟到水位：{row['decay_world']} != {end}"
        assert abs(after - expected) < 1e-9, f"衰减不按世界时长：{after} vs {expected}"
        rate_part = (f"倍率 10 → {int((end - start) / DAY)} 世界日内强度 1.0→{after:.6f}"
                     f"（按世界秒应为 {expected:.6f}，{result['batches']} 批）")
    with env(memory_decay_per_day=0.5) as (store, world, _):
        info, tl, ch = ready(store, world)
        mem(store, info, tl, ch, "mm-frz", "潮位记录", strength=1.0)
        store._conn.execute("UPDATE memory SET decay_world=? WHERE id=?", (watermark(store, tl), "mm-frz"))
        world.freeze(info["id"], tl, now_real=NOW + 2 * DAY)
        frozen = float(store.memory_get("mm-frz", instance_id=info["id"], timeline_id=tl)["strength"])
        world.advance(info["id"], tl, now_real=NOW + 40 * DAY, max_batches=60)
        still = float(store.memory_get("mm-frz", instance_id=info["id"], timeline_id=tl)["strength"])
        world.activate(info["id"], tl, now_real=NOW + 40 * DAY)
        world.advance(info["id"], tl, now_real=NOW + 48 * DAY, max_batches=60)
        thawed = float(store.memory_get("mm-frz", instance_id=info["id"], timeline_id=tl)["strength"])
        assert still == frozen, f"冻结期间衰减了：{frozen} → {still}"
        assert thawed < frozen, f"激活后没有继续衰减：{frozen} → {thawed}"
        freeze_part = f"冻结 {frozen:.6f} 保持不变；激活后 {thawed:.6f}"
    return f"{rate_part}；{freeze_part}"


def accept7b() -> str:
    """验收7：归档条目仍可被强相关检索唤起（且不豁免作用域）。"""
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        mem(store, info, tl, ch, "mm-arch", "堤长私吞修堤粮的旧名单在她手里", strength=0.1)
        row = store.memory_get("mm-arch", instance_id=info["id"], timeline_id=tl)
        recalled = world.recall(info["id"], tl, ch, topic="堤长私吞修堤粮的旧名单在她手里")
        assert row["state"] == "archived", f"低于阈值未进入归档：{row['state']}"
        assert "mm-arch" in recalled["ids"], (
            f"归档条目在强相关检索下无法唤起（§六）：recall(精确话题).ids={recalled['ids']}，"
            "entries=" + str(ids(recalled["entries"])) + "。最小复现：memory_add(strength=0.1) → "
            "recall(topic=该条全文) → 无命中。实现位置 isekai_core/runtime/service.py:1059 无条件丢弃 archived"
        )
        return f"归档条目 {row['state']}，强相关召回命中"


def accept7c() -> str:
    """验收7：记忆整理挂角色作息节律（无睡眠角色按世界日界）。"""
    hits: list[str] = []
    for path in sorted((ROOT / "isekai_core").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(word in line for word in ("organize", "memory_tidy", "memory_maintain", "def 整理")):
                hits.append(f"{path.relative_to(ROOT)}:{number}")
    hook = any(word in (ROOT / "isekai_core" / "app.py").read_text(encoding="utf-8")
               for word in ("organize", "memory_maintain"))
    assert hits, (
        "没有任何记忆整理实现（§六「整理只调整组织、强度与表达长度」/ 验收 7「无睡眠角色整理按世界日界触发」）："
        f"全文扫描 organize / memory_tidy / memory_maintain 命中 {hits}，app.py tick 钩子={hook}；"
        "runtime 只有衰减（service.py:1085 decay_memories），没有挂作息节律的整理入口"
        "（唯一的「整理」字样在 memory.py:176 的提取提示词句子里，不是实现）。"
        "最小复现：grep -rn 'organize\\|memory_maintain' isekai_core → 0 命中"
    )
    return f"整理入口：{hits}"


def accept7d() -> str:
    """验收7（衰减起点）：提取入库的记忆按记录 / 来源水位起算衰减，而不是从世界纪元 0。"""
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        start = watermark(store, tl)
        turn(store, world, info, tl, ch, "潮位怎么样", world_seconds=start, ref="env-d", reply_ref="m-d")
        task = [t for t in store.memory_tasks(info["id"], tl) if t["source_ref"].startswith("user:")][0]
        llm = ScriptLLM([entry("联络者问潮位怎么样", task["id"], strength=1.0)])
        asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW))
        row = store.memory_scope(info["id"], tl, ch)[0]
        origin = int(row["decay_world"])
        world.advance(info["id"], tl, now_real=NOW + 3 * DAY, max_batches=20)
        after = store.memory_get(str(row["id"]), instance_id=info["id"], timeline_id=tl)
        expected = memory_mod.decayed_strength(float(row["strength"]), from_world=start,
                                              to_world=watermark(store, tl), day_seconds=DAY, per_day=0.02)
        assert origin == start, (
            f"提取入库的记忆把衰减起点落成 decay_world={origin}（记录水位是 {start}）：memory_add 收到 decay_world 但 INSERT "
            f"没写这一列，列默认 0，于是衰减从世界纪元 0 起算——世界推进 {int((watermark(store, tl) - start) / DAY)} 个世界日后"
            f"强度 {float(row['strength'])}→{float(after['strength'])}、state={after['state']}（按记录水位应为 {expected:.6f}）。"
            "最小复现：extract_memories 写入一条 strength=1.0 的记忆 → 推进一个世界日 → 强度归零并归档，此后不再进入普通召回。"
            "实现位置 isekai_core/store.py:2225-2242（memory_add 的 INSERT 未包含 decay_world）"
        )
        return f"decay_world={origin}（记录水位）；推进后强度 {float(after['strength']):.6f}（state={after['state']}）"


def accept8() -> str:
    """验收8：导出导入恢复三层内容与引用、不含密钥；不存在记忆明细入口。"""
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        say(store, info, tl, ch, env_id="env-p", world_seconds=10, text="问", reply="答")
        mem(store, info, tl, ch, "mm-portable", "通行牌改按新滩路核发",
            sources=[{"kind": "claim", "ref": "cl-9", "via": "驿站"}],
            happened_world=1, learned_world=1, recorded_world=2, semantic_watermark=2,
            strength=0.75, confidence=0.8)
        container = build_container(store, info["id"])
        blob = json.dumps(container, ensure_ascii=False)
        imported = import_instance(store, container, display_name="记忆副本")
        line = str(store.timeline_list(imported["id"])[0]["id"])
        rows = store.memory_scope(imported["id"], line, ch)
        messages = sql_int(store, "SELECT COUNT(*) FROM message WHERE session_id IN "
                                  "(SELECT id FROM session WHERE instance_id=?)", (imported["id"],))
        cards = (container.get("setting") or {}).get("cards") or []
        names = set(ops_mod.SYNC_OPS) | set(ops_mod.ASYNC_OPS)
        ops_src = (ROOT / "isekai_core" / "world" / "ops.py").read_text(encoding="utf-8")
        leaks = [name for name in names if "memory" in name or "recall" in name]
        runtimes = (container.get("runtime") or {}).get("state") or {}
        experiences = sum(len(item.get("experiences") or []) for item in runtimes.values())
        assert len(rows) == 1 and rows[0]["text"] == "通行牌改按新滩路核发", f"记忆没随件：{rows}"
        assert json.loads(rows[0]["sources"])[0]["ref"] == "cl-9", "来源引用没随件"
        assert experiences, f"经历没随件：{runtimes.keys()}"
        assert messages > 0, "对话原文没随件"
        assert "sk-" not in blob and "api_key" not in blob.lower(), "导出件含密钥"
        assert not leaks, f"操作面存在记忆浏览入口：{leaks}"
        assert "memory_scope" not in ops_src and "memory_get" not in ops_src, "ops 层直接暴露记忆明细"
        return (f"导入后记忆 {len(rows)} 条（来源 {json.loads(rows[0]['sources'])}）；经历 {experiences} 条、"
                f"对话 {messages} 条随件；导出件无 sk-/api_key；{len(names)} 个操作无记忆浏览入口；"
                f"cards 随件 {len(cards)} 张")


def accept9a() -> str:
    """验收9（前段）：合并批次共享一份回复不重复提取；重放与同轮强化幂等。"""
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        w = watermark(store, tl)
        add1 = world.queue_dialog_turn(info["id"], tl, ch, world_seconds=w, user_ref="env-1", user_text="甲问",
                                       reply_message_id="m-merge", reply_text="知道了")
        add2 = world.queue_dialog_turn(info["id"], tl, ch, world_seconds=w + 10, user_ref="env-2", user_text="乙问",
                                       reply_message_id="m-merge", reply_text="知道了")
        tasks = store.memory_tasks(info["id"], tl)
        llm = ScriptLLM([json.dumps([
            {"text": "联络者问她今天好不好", "kind": "fact", "ref": tasks[0]["id"], "strength": 0.6, "confidence": 0.8},
            {"text": "她答了一句知道了", "kind": "fragment", "ref": tasks[1]["id"], "strength": 0.4, "confidence": 0.9},
            {"text": "她记下这一轮里说过的话", "kind": "fragment", "ref": tasks[2]["id"], "strength": 0.5, "confidence": 0.7},
        ], ensure_ascii=False)])
        first = asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW))
        replay = world.queue_dialog_turn(info["id"], tl, ch, world_seconds=w, user_ref="env-1", user_text="甲问",
                                         reply_message_id="m-merge", reply_text="知道了")
        second = asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW))
        count = store.memory_count(info["id"], tl, ch)
        scope = ids(store.memory_scope(info["id"], tl, ch))
        cite1 = world.cite_memories(info["id"], tl, ch, turn_id="m-merge", memory_ids=scope, world_seconds=w)
        cite2 = world.cite_memories(info["id"], tl, ch, turn_id="m-merge", memory_ids=scope, world_seconds=w)
        assert add1 == 2 and add2 == 1, f"合并批次来源登记：{add1} / {add2}（回复应只登记一次）"
        assert len(tasks) == 3 and first["written"] == 3, f"待提取 {len(tasks)} 条，写入 {first['written']}"
        assert replay == 0 and second["written"] == 0 and count == 3, f"重放 {replay}，再提取 {second}"
        assert cite1 == 3 and cite2 == 0, f"同一轮重复强化：{cite1} / {cite2}"
        return (f"批内来源 {len(tasks)} 条（2 输入 + 1 回复）；首次写入 {first['written']}；重放新增来源 {replay}、"
                f"再提取写入 {second['written']}；同轮强化 {cite1}→{cite2}")


def accept9b() -> str:
    """验收9（第三段）：回滚 / 来源失效后迟到任务不写入；冻结线不参与提取调度。"""
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        commit = world.commit(info["id"], tl, note="回滚点")
        turn(store, world, info, tl, ch, "回滚后登记的一轮", world_seconds=watermark(store, tl),
             ref="env-late", reply_ref="m-late")
        pending_before = len(store.memory_tasks(info["id"], tl))
        world.rollback(info["id"], tl, commit_id=commit["id"], now_real=NOW + 6 * DAY)
        pending_after = store.memory_tasks(info["id"], tl)
        llm = ScriptLLM([entry("不该出现", "x")])
        res = asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW + 6 * DAY))
        store.memory_task_add({
            "id": "mt-bad", "instance_id": info["id"], "timeline_id": tl, "character_id": ch,
            "source_kind": "experience", "source_ref": "ex-不存在",
            "source_world": watermark(store, tl), "created_world": watermark(store, tl), "text": "",
        })
        res2 = asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW + 6 * DAY))
        dropped = store.memory_tasks(info["id"], tl, state="dropped")
        world.freeze(info["id"], tl, now_real=NOW + 6 * DAY)
        active = world.active_timelines()
        assert pending_before == 2, f"迟到任务登记数：{pending_before}"
        assert pending_after == [], f"回滚后迟到任务仍在待提取：{pending_after}"
        assert res["extracted"] == 0 and res["written"] == 0, f"回滚后仍写入：{res}"
        assert [t["id"] for t in dropped] == ["mt-bad"] and res2["written"] == 0, f"{dropped} / {res2}"
        assert store.memory_count(info["id"], tl, ch) == 0, "回滚后的迟到任务写入了记忆"
        assert (info["id"], tl) not in active, f"冻结线仍在提取调度范围：{active}"
        return (f"回滚前待提取 {pending_before} → 回滚后 {len(pending_after)} 条；提取 {res['extracted']}/{res['written']}；"
                f"失效来源 → {dropped[0]['state']}/{dropped[0]['note']}；冻结后 active_timelines 不含本线")


def accept10() -> str:
    """验收10：召回与重复引用不提高采信，也不把未获知的内容喂给提取。"""
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        other = add_character(store, world, info, tl, "堤砚")
        w = watermark(store, tl)
        secret = "堤长私吞了修堤粮，这事只有我知道"
        mem(store, info, tl, ch, "mm-c", "她听说堤长私吞修堤粮", strength=0.5, confidence=0.4,
            sources=[{"kind": "claim", "ref": "cl-1", "via": "驿站"}])
        mem(store, info, tl, ch, "mm-t", "她记得通行牌停发", strength=0.5, confidence=0.9)
        world.recall(info["id"], tl, ch, topic="堤长 修堤粮")
        world.recall(info["id"], tl, ch, topic="堤长 修堤粮")
        world.cite_memories(info["id"], tl, ch, turn_id="m-1", memory_ids=["mm-c"], world_seconds=w)
        row = store.memory_get("mm-c", instance_id=info["id"], timeline_id=tl)
        low = world.recall(info["id"], tl, ch, topic="修堤粮")
        high = world.recall(info["id"], tl, ch, topic="通行牌")
        low_lines = dict(zip(low["brief"]["ids"], low["brief"]["lines"]))
        high_lines = dict(zip(high["brief"]["ids"], high["brief"]["lines"]))
        turn(store, world, info, tl, ch, secret, world_seconds=w, ref="env-secret", reply_ref="m-secret")
        turn(store, world, info, tl, other, "今天滩上风大", world_seconds=w, ref="env-b", reply_ref="m-b")
        llm = ScriptLLM([entry("联络者问她今天风大不大", "x")])
        asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW))
        other_prompts = [p for call in llm.calls for p in call
                         if "堤砚" in json.dumps(p, ensure_ascii=False)]
        other_seen = world.recall(info["id"], tl, other, topic="修堤粮")
        assert float(row["confidence"]) == 0.4, f"强化改动了采信：{row['confidence']}"
        assert float(row["strength"]) > 0.5, f"实际采纳没有强化：{row['strength']}"
        assert "不太确定" in low_lines.get("mm-c", ""), f"低确信缺模糊线索：{low_lines}"
        assert "不太确定" not in high_lines.get("mm-t", ""), f"高确信被标成不确定：{high_lines}"
        assert other_prompts and secret not in json.dumps(other_prompts, ensure_ascii=False), "未获知内容进了另一角色的提取材料"
        assert secret not in json.dumps(other_seen, ensure_ascii=False), "未获知内容被召回"
        return (f"强化后 strength {row['strength']} / confidence {row['confidence']}（不变）；"
                f"低确信简报带「不太确定」、高确信不带；B 的提取提示词 {len(other_prompts)} 条均不含 A 的私聊")


def accept11() -> str:
    """验收11：打算跨重启保留；未执行不产生亲历；随口愿望不被升级为义务。"""
    root = Path(tempfile.mkdtemp(prefix="audit-memory-"))
    db = root / "data" / "isekai.db"
    try:
        store = Store(db)
        store.ensure_schema()
        world = RuntimeService(store, **SVC)
        info, tl, ch = ready(store, world)
        queued = world.queue_world_sources(info["id"], tl)
        intents = [t for t in store.memory_tasks(info["id"], tl) if t["source_kind"] == "intent"]
        assert intents, f"打算没有进提取来源（queued={queued}）"
        material = world._source_material(intents[0])
        llm = ScriptLLM([entry("她打算等那份潮位告警的抄本重新归档", intents[0]["id"], kind="promise", confidence=0.8)])
        asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW))
        rows = [r for r in store.memory_scope(info["id"], tl, ch) if r["kind"] == "promise"]
        intents_before = store.intent_list(info["id"], tl, ch)
        events_before = sql_int(store, "SELECT COUNT(*) FROM event WHERE timeline_id=?", (tl,))
        pending_before = sql_int(store, "SELECT COUNT(*) FROM pending_event WHERE timeline_id=?", (tl,))
        assert rows and json.loads(rows[0]["sources"])[0]["kind"] == "intent", f"打算条目来源：{rows}"
        assert memory_mod.source_label(json.loads(rows[0]["sources"])) == "她自己的打算"
        assert material and material["source"] == "她自己的打算" and "依据" in material["text"], material
        assert store.intent_list(info["id"], tl, ch) == intents_before, "记下打算改变了角色状态（开始执行）"
        assert sql_int(store, "SELECT COUNT(*) FROM event WHERE timeline_id=?", (tl,)) == events_before
        assert sql_int(store, "SELECT COUNT(*) FROM pending_event WHERE timeline_id=?", (tl,)) == pending_before
        memory_id = str(rows[0]["id"])
        store.close()
        reopened = Store(db)
        row2 = reopened.memory_get(memory_id, instance_id=info["id"], timeline_id=tl)
        reopened.close()
        assert row2 is not None and row2["text"] == rows[0]["text"] and row2["state"] == "active", (
            f"重启后打算丢失：{row2}"
        )
        return (f"打算来源 {json.loads(rows[0]['sources'])} → 标签「她自己的打算」；重开后仍在（state={row2['state']}）；"
                f"事件 {events_before} 条、待执行 {pending_before} 条未变（未升级为义务）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def accept12() -> str:
    """验收12：相关新经历能接回旧片段；回接仍受隔离与披露限制。"""
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        other = add_character(store, world, info, tl, "堤砚")
        w = watermark(store, tl)
        old_world = w - 30 * DAY
        mem(store, info, tl, ch, "mm-old", "她和联络者谈过堤长的接任，事情还没办完", strength=0.4,
            happened_world=old_world, learned_world=old_world, recorded_world=old_world)
        mem(store, info, tl, ch, "mm-new", "这两天滩上风大，她照常值夜", strength=0.9,
            happened_world=w, learned_world=w, recorded_world=w, semantic_watermark=w)
        for ident, origin in (("mm-old", old_world), ("mm-new", w)):
            store._conn.execute("UPDATE memory SET decay_world=? WHERE id=?", (origin, ident))
        world.advance(info["id"], tl, now_real=NOW + 6 * DAY, max_batches=20)
        back = world.recall(info["id"], tl, ch, topic="堤长")
        other_sees = world.recall(info["id"], tl, other, topic="堤长")
        reply_id = say(store, info, tl, ch, env_id="env-a", world_seconds=w,
                       text="堤上的事", reply="信报上抄到堤长身故，接任未毕")
        mem(store, info, tl, ch, "mm-hidden", "她自己压着一份没说出去的名单", strength=0.9)
        world.disclose(info["id"], tl, from_character=ch, to_character=other, refs=[reply_id], note="让堤砚知道")
        frags = world.disclosed_fragments(info["id"], tl, other)
        seen_b = world.recall(info["id"], tl, other, topic="堤长")
        hit = [r for r in seen_b["entries"] if "堤长身故" in str(r["text"])]
        assert "mm-old" in ids(back["entries"]), f"旧话题接不回来：{ids(back['entries'])}"
        assert other_sees["entries"] == [], "回接越过了角色隔离"
        assert len(frags) == 1 and "堤长身故" in frags[0]["text"], f"披露片段：{frags}"
        assert hit and "转述" in str(hit[0]["text"]), f"披露后不是转述视图：{hit}"
        assert json.loads(hit[0]["sources"])[0]["source_role"] == "other_character"
        assert not any("名单" in str(r["text"]) for r in seen_b["entries"]), "未披露内容被一并带过去"
        return (f"旧片段回接={ids(back['entries'])}；另一角色（未披露前）={other_sees['ids']}；"
                f"披露片段 {len(frags)} 条；披露后 B 召回 {ids(seen_b['entries'])}（转述、不含未披露的名单）")


def accept13() -> str:
    """验收13：预算耗尽时既不阻塞已固化对话，也不让重试绕过预算。"""
    stub = _Stub()
    try:
        with env(task_tokens_per_day=1, memory_embedding_model="stub-embed",
                 memory_embedding_base_url=stub.base_url, memory_embedding_api_key="k") as (store, world, _):
            info, tl, ch = ready(store, world)
            w = watermark(store, tl)
            turn(store, world, info, tl, ch, "预算耗尽时的一轮", world_seconds=w, ref="env-b", reply_ref="m-b")
            llm = ScriptLLM([entry("联络者问她一句", "x")])
            res = asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW))
            pending = store.memory_tasks(info["id"], tl)
            mem(store, info, tl, ch, "mm-cite", "通行牌停发", strength=0.5)
            cited = world.cite_memories(info["id"], tl, ch, turn_id="m-b", memory_ids=["mm-cite"], world_seconds=w)
            again = world.cite_memories(info["id"], tl, ch, turn_id="m-b", memory_ids=["mm-cite"], world_seconds=w)
            embed1 = asyncio.run(world.embed_memories(info["id"], tl, now_real=NOW))
            embed2 = asyncio.run(world.embed_memories(info["id"], tl, now_real=NOW))
            rows = sql_int(store, "SELECT COUNT(*) FROM memory_embedding WHERE timeline_id=?", (tl,))
            recalled = world.recall(info["id"], tl, ch, topic="通行牌")
            assert res["written"] == 0 and res["calls"] == 0 and res["pending"] >= 1, f"预算耗尽仍写入：{res}"
            assert len(pending) == 2 and not llm.calls, "任务应保持待处理且不调用模型"
            assert cited == 1 and again == 0, f"已固化轮的强化：{cited} / {again}"
            assert embed1.get("paused") is True and embed2.get("paused") is True, f"{embed1} / {embed2}"
            assert rows == 0, f"重试绕过了预算：{rows} 条向量落库"
            assert recalled["ids"] == ["mm-cite"], f"全文召回不可用：{recalled['ids']}"
            return (f"提取 {res}（模型调用 {len(llm.calls)} 次）；已固化轮的记账与强化照常 {cited}/{again}；"
                    f"向量两次重试 {embed1.get('paused')}/{embed2.get('paused')}，落库 {rows} 条；"
                    f"全文召回 {recalled['ids']}")
    finally:
        stub.close()


def accept14a() -> str:
    """验收14（前段）：迟到数个世界日的提取按来源时刻结算强度与三个时间戳。"""
    with env(memory_decay_per_day=0.2) as (store, world, _):
        info, tl, ch = ready(store, world)
        start = watermark(store, tl)
        world.queue_dialog_turn(info["id"], tl, ch, world_seconds=start, user_ref="env-old",
                                user_text="还记得那天吗", reply_message_id="m-old", reply_text="记得")
        task = [t for t in store.memory_tasks(info["id"], tl) if t["source_ref"].startswith("user:")][0]
        world.advance(info["id"], tl, now_real=NOW + 30 * DAY, max_batches=60)
        at = watermark(store, tl)
        llm = ScriptLLM([entry("联络者问她还记得那天吗", task["id"], strength=1.0)])
        asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW + 30 * DAY))
        row = store.memory_scope(info["id"], tl, ch)[0]
        expected = memory_mod.decayed_strength(1.0, from_world=start, to_world=at, day_seconds=DAY, per_day=0.2)
        assert abs(float(row["strength"]) - expected) < 1e-9, f"{row['strength']} != {expected}"
        assert float(row["strength"]) < 0.05, f"迟到结果没有按世界时间结算：{row['strength']}"
        assert int(row["learned_world"]) == start and int(row["recorded_world"]) == at > start
        return (f"来源时刻 {start} → 记录水位 {at}（相差 {int((at - start) / DAY)} 世界日）；"
                f"入库强度 {row['strength']:.6f} = 按来源时刻结算的 {expected:.6f}；"
                f"happened={row['happened_world']} learned={row['learned_world']} recorded={row['recorded_world']}")


def accept14b() -> str:
    """验收14（后段）/§4.1：迟到提取不得覆盖期间已经固化的纠正。"""
    with env(memory_decay_per_day=0.0) as (store, world, _):
        info, tl, ch = ready(store, world)
        start = watermark(store, tl)
        world.queue_dialog_turn(info["id"], tl, ch, world_seconds=start, user_ref="env-old",
                                user_text="堤长的事有下文吗", reply_message_id="m-old", reply_text="听人说堤长身故了")
        task = [t for t in store.memory_tasks(info["id"], tl) if t["source_ref"].startswith("user:")][0]
        world.advance(info["id"], tl, now_real=NOW + 10 * DAY, max_batches=30)
        fix_world = watermark(store, tl)
        mem(store, info, tl, ch, "mm-fix", "堤长没有身故，接任推举未毕", strength=0.9, confidence=0.9,
            sources=[{"kind": "dialog", "ref": "reply:m-fix", "source_role": "user"}],
            happened_world=start, learned_world=fix_world, recorded_world=fix_world,
            semantic_watermark=fix_world, decay_world=fix_world)
        world.advance(info["id"], tl, now_real=NOW + 30 * DAY, max_batches=60)
        llm = ScriptLLM([entry("堤长身故，接任推举未毕", task["id"], strength=0.8)])
        res = asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW + 30 * DAY))
        fix = store.memory_get("mm-fix", instance_id=info["id"], timeline_id=tl)
        current = [r["id"] for r in store.memory_scope(info["id"], tl, ch) if r["superseded_by"] is None]
        late = [(r["id"], r["state"], r["superseded_by"]) for r in store.memory_scope(info["id"], tl, ch)
                if r["id"] != "mm-fix"]
        assert fix["superseded_by"] is None and fix["state"] != "archived", (
            f"迟到提取（来源水位 {start}）把期间已固化的纠正顶掉了：mm-fix.superseded_by={fix['superseded_by']} "
            f"state={fix['state']}；当前有效版本={current}；迟到条目={late}。"
            "最小复现：登记水位 w0 的轮次 → 10 世界日后写入纠正（mm-fix）→ 30 世界日后提取 w0 的来源"
            "（脚本模型返回与旧说法一致的相反文本）→ memory_add 只按 (learned_world, id) 取第一个矛盾兄弟建替代链。"
            f"实现位置 isekai_core/store.py:2217-2224（memory_add 未比较来源新旧 / 语义水位）"
        )
        return f"纠正有效版本={current}；迟到条目={late}；提取返回 {res['written']} 条"


def spec_extraction_validation() -> str:
    """§4.1：不合规的提取结果整条丢弃，不拿猜测填空；蒸馏不丢时间 / 否定。"""
    refs = {"mt-1"}
    good = memory_mod.parse_extraction(entry("潮位五尺", "mt-1"), refs)
    bad_ref = memory_mod.parse_extraction(entry("潮位五尺", "mt-不存在"), refs)
    truncated = memory_mod.parse_extraction('[{"text":"半截","ref":"mt-1"}', refs)
    bad_kind = memory_mod.parse_extraction('[{"text":"x","kind":"秘密","ref":"mt-1"}]', refs)
    not_json = memory_mod.parse_extraction("从 [ 开始就不是 JSON", refs)
    long_text = memory_mod.distill("堤长身故，接任推举未毕，通行牌停发。" * 12)
    assert good and not (bad_ref or truncated or bad_kind or not_json), (
        f"校验漏过：good={good} bad_ref={bad_ref} truncated={truncated} bad_kind={bad_kind} not_json={not_json}"
    )
    assert len(long_text) <= memory_mod.MAX_TEXT_CHARS + 1 and "未毕" in long_text, long_text
    return (f"合法={len(good)} 条；假来源={len(bad_ref)}；半截 JSON={len(truncated)}；闭集外类型={len(bad_kind)}；"
            f"非 JSON={len(not_json)}；蒸馏后 {len(long_text)} 字仍含「未毕」")


def spec_extraction_failure_pending() -> str:
    """§4.1：提取失败时保留待处理标记（可重试），不把来源永久标成已处理。"""
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        turn(store, world, info, tl, ch, "再说一遍", world_seconds=watermark(store, tl),
             ref="env-x", reply_ref="m-x")
        llm = ScriptLLM(["这不是 JSON 也没有数组"])
        res = asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW))
        pending = store.memory_tasks(info["id"], tl)
        done = [(t["id"], t["note"]) for t in store.memory_tasks(info["id"], tl, state="done")]
        assert pending, (
            f"不可解析的回答把来源永久标成已处理：pending={pending} done={done}（提取返回 {res}）。"
            "§4.1 要求「失败时先保留原文 / 经历和待处理标记」；最小复现：脚本模型返回 "
            "'这不是 JSON 也没有数组' → 任务被标 done/note=无可记内容，此后不再重试，来源静默丢失。"
            "实现位置 isekai_core/runtime/service.py:848-851"
        )
        return f"待处理 {len(pending)} 条"


def spec_rank_and_brief() -> str:
    """§5.1：全文与向量先归一 / 秩融合再排序；同分用稳定标识；简报按预算并带来源与确信线索。"""
    entries = [
        {"id": "e-1", "text": "潮位到了刻线", "strength": 0.6, "learned_world": 0},
        {"id": "e-2", "text": "完全无关的一段内容", "strength": 0.6, "learned_world": 0},
    ]
    tie = memory_mod.rank(query="潮位", entries=entries, now_world=0, day_seconds=DAY,
                          vector_scores={"e-2": 0.99})
    third = memory_mod.rank(query="潮位", entries=entries + [
        {"id": "e-3", "text": "潮位刻线又记了一笔", "strength": 0.6, "learned_world": 0},
    ], now_world=0, day_seconds=DAY, vector_scores={"e-1": 0.1, "e-2": 0.4, "e-3": 0.9})
    brief = memory_mod.pack_brief([
        {"id": "b-1", "text": "潮位五尺", "confidence": 0.4, "source_label": "听说", "state": "active"},
        {"id": "b-2", "text": "通行牌停发", "confidence": 0.9, "source_label": "亲历", "state": "archived"},
    ], budget_tokens=200)
    tight = memory_mod.pack_brief(
        [{"id": f"b-{index}", "text": "潮位五尺", "confidence": 0.9, "source_label": "", "state": "active"}
         for index in range(6)], budget_tokens=45,
    )
    scores = {row["id"]: row["score"] for row in tie}
    assert scores["e-1"] == scores["e-2"], f"向量与全文不同量纲直接相乘：{scores}"
    assert ids(tie) == ["e-1", "e-2"], f"同分未用稳定标识排序：{ids(tie)}"
    assert ids(third)[0] == "e-3", f"两项都命中的条目未排前：{ids(third)}"
    assert brief["ids"] == ["b-1", "b-2"] and "不太确定" in brief["text"] and "听说" in brief["text"]
    assert "模糊回想" in brief["text"] and "亲历" in brief["text"]
    spent = sum(len(line) + 4 for line in tight["lines"])
    assert 0 < len(tight["lines"]) < 6 and spent <= 45 < spent + 10, f"简报预算失效：{tight}"
    return (f"同分 {scores}（秩融合后相等）；稳定顺序 {ids(tie)}；双命中排前 {ids(third)}；"
            f"简报带来源/确信/归档线索且预算生效（{len(tight['lines'])} 行 {len(tight['text'])} 字）")


def spec_vector_model_rebuild() -> str:
    """§5.2：模型指纹变化 → 旧向量不混算且从源文本重建。"""
    stub = _Stub(dim=4)
    try:
        with env(memory_embedding_model="stub-embed", memory_embedding_base_url=stub.base_url,
                 memory_embedding_api_key="k") as (store, world, _):
            info, tl, ch = ready(store, world)
            mem(store, info, tl, ch, "mm-1", "潮位到了刻线")
            mem(store, info, tl, ch, "mm-2", "船到了")
            first = asyncio.run(world.embed_memories(info["id"], tl, now_real=NOW))
            probe = stub.vector("船", 4)
            fresh_scores = store.memory_vector_scores(info["id"], tl, ch, "船", query_vector=probe, model="stub-embed")
            other = RuntimeService(store, **{**SVC, "memory_embedding_model": "别的模型",
                                            "memory_embedding_base_url": stub.base_url,
                                            "memory_embedding_api_key": "k"})
            stale = store.memory_vector_scores(info["id"], tl, ch, "船", query_vector=probe, model="别的模型")
            missing = store.memory_missing_embeddings(info["id"], tl, model="别的模型")
            rebuilt = asyncio.run(other.embed_memories(info["id"], tl, now_real=NOW))
            again = store.memory_vector_scores(info["id"], tl, ch, "船", query_vector=probe, model="别的模型")
            assert first["embedded"] == 2 and len(fresh_scores) == 2, f"{first} / {fresh_scores}"
            assert stale == {} and len(missing) == 2, f"换模型后旧向量仍混算：{stale}；待重建 {len(missing)}"
            assert rebuilt["embedded"] == 2 and len(again) == 2, f"重建失败：{rebuilt} / {again}"
            return (f"原指纹 {len(fresh_scores)} 条可召回；换模型后旧向量评分 {stale}、待重建 {len(missing)} 条；"
                    f"重建 {rebuilt['embedded']} 条后新指纹可召回 {len(again)} 条")
    finally:
        stub.close()


def spec_vector_dim_rebuild() -> str:
    """§5.2：维度变化时旧向量停止混算，并**从源文本重建**。"""
    stub = _Stub(dim=4)
    try:
        with env(memory_embedding_model="stub-embed", memory_embedding_base_url=stub.base_url,
                 memory_embedding_api_key="k") as (store, world, _):
            info, tl, ch = ready(store, world)
            mem(store, info, tl, ch, "mm-d", "潮位到了刻线")
            first = asyncio.run(world.embed_memories(info["id"], tl, now_real=NOW))
            _Stub.DIM = 8  # 同一模型名，维度变了（指纹的维度部分）
            probe = stub.vector("潮位", 8)
            missing = store.memory_missing_embeddings(info["id"], tl, model="stub-embed")
            rebuilt = asyncio.run(world.embed_memories(info["id"], tl, now_real=NOW))
            scores = store.memory_vector_scores(info["id"], tl, ch, "潮位", query_vector=probe, model="stub-embed")
            assert len(missing) == 1 and rebuilt.get("embedded") == 1, (
                f"维度变化不触发重建：待重建 {len(missing)} 条、embed_memories 返回 {rebuilt}（写入 {first['embedded']} 条旧向量）。"
                "§5.2 要求「模型 / 维度变化时旧向量停止参与混合召回，重建从源文本开始」；"
                "最小复现：用 dim=4 落库 → 同模型名改 dim=8 → memory_missing_embeddings(model='stub-embed') 返回 []"
                "（只比 model 不比 dim）→ 该条向量永久缺席混合召回。实现位置 isekai_core/store.py:2450-2461"
            )
            return f"维度变化后重建 {rebuilt}，新指纹参与评分 {len(scores)} 条"
    finally:
        stub.close()


def spec_disclosure_recall() -> str:
    """§7.1 / 阶段 5：未披露不可见；披露后按转述视图召回；回滚撤销授权与派生。"""
    with env() as (store, world, _):
        info, tl, first = ready(store, world)
        second = add_character(store, world, info, tl, "堤砚")
        third_card = sample_card(sample_package(), name="第三个人")
        world.add_character(info["id"], tl, third_card, now_real=NOW + 2 * DAY,
                            joined_world=watermark(store, tl))
        third = str(third_card["meta"]["card_id"])
        w = watermark(store, tl)
        reply_id = say(store, info, tl, first, env_id="env-a", world_seconds=w,
                       text="堤上的事", reply="信报上抄到堤长身故，接任未毕")
        mem(store, info, tl, first, "mm-a2", "她自己压着一份没说出去的名单", strength=0.9)
        before = world.recall(info["id"], tl, second, topic="堤长 名单")
        queued_before = world.queue_disclosed_sources(info["id"], tl, second)
        tasks_before = [t for t in store.memory_tasks(info["id"], tl) if t["character_id"] == second]
        commit = world.commit(info["id"], tl, note="披露前")
        granted = world.disclose(info["id"], tl, from_character=first, to_character=second, refs=[reply_id])
        queued_after = world.queue_disclosed_sources(info["id"], tl, second)
        task = [t for t in store.memory_tasks(info["id"], tl) if t["character_id"] == second][0]
        material = world._source_material(task)
        frags = world.disclosed_fragments(info["id"], tl, second)
        after = world.recall(info["id"], tl, second, topic="堤长")
        third_sees = world.disclosed_fragments(info["id"], tl, third)
        hidden = world.disclosed_fragments(info["id"], tl, second, until=int(granted["granted_world"]) - 1)
        mem(store, info, tl, second, "mm-derived", "联络者转述了堤禾说过的话：信报上抄到堤长身故",
            sources=[{"kind": "dialog", "ref": reply_id, "source_role": "other_character"}], strength=0.6)
        world.rollback(info["id"], tl, commit_id=commit["id"], now_real=NOW + 6 * DAY)
        assert before["entries"] == [] and queued_before == 0 and tasks_before == [], "未披露就进了 B 的召回 / 提取"
        assert len(frags) == 1 and "堤长身故" in frags[0]["text"], f"披露后不可见：{frags}"
        assert queued_after == 1 and str(material["source"]) == "联络者转述", f"披露来源登记：{queued_after} / {material}"
        assert material["sources"][0]["source_role"] == "other_character", material["sources"]
        hit = [r for r in after["entries"] if "堤长身故" in str(r["text"])]
        assert hit and "转述" in str(hit[0]["text"]), f"披露后不是转述视图：{hit}"
        assert not any("名单" in str(r["text"]) for r in after["entries"]), "授权把整库带过去了"
        assert third_sees == [] and hidden == [], "授权传给第三人 / 未按水位版本化"
        assert world.disclosures(info["id"], tl) == [], "回滚没有撤销授权"
        assert world.disclosed_fragments(info["id"], tl, second) == [], "回滚后转述仍可见"
        assert store.memory_get("mm-derived", instance_id=info["id"], timeline_id=tl) is None, "派生记忆没随回滚撤销"
        return (f"未披露 {before['ids']}/{queued_before}；披露后片段 {len(frags)} 条、B 召回 {ids(after['entries'])}（转述）、"
                f"第三人 {len(third_sees)} 条；回滚后授权与派生一并消失")


def spec_no_world_truth_extraction() -> str:
    """§4.1 / §七：不从未过滤实情提取；系统通知与诊断不产生提取来源。"""
    with env() as (store, world, _):
        info, tl, ch = ready(store, world)
        store.memory_task_add({
            "id": "mt-wt", "instance_id": info["id"], "timeline_id": tl, "character_id": ch,
            "source_kind": "world_truth", "source_ref": "ev-secret", "source_world": watermark(store, tl),
            "created_world": watermark(store, tl), "text": "世界实情：堤长私吞修堤粮",
        })
        llm = ScriptLLM([entry("不该出现", "mt-wt")])
        res = asyncio.run(world.extract_memories(info["id"], tl, llm=llm, now_real=NOW))
        dropped = store.memory_tasks(info["id"], tl, state="dropped")
        sites: list[str] = []
        needles = ("memory_task_add(", "queue_dialog_turn(", "queue_world_sources(", "queue_disclosed_sources(")
        for path in sorted((ROOT / "isekai_core").rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(needle in line for needle in needles) and "def " not in line:
                    sites.append(f"{path.relative_to(ROOT)}:{number}:{line.strip()[:70]}")
        assert res["written"] == 0 and res["calls"] == 0 and not llm.calls, f"未知来源仍处理：{res}"
        assert [t["id"] for t in dropped] == ["mt-wt"] and dropped[0]["state"] == "dropped", dropped
        assert all("notice" not in site.split(":", 2)[2].lower() for site in sites), f"系统通知路径登记了提取来源：{sites}"
        return (f"未知来源 world_truth → {dropped[0]['state']}（{dropped[0]['note']}），未调模型、未写入；"
                f"提取来源登记点 {len(sites)} 处：{sites}")


CHECKS: list[tuple[str, Callable[[], str], str]] = [
    ("验收1 同实例同线双角色 / 跨线 / 跨实例默认隔离", accept1, "isekai_core/store.py:2188 memory_scope"),
    ("验收2 分叉继承共同过去；回滚清掉后续记忆与其向量", accept2, "isekai_core/store.py:1525 timeline_clear_state"),
    ("验收3 无依据往事不变亲历；传闻不变真相且留来源", accept3, "isekai_core/runtime/memory.py:272 source_label"),
    ("验收4 相似但矛盾的陈述不被去重吞掉、纠正留替代链", accept4, "isekai_core/store.py:2199 memory_add"),
    ("验收5 缺配置 / 请求失败时退化全文召回且不阻断推进", accept5a, "isekai_core/runtime/service.py:931 embed_memories"),
    ("验收5 查询向量维度改变时旧向量退出混合召回", accept5b, "isekai_core/store.py:2354 memory_vector_scores"),
    ("验收6 重复提取 / 重复召回 / 中断补算不重复写或强化", accept6, "isekai_core/store.py:2311 memory_cite"),
    ("验收7 高倍率衰减与冻结恢复遵守世界时间", accept7a, "isekai_core/store.py:2388 memory_decay"),
    ("验收7 归档条目在强相关检索下可唤起", accept7b, "isekai_core/runtime/service.py:1059 丢弃 archived"),
    ("验收7 记忆整理挂作息节律（无睡眠角色按世界日界）", accept7c, "isekai_core/runtime/service.py（无整理入口）"),
    ("验收7 提取入库的记忆按记录水位起算衰减", accept7d, "isekai_core/store.py:2225-2242 memory_add 未写 decay_world"),
    ("验收8 导出导入恢复三层内容与引用、无密钥、无明细入口", accept8, "isekai_core/world/portable.py:185 import_instance"),
    ("验收9 合并批次共享回复不重复提取 / 重放幂等", accept9a, "isekai_core/runtime/service.py:633 queue_dialog_turn"),
    ("验收9 回滚 / 来源失效后迟到任务不写入、冻结线不调度", accept9b, "isekai_core/runtime/service.py:712 _source_material"),
    ("验收10 召回与重复引用不提高采信、不喂未获知内容", accept10, "isekai_core/runtime/service.py:1065 cite_memories"),
    ("验收11 打算跨重启保留、未执行不产生亲历 / 义务", accept11, "isekai_core/store.py:2264 memory_update_strength"),
    ("验收12 旧话题可回接且仍受隔离与披露限制", accept12, "isekai_core/runtime/service.py:1015 recall"),
    ("验收13 预算耗尽不阻塞、向量待处理、重试不绕过", accept13, "isekai_core/runtime/service.py:1115 reserve_call"),
    ("验收14 迟到提取按来源世界时间结算强度与三时间戳", accept14a, "isekai_core/runtime/service.py:855 按来源时刻结算"),
    ("验收14 迟到提取不得覆盖期间已固化的纠正", accept14b, "isekai_core/store.py:2217-2224 memory_add"),
    ("§4.1 不合规提取结果整条丢弃、不拿猜测填空", spec_extraction_validation, "isekai_core/runtime/memory.py:199 parse_extraction"),
    ("§4.1 提取失败保留待处理标记（可重试）", spec_extraction_failure_pending, "isekai_core/runtime/service.py:848-851"),
    ("§5.1 归一 / 秩融合排序、同分稳定、简报带线索与预算", spec_rank_and_brief, "isekai_core/runtime/memory.py:96 rank"),
    ("§5.2 模型指纹变化 → 旧向量退出并从源文本重建", spec_vector_model_rebuild, "isekai_core/store.py:2450 memory_missing_embeddings"),
    ("§5.2 维度变化 → 旧向量退出并从源文本重建", spec_vector_dim_rebuild, "isekai_core/store.py:2450-2461 只比 model"),
    ("§7.1 / 阶段5 未披露不可见、披露后按转述召回、回滚撤销", spec_disclosure_recall, "isekai_core/runtime/service.py:212 disclosed_fragments"),
    ("§4.1 / §七 不从未过滤实情提取；通知与诊断无提取路径", spec_no_world_truth_extraction, "isekai_core/runtime/service.py:682 queue_world_sources"),
]

DEFERRED_ITEMS: list[tuple[str, str]] = [
    ("验收9 睡眠等待中的消息在恢复后不重复写记忆",
     "会话层的「睡眠期等待（多入一回）」本体未实现（SESSION_CORE_SPEC §4.5；grep 睡眠 / deadline / wait 在 "
     "isekai_core 无会话侧等待逻辑），属会话核心后续工作（DESIGN.md 阶段表未列此条）；记忆侧的幂等已由验收9 条目"
     "（env_id / reply 幂等 + 同轮强化幂等）覆盖，恢复后不会重复写记忆"),
]


def report(status: str, summary: str, evidence: str) -> None:
    COUNT[status] = COUNT.get(status, 0) + 1
    print(f"{status} {summary} — 证据：{evidence}", flush=True)


def check(summary: str, fn: Callable[[], str], site: str) -> None:
    try:
        evidence = fn()
    except AssertionError as exc:
        report("FAIL", summary, f"{exc} ｜ 实现位置 {site}")
        return
    except Exception:  # 探针自身出错也要如实报告
        tail = traceback.format_exc().strip().splitlines()[-1]
        report("FAIL", summary, f"探针异常：{tail} ｜ 实现位置 {site}")
        return
    report("PASS", summary, evidence)


def main() -> int:
    print(f"# MEMORY_SPEC 行为探针 @ {ROOT}（临时库；本地 embedding 桩；无真实 LLM）", flush=True)
    for summary, fn, site in CHECKS:
        check(summary, fn, site)
    for summary, evidence in DEFERRED_ITEMS:
        report("DEFERRED", summary, evidence)
    total = COUNT["PASS"] + COUNT["FAIL"] + COUNT["DEFERRED"]
    print(f"TOTAL {total} PASS {COUNT['PASS']} FAIL {COUNT['FAIL']} DEFERRED {COUNT['DEFERRED']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
