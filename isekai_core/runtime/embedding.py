"""远程 embedding（MEMORY_SPEC §5.2）：只做一次 HTTP 往返与指纹，不管降级策略。

不打包本地模型：地址、模型、凭据独立配置，可显式复用 LLM 凭据。缺少配置、请求失败或单条
未嵌入时由调用方退化为全文召回——本模块只负责把「发什么、怎么解析、指纹是什么」讲清楚。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from ..log import get_logger

log = get_logger("isekai.embedding")

TIMEOUT_S = 20.0


class EmbeddingError(RuntimeError):
    """embedding 不可用（缺配置 / 网络 / 响应异常）：调用方应退化全文召回并保留待处理状态。"""


def fingerprint(model: str, dim: int) -> str:
    """模型指纹（模型 + 维度 + 归一化约定）：变了就不能混算旧向量。"""
    return f"{model}:{dim}:l2"


def endpoint(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    if not base:
        raise EmbeddingError("未配置 embedding 地址")
    return f"{base}/embeddings" if base.endswith("/v1") else f"{base}/v1/embeddings"


async def embed(
    texts: list[str],
    *,
    model: str,
    base_url: str,
    api_key: str = "",
    timeout: float = TIMEOUT_S,
) -> list[list[float]]:
    """一次请求把一批文本嵌入；只发送该任务需要的文本（不上传整库）。"""
    batch = [str(item or "") for item in texts]
    if not batch:
        return []
    if not model:
        raise EmbeddingError("未配置 embedding 模型")
    url = endpoint(base_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {"model": model, "input": batch}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, content=json.dumps(payload).encode("utf-8"))
    if response.status_code >= 400:
        raise EmbeddingError(f"embedding HTTP {response.status_code}")
    try:
        data = json.loads(response.content.decode("utf-8", "replace"))
        items = data["data"]
        vectors = [list(item["embedding"]) for item in sorted(items, key=lambda row: row.get("index", 0))]
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingError("embedding 响应缺少 data[].embedding") from exc
    if len(vectors) != len(batch):
        raise EmbeddingError(f"embedding 数量不符：{len(vectors)} != {len(batch)}")
    dims = {len(item) for item in vectors}
    if len(dims) != 1 or not dims.pop():
        raise EmbeddingError("embedding 维度不一致")
    return vectors


def pack(vector: list[float]) -> bytes:
    """归一化后落库（float32 小端）：点积即余弦，避免每次召回重复归一。"""
    import struct

    norm = sum(value * value for value in vector) ** 0.5 or 1.0
    return struct.pack(f"<{len(vector)}f", *[value / norm for value in vector])


def content_hash(text: str) -> str:
    """源文本版本：文本变了就该重新嵌入。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]
