"""终局矩阵：跑齐 8 支 SPEC 探针 + 桌面壳分段 + pytest，按行末汇总行统计。

用法（仓库根）：.venv/Scripts/python.exe scripts/_audit2_matrix.py [--skip-desk] [--quiet]

真源是各探针自己打印的 `TOTAL n PASS n FAIL n DEFERRED n`（以及桌面壳的 `PASS=n FAIL=n DEFERRED=n`），
这里只做搬运与计数，不改判据、不猜数字；任何一段没跑出汇总行就报「未取到」而不是算零。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
CORE_PROBES = ["chan", "sc", "ws", "wr", "ee", "mem", "card", "design"]
DESK_ARGS = {"static": ["static"], "main": [], "restore": ["restore"], "notify": ["notify"]}

TOTAL_RE = re.compile(r"TOTAL (\d+) PASS (\d+) FAIL (\d+) DEFERRED (\d+)")
DESK_RE = re.compile(r"PASS=(\d+) FAIL=(\d+) DEFERRED=(\d+)")
DESK_LINE = "[{label}]{tail}PASS={p} FAIL={f} DEFERRED={d}"  # 段名锚定的汇总行（见下方 _desk_counts）
COUNT_RE = re.compile(r"^\[(PASS|FAIL|DEFERRED)\s*\]", re.MULTILINE)


def _run(cmd: list[str], *, timeout: int = 1800) -> tuple[str, int]:
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    return (proc.stdout or "") + (proc.stderr or ""), proc.returncode


def _counts(text: str) -> tuple[int, int, int]:
    found = {key: 0 for key in ("PASS", "FAIL", "DEFERRED")}
    for key in COUNT_RE.findall(text):
        found[key] += 1
    return found["PASS"], found["FAIL"], found["DEFERRED"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-desk", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    rows: list[tuple[str, int, int, int, str]] = []
    started = time.time()
    for name in CORE_PROBES:
        script = ROOT / "scripts" / f"_audit2_{name}.py"
        if not script.is_file():
            rows.append((name, 0, 0, 0, "探针不存在"))
            continue
        text, code = _run([PY, str(script)])
        match = TOTAL_RE.search(text)
        if match:
            total, passed, failed, deferred = (int(item) for item in match.groups())
        else:
            passed, failed, deferred = _counts(text)
            total = passed + failed + deferred
        rows.append((name, passed, failed, deferred, "" if total else "探针没跑出任何条目"))
        if not args.quiet:
            print(f"[{name}] PASS={passed} FAIL={failed} DEFERRED={deferred}（exit={code}）")

    if not args.skip_desk:
        rows_desk: list[tuple[str, int, int, int, str]] = []
        for label, extra in DESK_ARGS.items():
            text, code = _run([PY, str(ROOT / "scripts" / "_audit2_desk.py"), *extra])
            # 段名锚定：先在该段自己的汇总行里找，找不到才退回第一条匹配（裸跑 last 行可能是别的段）
            labelled = re.search(rf"\[{re.escape(label)}\][^\n]*PASS=(\d+) FAIL=(\d+) DEFERRED=(\d+)", text)
            match = labelled or DESK_RE.search(text)
            if match:
                passed, failed, deferred = (int(item) for item in match.groups())
                rows_desk.append((f"desk:{label}", passed, failed, deferred, "" if labelled else "按第一条汇总行计数（未锚定到段名）"))
            else:
                passed, failed, deferred = _counts(text)
                rows_desk.append((f"desk:{label}", passed, failed, deferred, "未取到汇总行（按条目行计数）"))
            if not args.quiet:
                print(f"[desk:{label}] PASS={rows_desk[-1][1]} FAIL={rows_desk[-1][2]} DEFERRED={rows_desk[-1][3]}（exit={code}）")
        rows.extend(rows_desk)

    text, code = _run([PY, "-m", "pytest", "-o", "addopts=", "-q"])
    match = re.search(r"(\d+) passed", text)
    passed = int(match.group(1)) if match else 0
    failed = len(re.findall(r"(\d+) failed", text))
    print(f"[pytest] passed={passed} exit={code}")

    print("\n=== 终局矩阵 ===")
    for name, ok, bad, deferred, note in rows:
        flag = "⚠" if bad else " "
        print(f"{flag} {name:<14} PASS {ok:>3}  FAIL {bad:>2}  DEFERRED {deferred:>2}  {note}")
    total_fail = sum(row[2] for row in rows)
    print(f"\n总计 FAIL={total_fail}，pytest passed={passed}，用时 {time.time() - started:.0f}s")
    return 1 if (total_fail or not passed) else 0


if __name__ == "__main__":
    sys.exit(main())
