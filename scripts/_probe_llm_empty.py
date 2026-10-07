# -*- coding: utf-8 -*-
"""抓 deepseek-flash「空返回」的响应全貌：复刻 propose 形状的请求打点。

背景：runtime 的 propose_intents / memory extract 会偶发 empty_completion
（llm.chat 两次尝试都空）。本探针用 planning.prompt 的真实形状直发 20 次，
打印 finish_reason / content / reasoning_content / usage，并对空返回做一次
「4096 预算救回」实验，判断是不是推理预算被烧空。

用法：cd D:/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_probe_llm_empty.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

for key in ("http_proxy", "https_proxy", "all_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(key, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from isekai_core.config import load_config  # noqa: E402
from isekai_core.runtime import planning  # noqa: E402

N = 20


def main() -> int:
    cfg = load_config()
    llm = cfg.llm
    messages = planning.prompt(
        name="堤禾", occupation="堤务吏", world_label="灰潮纪 雾月 3 日 晨",
        aims=[{"object": "去堤尾看看前两日停摆的驿站", "stage": "adopted"}],
        knowledge=[
            {"source": "巷口", "text": "听说昨夜退潮比往常晚了半个时辰，堤上有人守着不许靠近。"},
            {"source": "布告", "text": "堤务房贴出告示：本轮修堤人手不足，各里自行轮值。"},
            {"source": "旧历", "text": "上旬的潮信连着三日偏低，老堤工说这兆头不好。"},
        ],
        effects=[{"kind": "source_delay", "target": "src-1"}],
        observations=[{"name": "潮位", "value": "偏低", "unit": ""}],
        allowed={"activity_constraint": ["role-堤务吏", "region-滩区"], "route_blocked": ["region-滩区"],
                 "public_notice": ["src-1"], "rumor_spread": ["src-1"], "institution_state": ["off-1"]},
        pending="堤尾驿站停摆的真正原因",
    )
    print(f"prompt chars={sum(len(m['content']) for m in messages)} model={llm.model} "
          f"base={llm.base_url}")
    url = llm.base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {llm.api_key}", "Content-Type": "application/json"}
    empties = 0
    rescue = 0
    with httpx.Client(trust_env=False, timeout=90.0) as client:
        for i in range(N):
            payload = {"model": llm.model, "messages": messages, "max_tokens": int(llm.max_tokens),
                       "temperature": 0.7, "stream": False}
            t0 = time.time()
            try:
                r = client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                print(f"[{i:2d}] transport {type(exc).__name__}")
                continue
            dt = time.time() - t0
            if r.status_code != 200:
                print(f"[{i:2d}] HTTP {r.status_code}: {r.text[:160]}")
                continue
            data = r.json()
            ch = (data.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
            usage = data.get("usage") or {}
            fin = ch.get("finish_reason")
            if not content.strip():
                empties += 1
                print(f"[{i:2d}] EMPTY finish={fin} reasoning_len={len(reasoning)} "
                      f"usage={usage} {dt:.1f}s")
                payload2 = dict(payload, max_tokens=4096)
                t1 = time.time()
                try:
                    r2 = client.post(url, json=payload2, headers=headers)
                except httpx.HTTPError as exc:
                    print(f"      → 4096 transport {type(exc).__name__}")
                    continue
                dt2 = time.time() - t1
                if r2.status_code == 200:
                    d2 = r2.json()
                    ch2 = (d2.get("choices") or [{}])[0]
                    m2 = ch2.get("message") or {}
                    c2 = m2.get("content") or ""
                    if c2.strip():
                        rescue += 1
                        print(f"      → 4096 救回 len={len(c2)} finish={ch2.get('finish_reason')} {dt2:.1f}s")
                    else:
                        print(f"      → 4096 仍空 reasoning_len={len(m2.get('reasoning_content') or '')} "
                              f"finish={ch2.get('finish_reason')} usage={d2.get('usage')} {dt2:.1f}s")
                else:
                    print(f"      → 4096 HTTP {r2.status_code}")
            else:
                print(f"[{i:2d}] ok len={len(content)} finish={fin} reasoning_len={len(reasoning)} "
                      f"out_tokens={usage.get('completion_tokens')} {dt:.1f}s")
            time.sleep(0.4)
    print(f"\nSUMMARY: {N} 次里空 {empties}；4096 预算救回 {rescue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
