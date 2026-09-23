"""组装发行件（USER_INTERFACE_DESIGN 实施进度 · U5 / ONBOARDING §3.2）。

做法：自带解释器（复用本机已有的 CPython 3.11，不联网下载）+ 壳 exe + 核心源码 + 随发行样例，
打成一个目录，再压成 zip。装完直接双击 `isekai.exe`：

    release/isekai/
      isekai.exe                ← 壳（Tauri，release 构建）
      runtime/python/           ← 自带解释器（核心与规则插件都用它）
      runtime/isekai_core/      ← 核心源码
      runtime/examples/         ← 随发行样例（世界包 + 角色卡 + 潮汐规则）
      runtime/site-packages/    ← 运行依赖（websockets / httpx / PyYAML …）
      README.md  LICENSE  发行说明.md

数据不放进程序目录：发行件把数据根落在 `%LOCALAPPDATA%\\isekai`（`isekai_core.config.user_data_root`）。
跑法：.venv/Scripts/python.exe scripts/build_release.py [--skip-shell] [--out release]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON_HOME = Path.home() / "AppData" / "Roaming" / "uv" / "python"
PYTHON_LINK = PYTHON_HOME / "cpython-3.11-windows-x86_64-none"
#: 运行依赖（与 pyproject 的 dependencies 同源）；开发依赖不进发行件
RUNTIME_PACKAGES = (
    "websockets", "httpx", "httpcore", "h11", "anyio", "idna", "certifi", "sniffio", "sniffio",
    "yaml", "yaml-*.dist-info", "websockets-*.dist-info", "httpx-*.dist-info", "httpcore-*.dist-info",
    "h11-*.dist-info", "anyio-*.dist-info", "idna-*.dist-info", "certifi-*.dist-info", "sniffio-*.dist-info",
    "PyYAML-*.dist-info", "typing_extensions.py",
)
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
VERSION = "0.1.0"
APP_NAME = "isekai"


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("·", " ".join(str(part) for part in command), flush=True)
    subprocess.run(command, cwd=str(cwd or REPO), check=True)


def copy_tree(source: Path, target: Path, *, ignore_extra: set[str] | None = None) -> int:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source, target,
        ignore=shutil.ignore_patterns(*SKIP_DIRS, *(ignore_extra or set())),
        dirs_exist_ok=False,
    )
    return sum(1 for item in target.rglob("*") if item.is_file())


def build_shell() -> Path:
    """release 构建壳（资源在编译期烘进 exe，所以前端要先 build）。"""
    npm = "C:/Program Files/nodejs/npm.cmd"
    run([npm, "run", "build"], cwd=REPO / "desktop")
    run(["cargo", "build", "--release", "--features", "custom-protocol"], cwd=REPO / "desktop" / "src-tauri")
    exe = REPO / "desktop" / "src-tauri" / "target" / "release" / f"{APP_NAME}-desktop.exe"
    if not exe.exists():
        raise SystemExit(f"壳没有构建出来：{exe}")
    return exe


def resolve_python() -> Path:
    if PYTHON_LINK.exists():
        return PYTHON_LINK.resolve()
    candidates = sorted(PYTHON_HOME.glob("cpython-3.11*"))
    if not candidates:
        raise SystemExit(f"没有找到本机 CPython 3.11：{PYTHON_HOME}（装 .venv 时用的那个）")
    return candidates[0].resolve()


def copy_runtime(python_home: Path, runtime: Path) -> dict[str, int]:
    """自带解释器：整份 CPython（含 Lib / DLLs），再补上运行依赖与核心源码。"""
    counts = {}
    counts["python"] = copy_tree(python_home, runtime / "python")
    counts["isekai_core"] = copy_tree(REPO / "isekai_core", runtime / "isekai_core")
    counts["examples"] = copy_tree(REPO / "examples", runtime / "examples")

    site = runtime / "python" / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    venv_site = REPO / ".venv" / "Lib" / "site-packages"
    copied = 0
    for pattern in RUNTIME_PACKAGES:
        for item in venv_site.glob(pattern):
            target = site / item.name
            if target.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, target, ignore=shutil.ignore_patterns(*SKIP_DIRS))
                copied += sum(1 for node in target.rglob("*") if node.is_file())
            else:
                shutil.copy2(item, target)
                copied += 1
    counts["deps"] = copied

    # 依赖清单：发行件跑起来以后要能自证带了什么（也方便排错）
    (runtime / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "app": APP_NAME,
                "version": VERSION,
                "python": python_home.name,
                "built_real": int(time.time()),
                "deps": list(RUNTIME_PACKAGES),
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    counts["manifest"] = 1
    return counts


RELEASE_NOTE = """# 发行说明（{version}）

## 怎么用

1. 解压这个目录到任意位置（不要解压到需要管理员权限的地方）。
2. 双击 `isekai.exe`：程序自己带运行环境，不需要另外装 Python。
3. 第一次打开会进「首次设置」：先做本机检查，再填 AI 服务的地址与密钥，然后从样例世界开始。
4. 选项都在程序里：设置 → AI 服务（密钥可随时改）、设置 → 数据与备份（立即备份、恢复、搬迁）。
5. 卸载 = 删除这个目录；你的数据在 `%LOCALAPPDATA%\\isekai`，要一起清掉再删除那个目录。

## 数据与隐私

- 数据根：`%LOCALAPPDATA%\\isekai`（数据库、素材、日志都在这里，程序目录只放程序）。
- 密钥写在数据根的 `config/config.yaml` 里，只在本机使用；备份文件里不含密钥。
- 规则插件是本机扩展程序：登记时会显示来源、规则与版本、入口，进程隔离不是完整安全沙箱。

## 这一版里还没有的

- 安装程序（当前是 zip + 手动解压；卸载靠删除目录）。
- 面向普通用户的三条完整路径的真实试用记录（需要真人试跑，尚未做）。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="组装发行件")
    parser.add_argument("--out", default="release", help="输出目录（默认 release/）")
    parser.add_argument("--skip-shell", action="store_true", help="跳过壳构建（复用已有 release exe）")
    args = parser.parse_args()

    out = (REPO / args.out).resolve()
    target = out / APP_NAME
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    exe = (
        REPO / "desktop" / "src-tauri" / "target" / "release" / f"{APP_NAME}-desktop.exe"
        if args.skip_shell
        else build_shell()
    )
    if not exe.exists():
        raise SystemExit(f"没有可用的壳 exe：{exe}")
    shutil.copy2(exe, target / f"{APP_NAME}.exe")

    python_home = resolve_python()
    counts = copy_runtime(python_home, target / "runtime")
    for name in ("README.md", "LICENSE"):
        source = REPO / name
        if source.exists():
            shutil.copy2(source, target / name)
    (target / "发行说明.md").write_text(RELEASE_NOTE.format(version=VERSION), encoding="utf-8")

    total = sum(1 for item in target.rglob("*") if item.is_file())
    size = sum(item.stat().st_size for item in target.rglob("*") if item.is_file())
    zip_path = out / f"{APP_NAME}-{VERSION}-win64.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(target.rglob("*")):
            if item.is_file():
                archive.write(item, item.relative_to(out))

    print()
    print(f"目录：{target}")
    print(f"  文件 {total} 个 / {size / 1024 / 1024:.1f} MB；解释器来自 {python_home.name}")
    print(f"  runtime：{json.dumps(counts, ensure_ascii=False)}")
    print(f"压缩包：{zip_path}（{zip_path.stat().st_size / 1024 / 1024:.1f} MB）")
    print("下一步自检：release/isekai/runtime/python/python.exe -m isekai_core --root <临时目录>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
