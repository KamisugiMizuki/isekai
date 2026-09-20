"""向量召回（MEMORY_SPEC §5.2）：真 HTTP 往返（本地桩服务）、指纹、降级、预算、融合。"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from isekai_core.runtime import embedding as embedding_mod
from isekai_core.runtime.service import RuntimeService
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边

#: 桩模型：按文本首字决定方向（测试用来构造「字面不像但向量近」的情形）
VECTORS = {
    "潮": [1.0, 0.0, 0.0],
    "信": [0.0, 1.0, 0.0],
    "船": [0.9, 0.1, 0.0],
}


class _StubHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    fail = False

    def do_POST(self) -> None:  # noqa: N802  http.server 的接口
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append({"path": self.path, "payload": payload,
                                 "auth": self.headers.get("Authorization") or ""})
        if type(self).fail:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        texts = payload.get("input") or []
        data = [
            {"index": index, "embedding": VECTORS.get(str(text)[:1], [0.0, 0.0, 1.0])}
            for index, text in enumerate(texts)
        ]
        body = json.dumps({"data": data, "model": payload.get("model")}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音
        return


class _Stub:
    def __init__(self) -> None:
        _StubHandler.calls = []
        _StubHandler.fail = False
        self.server = HTTPServer(("127.0.0.1", 0), _StubHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _service(store, **over) -> RuntimeService:
    params = {
        "instance_tokens_per_day": 400_000,
        "timeline_tokens_per_day": 150_000,
        "task_tokens_per_day": 60_000,
    }
    params.update(over)
    return RuntimeService(store, **params)


def _ready(store, world_service, *, moment=DAY * 1500):
    info, timeline_id, character_id = make_instance(store, world_service, moment=moment)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    return info, timeline_id, character_id


def _memory(store, info, timeline_id, character_id, ident: str, text: str) -> None:
    store.memory_add({
        "id": ident, "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "text": text, "kind": "fact",
        "sources": [{"kind": "claim", "ref": f"cl-{ident}"}], "happened_world": 0, "learned_world": 0,
        "recorded_world": 0, "semantic_watermark": 0, "strength": 0.6, "confidence": 0.8,
    })


def test_embed_posts_openai_shape_with_bearer(store) -> None:
    """一次真 HTTP 往返：OpenAI 兼容 /v1/embeddings，带模型与 Bearer 凭据。"""
    stub = _Stub()
    try:
        vectors = asyncio.run(embedding_mod.embed(
            ["潮位五尺", "信报到了"], model="stub-embed", base_url=stub.base_url, api_key="sk-test"
        ))
        assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        call = _StubHandler.calls[-1]
        assert call["path"] == "/v1/embeddings" and call["payload"]["model"] == "stub-embed"
        assert call["payload"]["input"] == ["潮位五尺", "信报到了"]
        assert call["auth"] == "Bearer sk-test"
    finally:
        stub.close()


def test_unconfigured_embedding_degrades_to_full_text(store) -> None:
    """缺配置 → 不调用、退化为全文召回，世界与对话都不受影响（§5.2）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    _memory(store, info, timeline_id, character_id, "mm-a", "潮位到了刻线")
    assert world_service.embedding_ready is False
    result = asyncio.run(world_service.embed_memories(info["id"], timeline_id))
    assert result == {"embedded": 0, "skipped": "not_configured"}
    recalled = world_service.recall(info["id"], timeline_id, character_id, topic="潮位")
    assert recalled["ids"] == ["mm-a"], "全文召回照常工作"
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 4 * DAY)
    assert int(store.clock_get(timeline_id)["processed_world"]) > 0, "世界推进不被 embedding 阻塞"


def test_embedding_failure_leaves_items_pending(store) -> None:
    """远程失败 → 保留待嵌入状态、不改世界状态（§5.2）。"""
    stub = _Stub()
    try:
        world_service = _service(
            store, memory_embedding_model="stub-embed", memory_embedding_base_url=stub.base_url, memory_embedding_api_key="k"
        )
        info, timeline_id, character_id = _ready(store, world_service)
        _memory(store, info, timeline_id, character_id, "mm-b", "船到了")
        _StubHandler.fail = True
        result = asyncio.run(world_service.embed_memories(info["id"], timeline_id))
        assert result["embedded"] == 0 and result["error"] == "EmbeddingError"
        assert "503" in result["reason"] and result["model"] == "stub-embed", "失败原因要一眼看得出"
        assert store.memory_missing_embeddings(info["id"], timeline_id, model="stub-embed")
        assert asyncio.run(world_service.embed_query("潮位", instance_id=info["id"], timeline_id=timeline_id)) is None, "查询向量拿不到就退化全文"
        _StubHandler.fail = False
        again = asyncio.run(world_service.embed_memories(info["id"], timeline_id))
        assert again["embedded"] == 1, "恢复后补齐"
    finally:
        stub.close()


def test_vectors_participate_in_recall_and_respect_model_fingerprint(store) -> None:
    """向量参与融合排序；模型指纹变了旧向量不参与（§5.2）。"""
    stub = _Stub()
    try:
        world_service = _service(
            store, memory_embedding_model="stub-embed", memory_embedding_base_url=stub.base_url, memory_embedding_api_key="k"
        )
        info, timeline_id, character_id = _ready(store, world_service)
        _memory(store, info, timeline_id, character_id, "mm-tide", "潮位到了刻线")   # 向量 [1,0,0]
        _memory(store, info, timeline_id, character_id, "mm-boat", "船到了")        # 向量 [0.9,0.1,0]
        _memory(store, info, timeline_id, character_id, "mm-mail", "信报到了")      # 向量 [0,1,0]
        result = asyncio.run(world_service.embed_memories(info["id"], timeline_id))
        assert result["embedded"] == 3

        # 查询「船」→ 向量上潮位与船都近，但字面只命中「船」
        vector = asyncio.run(world_service.embed_query("船", instance_id=info["id"], timeline_id=timeline_id))
        assert vector == [0.9, 0.1, 0.0]
        recalled = world_service.recall(info["id"], timeline_id, character_id, topic="船", query_vector=vector)
        top = recalled["ids"][:2]
        assert "mm-boat" in top, "字面命中在前"
        assert "mm-tide" in recalled["ids"], "向量近的条目被融合进来"

        # 换模型：旧向量不得参与
        other = _service(
            store, memory_embedding_model="别的模型", memory_embedding_base_url=stub.base_url, memory_embedding_api_key="k"
        )
        stale = other.store.memory_vector_scores(
            info["id"], timeline_id, character_id, "船", query_vector=[0.9, 0.1, 0.0], model="别的模型"
        )
        assert stale == {}, "模型不符的向量不算"
        missing = other.store.memory_missing_embeddings(info["id"], timeline_id, model="别的模型")
        assert len(missing) == 3, "换模型后从源文本重建"
    finally:
        stub.close()


def test_budget_blocks_embedding_but_keeps_text_recall(store) -> None:
    """预算耗尽 → 向量任务待处理，全文召回仍可用（验收 13）。"""
    stub = _Stub()
    try:
        world_service = _service(
            store, memory_embedding_model="stub-embed", memory_embedding_base_url=stub.base_url,
            memory_embedding_api_key="k", task_tokens_per_day=1,
        )
        info, timeline_id, character_id = _ready(store, world_service)
        _memory(store, info, timeline_id, character_id, "mm-c", "潮位到了刻线")
        result = asyncio.run(world_service.embed_memories(info["id"], timeline_id))
        assert result["embedded"] == 0 and result.get("paused") is True
        assert asyncio.run(world_service.embed_query("潮位", instance_id=info["id"], timeline_id=timeline_id)) is None
        recalled = world_service.recall(info["id"], timeline_id, character_id, topic="潮位")
        assert recalled["ids"] == ["mm-c"], "全文召回不受影响"
    finally:
        stub.close()
