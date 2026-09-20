"""行为探针：WORLD_SETTING_SPEC 附录 C（行为验收）逐条实测。

用法：
    cd /d/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_audit_world_setting.py

只读探针：数据库全部落在 tempfile 临时目录（不触碰 data/isekai.db），不启动核心 / 壳，
不联网（LLM 一律 FakeLLM 或「未配置 Key」的真实客户端），不修改任何项目文件。
每条输出一行 `PASS/FAIL/DEFERRED <摘要> — <证据>`，末尾汇总 `TOTAL n PASS p FAIL f DEFERRED d`。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM, LLMClient  # noqa: E402
from isekai_core.runtime import cognition  # noqa: E402
from isekai_core.runtime.service import RuntimeService, RuntimeStateError  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.ump import Err, UmpError  # noqa: E402
from isekai_core.world import ops  # noqa: E402
from isekai_core.world.cards import validate_card  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.generator import generate_package, revise_package  # noqa: E402
from isekai_core.world.instances import InstanceError, create_instance  # noqa: E402
from isekai_core.world.ops import ASYNC_OPS, SYNC_OPS, dispatch, dispatch_async  # noqa: E402
from isekai_core.world.package import PackageError, load_package, save_package  # noqa: E402
from isekai_core.world.portable import build_container, import_instance, write_export  # noqa: E402
from isekai_core.world.validate import validate_package  # noqa: E402

ALL_OPS = sorted(set(SYNC_OPS) | set(ASYNC_OPS))
TABLES = (
    "timeline", "session", "memory", "knowledge", "disclosure", "character_join",
    "event", "claim", "life_plan", "unit", "commit_log", "commit_snapshot", "pending_event", "effect_state",
    "institution_state", "custom_state", "environment_state",
)


def clone(payload: Any) -> Any:
    return json.loads(json.dumps(payload, ensure_ascii=False))


def digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]


class Ctx:
    """一个临时根目录 + 一个新库（每个检查各自独立，互不污染）。"""

    def __init__(self, name: str) -> None:
        self.root = Path(tempfile.mkdtemp(prefix=f"audit-ws-{name}-"))
        self.cfg = load_config(self.root)
        self.cfg.paths.packages.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root / "data" / "isekai.db")
        self.store.ensure_schema()

    def service(self, **over: Any) -> RuntimeService:
        params: dict[str, Any] = {"autocommit_enabled": False}
        params.update(over)
        return RuntimeService(self.store, **params)

    def close(self) -> None:
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)


def counts(store: Store, instance_id: str) -> dict[str, int]:
    """实例级行数统计（只读 SQL；message/thread 走 session 子查询）。"""
    def one(sql: str, *args: Any) -> int:
        row = store._conn.execute(sql, args).fetchone()
        return int(row[0] if row else 0)

    sub = "SELECT id FROM session WHERE instance_id=?"
    out = {"instance": one("SELECT COUNT(*) FROM instance")}
    for table in TABLES:
        out[table] = one(f"SELECT COUNT(*) FROM {table} WHERE instance_id=?", instance_id)
    out["rate_command"] = one(
        "SELECT COUNT(*) FROM rate_command WHERE timeline_id IN (SELECT id FROM timeline WHERE instance_id=?)",
        instance_id,
    )
    out["message"] = one(f"SELECT COUNT(*) FROM message WHERE session_id IN ({sub})", instance_id)
    out["thread"] = one(f"SELECT COUNT(*) FROM thread WHERE session_id IN ({sub})", instance_id)
    out["timeline_clock"] = one(
        "SELECT COUNT(*) FROM timeline_clock WHERE timeline_id IN (SELECT id FROM timeline WHERE instance_id=?)",
        instance_id,
    )
    out["delivery"] = one(
        f"SELECT COUNT(*) FROM delivery WHERE msg_seq IN (SELECT seq FROM message WHERE session_id IN ({sub}))",
        instance_id,
    )
    out["memory_citation"] = one(
        "SELECT COUNT(*) FROM memory_citation WHERE timeline_id IN (SELECT id FROM timeline WHERE instance_id=?)",
        instance_id,
    )
    return out


def mk(ctx: Ctx, world: RuntimeService, *, moment: int = DAY * 1500, cards: int = 1) -> tuple[dict, str, str]:
    """建一个真实实例（示例包 + 示例卡，同一真源）+ 补齐运行层。"""
    package = example_package(moment=moment)
    first = example_card(package)
    card_list = [first] + [example_card(package, name=f"配角{i}") for i in range(1, cards)]
    info = create_instance(ctx.store, package, card_list)
    timeline = ctx.store.timeline_list(info["id"])[0]
    world.ensure_instance(info["id"], now_real=time.time())
    return info, timeline["id"], str(first["meta"]["card_id"])


def say(store: Store, info: dict, timeline_id: str, character_id: str, *, env: str, text: str, reply: str) -> str:
    """固化一轮对话（不经 LLM）：返回回复的 message_id。"""
    session = store.session_ensure(info["id"], timeline_id, character_id)
    store.inbound_put(
        session_id=session["id"], channel_id="cli-dev", thread_id=f"t-{character_id}",
        env_id=env, text=text, binding_version=1,
    )
    outbound = store.outbound_put(
        session_id=session["id"], message_id=f"m-{env}", reply_to=env, covers=[env],
        batches=[[reply]], target_channel="cli-dev", target_thread=f"t-{character_id}",
        binding_version=1, binding_token="tok",
    )
    return outbound["message_id"]


def add_second(ctx: Ctx, world: RuntimeService, info: dict, timeline_id: str, *, name: str = "堤砚", **over: Any):
    """补入一名第二角色（锚定当前水位）。"""
    package = json.loads(ctx.store.instance_get(info["id"])["setting"])["world_package"]
    card = example_card(package, name=name)
    watermark = int(ctx.store.clock_get(timeline_id)["processed_world"])
    joined = world.add_character(
        info["id"], timeline_id, card, now_real=1.7e9, joined_world=over.pop("joined_world", watermark), **over
    )
    return card, str(card["meta"]["card_id"]), joined


def setting_hash(store: Store, instance_id: str) -> str:
    return digest(json.loads(store.instance_get(instance_id)["setting"]))


def blob(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------- C1

def c01_locked_and_source_decoupled(ctx: Ctx) -> tuple[str, str, str]:
    """C1 改 / 删源文件不影响既有实例；锁定设定无写入口；补卡只增不减。"""
    package = example_package()
    card = example_card(package)
    pkg_path = ctx.cfg.paths.packages / "world.json"
    card_path = ctx.cfg.paths.packages / "card.json"
    save_package(pkg_path, package)
    save_package(card_path, card)  # 卡片也是单文件 JSON
    info = create_instance(ctx.store, load_package(pkg_path), [json.loads(card_path.read_text(encoding="utf-8"))])
    before = setting_hash(ctx.store, info["id"])

    edited = clone(package)
    edited["calendar"]["day_seconds"] = 12345
    edited["calendar"]["months"][0]["days"] = 11
    edited["world"]["axioms"][0]["text"] = "被改写后的公理。"
    save_package(pkg_path, edited)
    card_edit = clone(card)
    card_edit["identity"]["name"] = "改过名字的人"
    card_edit["initial_units"][0]["semantic"] = "被改写后的单元"
    save_package(card_path, card_edit)
    after_edit = setting_hash(ctx.store, info["id"])

    os.remove(pkg_path)
    os.remove(card_path)
    after_delete = setting_hash(ctx.store, info["id"])
    setting = json.loads(ctx.store.instance_get(info["id"])["setting"])
    calendar_ok = setting["world_package"]["calendar"]["day_seconds"] == DAY
    card_ok = setting["cards"][0]["identity"]["name"] == card["identity"]["name"]

    writers = [
        op for op in ALL_OPS
        if re.search(r"(axiom|calendar|setting)", op) and re.search(r"\.(set|write|update|edit|patch|delete)$", op)
    ]
    world = ctx.service()
    timeline_id = ctx.store.timeline_list(info["id"])[0]["id"]
    world.ensure_instance(info["id"], now_real=time.time())
    cards_before = len(json.loads(ctx.store.instance_get(info["id"])["setting"])["cards"])
    card2, second_id, joined = add_second(ctx, world, info, timeline_id, name="堤砚")
    cards_after = len(json.loads(ctx.store.instance_get(info["id"])["setting"])["cards"])
    joins = len(ctx.store.character_join_list(info["id"], timeline_id))

    ok = (
        before == after_edit == after_delete and calendar_ok and card_ok
        and not writers and cards_before == cards_after == 1 and joins == 1
    )
    evidence = (
        f"改源包+改源卡后 setting 指纹 {before}→{after_edit}，删两个源文件后 {after_delete}；"
        f"锁定历法 day_seconds={setting['world_package']['calendar']['day_seconds']}（未被改成 12345）；"
        f"设定写入口 {writers or '无'}；补卡后 setting.cards={cards_after}（原 1）、成员资格={joins}"
    )
    return ("PASS" if ok else "FAIL"), "源文件改动 / 删除不追溯实例，锁定设定与既有角色卡不可改，补卡只扩充", evidence


# ---------------------------------------------------------------- C2

def c02_shared_validation(ctx: Ctx) -> tuple[str, str, str]:
    """C2 新建与修订共用校验；失败 / 取消不覆盖确认版本。"""
    from isekai_core.world import generator as gen

    shared = gen.validate_package is validate_package
    package = example_package()
    replies = [
        blob({key: package[key] for key in ("meta", "calendar", "world")}),
        blob({key: package[key] for key in ("sources", "canon", "narratives", "races", "entities")}),
        blob({
            key: package[key]
            for key in ("historiography", "environment", "events", "life", "roles", "comms", "initial_state")
        }),
    ]
    good, errors, usage = asyncio.run(generate_package(FakeLLM(replies), "灰潮退去后的堤邦世界", name="灰潮纪"))
    good_ok = errors == [] and validate_package(good) == []

    garbage = asyncio.run(generate_package(FakeLLM(["这不是 JSON"]), "随便"))
    bad_ok = bool(garbage[1]) and garbage[2]["paused"] is False

    revised, r_errors, _ = asyncio.run(revise_package(FakeLLM(["仍然不是 JSON"]), package, "把堤长改成两人共治"))
    revise_ok = bool(r_errors)

    target = ctx.cfg.paths.packages / "confirmed.json"
    save_package(target, package)
    frozen = target.read_text(encoding="utf-8")
    refused = ""
    try:
        broken = clone(package)
        broken["world"]["axioms"] = []
        dispatch(ctx.cfg, ctx.store, "world.package.save", {"path": str(target), "package": broken})
    except UmpError as exc:
        refused = str(exc)
    intact = target.read_text(encoding="utf-8") == frozen

    ok = shared and good_ok and bad_ok and revise_ok and bool(refused) and intact
    evidence = (
        f"生成/修订共用 validate_package={shared}；正常生成整包通过校验={good_ok}；"
        f"两次畸形输出均交回错误={bad_ok}/{revise_ok}；非法包落盘被拒={bool(refused)}且原文件逐字节不变={intact}"
    )
    return ("PASS" if ok else "FAIL"), "新建与修订同一套校验，失败候选不落盘、不覆盖确认版本", evidence


def c02d_android_parity(ctx: Ctx) -> tuple[str, str, str]:
    """C2 的「两端解释同一包一致」子句（安卓端）。"""
    return "DEFERRED", "两端（桌面/安卓）解释同一包一致", (
        "安卓端未移植：DESIGN.md §6.1 阶段 7（可选）「安卓移植评估」；现无安卓实现可实测。"
        "桌面端走同一 ops 面（world.package.validate / instance.*），无独立第二套校验器"
    )


# ---------------------------------------------------------------- C3

def c03_unique_names(ctx: Ctx) -> tuple[str, str, str]:
    """C3 创建 / 导入 / 重命名并发下仍全局唯一；改名不取代原始名称记录。"""
    package = example_package()
    card = example_card(package)
    results: list[str] = []
    failures: list[str] = []
    lock = threading.Lock()

    def create(index: int) -> None:
        try:
            info = create_instance(ctx.store, clone(package), [clone(card)], display_name="同名世界")
            with lock:
                results.append(str(info["name"]))
        except Exception as exc:  # 唯一索引兜底拒绝也算「不产生重名」
            with lock:
                failures.append(f"{type(exc).__name__}:{str(exc)[:36]}")

    threads = [threading.Thread(target=create, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    unique = bool(results) and len(set(results)) == len(results)  # 唯一性：并发请求之间不产生重名

    # 产品路径（管理面串行请求）：同名创建应依次拿到 基名 / _2 / _3
    serial: list[str] = []
    for _ in range(3):
        serial.append(str(dispatch(ctx.cfg, ctx.store, "instance.create",
                                   {"package": clone(package), "cards": [clone(card)],
                                    "display_name": "串行同名"})["instance"]["name"]))
    serial_ok = serial == ["串行同名", "串行同名_2", "串行同名_3"]

    seed = create_instance(ctx.store, clone(package), [clone(card)], display_name="对照世界")
    conflict = ""
    try:
        from isekai_core.world.instances import rename_instance

        rename_instance(ctx.store, seed["id"], results[0])
    except InstanceError as exc:
        conflict = str(exc)
    renamed = None
    if not conflict:
        pass
    from isekai_core.world.instances import rename_instance

    renamed = rename_instance(ctx.store, seed["id"], "对照世界改名")
    original_kept = renamed["original_name"] == seed["original_name"] == package["meta"]["original_name"]
    display_edit = clone(package)
    display_edit["meta"]["display_name"] = "换了个显示名"
    still = renamed["original_name"] == package["meta"]["original_name"]

    export_path = ctx.root / "dup.isekai.json"
    write_export(ctx.store, seed["id"], export_path)
    container = json.loads(export_path.read_text(encoding="utf-8"))
    first_import = import_instance(ctx.store, container)
    second_import = import_instance(ctx.store, container)
    import_unique = len({first_import["name"], second_import["name"]}) == 2 and all(
        item["name"] not in ("对照世界改名", "对照世界") for item in (first_import, second_import)
    )
    db_unique = False
    try:
        ctx.store._conn.execute(
            "INSERT INTO instance(id,name,original_name,package_id,data_format,rules_version,app_version,seed,moment,setting,imported,created_at)"
            " VALUES('in-dup','对照世界改名','x','x','0.1','0.1','0.1','s',0,'{}',0,0)"
        )
    except Exception as exc:
        db_unique = "UNIQUE" in str(exc) or "unique" in str(exc)

    ok = unique and serial_ok and bool(conflict) and original_kept and still and import_unique and db_unique
    evidence = (
        f"6 个真并发同名创建 → {sorted(results)}（无重名；{len(failures)} 个竞争请求被唯一约束拒绝并抛 "
        f"{set(failures) or '无'}，不是分配 _N）；管理面串行创建 → {serial}；改名冲突被拒={'名称已被占用' in conflict}；"
        f"改名后 original_name 仍为 {renamed['original_name']!r}（改 display_name 不生效）；"
        f"同一导出件导入两次 → {[first_import['name'], second_import['name']]}；库层唯一约束={db_unique}"
    )
    return ("PASS" if ok else "FAIL"), "并发创建 / 导入 / 重命名仍全局唯一，改显示名不取代原始名称记录", evidence


# ---------------------------------------------------------------- C4

def c04_export_import_roundtrip(ctx: Ctx) -> tuple[str, str, str]:
    """C4 多线导出再导入恢复对话 / 披露 / 记忆 / 引用；删原实例不影响副本。"""
    world = ctx.service()
    info, timeline_id, first = mk(ctx, world)
    world.activate(info["id"], timeline_id, now_real=1.7e9)
    world.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    second_card, second, _ = add_second(ctx, world, info, timeline_id, name="堤砚")

    reply_id = say(ctx.store, info, timeline_id, first, env="env-a1", text="堤上的事",
                   reply="信报上抄到堤长身故，接任未毕")
    ctx.store.memory_add({
        "id": "mm-a1", "instance_id": info["id"], "timeline_id": timeline_id, "character_id": first,
        "text": "她答应过替堤长压着那份信报", "kind": "promise",
        "sources": [{"kind": "intent", "ref": "in-1"}], "happened_world": 0, "learned_world": 0,
        "recorded_world": 0, "semantic_watermark": 0, "strength": 0.9, "confidence": 0.9,
    })
    grant = world.disclose(info["id"], timeline_id, from_character=first, to_character=second, refs=[reply_id])
    commit = world.commit(info["id"], timeline_id, note="导出点")
    branch = world.fork(info["id"], timeline_id, commit_id=commit["id"], name="分支线")

    path = ctx.root / "roundtrip.isekai.json"
    write_export(ctx.store, info["id"], path)
    container = json.loads(path.read_text(encoding="utf-8"))
    before = counts(ctx.store, info["id"])

    copy_store = Store(ctx.root / "copy" / "isekai.db")
    copy_store.ensure_schema()
    copy_info = import_instance(copy_store, container)
    after = counts(copy_store, copy_info["id"])
    lines = len(copy_store.timeline_list(copy_info["id"]))

    checked = ("timeline", "commit_log", "session", "message", "memory", "knowledge", "disclosure",
               "character_join", "event", "claim", "life_plan", "institution_state", "custom_state",
               "environment_state", "memory_citation")
    mismatch = {key: (before[key], after[key]) for key in checked if before[key] != after[key]}

    ctx.store.instance_delete(info["id"])
    survivor = copy_store.instance_get(copy_info["id"]) is not None and counts(copy_store, copy_info["id"])["message"] == after["message"]
    copy_store.close()

    ok = not mismatch and lines == 2 and survivor
    evidence = (
        f"2 条线（{lines}）导出→导入：对话 {before['message']}→{after['message']}、记忆 {before['memory']}→{after['memory']}、"
        f"知识/引用 {before['knowledge']}→{after['knowledge']}、事件 {before['event']}→{after['event']}、"
        f"披露 {before['disclosure']}→{after['disclosure']}；不一致项={mismatch or '无'}（"
        f"portable.py:238-252 的 _restore_runtime_state 只列 12 个键，漏 institution / customs；"
        f"disclosure 连 runtime_dump（store.py:1800-1898）都没导出，§7.1 明列「披露」应随件）；"
        f"删原实例后副本仍完整={survivor}"
    )
    return ("PASS" if ok else "FAIL"), "多线导出再导入恢复全部对话 / 披露 / 记忆 / 引用，删原实例不影响副本", evidence


# ---------------------------------------------------------------- C5

#: §7.1 明令不进便携包的本机控制 / 凭据类字段（按键名精确匹配，不误伤能力标识 message.delivery.v1）
FORBIDDEN_KEYS = ("credential", "binding_token", "channel_id", "thread_id", "api_key", "apikey",
                  "device", "device_path", "log_path", "rate_command", "pending_rate", "anchor_real",
                  "high_water_real", "session_token", "access_token")


def key_paths(payload: Any, prefix: str = "") -> list[str]:
    """递归收集全部键路径（导出件的键名扫描用）。"""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = f"{prefix}.{key}" if prefix else str(key)
            found.append(here)
            found.extend(key_paths(value, here))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(key_paths(value, f"{prefix}[{index}]"))
    return found


def forbidden_hits(container: dict[str, Any]) -> list[str]:
    hits = []
    for path in key_paths(container):
        leaf = path.rsplit(".", 1)[-1].split("[")[0]
        if leaf in FORBIDDEN_KEYS:
            hits.append(path)
    return hits


def c05_import_frozen_no_transport_catchup(ctx: Ctx) -> tuple[str, str, str]:
    """C5 导入全冻结、无运输期补算、不恢复 Key / 绑定、不向旧通道补发。"""
    world = ctx.service()
    info, timeline_id, first = mk(ctx, world)
    world.activate(info["id"], timeline_id, now_real=1.7e9)
    world.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    watermark = int(ctx.store.clock_get(timeline_id)["processed_world"])
    say(ctx.store, info, timeline_id, first, env="env-a5", text="问", reply="答")
    world.set_rate(info["id"], timeline_id, rate=60, now_real=1.7e9 + 2 * DAY)
    path = ctx.root / "t5.isekai.json"
    write_export(ctx.store, info["id"], path)
    text = path.read_text(encoding="utf-8")
    container = json.loads(text)
    leaked = forbidden_hits(container) + [str(ctx.root)] * (str(ctx.root) in text)

    copy_store = Store(ctx.root / "copy" / "isekai.db")
    copy_store.ensure_schema()
    copy_info = import_instance(copy_store, container)
    copy_tl = copy_store.timeline_list(copy_info["id"])[0]["id"]
    state = copy_store.timeline_get(copy_tl)["state"]
    frozen_clock = int(copy_store.clock_get(copy_tl)["processed_world"])
    world2 = RuntimeService(copy_store, autocommit_enabled=False)
    advanced = world2.advance(copy_info["id"], copy_tl, now_real=1.7e9 + 30 * DAY)
    catch_up = world2.catch_up_all(now_real=1.7e9 + 60 * DAY)
    rows = counts(copy_store, copy_info["id"])
    copy_store.close()

    ok = (state == "frozen" and frozen_clock == watermark and advanced["state"] == "frozen"
          and advanced["processed_world"] == watermark and catch_up == {}
          and rows["thread"] == 0 and rows["delivery"] == 0 and not leaked)
    evidence = (
        f"导入线 state={state}，时钟停在导出水位 {frozen_clock}（导出时 {watermark}）；"
        f"越过 30 天现实时间再推进 → state={advanced['state']}、水位 {advanced['processed_world']} 不变、"
        f"catch_up_all={catch_up}；回传线程绑定 {rows['thread']} 条、投递回执 {rows['delivery']} 条；"
        f"导出件里的凭据 / 绑定 / 锚点 / 本机路径键={leaked or '无'}"
    )
    return ("PASS" if ok else "FAIL"), "导入即冻结、运输期不补算，不恢复 Key / 绑定、不向旧通道补发", evidence


# ---------------------------------------------------------------- C6

def c06_atomic_failure(ctx: Ctx) -> tuple[str, str, str]:
    """C6 版本不兼容 / 损坏 / 恶意容器原子失败，已有实例不变。"""
    world = ctx.service()
    info, timeline_id, _ = mk(ctx, world)
    say(ctx.store, info, timeline_id, str(json.loads(ctx.store.instance_get(info["id"])["setting"])["cards"][0]["meta"]["card_id"]),
        env="env-a6", text="问", reply="答")
    path = ctx.root / "t6.isekai.json"
    write_export(ctx.store, info["id"], path)
    container = json.loads(path.read_text(encoding="utf-8"))
    baseline = counts(ctx.store, info["id"])
    baseline_lines = len(ctx.store.timeline_list(info["id"]))

    cases: list[tuple[str, Any]] = []
    tampered = clone(container)
    tampered["runtime"]["messages"] = tampered["runtime"]["messages"][:1]
    cases.append(("载荷被改动（指纹不符）", tampered))
    bad_version = clone(container)
    bad_version["container"]["container_version"] = "2.0"
    cases.append(("容器主版本不兼容", bad_version))
    bad_format = clone(container)
    bad_format["container"]["format"] = "other.app"
    cases.append(("不是本应用的导出件", bad_format))
    unknown_cap = clone(container)
    unknown_cap["container"]["capabilities"] = ["world.package.v1", "nope.v9"]
    cases.append(("要求未知必需能力", unknown_cap))
    empty = clone(container)
    empty.pop("container")
    cases.append(("缺少 container 段", empty))
    bad_setting = clone(container)
    bad_setting["setting"]["world_package"]["calendar"]["day_seconds"] = -1
    from isekai_core.world.portable import _digest  # 只重算指纹，模拟「自洽但内容非法」的容器

    bad_setting["integrity"]["digest"] = _digest({"setting": bad_setting["setting"], "runtime": bad_setting["runtime"]})
    cases.append(("重算指纹但锁定设定非法", bad_setting))

    results: list[str] = []
    for label, payload in cases:
        try:
            import_instance(ctx.store, payload)
            results.append(f"{label}:未被拒")
        except (InstanceError, PackageError) as exc:
            results.append(f"{label}:拒（{str(exc)[:28]}…）")
    garbage_path = ctx.root / "garbage.isekai.json"
    garbage_path.write_text("{不是 JSON", encoding="utf-8")
    try:
        from isekai_core.world.portable import read_container

        read_container(garbage_path)
        results.append("坏 JSON:未被拒")
    except InstanceError as exc:
        results.append(f"坏 JSON:拒（{str(exc)[:20]}…）")

    after = counts(ctx.store, info["id"])
    atomic = all(after[key] == baseline[key] for key in baseline) and len(ctx.store.timeline_list(info["id"])) == baseline_lines
    ok = all("未被拒" not in item for item in results) and atomic
    evidence = (
        f"{len(cases)+1} 类坏容器逐一被拒：{'；'.join(results)}；"
        f"每次失败后已有实例行数不变={atomic}（instance={after['instance']}，时间线仍 {baseline_lines} 条）"
    )
    return ("PASS" if ok else "FAIL"), "版本不兼容 / 损坏 / 恶意容器均原子失败，已有实例不变", evidence


def c06d_converter(ctx: Ctx) -> tuple[str, str, str]:
    """C6 的「转换失败」子句（可信转换器）。"""
    return "DEFERRED", "转换失败保留原包、不修改已有实例", (
        "无转换器实现：WORLD_SETTING_SPEC §十「待模块设计项」列明「转换器注册格式」未定，"
        "§7.5 规定「重大不兼容变更发布时必须配转换器」——尚无此类发布；"
        "instances.compatibility 只产出 compatible/convertible/blocked 判定，不执行转换"
    )


# ---------------------------------------------------------------- C7

def c07_no_leak(ctx: Ctx) -> tuple[str, str, str]:
    """C7 包内无激活状态 / 凭据；管理面、普通日志与导入错误不泄露内部事件、性格与记忆。"""
    secret_memory = "她答应过替堤长压着那份信报，绝不能让议会的抄手知道"
    secret_unit = "把信报压到退潮之后再说"
    records: list[str] = []
    handler = logging.Handler()
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    handler.emit = lambda record: records.append(formatter.format(record))
    attached = ["isekai", "isekai.world.ops", "isekai.world.generator", "isekai.llm", "isekai.runtime"]
    for name in attached:
        target = logging.getLogger(name)
        target.setLevel(logging.DEBUG)
        target.addHandler(handler)

    world = ctx.service()
    info, timeline_id, first = mk(ctx, world)
    say(ctx.store, info, timeline_id, first, env="env-a7", text="问一句", reply="答一句")
    ctx.store.memory_add({
        "id": "mm-secret", "instance_id": info["id"], "timeline_id": timeline_id, "character_id": first,
        "text": secret_memory, "kind": "fragment", "sources": [{"kind": "self", "ref": "x"}],
        "happened_world": 0, "learned_world": 0, "recorded_world": 0, "semantic_watermark": 0,
        "strength": 0.7, "confidence": 0.8,
    })
    ctx.store.unit_put({
        "id": "iu-secret", "instance_id": info["id"], "timeline_id": timeline_id, "character_id": first,
        "mode": "dialog", "semantic": secret_unit, "basis": "测试", "confidence": 0.4, "stability": 0.0,
        "archived": 0, "consumed": "[]", "updated_world": 0,
    })
    path = ctx.root / "t7.isekai.json"
    write_export(ctx.store, info["id"], path)
    container = json.loads(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    leaked_keys = forbidden_hits(container) + [str(ctx.root)] * (str(ctx.root) in text)

    # 管理面：这些 op 的返回值是壳 / UI 直接渲染的东西
    surfaces: dict[str, Any] = {
        "instance.list": dispatch(ctx.cfg, ctx.store, "instance.list", {}),
        "instance.info": dispatch(ctx.cfg, ctx.store, "instance.info", {"id": info["id"]}),
        "runtime.clock": dispatch(ctx.cfg, ctx.store, "runtime.clock",
                                  {"instance_id": info["id"], "timeline_id": timeline_id}, runtime=world),
        "runtime.commits": dispatch(ctx.cfg, ctx.store, "runtime.commits",
                                    {"instance_id": info["id"], "timeline_id": timeline_id}),
        "runtime.budget": dispatch(ctx.cfg, ctx.store, "runtime.budget", {"instance_id": info["id"]}),
        "disclose.list": dispatch(ctx.cfg, ctx.store, "disclose.list",
                                  {"instance_id": info["id"], "timeline_id": timeline_id}),
    }
    leaks = [name for name, payload in surfaces.items() if secret_memory in blob(payload) or secret_unit in blob(payload)]

    tampered = clone(container)
    tampered["runtime"]["messages"] = tampered["runtime"]["messages"] + [
        {"session_id": container["runtime"]["sessions"][0]["id"], "role": "character", "text": "夹带的一行",
         "state": "fixed", "created_at": 0.0, "message_id": "m-injected"}
    ]
    tampered_path = ctx.root / "tampered.isekai.json"
    tampered_path.write_text(blob(tampered), encoding="utf-8")
    error_text = ""
    try:
        dispatch(ctx.cfg, ctx.store, "instance.import", {"path": str(tampered_path)})
    except UmpError as exc:
        error_text = str(exc)
    error_leak = secret_memory in error_text or secret_unit in error_text

    join_error = ""
    try:
        bad_card = example_card(json.loads(ctx.store.instance_get(info["id"])["setting"])["world_package"], name="堤砚")
        bad_card["initial_knowledge"] = [{"ref_type": "historiography", "ref_id": "hs-9", "scope": ["cf-9"], "obtained_at": 1}]
        dispatch(ctx.cfg, ctx.store, "runtime.card.add",
                 {"instance_id": info["id"], "timeline_id": timeline_id, "card": bad_card}, runtime=world)
    except UmpError as exc:
        join_error = str(exc)
    log_leak = [item for item in records if secret_memory in item or secret_unit in item]

    ok = (not leaked_keys and not leaks and not error_leak and not log_leak and bool(error_text)
          and bool(join_error) and len(records) > 0)
    evidence = (
        f"导出件出现 {leaked_keys or '无'} 个本机控制 / 凭据类键、本机路径泄漏={str(ctx.root) in text}；"
        f"管理面 {len(surfaces)} 个 op 载荷含内部记忆 / 性格={leaks or '无'}；"
        f"导入错误文本={error_text[:34]!r}（不含内部内容={not error_leak}）；"
        f"补卡拒绝信息={join_error[:44]!r}；共 {len(records)} 条日志记录，含内部内容={log_leak or '无'}"
    )
    for name in attached:  # 摘掉探针自己的日志钩子，别影响后续检查
        logging.getLogger(name).removeHandler(handler)
    handler.close()
    return ("PASS" if ok else "FAIL"), "导出件无激活状态 / 凭据，管理面 / 日志 / 导入错误不泄露内部事件·性格·记忆", evidence


# ---------------------------------------------------------------- C8

def c08_axiom_change_needs_new_instance(ctx: Ctx) -> tuple[str, str, str]:
    """C8 改公理须新建实例、改局势须新建时间线、普通聊天不隐式触发。"""
    world = ctx.service()
    info, timeline_id, first = mk(ctx, world)
    world.activate(info["id"], timeline_id, now_real=1.7e9)
    world.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    setting_before = setting_hash(ctx.store, info["id"])
    axioms = json.loads(ctx.store.instance_get(info["id"])["setting"])["world_package"]["world"]["axioms"]
    lines_before = len(ctx.store.timeline_list(info["id"]))
    events_before = len(ctx.store.event_window(info["id"], timeline_id, until=10**15, limit=500))

    edited = example_package()
    edited["world"]["axioms"] = [{"id": "ax-1", "text": "完全不同的公理：潮水不会退。通行的只有冬路。"}]
    save_package(ctx.cfg.paths.packages / "edited.json", edited)
    axioms_after = json.loads(ctx.store.instance_get(info["id"])["setting"])["world_package"]["world"]["axioms"]
    axiom_isolated = axioms_after == axioms and setting_hash(ctx.store, info["id"]) == setting_before

    payload = {
        "intent": "堤务吏换人：柳氏接任守碑人",
        "when": "now",
        "effects": [{"kind": "institution_state", "target": "rl-1", "expiry": "until_cleared"}],
        "claims": [{"text": "守碑人换人，柳氏接任", "source_id": "src-1", "audience": "public"}],
    }
    drafted = asyncio.run(world.draft_user_event(info["id"], timeline_id, intent=payload["intent"], payload=payload))
    draft = drafted.get("draft") or {}
    confirmed = (world.confirm_user_event(info["id"], draft["draft_id"], name="柳氏线")
                 if drafted.get("accepted") and draft.get("draft_id") else {})
    lines_after = len(ctx.store.timeline_list(info["id"]))
    old_line_events = len(ctx.store.event_window(info["id"], timeline_id, until=10**15, limit=500))
    new_line_state = (ctx.store.timeline_get(confirmed["timeline_id"])["state"] if confirmed
                      else f"草案未接受：{drafted.get('reason')}")
    new_line_frozen = bool(confirmed) and new_line_state == "frozen"

    say(ctx.store, info, timeline_id, first, env="env-a8", text="今天滩上风大", reply="嗯，风大")
    say(ctx.store, info, timeline_id, first, env="env-a8b", text="堤上的事", reply="信报上抄到堤长身故")
    lines_after_chat = len(ctx.store.timeline_list(info["id"]))
    chat_quiet = lines_after_chat == lines_after and setting_hash(ctx.store, info["id"]) == setting_before

    ok = (axiom_isolated and drafted.get("accepted") is True and lines_after == lines_before + 1
          and new_line_frozen and old_line_events == events_before and chat_quiet)
    evidence = (
        f"改源包公理 → 实例锁定公理不变={axiom_isolated}；引入合法事件 → 新建线（{lines_before}→{lines_after} 条、"
        f"新线默认 {new_line_state}）且原线事件数不变（{events_before}→{old_line_events}）；"
        f"两轮普通对话后线数 {lines_after_chat}、设定指纹不变={chat_quiet}；"
        f"逐项={axiom_isolated}/{drafted.get('accepted')!r}/{lines_after == lines_before + 1}/"
        f"{new_line_frozen}/{old_line_events == events_before}/{chat_quiet}"
    )
    return ("PASS" if ok else "FAIL"), "改公理只影响新建实例、改局势只新建时间线，普通聊天不隐式触发任一操作", evidence


# ---------------------------------------------------------------- C9

def c09_backfill_respects_confirmed(ctx: Ctx) -> tuple[str, str, str]:
    """C9 回填不推翻已确认内容；约束不满足即失败或留白；越权初始知识不能创建。"""
    world = ctx.service()
    package_in = example_package()
    card_in = example_card(package_in)
    info = create_instance(ctx.store, package_in, [card_in])
    timeline_id = ctx.store.timeline_list(info["id"])[0]["id"]
    setting = json.loads(ctx.store.instance_get(info["id"])["setting"])
    package = setting["world_package"]
    before = digest({
        "axioms": package["world"]["axioms"],
        "canon": package["canon"],
        "narratives": package["narratives"],
        "historiography": package["historiography"],
        "cards": setting["cards"],
    })
    rows_before = counts(ctx.store, info["id"])
    populated = world.backfill(info["id"], timeline_id)
    rows = counts(ctx.store, info["id"])
    after = digest({
        "axioms": json.loads(ctx.store.instance_get(info["id"])["setting"])["world_package"]["world"]["axioms"],
        "canon": json.loads(ctx.store.instance_get(info["id"])["setting"])["world_package"]["canon"],
        "narratives": json.loads(ctx.store.instance_get(info["id"])["setting"])["world_package"]["narratives"],
        "historiography": json.loads(ctx.store.instance_get(info["id"])["setting"])["world_package"]["historiography"],
        "cards": json.loads(ctx.store.instance_get(info["id"])["setting"])["cards"],
    })
    no_side_effects = (rows["effect_state"] == rows_before["effect_state"] == 0
                       and rows["knowledge"] == rows_before["knowledge"] == 0)
    repeat = world.backfill(info["id"], timeline_id)

    blank_package = example_package()
    blank_package["initial_state"] = {"events": [], "rumors": [], "mysteries": []}
    blank_card = example_card(blank_package)
    blank_ok = validate_package(blank_package) == []
    blank_info = None
    blank_rows = -1
    if blank_ok:
        blank_info = create_instance(ctx.store, blank_package, [blank_card])
        blank_tl = ctx.store.timeline_list(blank_info["id"])[0]["id"]
        world.ensure_instance(blank_info["id"], now_real=time.time())
        blank_rows = world.backfill(blank_info["id"], blank_tl)

    dangling = example_card(example_package())
    dangling["initial_knowledge"] = [{"ref_type": "historiography", "ref_id": "hs-99", "scope": ["cf-1"], "obtained_at": DAY}]
    over_scope = example_card(example_package())
    over_scope["initial_knowledge"] = [{"ref_type": "historiography", "ref_id": "hs-1", "scope": ["cf-2"], "obtained_at": DAY * 1200}]
    late = example_card(example_package())
    late["initial_knowledge"] = [{"ref_type": "canon", "ref_id": "cf-1", "obtained_at": DAY * 99999}]
    rejects = {}
    for label, bad_card in (("引用不存在的史料条目", dangling), ("scope 越权", over_scope), ("获知晚于初始时刻", late)):
        try:
            create_instance(ctx.store, example_package(), [bad_card])
            rejects[label] = "未被拒"
        except InstanceError as exc:
            rejects[label] = f"拒（{str(exc)[:26]}…）"

    ok = (before == after and no_side_effects and repeat == 0 and blank_ok and blank_rows == 0
          and all("未被拒" not in value for value in rejects.values()))
    evidence = (
        f"回填 {populated} 行（再跑一次 {repeat} 行，幂等）；公理 / 实情 / 说法 / 史料 / 角色卡指纹不变={before == after}；"
        f"不施加效果={rows['effect_state'] == 0}、不产生获知={rows['knowledge'] == 0}；空初始状态合法包留白={blank_rows} 行；"
        f"越权 / 悬空初始知识：{rejects}"
    )
    return ("PASS" if ok else "FAIL"), "回填不推翻已确认内容、无合法候选即留白，越权或悬空初始知识不能创建实例", evidence


# ---------------------------------------------------------------- C10

def c10_registry_and_references(ctx: Ctx) -> tuple[str, str, str]:
    """C10 未登记对象作事实参与者 / 效果目标 / 结构引用即失败；纯署名不被补造生平；虚构名字的传闻可获知；在册≠已知。"""
    package = example_package()
    bad_target = clone(package)
    bad_target["events"]["families"][0]["templates"][0]["effects"][0]["target"] = "src-999"
    bad_ref = clone(package)
    bad_ref["initial_state"]["events"] = ["cf-999"]
    bad_mystery = clone(package)
    bad_mystery["initial_state"]["mysteries"][0]["refs"] = ["nv-999"]
    rejects = {
        "效果目标未登记": validate_package(bad_target),
        "初始事件引用未登记": validate_package(bad_ref),
        "谜题引用未登记": validate_package(bad_mystery),
    }
    def has_unknown(errors: list[str]) -> bool:
        return any("未登记对象" in item or "不存在" in item for item in errors)

    plain = clone(package)
    plain["historiography"][0]["contributors"] = [
        {"name": "堤南史馆老史官", "role": "编纂", "period": "崩塌后第 3 年"}
    ]
    plain["narratives"].append({
        "id": "nv-3", "text": "有传闻说巡堤人「阿岐」在崩堤当夜登堤敲过钟。", "source_id": "src-1",
        "canon_ref": "cf-2", "obtain": ["在城驿听脚夫转述"], "confidence": "doubted",
    })
    plain["initial_state"]["rumors"] = ["nv-1", "nv-2", "nv-3"]
    card = example_card(plain)
    card["initial_knowledge"].append({"ref_type": "narrative", "ref_id": "nv-3", "obtained_at": DAY * 1300})
    accepted = validate_package(plain) == [] and validate_card(card, plain, moment=DAY * 1500) == []

    world = ctx.service()
    info = None
    entities_before = len(plain["entities"])
    bio_added = entities_before
    hearsay = False
    registered_unknown = False
    if accepted:
        info = create_instance(ctx.store, plain, [card])
        timeline_id = ctx.store.timeline_list(info["id"])[0]["id"]
        world.ensure_instance(info["id"], now_real=time.time())
        world.backfill(info["id"], timeline_id)
        locked = json.loads(ctx.store.instance_get(info["id"])["setting"])["world_package"]
        bio_added = len(locked["entities"])
        slice_ = cognition.knowledge_slice(plain, card, world_seconds=DAY * 1500)
        hearsay = any("阿岐" in str(item.get("text") or "") for item in slice_)
        registered_unknown = not any("堤长议会" in str(item.get("text") or "") and "en-2" in str(item) for item in slice_)
        registry_has_org = any(str(item.get("id")) == "en-2" for item in locked["entities"])

    ok = (all(has_unknown(errors) for errors in rejects.values()) and accepted
          and bio_added == entities_before and hearsay and registered_unknown and registry_has_org)
    evidence = (
        f"未登记引用被拒：{[name for name, errors in rejects.items() if has_unknown(errors)]}；"
        f"纯署名贡献者「堤南史馆老史官」未进名册（entities {entities_before}→{bio_added}）；"
        f"含虚构名字『阿岐』的传闻可被角色合法获知={hearsay}；名册里的 en-2 堤长议会不在该角色已知切片里={registered_unknown}"
    )
    return ("PASS" if ok else "FAIL"), "未登记对象作参与者 / 效果目标 / 结构引用即失败，纯署名不造生平，传闻可获知，在册≠已知", evidence


# ---------------------------------------------------------------- C11

def c11_minimum_content(ctx: Ctx) -> tuple[str, str, str]:
    """C11 一族 / 无谜题的合法包不被拒；空壳史料、非法寿命依据、越权初始知识仍失败。"""
    package = example_package()
    lean = clone(package)
    lean["events"]["families"] = [lean["events"]["families"][0]]
    lean["events"]["calendar"] = [lean["events"]["calendar"][0]]
    lean["initial_state"] = {"events": [], "rumors": [], "mysteries": []}
    lean_card = example_card(lean)
    lean_ok = validate_package(lean) == []
    created = None
    if lean_ok:
        created = create_instance(ctx.store, lean, [lean_card])

    shell = clone(package)
    shell["historiography"][0]["entries"] = []
    shell_errors = validate_package(shell)

    moment = DAY * 1500
    long_lived = example_card(package, born=moment - 200 * 90 * DAY)
    lifespan_errors = validate_card(long_lived, package, moment=moment)
    span_errors = validate_card(example_card(package, born=moment + 10 * DAY), package, moment=moment)

    over = example_card(package)
    over["initial_knowledge"] = [{"ref_type": "historiography", "ref_id": "hs-1", "scope": ["cf-2"], "obtained_at": DAY * 1200}]
    over_errors = validate_card(over, package, moment=moment)

    ok = (lean_ok and created is not None and bool(shell_errors) and bool(lifespan_errors)
          and bool(span_errors) and bool(over_errors))
    evidence = (
        f"单一事件族 + 空 initial_state 的包通过校验={lean_ok} 且可建实例={created is not None}（无题材配额拒绝）；"
        f"空壳史料被拒={'不能是空壳' in blob(shell_errors)}；寿命越界被拒={lifespan_errors[:1]}；"
        f"出生晚于初始时刻被拒={bool(span_errors)}；越权初始知识被拒={over_errors[:1]}"
    )
    return ("PASS" if ok else "FAIL"), "最低标准：内容稀少的合法包不被配额拒绝，空壳 / 非法寿命 / 越权知识仍失败", evidence


# ---------------------------------------------------------------- C12

def c12_draft_and_budget(ctx: Ctx) -> tuple[str, str, str]:
    """C12 无 Key 可手编 / 存草稿 / 导入；丢弃不伤正式资产；达上限暂停并保留已完成产物；两类导入不混淆。"""
    package = example_package()
    target = ctx.cfg.paths.packages / "official.json"
    save_package(target, package)
    frozen_bytes = target.read_bytes()

    dispatched = dispatch(ctx.cfg, ctx.store, "world.draft.save", {
        "name": "灰潮纪草稿", "kind": "package", "payload": {"partial": True},
        "progress": {"section": "world"}, "errors": ["world.axioms: 公理内容为空"],
    })
    draft_file = ctx.cfg.paths.packages / dispatched["file"]
    loaded = dispatch(ctx.cfg, ctx.store, "world.draft.load", {"name": "灰潮纪草稿"})
    listing = dispatch(ctx.cfg, ctx.store, "world.draft.list", {})
    listed_files = [item["file"] for item in listing["drafts"]]
    drafts_not_creations = not [item for item in dispatch(ctx.cfg, ctx.store, "world.package.list", {})["packages"]
                                if item["file"].endswith(".draft.json")]
    dispatch(ctx.cfg, ctx.store, "world.draft.discard", {"name": "灰潮纪草稿"})
    official_intact = target.read_bytes() == frozen_bytes and not draft_file.exists()

    replies = [blob({key: package[key] for key in ("meta", "calendar", "world")})]
    partial, p_errors, p_usage = asyncio.run(generate_package(FakeLLM(replies), "灰潮", name="灰潮纪", max_calls=1))

    export_path = ctx.root / "t12.isekai.json"
    world = ctx.service()
    info, timeline_id, _ = mk(ctx, world)
    write_export(ctx.store, info["id"], export_path)
    mixed = {}
    try:
        dispatch(ctx.cfg, ctx.store, "instance.import", {"path": str(target)})
        mixed["把世界包当实例包导入"] = "未被拒"
    except UmpError as exc:
        mixed["把世界包当实例包导入"] = f"拒（{str(exc)[:22]}…）"
    try:
        dispatch(ctx.cfg, ctx.store, "world.package.load", {"path": str(export_path)})
        mixed["把实例包当世界包读"] = "未被拒"
    except UmpError as exc:
        mixed["把实例包当世界包读"] = f"拒（{str(exc)[:22]}…）"

    keyless = load_config(ctx.root)
    keyless_llm = LLMClient(keyless.llm)
    no_key = not keyless.llm.api_key
    fake_success = ""
    try:
        asyncio.run(dispatch_async(ctx.cfg, keyless_llm, "world.package.generate", {"brief": "无 Key"}))
        fake_success = "未报错（疑似伪造成功）"
    except UmpError as exc:
        fake_success = f"{exc.code}"

    ok = (loaded["draft"]["payload"] == {"partial": True} and draft_file.name in listed_files
          and drafts_not_creations and official_intact and p_usage["paused"] is True
          and p_usage["calls"] == 1 and partial["calendar"]["day_seconds"] == DAY
          and all("未被拒" not in value for value in mixed.values()) and no_key and fake_success == Err.LLM_NOT_CONFIGURED)
    evidence = (
        f"无 Key 下草稿 存/读/列/弃 全通（{dispatched['file']}，progress 保留={loaded['draft']['progress']}）；"
        f"草稿不进创作目录={drafts_not_creations}；丢弃后正式包逐字节不变={official_intact}；"
        f"预算 max_calls=1 → calls={p_usage['calls']} paused={p_usage['paused']}，已完成段落保留={partial['calendar']['day_seconds'] == DAY}；"
        f"两类导入不混淆：{mixed}；无 Key 生成={fake_success}"
    )
    return ("PASS" if ok else "FAIL"), "草稿 / 用量：无 Key 可手编与存草稿、丢弃不伤正式资产、达上限暂停且两类导入不混淆", evidence


# ---------------------------------------------------------------- C13

def c13_undeclared_institutions(ctx: Ctx) -> tuple[str, str, str]:
    """C13 未声明制度 / 惯例的题材不被拒且不隐式补造；声明了却无法一致解释则创建期失败。"""
    package = example_package()
    lean = clone(package)
    lean["world"]["institutions"] = []
    lean["world"]["customs"] = []
    lean["events"]["families"][0]["templates"][0]["effects"] = [
        {"kind": "source_delay", "target": "src-1", "expiry": "with_cause"},
    ]
    lean_card = example_card(lean)
    accepted = validate_package(lean) == [] and validate_card(lean_card, lean, moment=DAY * 1500) == []
    world = ctx.service()
    created = None
    no_fabrication = False
    intents_kept = -1
    if accepted:
        created = create_instance(ctx.store, lean, [lean_card])
        timeline_id = ctx.store.timeline_list(created["id"])[0]["id"]
        world.ensure_instance(created["id"], now_real=time.time())
        world.activate(created["id"], timeline_id, now_real=1.7e9)
        world.advance(created["id"], timeline_id, now_real=1.7e9 + DAY)
        locked = json.loads(ctx.store.instance_get(created["id"])["setting"])
        locked_package = locked["world_package"]
        locked_card = locked["cards"][0]
        no_fabrication = locked_package["world"]["institutions"] == [] and locked_package["world"]["customs"] == []
        intents_kept = len(locked_card["intents"])
        rows = counts(ctx.store, created["id"])
        no_fabrication = no_fabrication and rows["institution_state"] == 0 and rows["custom_state"] == 0

    declared = clone(package)
    declared["world"]["institutions"][0].pop("mandate")
    missing_ok = any("职权" in item for item in validate_package(declared))
    forms = clone(package)
    forms["world"]["customs"][0]["practice"] = "与 forms 不一致的做法"
    forms_ok = any("允许变化范围" in item for item in validate_package(forms))
    office = clone(package)
    office["events"]["families"][0]["templates"][0]["effects"].append(
        {"kind": "institution_state", "target": "off-99", "value": "en-1", "expiry": "until_cleared"}
    )
    office_errors = validate_package(office)
    office_ok = any("未声明的职位" in item for item in office_errors)

    ok = accepted and created is not None and no_fabrication and intents_kept == 1 and missing_ok and forms_ok and office_ok
    evidence = (
        f"未声明制度 / 惯例的包通过校验并可创建={accepted and created is not None}；"
        f"锁定包内仍未出现制度 / 惯例={no_fabrication}（含运行态 institution_state/custom_state=0）；"
        f"角色打算数保持声明值={intents_kept}；制度缺职权被拒={missing_ok}、做法越出 forms 被拒={forms_ok}、"
        f"效果指向未声明职位被拒={office_ok}"
    )
    return ("PASS" if ok else "FAIL"), "未声明的制度 / 惯例不拒绝也不隐式补造，声明了却不自洽即创建期失败", evidence


# ---------------------------------------------------------------- C14

def c14_add_character(ctx: Ctx) -> tuple[str, str, str]:
    """C14 补卡：不凭空出现、锚定补入时刻、个人史不与既定历史冲突、初始知识不越权、失败不改实例。"""
    world = ctx.service()
    info, timeline_id, first = mk(ctx, world)
    world.activate(info["id"], timeline_id, now_real=1.7e9)
    world.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    watermark = int(ctx.store.clock_get(timeline_id)["processed_world"])
    day_index = world.calendar(ctx.store.instance_get(info["id"])).day_index(watermark)
    before = counts(ctx.store, info["id"])
    setting_before = setting_hash(ctx.store, info["id"])

    card, second, joined = add_second(ctx, world, info, timeline_id)
    after = counts(ctx.store, info["id"])
    her_units = ctx.store.unit_list(info["id"], timeline_id, second)
    anchor = bool(her_units) and all(int(row["updated_world"]) == watermark for row in her_units)
    plan = ctx.store.plan_latest(info["id"], timeline_id, second)
    plan_day = int(plan["day_index"]) if plan else None
    no_new_event = after["event"] == before["event"] and after["claim"] == before["claim"]

    package = json.loads(ctx.store.instance_get(info["id"])["setting"])["world_package"]
    unborn = example_card(package, name="未来的孩子", born=watermark + DAY)
    over = example_card(package, name="越权者")
    over["initial_knowledge"] = [{"ref_type": "historiography", "ref_id": "hs-1", "scope": ["cf-2"], "obtained_at": DAY * 1200}]
    rejected = {}
    for label, bad in (("出生晚于补入时刻", unborn), ("初始知识越权", over)):
        try:
            world.add_character(info["id"], timeline_id, bad, now_real=1.7e9 + 3 * DAY)
            rejected[label] = "未被拒"
        except RuntimeStateError as exc:
            rejected[label] = f"拒（{str(exc)[:30]}）"
    final = counts(ctx.store, info["id"])
    intact = (
        final["character_join"] == after["character_join"] and final["unit"] == after["unit"]
        and final["event"] == before["event"]
        and setting_hash(ctx.store, info["id"]) == setting_before
        and ctx.store.timeline_get(timeline_id)["state"] == "active"
    )

    # 个人史相容（§3.7.4「不能是已死之人」）：声明在补入前已死亡的卡片不应被补入
    dead = example_card(package, name="已故之人")
    dead["identity"]["died"] = watermark - DAY
    dead_accepted = False
    try:
        world.add_character(info["id"], timeline_id, dead, now_real=1.7e9 + 4 * DAY)
        dead_accepted = True
    except RuntimeStateError:
        dead_accepted = False
    ok = (no_new_event and anchor is True and plan_day == day_index
          and all(value.startswith("拒") for value in rejected.values()) and intact and not dead_accepted)
    evidence = (
        f"补入水位 {joined['joined_world']}＝当前水位 {watermark}（非实例初始时刻 {info['moment']}）；"
        f"世界事件/说法数不变={no_new_event}（不凭空出现）；她 {len(her_units)} 个单元的 updated_world 全为补入水位={anchor}；"
        f"首日计划 day_index={plan_day}（补入日 {day_index}）；拒绝项={rejected}；被拒请求不留痕={intact}"
        f"（成员资格 {final['character_join']}、设定指纹 {setting_hash(ctx.store, info['id'])}）；"
        f"声明 dies at {dead['identity']['died']}（补入前一日）的卡片={'被接受（缺口，service.py:2472-2480 只校验出生与补入时刻，未校验 identity.died）' if dead_accepted else '被拒'}"
    )
    status = "PASS" if ok else "FAIL"
    return status, "补卡锚定补入时刻、不凭空出现、个人史相容、初始知识不越权、失败不改实例", evidence


# ---------------------------------------------------------------- C15

def c15_join_rollback(ctx: Ctx) -> tuple[str, str, str]:
    """C15 回滚跨过加入点即该角色在本线退出、分叉继承、冻结线补入不激活。"""
    world = ctx.service()
    info, timeline_id, first = mk(ctx, world)
    world.activate(info["id"], timeline_id, now_real=1.7e9)
    before_join = world.commit(info["id"], timeline_id, note="补卡前")
    world.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    card, second, joined = add_second(ctx, world, info, timeline_id)
    after_join = world.commit(info["id"], timeline_id, note="补卡后")
    say(ctx.store, info, timeline_id, second, env="env-b15", text="问", reply="答")
    ctx.store.memory_add({
        "id": "mm-b15", "instance_id": info["id"], "timeline_id": timeline_id, "character_id": second,
        "text": "她的私事", "kind": "self", "sources": [{"kind": "dialog", "ref": "x"}],
        "happened_world": 0, "learned_world": 0, "recorded_world": 0, "semantic_watermark": 0,
        "strength": 0.5, "confidence": 0.5,
    })
    joined_before = len(ctx.store.character_join_list(info["id"], timeline_id))
    rolled = world.rollback(info["id"], timeline_id, commit_id=before_join["id"], now_real=1.7e9 + 3 * DAY)
    joins_after = ctx.store.character_join_list(info["id"], timeline_id)
    watermark = int(rolled["world"])
    visible = [str((item.get("meta") or {}).get("card_id")) for item in
               world.cards(ctx.store.instance_get(info["id"]), timeline_id=timeline_id, world_seconds=watermark)]
    memory_gone = ctx.store.memory_get("mm-b15", instance_id=info["id"], timeline_id=timeline_id) is None
    cards_setting = json.loads(ctx.store.instance_get(info["id"])["setting"])["cards"]

    branch = world.fork(info["id"], timeline_id, commit_id=after_join["id"], name="补卡后分支")
    branch_card = world.cards(ctx.store.instance_get(info["id"]), timeline_id=branch["timeline"]["id"])
    inherited = any(str((item.get("meta") or {}).get("card_id")) == second for item in branch_card)

    frozen_world = ctx.service()
    frozen_info, frozen_tl, _ = mk(ctx, frozen_world)
    frozen_card, frozen_id, frozen_join = add_second(ctx, frozen_world, frozen_info, frozen_tl, name="冻结线新人")
    frozen_state = ctx.store.timeline_get(frozen_tl)["state"]
    frozen_clock = int(ctx.store.clock_get(frozen_tl)["processed_world"])
    frozen_before = counts(ctx.store, frozen_info["id"])["event"]
    frozen_world.advance(frozen_info["id"], frozen_tl, now_real=1.7e9 + 10 * DAY)
    frozen_after = counts(ctx.store, frozen_info["id"])["event"]

    ok = (joined_before == 1 and joins_after == [] and second not in visible
          and all(str((item.get("meta") or {}).get("card_id")) == first for item in cards_setting)
          and memory_gone and inherited and frozen_state == "frozen"
          and frozen_after == frozen_before and frozen_join["timeline_state"] == "frozen")
    evidence = (
        f"回滚跨过加入点：成员资格 {joined_before}→{len(joins_after)}、她的记忆随之撤销={memory_gone}、"
        f"回滚水位可选角色={visible}；实例级角色定义仍保留（setting.cards={len(cards_setting)} 张）；"
        f"从补入后的提交分叉 → 新线继承她={inherited}；冻结线补入后 state={frozen_state}、推进后事件数不变"
        f"（{frozen_before}→{frozen_after}）且补入结果标注 timeline_state={frozen_join['timeline_state']}"
    )
    return ("PASS" if ok else "FAIL"), "补卡可回滚（本线退出 / 分叉继承），冻结线补入不激活该线", evidence


# ---------------------------------------------------------------- C16

def c16_isolation(ctx: Ctx) -> tuple[str, str, str]:
    """C16 补入角色与既有角色默认隔离；用户声明「已相识」不改变这一点。"""
    world = ctx.service()
    info, timeline_id, first = mk(ctx, world)
    world.activate(info["id"], timeline_id, now_real=1.7e9)
    world.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    card, second, joined = add_second(ctx, world, info, timeline_id, acquainted=True)
    secret = "堤长私吞了修堤粮，这事只有我知道"
    say(ctx.store, info, timeline_id, first, env="env-a16", text="你听说了吗", reply=secret)
    say(ctx.store, info, timeline_id, second, env="env-b16", text="今天滩上风大", reply="嗯，风大")
    ctx.store.memory_add({
        "id": "mm-a16", "instance_id": info["id"], "timeline_id": timeline_id, "character_id": first,
        "text": secret, "kind": "fragment", "sources": [{"kind": "dialog", "ref": "x"}],
        "happened_world": 0, "learned_world": 0, "recorded_world": 0, "semantic_watermark": 0,
        "strength": 0.9, "confidence": 0.9,
    })
    seen = world.recall(info["id"], timeline_id, second, topic="堤长 修堤粮")
    context = world.turn_context({"instance_id": info["id"], "timeline_id": timeline_id, "character_id": second}, topic="堤长")
    grants = world.disclosures(info["id"], timeline_id)
    fragments = world.disclosed_fragments(info["id"], timeline_id, second)
    her_units = [row for row in ctx.store.unit_list(info["id"], timeline_id, second)
                 if row["mode"] == "dialog" and "已相识" in str(row["semantic"])]
    sessions = ctx.store.instance_sessions(info["id"])
    session_ids = {str(item["character_id"]) for item in sessions}
    no_cross = (secret not in blob(seen) and secret not in context["prompt"]
                and grants == [] and fragments == [])

    ok = no_cross and joined["acquainted"] is True and session_ids == {first, second} and bool(her_units)
    evidence = (
        f"「已相识」声明已记录（acquainted={joined['acquainted']}）：B 的召回 / 扮演定义均无 A 的私聊={no_cross}、"
        f"未产生任何披露授权={grants}；两人各有独立会话 {sorted(session_ids)}；"
        f"声明只落在她自己的对话单元（{'与联络者已相识' if her_units else '未写入'}）"
    )
    return ("PASS" if ok else "FAIL"), "补入角色与既有角色默认隔离，用户声明「已相识」不改变隔离", evidence


# ---------------------------------------------------------------- 附录 D

def d1_at_least_one_source(ctx: Ctx) -> tuple[str, str, str]:
    """附录 D 阻断项：史料（传本）至少一部。"""
    package = example_package()
    package["historiography"] = []
    package["initial_state"]["rumors"] = []
    card = example_card(package)
    # 角色只凭实情层条目接触世界内容（不含任何史料引用），隔离「零传本」这一个变量
    card["initial_knowledge"] = [
        {"ref_type": "canon", "ref_id": "cf-1", "obtained_at": DAY * 1200},
        {"ref_type": "self", "claim": "她记得崩堤那年的盐味。"},
    ]
    errors = validate_package(package)
    blocked = any("historiography" in item for item in errors)
    created = None
    if not blocked:
        try:
            created = create_instance(ctx.store, package, [card])
        except InstanceError as exc:
            created = f"InstanceError：{str(exc)[:40]}"
    evidence = (
        f"传本数=0 的包：validate_package 返回 {len(errors)} 条错误（史料类 {blocked}）：{errors[:2]}；"
        f"创建结果={created['id'] if isinstance(created, dict) else created}"
        f"（validate.py:477-480 只校验 historiography 是不是列表，空列表通过；附录 D 史料行标为阻断条件）"
    )
    return ("PASS" if blocked else "FAIL"), "附录 D 阻断项「至少一部有效传本」在创建前生效", evidence


CHECKS: list[tuple[str, str, Callable[[Ctx], tuple[str, str, str]]]] = [
    ("C1", "锁定实例与源文件脱钩 / 不可改 / 补卡只增（附录 C #1）", c01_locked_and_source_decoupled),
    ("C2", "新建与修订共用校验、失败不覆盖（附录 C #2）", c02_shared_validation),
    ("C2d", "两端（桌面 / 安卓）解释同一包一致（附录 C #2）", c02d_android_parity),
    ("C3", "创建 / 导入 / 重命名并发唯一、原始名称不被取代（附录 C #3）", c03_unique_names),
    ("C4", "多线导出再导入恢复对话 / 披露 / 记忆 / 引用（附录 C #4）", c04_export_import_roundtrip),
    ("C5", "导入全冻结、无运输期补算、不恢复 Key / 绑定（附录 C #5）", c05_import_frozen_no_transport_catchup),
    ("C6", "坏容器原子失败、已有实例不变（附录 C #6）", c06_atomic_failure),
    ("C6d", "转换器失败保留原包（附录 C #6）", c06d_converter),
    ("C7", "无凭据 / 激活状态，管理面 / 日志 / 导入错误不泄露（附录 C #7）", c07_no_leak),
    ("C8", "改公理需新实例、改局势需新线、聊天不隐式触发（附录 C #8）", c08_axiom_change_needs_new_instance),
    ("C9", "回填不推翻已确认内容、留白合法、越权知识不能创建（附录 C #9）", c09_backfill_respects_confirmed),
    ("C10", "名册与引用：未登记即失败、纯署名不造生平（附录 C #10）", c10_registry_and_references),
    ("C11", "最低标准：稀缺内容不拒、空壳 / 非法寿命 / 越权知识仍失败（附录 C #11）", c11_minimum_content),
    ("C12", "草稿与用量：无 Key 可用、达上限暂停、两类导入不混淆（附录 C #12）", c12_draft_and_budget),
    ("C13", "未声明制度 / 惯例不被拒也不补造（附录 C #13）", c13_undeclared_institutions),
    ("C14", "补卡：不凭空出现、锚定补入时刻、失败不改实例（附录 C #14）", c14_add_character),
    ("C15", "补卡可回滚、分叉继承、冻结线补入不激活（附录 C #15）", c15_join_rollback),
    ("C16", "补入角色与既有角色默认隔离（附录 C #16）", c16_isolation),
    ("D1", "附录 D 阻断项：至少一部有效传本", d1_at_least_one_source),
]


def main() -> int:
    tally = {"PASS": 0, "FAIL": 0, "DEFERRED": 0, "SKIP": 0}
    lines: list[str] = []
    only = [item for item in sys.argv[1:]] or None
    for label, summary, check in CHECKS:
        if only and label not in only:
            continue
        ctx = Ctx(label.lower())
        try:
            status, text, evidence = check(ctx)
        except Exception:
            traceback.print_exc()
            status, text, evidence = "FAIL", summary, "探针异常：" + traceback.format_exc(limit=8).replace("\n", " | ")[:700]
        finally:
            ctx.close()
        tally[status] = tally.get(status, 0) + 1
        line = f"{status} [{label}] {text} — {evidence}"
        lines.append(line)
        print(line, flush=True)
    total = sum(tally.values())
    print(
        f"TOTAL {total} PASS {tally['PASS']} FAIL {tally['FAIL']} DEFERRED {tally['DEFERRED']}"
        + (f" SKIP {tally['SKIP']}" if tally["SKIP"] else ""),
        flush=True,
    )
    return 1 if tally["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
