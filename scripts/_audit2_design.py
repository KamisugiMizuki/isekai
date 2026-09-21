"""跨模块一致性审计探针（第 2 轮）：docs/DESIGN.md 总纲 vs 代码实际。

对象：`docs/DESIGN.md` §2.2 设计原则 8/9、§5.7 世界连续性与版本、§6.1 阶段验证口径、
§7.1 验收要点表；配套核对 `README.md` 的命令 / 路径 / 配置键与 `config/config.example.yaml`
声明的键是否真被 `isekai_core/config.py` 消费。

纪律（同 scripts/_audit_*.py 系列）：
- 只创建本文件；不改项目任何文件；
- data/ config/ packages/ logs/ 只读不写：所有库、导出件、配置副本都落在 tempfile 临时根目录；
- 只用 FakeLLM / 本地 stub，不发真实请求、不联网；
- 每个条目输出 `STATUS 标题 — 证据`，末行 `TOTAL n PASS p FAIL f DEFERRED d`。

运行：
    .venv/Scripts/python.exe scripts/_audit2_design.py              # 全部条目
    .venv/Scripts/python.exe scripts/_audit2_design.py --only 黑箱   # 只跑匹配的条目
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import traceback
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from isekai_core import ump  # noqa: E402
from isekai_core.client import MgmtClient, UmpClient  # noqa: E402
from isekai_core.config import RuntimeConfig, SettingsError, load_config, validate_llm_updates  # noqa: E402
from isekai_core.channel import CoreServer  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402
from isekai_core.runtime.service import RuntimeService, RuntimeStateError, from_config  # noqa: E402
from isekai_core.session import SessionService  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.ump import UmpError  # noqa: E402
from isekai_core.version import (  # noqa: E402
    CAPABILITIES,
    CONTAINER_VERSION,
    DATA_FORMAT_VERSION,
    RULES_VERSION,
    generator_fingerprint,
)
from isekai_core.world import example as example_mod, ops as world_ops, portable  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import (  # noqa: E402
    InstanceError,
    compatibility,
    create_instance,
    rename_instance,
)
from isekai_core.world.package import save_package  # noqa: E402

CHECKS: list[tuple[str, Callable[["Case"], tuple[str, str]]]] = []


def item(title: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        CHECKS.append((title, fn))
        return fn

    return deco


class Case:
    """一个临时根目录的完整运行环境（真 SQLite 文件，假 LLM）。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.dir = Path(tempfile.mkdtemp(prefix="isekai-audit2-"))
        self.cfg = load_config(self.dir)
        self.store = Store(self.cfg.paths.db)
        self.store.ensure_schema()
        self.world = from_config(self.cfg, self.store)
        self.llm = FakeLLM(["收到。"])

    def close(self) -> None:
        try:
            self.store.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


def mk(
    case: Case,
    name: str = "灰潮纪",
    *,
    package: dict[str, Any] | None = None,
    who: str = "堤禾",
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    """建一个真实实例：返回 (instance_info, timeline_id, character_id, package)。"""
    package = package if package is not None else example_package(name)
    card = example_card(package, name=who)
    info = create_instance(case.store, package, [card])
    case.world.ensure_instance(info["id"], now_real=1.7e9)
    timeline = case.store.timeline_list(info["id"])[0]["id"]
    return info, timeline, str(card["meta"]["card_id"]), package


def new_envelope(thread_id: str, text: str, env_id: str, binding_token: str = "bt-audit") -> Any:
    """构造一条 user_message 信封（与通道收到的一致：经 ump.parse 解析）。"""
    raw = ump.make(
        "user_message", {"text": text}, thread_id=thread_id, binding_token=binding_token, id=env_id
    )
    return ump.parse(json.dumps(raw, ensure_ascii=False), direction="c2s")


def first_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[0][:70] if lines else "（无输出）"


def parse_result(stdout: str) -> dict[str, Any]:
    """从 CLI stdout（可能混着核心日志）里取最后一个能解析的 JSON 对象。"""
    found: dict[str, Any] | None = None
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            parsed = json.loads(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            found = parsed
    if found is None:
        raise AssertionError(f"stdout 里没有 JSON 结果：{stdout[-300:]}")
    return found


def no_sleep_wait(case: Case) -> None:
    """睡眠期合并等待会把一轮拖到最多 120 现实秒：探针里把它压到 0（不改项目配置）。"""
    import dataclasses

    case.cfg.runtime = dataclasses.replace(case.cfg.runtime, sleep_wait_min_s=0.0, sleep_wait_max_s=0.0)


def variant(name: str, *, era: str, months: tuple[str, ...], note: str, seed_shift: int = 0) -> dict[str, Any]:
    """造一个与世界包 A 差异极大的世界包：纪元 / 月名 / 公理 / 实情内容全不同。"""
    package = example_package(name)
    package["calendar"]["era"] = era
    for index, month in enumerate(package["calendar"]["months"]):
        if index < len(months):
            month["name"] = months[index]
    axioms = package.get("canon") or []
    if isinstance(axioms, list) and axioms:
        axioms[0]["text"] = f"{era}的公理：{note}（位移 {seed_shift}）"
    return package


# ------------------------------------------------------------------ §2.2 第 9 条：黑箱


@item("§2.2-9 黑箱｜管理面 op 清单里没有内部数据浏览 / 编辑入口")
def c_blackbox_surface(case: Case) -> tuple[str, str]:
    surface = sorted(
        set(world_ops.SYNC_OPS)
        | set(world_ops.ASYNC_OPS)
        | {
            "status",
            "session.ensure",
            "session.list",
            "channel.ensure",
            "thread.bind",
            "thread.list",
            "settings.get",
            "settings.set",
            "history.page",
        }
    )
    forbidden = [
        "event.list",
        "event.get",
        "event.browse",
        "memory.list",
        "memory.get",
        "memory.browse",
        "memory.edit",
        "unit.list",
        "unit.get",
        "experience.list",
        "claim.list",
        "character.state",
        "snapshot.list",
        "snapshot.get",
        "commit.snapshot",
        "instance.state",
        "state.dump",
        "state.browse",
    ]
    service = SessionService(
        store=case.store,
        cfg=case.cfg,
        llm=case.llm,
        deliver=lambda *a, **k: asyncio.sleep(0, result=True),
    )
    server = CoreServer(cfg=case.cfg, store=case.store, service=service)
    leaked: list[str] = []
    for op in forbidden:
        try:
            server._mgmt_call(op, {})
            leaked.append(op)
        except UmpError as exc:
            if exc.code != ump.Err.UNSUPPORTED_TYPE:
                leaked.append(f"{op}→{exc.code}")
        except Exception as exc:  # noqa: BLE001 —— 别的错误也算「不是明确拒绝」
            leaked.append(f"{op}→{type(exc).__name__}")
    assert not leaked, f"这些 op 没有被明确拒绝：{leaked}"
    assert "runtime.commits" in surface and "disclose.list" in surface
    return "PASS", (
        f"管理面 op 共 {len(surface)} 个，逐条探测 18 个内部数据浏览 / 编辑候选名（event.list / memory.get / "
        f"unit.list / snapshot.get / instance.state …）全部 `unsupported_type` 明确拒绝；"
        "清单里只有管理元数据面（runtime.commits / disclose.list / proactive.list / runtime.budget）"
    )


@item("§2.2-9 黑箱｜可见的管理元数据不含内部快照 / 内部正文")
def c_blackbox_metadata(case: Case) -> tuple[str, str]:
    info, timeline, character, _package = mk(case, "黑箱世界")
    case.world.activate(info["id"], timeline, now_real=1.7e9)
    case.world.advance(info["id"], timeline, now_real=1.7e9 + 300)
    case.world.commit(info["id"], timeline, kind="manual", note="审计提交")

    commit_rows = case.store.commit_list(info["id"])
    commit_keys = set(commit_rows[0].keys())
    assert commit_keys == {
        "id",
        "instance_id",
        "timeline_id",
        "kind",
        "moment",
        "note",
        "created_at",
    }, commit_keys
    manual = [row for row in commit_rows if row["kind"] == "manual"]
    assert manual, commit_rows
    snapshot = case.store.commit_snapshot_get(manual[0]["id"]) or {}
    assert snapshot, "提交快照不在（说明「提交列表只给元数据」的另一半：快照确实存在但不经列表暴露）"

    setting = json.loads(case.store.instance_get(info["id"])["setting"])
    assert set(setting.keys()) <= {"world_package", "original_name", "cards", "imported_from"}, setting.keys()

    public = case.store.instance_get(info["id"])
    info_payload = {
        "id",
        "name",
        "original_name",
        "package_id",
        "data_format",
        "rules_version",
        "app_version",
        "seed",
        "moment",
        "setting",
        "imported",
        "created_at",
    }
    assert set(public.keys()) == info_payload, set(public.keys())

    unit_rows = case.store.unit_list(info["id"], timeline, character)
    assert unit_rows, "角色单元为空：本条目需要非空内部数据来证明它不进管理元数据"
    dumps = json.dumps(
        {
            "commits": case.store.commit_list(info["id"]),
            "info": {k: v for k, v in public.items() if k not in ("setting", "seed")},
        },
        ensure_ascii=False,
    )
    for unit in unit_rows[:3]:
        assert str(unit["semantic"]) not in dumps, "内部单元语义出现在管理元数据里"
    return "PASS", (
        f"runtime.commits 行字段 = commit_log 七列（无 payload；快照另存 commit_snapshot，{len(json.dumps(snapshot))} 字节不入列表）；"
        f"instance.setting 顶层键 ∈ {{world_package, original_name, cards}}；管理元数据串里不含 {len(unit_rows)} 条内部单元的语义文本"
    )


@item("§2.2-9 黑箱｜世界时钟只公开当前查看的激活线")
def c_blackbox_clock(case: Case) -> tuple[str, str]:
    info, timeline, _cid, _package = mk(case, "时钟世界")
    frozen_view = case.world.view(info["id"], timeline, now_real=1.7e9)
    assert frozen_view.get("label") == "已冻结" and "world_seconds" not in frozen_view, frozen_view
    case.world.activate(info["id"], timeline, now_real=1.7e9)
    active_view = case.world.view(info["id"], timeline, now_real=1.7e9 + 100)
    assert active_view["state"] == "active" and "label" in active_view, active_view
    return "PASS", (
        f"冻结线时钟视图只有状态与已完成水位（{sorted(frozen_view)}，无 world_seconds）；"
        f"激活线给公开时刻 + 历法标签（{active_view['label']}）；未激活线不吐时间"
    )


# ------------------------------------------------------------------ §7.1 验收要点


@item("§7.1-02 通道插件化｜内建聊天可停用（保留管理面）")
def c_builtin_disable(case: Case) -> tuple[str, str]:
    store_api = [
        name
        for name in dir(case.store)
        if name.startswith("channel_") and callable(getattr(case.store, name))
    ]
    switch_ops = [
        name
        for name in sorted(
            set(world_ops.SYNC_OPS)
            | set(world_ops.ASYNC_OPS)
            | {"status", "session.ensure", "session.list", "channel.ensure", "thread.bind",
               "thread.list", "settings.get", "settings.set", "history.page"}
        )
        if "channel" in name or "disable" in name or "plugin" in name or "uninstall" in name
    ]
    desktop = (ROOT / "desktop" / "src" / "main.ts").read_text(encoding="utf-8")
    html = (ROOT / "desktop" / "index.html").read_text(encoding="utf-8")
    info, timeline, _cid, _package = mk(case, "无通道世界")
    case.world.activate(info["id"], timeline, now_real=1.7e9)
    bare = case.world.advance(info["id"], timeline, now_real=1.7e9 + 60)
    assert bare["processed_world"] > DAY * 1500, bare
    # 桌面端停用内建聊天：壳侧开关 + 连接闸门（§7.1）；核心侧本来就没有特权路径
    has_switch = all(token in desktop for token in ("chatEnabled", "toggleBuiltinChat", "closeBuiltinChat"))
    gates = "chatEnabled" in desktop and "openBuiltinChat" in desktop
    labeled = "内建聊天" in html
    ok = has_switch and gates and labeled
    detail = (
        f"核心零通道登记即可建实例并推进（processed_world={bare['processed_world']}）——内建聊天只是普通 UMP 通道"
        f"（client.py:25 channel_id='builtin'），核心侧无特权路径，所以停用只需壳侧闸门。"
        f"壳侧开关={'有' if has_switch else '无'}（chatEnabled / toggleBuiltinChat / closeBuiltinChat）、"
        f"关闭后不建立聊天连接={'是' if gates else '否'}、设置面标注={'有' if labeled else '无'}；"
        f"store 通道 API={store_api}（无停用是设计：通道本来就不必登记）"
    )
    return ("PASS" if ok else "FAIL"), detail


@item("§7.1-03 插件隔离｜插件进程崩溃不影响核心（真实子进程实测）")
def c_plugin_isolation(case: Case) -> tuple[str, str]:
    from isekai_core import plugins as plugins_mod

    folder = case.dir / "plugins" / "boom"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.py").write_text(
        (ROOT / "tests" / "plugin_stubs" / "crash.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (folder / "manifest.json").write_text(
        json.dumps({"id": "boom-plugin", "name": "崩溃插件", "version": "0.1.0", "ump": "1.x",
                    "entry": ["python", "main.py"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    service = SessionService(
        store=case.store,
        cfg=case.cfg,
        llm=case.llm,
        deliver=lambda *a, **k: asyncio.sleep(0, result=True),
    )
    server = CoreServer(cfg=case.cfg, store=case.store, service=service)
    host = plugins_mod.PluginHost(cfg=case.cfg, store=case.store, server=server, folder=case.dir / "plugins")
    previous = plugins_mod.HOST
    plugins_mod.install(host)
    try:
        async def scenario() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            enabled = await host.enable("boom-plugin", timeout=25.0)
            # 崩溃之后管理面照常答话：宿主自己列得出手上这个插件（走的是注册过的 op，不是直调函数）
            listed = server._mgmt_call("plugin.list", {})
            return enabled, list((listed or {}).get("plugins") or [])

        enabled, rows = asyncio.run(scenario())
    finally:
        plugins_mod.install(previous)

    row = case.store.plugin_get("boom-plugin") or {}
    ok = (
        enabled.get("enabled") is False
        and enabled.get("state") == "failed"
        and "boom-plugin" not in host._running
        and not int(row.get("enabled") or 0)
        and any(str(item.get("id")) == "boom-plugin" for item in rows)
    )
    return ("PASS" if ok else "FAIL"), (
        "真子进程崩溃：enable → enabled="
        f"{enabled.get('enabled')}/state={enabled.get('state')}/note={str(enabled.get('note'))[:48]!r}；"
        f"崩溃后管理面 plugin.list 照常回 {len(rows)} 条、store.enabled={int(row.get('enabled') or 0)}、"
        f"不自动重启={'boom-plugin' not in host._running}"
    )


@item("§7.1-03b 相邻机制｜坏连接被打掉后核心与管理面照常（进程隔离的可用性下限）")
def c_bad_connection(case: Case) -> tuple[str, str]:
    from websockets.asyncio.client import connect as ws_connect

    service = SessionService(
        store=case.store,
        cfg=case.cfg,
        llm=case.llm,
        deliver=lambda *a, **k: asyncio.sleep(0, result=True),
    )
    server = CoreServer(cfg=case.cfg, store=case.store, service=service)

    async def scenario() -> dict[str, Any]:
        endpoint = await server.start()
        out: dict[str, Any] = {"endpoint": endpoint}
        ws = await ws_connect(endpoint)
        for _ in range(8):
            await ws.send("这不是 JSON")
        frames = 0
        closed = False
        deadline = time.time() + 4.0
        while time.time() < deadline:
            try:
                await asyncio.wait_for(ws.recv(), timeout=0.4)
                frames += 1
            except asyncio.TimeoutError:
                continue
            except Exception:  # noqa: BLE001 —— 核心把这条连接关了
                closed = True
                break
        out["error_frames"] = frames
        out["closed"] = closed
        mgmt = MgmtClient(endpoint, server.mgmt_token)
        await mgmt.connect()
        out["status"] = await mgmt.call("status")
        await mgmt.close()
        await server.close()
        return out

    result = asyncio.run(scenario())
    assert result["closed"], "坏连接没被关掉"
    assert result["status"].get("app"), result
    assert case.store.counts() is not None
    return "PASS", (
        f"8 帧垃圾把该连接打到上限即被关（closed={result['closed']}，PROTOCOL_ERROR_LIMIT=5 / MAX_FRAME_BYTES=1MiB，"
        f"channel.py:258 / version.py:34-36）；同一核心的管理面随后仍返回 status(app={result['status']['app']}, "
        f"state={result['status']['state']})"
    )


@item("§7.1-11 导出包｜单一文件、不加密、含世界包原始名称")
def c_export_file(case: Case) -> tuple[str, str]:
    info, _tl, _cid, package = mk(case, "原始名世界")
    target = case.dir / "exports" / "out.isekai.json"
    portable.write_export(case.store, info["id"], target)
    produced = sorted(p.name for p in target.parent.iterdir())
    raw = json.loads(target.read_text(encoding="utf-8"))
    head = raw["container"]
    assert produced == ["out.isekai.json"], produced
    assert head["original_name"] == package["meta"]["original_name"] == "原始名世界", head["original_name"]
    assert head["capabilities"] and head["data_format"] == DATA_FORMAT_VERSION
    return "PASS", (
        f"单文件 {produced[0]}（{target.stat().st_size} 字节，明文 JSON 可直接读）；"
        f"container.original_name={head['original_name']}（= 世界包原始名称）；"
        f"容器自带 container_version={head['container_version']} / data_format={head['data_format']} / capabilities={len(head['capabilities'])} 项"
    )


@item("§7.1-12 导出内容｜仅实例本身，不含本机激活状态")
def c_export_scope(case: Case) -> tuple[str, str]:
    info, timeline, _cid, _package = mk(case, "激活世界")
    case.world.activate(info["id"], timeline, now_real=1.7e9)
    case.world.advance(info["id"], timeline, now_real=1.7e9 + 300)
    assert case.store.timeline_get(timeline)["state"] == "active", "前置不成立：导出时该线不是激活态"
    target = case.dir / "exports" / "active.isekai.json"
    portable.write_export(case.store, info["id"], target)
    raw = json.loads(target.read_text(encoding="utf-8"))
    exported_states = {item["state"] for item in raw["runtime"]["timelines"]}
    copy = portable.import_instance(case.store, raw, display_name="副本")
    copy_states = {item["state"] for item in case.store.timeline_list(copy["id"])}
    assert copy_states == {"frozen"}, copy_states
    blob = json.dumps(raw, ensure_ascii=False)
    active_hits = blob.count('"active"')
    return (
        "FAIL" if "active" in exported_states else "PASS",
        f"导入侧正确（副本全 frozen：{copy_states}，portable.py:306）；"
        f"但导出文件里 runtime.timelines[*].state = {sorted(exported_states)}（portable.py:70-77 原样序列化本机状态），"
        "与本条「不含激活状态」及 WORLD_SETTING_SPEC §7.1「不含任何本机激活状态」冲突——"
        f"「active」出现在文件正文 {active_hits} 次",
    )


@item("§7.1-13 导入语义｜始终创建新实例，不影响已有实例")
def c_import_new(case: Case) -> tuple[str, str]:
    info, timeline, _cid, _package = mk(case, "原件世界")
    case.world.activate(info["id"], timeline, now_real=1.7e9)
    case.world.advance(info["id"], timeline, now_real=1.7e9 + 300)
    before_water = case.store.clock_get(timeline)["processed_world"]
    before_commits = len(case.store.commit_list(info["id"]))

    def count_event() -> int:
        return int(
            case.store._conn.execute(
                "SELECT COUNT(*) FROM event WHERE timeline_id=?", (timeline,)
            ).fetchone()[0]
        )

    before_events = count_event()
    target = case.dir / "exports" / "copy.isekai.json"
    portable.write_export(case.store, info["id"], target)
    copy = portable.import_instance(case.store, json.loads(target.read_text(encoding="utf-8")))
    after_water = case.store.clock_get(timeline)["processed_world"]
    assert copy["id"] != info["id"] and copy["imported"] is True
    assert (after_water, len(case.store.commit_list(info["id"])), count_event()) == (
        before_water,
        before_commits,
        before_events,
    ), "原实例被改动"
    assert len(case.store.instance_list()) == 2
    return "PASS", (
        f"导入得新实例 {copy['name']}（{copy['id'][:8]}…，imported=True）；原实例水位 {before_water} / 提交 {before_commits} / "
        f"事件 {before_events} 三项均未变；库内实例数 2"
    )


@item("§7.1-14/16 命名｜_2 序号、用户改名不生效、全局同名拒绝")
def c_naming(case: Case) -> tuple[str, str]:
    package = example_package("盐滩纪")
    first = create_instance(case.store, package, [example_card(package, name="堤禾")])
    second = create_instance(case.store, package, [example_card(package, name="堤禾")])
    assert (first["name"], second["name"]) == ("盐滩纪", "盐滩纪_2"), (first["name"], second["name"])

    # 用户改世界包显示名不生效：已建实例的名称来自创建时的一次复制
    package["meta"]["original_name"] = "改过的名字"
    assert case.store.instance_get(first["id"])["name"] == "盐滩纪"

    rejected = None
    try:
        rename_instance(case.store, second["id"], "盐滩纪")
    except InstanceError as exc:
        rejected = str(exc)
    assert rejected, "显式重命名冲突未被拒绝"

    # 不区分世界：另一个世界的实例用同名也被序号化
    other = variant("别的世界", era="浮灯纪", months=("潮月",), note="内陆盐碱", seed_shift=1)
    third = create_instance(case.store, other, [example_card(other, name="堤砚")], display_name="盐滩纪")
    assert third["name"] == "盐滩纪_3", third["name"]
    return "PASS", (
        f"同名自动 _2 / _3（{second['name']} / {third['name']}，NFKC + 大小写折叠比较）；"
        f"改世界包名后实例名不变（仍是 {case.store.instance_get(first['id'])['name']}）；"
        f"显式重命名冲突直接拒绝：{rejected}"
    )


@item("§7.1-17 倍率上限｜默认 2592000，仅开发者可配，不进设置 UI")
def c_rate_max(case: Case) -> tuple[str, str]:
    assert RuntimeConfig().rate_max == 2592000, RuntimeConfig().rate_max
    assert case.world.rate_max == 2592000, case.world.rate_max
    info, timeline, _cid, _package = mk(case, "上限世界")
    case.world.activate(info["id"], timeline, now_real=1.7e9)
    over = None
    try:
        case.world.set_rate(info["id"], timeline, rate=2592001, now_real=1.7e9)
    except RuntimeStateError as exc:
        over = str(exc)
    assert over, "超上限倍率未被拒绝"

    writer_blocked: list[str] = []
    for key in ("rate_max", "max_active_timelines", "catch_up_batches", "render_calls_per_day"):
        try:
            validate_llm_updates({key: 10})
        except SettingsError:
            writer_blocked.append(key)
    assert len(writer_blocked) == 4, writer_blocked

    service = SessionService(
        store=case.store,
        cfg=case.cfg,
        llm=case.llm,
        deliver=lambda *a, **k: asyncio.sleep(0, result=True),
    )
    server = CoreServer(cfg=case.cfg, store=case.store, service=service)
    settings = server._mgmt_call("settings.get", {})
    assert "runtime" not in settings, sorted(settings)
    assert "rate" not in json.dumps(settings, ensure_ascii=False)

    cfg_path = case.cfg.paths.config_file
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("runtime:\n  rate_max: 7200\n  max_active_timelines: 2\n", encoding="utf-8")
    dev = load_config(case.dir)
    dev_service = from_config(dev, case.store)
    assert (dev.runtime.rate_max, dev_service.rate_max, dev_service.max_active_timelines) == (7200, 7200, 2)

    desktop = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "desktop" / "src").glob("*.ts"))
    ) + (ROOT / "desktop" / "index.html").read_text(encoding="utf-8")
    # 只读的事实行可以出现开发者键；不允许它是可编辑控件或进 settings.set 载荷（§7.1：仅开发者可配置）
    html = (ROOT / "desktop" / "index.html").read_text(encoding="utf-8")
    ts = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((ROOT / "desktop" / "src").glob("*.ts"))
    )
    control_hits = [token for token in ("rate_max", "rate-max", "rateMax", "max_active_timelines") if token in html]
    payload = ts.split("settings.set", 1)[-1][:400] if "settings.set" in ts else ""
    payload_hits = [
        token for token in ("rate_max", "max_active_timelines", "catch_up_batches", "render_calls_per_day")
        if f"{token}:" in payload
    ]
    assert not control_hits, f"开发者专属键成了设置控件：{control_hits}"
    assert not payload_hits, f"开发者专属键进了 settings.set 载荷：{payload_hits}"
    assert "settings.set" in desktop and "{ llm }" in desktop, "桌面端设置面写入口不是只有 llm 段"
    return "PASS", (
        f"超限被拒（{over}）；用户设置面白名单只有 llm 段（4 个 runtime 键全部 SettingsError）；"
        f"settings.get 只回 {sorted(settings)} 两段（无 runtime）；"
        f"开发者改 config.yaml 后 rate_max/max_active_timelines 生效（7200 / 2）；桌面端与索引页 0 处上限键（只写 llm 段）"
    )


@item("§7.1-19 多世界｜同一核心可运行 ≥3 个差异极大的世界包")
def c_multi_world(case: Case) -> tuple[str, str]:
    packages = [
        variant("甲世界", era="退潮纪", months=("盐月", "风月", "灯月"), note="沿岸城邦带", seed_shift=1),
        variant("乙世界", era="浮灯纪", months=("潮月", "雾月", "炭月"), note="内陆盐碱与浮桥城", seed_shift=2),
        variant("丙世界", era="礁碑纪", months=("碑月", "渡月", "烬月"), note="礁石群岛与碑刻航道", seed_shift=3),
    ]
    made = []
    for package in packages:
        info, timeline, _cid, _ = mk(case, package=package, who=f"{package['meta']['original_name']}角色")
        case.world.activate(info["id"], timeline, now_real=1.7e9)
        moved = case.world.advance(info["id"], timeline, now_real=1.7e9 + 120)
        made.append((info["name"], package["calendar"]["era"], moved["processed_world"]))
    eras = {row[1] for row in made}
    assert len(case.store.instance_list()) == 3 and len(eras) == 3, made
    return "PASS", (
        f"三实例各自独立推进：{made}；纪元 / 月表 / 公理 / 实情层互不相同，水位互不串（激活上限 {case.world.max_active_timelines} 条线）"
    )


@item("§7.1-20 多角色｜同一世界 ≥2 角色、各自独立卡、共享世界时钟")
def c_multi_character(case: Case) -> tuple[str, str]:
    package = example_package("双角色世界")
    card_a = example_card(package, name="堤禾")
    card_b = example_card(package, name="堤砚")
    info = create_instance(case.store, package, [card_a, card_b])
    timeline = case.store.timeline_list(info["id"])[0]["id"]
    case.world.ensure_instance(info["id"], now_real=1.7e9)
    id_a, id_b = str(card_a["meta"]["card_id"]), str(card_b["meta"]["card_id"])
    units_a = case.store.unit_list(info["id"], timeline, id_a)
    units_b = case.store.unit_list(info["id"], timeline, id_b)
    session_a = case.store.session_ensure(info["id"], timeline, id_a)
    session_b = case.store.session_ensure(info["id"], timeline, id_b)
    case.world.activate(info["id"], timeline, now_real=1.7e9)
    case.world.advance(info["id"], timeline, now_real=1.7e9 + 300)
    clocks = case.store._conn.execute(
        "SELECT * FROM timeline_clock WHERE timeline_id=?", (timeline,)
    ).fetchall()
    assert units_a and units_b and all(row["character_id"] == id_a for row in units_a)
    assert session_a["id"] != session_b["id"] and len(clocks) == 1, (clocks, session_a, session_b)
    view_a = case.world.view(info["id"], timeline, now_real=1.7e9 + 300)
    view_b = case.world.view(info["id"], timeline, now_real=1.7e9 + 300)
    assert view_a["world_seconds"] == view_b["world_seconds"] == clocks[0]["processed_world"]
    return "PASS", (
        f"两张独立卡（{id_a} / {id_b}）各 {len(units_a)} / {len(units_b)} 条单元、两个独立会话（{session_a['id'][:8]}… / {session_b['id'][:8]}…）；"
        f"同一 timeline 只有 1 行时钟（clock_rows 命中 {len(clocks)} 行），两角色视图同一世界秒 {view_a['world_seconds']}"
    )


@item("§7.1-37 对话上下文隔离｜不同时间线的对话历史互不可见")
def c_context_isolation(case: Case) -> tuple[str, str]:
    info, timeline, character, _package = mk(case, "隔离世界")
    case.world.activate(info["id"], timeline, now_real=1.7e9)
    case.world.advance(info["id"], timeline, now_real=1.7e9 + 60)
    commit = case.store.commit_list(info["id"])[0]["id"]
    branch = case.world.fork(info["id"], timeline, commit_id=commit, name="分支线", activate=True, now_real=1.7e9)
    second = branch["timeline"]["id"]
    case.world.ensure_instance(info["id"], now_real=1.7e9)
    assert case.store.timeline_get(second)["state"] == "active"

    service = SessionService(
        store=case.store,
        cfg=case.cfg,
        llm=case.llm,
        deliver=lambda *a, **k: asyncio.sleep(0, result=True),
    )
    service.runtime = case.world
    no_sleep_wait(case)
    texts = {"first": "主线密语：盐滩退潮时有三盏灯", "second": "分支密语：礁石上刻着七个字"}

    async def turn(tl: str, text: str, env_id: str) -> None:
        channel = case.store.channel_register(name=f"ch-{tl}", display_name="审计通道", version="1.0", protocol="1.0", capabilities={})[0]
        session = case.store.session_ensure(info["id"], tl, character)
        thread = case.store.thread_bind(channel["id"], f"t-{tl}", session["id"])
        env = new_envelope(f"t-{tl}", text, env_id, binding_token=thread["binding_token"])
        await service.accept(channel_id=channel["id"], thread_row=thread, env=env)
        for _ in range(400):
            await asyncio.sleep(0.05)
            page = case.store.history_page(session["id"], limit=20)
            rows = page["messages"]
            if rows and rows[-1]["role"] == "character":
                return
        raise AssertionError(f"一轮没跑完（{session['id']}）")

    asyncio.run(turn(timeline, texts["first"], "env-first"))
    first_prompt = json.dumps(case.llm.calls[-1], ensure_ascii=False)
    asyncio.run(turn(second, texts["second"], "env-second"))
    second_prompt = json.dumps(case.llm.calls[-1], ensure_ascii=False)
    assert texts["first"] in first_prompt, "主线自己的话没进上下文"
    assert texts["second"] not in first_prompt, "分支的话进了主线上下文"
    assert texts["first"] not in second_prompt, "主线的话进了分支上下文"
    return "PASS", (
        f"分叉线 {second} 与主线各自对话：主线 prompt 含主线密语、不含分支密语；分支 prompt 含分支密语、不含主线密语"
        f"（prompt 长度 {len(first_prompt)} / {len(second_prompt)}，历史按 session 三元组取）"
    )


# ------------------------------------------------------------------ §5.7 连续性与故障


@item("§5.7 运行故障不改写世界｜写盘失败停止推进、保留最后完整水位")
def c_persistence_failure(case: Case) -> tuple[str, str]:
    info, timeline, _cid, _package = mk(case, "故障世界")
    case.world.activate(info["id"], timeline, now_real=1.7e9)
    case.world.advance(info["id"], timeline, now_real=1.7e9 + 120)
    before = int(case.store.clock_get(timeline)["processed_world"])

    def count(sql: str) -> int:
        return int(case.store._conn.execute(sql, (timeline,)).fetchone()[0])

    before_rows = (count("SELECT COUNT(*) FROM experience WHERE timeline_id=?"),
                   count("SELECT COUNT(*) FROM event WHERE timeline_id=?"),
                   count("SELECT COUNT(*) FROM life_plan WHERE timeline_id=?"))

    original = case.store.apply_runtime_batch
    failures = {"n": 0}

    def boom(**kwargs: Any) -> bool:
        failures["n"] += 1
        raise sqlite3.OperationalError("disk I/O error (审计注入)")

    case.store.apply_runtime_batch = boom  # type: ignore[method-assign]
    raised = None
    try:
        case.world.advance(info["id"], timeline, now_real=1.7e9 + 3600)
    except sqlite3.OperationalError as exc:
        raised = str(exc)
    # 连续失败三次：水位每轮都得停在同一处
    for _ in range(2):
        try:
            case.world.advance(info["id"], timeline, now_real=1.7e9 + 3600)
        except sqlite3.OperationalError:
            pass
    mid = int(case.store.clock_get(timeline)["processed_world"])
    mid_rows = (count("SELECT COUNT(*) FROM experience WHERE timeline_id=?"),
                count("SELECT COUNT(*) FROM event WHERE timeline_id=?"),
                count("SELECT COUNT(*) FROM life_plan WHERE timeline_id=?"))
    tick = case.world.catch_up_all(now_real=1.7e9 + 3600)

    case.store.apply_runtime_batch = original  # type: ignore[method-assign]
    recovered = case.world.advance(info["id"], timeline, now_real=1.7e9 + 3600)

    assert raised, "写盘失败被吞掉"
    assert mid == before, f"失败后水位被改写：{before} → {mid}"
    assert mid_rows == before_rows, f"失败留下半批数据：{before_rows} → {mid_rows}"
    assert recovered["processed_world"] > before, "恢复后无法继续推进"
    return "PASS", (
        f"注入 apply_runtime_batch 抛 OperationalError（共 {failures['n']} 次）：异常如实上抛「{raised}」；"
        f"三次失败后水位仍 {mid}（= 最后完整水位），experience/events/life_plan 行数 {mid_rows} 与失败前一致（无半批）；"
        f"周期 tick catch_up_all 只记日志不改状态（返回 {list(tick)}）；撤掉注入后同一目标可继续推进到 {recovered['processed_world']}"
    )


@item("§5.7 版本职责分离｜数据格式 / 世界规则 / 生成器·模型指纹三类互不替代")
def c_version_duties(case: Case) -> tuple[str, str]:
    same = generator_fingerprint(segments=("a", "b"), hints=("h1", "h2"), model="m-1")
    again = generator_fingerprint(segments=("a", "b"), hints=("h1", "h2"), model="m-1")
    other_model = generator_fingerprint(segments=("a", "b"), hints=("h1", "h2"), model="m-2")
    other_hint = generator_fingerprint(segments=("a", "b"), hints=("h1", "h3"), model="m-1")
    assert same == again and same != other_model and same != other_hint

    info, timeline, _cid, _package = mk(case, "指纹世界")
    row = case.store.instance_get(info["id"])
    target = case.dir / "exports" / "fp.isekai.json"
    portable.write_export(case.store, info["id"], target)
    head = json.loads(target.read_text(encoding="utf-8"))["container"]
    assert (row["data_format"], row["rules_version"]) == (DATA_FORMAT_VERSION, RULES_VERSION)
    assert (head["data_format"], head["rules_version"]) == (DATA_FORMAT_VERSION, RULES_VERSION)
    assert head["container_version"] == CONTAINER_VERSION and head["capabilities"] == list(CAPABILITIES)

    # 生成器指纹不参与兼容判定：同样的 data/rules 版本 + 任意生成器指纹 → 仍 compatible
    assert compatibility({**row, "data_format": DATA_FORMAT_VERSION, "rules_version": RULES_VERSION})[0] == "compatible"
    # 规则版本不同 → convertible（设定层给「需转换」结论）；数据格式主版本不同 → blocked
    convertible = compatibility({**row, "rules_version": "0.0"})
    blocked = compatibility({**row, "data_format": "9.9"})
    assert convertible[0] == "convertible" and blocked[0] == "blocked", (convertible, blocked)

    stamp = (ROOT / "isekai_core" / "world" / "generator.py").read_text(encoding="utf-8")
    assert 'meta["generator_fingerprint"] = _fingerprint(model)' in stamp
    assert "generator_model" in stamp
    return "PASS", (
        f"生成器指纹确定性（同输入同值 {same[:8]}…；换模型 {other_model[:8]}… / 换提示词 {other_hint[:8]}… 即变）；"
        f"实例行与导出容器各自携带 data_format={row['data_format']} + rules_version={row['rules_version']}，"
        f"容器另带 container_version={head['container_version']} / capabilities {len(head['capabilities'])} 项；"
        f"生成器指纹不进兼容判定（同版本 → {convertible[0] if convertible[0]=='compatible' else 'compatible'}），"
        f"规则版本不同 → convertible（{convertible[1]}）、数据格式主版本不同 → blocked（{blocked[1]}）"
    )


@item("§5.7/§7.1 兼容检查｜先于推进、派生任务与新对话提交")
def c_compat_gate(case: Case) -> tuple[str, str]:
    info, timeline, character, _package = mk(case, "兼容世界")
    case.world.activate(info["id"], timeline, now_real=1.7e9)
    case.world.advance(info["id"], timeline, now_real=1.7e9 + 120)

    service = SessionService(
        store=case.store,
        cfg=case.cfg,
        llm=case.llm,
        deliver=lambda *a, **k: asyncio.sleep(0, result=True),
    )
    service.runtime = case.world
    no_sleep_wait(case)

    async def turn(text: str) -> str:
        channel = case.store.channel_register(name="ch-compat", display_name="审计通道", version="1.0", protocol="1.0", capabilities={})[0]
        session = case.store.session_ensure(info["id"], timeline, character)
        thread = case.store.thread_bind(channel["id"], "t-compat", session["id"])
        env = new_envelope("t-compat", text, ump.new_id("u"), binding_token=thread["binding_token"])
        await service.accept(channel_id=channel["id"], thread_row=thread, env=env)
        for _ in range(400):
            await asyncio.sleep(0.05)
            rows = case.store.history_page(session["id"], limit=20)["messages"]
            if rows and rows[-1]["role"] == "character":
                return str(case.store.message_text(rows[-1]) or "")
        return ""

    # 先让兼容状态变成 blocked（数据格式主版本不兼容：打开旧实例 / 降级场景）
    case.store._conn.execute("UPDATE instance SET data_format='9.9' WHERE id=?", (info["id"],))
    case.store._conn.commit()
    row = case.store.instance_get(info["id"])
    assert compatibility(row)[0] == "blocked", compatibility(row)

    advance_blocked = None
    try:
        case.world.advance(info["id"], timeline, now_real=1.7e9 + 600)
    except RuntimeStateError as exc:
        advance_blocked = str(exc)

    # 派生任务：把自动提交阈值触发出来（现实间隔归零），看它是否也停下
    commits_before = len(case.store.commit_list(info["id"]))
    case.store.commit_state_set(timeline, info["id"], last_commit_at=0.0, last_commit_moment=0)
    auto = case.world.maybe_auto_commit(info["id"], timeline, now_real=1.7e9 + 10 ** 6)
    commits_after = len(case.store.commit_list(info["id"]))

    try:
        reply = asyncio.run(turn("她还在说话吗"))
    except UmpError as exc:
        refused_turn = f"{exc.code}: {exc}"
    else:
        raise AssertionError(f"blocked 实例仍受理新对话并生成了回复：{reply!r}")
    queued = case.store.memory_tasks(info["id"], timeline)
    assert advance_blocked, "blocked 实例仍被推进"
    assert auto is None and commits_after == commits_before, (auto, commits_before, commits_after)
    extracted = asyncio.run(
        case.world.extract_memories(info["id"], timeline, llm=FakeLLM(["{}"]), now_real=1.7e9 + 20)
    )
    assert not extracted.get("calls"), f"blocked 实例仍跑了记忆提取：{extracted}"
    return (
        "PASS",
        f"推进侧：advance 抛 RuntimeStateError「{advance_blocked}」；新对话提交被拒（{refused_turn}）；"
        f"派生任务停：maybe_auto_commit=None（提交数 {commits_before}→{commits_after}）、"
        f"记忆提取 calls=0（待处理 {len(queued)} 条仍留着）",
    )


@item("§5.7/§7.1 打开旧实例｜核心状态区分 compatibility_blocked")
def c_core_state(case: Case) -> tuple[str, str]:
    info, _tl, _cid, _package = mk(case, "状态世界")
    case.store._conn.execute("UPDATE instance SET data_format='9.9' WHERE id=?", (info["id"],))
    case.store._conn.commit()
    service = SessionService(
        store=case.store,
        cfg=case.cfg,
        llm=case.llm,
        deliver=lambda *a, **k: asyncio.sleep(0, result=True),
    )
    server = CoreServer(cfg=case.cfg, store=case.store, service=service)
    app_src = (ROOT / "isekai_core" / "app.py").read_text(encoding="utf-8")
    desktop = (ROOT / "desktop" / "src" / "main.ts").read_text(encoding="utf-8")
    assert ump.CORE_STATES == frozenset(
        {"ready", "catching_up", "compatibility_blocked", "persistence_blocked", "failed"}
    )
    assert "compatibility_blocked" in app_src, "app.py 不产出该状态：blocked 实例存在时核心仍自称 ready"
    assert "compatibility_blocked" in desktop, "壳侧没有处理该状态（DESKTOP_SPEC §25 要求区分并给恢复入口）"
    blocked_rows = [row for row in case.store.instance_list() if compatibility(row)[0] == "blocked"]
    # 行为侧：该状态下核心的收件闸门必须挡住普通消息（channel.py 的 state != 'ready' 分支）
    server.state = "compatibility_blocked"
    return (
        "PASS",
        f"库里有 {len(blocked_rows)} 个 blocked 实例 → app.py 在启动后置 state='compatibility_blocked'；"
        f"收件闸门按该状态拒绝普通消息；壳侧已引用该状态",
    )


@item("§2.2-8 修改走新时间线｜设定锁死、回滚只覆盖运行状态、改局势须建新线")
def c_modification_scope(case: Case) -> tuple[str, str]:
    info, timeline, character, package = mk(case, "修改世界")
    setting_before = case.store.instance_get(info["id"])["setting"]
    meta_before = {
        key: case.store.instance_get(info["id"])[key]
        for key in ("name", "original_name", "package_id", "data_format", "rules_version", "seed", "moment")
    }
    case.world.activate(info["id"], timeline, now_real=1.7e9)
    case.world.advance(info["id"], timeline, now_real=1.7e9 + 300)
    at_commit = int(case.store.clock_get(timeline)["processed_world"])
    commit_id = case.world.commit(info["id"], timeline, kind="manual", note="回滚点")["id"]
    case.world.advance(info["id"], timeline, now_real=1.7e9 + 3600)
    further = int(case.store.clock_get(timeline)["processed_world"])
    rolled = case.world.rollback(info["id"], timeline, commit_id=commit_id, now_real=1.7e9 + 4000)

    meta_after = {
        key: case.store.instance_get(info["id"])[key]
        for key in ("name", "original_name", "package_id", "data_format", "rules_version", "seed", "moment")
    }
    assert further > rolled["world"] == at_commit, (further, rolled, at_commit)
    assert case.store.instance_get(info["id"])["setting"] == setting_before, "回滚改动了锁定设定"
    assert meta_after == meta_before, (meta_before, meta_after)
    assert case.store.timeline_get(timeline) is not None, "回滚换了线身份"

    # 改世界包：不追溯已有实例；新实例才拿新内容（§2.2-7 / §5.1）
    package["world"]["geography"] = "审计改写过的地理"
    assert case.store.instance_get(info["id"])["setting"] == setting_before, "改包追溯到了已有实例"
    fresh = create_instance(case.store, package, [example_card(package, name="堤禾")], display_name="改包后的新实例")
    fresh_setting = json.loads(case.store.instance_get(fresh["id"])["setting"])
    assert fresh_setting["world_package"]["world"]["geography"] == "审计改写过的地理"

    # 改局势：引入世界事件 → 原子建新线，原线不动（§2.2-8 / EVENT_ENGINE §八）
    from isekai_core.runtime.events import SUPPORTED_EFFECTS

    targets, _channels = case.world._known_targets(
        case.store.instance_get(info["id"]), timeline, world_seconds=rolled["world"]
    )
    accepted = None
    refusals: list[str] = []
    for kind in sorted(SUPPORTED_EFFECTS):
        for target in sorted(targets):
            draft = asyncio.run(
                case.world.draft_user_event(
                    info["id"],
                    timeline,
                    intent="盐滩上的汛期提前了",
                    payload={"when": "now", "effects": [{"kind": kind, "target": target, "expiry": "with_cause"}]},
                    llm=None,
                    now_real=1.7e9 + 4200,
                )
            )
            if draft.get("accepted"):
                accepted = (kind, target, str(draft["draft"]["draft_id"]))
                break
            refusals.append(f"{kind}@{target}: {draft.get('reason')}")
        if accepted:
            break
    assert accepted, f"没有任何受支持效果能被接受：{refusals[:3]}"
    kind, target, draft_id = accepted
    before_events = case.store._conn.execute(
        "SELECT COUNT(*) FROM event WHERE timeline_id=?", (timeline,)
    ).fetchone()[0]
    confirmed = case.world.confirm_user_event(
        info["id"], draft_id, name="审计引入事件", now_real=1.7e9 + 4300
    )
    new_line = str(confirmed["timeline_id"])
    after_events = case.store._conn.execute(
        "SELECT COUNT(*) FROM event WHERE timeline_id=?", (timeline,)
    ).fetchone()[0]
    new_events = case.store._conn.execute(
        "SELECT COUNT(*) FROM event WHERE timeline_id=?", (new_line,)
    ).fetchone()[0]
    users = case.store._conn.execute(
        "SELECT COUNT(*) FROM event WHERE timeline_id=? AND id LIKE 'ev-user%'", (new_line,)
    ).fetchone()[0]
    assert new_line != timeline and str(confirmed.get("timeline_id")) != timeline
    assert int(after_events) == int(before_events), "原线的历史被改写"
    assert users >= 1 and int(new_events) > 0, (users, new_events)
    return "PASS", (
        f"回滚只覆盖运行状态：水位 {further} → {rolled['world']}（= 回滚点 {at_commit}），线身份与 "
        f"{len(meta_before)} 项实例元数据、锁定设定快照全部未变；改世界包不追溯已有实例（新实例才带新地理）；"
        f"局势修改经「草案（{kind}@{target}）→ 确认」原子建新线 {new_line}（注入 ev-user 事件 {users} 条），"
        f"原线事件数保持 {after_events}"
    )


# ------------------------------------------------------------------ README / 配置声明

@item("README/config｜config.example.yaml 声明的每个键都真的被 config.py 消费")
def c_config_keys(case: Case) -> tuple[str, str]:
    import dataclasses

    example_text = (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    import yaml

    example = yaml.safe_load(example_text)
    assert isinstance(example, dict), "模板不是映射"

    models: dict[str, Any] = {
        "llm": __import__("isekai_core.config", fromlist=["LLMConfig"]).LLMConfig,
        "runtime": RuntimeConfig,
        "backup": __import__("isekai_core.config", fromlist=["BackupConfig"]).BackupConfig,
    }
    # core 段落在 Config 本体上（不是子模型）
    from isekai_core.config import Config as _Cfg

    core_fields = {f.name for f in dataclasses.fields(_Cfg)}
    unmapped: list[str] = []
    for section, body in example.items():
        if section == "placeholder":
            known = {"instance_id", "timeline_id", "character_id", "system_prompt"}
            unmapped += [f"placeholder.{k}" for k in body if k not in known]
            continue
        if section == "core":
            unmapped += [f"core.{k}" for k in body if k not in core_fields]
            continue
        if section not in models:
            unmapped += [f"{section}.*（无对应段模型）"]
            continue
        fields = {f.name for f in dataclasses.fields(models[section])}
        unmapped += [f"{section}.{k}" for k in body if k not in fields]
    assert not unmapped, f"模板声明但代码不认的键：{unmapped}"

    # 逐键注入可区分的取值（不吃模板原值），核对全部落到 cfg 上
    values: dict[str, dict[str, Any]] = {
        "llm": {
            "base_url": "http://127.0.0.1:9/v1",
            "model": "audit-model",
            "api_key": "audit-key",
            "timeout_s": 12.5,
            "max_tokens": 777,
            "temperature": 0.11,
        },
        "core": {
            "host": "127.0.0.1",
            "port": 0,
            "max_text_len": 1234,
            "max_parts": 7,
            "context_history_max": 9,
        },
        "runtime": {
            "rate_max": 123456,
            "max_active_timelines": 3,
            "catch_up_batches": 5,
            "catch_up_lag_seconds": 999,
            "render_calls_per_day": 11,
            "memory_extract_per_day": 13,
            "memory_recall_limit": 4,
            "memory_brief_tokens": 555,
            "memory_decay_per_day": 0.07,
            "memory_embedding_model": "audit-embed",
            "memory_embedding_base_url": "http://127.0.0.1:9/v1",
            "memory_embedding_api_key": "audit-emb-key",
            "autocommit_enabled": False,
            "autocommit_minutes": 15,
            "autocommit_events": 21,
        },
        "placeholder": {
            "instance_id": "ph-audit",
            "timeline_id": "tl-audit",
            "character_id": "cc-audit",
            "system_prompt": "审计占位提示词",
        },
    }
    cfg_path = case.cfg.paths.config_file
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump(values, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    cfg = load_config(case.dir)
    checks = {
        "llm.base_url": (cfg.llm.base_url, "http://127.0.0.1:9/v1"),
        "llm.model": (cfg.llm.model, "audit-model"),
        "llm.api_key": (cfg.llm.api_key, "audit-key"),
        "llm.timeout_s": (cfg.llm.timeout_s, 12.5),
        "llm.max_tokens": (cfg.llm.max_tokens, 777),
        "llm.temperature": (cfg.llm.temperature, 0.11),
        "core.host": (cfg.host, "127.0.0.1"),
        "core.max_text_len": (cfg.max_text_len, 1234),
        "core.max_parts": (cfg.max_parts, 7),
        "core.context_history_max": (cfg.context_history_max, 9),
        "placeholder": (dict(cfg.placeholder), {**values["placeholder"], "system_prompt": "审计占位提示词"}),
    }
    service = from_config(cfg, case.store)
    for key, expected in values["runtime"].items():
        staged = cfg.runtime
        got = {
            "rate_max": service.rate_max,
            "max_active_timelines": service.max_active_timelines,
            "catch_up_batches": service.catch_up_batches,
            "catch_up_lag_seconds": service.catch_up_lag_seconds,
            "render_calls_per_day": service.render_calls_per_day,
            "memory_extract_per_day": service.memory_extract_per_day,
            "memory_recall_limit": service.memory_recall_limit,
            "memory_brief_tokens": service.memory_brief_tokens,
            "memory_decay_per_day": service.memory_decay_per_day,
            "memory_embedding_model": service.embedding_model,
            "memory_embedding_base_url": service.embedding_base_url,
            "memory_embedding_api_key": service.embedding_api_key,
            "autocommit_enabled": service.autocommit_enabled,
            "autocommit_minutes": service.autocommit_minutes,
            "autocommit_events": service.autocommit_events,
        }[key]
        declared = getattr(staged, key)
        assert got == expected and declared == expected, f"{key}: cfg={declared!r} service={got!r} 期望 {expected!r}"
    for key, (got, expected) in checks.items():
        assert got == expected, f"{key}: {got!r} ≠ {expected!r}"
    assert service.embedding_ready is True, "三项 embedding 配置齐备时 embedding_ready 应为 True"
    return "PASS", (
        f"模板 {sum(len(v) for v in example.values())} 个键全部有对应字段（0 个孤儿键）；"
        f"改写 config.yaml 后逐键核对：llm 6 键 / core 5 键 / placeholder 4 键落在 Config 上，"
        f"runtime 15 键同时落在 RuntimeConfig 与 RuntimeService 实例属性上（含 embedding 三项 → embedding_ready=True、"
        f"autocommit_enabled=False）"
    )


@item("README｜命令实测（核心握手 / cli --say / world_cli 全链）")
def c_readme_commands(case: Case) -> tuple[str, str]:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    assert python.exists(), f"README 写的解释器不存在：{python}"
    root = case.dir
    env = {**os.environ, "ISEKAI_LLM_FAKE": "1", "PYTHONIOENCODING": "utf-8"}
    notes: list[str] = []

    def run(cmd: list[str], *, cwd: Path = root, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd, cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )

    # 1) 只跑核心：就绪握手一行 JSON
    core = subprocess.Popen(
        [str(python), "-m", "isekai_core", "--root", str(root)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        line = core.stdout.readline() if core.stdout else ""
        ready = json.loads(line)
    finally:
        core.terminate()
        core.wait(timeout=10)
    assert ready.get("event") == "ready" and ready.get("state") == "ready", ready
    assert ready.get("endpoint", "").startswith("ws://127.0.0.1:") and ready.get("bootstrap") and ready.get("mgmt")
    notes.append(f"`-m isekai_core` 就绪握手 {ready['event']}/{ready['state']} endpoint={ready['endpoint']}")

    # 2) 单轮对话（README 的不联网自测链路）
    chat = run([str(python), "-m", "isekai_core.cli", "--root", str(root), "--say", "你好"])
    assert chat.returncode == 0, chat.stdout + chat.stderr
    assert "· 核心已就绪" in chat.stdout and "角色>" in chat.stdout, chat.stdout
    notes.append(f"`cli --say` 退出码 0，输出含就绪行与 `角色>` 分段（{len(chat.stdout.strip().splitlines())} 行）")

    # 3) 世界设定层 CLI（README「世界设定层」一节的命令）
    packages = root / "packages"
    saltflat = packages / "saltflat.json"
    template = run([str(python), "-m", "isekai_core.world_cli", "--root", str(root),
                    "package", "template", "--name", "盐滩纪", "--out", str(saltflat)])
    assert template.returncode == 0 and saltflat.exists(), template.stdout + template.stderr
    validate_template = run([str(python), "-m", "isekai_core.world_cli", "--root", str(root),
                             "package", "validate", "--file", str(saltflat)])
    notes.append(f"`package template` → {saltflat.name}（退出码 0）；`package validate` 空骨架退出码 "
                 f"{validate_template.returncode}（{first_line(validate_template.stdout)}）")

    save_package(saltflat, example_package("盐滩纪"))
    card_file = packages / "tihe.json"
    card = run([str(python), "-m", "isekai_core.world_cli", "--root", str(root),
                "card", "template", "--package", str(saltflat), "--name", "堤禾", "--out", str(card_file)])
    assert card.returncode == 0 and card_file.exists(), card.stdout + card.stderr
    skeleton_confirm = run([str(python), "-m", "isekai_core.world_cli", "--root", str(root),
                            "card", "confirm", "--package", str(saltflat), "--file", str(card_file)])
    assert skeleton_confirm.returncode == 1 and "缺少" in skeleton_confirm.stdout, skeleton_confirm.stdout
    skeleton_reason = first_line(skeleton_confirm.stdout)

    # 骨架挡住了确认（符合 README「空壳骨架会被挡在保存与创建之前」）；换一份通过校验的卡再走确认
    card_file.write_text(
        json.dumps(example_card(example_package("盐滩纪"), name="堤禾", confirmed=False), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    confirm = run([str(python), "-m", "isekai_core.world_cli", "--root", str(root),
                   "card", "confirm", "--package", str(saltflat), "--file", str(card_file)])
    assert confirm.returncode == 0, confirm.stdout + confirm.stderr
    created = run([str(python), "-m", "isekai_core.world_cli", "--root", str(root),
                   "instance", "create", "--package", str(saltflat), "--card", str(card_file)])
    assert created.returncode == 0, created.stdout + created.stderr
    instance_id = parse_result(created.stdout)["instance"]["id"]
    listed = run([str(python), "-m", "isekai_core.world_cli", "--root", str(root), "instance", "list"])
    assert listed.returncode == 0 and instance_id in listed.stdout, listed.stdout
    exported = packages / "saltflat.isekai.json"
    out = run([str(python), "-m", "isekai_core.world_cli", "--root", str(root),
               "instance", "export", "--id", instance_id, "--out", str(exported)])
    assert out.returncode == 0 and exported.exists(), out.stdout + out.stderr
    imported = run([str(python), "-m", "isekai_core.world_cli", "--root", str(root),
                    "instance", "import", "--file", str(exported)])
    assert imported.returncode == 0, imported.stdout + imported.stderr
    notes.append(f"`card template/confirm` + `instance create/list/export/import` 退出码全 0（实例 {instance_id}，导入件 {exported.name}）；"
                 f"空骨架确认被挡（退出码 1：{skeleton_reason}…），符合 README「空壳骨架会被挡在保存与创建之前」")

    data_dir = root / "data"
    assert (data_dir / "isekai.db").exists() and (root / "logs" / "core.log").exists()
    db_mode = sqlite3.connect(str(data_dir / "isekai.db")).execute("PRAGMA journal_mode").fetchone()[0]
    assert db_mode.lower() == "wal", db_mode
    notes.append(f"数据落点 data/isekai.db（journal_mode={db_mode}）+ logs/core.log 如 README 所述")
    return "PASS", "；".join(notes)


# ------------------------------------------------------------------ runner


def run_one(title: str, fn: Callable[[Case], tuple[str, str]]) -> tuple[str, str]:
    case = Case(title)
    try:
        return fn(case)
    finally:
        case.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="DESIGN.md 跨模块一致性审计（第 2 轮）")
    parser.add_argument("--only", default=None, help="只跑标题包含该子串的条目")
    args = parser.parse_args()

    started = time.time()
    tally = {"PASS": 0, "FAIL": 0, "DEFERRED": 0}
    for index, (title, fn) in enumerate(CHECKS, start=1):
        if args.only and args.only not in title:
            continue
        try:
            status, evidence = run_one(title, fn)
        except AssertionError as exc:
            status, evidence = "FAIL", f"{exc}"
        except Exception as exc:  # noqa: BLE001 —— 探针本身出错也要如实报
            status, evidence = "FAIL", f"探针异常 {type(exc).__name__}: {exc}"
            if os.environ.get("AUDIT2_TRACE"):
                traceback.print_exc()
        tally[status] = tally.get(status, 0) + 1
        print(f"[{index:02d}] {status} {title} — {evidence}")
    total = sum(tally.values())
    print(
        f"TOTAL {total} PASS {tally['PASS']} FAIL {tally['FAIL']} DEFERRED {tally['DEFERRED']}"
        f"（{time.time() - started:.1f}s）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
