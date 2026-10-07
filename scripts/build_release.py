"""组装发行件（USER_INTERFACE_DESIGN 实施进度 · U5 / ONBOARDING §3.2）。

做法：自带解释器（复用本机已有的 CPython 3.11，不联网下载）+ 壳 exe + 核心源码 + 随发行样例，
打成一个目录，再压成 zip。装完直接双击 `isekai.exe`：

    release/isekai/
      isekai.exe                ← 壳（Tauri，release 构建）
      runtime/python/           ← 自带解释器（核心与规则插件都用它）
      runtime/isekai_core/      ← 核心源码
      runtime/examples/         ← 随发行样例（世界包 + 角色卡 + 潮汐规则）
      runtime/python/Lib/site-packages/  ← 运行依赖（websockets / httpx / PyYAML …）
      README.md  LICENSE  发行说明.md

`README.md` 与 `发行说明.md` 都由本脚本**生成**（不是拷仓库根 README）：包里没有 `docs/`、
`tests/`、`scripts/`，拷过去只会留下一片死链。两者共用同一份正文片段（`RELEASE_*` 常量），
改一处两边同步。

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
    "websockets", "httpx", "httpcore", "h11", "anyio", "idna", "certifi",
    "yaml", "yaml-*.dist-info", "websockets-*.dist-info", "httpx-*.dist-info", "httpcore-*.dist-info",
    "h11-*.dist-info", "anyio-*.dist-info", "idna-*.dist-info", "certifi-*.dist-info",
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


#: 发行件里给用户看的正文片段——`README.md` 与 `发行说明.md` 共用同一份，不各写一遍
RELEASE_INTRO = """# isekai

一个持续运行的异世界。你通过与世界内角色的对话（经由角色专属的双向联络方式），
碎片化地发现这个世界的历史、人文与重要事件；世界不依赖你在线而存在。
"""

RELEASE_STEPS = """## 怎么用（五步）

1. 解压这个目录到任意位置（不要解压到需要管理员权限的地方）。
2. 双击 `isekai.exe`：程序自己带运行环境，不需要另外装 Python。
   - 界面依赖 WebView2 运行时——Win11 与多数已更新的 Win10 已预装。若双击**没有反应**，
     装一次微软的 WebView2 Runtime 再双击：<https://developer.microsoft.com/microsoft-edge/webview2/>
     （选「Evergreen Standalone Installer → x64」）。
   - 本程序未购买数字签名。从网上下载的压缩包可能被 Windows 标记，双击时弹「Windows 已保护你的电脑」——点「更多信息 → 仍要运行」即可。
3. 第一次打开直接进「首次设置」：本机检查 → 连接 AI → 选择第一件事 → 准备材料 → 开始使用。
   - **用 AI 需要自己有一个 AI 服务的账号**（要注册，多数要充值或领免费额度）。界面里推荐 DeepSeek：
     地址和模型名已经填好，你只要点「打开密钥申请页」，按页面上写的步骤创建密钥，把那一整串粘贴回来。
   - 不想现在弄：点「稍后配置，先整理素材」，照样能把世界和角色建起来，之后再回来补。
4. 准备材料：在「世界与素材」里从**样例世界**开始（推荐），或者自己新建世界设定、起草角色卡。
5. 开始用：想认识角色去「角色联络」，想整理故事走向去「辅助写作」，想跑一局去「跑团」。
   - **关掉窗口 ≠ 退出程序**：点右上角 × 只是收进任务栏托盘，世界还在后台走。
     要真正退出：右键任务栏里的 isekai 图标 → 「退出」。
   - 卸载 = 先在托盘里退出，再删除这个目录；你的数据在 `%LOCALAPPDATA%\\isekai`，要一起清掉再删那个目录。
"""

RELEASE_SLOW_REPLY = """## 第一次与角色说话可能慢一拍

角色若正处于睡眠时段，她的回复会先攒一拍再答（把这期间的多句话并成一批处理），
所以**第一句可能要等 30–120 秒才出现回复**——这是设计，不是卡死。
联络页会写着「最长约 2 分钟」并显示你已经等了多久；等不下去可以直接关掉窗口，
回复生成后仍然会保存下来，下次打开就能看到。
"""

RELEASE_PRIVACY = """## 数据与隐私

- 数据根：`%LOCALAPPDATA%\\isekai`（数据库、素材、日志都在这里，程序目录只放程序）。
- API 密钥只存在这台机器上：在界面「设置 → AI 服务」里填、在那里改；不进入日志、插件环境或导出件，备份文件里也不含密钥。
- 规则插件是本机扩展程序：登记时会显示来源、规则与版本、入口，进程隔离不是完整安全沙箱。
"""

RELEASE_SCOPE = """## 这一版里还没有的

- 安装程序（当前是 zip + 手动解压；卸载靠「托盘退出 + 删除目录」）。
- 面向普通用户的三条完整路径的真实试用记录（需要真人试跑，尚未做）。

出问题时：界面「帮助与诊断」里有常见问题（密钥去哪申请、填哪个模型名、回复慢、怎么退出、怎么备份），
以及「复制诊断信息」——把那段连同问题描述发给提供这个程序的人。
"""

#: 包里没有 docs/，所以 README 只留「包里真实存在」的指路
RELEASE_DOCS_SECTION = """## 文档

包里只有程序本身，没有仓库的 `docs/`。你需要的都在这两份里：

- [`发行说明.md`](发行说明.md)：怎么装、怎么开始、第一次为什么可能慢、数据放在哪、这一版还没有什么。
- 界面内「帮助与诊断」：本机检查、日志位置与高级调试。

想了解设计与协议、或用源码开发，去仓库（本包不含）：

- 设计文档地图：`docs/README.md`
- 十分钟跑通一个世界：`docs/QUICKSTART.md`
- 开发指南：`docs/DEVELOPING.md`

## 许可

MIT，见 [LICENSE](LICENSE)。
"""


def release_readme() -> str:
    """发行件里的 README：自包含，不引用包里不存在的 docs/ / scripts/ / tests/。

    以前这里直接拷仓库根 README（41 KB 开发者文档），在包里造成 22 条死链——可用性审计
    认定的发行件最大摩擦。现在改成由本文正文片段拼装，内容与 `发行说明.md` 同源。
    """
    return "\n\n---\n\n".join(
        part.rstrip() for part in (RELEASE_INTRO, RELEASE_STEPS, RELEASE_SLOW_REPLY, RELEASE_DOCS_SECTION)
    ) + "\n"


def release_note(version: str) -> str:
    """发行说明：怎么用 + 慢一拍的原因 + 数据与隐私 + 这一版还没有的。"""
    return "\n".join(
        part.rstrip() for part in (
            f"# 发行说明（{version}）", RELEASE_STEPS, RELEASE_SLOW_REPLY,
            RELEASE_PRIVACY, RELEASE_SCOPE,
        )
    ) + "\n"


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
    # LICENSE 是发行件的必需件：缺了就停下，不静默产出一个没有许可声明的包
    license_file = REPO / "LICENSE"
    if not license_file.is_file():
        raise SystemExit(f"发行件缺少必需文件：{license_file}（MIT 许可全文）。补齐后再组装。")
    shutil.copy2(license_file, target / "LICENSE")
    readme = REPO / "README.md"
    if readme.is_file():
        # 只带自包含的用户部分：包里没有 docs/，整份拷过去会是一片死链
        (target / "README.md").write_text(release_readme(), encoding="utf-8")
    (target / "发行说明.md").write_text(release_note(VERSION), encoding="utf-8")

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
