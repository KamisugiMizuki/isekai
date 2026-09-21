#!/usr/bin/env python
"""世界包附录 ↔ 代码对拍（键集 / 枚举 / 数值 / 标识前缀），只读。

用法：.venv/Scripts/python.exe scripts/_audit2_pkg_doc.py
不一致返回 1 并打印差异；doc 与代码必须一起改。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from isekai_core import version  # noqa: E402
from isekai_core.world import package, portable, validate  # noqa: E402

DOC = next((REPO / "docs").rglob("WORLD_PACKAGE_APPENDIX.md")).read_text(encoding="utf-8")
SRC = {
    name: (REPO / "isekai_core" / name).read_text(encoding="utf-8")
    for name in ("session.py", "store.py", "channel.py")
} | {"world/instances.py": (REPO / "isekai_core/world/instances.py").read_text(encoding="utf-8")}
RESULT: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULT.append(bool(ok))
    suffix = "" if ok else f" — {detail}"
    print(f"{'PASS' if ok else 'FAIL'} {name}{suffix}", flush=True)


def section(header: str) -> str:
    return DOC.split(header)[1].split("\n## ")[0]


def table_rows(text: str) -> list[list[str]]:
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.startswith("|") and not set(line) <= set("|-: ")
    ]


def numeric(label: str) -> int | None:
    for row in table_rows(section("## ⑥")):
        if row[0] == f"`{label}`" and len(row) > 2 and row[1].isdigit():
            return int(row[1])
    return None


def joined(values) -> str:
    """doc 里「取值」列的写法：反引号包住每项、` / ` 相连。"""
    return " / ".join(f"`{item}`" for item in values)


# ① / ② 文件形态与容器
check("② 容器格式与版本", version.CONTAINER_FORMAT == "isekai.instance" and "`isekai.instance`" in DOC, version.CONTAINER_FORMAT)
check("② 容器主版本", f'`CONTAINER_VERSION = "{version.CONTAINER_VERSION}"`' in DOC, version.CONTAINER_VERSION)
check("② 容器上限", portable.MAX_CONTAINER_BYTES == 256 << 20 and "`MAX_CONTAINER_BYTES = 256 MiB`" in DOC, portable.MAX_CONTAINER_BYTES)

# ③ 标识前缀：doc 表里的前缀必须真由代码产出（按 f"…-{" 找生成器）
prefixes = set(
    re.findall(
        r'f"([a-z]{1,3})-',
        "".join(SRC.values()) + (REPO / "isekai_core/world/package.py").read_text(encoding="utf-8"),
    )
)
doc_prefixes = {
    row[0].strip("`").rstrip("-") for row in table_rows(section("③ 稳定标识编码")) if re.fullmatch(r"`[a-z]{1,3}-`", row[0])
}
check(
    "③ 生成器前缀表",
    doc_prefixes and doc_prefixes <= prefixes,
    f"doc 里但代码没有：{sorted(doc_prefixes - prefixes)}（代码里：{sorted(prefixes)}）",
)

# ④ 顶层键
key_lines = DOC.splitlines()
start = next(index for index, line in enumerate(key_lines) if line.startswith("`meta`"))
keys_para: list[str] = []
for line in key_lines[start:]:
    if not line.strip():
        break
    keys_para.append(line)
doc_keys = tuple(re.findall(r"`([a-z_]+)`", " ".join(keys_para)))
check("④ 顶层键集合", doc_keys == tuple(package.template_package("x").keys()), f"doc={doc_keys}")

# ⑤ 枚举
check("⑤ meta.density", joined(package.DENSITIES) in DOC, package.DENSITIES)
check("⑤ events.density 目标", joined(validate.DENSITY_TARGETS) in DOC, tuple(validate.DENSITY_TARGETS))
check("⑤ entities.kind", joined(validate.ENTITY_KINDS) in DOC, validate.ENTITY_KINDS)
check("⑤ lifespan.mode", joined(validate.LIFESPAN_MODES) in DOC, validate.LIFESPAN_MODES)
check("⑤ 效果 expiry", joined(validate.EXPIRY_KINDS) in DOC, validate.EXPIRY_KINDS)
check("⑤ 效果 kind", len(validate.SUPPORTED_EFFECTS) == 8 and all(f"`{k}`" in DOC for k in validate.SUPPORTED_EFFECTS), tuple(validate.SUPPORTED_EFFECTS))
check("⑤ 能力表", all(f"`{item}`" in DOC for item in version.CAPABILITIES), version.CAPABILITIES)
check("⑤ 历法时段键", joined(validate.SEGMENT_KEYS) in DOC, validate.SEGMENT_KEYS)

# ⑥ 限额
check("⑥ MAX_PACKAGE_BYTES", package.MAX_PACKAGE_BYTES == 1 << 20 and "`MAX_PACKAGE_BYTES` | 1 MiB" in DOC, package.MAX_PACKAGE_BYTES)
check("⑥ MAX_DEPTH", numeric("MAX_DEPTH") == validate.MAX_DEPTH, f"doc={numeric('MAX_DEPTH')} code={validate.MAX_DEPTH}")
check("⑥ MAX_NODES", numeric("MAX_NODES") == validate.MAX_NODES, f"doc={numeric('MAX_NODES')} code={validate.MAX_NODES}")
check("⑥ MAX_STRING", numeric("MAX_STRING") == validate.MAX_STRING, f"doc={numeric('MAX_STRING')} code={validate.MAX_STRING}")
check("⑥ MAX_COLLECTION", numeric("MAX_COLLECTION") == validate.MAX_COLLECTION, f"doc={numeric('MAX_COLLECTION')} code={validate.MAX_COLLECTION}")
check("⑥ PACKAGE_SCHEMA_VERSION", f"`{package.PACKAGE_SCHEMA_VERSION}`" in DOC, package.PACKAGE_SCHEMA_VERSION)
check("③ 名称长度无上限（NAME_MAX_LEN 已删）", not hasattr(package, "NAME_MAX_LEN") and "NAME_MAX_LEN" in DOC, "doc 自陈已删 → 代码里也不能有")

print(f"TOTAL {len(RESULT)} PASS {sum(RESULT)} FAIL {RESULT.count(False)}", flush=True)
sys.exit(1 if False in RESULT else 0)
