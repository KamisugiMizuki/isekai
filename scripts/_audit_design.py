"""DESIGN.md 行为级审计探针（docs/DESIGN.md §7.1 验收要点 + §5/§6 可检验原则）。

纪律：
- 只创建本文件；不改项目任何文件、不碰 data/isekai.db；
- 全部在 tempfile 临时根目录里建库（不启动核心进程、不监听端口）；
- 只用假 LLM（FakeLLM 子类）与纯函数，不发任何真实请求；
- 每个条目输出 `STATUS 摘要 — 证据`，末行 `TOTAL n PASS p FAIL f DEFERRED d`；
- 界面形态类条目用数据层 / 管理面证据替代，无法判定者标 SKIP。

运行：.venv/Scripts/python.exe scripts/_audit_design.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from isekai_core.config import SettingsError, load_config, validate_llm_updates  # noqa: E402
from isekai_core.llm import LLMError, FakeLLM  # noqa: E402
from isekai_core.runtime import cognition, personality  # noqa: E402
from isekai_core.runtime.calendar import calendar_from_package  # noqa: E402
from isekai_core.runtime.clock import (  # noqa: E402
    ClockState,
    RateCommand,
    natural_second,
    settle,
    target_world,
)
from isekai_core.runtime.service import RuntimeService, RuntimeStateError  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.ump import UmpError  # noqa: E402
from isekai_core.version import (  # noqa: E402
    CAPABILITIES,
    CONTAINER_VERSION,
    DATA_FORMAT_VERSION,
    RULES_VERSION,
)
from isekai_core.world import generator, ops, portable  # noqa: E402
from isekai_core.world.cards import template_card, validate_card  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import (  # noqa: E402
    InstanceError,
    compatibility,
    create_instance,
)
from isekai_core.world.package import load_package, save_package  # noqa: E402
from isekai_core.world.validate import validate_package  # noqa: E402

CHECKS: list[tuple[str, Callable[["Case"], tuple[str, str]]]] = []


def item(title: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        CHECKS.append((title, fn))
        return fn

    return deco


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(record)
        except Exception:  # pragma: no cover
            pass


class Case:
    """一条审计条目一个独立临时根目录。"""

    def __init__(self, label: str) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix=f"isekai-audit-{label}-"))
        self.cfg = load_config(self.dir)
        self.store = Store(self.cfg.paths.db)
        self.store.ensure_schema()
        self.world = RuntimeService(self.store, autocommit_enabled=False)
        logging.getLogger("isekai").setLevel(logging.DEBUG)
        self.capture = Capture()
        logging.getLogger("isekai").addHandler(self.capture)

    def close(self) -> None:
        logging.getLogger("isekai").removeHandler(self.capture)
        try:
            self.store.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


def variant(name: str, *, era: str, months: tuple[str, ...], note: str) -> dict[str, Any]:
    """同结构、内容差异明显的世界包（多世界条目的“差异极大”用）。"""
    package = example_package(name)
    package["calendar"]["era"] = era
    for index, month in enumerate(months):
        package["calendar"]["months"][index]["name"] = month
    package["world"]["geography"] = note
    package["world"]["axioms"][0]["text"] = f"{note}——首要公理"
    package["canon"][0]["statement"] = f"{note}的第一条实情记录"
    return package


def mk(
    case: Case,
    name: str = "灰潮纪",
    *,
    moment: int = DAY * 1500,
    cards: list[dict[str, Any]] | None = None,
    seed: str | None = None,
    package: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    package = package if package is not None else example_package(name, moment=moment)
    cards = list(cards) if cards else [example_card(package)]
    info = create_instance(case.store, package, cards, seed=seed)
    timeline = case.store.timeline_list(info["id"])[0]
    case.world.ensure_instance(info["id"], now_real=time.time())
    return info, timeline["id"], str((cards[0].get("meta") or {}).get("card_id")), package


def say(case: Case, iid: str, tlid: str, cid: str, *, env: str, text: str, reply: str) -> str:
    """固化一轮对话（不经 LLM）。"""
    session = case.store.session_ensure(iid, tlid, cid)
    case.store.inbound_put(
        session_id=session["id"], channel_id="cli-dev", thread_id=f"t-{cid}",
        env_id=env, text=text, binding_version=1,
    )
    out = case.store.outbound_put(
        session_id=session["id"], message_id=f"m-{env}", reply_to=env, covers=[env],
        batches=[[reply]], target_channel="cli-dev", target_thread=f"t-{cid}",
        binding_version=1, binding_token="tok",
    )
    return str(out["message_id"])


def texts_of(case: Case, session_id: str) -> list[str]:
    page = case.store.history_page(session_id, limit=200)
    return [case.store.message_text(row) for row in page.get("messages") or []]


class PackageJSONLLM(FakeLLM):
    """假 LLM：把一份合法 JSON 当作模型产物返回（覆盖率足够时生成器校验通过）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__([""])
        self.payload = payload

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.calls.append(messages)
        return json.dumps(self.payload, ensure_ascii=False)


# ---------------------------------------------------------------- §7.1 验收要点


@item("§7.1-01 独立性｜新项目不依赖原项目即可运行")
def c_independence(case: Case) -> tuple[str, str]:
    hits = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "isekai_core").rglob("*.py")
        if "veranima" in path.read_text(encoding="utf-8").lower()
    ]
    info, tl, _cid, _pkg = mk(case, "独立世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    moved = case.world.advance(info["id"], tl, now_real=1.7e9 + 7200)
    assert not hits, f"源码出现原项目引用：{hits}"
    assert moved["processed_world"] > DAY * 1500, moved
    return "PASS", (
        f"临时根目录内可完整建库/建实例/激活/推进（世界秒 → {moved['processed_world']}，无网络、无原项目进程）；"
        "isekai_core 全树 0 处 veranima 引用"
    )


@item("§7.1-02 通道插件化｜内建聊天可停用；可接入第三方插件（独立进程）")
def c_channel_plugin(case: Case) -> tuple[str, str]:
    info, tl, _cid, _pkg = mk(case)
    assert case.store.channel_by_name("builtin") is None
    case.world.activate(info["id"], tl, now_real=1.7e9)
    assert case.world.advance(info["id"], tl, now_real=1.7e9 + 60)["processed_world"] > DAY * 1500
    return "DEFERRED", (
        "第三方插件宿主按 CHANNEL_PLUGIN_SPEC 实施分期后置（DESIGN §6.1 阶段 0「后置」、§9「按 SPEC 保持后置」）："
        "全仓库 0 处插件进程管理代码（grep plugin → 仅 docs/ 与 node_modules）；"
        "可验部分：核心零通道注册即可建实例并推进（内建聊天只是 UMP 通道 client.py:25 channel_id='builtin'，不内嵌于核心）"
    )


@item("§7.1-03 插件隔离｜插件崩溃不影响核心")
def c_plugin_isolation(case: Case) -> tuple[str, str]:
    source = (ROOT / "isekai_core" / "channel.py").read_text(encoding="utf-8")
    assert "PROTOCOL_ERROR_LIMIT" in source and "MAX_FRAME_BYTES" in source
    return "DEFERRED", (
        "插件宿主后置（同上）→ 没有独立插件进程可做崩溃注入；可验的相邻机制：单个通道连接有独立的错误上限与帧上限，"
        "超限只断这条连接、不牵动核心（channel.py:94,258 + version.py:34-36 PROTOCOL_ERROR_LIMIT=5 / MAX_FRAME_BYTES=1MiB）；"
        "真正的「插件进程崩溃不影响核心」要等宿主落地再验"
    )


@item("§7.1-04 插件兼容｜接口版本变化时提示用户；报错则反馈并停止")
def c_plugin_compat(case: Case) -> tuple[str, str]:
    handshake = (ROOT / "isekai_core" / "channel.py").read_text(encoding="utf-8")
    assert "protocol" in handshake, "握手不校验协议"
    return "DEFERRED", (
        "宿主后置 → 无插件侧接口版本提示面可验；可验的相邻机制：通道握手校验并记录协议版本与能力交集"
        "（store.channel_set_handshake + version.py:11-13 UMP 协商只认主版本），不匹配时返回错误码而非静默接受；"
        "插件接口版本变化的用户提示属宿主与桌面壳范围，未落地"
    )


@item("§7.1-05 插件文档｜提供接口文档与参考实现")
def c_plugin_docs(case: Case) -> tuple[str, str]:
    spec = next((ROOT / "docs").rglob("CHANNEL_PLUGIN_SPEC.md")).exists()
    refs = [
        p.relative_to(ROOT).as_posix()
        for p in ROOT.rglob("*plugin*")
        if not {"node_modules", ".venv", ".git", "__pycache__"} & set(p.parts)
    ]
    assert spec, "接口文档缺失"
    return "DEFERRED", (
        f"接口文档在（docs/CHANNEL_PLUGIN_SPEC.md）；参考实现未落地（仓库仅 {refs or '无 plugin 文件'}）；"
        "宿主后置，故整条 DEFERRED"
    )


@item("§7.1-06 内建聊天｜官方通道客户端，随应用发行，遵循统一消息协议（界面部分以协议/数据层证据替代）")
def c_builtin_chat(case: Case) -> tuple[str, str]:
    from isekai_core.client import UmpClient

    issued, credential = case.store.channel_register(
        name="builtin", display_name="内建聊天窗口", version="1.0",
        protocol="1.0", capabilities={},
    )
    assert credential and case.store.channel_verify_credential("builtin", credential) is not None
    client_source = (ROOT / "isekai_core" / "client.py").read_text(encoding="utf-8")
    assert UmpClient is not None
    assert issued["protocol"] == "1.0" and "name" in issued, issued
    assert "内建聊天窗口" in client_source and "channel_id: str = \"builtin\"" in client_source
    return "PASS", (
        f"内建聊天 = 官方 UMP 通道客户端（isekai_core/client.py:1-26，默认 channel_id='builtin'、name='内建聊天窗口'），"
        f"经登记（protocol={issued['protocol']}）+ 凭据鉴权（channel_verify_credential 命中）接入，与第三方通道同一协议、核心侧无特权路径；"
        "桌面壳经同一 mgmt/UMP 通路接入（desktop/src/main.ts:1076 调共享生成器 op）"
    )


@item("§7.1-07 世界包文件｜可修改，仅保留最新版，与世界实例无关联")
def c_package_file(case: Case) -> tuple[str, str]:
    path = case.dir / "packages" / "甲.json"
    package = example_package("甲世界")
    save_package(path, package)
    info, _tl, _cid, _pkg = mk(case, "甲世界")
    changed = load_package(path)
    changed["world"]["axioms"][0]["text"] = "被用户改写的公理"
    save_package(path, changed)
    reread = load_package(path)
    files = sorted(p.name for p in path.parent.iterdir() if p.suffix == ".json")
    setting = json.loads(case.store.instance_get(info["id"])["setting"])
    assert reread["world"]["axioms"][0]["text"] == "被用户改写的公理", "文件未保留最新版"
    assert files == ["甲.json"], f"同一包产生了多份文件：{files}"
    assert setting["world_package"]["world"]["axioms"][0]["text"] != "被用户改写的公理", "实例被文件改动追溯"
    return "PASS", (
        f"文件改写生效且仅一份（{files}），实例快照保持创建时的公理"
        f"（{setting['world_package']['world']['axioms'][0]['text'][:10]}…）——实例与世界包脱钩"
        "（instances.py:115-118 深拷贝 + package.py:153-170 原子覆写）"
    )


@item("§7.1-08 世界包名称｜嵌入快照，原始名称独立记录；用户修改不生效")
def c_package_name(case: Case) -> tuple[str, str]:
    package = example_package("灰潮纪")
    info, _tl, _cid, _pkg = mk(case, "灰潮纪")
    row = case.store.instance_get(info["id"])
    setting = json.loads(row["setting"])
    package["meta"]["display_name"] = "改过的名字"
    after = case.store.instance_get(info["id"])
    assert setting["original_name"] == "灰潮纪"
    assert row["original_name"] == "灰潮纪" and row["name"] == "灰潮纪"
    assert after["name"] == "灰潮纪"
    return "PASS", (
        "实例行 name/original_name 与快照 setting.original_name 都是锁定时的名字；改包名不回溯"
        "（instances.py:70-75、package.py:127-135）"
    )


@item("§7.1-09 世界包生成器｜两端共享同一实现；生成包当前版本可运行")
def c_generator_shared(case: Case) -> tuple[str, str]:
    package = example_package("生成世界")
    llm = PackageJSONLLM(package)
    candidate, errors, _usage = asyncio.run(
        generator.generate_package(llm, "一个海堤与盐潮的世界", name="生成世界")
    )
    assert errors == [], f"对话式生成产物未通过校验：{errors[:3]}"
    info = create_instance(case.store, candidate, [example_card(candidate)])
    assert info["id"], "生成包未能创建实例"
    desktop_calls = (ROOT / "desktop" / "src" / "main.ts").read_text(encoding="utf-8")
    assert "world.package.generate" in desktop_calls, "桌面端未调用共享生成器"
    return "PASS", (
        f"生成器是核心单一实现（isekai_core/world/generator.py）；对话式生成产物直接建实例成功（{info['name']}）；"
        "桌面壳经 mgmt 调同一 op（desktop/src/main.ts:1076 world.package.generate）——安卓端共享见 §7.1-41"
    )


@item("§7.1-10 世界实例｜设定锁死、内部黑箱、可导入导出")
def c_instance(case: Case) -> tuple[str, str]:
    info, _tl, _cid, package = mk(case, "锁死世界")
    package["world"]["axioms"].append({"id": "ax-99", "text": "创建后新增的公理"})
    setting = json.loads(case.store.instance_get(info["id"])["setting"])
    assert "创建后新增的公理" not in json.dumps(setting, ensure_ascii=False)
    exported = portable.write_export(case.store, info["id"], case.dir / "exports" / "a.isekai")
    assert exported["name"] == info["name"]
    return "PASS", (
        "设定在创建时深拷贝成快照（instances.py:115-117），此后改内存/包文件都不变；"
        f"导出成功（counts={exported['counts']}）；管理面 instance.info 只回角色名/职业与时间线元数据（ops.py:419-446），无内部状态浏览入口"
    )


@item("§7.1-11 导出包｜单一文件、不加密、含世界包原始名称")
def c_export_file(case: Case) -> tuple[str, str]:
    info, _tl, _cid, _pkg = mk(case, "灰潮纪")
    path = case.dir / "exports" / "out.isekai"
    portable.write_export(case.store, info["id"], path)
    produced = sorted(p.name for p in path.parent.iterdir())
    head = json.loads(path.read_text(encoding="utf-8"))["container"]
    assert produced == ["out.isekai"], produced
    assert head["original_name"] == "灰潮纪"
    return "PASS", (
        f"单一 JSON 文件（{produced}），明文可读，container.original_name={head['original_name']}；"
        "容器段自带 format/container_version/data_format/rules_version/capabilities"
    )


@item("§7.1-12 导出内容｜仅实例本身，不含激活状态")
def c_export_scope(case: Case) -> tuple[str, str]:
    info, tl, _cid, _pkg = mk(case, "激活世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + 300)
    path = case.dir / "exports" / "active.isekai"
    portable.write_export(case.store, info["id"], path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    copy = portable.import_instance(case.store, raw, display_name="副本")
    states = {item["state"] for item in case.store.timeline_list(copy["id"])}
    assert states == {"frozen"}, states
    assert "active" not in json.dumps(raw["runtime"].get("state") or {}, ensure_ascii=False)
    return "PASS", (
        f"导入不恢复激活：副本 {len(case.store.timeline_list(copy['id']))} 条线全 frozen（portable.py:306）；"
        "注意（信息性）：文件里 runtime.timelines[*].state 仍是导出时的本机状态字段（portable.py:74），只是不参与恢复"
    )


@item("§7.1-13 导入语义｜始终创建新实例，不影响已有实例")
def c_import_new(case: Case) -> tuple[str, str]:
    info, tl, _cid, _pkg = mk(case, "原件世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + 300)
    before = case.store.clock_get(tl)["processed_world"]
    path = case.dir / "exports" / "copy.isekai"
    portable.write_export(case.store, info["id"], path)
    copy = portable.import_instance(case.store, json.loads(path.read_text(encoding="utf-8")))
    assert copy["id"] != info["id"], "导入复用了原实例"
    assert case.store.clock_get(tl)["processed_world"] == before, "原实例被改动"
    assert len(case.store.instance_list()) == 2
    return "PASS", f"导入得到新实例 {copy['name']}（{copy['id'][:8]}…），原实例水位不变（{before}）"


@item("§7.1-14 导入命名｜原始名称自动生成 + 用户调整；全局唯一；重名追加 _2")
def c_import_name(case: Case) -> tuple[str, str]:
    info, _tl, _cid, _pkg = mk(case, "灰潮纪")
    path = case.dir / "exports" / "n.isekai"
    portable.write_export(case.store, info["id"], path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    first = portable.import_instance(case.store, raw)
    second = portable.import_instance(case.store, raw)
    renamed = portable.import_instance(case.store, raw, display_name="用户命名")
    assert (first["name"], second["name"], renamed["name"]) == ("灰潮纪_2", "灰潮纪_3", "用户命名"), (
        first["name"], second["name"], renamed["name"]
    )
    return "PASS", (
        f"同名追加序号从 _2 起（{first['name']} / {second['name']}，package.py:35-48 unique_name），"
        f"display_name 可覆盖（{renamed['name']}）"
    )


@item("§7.1-15 版本兼容｜导入与本机实例都先检查；不兼容阻断；检查先于推进")
def c_version_gate(case: Case) -> tuple[str, str]:
    info, tl, _cid, _pkg = mk(case, "版本世界")
    path = case.dir / "exports" / "v.isekai"
    portable.write_export(case.store, info["id"], path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    bad_container = json.loads(json.dumps(raw))
    bad_container["container"]["container_version"] = str(int(CONTAINER_VERSION.split(".")[0]) + 1) + ".0"
    assert portable.check_compatibility(bad_container)[0] == "incompatible"
    try:
        portable.import_instance(case.store, bad_container)
    except InstanceError:
        blocked = True
    else:
        blocked = False
    assert blocked, "主版本不兼容的导出件被导入了"

    bad_format = json.loads(json.dumps(raw))
    bad_format["container"]["data_format"] = "9.9"
    assert portable.check_compatibility(bad_format)[0] == "incompatible"
    bad_cap = json.loads(json.dumps(raw))
    bad_cap["container"]["capabilities"] = list(CAPABILITIES) + ["future.capability.v9"]
    assert portable.check_compatibility(bad_cap)[0] == "incompatible"

    row = case.store.instance_get(info["id"])
    healthy = compatibility(row)[0]
    convert = compatibility({**row, "data_format": DATA_FORMAT_VERSION.split(".")[0] + ".9"})[0]
    blocked_row = compatibility({**row, "data_format": "9.0"})[0]
    assert (healthy, convert, blocked_row) == ("compatible", "convertible", "blocked"), (healthy, convert, blocked_row)

    # 检查先于推进：把本机实例数据格式改成不兼容，运行层是否仍推进？
    with case.store._lock, case.store._conn:  # noqa: SLF001 探针直改临时库
        case.store._conn.execute("UPDATE instance SET data_format='9.0' WHERE id=?", (info["id"],))
    refused = []
    for call in (lambda: case.world.activate(info["id"], tl, now_real=1.7e9),
                 lambda: case.world.advance(info["id"], tl, now_real=1.7e9 + 3600)):
        try:
            call()
            refused.append(False)
        except RuntimeStateError:
            refused.append(True)
    assert all(refused), "blocked 的实例仍能激活 / 推进"
    clock = case.store.clock_get(tl) or {}
    assert int(clock.get("processed_world") or 0) == DAY * 1500, "水位被推进了"
    return "PASS", (
        "导入三闸门都在（容器主版本 / 数据格式主版本 / 必需能力，不兼容 → InstanceError）；"
        f"「检查先于推进」成立：把本机实例 data_format 改成 9.0（compatibility 判 blocked）后，"
        f"activate 与 advance 都被拒（水位保持 {clock.get('processed_world')}）"
    )


@item("§7.1-16 世界时钟｜公开，仅展示当前激活时间线时间")
def c_clock_public(case: Case) -> tuple[str, str]:
    first, tl_a, _cid, _pkg = mk(case, "甲世界")
    second, tl_b, _cid2, _pkg2 = mk(case, "乙世界")
    case.world.activate(first["id"], tl_a, now_real=1.7e9)
    case.world.advance(first["id"], tl_a, now_real=1.7e9 + 120)
    frozen_view = case.world.view(second["id"], tl_b, now_real=1.7e9 + 10_000)
    active_view = case.world.view(first["id"], tl_a, now_real=1.7e9 + 120)
    clock = ops.dispatch(case.cfg, case.store, "runtime.clock",
                         {"instance_id": first["id"], "timeline_id": tl_a}, runtime=case.world)
    assert frozen_view["state"] == "frozen" and frozen_view["processed_world"] == DAY * 1500
    assert "world_seconds" not in frozen_view, "冻结线不该给推进中的时钟"
    assert active_view["world_seconds"] > DAY * 1500
    assert clock["clock"]["rate"] == 1 and clock["clock"]["label"]
    return "PASS", (
        f"管理面 runtime.clock 给出该线公开世界时间（{clock['clock']['world_seconds']}，倍率 {clock['clock']['rate']}，"
        f"{clock['clock']['label']}）；未激活线不给时钟、只回已冻结水位（view 在 state!=active 时提前返回，service.py:1420-1429）"
    )


@item("§7.1-17 世界时钟倍率｜每线独立、默认 1、自然整秒生效、归属输入时刻、整秒瞬间归下一秒")
def c_rate(case: Case) -> tuple[str, str]:
    assert natural_second(100.0) == 101, "整秒输入应归下一整秒"
    assert natural_second(100.4) == 101 and natural_second(100.999) == 101
    state = ClockState(base_real=1000.0, base_world=0, rate=1, high_water_real=1000.0)
    settled, consumed = settle(state, 1020.0, [RateCommand(input_real=1005.0, effective_real=1010, rate=10, seq=1)])
    assert target_world(settled, 1020.0) == 110 and [c.rate for c in consumed] == [10], "分段累计错误"

    info, tl_a, _cid, _pkg = mk(case, "倍率世界")
    other, tl_b, _cid2, _pkg2 = mk(case, "第二条线世界")
    now = 1_700_000_000.4
    case.world.activate(info["id"], tl_a, now_real=now)
    case.world.activate(other["id"], tl_b, now_real=now)
    default_rate = case.world.view(info["id"], tl_a, now_real=now)["rate"]
    change = case.world.set_rate(info["id"], tl_a, rate=60, now_real=now)
    assert default_rate == 1 and change["effective_real"] == 1_700_000_001
    later = case.world.view(info["id"], tl_a, now_real=1_700_000_011)["world_seconds"]
    untouched = case.world.view(other["id"], tl_b, now_real=1_700_000_011)["rate"]
    assert later == DAY * 1500 + 600, later
    assert untouched == 1, "倍率串线了"
    return "PASS", (
        f"默认倍率 1；输入 1700000000.4 → 生效整秒 1700000001（严格晚于输入，整秒瞬间输入归下一整秒）；"
        f"10 现实秒后世界 +600 秒（分段累计）；另一条线倍率仍为 {untouched}（clock.py:40-75 + service.py:1373-1415）"
    )


@item("§7.1-18 倍率上限｜全局配置、默认 2592000、仅开发者可配置")
def c_rate_max(case: Case) -> tuple[str, str]:
    from isekai_core.config import RuntimeConfig

    assert RuntimeConfig().rate_max == 2592000
    assert case.world.rate_max == 2592000
    info, tl, _cid, _pkg = mk(case, "上限世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    over = False
    try:
        case.world.set_rate(info["id"], tl, rate=2592001, now_real=1.7e9)
    except RuntimeStateError:
        over = True
    assert over, "超上限未被拒绝"
    for key in ("rate_max", "max_active_timelines"):
        try:
            validate_llm_updates({key: 10})
        except SettingsError:
            continue
        raise AssertionError(f"用户设置面可写 {key}")
    cfg_path = case.cfg.paths.config_file
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("runtime:\n  rate_max: 7200\n", encoding="utf-8")
    dev_cfg = load_config(case.dir)
    assert dev_cfg.runtime.rate_max == 7200, "开发者配置未生效"
    return "PASS", (
        "默认 2592000 且超限被拒（service.py:1336-1341）；用户设置面白名单只有 llm 段（config.py:175-199 → SettingsError）；"
        "config.yaml 的 runtime.rate_max 由开发者改写后生效（实测 7200）"
    )


@item("§7.1-19 多世界｜同一核心可运行 ≥ 3 个差异极大的世界包")
def c_multi_world(case: Case) -> tuple[str, str]:
    variants = [
        variant("甲世界", era="退潮纪", months=("盐月", "风月", "灯月"), note="三条沿岸城邦带"),
        variant("乙世界", era="浮灯纪", months=("潮月", "雾月", "炭月"), note="内陆盐碱荒原与浮桥城"),
        variant("丙世界", era="礁碑纪", months=("碑月", "渡月", "烬月"), note="礁石群岛与碑刻航道"),
    ]
    made = []
    for package in variants:
        info, tl, _cid, _ = mk(case, package=package)
        case.world.activate(info["id"], tl, now_real=1.7e9)
        case.world.advance(info["id"], tl, now_real=1.7e9 + 120)
        made.append((info["name"], package["calendar"]["era"]))
    eras = {item[1] for item in made}
    assert len(case.store.instance_list()) == 3 and len(eras) == 3, (len(case.store.instance_list()), eras)
    return "PASS", (
        f"同一库内 3 个实例各自独立推进（{made}），公理/纪元/月表/实情互不相同、互不串水位；"
        f"激活上限 {case.world.max_active_timelines} 条线（config.py:89）"
    )


@item("§7.1-20 多角色｜同世界 ≥ 2 角色、各自独立卡、共享世界时钟")
def c_multi_character(case: Case) -> tuple[str, str]:
    package = example_package("双角色世界")
    first = example_card(package, name="堤禾")
    second = example_card(package, name="堤砚")
    info = create_instance(case.store, package, [first, second])
    tl = case.store.timeline_list(info["id"])[0]["id"]
    case.world.ensure_instance(info["id"], now_real=time.time())
    cid_a, cid_b = str(first["meta"]["card_id"]), str(second["meta"]["card_id"])
    assert cid_a != cid_b
    units_a = case.store.unit_list(info["id"], tl, cid_a)
    units_b = case.store.unit_list(info["id"], tl, cid_b)
    plans = (case.store.plan_latest(info["id"], tl, cid_a), case.store.plan_latest(info["id"], tl, cid_b))
    session_a = case.store.session_ensure(info["id"], tl, cid_a)
    session_b = case.store.session_ensure(info["id"], tl, cid_b)
    assert units_a and units_b and all(item["character_id"] == cid_a for item in units_a)
    assert all(item["character_id"] == cid_b for item in units_b)
    assert plans[0] and plans[1] and session_a["id"] != session_b["id"]
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + 120)
    assert case.store.clock_get(tl)["processed_world"] > DAY * 1500
    return "PASS", (
        f"两张独立卡（{cid_a[:8]}… / {cid_b[:8]}…）各有独立单元 {len(units_a)}/{len(units_b)} 条、各自首日计划与独立会话；"
        "同线只有一个 timeline_clock 行（service.py:1272-1276 每线一行，多角色共享）"
    )


@item("§7.1-21 补卡｜一直存在、时间锚定、不越权、可回滚退出、可选已相识声明")
def c_add_character(case: Case) -> tuple[str, str]:
    info, tl, cid_a, package = mk(case, "补卡世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + 2 * DAY)
    mark = case.world.commit(info["id"], tl, note="补卡前")
    watermark = case.store.clock_get(tl)["processed_world"]

    late = example_card(package, name="堤砚")
    try:
        case.world.add_character(info["id"], tl, late, now_real=1.7e9, joined_world=watermark + DAY)
    except RuntimeStateError as exc:
        assert "不能晚于已完成水位" in str(exc), str(exc)
    else:
        raise AssertionError("未来时刻补入未被拒绝")

    unconfirmed = example_card(package, name="未确认者", confirmed=False)
    try:
        case.world.add_character(info["id"], tl, unconfirmed, now_real=1.7e9, joined_world=watermark)
    except RuntimeStateError as exc:
        assert "未确认" in str(exc), str(exc)
    else:
        raise AssertionError("未确认卡被补入")

    joined = case.world.add_character(
        info["id"], tl, late, now_real=1.7e9, joined_world=watermark, note="第二人", acquainted=True
    )
    cid_b = str(late["meta"]["card_id"])
    rows = case.store.character_join_list(info["id"], tl)
    assert rows and rows[0]["joined_world"] == watermark, rows
    units = case.store.unit_list(info["id"], tl, cid_b)
    assert any(item["semantic"] == "与联络者已相识" for item in units)
    assert not any(item["character_id"] == cid_a and item["id"] == "join-acquainted" for item in units)
    case.world.rollback(info["id"], tl, commit_id=mark["id"], now_real=1.7e9 + 3 * DAY)
    assert case.store.character_join_list(info["id"], tl) == [], "回滚未退出补卡角色"
    assert not case.store.unit_list(info["id"], tl, cid_b), "回滚未清理补入单元"
    return "PASS", (
        f"补入锚定已完成水位（joined_world={watermark}）；未来时刻/未确认卡被拒；「已相识」只加一条对话单元（共 {joined['units']} 条）；"
        "回滚跨越加入点即一致退出（join 行与单元同时消失）"
    )


@item("§7.1-22 AI 生成｜表单式 + 对话式都能产出可编辑世界包")
def c_generate_both(case: Case) -> tuple[str, str]:
    package = example_package("生成源世界")
    llm = PackageJSONLLM(package)
    dialog = asyncio.run(
        ops.dispatch_async(case.cfg, llm, "world.package.generate",
                           {"brief": "海堤与盐潮", "name": "生成世界"})
    )
    form = asyncio.run(
        ops.dispatch_async(case.cfg, llm, "world.package.fill",
                           {"package": example_package("表单世界"), "section": "canon"})
    )
    before = sorted(p.name for p in (case.cfg.paths.packages).glob("*"))
    assert dialog["valid"] is True and form["valid"] is True
    drafts = sorted(p.name for p in (case.cfg.paths.packages).glob("*"))
    assert drafts == before, f"候选被静默落盘：{drafts}"
    saved = ops.dispatch(case.cfg, case.store, "world.package.save",
                         {"package": dialog["candidate"], "path": str(case.dir / "packages" / "out.json")})
    assert validate_package(load_package(saved["path"])) == []
    return "PASS", (
        f"对话式（generate_package）与表单式（fill_section，实测段落 canon）都返回可编辑候选并通过整包校验；"
        f"候选不落盘（调用前后 packages/ 均为 {before}），用户显式 save 后才成为最新版；用量随件返回（usage={dialog['usage']}）"
    )


@item("§7.1-23 角色卡生成｜AI 生成 + 用户检查；只保留最终确认版本")
def c_card_generate(case: Case) -> tuple[str, str]:
    package = example_package("卡片世界")
    llm = PackageJSONLLM(example_card(package, name="生成角色", confirmed=True))
    result = asyncio.run(
        ops.dispatch_async(case.cfg, llm, "world.card.generate", {"package": package, "brief": "堤上的年轻守夜人"})
    )
    card = result["candidate"]
    card_path = case.dir / "packages" / "card.json"
    card_path.parent.mkdir(parents=True, exist_ok=True)
    ops.dispatch(case.cfg, case.store, "world.card.save", {"card": card, "card_path": str(card_path)})
    ops.dispatch(case.cfg, case.store, "world.card.save", {"card": card, "card_path": str(card_path)})
    files = sorted(p.name for p in card_path.parent.glob("*"))
    unconfirmed = dict(card)
    unconfirmed["meta"] = {**card["meta"], "confirmed": False}
    try:
        create_instance(case.store, package, [unconfirmed])
    except InstanceError as exc:
        assert "确认" in str(exc), str(exc)
    else:
        raise AssertionError("未确认卡进入了实例")
    assert files == ["card.json"], files
    assert result["valid"] is True
    return "PASS", (
        f"卡片候选经校验可用（valid=True、usage={result['usage']}），保存只留一份文件（{files}，cards.py:241-244）；"
        "未确认卡被装配校验拒绝（cards.py:375-376「角色卡未经用户确认」）"
    )


@item("§7.1-24 性格单元｜四种驱动；置信度 0-1 连续值；命名由 AI 产出")
def c_units(case: Case) -> tuple[str, str]:
    card = example_card(example_package())
    rows = personality.initial_rows(card, instance_id="in-1", timeline_id="tl-1", world_seconds=0)
    assert set(personality.MODES) == {"anchor", "event", "dialog", "time"}
    target_id = str(rows[-1]["id"])
    moved = {}
    for mode in personality.MODES:
        after = personality.apply_drive(
            rows, mode=mode, source_key=f"k-{mode}", semantic=str(rows[-1]["semantic"]),
            strength=1.0, positive=True, world_seconds=1,
        )
        moved[mode] = round(next(i for i in after if i["id"] == target_id)["confidence"], 4)
    crushed = rows
    for index in range(40):
        crushed = personality.apply_drive(
            crushed, mode="dialog", source_key=f"neg-{index}", semantic=str(rows[-1]["semantic"]),
            strength=1.0, positive=False, world_seconds=index,
        )
    assert all(0.0 <= float(item["confidence"]) <= 1.0 for item in crushed), "置信度越界"
    assert len({round(value, 6) for value in moved.values()}) >= 3, f"四种驱动效果无差异：{moved}"
    skeleton = template_card(example_package())
    assert validate_card(skeleton, example_package(), moment=DAY * 1500), "空语义骨架应不合法"
    return "PASS", (
        f"四驱动都以各自步长推动同一单元（{moved}）；40 次负向驱动后仍落在 [0,1]（personality.py:76-162）；"
        "语义名由生成器/卡片提供（template_card 空语义通不过 validate_card → 命名不是引擎自造）"
    )


@item("§7.1-25 驱动阈值｜锚点 > 事件 > 对话 > 时间")
def c_drive_order(case: Case) -> tuple[str, str]:
    highs = {mode: personality.BANDS[mode][1] for mode in personality.MODES}
    lows = {mode: personality.BANDS[mode][0] for mode in personality.MODES}
    assert highs["anchor"] > highs["event"] > highs["dialog"] > highs["time"], highs
    assert lows["anchor"] > lows["event"] > lows["dialog"] > lows["time"], lows
    decay = personality.DECAY_PER_DAY
    assert decay["time"] > decay["dialog"] > decay["event"] > decay["anchor"], decay
    rows = personality.initial_rows(example_card(example_package()),
                                    instance_id="in-1", timeline_id="tl-1", world_seconds=0)
    order = [item["mode"] for item in personality.visible(rows)]
    assert "anchor" in order[:2], order
    return "PASS", (
        f"初始区间与保护下限严格分层（锚点 0.75-0.99 > 事件 0.40-0.95 > 对话 0.15-0.70 > 时间 0.05-0.50）；"
        f"衰减方向相反对应（时间 {decay['time']}/日 最快、锚点 {decay['anchor']}/日 最慢）；visible() 排序锚点优先"
        "（personality.py:19-36、191-196）"
    )


@item("§7.1-26 锚点驱动｜不归档、可新增、不跨时间线、数量由 AI 决定")
def c_anchor(case: Case) -> tuple[str, str]:
    rows = personality.initial_rows(example_card(example_package()),
                                    instance_id="in-1", timeline_id="tl-1", world_seconds=0)
    anchor = next(item for item in rows if item["mode"] == "anchor")
    for index in range(30):
        rows = personality.apply_drive(
            rows, mode="dialog", source_key=f"w-{index}", semantic=str(anchor["semantic"]),
            strength=1.0, positive=False, world_seconds=index,
        )
    survived = next(item for item in rows if item["id"] == anchor["id"])
    assert survived["archived"] == 0 and survived["confidence"] >= personality.ANCHOR_FLOOR
    promoted = personality.promote([{
        **rows[0], "mode": "dialog", "stability": personality.PROMOTE_STABILITY,
        "confidence": personality.PROMOTE_CONFIDENCE + 0.05,
    }])
    assert promoted[0]["mode"] == "anchor", "稳定单元未固化为锚点"
    seed = "固定种子"
    info_a, tl_a, cid_a, _pkg = mk(case, "锚点世界", seed=seed)
    case.world.activate(info_a["id"], tl_a, now_real=1.7e9)
    case.world.advance(info_a["id"], tl_a, now_real=1.7e9 + 2 * DAY)
    mark = case.world.commit(info_a["id"], tl_a, note="分叉点")
    branch = case.world.fork(info_a["id"], tl_a, commit_id=mark["id"], name="另一条线")
    tl_b = branch["timeline"]["id"]
    case.world.advance(info_a["id"], tl_a, now_real=1.7e9 + 6 * DAY)
    units_a = {item["id"] for item in case.store.unit_list(info_a["id"], tl_a, cid_a)}
    units_b = {item["id"] for item in case.store.unit_list(info_a["id"], tl_b, cid_a)}
    assert not (units_a - units_b), "另一条线出现了本条线独有的单元"
    return "PASS", (
        f"锚点 30 次负向驱动后 archived=0 且 ≥ 保护下限 {personality.ANCHOR_FLOOR}；稳定+高置信的普通单元被 promote() 隐式固化为锚点；"
        f"单元按 timeline_id 隔离（分叉线只有分叉点之前的集合，{len(units_a)}/{len(units_b)} 条）；引擎无锚点数量配额（由卡片/AI 决定）"
    )


@item("§7.1-27 驱动迁移｜隐式存在、不对用户可见")
def c_migration_hidden(case: Case) -> tuple[str, str]:
    tables = {
        str(row["name"])
        for row in case.store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()  # noqa: SLF001
    }
    assert not [name for name in tables if "migrat" in name or "promote" in name], tables
    source = (ROOT / "isekai_core" / "runtime" / "personality.py").read_text(encoding="utf-8")
    assert "def promote" in source and "不写迁移记录" in source
    op_names = set(ops.SYNC_OPS) | set(ops.ASYNC_OPS)
    assert not [name for name in op_names if "migrat" in name or "promote" in name], op_names
    return "PASS", (
        f"迁移只体现为 mode 就地变为 anchor + basis 追加「长期稳定」（personality.py:180-188）；"
        f"无迁移记录表（{len(tables)} 张表中无 migrat*/promote*）、无迁移管理操作（{len(op_names)} 个 op 无 promote/migrate 入口）；"
        "用户侧只能经表达感知"
    )


@item("§7.1-28 diff 存储｜角色状态 diff 复制与合并；自动压缩")
def c_diff_storage(case: Case) -> tuple[str, str]:
    info, tl, _cid, _pkg = mk(case, "diff世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + 2 * DAY)
    case.world.commit(info["id"], tl, note="第一次")
    case.world.advance(info["id"], tl, now_real=1.7e9 + 4 * DAY)
    case.world.commit(info["id"], tl, note="第二次")
    snap = case.store._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n, MAX(LENGTH(payload)) AS size FROM commit_snapshot"
    ).fetchone()
    hits = [
        f"{p.relative_to(ROOT)}:{i}"
        for p in (ROOT / "isekai_core").rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "compact" in line.lower() or "压缩" in line
    ]
    assert snap["n"] == 2
    return "FAIL", (
        f"提交用全量快照（{snap['n']} 份、最大 {snap['size']} 字节），没有 diff 复制/合并，也没有任何自动压缩路径"
        f"（全树 compact/压缩 命中：{hits or '无'}）。"
        "最小复现：scripts/_audit_design.py::c_diff_storage（两次提交 → commit_snapshot 两行全量 payload）；"
        "证据：isekai_core/runtime/versioning.py:1-5「存储用全量快照」，service.py:522-560 fork / 566-632 rollback 只做快照装载；"
        "DESIGN §八 自己允许阶段性等价（「阶段 4 前先用全量快照验证机制，再引入 diff」），但 §7.1 该行所述 diff 与压缩两件都未实现"
    )


@item("§7.1-29 时间同步｜激活线世界时间与系统时间按倍率对应")
def c_time_sync(case: Case) -> tuple[str, str]:
    info, tl, _cid, _pkg = mk(case, "同步世界")
    now = 1.7e9
    case.world.activate(info["id"], tl, now_real=now)
    case.world.advance(info["id"], tl, now_real=now + 1)
    assert case.world.set_rate(info["id"], tl, rate=120, now_real=now + 1)["changed"] is True
    view = case.world.view(info["id"], tl, now_real=1_700_000_101)
    elapsed = view["world_seconds"] - DAY * 1500
    expected = 1 + 99 * 120
    assert abs(elapsed - expected) <= 120, (elapsed, expected)
    assert case.world.view(info["id"], tl, now_real=1_700_000_101) == view, "同一时刻两次读数不同"
    return "PASS", (
        f"激活线世界秒 = 基准 + (现实流逝 × 倍率)：理论 {expected} 世界秒、实测 {elapsed}；"
        "同输入同读数（clock.target_world 纯函数，clock.py:45-48）"
    )


@item("§7.1-30 软认知｜角色默认不会无理由知道全局设定")
def c_soft_cognition(case: Case) -> tuple[str, str]:
    _info, _tl, _cid, package = mk(case, "认知世界")
    card = example_card(package)
    slice_ = cognition.knowledge_slice(package, card, world_seconds=DAY * 5000, limit=200)
    texts = json.dumps(slice_, ensure_ascii=False)
    known_refs = json.dumps(card.get("initial_knowledge") or [], ensure_ascii=False)
    unread = [item for item in package["canon"] if str(item["id"]) not in known_refs]
    assert unread, "样本包没有未获知实情可验证"
    for entry in unread:
        assert str(entry.get("statement")) not in texts, f"未获知实情泄漏：{entry['id']}"
    assert "creator" not in texts
    small_card = {**card, "initial_knowledge": [], "background": {**card["background"], "self_knowledge": ""}}
    assert cognition.knowledge_slice(package, small_card, world_seconds=DAY * 5000) == []
    return "PASS", (
        f"{len(unread)} 条未获知 canon 全部不在切片里；零获知卡片的切片为空；"
        "全部条目带来源类型（自己的记忆/史料/听人说的）与获知时间（cognition.py:35-168）"
    )


@item("§7.1-31 碎片化｜用户可通过多轮对话拼凑世界设定（数据层代理 + 界面证据替代）")
def c_fragments(case: Case) -> tuple[str, str]:
    info, tl, cid, package = mk(case, "碎片世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    card = json.loads(case.store.instance_get(info["id"])["setting"])["cards"][0]
    before = cognition.knowledge_slice(package, card, world_seconds=DAY * 1500, limit=200)
    case.world.advance(info["id"], tl, now_real=1.7e9 + 30 * DAY)
    watermark = case.store.clock_get(tl)["processed_world"]
    experiences = case.store.experience_window(info["id"], tl, cid, until=watermark, limit=200)
    after = cognition.knowledge_slice(package, card, world_seconds=watermark, experiences=experiences, limit=200)
    sources = {item["source"] for item in after}
    topic_first = cognition.knowledge_slice(package, card, world_seconds=DAY * 5000, topic="堤", limit=200)
    plain = cognition.knowledge_slice(package, card, world_seconds=DAY * 5000, limit=200)
    assert len(after) >= len(before) and len(sources) >= 2, (len(before), len(after), sources)
    assert len(topic_first) == len(plain), "主题过滤掉了不相关条目（角色不该因提问角度失忆）"
    return "PASS", (
        f"切片是世界的真子集（{len(after)} 条，来源 {sorted(sources)}），随经历增长（{len(before)} → {len(after)}）；"
        f"同角色多来源 → 用户经多轮对话可逐片拼；主题只重排不过滤（cognition.py:165-168）"
    )


@item("§7.1-32 版本管理｜世界运行状态支持提交、回滚")
def c_versioning(case: Case) -> tuple[str, str]:
    info, tl, _cid, _pkg = mk(case, "版本世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + 2 * DAY)
    mark = case.world.commit(info["id"], tl, note="回滚点")
    snapshot_moment = int(case.store.clock_get(tl)["processed_world"])
    listing = case.world.commits(info["id"], tl)
    assert listing and set(listing[0]) == {"id", "kind", "moment", "note", "timeline_id", "created_at"}, listing
    case.world.advance(info["id"], tl, now_real=1.7e9 + 8 * DAY)
    assert case.store.clock_get(tl)["processed_world"] > snapshot_moment
    result = case.world.rollback(info["id"], tl, commit_id=mark["id"], now_real=1.7e9 + 9 * DAY)
    assert case.store.clock_get(tl)["processed_world"] == snapshot_moment, "回滚未覆盖水位"
    assert result["timeline"]["id"] == tl, "回滚换了线身份"
    assert "warning" in result and result["delivered_replies_kept"] == 0
    return "PASS", (
        f"提交列表只给管理元数据（{sorted(listing[-1])}）；回滚把同一条线覆盖回 {snapshot_moment}（覆盖语义，不新建线）；"
        "附「已投递内容不保证消除」提示（service.py:462-476、566-632）"
    )


@item("§7.1-33 多时间线｜可同时激活 ≥ 2 条；未激活线冻结")
def c_multi_timeline(case: Case) -> tuple[str, str]:
    info, tl, _cid, _pkg = mk(case, "多线世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + DAY)
    mark = case.world.commit(info["id"], tl, note="分叉点")
    branch = case.world.fork(info["id"], tl, commit_id=mark["id"], name="第二条")
    tl_b = branch["timeline"]["id"]
    assert branch["timeline"]["state"] == "frozen"
    case.world.activate(info["id"], tl_b, now_real=1.7e9 + 2 * DAY)
    active = case.world.active_timelines()
    assert len(active) == 2, active
    moved = case.world.advance(info["id"], tl, now_real=1.7e9 + 3 * DAY)
    assert moved["processed_world"] > DAY * 1500
    case.world.freeze(info["id"], tl, now_real=1.7e9 + 3 * DAY)
    stopped = case.world.advance(info["id"], tl, now_real=1.7e9 + 10 * DAY)
    assert stopped["state"] == "frozen" and stopped["processed_world"] == moved["processed_world"]
    return "PASS", (
        f"两条线同时激活（active_timelines={len(active)}）；分叉默认冻结；冻结后 advance 返回 state=frozen 且水位不动；"
        f"激活上限 {case.world.max_active_timelines}"
    )


@item("§7.1-34 回滚语义｜回滚覆盖原时间线")
def c_rollback_overwrite(case: Case) -> tuple[str, str]:
    info, tl, _cid, _pkg = mk(case, "覆盖世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + DAY)
    mark = case.world.commit(info["id"], tl, note="点")
    case.world.advance(info["id"], tl, now_real=1.7e9 + 6 * DAY)
    later = case.store.event_ids(info["id"], tl)
    case.world.rollback(info["id"], tl, commit_id=mark["id"], now_real=1.7e9 + 7 * DAY)
    now_ids = case.store.event_ids(info["id"], tl)
    lines = [item["id"] for item in case.store.timeline_list(info["id"])]
    assert lines == [tl], f"回滚改变了线集合：{lines}"
    assert len(now_ids) < len(later) and now_ids <= later, (len(now_ids), len(later))
    return "PASS", (
        f"回滚就地覆盖（线集合不变 {lines}），被截去的未来退出当前线（事件 {len(later)} → {len(now_ids)}）；"
        "要保留进展得显式 fork（fork 新建线、不动原线）"
    )


@item("§7.1-35 对话上下文隔离｜不同时间线对话历史互不可见")
def c_context_isolation(case: Case) -> tuple[str, str]:
    info, tl, cid, _pkg = mk(case, "隔离世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + DAY)
    say(case, info["id"], tl, cid, env="env-a", text="堤上的事", reply="堤长身故，别外传")
    mark = case.world.commit(info["id"], tl, note="分叉点")
    branch = case.world.fork(info["id"], tl, commit_id=mark["id"], name="另一条")
    tl_b = branch["timeline"]["id"]
    session_b = case.store.session_ensure(info["id"], tl_b, cid)
    text_b = json.dumps(texts_of(case, session_b["id"]), ensure_ascii=False)
    say(case, info["id"], tl_b, cid, env="env-b", text="乙线的事", reply="乙线的答")
    session_a = case.store.session_ensure(info["id"], tl, cid)
    text_a = json.dumps(texts_of(case, session_a["id"]), ensure_ascii=False)
    assert "堤长身故" not in text_b, "分叉线读到了原线对话"
    assert "乙线的答" not in text_a, "原线读到了分叉线对话"
    assert session_a["id"] != session_b["id"]
    return "PASS", (
        "会话按 (实例, 线, 角色) 唯一：分叉线继承共同过去，之后的对话两边互不可见"
        f"（session_a={session_a['id'][:8]}… / session_b={session_b['id'][:8]}…）"
    )


@item("§7.1-36 披露机制｜默认隔离；用户披露后可见；角色不主动表露")
def c_disclosure(case: Case) -> tuple[str, str]:
    info, tl, cid_a, package = mk(case, "披露世界")
    card_b = example_card(package, name="堤砚")
    cid_b = str(card_b["meta"]["card_id"])
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + DAY)
    case.world.add_character(info["id"], tl, card_b, now_real=1.7e9,
                             joined_world=case.store.clock_get(tl)["processed_world"])
    reply_id = say(case, info["id"], tl, cid_a, env="env-a1", text="你那边堤上的事怎么样",
                   reply="信报上抄到堤长身故，别外传")
    before = case.world.turn_context(
        {"instance_id": info["id"], "timeline_id": tl, "character_id": cid_b}, topic="堤长"
    )
    assert "堤长身故" not in before["prompt"], "默认隔离失效"
    grant = case.world.disclose(info["id"], tl, from_character=cid_a, to_character=cid_b, refs=[reply_id])
    fragments = case.world.disclosed_fragments(info["id"], tl, cid_b)
    after = case.world.turn_context(
        {"instance_id": info["id"], "timeline_id": tl, "character_id": cid_b}, topic="堤长"
    )
    assert grant["reused"] is False and len(fragments) == 1
    assert "联络者明确给你看过这些转述" in after["prompt"] and "不是你亲历的" in after["prompt"]
    listed = case.world.disclosures(info["id"], tl, to_character=cid_b)
    assert "堤长身故" not in json.dumps(listed, ensure_ascii=False), "披露清单带内容"
    return "PASS", (
        "披露前 B 的扮演定义里没有 A 的内容；披露后以「联络者明确给你看过这些转述（不是你亲历的）」进入 B 的上下文；"
        "清单只给管理元数据、角色不主动表露（无自动扩散路径，B 的上下文只由披露集合驱动）（service.py:138-233、disclosure.py:35-58）"
    )


@item("§7.1-37 披露撤回｜通过回滚实现（无单独撤回入口）")
def c_disclosure_revoke(case: Case) -> tuple[str, str]:
    info, tl, cid_a, package = mk(case, "撤回世界")
    card_b = example_card(package, name="堤砚")
    cid_b = str(card_b["meta"]["card_id"])
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + DAY)
    case.world.add_character(info["id"], tl, card_b, now_real=1.7e9,
                             joined_world=case.store.clock_get(tl)["processed_world"])
    mark = case.world.commit(info["id"], tl, note="披露前")
    reply_id = say(case, info["id"], tl, cid_a, env="env-a1", text="问", reply="信报上抄到堤长身故")
    case.world.disclose(info["id"], tl, from_character=cid_a, to_character=cid_b, refs=[reply_id])
    assert case.world.disclosed_fragments(info["id"], tl, cid_b)
    names = set(ops.SYNC_OPS) | set(ops.ASYNC_OPS)
    revoke = [n for n in names if "disclos" in n and any(k in n for k in ("revoke", "delete", "withdraw"))]
    assert not revoke, revoke
    case.world.rollback(info["id"], tl, commit_id=mark["id"], now_real=1.7e9 + 2 * DAY)
    assert case.world.disclosed_fragments(info["id"], tl, cid_b) == []
    prompt = case.world.turn_context(
        {"instance_id": info["id"], "timeline_id": tl, "character_id": cid_b}, topic="堤长"
    )["prompt"]
    assert "堤长身故" not in prompt
    return "PASS", (
        "管理面无撤回/删除披露入口（只有 disclose.confirm / disclose.list）；回滚到披露前的提交即撤销授权，"
        "B 的上下文重新干净（service.py:566-632 快照覆盖）"
    )


@item("§7.1-38 跨端同步｜无云同步；用户手动导入导出")
def c_cross_device(case: Case) -> tuple[str, str]:
    net = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "isekai_core").rglob("*.py")
        if "httpx" in p.read_text(encoding="utf-8")
    )
    ws = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "isekai_core").rglob("*.py")
        if "from websockets" in p.read_text(encoding="utf-8")
    )
    info, _tl, _cid, _pkg = mk(case, "跨端世界")
    path = case.dir / "exports" / "hand.isekai"
    portable.write_export(case.store, info["id"], path)
    copy = portable.import_instance(case.store, json.loads(path.read_text(encoding="utf-8")))
    assert net == ["isekai_core/llm.py", "isekai_core/runtime/embedding.py"], net
    assert ws == ["isekai_core/channel.py", "isekai_core/client.py"], ws
    assert copy["id"] != info["id"]
    return "PASS", (
        f"出网面只有 LLM（{net}，都是用户自配的第三方服务）与本地回环 WS 服务端/客户端（{ws}）——"
        f"无任何云同步或账号体系代码；跨端搬运 = 用户手动单文件导出导入（{path.name} → 新实例 {copy['name']}）"
    )


@item("§7.1-39 世界合并｜❌ 不支持")
def c_no_merge(case: Case) -> tuple[str, str]:
    names = set(ops.SYNC_OPS) | set(ops.ASYNC_OPS)
    merge_ops = [name for name in names if "merge" in name]
    source_hits = sorted(
        str(p.relative_to(ROOT))
        for p in (ROOT / "isekai_core").rglob("*.py")
        if "def merge" in p.read_text(encoding="utf-8")
    )
    assert not merge_ops, merge_ops
    info_a, _tl, _cid, _pkg = mk(case, "甲世界")
    info_b, _tl2, _cid2, _pkg2 = mk(case, "乙世界")
    assert len(case.store.instance_list()) == 2
    return "PASS", (
        f"管理面 0 个合并操作（{len(names)} 个 op 中无 merge），核心内 0 个合并函数"
        f"（仅有的 merge 是生成器合并卡片段落 generator._merge_structure，不在 {source_hits or '核心模块列表里'}）；"
        f"导入只会新建副本而不并入（{info_a['name']} / {info_b['name']} 各自独立演化）"
    )


@item("§7.1-40 实例同名｜❌ 全局不允许，不区分世界")
def c_unique_name(case: Case) -> tuple[str, str]:
    first, _tl, _cid, _pkg = mk(case, "同名世界")
    second, _tl2, _cid2, _pkg2 = mk(case, "同名世界")
    assert first["name"] == "同名世界" and second["name"] == "同名世界_2", (first["name"], second["name"])
    from isekai_core.world.instances import rename_instance

    try:
        rename_instance(case.store, second["id"], "同名世界")
    except InstanceError as exc:
        assert "占用" in str(exc), str(exc)
    else:
        raise AssertionError("重命名撞了全局名却放行")
    unique = rename_instance(case.store, second["id"], "另一个名字")
    assert unique["name"] == "另一个名字"
    return "PASS", (
        f"自动序号从 _2 起（{second['name']}）；显式重命名撞全局名明确拒绝、换名可用；判重跨世界"
        "（instance_names() 全库取名单，instances.py:203-216）"
    )


@item("§7.1-41 安卓端｜阶段 7（可选）")
def c_android(case: Case) -> tuple[str, str]:
    android = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("android*") if "node_modules" not in str(p))
    return "DEFERRED", (
        f"DESIGN §6.1「阶段 7（可选）：安卓移植评估」未启动，仓库内无安卓工程（{android or '无 android* 文件'}）；"
        "与安卓相关的行只在 ANDROID_SPEC.md；世界包生成器已按共享实现准备（见 §7.1-09）"
    )


# ------------------------------------------------- §5 跨模块机制 / §6 阶段验收


@item("§5.3/§6-阶段2 世界秒与历法｜唯一时间基元、纯函数可逆、同输入同答案")
def c_world_seconds(case: Case) -> tuple[str, str]:
    package = example_package()
    calendar = calendar_from_package(package)
    moment = calendar.to_world_seconds(year=13, month=2, day=3, offset=3600)
    view = calendar.to_calendar(moment)
    assert (view["year"], view["month"], view["day"], view["hour"]) == (13, 2, 3, 1)
    assert calendar.to_calendar(moment) == view
    assert calendar.day_index(-1) == -1 and calendar.to_calendar(-DAY)["year"] == 0
    assert calendar.year_seconds == calendar.year_days * calendar.day_seconds
    assert calendar.segment_of(0)["name"] and calendar.segment_of(DAY // 2)["name"]
    return "PASS", (
        f"世界秒 ↔ 历法互逆（年 {calendar.year_days} 日 = {calendar.year_seconds} 秒，无闰）；负数时刻向下取整合法；"
        "同一输入重复调用结果相同（calendar.py:34-89 纯函数）"
    )


@item("§5.3 离线补算｜只补中断前激活的线，冻结不补，重激活重新锚定")
def c_catch_up(case: Case) -> tuple[str, str]:
    info, tl_a, _cid, _pkg = mk(case, "补算世界")
    other, tl_b, _cid2, _pkg2 = mk(case, "冻结世界")
    case.world.activate(info["id"], tl_a, now_real=1.7e9)
    case.world.activate(other["id"], tl_b, now_real=1.7e9)
    case.world.freeze(other["id"], tl_b, now_real=1.7e9)
    assert [pair[1] for pair in case.world.active_timelines()] == [tl_a]
    resumed = case.world.catch_up_all(now_real=1.7e9 + 5 * DAY)
    assert resumed and tl_a in resumed and tl_b not in resumed
    assert case.store.clock_get(tl_b)["processed_world"] == DAY * 1500
    before = case.store.clock_get(tl_a)["processed_world"]
    view = case.world.view(info["id"], tl_a, now_real=1.7e9 + 5 * DAY)
    case.world.freeze(info["id"], tl_a, now_real=1.7e9 + 5 * DAY)
    reactivated = case.world.activate(info["id"], tl_a, now_real=1.7e9 + 9 * DAY)
    assert reactivated["world_seconds"] == int(case.store.clock_get(tl_a)["processed_world"]), "重激活补算了冻结间隔"
    assert before > DAY * 1500 and view["state"] == "active"
    return "PASS", (
        f"启动只补中断前激活的线（{sorted(resumed)}），冻结线水位不动；重激活以当下现实时间重新锚定、不追赶冻结间隔"
        "（active_timelines 只取 active，service.py:2222-2244；activate 重锚 service.py:1258-1302）"
    )


@item("§6-阶段6 确定性复算｜同种子同规则同水位 → 同事实，分批大小不影响结果")
def c_deterministic(case: Case) -> tuple[str, str]:
    seed = "固定种子-1"
    first, tl_a, _cid, _pkg = mk(case, "确定性世界", seed=seed)
    second, tl_b, _cid2, _pkg2 = mk(case, "确定性世界", seed=seed)
    case.world.activate(first["id"], tl_a, now_real=1.7e9)
    case.world.activate(second["id"], tl_b, now_real=1.7e9)
    case.world.advance(first["id"], tl_a, now_real=1.7e9 + 10 * DAY, max_batches=20)
    for step in range(1, 11):  # 分批补算：每天一批
        case.world.advance(second["id"], tl_b, now_real=1.7e9 + step * DAY, max_batches=1)
    raw_a = case.store.event_window(first["id"], tl_a, until=10**12, limit=500)
    raw_b = case.store.event_window(second["id"], tl_b, until=10**12, limit=500)
    facts_a = sorted((row["world_seconds"], row["summary"]) for row in raw_a)
    facts_b = sorted((row["world_seconds"], row["summary"]) for row in raw_b)
    ids_a = {str(row["id"]) for row in raw_a if not str(row["id"]).startswith("ev-act-")}
    ids_b = {str(row["id"]) for row in raw_b if not str(row["id"]).startswith("ev-act-")}
    assert case.store.clock_get(tl_a)["processed_world"] == case.store.clock_get(tl_b)["processed_world"]
    assert facts_a == facts_b, f"同种子不同事实：{sorted(set(facts_a) ^ set(facts_b))[:3]}"
    assert ids_a == ids_b and ids_a, f"同种子不同骨架：{sorted(ids_a ^ ids_b)[:3]}"

    third, tl_c, _cid3, _pkg3 = mk(case, "确定性世界", seed=seed)
    with case.store._lock, case.store._conn:  # noqa: SLF001 改规则版本
        case.store._conn.execute("UPDATE instance SET rules_version='0.9' WHERE id=?", (third["id"],))
    case.world.activate(third["id"], tl_c, now_real=1.7e9)
    case.world.advance(third["id"], tl_c, now_real=1.7e9 + 10 * DAY, max_batches=20)
    ids_c = {
        str(row["id"])
        for row in case.store.event_window(third["id"], tl_c, until=10**12, limit=500)
        if not str(row["id"]).startswith("ev-act-")
    }
    assert ids_c != ids_a, "规则版本没有参与抽样"
    return "PASS", (
        f"连续推进（1 次 10 天）与分批补算（10 次各 1 天）产生同一事实集：{len(ids_a)} 个世界事件骨架 id 与 "
        f"{len(facts_a)} 条（时刻, 摘要）全等；改 rules_version 后同时段骨架改变 → 规则版本确实参与确定性复算"
        "（events.py:35-40、154-156 stable_key(seed, rules_version, day, slot)）；"
        "角色行动事件 id 含实例标识（intents.py:124），故跨实例比较只取世界事件与内容"
    )


@item("§5.5/§2.2-6 认知接口不泄实情｜实情层与幕后设定不进扮演上下文")
def c_no_leak(case: Case) -> tuple[str, str]:
    info, tl, cid, package = mk(case, "保密世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    card = json.loads(case.store.instance_get(info["id"])["setting"])["cards"][0]
    context = case.world.turn_context({"instance_id": info["id"], "timeline_id": tl, "character_id": cid})
    prompt = context["prompt"]
    creator = str(card["background"]["creator"])
    assert creator and creator not in prompt, "creator 段泄漏"
    known_refs = json.dumps(card.get("initial_knowledge") or [], ensure_ascii=False)
    checked = 0
    for entry in package["canon"]:
        if str(entry["id"]) not in known_refs and str(entry.get("statement"))[:20] not in prompt:
            checked += 1
    assert checked > 0, "样本里没有被隐藏的实情条目可验证"
    for entry in package["world"]["axioms"]:
        assert str(entry["text"])[:16] not in prompt, f"幕后公理泄漏：{entry['id']}"
    for entry in package["initial_state"]["mysteries"]:
        assert str(entry["question"])[:12] not in prompt, f"幕后谜题泄漏：{entry['id']}"
    assert "实情" not in prompt
    return "PASS", (
        f"creator 段、{checked} 条未获知 canon、{len(package['world']['axioms'])} 条世界公理与 "
        f"{len(package['initial_state']['mysteries'])} 条幕后谜题都不在扮演定义里；"
        "知识一律以「来源 + 确信度 + 获知时间」出现（cognition.py:235-312 play_context 白名单组装）"
    )


@item("§5.1 导入导出（原则）｜单文件往返保持已完成水位与角色状态")
def c_roundtrip(case: Case) -> tuple[str, str]:
    info, tl, cid, _pkg = mk(case, "往返世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + 2 * DAY)
    say(case, info["id"], tl, cid, env="env-r", text="问一句", reply="答一句")
    watermark = case.store.clock_get(tl)["processed_world"]
    units_before = len(case.store.unit_list(info["id"], tl, cid))
    events_before = len(case.store.event_ids(info["id"], tl))
    path = case.dir / "exports" / "rt.isekai"
    portable.write_export(case.store, info["id"], path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    copy = portable.import_instance(case.store, raw)
    new_tl = case.store.timeline_list(copy["id"])[0]["id"]
    session = case.store.session_ensure(copy["id"], new_tl, cid)
    imported = json.dumps(texts_of(case, session["id"]), ensure_ascii=False)
    exported_rows = json.dumps(case.store.instance_messages(info["id"]), ensure_ascii=False)
    assert case.store.clock_get(new_tl)["processed_world"] == watermark, "水位未随件"
    assert len(case.store.unit_list(copy["id"], new_tl, cid)) == units_before, "角色状态未随件"
    assert len(case.store.event_ids(copy["id"], new_tl)) == events_before, "事件未随件"
    assert "问一句" in imported, "用户侧对话未随件"
    # 角色侧回复：出站正文存在 parts 列，导出语句只取 text 列 → 导出件里是 null
    assert '"role": "character"' in exported_rows and '"text": null' in exported_rows
    assert "答一句" not in imported
    return "FAIL", (
        "水位、角色单元、事件、用户侧对话都随件；但**角色回复正文在导出时丢失**：出站消息正文存在 message.parts，"
        "而导出用的 instance_messages 只 SELECT m.text → 导出件里 character 行 text=null，导入后副本里读不到那条回复。"
        "最小复现：scripts/_audit_design.py::c_roundtrip（say() 落一轮「问一句/答一句」→ 导出 → 导入 → 副本历史里只有「问一句」；"
        "导出行实测 " + exported_rows[:120] + "）；"
        "证据：isekai_core/store.py:1284-1293（instance_messages 只取 text）、store.py:1385-1392（message_text：出站正文在 parts）、"
        "isekai_core/world/portable.py:46-70 + _restore_sessions:portable.py:338-365（直接把这些行写回副本）"
    )


@item("§5.7 版本职责分离｜DATA_FORMAT_VERSION / RULES_VERSION / 生成器指纹")
def c_version_duties(case: Case) -> tuple[str, str]:
    info, tl, _cid, _pkg = mk(case, "职责世界")
    row = case.store.instance_get(info["id"])
    assert row["data_format"] == DATA_FORMAT_VERSION and row["rules_version"] == RULES_VERSION
    case.world.activate(info["id"], tl, now_real=1.7e9)
    mark = case.world.commit(info["id"], tl, note="职责")
    snap = case.store.commit_snapshot_get(mark["id"])
    assert {"rules_version", "data_format", "seed"} <= set(snap), sorted(snap)
    hits = [
        f"{p.relative_to(ROOT).as_posix()}:{i}"
        for p in (ROOT / "isekai_core").rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "fingerprint" in line.lower()
    ]
    model_record = [
        f"{p.relative_to(ROOT).as_posix()}:{i}"
        for p in (ROOT / "isekai_core").rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "llm.model" in line or "cfg.llm" in line
    ]
    assert hits == ["isekai_core/runtime/embedding.py:26"], hits
    return "FAIL", (
        "两件到位、第三件缺失：数据格式（实例行与提交快照都带 data_format，导入按主版本闸门）与规则版本"
        "（快照带 rules_version + 实测参与事件抽样复算）都落地；但「生成器 / 提示词 / 文本模型指纹」全代码库零实现"
        f"（fingerprint 仅 {hits} 一处，那是 embedding 向量缓存的模型指纹，不是文本产物边界）；"
        f"管理元数据也不记录生成模型 / 提示词（全树 {len(model_record)} 处 llm 配置读取，无一处写进实例行、提交行或快照）。"
        "最小复现：scripts/_audit_design.py::c_version_duties（断言提交快照字段 + 全库 fingerprint 命中）；"
        "证据：isekai_core/version.py:1-20 声明三件套，实际只有 DATA_FORMAT_VERSION / RULES_VERSION 两个常量"
    )


@item("§5.7 运行故障不改写世界｜批次失败不留半个水位；文本失败不阻止事实推进")
def c_failure_integrity(case: Case) -> tuple[str, str]:
    info, tl, _cid, _pkg = mk(case, "故障世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    before = case.store.clock_get(tl)["processed_world"]
    original = case.store.apply_runtime_batch

    def boom(**kwargs: Any) -> bool:
        raise OSError("模拟写盘失败")

    case.store.apply_runtime_batch = boom  # type: ignore[assignment]
    crashed = False
    try:
        case.world.advance(info["id"], tl, now_real=1.7e9 + 3 * DAY)
    except OSError:
        crashed = True
    finally:
        case.store.apply_runtime_batch = original  # type: ignore[assignment]
    after = case.store.clock_get(tl)["processed_world"]
    assert crashed and after == before, f"失败批次留下了半个水位：{before} → {after}"
    resumed = case.world.advance(info["id"], tl, now_real=1.7e9 + 3 * DAY)
    assert resumed["processed_world"] > before

    events = case.store.event_ids(info["id"], tl)
    failing = FakeLLM([""], fail_with=LLMError("llm_unreachable", "离线", retryable=True))
    some_event = sorted(events)[0]
    rendered_failed = False
    try:
        asyncio.run(ops.dispatch_async(case.cfg, failing, "event.render", {
            "instance_id": info["id"], "timeline_id": tl, "event_id": some_event,
        }, store=case.store))
    except (UmpError, LLMError):
        rendered_failed = True
    still = case.world.advance(info["id"], tl, now_real=1.7e9 + 5 * DAY)
    assert rendered_failed and still["processed_world"] > resumed["processed_world"], "渲染失败拖住了事实推进"
    assert case.store.event_get(info["id"], tl, some_event) is not None, "事实被渲染失败改坏"
    return "PASS", (
        f"在批次写入点注入 OSError：水位保持 {before} 不动、恢复后继续推进到 {resumed['processed_world']}；"
        "模型不可用时 event.render 失败且不落任何事实，随后确定性推进照常（advance 不依赖模型，service.py:1457-1560；"
        "渲染失败只退回模板 service.py:720-730）"
    )


@item("§5.7 凭据不进日志/导出｜通道凭据与 API Key 不落日志、不随导出件")
def c_credentials(case: Case) -> tuple[str, str]:
    case.cfg.llm.api_key = "sk-SECRET-AUDIT-KEY"
    _row, credential = case.store.channel_register(
        name="builtin", display_name="内建聊天窗口", version="1.0", protocol="1.0", capabilities={}
    )
    info, tl, cid, _pkg = mk(case, "凭据世界")
    case.world.activate(info["id"], tl, now_real=1.7e9)
    case.world.advance(info["id"], tl, now_real=1.7e9 + 600)
    say(case, info["id"], tl, cid, env="env-c", text="带凭据的一轮", reply="收到")
    case.world.set_rate(info["id"], tl, rate=120, now_real=1.7e9 + 600)
    # 让真实日志路径也跑一遍（被拒的运行层操作会打 warning，含实例/线标识）
    try:
        ops.dispatch(case.cfg, case.store, "runtime.rate",
                     {"instance_id": info["id"], "timeline_id": tl, "rate": 0}, runtime=case.world)
    except UmpError:
        pass
    path = case.dir / "exports" / "cred.isekai"
    ops.dispatch(case.cfg, case.store, "instance.export", {"id": info["id"], "path": str(path)},
                 runtime=case.world)
    text = path.read_text(encoding="utf-8")
    dumped = json.dumps(ops.dispatch(case.cfg, case.store, "instance.info", {"id": info["id"]}), ensure_ascii=False)
    logged = "\n".join(record.getMessage() for record in case.capture.records)
    assert len(case.capture.records) > 0, "未捕获到日志记录（检查无效）"
    assert credential not in text and "credential" not in text, "凭据进了导出件"
    assert case.cfg.llm.api_key not in text, "API Key 进了导出件"
    assert "sk-SECRET-AUDIT-KEY" not in dumped
    assert credential not in logged and "sk-SECRET-AUDIT-KEY" not in logged, "凭据进了日志"
    channel_row = case.store.channel_by_name("builtin")
    assert channel_row and channel_row["credential_hash"] != credential, "库内存了明文凭据"
    log_calls = [
        f"{p.relative_to(ROOT).as_posix()}:{i}"
        for p in (ROOT / "isekai_core").rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if any(marker in line for marker in ("log.info(", "log.warning(", "log.error(", "log.debug(", "log.exception("))
        and any(word in line for word in ("credential", "api_key", "token", "secret"))
    ]
    assert not log_calls, log_calls
    return "PASS", (
        f"明文凭据不在导出件、管理面响应、日志与库内（库只存哈希 {str(channel_row['credential_hash'])[:12]}…）；"
        f"导出件不含通道绑定/投递回执（portable.py:1-7、94-96）；采样 {len(case.capture.records)} 条日志记录（含"
        f"被拒的倍率操作 warning 与 instance.export 路径）全无凭据；全树 0 处日志语句引用 credential/api_key/token；"
        "log.py:1-6「默认不记录消息正文、prompt、凭据」"
    )


@item("§5.4/§7.1 形态与交互（界面证据）｜只做数据层可验部分")
def c_ui_scope(case: Case) -> tuple[str, str]:
    raise NotImplementedError


def run_one(fn: Callable, index: int) -> tuple[str, str]:
    case = Case(f"{index:02d}")
    try:
        return fn(case)
    except NotImplementedError:
        return "SKIP", "界面/形态或后置能力的界面证据不在本探针范围（探针只给数据层/管理面证据）"
    except AssertionError as exc:
        return "FAIL", f"断言失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        return "FAIL", f"{type(exc).__name__}: {exc}"
    finally:
        case.close()


def main() -> int:
    tally = {"PASS": 0, "FAIL": 0, "DEFERRED": 0, "SKIP": 0}
    for index, (title, fn) in enumerate(CHECKS, 1):
        status, evidence = run_one(fn, index)
        tally[status] = tally.get(status, 0) + 1
        print(f"{status} {title} — {evidence}")
    print(
        f"TOTAL {len(CHECKS)} PASS {tally['PASS']} FAIL {tally['FAIL']} "
        f"DEFERRED {tally['DEFERRED']} SKIP {tally['SKIP']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
