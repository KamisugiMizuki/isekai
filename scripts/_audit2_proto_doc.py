#!/usr/bin/env python
"""附录 ↔ 代码对拍（集合 / 数值），只读；用法 .venv/Scripts/python.exe scripts/_audit2_proto_doc.py，不一致返回 1。"""
from __future__ import annotations
import inspect, re, sys
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from isekai_core import config, session, store, ump, version  # noqa: E402
DOC = next((REPO / "docs").rglob("CHANNEL_PROTOCOL_APPENDIX.md")).read_text(encoding="utf-8")
SRC = {n: (REPO / "isekai_core" / n).read_text(encoding="utf-8") for n in ("channel.py", "llm.py", "log.py")}
LOG = tuple(int(x) for x in re.search(r"maxBytes=(\d+) \* (\d+) \* (\d+), backupCount=(\d+)", SRC["log.py"]).groups())
LIMITS = {k: getattr(version, k) for k in ("MAX_FRAME_BYTES", "HANDSHAKE_TIMEOUT_S", "PROTOCOL_ERROR_LIMIT", "DEFAULT_MAX_TEXT_LEN", "DEFAULT_MAX_PARTS")} | {  # ⑤ 常量名 → 代码取值
    "SEND_TIMEOUT_S": session.SEND_TIMEOUT_S, "merge_batch_max": config.RuntimeConfig().merge_batch_max, "core.log maxBytes": LOG[0] * LOG[1] * LOG[2], "core.log backupCount": LOG[3],
    "pending_outbound limit": inspect.signature(store.Store.pending_outbound).parameters["limit"].default, "ping_interval": int(re.search(r"ping_interval=(\d+)", SRC["channel.py"]).group(1)), "ping_timeout": int(re.search(r"ping_timeout=(\d+)", SRC["channel.py"]).group(1))}
RESULT: list[bool] = []


def cells(header: str, n: int) -> list[list[str]]:  # 取表头首格为 header 的表格的前 n 列（行的首列须为 `标识`）
    found = re.findall(r"^\| `([^`]+)` \|" + r"([^|]*)\|" * (n - 1), DOC.split(f"| {header} |")[1].split("\n\n")[0], re.M)
    return [[str(c).strip() for c in (r if isinstance(r, tuple) else (r,))] for r in found]


CHECKS = [
    ("② 消息类型表", {r[0]: tuple(r[1:]) for r in cells("type", 4)}, {t: ("双向" if t in ump.CLIENT_TYPES and t in ump.SERVER_TYPES else "c2s" if t in ump.CLIENT_TYPES else "s2c", "是" if t in ump.THREAD_REQUIRED else "否", "是" if t in ump.TOKEN_REQUIRED else "否") for t in ump.ALL_TYPES}),
    ("③ 阶段枚举", {r[0]: r[1] for r in cells("阶段", 2)}, {k: v for k, v in vars(ump.Stage).items() if k != "ALL" and not k.startswith("_")}),
    ("③ Err 枚举", {r[0]: r[1] for r in cells("枚举名", 2)}, {k: v for k, v in vars(ump.Err).items() if not k.startswith("_")}),
    ("③ 生成阶段 code", {r[0] for r in cells("生成码", 2)}, set(re.findall(r'LLMError\(\s*"([a-z_]+)"', SRC["llm.py"]))),
    ("⑤ 上限常量", {r[0]: r[1] for r in cells("常量", 2)}, {k: str(v) for k, v in LIMITS.items()}),
]
for name, doc, code in CHECKS:
    RESULT.append(ok := doc == code); print(f"{'PASS' if ok else 'FAIL'} {name} — {'与代码一致' if ok else f'doc={doc!r} ≠ code={code!r}'}", flush=True)
print(f"TOTAL {len(RESULT)} PASS {sum(RESULT)} FAIL {RESULT.count(False)}", flush=True)
sys.exit(1 if False in RESULT else 0)
