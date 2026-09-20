#!/usr/bin/env python
"""本地桩 embedding 服务（真 HTTP，OpenAI 兼容）：验证 runtime.memory_embedding_* 配置链路。

用法：python scripts/_stub_embedding.py [port]
"""

from __future__ import annotations

import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

DIM = 64


def vector_for(text: str) -> list[float]:
    """确定性伪向量：同一文本永远同一向量，够验证写入 / 召回 / 指纹。"""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = [(digest[index % len(digest)] / 255.0) - 0.5 for index in range(DIM)]
    norm = sum(value * value for value in values) ** 0.5 or 1.0
    return [round(value / norm, 6) for value in values]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        texts = payload.get("input") or []
        data = [{"index": index, "embedding": vector_for(str(text))} for index, text in enumerate(texts)]
        body = json.dumps({"data": data, "model": payload.get("model")}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[stub] {fmt % args}", flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"[stub] listening on http://127.0.0.1:{port}/v1/embeddings", flush=True)
    server.serve_forever()
