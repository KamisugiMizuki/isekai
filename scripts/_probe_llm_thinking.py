# -*- coding: utf-8 -*-
"""验证 deepseek-flash「空返回」的修复配置：同一 propose 形状 prompt 的变体实验。

背景（2026-10 实测）：默认配置下该 prompt 每次把预算全烧在 reasoning 里
（reasoning_tokens=预算全额、finish=length、content 空）；加倍到 4096 也烧光。
官方支持两种控制：reasoning_effort=low / thinking={"type":"disabled"}。
本探针逐个变体打点，选能稳定给出 content 的配置。

用法：cd D:/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_probe_llm_thinking.py
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

VARIANTS: list[tuple[str, dict]] = [
    ("baseline", {}),
    ("effort=low", {"reasoning_effort": "low"}),
    ("disabled", {"thinking": {"type": "disabled"}}),
    ("low+disabled", {"reasoning_effort": "low", "thinking": {"type": "disabled"}}),
]
N_EACH = 2


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
    url = llm.base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {llm.api_key}", "Content-Type": "application/json"}
    summary: dict[str, dict] = {}
    with httpx.Client(trust_env=False, timeout=90.0) as client:
        for label, extra in VARIANTS:
            ok = 0
            rs = []
            for i in range(N_EACH):
                payload = {"model": llm.model, "messages": messages,
                           "max_tokens": int(llm.max_tokens), "temperature": 0.7,
                           "stream": False, **extra}
                t0 = time.time()
                try:
                    r = client.post(url, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    print(f"[{label} #{i}] transport {type(exc).__name__}")
                    continue
                dt = time.time() - t0
                if r.status_code != 200:
                    print(f"[{label} #{i}] HTTP {r.status_code}: {r.text[:140]}")
                    continue
                data = r.json()
                ch = (data.get("choices") or [{}])[0]
                msg = ch.get("message") or {}
                content = msg.get("content") or ""
                reasoning = msg.get("reasoning_content") or ""
                det = (data.get("usage") or {}).get("completion_tokens_details") or {}
                rtok = det.get("reasoning_tokens")
                rs.append(rtok or 0)
                hit = bool(content.strip())
                ok += hit
                body = content.strip().replace("\n", " ")[:100]
                print(f"[{label} #{i}] {'OK ' if hit else 'EMPTY'} len={len(content)} "
                      f"reasoning_tokens={rtok} finish={ch.get('finish_reason')} {dt:.1f}s "
                      f"{('| ' + body) if hit else ''}")
                time.sleep(0.4)
            summary[label] = {"ok": ok, "of": N_EACH, "reasoning_tokens": rs}
    print("\nSUMMARY:")
    for label, item in summary.items():
        print(f"  {label:14s} content 出字 {item['ok']}/{item['of']}  reasoning_tokens={item['reasoning_tokens']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
