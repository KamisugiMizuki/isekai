"""显式运行「保留在测试套里的」三套参考审计。

为什么要单独写一个运行器，而不是直接 `python .hermes/self_audit.py`：

1. **三套审计脚本自己从不设置退出码**——它们只 `print` 汇总，FAIL 时也退出 0。
   本运行器解析它们的 `PASS/FAIL` 行，**由自己决定退出码**，才能当门禁用。
2. **它们要求靶子是「根目录」**（内部拼 `{root}/data/isekai.db`），而人通常只有 db 路径。
   本运行器的 `--instance` 两种都收：给目录取 `data/isekai.db`，给 `.db` 文件取其 `../..`。
3. **脚本用仓库根下的相对路径读源码与规格文档**，所以必须在仓库根跑。
4. **不做管道**：子进程 stdout 直接落盘到日志文件，父进程再读文件
   （本项目历史上带管道的子进程会 EPERM）。子进程另设 `PYTHONIOENCODING=utf-8`，
   免得重定向后 CJK 按宿主 locale(cp936) 写出、父进程解错码。

默认跑的是 `tools/archive_reference_audits/` 里的**归档件**（`MANIFEST.sha256` 可与
`.hermes/` 原件对哈希），而不是随时可能被清掉的 `.hermes/`。

用法::

    .venv\\Scripts\\python.exe tools/run_audits.py --instance .hermes/acceptH
    .venv\\Scripts\\python.exe tools/run_audits.py --instance .hermes/acceptH/data/isekai.db
    .venv\\Scripts\\python.exe tools/run_audits.py --instance <db> --json
    .venv\\Scripts\\python.exe tools/run_audits.py --help

退出码：0 = 三套全 PASS；1 = 有 FAIL 或某套脚本自身出错；2 = 用法 / 靶子不可用。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDITS_DIR = Path(__file__).resolve().parent / "archive_reference_audits"

#: 运行顺序即报告顺序；名字 = 归档目录下的文件名（不含 .py）。
AUDIT_SCRIPTS: tuple[str, ...] = ("self_audit", "final_audit", "perf_audit")

#: 三套脚本每项打印一行：`PASS  <名字>` / `FAIL  <名字>`，有补充信息时再接 `  [<detail>]`
#: （分隔符固定两个空格）。汇总行 `合计 30 项：PASS 28，FAIL 2` 不在行首，不会被误计。
_ITEM_RE = re.compile(r"^(?P<status>PASS|FAIL)  (?P<name>.+?)(?:  \[(?P<detail>.*)\])?$")


@dataclass
class AuditResult:
    """一套审计的结果。"""

    script: str
    returncode: int
    checks: int = 0
    passed: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)
    log_path: Path | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.error and self.checks > 0 and self.failed == 0


def resolve_target(instance: str) -> tuple[Path, Path]:
    """把 `--instance` 归一化成 `(根目录, db 路径)`。

    接受：实例根目录（内部找 `data/isekai.db`）或直接的 `.db` 文件路径。
    目录布局不匹配时抛 `ValueError`（交给上层转成退出码 2）。
    """
    path = Path(instance).expanduser()
    if not path.exists():
        raise ValueError(f"靶子不存在：{path}")
    if path.is_dir():
        db = path / "data" / "isekai.db"
        if not db.is_file():
            raise ValueError(f"目录里没有 data/isekai.db：{path}")
        return path.resolve(), db.resolve()
    db = path.resolve()
    root = db.parent.parent
    if not root.is_dir():
        raise ValueError(f"db 的上一级目录结构不是 <root>/data/isekai.db：{db}")
    return root, db


def preflight(db: Path) -> list[str]:
    """只读侦察靶子，返回给人看的提示（不阻断运行）。

    关键用途：`acceptF` 这类 **S-4 之前**建的实例仍留着 `ix_effect_target` /
    `ix_effect_retire`，会让 `self_audit`(2 条) 与 `perf_audit`(1 条) **预期地** FAIL。
    不先说明的话，很容易被误读成回归。
    """
    notes: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - 取决于文件系统
        return [f"无法只读打开靶子：{exc}"]
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    notes.append(f"靶子有 {len(tables)} 张表、{len(indexes)} 个索引")
    for table in ("effect_superseded", "world_ledger", "relation_state"):
        if table not in tables:
            notes.append(f"[警告] 缺表 {table}：final_audit / self_audit 会 FAIL（实例过旧）")
    stale = sorted(indexes & {"ix_effect_target", "ix_effect_retire"})
    if stale:
        notes.append(
            f"[警告] 该实例建于 S-4 索引收敛之前，仍留着 {stale}："
            "self_audit 有 2 条、perf_audit 有 1 条会**预期地** FAIL（不是回归）。"
            "要 78/78 请换一个由当前代码新建的实例。"
        )
    return notes


def run_one(script: str, root: Path, audits_dir: Path, log_dir: Path) -> AuditResult:
    """跑一套审计：stdout/stderr **落盘**，父进程读文件解析（不走管道）。"""
    path = audits_dir / f"{script}.py"
    result = AuditResult(script=script, returncode=-1)
    if not path.is_file():
        result.error = f"找不到审计脚本：{path}"
        return result

    log_path = log_dir / f"{script}.log"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # 重定向后也别按 cp936 写
    with log_path.open("wb") as handle:
        proc = subprocess.run(  # noqa: S603 - 固定脚本 + 固定解释器，无 shell
            [sys.executable, str(path), str(root)],
            cwd=str(REPO_ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
    result.returncode = proc.returncode
    result.log_path = log_path

    text = log_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        match = _ITEM_RE.match(line)
        if match is None:
            continue
        result.checks += 1
        if match.group("status") == "PASS":
            result.passed += 1
            continue
        result.failed += 1
        name = match.group("name")
        detail = match.group("detail")
        result.failures.append(name + (f"  [{detail}]" if detail else ""))
    if result.checks == 0:
        tail = "\n".join(text.splitlines()[-8:])
        result.error = f"没解析到任何 PASS/FAIL 行（脚本可能崩了）。输出尾部：\n{tail}"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_audits.py",
        description="依次运行 self_audit / final_audit / perf_audit 三套参考审计并给出退出码。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0 = 三套全 PASS；1 = 有 FAIL 或某套脚本自身出错；2 = 用法 / 靶子不可用。\n"
            "注意：这些审计只查 sqlite_master 与源码 / 规格文本，所以任意「结构完整」的实例都能跑；\n"
            "但 S-4 之前建的旧实例上，索引收敛相关的 3 条会预期 FAIL。"
        ),
    )
    parser.add_argument(
        "--instance",
        required=True,
        metavar="<db 路径|实例根目录>",
        help="实例根目录（内部取 data/isekai.db）或直接给 isekai.db 路径。",
    )
    parser.add_argument(
        "--audits-dir",
        default=str(DEFAULT_AUDITS_DIR),
        metavar="<目录>",
        help="审计脚本所在目录（默认：tools/archive_reference_audits）。",
    )
    parser.add_argument(
        "--log-dir",
        default="",
        metavar="<目录>",
        help="审计原始输出的落盘目录（默认：新建临时目录，路径会打印出来）。",
    )
    parser.add_argument("--json", action="store_true", help="额外输出一行 JSON 汇总（给脚本消费）。")
    parser.add_argument(
        "--quiet", action="store_true", help="只打印汇总，不逐条打印 FAIL。"
    )
    args = parser.parse_args(argv)

    try:
        root, db = resolve_target(args.instance)
    except ValueError as exc:
        print(f"用法错误：{exc}", file=sys.stderr)
        return 2

    audits_dir = Path(args.audits_dir).expanduser().resolve()
    if not audits_dir.is_dir():
        print(f"用法错误：审计目录不存在：{audits_dir}", file=sys.stderr)
        return 2

    print("=" * 78)
    print("参考审计运行器")
    print(f"  仓库根  : {REPO_ROOT}")
    print(f"  审计目录: {audits_dir}")
    print(f"  靶子根  : {root}")
    print(f"  靶子 db : {db}")
    print("=" * 78)
    for note in preflight(db):
        print(f"  {note}")
    print("-" * 78)

    if args.log_dir:
        log_dir = Path(args.log_dir).expanduser().resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        temporary = False
    else:
        log_dir = Path(tempfile.mkdtemp(prefix="isekai-audits-"))
        temporary = True

    results: list[AuditResult] = []
    for script in AUDIT_SCRIPTS:
        print(f">>> {script} ... ", end="", flush=True)
        result = run_one(script, root, audits_dir, log_dir)
        results.append(result)
        print(
            f"PASS {result.passed}/{result.checks}"
            if result.ok
            else f"FAIL {result.failed}/{result.checks}（passed {result.passed}）"
        )

    print("-" * 78)
    total_checks = sum(item.checks for item in results)
    total_passed = sum(item.passed for item in results)
    total_failed = sum(item.failed for item in results)
    errored = [item for item in results if item.error]

    for result in results:
        status = "PASS" if result.ok else "FAIL"
        # `脚本自身 exit` 恒为 0——审计脚本从不设退出码（README §4 D-1）。
        # 真正决定门禁的是本运行器最后的退出码，不是这个数。
        print(f"{status:4}  {result.script:<11} {result.passed:>3}/{result.checks:<3} "
              f"(脚本自身 exit={result.returncode}, log={result.log_path})")
        if result.error:
            print(f"      [错误] {result.error}")
        elif not args.quiet:
            for failure in result.failures:
                print(f"      FAIL  {failure}")

    print("-" * 78)
    print(f"合计 {total_checks} 项：PASS {total_passed}，FAIL {total_failed}，"
          f"脚本自身出错 {len(errored)} 套")
    if args.json:
        print(json.dumps({
            "instance": str(db),
            "root": str(root),
            "checks": total_checks,
            "passed": total_passed,
            "failed": total_failed,
            "errored": [item.script for item in errored],
            "audits": [
                {
                    "script": item.script,
                    "checks": item.checks,
                    "passed": item.passed,
                    "failed": item.failed,
                    "returncode": item.returncode,
                    "error": item.error,
                    "failures": item.failures,
                }
                for item in results
            ],
        }, ensure_ascii=False))

    if temporary:
        print(f"（原始输出留在临时目录：{log_dir}；用 --log-dir 指定固定位置可保留）")

    code = 0 if (total_checks > 0 and total_failed == 0 and not errored) else 1
    print(f"退出码 {code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
