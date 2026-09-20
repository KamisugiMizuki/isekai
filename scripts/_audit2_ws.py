#!/usr/bin/env python
"""独立行为探针 2：WORLD_SETTING_SPEC 逐条实测（真 WS 核心 + 真 SQLite，只换 LLM）。

用法：
    cd /d/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_audit2_ws.py
    .venv/Scripts/python.exe scripts/_audit2_ws.py --list
    .venv/Scripts/python.exe scripts/_audit2_ws.py --only A1,F2

独立性说明（与 scripts/_audit_world_setting.py 的区别）：
- 夹具是探针自己写的极简世界包 / 角色卡（不依赖 isekai_core.world.example），
  顺便把「附录 D 最小内容标准」当成实测对象。
- 实例创建 / 导入 / 补卡 / 对话尽量走真实核心的管理面与 UMP 通道（真 WebSocket），
  不直接调 setting 层函数，减少「测的是探针的调用方式」这类假证据。
- 所有状态落在 tempfile 临时目录；不读 config/config.yaml 的 key；不联网（FakeLLM）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from isekai_core import log as logmod  # noqa: E402
from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.client import MgmtClient, UmpClient  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.ump import UmpError  # noqa: E402
from isekai_core.world import ops as world_ops  # noqa: E402
from isekai_core.world.instances import InstanceError, create_instance, rename_instance  # noqa: E402
from isekai_core.world.package import (  # noqa: E402
    normalize_name,
    save_package,
    template_package,
    unique_name,
)
from isekai_core.world.portable import (  # noqa: E402
    build_container,
    check_compatibility,
    import_instance,
    write_export,
)
from isekai_core.world.validate import (  # noqa: E402
    MAX_COLLECTION,
    MAX_DEPTH,
    MAX_NODES,
    MAX_STRING,
    validate_package,
)

DAY = 86400
REPLIES = [
    "探针回复甲：井绳是今天早上换的。",
    "探针回复乙：我记下了，等下次汲水再对一遍。",
    "探针回复丙：这事我只跟你说过。",
]
#: 内部正文探针串（事件详情 / 记忆）——不得出现在普通日志与导入错误里
SECRET_EVENT = "内部事件正文-勿泄漏-7f3a9b21"
SECRET_MEMORY = "内部记忆正文-勿泄漏-4c8d1e02"


def clone(payload: Any) -> Any:
    return json.loads(json.dumps(payload, ensure_ascii=False))


# --------------------------------------------------------------------------- 夹具

def mypkg(
    moment: int = DAY * 1500 + DAY * 3 // 4,
    *,
    name: str = "砚纪",
    density: str = "常规",
    families: int = 1,
    mysteries: int = 0,
    institutions: bool = False,
    customs: bool = False,
    empty_units: bool = False,
    bad_lifespan: bool = False,
    requires: list[str] | None = None,
) -> dict[str, Any]:
    """探针自建的世界包：默认就是「只有一族事件、无谜题、无制度 / 惯例」的最小合法包。"""
    package: dict[str, Any] = {
        "meta": {
            "schema": "1.0",
            "package_id": "wp-audit2-fixed",
            "original_name": name,
            "display_name": name,
            "description": "一口井与一道坡。",
            "density": "normal",
        },
        "calendar": {
            "era": "砚纪",
            "day_seconds": DAY,
            "months": [{"name": "一月", "days": 20}, {"name": "二月", "days": 20}],
            "week": {"name": "旬", "days": 5},
            "segments": [
                {"id": "seg-night", "name": "夜", "start": 0, "end": DAY // 2},
                {"id": "seg-day", "name": "昼", "start": DAY // 2, "end": DAY},
            ],
            "initial_moment": moment,
        },
        "world": {
            "axioms": [{"id": "ax-1", "text": "井水只在晨间可饮。"}],
            "geography": "坡上一口井，坡下两户人家。",
            "society": "以井绳与水位尺为界的两户人家。",
            "lexicon": {"note": "村名两字。", "terms": [{"term": "井胥", "meaning": "管井的人"}]},
        },
        "environment": {"types": []},
        "sources": [
            {"id": "src-1", "kind": "personal", "name": "坡上口信", "reach": "在坡口遇见送水的人即可听到"}
        ],
        "canon": [{"id": "cf-1", "statement": "井水在三十年前干过一次。", "tags": ["灾害"]}],
        "narratives": [
            {
                "id": "nv-1",
                "text": "干井那年是先人取水不敬，井神收回了水。",
                "source_id": "src-1",
                "canon_ref": "cf-1",
                "obtain": ["在坡口听老人讲"],
                "confidence": "believed",
            }
        ],
        "entities": [
            {"id": "en-1", "kind": "person", "name": "井胥", "race_id": "rc-1", "born": 0, "died": None},
            {"id": "en-2", "kind": "place", "name": "坡上井", "race_id": None, "born": None, "died": None},
        ],
        "races": [
            {"id": "rc-1", "name": "井民", "lifespan": {"min_years": 50, "max_years": 70}},
        ],
        "historiography": [
            {
                "id": "hs-1",
                "title": "井志",
                "contributors": [{"name": "无名录事", "role": "编", "period": "成书前十年"}],
                "written_at": moment - 10 * DAY,
                "compiled_at": moment - 5 * DAY,
                "coverage": {"from": 0, "to": moment - 5 * DAY},
                "genre": "民间志",
                "stance": "中立",
                "entries": ["cf-1", "nv-1"],
            }
        ],
        "events": {
            "density": density,
            "families": [
                {
                    "id": f"ef-{index + 1}",
                    "name": f"井事{index + 1}",
                    "templates": [
                        {
                            "id": f"et-{index + 1}",
                            "summary": "井绳断了半日",
                            "preconditions": ["cf-1"],
                            "effects": [
                                {"kind": "source_delay", "target": "src-1", "expiry": "with_cause"}
                            ],
                            "weight": 1,
                        }
                    ],
                }
                for index in range(families)
            ],
        },
        "life": [
            {
                "id": "lf-1",
                "name": "汲水日常",
                "sleep": True,
                "windows": [
                    {"start": 0, "end": DAY // 2, "activity": "sleep"},
                    {"start": DAY // 2, "end": DAY, "activity": "duty"},
                ],
            }
        ],
        "roles": [
            {
                "id": "rl-1",
                "name": "管井人",
                "description": "记水位的人。",
                "life_template": "lf-1",
                "channels": ["src-1"],
            }
        ],
        "comms": {"mechanisms": [{"id": "cm-1", "name": "水牌", "limits": "只在汲水时交换水牌，不进山"}]},
        "initial_state": {
            "events": [],
            "rumors": ["nv-1"],
            "mysteries": [
                {"id": f"my-{index + 1}", "question": "干井那年到底是谁先取的？", "refs": ["cf-1"]}
                for index in range(mysteries)
            ],
        },
    }
    if institutions:
        package["world"]["institutions"] = [
            {
                "id": "inst-1",
                "name": "井社",
                "mandate": "定汲水次序与修绳人力",
                "scope": "坡上两户",
                "succession": "管井人身故时由另一户推举，空缺期间汲水照旧、修绳暂停",
                "validity": "自干井那年起",
                "offices": [{"id": "off-1", "name": "井胥", "holder": "en-1"}],
                "vacancy_policy": {"continues": ["汲水次序"], "suspended": ["修绳"]},
            }
        ]
    if customs:
        package["world"]["customs"] = [
            {
                "id": "cus-1",
                "name": "晨汲礼",
                "applies_to": "两户人家",
                "practice": "晨间先量水位再取水",
                "basis": "干井那年留下的规矩",
                "variation": "可换用具，顺序不改",
                "forms": ["晨间先量水位再取水", "晨间先敲井栏再取水"],
            }
        ]
    if empty_units:
        package["historiography"][0]["entries"] = []
    if bad_lifespan:
        package["races"][0]["lifespan"] = {"min_years": 60, "max_years": 12}
    if requires is not None:
        package["meta"]["requires"] = requires
    return package


def mycard(
    package: dict[str, Any],
    name: str = "井栖",
    *,
    born: int | None = None,
    confirmed: bool = True,
    obtained_at: int | None = None,
    knowledge: list[dict[str, Any]] | None = None,
    died: int | None = None,
    role_id: str | None = "rl-1",
) -> dict[str, Any]:
    calendar = package["calendar"]
    moment = int(calendar["initial_moment"])
    year = sum(int(item["days"]) for item in calendar["months"]) * int(calendar["day_seconds"])
    if knowledge is None:
        knowledge = [
            {
                "ref_type": "canon",
                "ref_id": "cf-1",
                "obtained_at": moment - 4 * DAY if obtained_at is None else obtained_at,
            }
        ]
    card: dict[str, Any] = {
        "meta": {"schema": "1.0", "card_id": f"cc-{name}", "confirmed": confirmed},
        "identity": {
            "name": name,
            "race_id": "rc-1",
            "born": moment - 20 * year if born is None else born,
            "gender": "女",
            "occupation": "管井人",
            "self_identity": "记水位的人，不算官。",
        },
        "background": {"creator": "她家的旧水位册还在梁上。", "self_knowledge": "她记得干井那年的咸味。"},
        "region": "坡上",
        "role_id": role_id,
        "channels": [{"source_id": "src-1", "conditions": "凭管井人身份在坡口听口信"}],
        "initial_knowledge": knowledge,
        "comms": [{"mechanism_id": "cm-1", "note": "汲水时递水牌"}],
        "first_contact": {"stance": "谨慎但不回避", "intent": "先弄清对方从哪道坡上来"},
        "initial_units": [
            {"id": "iu-1", "semantic": "先看水位再答话", "driver": "anchor", "confidence": 0.9, "basis": "十年记水位"},
        ],
        "cognition": {"mode": "soft", "sources": ["self_experience", "user_contact"]},
        "life_template": {
            "sleep": True,
            "routine_note": "晨汲日提前半个时辰上坡。",
            "windows": [
                {"start": 0, "end": DAY // 2, "activity": "sleep"},
                {"start": DAY // 2, "end": DAY, "activity": "duty"},
            ],
        },
        "appearance": "袖口常年有水渍。",
    }
    if died is not None:
        card["identity"]["died"] = died
    return card


# --------------------------------------------------------------------------- 基础设施

def q(store: Store, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return store._conn.execute(sql, args).fetchall()


def n(store: Store, sql: str, args: tuple[Any, ...] = ()) -> int:
    row = store._conn.execute(sql, args).fetchone()
    return int(row[0]) if row else 0


def inst_counts(store: Store, instance_id: str) -> dict[str, int]:
    sub = "SELECT id FROM session WHERE instance_id=?"
    out = {"instance": n(store, "SELECT COUNT(*) FROM instance")}
    for table in (
        "timeline", "session", "memory", "knowledge", "disclosure", "character_join", "event",
        "claim", "life_plan", "unit", "commit_log", "commit_snapshot", "pending_event", "effect_state",
        "institution_state", "custom_state", "environment_state",
    ):
        out[table] = n(store, f"SELECT COUNT(*) FROM {table} WHERE instance_id=?", (instance_id,))
    out["rate_command"] = n(
        store,
        "SELECT COUNT(*) FROM rate_command WHERE timeline_id IN (SELECT id FROM timeline WHERE instance_id=?)",
        (instance_id,),
    )
    out["timeline_clock"] = n(
        store, "SELECT COUNT(*) FROM timeline_clock WHERE timeline_id IN (SELECT id FROM timeline WHERE instance_id=?)",
        (instance_id,),
    )
    out["message"] = n(store, f"SELECT COUNT(*) FROM message WHERE session_id IN ({sub})", (instance_id,))
    out["thread"] = n(store, f"SELECT COUNT(*) FROM thread WHERE session_id IN ({sub})", (instance_id,))
    out["delivery"] = n(
        store,
        f"SELECT COUNT(*) FROM delivery WHERE msg_seq IN (SELECT seq FROM message WHERE session_id IN ({sub}))",
        (instance_id,),
    )
    out["memory_citation"] = n(
        store, "SELECT COUNT(*) FROM memory_citation WHERE timeline_id IN (SELECT id FROM timeline WHERE instance_id=?)",
        (instance_id,),
    )
    out["channel_bound_messages"] = n(
        store,
        f"SELECT COUNT(*) FROM message WHERE channel_id IS NOT NULL AND session_id IN ({sub})",
        (instance_id,),
    )
    return out


@dataclass
class Ctx:
    root: Path
    cfg: Any
    runtime: Any
    mgmt: MgmtClient
    llm: Any
    _credential: str | None = None

    @property
    def store(self) -> Store:
        return self.runtime.store

    @property
    def world(self) -> Any:
        return self.runtime.world

    def write(self, name: str, payload: Any) -> Path:
        path = self.cfg.paths.packages / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    async def credential(self, channel: str = "builtin") -> str:
        if self._credential is None:
            self._credential = (await self.mgmt.call("channel.ensure", name=channel))["credential"]
        return self._credential

    async def send(self, *, instance: str, timeline: str, character: str, text: str,
                   channel: str = "builtin", thread: str = "dm-1") -> tuple[UmpClient, dict, dict]:
        """真 WS 送一条消息并取回回复。"""
        credential = await self.credential(channel)
        session = (await self.mgmt.call(
            "session.ensure", instance_id=instance, timeline_id=timeline, character_id=character
        ))["session"]
        bound = (await self.mgmt.call(
            "thread.bind", channel=channel, thread_id=thread, session_id=session["id"]
        ))["thread"]
        client = UmpClient(endpoint=self.runtime.server.endpoint, channel_id=channel, name=channel, credential=credential)
        await client.connect()
        env_id = await client.send_user_message(
            thread_id=thread, binding_token=bound["binding_token"], text=text
        )
        collected: list[Any] = []
        try:
            reply = await client.expect(lambda e: e.type == "reply", timeout=200, collect=collected)
        except TimeoutError as exc:
            detail = [
                f"{item.type}:{'/'.join(str(value) for value in (item.payload or {}).values())[:80]}"
                for item in collected
            ]
            raise RuntimeError(f"等待回复超时（env={env_id}，队列已见 {detail}）") from exc
        return client, {"session": session, "thread": thread, "env_id": env_id}, reply.payload


@asynccontextmanager
async def core(root: Path, *, replies: list[str] | None = None):
    cfg = load_config(root)
    cfg.paths.packages.mkdir(parents=True, exist_ok=True)
    llm = FakeLLM(replies or REPLIES)
    runtime = await build_runtime(cfg, llm=llm)
    endpoint = await runtime.server.start()
    mgmt = MgmtClient(endpoint, runtime.server.mgmt_token)
    await mgmt.connect()
    ctx = Ctx(root=root, cfg=cfg, runtime=runtime, mgmt=mgmt, llm=llm)
    try:
        yield ctx
    finally:
        await mgmt.close()
        await runtime.service.shutdown()
        await runtime.server.close()
        runtime.store.close()


def tmpdir(tag: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"audit2ws-{tag}-"))


# --------------------------------------------------------------------------- 检查

@dataclass
class Check:
    cid: str
    clause: str
    expected: str
    fn: Any
    code_ref: str


async def a1_source_decoupled(ctx: Ctx) -> tuple[str, str]:
    """附录 C1 上半：改 / 删源模板与源角色卡后，既有实例内容不变。"""
    package = mypkg()
    card = mycard(package)
    pkg_path = ctx.write("world.json", package)
    card_path = ctx.write("card.json", card)
    info = (await ctx.mgmt.call(
        "instance.create", package_path=str(pkg_path), card_paths=[str(card_path)]
    ))["instance"]
    before = json.dumps(json.loads(ctx.store.instance_get(info["id"])["setting"]), sort_keys=True, ensure_ascii=False)

    edited = mypkg()
    edited["calendar"]["day_seconds"] = 12345
    edited["calendar"]["months"][0]["days"] = 7
    edited["world"]["axioms"][0]["text"] = "被改写后的公理。"
    save_package(pkg_path, edited)
    edited_card = mycard(edited, name="改过名字的人")
    save_package(card_path, edited_card)

    after_edit = json.dumps(json.loads(ctx.store.instance_get(info["id"])["setting"]), sort_keys=True, ensure_ascii=False)
    pkg_path.unlink()
    card_path.unlink()
    after_delete = json.dumps(json.loads(ctx.store.instance_get(info["id"])["setting"]), sort_keys=True, ensure_ascii=False)
    setting = json.loads(ctx.store.instance_get(info["id"])["setting"])

    # 源文件没了，实例仍可导出（§3.3：来源名称不是运行依赖）
    export = tmpdir("a1") / "out.isekai.json"
    (await ctx.mgmt.call("instance.export", id=info["id"], path=str(export)))["manifest"]

    ok = (
        before == after_edit == after_delete
        and setting["world_package"]["calendar"]["day_seconds"] == DAY
        and setting["cards"][0]["identity"]["name"] == "井栖"
        and export.exists()
    )
    return ("PASS" if ok else "FAIL"), (
        f"改源包（日长→12345）+ 改源卡（改名）+ 删两个源文件后，实例 setting 逐字节不变={before == after_edit == after_delete}；"
        f"锁定日长={setting['world_package']['calendar']['day_seconds']}、锁定卡名={setting['cards'][0]['identity']['name']}；"
        f"源文件删除后仍可导出={export.exists()}"
    )

async def a2_locked_no_writer(ctx: Ctx) -> tuple[str, str]:
    """附录 C1 下半：锁定实例不能改历法或改既有角色卡（管理面没有写入口）。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call(
        "instance.create", package=package, cards=[card]
    ))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    character = card["meta"]["card_id"]
    ctx.world.activate(info["id"], timeline, now_real=1.7e9)
    client, meta, _ = await ctx.send(instance=info["id"], timeline=timeline, character=character, text="井绳换了吗")
    await client.close()
    ctx.world.advance(info["id"], timeline, now_real=1.7e9 + DAY)
    await ctx.mgmt.call("runtime.card.add", instance_id=info["id"], timeline_id=timeline, card=mycard(package, "桑叶"))
    after = json.loads(ctx.store.instance_get(info["id"])["setting"])

    # 管理面操作全集里，没有任何一个能用「写设定」的方式改到实例（下列为全部可用 op）
    all_ops = sorted(set(world_ops.SYNC_OPS) | set(world_ops.ASYNC_OPS))
    writers = [
        op for op in all_ops
        if re.search(r"(axiom|calendar|setting|snapshot|lock|history)", op)
        and re.search(r"\.(set|write|update|edit|patch|delete|save)$", op)
    ]
    ok = (
        after["world_package"]["calendar"] == package["calendar"]
        and after["cards"][0]["identity"] == card["identity"]
        and after["cards"][0]["initial_units"][0]["confidence"] == 0.9
        and not writers
    )
    calendar_same = after["world_package"]["calendar"] == package["calendar"]
    identity_same = after["cards"][0]["identity"] == card["identity"]
    return ("PASS" if ok else "FAIL"), (
        f"对话 + 推进 + 补卡之后：历法段{'一致' if calendar_same else '被改过'}、"
        f"既有角色卡身份段{'一致' if identity_same else '被改过'}、"
        f"性格锚点置信度仍为 {after['cards'][0]['initial_units'][0]['confidence']}；"
        f"管理面 op 全集 {len(all_ops)} 个，含 set/write/update 类={writers or '无'}"
    )


async def a3_add_card_extend_only(ctx: Ctx) -> tuple[str, str]:
    """附录 C1 末句 / §3.4：补卡只扩充角色集合。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    watermark_before = int(ctx.store.clock_get(timeline)["processed_world"])
    result = (await ctx.mgmt.call(
        "runtime.card.add", instance_id=info["id"], timeline_id=timeline, card=mycard(package, "桑叶")
    ))["join"]
    setting = json.loads(ctx.store.instance_get(info["id"])["setting"])
    joins = q(ctx.store, "SELECT * FROM character_join WHERE instance_id=?", (info["id"],))
    watermark_after = int(ctx.store.clock_get(timeline)["processed_world"])

    snapshot_ids = [str((c.get("meta") or {}).get("card_id")) for c in setting["cards"]]
    ok = (
        len(setting["cards"]) == 2 and "cc-桑叶" in snapshot_ids  # §3.7：定义写进实例设定快照
        and len(joins) == 1
        and str(joins[0]["character_id"]) == "cc-桑叶"
        and watermark_before == watermark_after
        and result["timeline_state"] == "frozen"
    )
    return ("PASS" if ok else "FAIL"), (
        f"补入「桑叶」：实例快照角色数 {len(setting['cards'])}={snapshot_ids}（原 1，§3.7 要求定义进快照）、"
        f"成员资格行 {len(joins)} 条（角色={joins[0]['character_id'] if joins else '无'}，加入水位={joins[0]['joined_world'] if joins else '-'}）、"
        f"线状态仍 {result['timeline_state']}、水位 {watermark_before}→{watermark_after}（未推进）"
    )


async def a4_join_trace_and_commit(ctx: Ctx) -> tuple[str, str]:
    """§3.7 版本与生命周期：补卡在三处留痕（快照定义 / 成员资格 / 加入提交）。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    commits_before = [row["id"] for row in ctx.store.commit_list(info["id"])]
    await ctx.mgmt.call("runtime.card.add", instance_id=info["id"], timeline_id=timeline, card=mycard(package, "桑叶"))
    commits_after = [row["id"] for row in ctx.store.commit_list(info["id"])]
    setting = json.loads(ctx.store.instance_get(info["id"])["setting"])
    definition = [c for c in setting["cards"] if (c.get("meta") or {}).get("card_id") == "cc-桑叶"]

    ok = bool(definition) and len(commits_after) > len(commits_before)
    return ("PASS" if ok else "FAIL"), (
        f"补卡后：实例设定快照里的角色定义={len(definition)} 处（§3.7 要求「定义写入实例设定快照」）、"
        f"提交数 {len(commits_before)}→{len(commits_after)}（§3.7 要求在目标线原子创建加入提交）；"
        f"定义只存在于 character_join 行（settings.cards 仍是 {len(setting['cards'])} 张）"
    )


async def a5_join_idempotent(ctx: Ctx) -> tuple[str, str]:
    """§3.7 版本与生命周期末条：同一补卡请求重试返回原子发布结果，不重复登记。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    spec = {"instance_id": info["id"], "timeline_id": timeline, "card": mycard(package, "桑叶"), "request_id": "req-join-1"}
    first = await ctx.mgmt.call("runtime.card.add", **spec)
    before = {k: inst_counts(ctx.store, info["id"])[k] for k in ("character_join", "unit", "life_plan")}
    second: dict[str, Any] = {}
    try:
        second = await ctx.mgmt.call("runtime.card.add", **spec)
    except UmpError as exc:
        second = {"error": f"{exc.code}: {exc}"}
    after = {k: inst_counts(ctx.store, info["id"])[k] for k in ("character_join", "unit", "life_plan")}
    same = bool(second.get("join")) and second.get("join", {}).get("character") == first["join"]["character"]

    ok = same and before == after
    return ("PASS" if ok else "FAIL"), (
        f"用同一 request_id 重试补卡：第二次返回={second.get('error') or '原子发布结果'}；"
        f"成员资格 / 单元 / 计划行数 {before}→{after}（未重复登记={before == after}）；"
        f"实现里没有 request 标识参数（ops.py:250-264 只转发 card/note/acquainted）"
    )


async def a6_join_event(ctx: Ctx) -> tuple[str, str]:
    """§3.7 目标线与加入事件：补卡时可自定义一个世界事件并登记为世界事件。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    events_before = inst_counts(ctx.store, info["id"])["event"]
    join_event = {
        "summary": "坡上来了一封信，指名找她",
        "effects": [{"kind": "public_notice", "target": "src-1", "expiry": "with_cause"}],
    }
    result = (await ctx.mgmt.call(
        "runtime.card.add", instance_id=info["id"], timeline_id=timeline,
        card=mycard(package, "桑叶"), event=join_event,
    ))["join"]
    events_after = inst_counts(ctx.store, info["id"])["event"]

    ok = bool(result.get("event")) and events_after > events_before
    return ("PASS" if ok else "FAIL"), (
        f"补卡时提供加入事件：返回里的事件字段={result.get('event')}；事件行数 {events_before}→{events_after}；"
        f"add_character（service.py:2835-2944）只收 now_real/joined_world/note/acquainted，无事件参数，"
        f"args 里多给的 event 被静默忽略"
    )


async def b1_shared_validation(ctx: Ctx) -> tuple[str, str]:
    """附录 C2 上半：新建与修订共用校验；失败 / 取消不覆盖确认版本。"""
    from isekai_core.world import generator as gen

    shared = gen.validate_package is validate_package
    package = mypkg()
    ctx.llm.replies = [json.dumps(package, ensure_ascii=False)]
    good = await ctx.mgmt.call("world.package.generate", brief="井边世界", name="砚纪", timeout=60)
    ctx.llm.replies = ["这不是 JSON"]
    garbage = await ctx.mgmt.call("world.package.generate", brief="说不清楚", timeout=60)
    target = ctx.cfg.paths.packages / "confirmed.json"
    save_package(target, package)
    frozen = target.read_text(encoding="utf-8")
    refused = ""
    try:
        broken = mypkg()
        broken["world"]["axioms"] = []
        await ctx.mgmt.call("world.package.save", path=str(target), package=broken)
    except UmpError as exc:
        refused = str(exc)
    intact = target.read_text(encoding="utf-8") == frozen
    candidates = sorted(p.name for p in ctx.cfg.paths.packages.iterdir() if "candidate" in p.name)

    ok = shared and good["errors"] == [] and bool(garbage["errors"]) and bool(refused) and intact and not candidates
    return ("PASS" if ok else "FAIL"), (
        f"generator.validate_package is validate.validate_package={shared}；生成正常包 errors={good['errors']}；"
        f"畸形输出回灌错误 {len(garbage['errors'])} 条；非法包落盘被拒（{refused[:28]}…）且原文件逐字节不变={intact}；"
        f"候选落盘文件={candidates or '无'}"
    )


async def c1_naming_unique(ctx: Ctx) -> tuple[str, str]:
    """附录 C3 / §7.4：创建 / 导入 / 重命名并发下仍全局唯一；改名不取代原始名称记录。"""
    package = mypkg()
    card = mycard(package)
    names: list[str] = []
    errors: list[str] = []

    def worker(index: int) -> None:
        try:
            info = create_instance(ctx.store, clone(package), [clone(card)], display_name="同名")
            names.append(info["name"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    unique = sorted(names)
    expected = ["同名", "同名_2", "同名_3", "同名_4", "同名_5"]
    conflict = ""
    same = False
    empty = ""
    first_row = next((row for row in ctx.store.instance_list() if row["name"] == "同名"), None)
    if first_row is not None:
        try:
            rename_instance(ctx.store, first_row["id"], "同名_2")
        except InstanceError as exc:
            conflict = str(exc)
        same = rename_instance(ctx.store, first_row["id"], " 同名 ")["name"] == "同名"
        try:
            rename_instance(ctx.store, first_row["id"], "   ")
        except InstanceError as exc:
            empty = str(exc)
    ok = unique == expected and not errors and bool(conflict) and same and bool(empty)
    return ("PASS" if ok else "FAIL"), (
        f"5 线程并发建同名实例 → 名称={unique}（期望 {expected}）；异常={errors or '无'}；"
        f"重命名撞名被拒={bool(conflict)}（{conflict[:24]}…）、规范化等价名（' 同名 '）允许={same}、空白名被拒={bool(empty)}；"
        f"比较规则统一走 package.normalize_name（NFKC+casefold，§7.4）"
    )


async def c2_original_name(ctx: Ctx) -> tuple[str, str]:
    """§7.2：原始名称独立记录，改显示名不改它；创建 / 导出嵌入，导入优先读取。"""
    package = mypkg(name="砚纪")
    card = mycard(package)
    path = ctx.write("orig.json", package)
    first = (await ctx.mgmt.call("instance.create", package_path=str(path), card_paths=[
        str(ctx.write("orig-card.json", card))
    ]))["instance"]
    edited = mypkg(name="改了显示名的世界")
    edited["meta"]["original_name"] = "砚纪"  # 不改原始名称记录
    save_package(path, edited)

    package2 = mypkg(name="砚纪")
    card2 = mycard(package2, "桑叶")
    second = (await ctx.mgmt.call("instance.create", package=package2, cards=[card2], display_name="自定义名"))["instance"]

    export = tmpdir("c2") / "b.isekai.json"
    await ctx.mgmt.call("instance.export", id=first["id"], path=str(export))
    container = json.loads(export.read_text(encoding="utf-8"))
    renamed_file = tmpdir("c2") / "文件名与世界名无关.isekai.json"
    shutil.copy(export, renamed_file)
    rename_instance(ctx.store, first["id"], "换个名字腾出位置")
    imported = (await ctx.mgmt.call("instance.import", path=str(renamed_file)))["instance"]

    got = json.loads(ctx.store.instance_get(first["id"])["setting"])
    ok = (
        first["original_name"] == "砚纪"
        and first["name"] == "砚纪"
        and second["name"] == "自定义名"
        and got["original_name"] == "砚纪"
        and container["container"]["original_name"] == "砚纪"
        and imported["name"] == "砚纪"
    )
    return ("PASS" if ok else "FAIL"), (
        f"源包改名后：实例 original_name={first['original_name']}（未跟着改）；"
        f"显式 display_name 生效={second['name']}；快照 original_name={got['original_name']}；"
        f"导出件 container.original_name={container['container']['original_name']}；"
        f"改文件名后导入的候选名={imported['name']}（取记录，不取文件名）"
    )


async def d1_roundtrip(ctx: Ctx) -> tuple[str, str]:
    """附录 C4 / §7.1：多线导出再导入恢复全部对话、披露、记忆与引用；删原实例不影响新副本。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    character = card["meta"]["card_id"]
    ctx.world.activate(info["id"], timeline, now_real=1.7e9)
    client, meta, reply = await ctx.send(instance=info["id"], timeline=timeline, character=character, text="井绳换了吗")
    await client.close()

    # 第二条线：从当前提交分叉 + 自己的对话
    commit = (await ctx.mgmt.call("runtime.commit", instance_id=info["id"], timeline_id=timeline, note="导出点"))["commit"]
    branch = (await ctx.mgmt.call("runtime.fork", instance_id=info["id"], timeline_id=timeline, commit_id=commit["id"], name="分支线"))
    branch_id = branch["timeline"]["id"]
    client2, meta2, _ = await ctx.send(
        instance=info["id"], timeline=branch_id, character=character, text="另一条线上也问一句", thread="dm-2"
    )
    await client2.close()

    # 披露 + 记忆 + 记忆引用（三层记忆的最小一行）
    second_card = mycard(package, "桑叶")
    await ctx.mgmt.call("runtime.card.add", instance_id=info["id"], timeline_id=timeline, card=second_card)
    disclosure = await ctx.mgmt.call(
        "disclose.confirm", instance_id=info["id"], timeline_id=timeline,
        to_character="cc-桑叶", from_character=character, refs=[reply["message_id"]], note="探针披露",
    )
    ctx.store.memory_add({
        "id": "mm-a1", "instance_id": info["id"], "timeline_id": timeline, "character_id": character,
        "text": SECRET_MEMORY, "kind": "promise", "sources": [{"kind": "dialog", "ref": reply["message_id"]}],
        "happened_world": 0, "learned_world": 0, "recorded_world": 0, "semantic_watermark": 0,
        "strength": 0.9, "confidence": 0.9,
    })
    ctx.store.memory_cite(
        reply["message_id"], "mm-a1", instance_id=info["id"], timeline_id=timeline,
        character_id=character, world_seconds=0,
    )

    export = tmpdir("d1") / "full.isekai.json"
    await ctx.mgmt.call("instance.export", id=info["id"], path=str(export))
    exported = json.loads(export.read_text(encoding="utf-8"))
    source_disclosure = q(ctx.store, "SELECT id, scope FROM disclosure WHERE instance_id=?", (info["id"],))
    ported_ids = {
        str(item["id"])
        for item in exported["runtime"]["state"][timeline].get("disclosure") or []
    }
    before = inst_counts(ctx.store, info["id"])
    lines_before = len(ctx.store.timeline_list(info["id"]))

    root_b = tmpdir("d1b")
    async with core(root_b) as other:
        imported = (await other.mgmt.call("instance.import", path=str(export)))["instance"]
        after = inst_counts(other.store, imported["id"])
        lines_after = len(other.store.timeline_list(imported["id"]))
        history = await other.mgmt.call(
            "history.page", session_id=other.store.session_list()[0]["id"], limit=50
        )
        texts = [other.store.message_text(row) for row in history["messages"]]
        imported_disclosure = q(other.store, "SELECT * FROM disclosure WHERE instance_id=?", (imported["id"],))
        # 删原实例
        await ctx.mgmt.call("instance.delete", id=info["id"])
        survived = n(other.store, "SELECT COUNT(*) FROM message") > 0 and other.store.instance_get(imported["id"]) is not None

    keep = ("timeline", "session", "message", "memory", "knowledge", "disclosure", "character_join",
            "event", "claim", "life_plan", "unit", "commit_log", "commit_snapshot", "memory_citation",
            "timeline_clock", "institution_state", "custom_state", "environment_state")
    mismatch = {key: (before[key], after[key]) for key in keep if before[key] != after[key]}
    ok = (
        not mismatch and lines_before == lines_after == 2 and bool(imported_disclosure)
        and any("井绳是今天早上换的" in str(text) for text in texts) and survived
    )
    return ("PASS" if ok else "FAIL"), (
        f"{lines_before} 条线导出→导入（{lines_after}）：行数不一致={mismatch or '无'}；"
        f"披露行随件={len(imported_disclosure)} 条（源库 id={[str(r['id']) for r in source_disclosure]}，"
        f"导出件里 id={sorted(ported_ids)}，导入后 id={[str(r['id']) for r in imported_disclosure]}，"
        f"探针披露 id={disclosure.get('id')}）、记忆引用行 {before['memory_citation']}→{after['memory_citation']}；"
        f"导入副本的历史页能读到原对话={any('井绳是今天早上换的' in str(t) for t in texts)}；"
        f"删原实例后副本完好={survived}"
    )


async def d1b_commit_closure(ctx: Ctx) -> tuple[str, str]:
    """附录 C4 + §7.1「所需提交闭包」+ §九阶段 4：导入件的提交必须可用（回滚 / 分叉不丢历史）。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    character = card["meta"]["card_id"]
    ctx.world.activate(info["id"], timeline, now_real=1.7e9)
    client, meta, _ = await ctx.send(instance=info["id"], timeline=timeline, character=character, text="第一句")
    await client.close()
    commit = (await ctx.mgmt.call("runtime.commit", instance_id=info["id"], timeline_id=timeline, note="闭包点"))["commit"]
    export = tmpdir("d1b") / "closure.isekai.json"
    await ctx.mgmt.call("instance.export", id=info["id"], path=str(export))

    root_b = tmpdir("d1bb")
    async with core(root_b) as other:
        imported = (await other.mgmt.call("instance.import", path=str(export)))["instance"]
        mapped_commit = other.store.commit_list(imported["id"])
        snapshot_missing = [row["id"] for row in mapped_commit if other.store.commit_snapshot_get(row["id"]) is None]
        rollback = ""
        try:
            await other.mgmt.call(
                "runtime.rollback", instance_id=imported["id"], timeline_id=other.store.timeline_list(imported["id"])[0]["id"],
                commit_id=mapped_commit[-1]["id"], confirm=True,
            )
            rollback = "成功"
        except UmpError as exc:
            rollback = str(exc)
        fork = await other.mgmt.call(
            "runtime.fork", instance_id=imported["id"], timeline_id=other.store.timeline_list(imported["id"])[0]["id"],
            commit_id=mapped_commit[-1]["id"], name="副本分叉",
        )
        fork_id = fork["timeline"]["id"]
        fork_messages = n(
            other.store,
            "SELECT COUNT(*) FROM message WHERE session_id IN (SELECT id FROM session WHERE timeline_id=?)",
            (fork_id,),
        )
        fork_units = n(other.store, "SELECT COUNT(*) FROM unit WHERE timeline_id=?", (fork_id,))
        source_messages = n(ctx.store, "SELECT COUNT(*) FROM message WHERE session_id IN (SELECT id FROM session WHERE instance_id=?)", (info["id"],))

    ok = not snapshot_missing and rollback == "成功" and fork_messages == source_messages
    carried = {str(item.get("id")): bool(item.get("snapshot")) for item in (json.loads(export.read_text(encoding="utf-8"))["runtime"]["commits"])}
    return ("PASS" if ok else "FAIL"), (
        f"导入后提交 {len(mapped_commit)} 条，缺快照的={len(snapshot_missing)} 条"
        f"（缺的 kind={[r['kind'] for r in mapped_commit if r['id'] in snapshot_missing]}，"
        f"导出件各提交带快照={list(carried.values())}）；"
        f"回滚到导入提交={rollback[:48]}；从导入提交分叉得到的新线：对话 {fork_messages} 条（原实例 {source_messages} 条）、"
        f"性格单元 {fork_units} 行；build_container（portable.py:40-121）只带 commit 元数据、不带 commit_snapshot"
    )


async def d2_import_frozen(ctx: Ctx) -> tuple[str, str]:
    """附录 C5 / §7.3：导入全部冻结，无运输期补算，不恢复绑定 / 凭据，不向旧通道补发。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    character = card["meta"]["card_id"]
    ctx.world.activate(info["id"], timeline, now_real=1.7e9)
    ctx.world.advance(info["id"], timeline, now_real=1.7e9 + 2 * DAY)
    client, meta, _ = await ctx.send(instance=info["id"], timeline=timeline, character=character, text="问一句")
    await client.close()
    credential = await ctx.credential()
    watermark = int(ctx.store.clock_get(timeline)["processed_world"])

    export = tmpdir("d2") / "frozen.isekai.json"
    await ctx.mgmt.call("instance.export", id=info["id"], path=str(export))
    raw = export.read_text(encoding="utf-8")

    root_b = tmpdir("d2b")
    async with core(root_b) as other:
        imported = (await other.mgmt.call("instance.import", path=str(export)))["instance"]
        copy_tl = other.store.timeline_list(imported["id"])[0]["id"]
        state = other.store.timeline_get(copy_tl)["state"]
        clock = int(other.store.clock_get(copy_tl)["processed_world"])
        advanced = other.world.advance(imported["id"], copy_tl, now_real=1.7e9 + 40 * DAY)
        catch_up = other.world.catch_up_all(now_real=1.7e9 + 90 * DAY)
        counts_b = inst_counts(other.store, imported["id"])
        db_bytes = (root_b / "data" / "isekai.db").read_bytes()
        credential_in_db = credential.encode("utf-8") in db_bytes
        channel_rows = n(other.store, "SELECT COUNT(*) FROM channel_instance")
        pending = n(other.store, "SELECT COUNT(*) FROM message WHERE state='queued'")

    ok = (
        state == "frozen" and clock == watermark and advanced["state"] == "frozen"
        and int(advanced["processed_world"]) == watermark and catch_up == {}
        and counts_b["thread"] == 0 and counts_b["delivery"] == 0
        and counts_b["channel_bound_messages"] == 0 and not credential_in_db
        and channel_rows == 0 and credential not in raw and pending == 0
    )
    return ("PASS" if ok else "FAIL"), (
        f"导入线 state={state}、时钟停在导出水位 {clock}（导出时 {watermark}）；"
        f"越过 40 天现实时间再推进 → state={advanced['state']}、水位 {advanced['processed_world']}；catch_up_all={catch_up}；"
        f"副本 thread={counts_b['thread']}、delivery={counts_b['delivery']}、带通道归属的消息={counts_b['channel_bound_messages']}；"
        f"新库通道行={channel_rows}、导出端凭据出现在新库里={credential_in_db}、导出件含凭据={credential in raw}、待发消息={pending}"
    )


async def d3_atomic_failures(ctx: Ctx) -> tuple[str, str]:
    """附录 C6 / §7.5：版本不兼容、转换失败、损坏或恶意容器均原子失败，已有实例不变。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    export = tmpdir("d3") / "tamper.isekai.json"
    await ctx.mgmt.call("instance.export", id=info["id"], path=str(export))
    good = json.loads(export.read_text(encoding="utf-8"))
    before = inst_counts(ctx.store, info["id"])

    cases: dict[str, dict[str, Any]] = {}
    damaged = clone(good)
    damaged["setting"]["cards"][0]["identity"]["name"] = "被改过"
    cases["损坏（内容改动）"] = damaged
    newer = clone(good)
    newer["container"]["container_version"] = "2.0"
    cases["容器主版本不兼容"] = newer
    data_newer = clone(good)
    data_newer["container"]["data_format"] = "9.0"
    cases["数据格式不兼容"] = data_newer
    capability = clone(good)
    capability["container"]["capabilities"] = ["future.capability.v9"]
    cases["未知必需能力"] = capability
    broken_setting = clone(good)
    broken_setting["setting"]["world_package"]["calendar"]["day_seconds"] = 0
    broken_setting["integrity"]["digest"] = None
    del broken_setting["integrity"]["digest"]
    cases["缺完整性指纹"] = broken_setting

    outcomes: list[str] = []
    for label, payload in cases.items():
        path = tmpdir("d3c") / "c.isekai.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            await ctx.mgmt.call("instance.import", path=str(path))
            outcomes.append(f"{label}:未拒绝")
        except UmpError as exc:
            outcomes.append(f"{label}:拒绝({exc.code})")
    after = inst_counts(ctx.store, info["id"])
    instances = len(ctx.store.instance_list())
    ok = all("拒绝" in item for item in outcomes) and before == after and instances == 1
    return ("PASS" if ok else "FAIL"), (
        f"5 类坏件导入结果：{'；'.join(outcomes)}；已有实例行数与状态不变={before == after}；库中实例数={instances}"
    )


async def d4_container_and_log_leak(ctx: Ctx) -> tuple[str, str]:
    """附录 C7 / §7.1 / §3.5：包中无激活状态与凭据；普通日志不复制内部正文。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    character = card["meta"]["card_id"]
    ctx.world.activate(info["id"], timeline, now_real=1.7e9)
    client, meta, reply = await ctx.send(instance=info["id"], timeline=timeline, character=character, text="秘密问句-别进日志-aa11")
    await client.close()
    credential = await ctx.credential()

    # 内部正文（事件详情）落一条，然后导出
    ctx.store.runtime_load(info["id"], timeline, {
        "watermark": 0,
        "events": [{
            "id": "ev-secret", "instance_id": info["id"], "timeline_id": timeline, "world_seconds": 0,
            "seq": 7, "kind": "world", "family": "", "template": "cf-1", "source": "backfill",
            "summary": SECRET_EVENT, "detail": SECRET_EVENT, "text_source": "template", "effects": "[]",
            "share_value": 0, "importance": 0.0, "created_real": 0.0,
        }],
    })
    ctx.store.memory_add({
        "id": "mm-secret", "instance_id": info["id"], "timeline_id": timeline, "character_id": character,
        "text": SECRET_MEMORY, "kind": "fact", "sources": [], "happened_world": 0, "learned_world": 0,
        "recorded_world": 0, "semantic_watermark": 0, "strength": 0.5, "confidence": 0.5,
    })
    export = tmpdir("d4") / "leak.isekai.json"
    await ctx.mgmt.call("instance.export", id=info["id"], path=str(export))
    raw = export.read_text(encoding="utf-8")
    container = json.loads(raw)

    def key_paths(payload: Any, prefix: str = "") -> list[str]:
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

    forbidden = ("credential", "binding_token", "channel_id", "thread_id", "api_key", "apikey",
                 "device", "log_path", "rate_command", "anchor_real", "high_water_real", "session_token")
    hits = [path for path in key_paths(container) if path.rsplit(".", 1)[-1].split("[")[0] in forbidden]
    states = [item["state"] for item in container["runtime"]["timelines"]]
    logs = ctx.root / "logs" / "core.log"
    log_text = logs.read_text(encoding="utf-8", errors="replace") if logs.exists() else ""
    leaks = [token for token in (SECRET_EVENT, SECRET_MEMORY, credential, "秘密问句-别进日志-aa11") if token in log_text]

    ok = not hits and not leaks and credential not in raw and all(state == "frozen" for state in states)
    return ("PASS" if ok else "FAIL"), (
        f"导出件里的凭据 / 绑定 / 本机锚点键={hits or '无'}；导出端通道凭据是否在包里={credential in raw}；"
        f"导出件里的线状态={states}（本机激活状态不随件）；"
        f"core.log（{logs}）里的内部正文 / 凭据命中={leaks or '无'}"
    )


async def d5_import_error_no_leak(ctx: Ctx) -> tuple[str, str]:
    """附录 C7 / §3.5：导入错误不泄露内部事件、性格与记忆。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    ctx.store.runtime_load(info["id"], timeline, {
        "watermark": 0,
        "events": [{
            "id": "ev-secret2", "instance_id": info["id"], "timeline_id": timeline, "world_seconds": 0,
            "seq": 8, "kind": "world", "family": "", "template": "cf-1", "source": "backfill",
            "summary": SECRET_EVENT, "detail": SECRET_EVENT, "text_source": "template", "effects": "[]",
            "share_value": 0, "importance": 0.0, "created_real": 0.0,
        }],
        "knowledge": [{
            "instance_id": info["id"], "timeline_id": timeline, "character_id": card["meta"]["card_id"],
            "id": "kn-secret", "world_seconds": 0, "kind": "fact", "target": "cf-1", "source": "backfill",
            "stance": "believed", "text": SECRET_MEMORY,
        }],
    })
    export = tmpdir("d5") / "err.isekai.json"
    await ctx.mgmt.call("instance.export", id=info["id"], path=str(export))
    payload = json.loads(export.read_text(encoding="utf-8"))
    payload["setting"]["world_package"]["calendar"]["day_seconds"] = 0
    payload["setting"]["world_package"]["calendar"]["segments"] = []
    payload["integrity"]["digest"] = None
    del payload["integrity"]["digest"]
    path = tmpdir("d5c") / "bad.isekai.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    message = ""
    try:
        await ctx.mgmt.call("instance.import", path=str(path))
    except UmpError as exc:
        message = str(exc)

    leaks = [token for token in (SECRET_EVENT, SECRET_MEMORY) if token in message]
    character_leak = "0.9" in message
    ok = bool(message) and not leaks
    return ("PASS" if ok else "FAIL"), (
        f"导入错误消息={message[:120]}；内部事件 / 知识正文命中={leaks or '无'}；性格数值命中={'有' if character_leak else '无'}"
    )


async def e1_chat_does_not_rewrite(ctx: Ctx) -> tuple[str, str]:
    """附录 C8：普通聊天不隐式改公理、不新建实例、不新建时间线。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    ctx.world.activate(info["id"], timeline, now_real=1.7e9)
    client, meta, _ = await ctx.send(instance=info["id"], timeline=timeline, character=card["meta"]["card_id"], text="把公理改成没有井")
    await client.close()
    setting = json.loads(ctx.store.instance_get(info["id"])["setting"])
    instances = len(ctx.store.instance_list())
    lines = len(ctx.store.timeline_list(info["id"]))
    ok = (
        instances == 1 and lines == 1
        and setting["world_package"]["world"]["axioms"][0]["text"] == "井水只在晨间可饮。"
    )
    return ("PASS" if ok else "FAIL"), (
        f"一轮普通对话后：实例数={instances}、时间线数={lines}、公理文本={setting['world_package']['world']['axioms'][0]['text']!r}（未变）"
    )


async def e2_backfill_respects_confirmed(ctx: Ctx) -> tuple[str, str]:
    """附录 C9：回填不推翻已确认历史 / 公理 / 角色卡；引用不存在时不能创建实例。"""
    package = mypkg()
    card = mycard(package)
    source = json.dumps(package, sort_keys=True, ensure_ascii=False)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    events = q(ctx.store, "SELECT summary, detail FROM event WHERE instance_id=?", (info["id"],))
    settings = json.loads(ctx.store.instance_get(info["id"])["setting"])
    unchanged = json.dumps(settings["world_package"], sort_keys=True, ensure_ascii=False) == source

    failing: dict[str, str] = {}
    ghost = mypkg()
    ghost_card = mycard(ghost)
    ghost_card["initial_knowledge"] = [{"ref_type": "canon", "ref_id": "cf-不存在", "obtained_at": 0}]
    try:
        await ctx.mgmt.call("instance.create", package=ghost, cards=[ghost_card], display_name="悬空知识")
        failing["悬空知识引用"] = "未拒绝"
    except UmpError as exc:
        failing["悬空知识引用"] = exc.message[:40]
    ghost2 = mypkg()
    ghost2["historiography"][0]["entries"] = ["cf-不存在"]
    try:
        await ctx.mgmt.call("instance.create", package=ghost2, cards=[mycard(ghost2)], display_name="悬空史料")
        failing["史料条目悬空"] = "未拒绝"
    except UmpError as exc:
        failing["史料条目悬空"] = exc.message[:40]
    instances = len(ctx.store.instance_list())

    ok = unchanged and all(value != "未拒绝" for value in failing.values()) and instances == 1
    return ("PASS" if ok else "FAIL"), (
        f"创建后锁定设定与原包逐字节一致={unchanged}；世界事件回填 {len(events)} 条（source=backfill，无效果叠加）；"
        f"非法引用创建结果={failing}；库中实例数={instances}"
    )


async def e3_registry_references(ctx: Ctx) -> tuple[str, str]:
    """附录 C10：结构引用指向未登记对象即失败；纯署名贡献者不被补造生平；在册不等于已知。"""
    package = mypkg()
    bad = mypkg()
    bad["events"]["families"][0]["templates"][0]["effects"][0]["target"] = "src-不存在"
    rejected = ""
    try:
        await ctx.mgmt.call("instance.create", package=bad, cards=[mycard(bad)], display_name="悬空效果目标")
    except UmpError as exc:
        rejected = exc.message

    info = (await ctx.mgmt.call("instance.create", package=package, cards=[mycard(package)]))["instance"]
    entities = json.loads(ctx.store.instance_get(info["id"])["setting"])["world_package"]["entities"]
    fabricated = [item for item in entities if str(item.get("name")) == "无名录事"]
    known = ctx.store.knowledge_holders(info["id"], ctx.store.timeline_list(info["id"])[0]["id"], "en-1")
    claims = n(ctx.store, "SELECT COUNT(*) FROM claim WHERE instance_id=?", (info["id"],))

    ok = bool(rejected) and not fabricated and not known and claims > 0
    return ("PASS" if ok else "FAIL"), (
        f"效果目标引用未登记对象 → 创建被拒（{rejected[:36]}…）；"
        f"史料贡献者「无名录事」是否被补造为登记实体={bool(fabricated)}；"
        f"名册实体 en-1 的知识持有者={known or '无'}（在册不等于已知）；说法层条目已落库 {claims} 条"
    )


async def e4_minimum_content(ctx: Ctx) -> tuple[str, str]:
    """附录 C11 / 附录 D：只有一族事件、无谜题的最小包不被拒；空壳史料 / 非法寿命 / 越权知识仍失败。"""
    minimal = mypkg(families=1, mysteries=0)
    created = (await ctx.mgmt.call(
        "instance.create", package=minimal, cards=[mycard(minimal)], display_name="最小包"
    ))["instance"]

    cases: dict[str, str] = {}
    empty_hist = mypkg(empty_units=True)
    try:
        await ctx.mgmt.call("instance.create", package=empty_hist, cards=[mycard(empty_hist)], display_name="空壳史料")
        cases["空壳史料"] = "未拒绝"
    except UmpError as exc:
        cases["空壳史料"] = exc.message[:36]
    lifespan = mypkg(bad_lifespan=True)
    try:
        await ctx.mgmt.call("instance.create", package=lifespan, cards=[mycard(lifespan)], display_name="非法寿命")
        cases["非法寿命依据"] = "未拒绝"
    except UmpError as exc:
        cases["非法寿命依据"] = exc.message[:36]
    scope = mypkg()
    over_card = mycard(scope)
    over_card["initial_knowledge"] = [
        {
            "ref_type": "historiography",
            "ref_id": "hs-1",
            "scope": ["cf-1", "nv-1", "cf-不存在"],
            "obtained_at": int(scope["calendar"]["initial_moment"]) - DAY,
        }
    ]
    try:
        await ctx.mgmt.call("instance.create", package=scope, cards=[over_card], display_name="越权知识")
        cases["越权初始知识"] = "未拒绝"
    except UmpError as exc:
        cases["越权初始知识"] = exc.message[:36]

    ok = bool(created["id"]) and all(value != "未拒绝" for value in cases.values())
    return ("PASS" if ok else "FAIL"), (
        f"最小包（1 个事件族；0 谜题；无制度 / 惯例；空环境）创建成功 name={created['name']}；"
        f"阻断项实测={cases}"
    )


async def e5_draft_and_budget(ctx: Ctx) -> tuple[str, str]:
    """附录 C12：无 Key 可手编 / 存草稿 / 导入；草稿可续作可丢弃；用量上限暂停并复用有效产物。"""
    package = mypkg()
    card = mycard(package)
    # 无 key：生成类操作明确报 llm_not_configured，而不是伪造成功
    llm_error = ""
    try:
        from isekai_core.app import build_llm

        cfg = load_config(tmpdir("e5cfg"))
        llm = build_llm(cfg)
        await llm.chat([{"role": "user", "content": "hi"}], timeout=5)
        llm_error = "未报错"
    except Exception as exc:  # noqa: BLE001
        llm_error = f"{type(exc).__name__}:{getattr(exc, 'code', '')}"

    # 手编 + 草稿（未过校验也能存 / 续作 / 丢弃）
    draft_payload = mypkg()
    draft_payload["world"]["axioms"] = []
    await ctx.mgmt.call("world.draft.save", name="探针草稿", kind="package", payload=draft_payload, errors=["world.axioms: 至少一条世界公理"])
    listed = (await ctx.mgmt.call("world.draft.list"))["drafts"]
    loaded = (await ctx.mgmt.call("world.draft.load", name="探针草稿"))["draft"]
    manual = ctx.write("manual.json", package)
    await ctx.mgmt.call("world.package.save", path=str(manual), package=package)
    await ctx.mgmt.call("world.draft.discard", name="探针草稿")
    after_discard = (await ctx.mgmt.call("world.draft.list"))["drafts"]
    manual_intact = json.loads(manual.read_text(encoding="utf-8"))["meta"]["package_id"] == package["meta"]["package_id"]

    # 用量上限：把实例上限设到极小 → 渲染被暂停；放宽上限后渲染成功，再调用复用既有产物
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    ctx.store.runtime_load(info["id"], timeline, {
        "watermark": 0,
        "events": [{
            "id": "ev-render", "instance_id": info["id"], "timeline_id": timeline, "world_seconds": 0,
            "seq": 3, "kind": "world", "family": "", "template": "cf-1", "source": "backfill",
            "summary": "井绳断了半日", "detail": "井绳断了半日", "text_source": "template", "effects": "[]",
            "share_value": 0, "importance": 0.0, "created_real": 0.0,
        }],
        "claims": [{
            "id": "cl-r1", "instance_id": info["id"], "timeline_id": timeline, "event_id": "ev-render",
            "source_id": "src-1", "text": "坡上口信只说井绳断了半日", "audience": "公开",
            "earliest_world": 0, "credibility": "recorded", "derived_from": None,
        }],
    })
    ctx.llm.replies = [json.dumps(
        {"detail": "井绳断了半日，坡上口信这么记着。", "claims": {"src-1": "口信只说井绳断了半日，没提是谁断的"}},
        ensure_ascii=False,
    )]
    ctx.store.budget_policy_set(info["id"], instance_tokens_per_day=1)
    paused = await ctx.mgmt.call(
        "event.render", instance_id=info["id"], timeline_id=timeline, event_id="ev-render", timeout=60
    )
    ctx.store.budget_policy_set(info["id"], instance_tokens_per_day=200000)
    rendered = await ctx.mgmt.call(
        "event.render", instance_id=info["id"], timeline_id=timeline, event_id="ev-render", timeout=60
    )
    reused = await ctx.mgmt.call(
        "event.render", instance_id=info["id"], timeline_id=timeline, event_id="ev-render", timeout=60
    )
    ok = (
        "llm_not_configured" in llm_error
        and len(listed) == 1 and loaded["name"] == "探针草稿" and not after_discard and manual_intact
        and paused["text_source"] == "template" and bool(paused.get("budget", {}).get("paused"))
        and rendered.get("text_source") == "llm"
        and reused.get("reused") is True
    )
    return ("PASS" if ok else "FAIL"), (
        f"无 Key 时真实客户端报 {llm_error}；草稿列表 {len(listed)} → 丢弃后 {len(after_discard)}、载回名称={loaded['name']}、"
        f"手编包仍在={manual_intact}；超限渲染 text_source={paused['text_source']}（budget={paused.get('budget')}）；"
        f"放宽上限后渲染 text_source={rendered.get('text_source')}、再调用 reused={reused.get('reused')} / calls={reused.get('calls')}"
    )


async def e6_institutions_optional(ctx: Ctx) -> tuple[str, str]:
    """附录 C13 / 附录 D：未声明制度 / 惯例不拒绝合法题材，也不隐式补造；声明了却解释不通即创建期失败。"""
    minimal = mypkg(families=1, mysteries=0)
    created = (await ctx.mgmt.call(
        "instance.create", package=minimal, cards=[mycard(minimal)], display_name="无制度题材"
    ))["instance"]
    filled = json.loads(ctx.store.instance_get(created["id"])["setting"])["world_package"]
    implicit = (
        n(ctx.store, "SELECT COUNT(*) FROM institution_state WHERE instance_id=?", (created["id"],)),
        n(ctx.store, "SELECT COUNT(*) FROM custom_state WHERE instance_id=?", (created["id"],)),
    )

    broken_inst = mypkg(institutions=True)
    del broken_inst["world"]["institutions"][0]["mandate"]
    broken_inst["world"]["institutions"][0]["offices"][0]["holder"] = "en-不存在"
    cases: dict[str, str] = {}
    try:
        await ctx.mgmt.call("instance.create", package=broken_inst, cards=[mycard(broken_inst)], display_name="制度缺项")
        cases["制度缺职权 / 在任者非登记实体"] = "未拒绝"
    except UmpError as exc:
        cases["制度缺职权 / 在任者非登记实体"] = exc.message[:48]
    broken_custom = mypkg(customs=True)
    broken_custom["world"]["customs"][0]["practice"] = "与 forms 不符的做法"
    try:
        await ctx.mgmt.call("instance.create", package=broken_custom, cards=[mycard(broken_custom)], display_name="惯例不自洽")
        cases["惯例现行做法不在允许范围"] = "未拒绝"
    except UmpError as exc:
        cases["惯例现行做法不在允许范围"] = exc.message[:48]

    ok = (
        bool(created["id"]) and not filled.get("world", {}).get("institutions") and implicit == (0, 0)
        and all(value != "未拒绝" for value in cases.values())
    )
    return ("PASS" if ok else "FAIL"), (
        f"无制度 / 惯例的最小题材创建成功（名称 {created['name']}）；运行期制度 / 惯例状态行={implicit}（未隐式补造）；"
        f"不自洽声明的创建结果={cases}"
    )


async def f1_join_anchoring(ctx: Ctx) -> tuple[str, str]:
    """附录 C14 / §3.7 时间锚定：锚定补入时刻；出生 / 死亡 / 知识越权被拒；失败不改实例。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    ctx.world.activate(info["id"], timeline, now_real=1.7e9)
    ctx.world.advance(info["id"], timeline, now_real=1.7e9 + 3 * DAY)
    watermark = int(ctx.store.clock_get(timeline)["processed_world"])
    second = mycard(package, "桑叶")
    joined = (await ctx.mgmt.call(
        "runtime.card.add", instance_id=info["id"], timeline_id=timeline, card=second,
        joined_world=watermark, note="探针补入",
    ))["join"]
    units = q(ctx.store, "SELECT id, updated_world FROM unit WHERE character_id='cc-桑叶'", ())
    plan = q(ctx.store, "SELECT day_index, created_world FROM life_plan WHERE character_id='cc-桑叶'", ())
    day_index = package["calendar"]["day_seconds"] and None
    from isekai_core.runtime.calendar import calendar_from_package

    calendar = calendar_from_package(package)
    expected_day = calendar.day_index(watermark)

    before = inst_counts(ctx.store, info["id"])
    rejects: dict[str, str] = {}
    future = mycard(package, "未来人", born=watermark + DAY)
    try:
        await ctx.mgmt.call("runtime.card.add", instance_id=info["id"], timeline_id=timeline, card=future)
        rejects["出生晚于补入时刻"] = "未拒绝"
    except UmpError as exc:
        rejects["出生晚于补入时刻"] = exc.message[:32]
    dead = mycard(package, "亡者", died=watermark - DAY)
    try:
        await ctx.mgmt.call("runtime.card.add", instance_id=info["id"], timeline_id=timeline, card=dead)
        rejects["已身故者"] = "未拒绝"
    except UmpError as exc:
        rejects["已身故者"] = exc.message[:32]
    over = mycard(package, "越权者", obtained_at=watermark + 5 * DAY)
    try:
        await ctx.mgmt.call("runtime.card.add", instance_id=info["id"], timeline_id=timeline, card=over)
        rejects["初始知识晚于补入时刻"] = "未拒绝"
    except UmpError as exc:
        rejects["初始知识晚于补入时刻"] = exc.message[:32]
    after = inst_counts(ctx.store, info["id"])
    ok = (
        joined["joined_world"] == watermark
        and all(int(row["updated_world"]) == watermark for row in units)
        and all(int(row["created_world"]) == watermark for row in plan)
        and [int(row["day_index"]) for row in plan] == [expected_day]
        and all(value != "未拒绝" for value in rejects.values())
        and before == after
    )
    return ("PASS" if ok else "FAIL"), (
        f"补入水位={joined['joined_world']}（当前水位 {watermark}）、单元水位={[int(r['updated_world']) for r in units]}、"
        f"生活计划 created_world={[int(r['created_world']) for r in plan]}、day_index={[int(r['day_index']) for r in plan]}（期望 {expected_day}）；"
        f"非法补入={rejects}；失败后实例行数不变={before == after}"
    )


async def f2_rollback_membership(ctx: Ctx) -> tuple[str, str]:
    """附录 C15：回滚跨过加入点后该角色在本线退出；从加入后提交分叉则继承。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    character = card["meta"]["card_id"]
    ctx.world.activate(info["id"], timeline, now_real=1.7e9)
    ctx.world.advance(info["id"], timeline, now_real=1.7e9 + DAY)
    baseline = (await ctx.mgmt.call("runtime.commit", instance_id=info["id"], timeline_id=timeline, note="补卡前"))["commit"]

    await ctx.mgmt.call("runtime.card.add", instance_id=info["id"], timeline_id=timeline, card=mycard(package, "桑叶"))
    client, meta, _ = await ctx.send(instance=info["id"], timeline=timeline, character="cc-桑叶", text="我是刚来的", thread="dm-9")
    await client.close()
    ctx.store.memory_add({
        "id": "mm-sangye", "instance_id": info["id"], "timeline_id": timeline, "character_id": "cc-桑叶",
        "text": "她记得第一次说话", "kind": "fact", "sources": [], "happened_world": 0, "learned_world": 0,
        "recorded_world": 0, "semantic_watermark": 0, "strength": 0.5, "confidence": 0.5,
    })
    after_join = (await ctx.mgmt.call("runtime.commit", instance_id=info["id"], timeline_id=timeline, note="补卡后"))["commit"]
    cards_now = [c["meta"]["card_id"] for c in ctx.world.cards(ctx.store.instance_get(info["id"]), timeline_id=timeline)]
    units_before = n(ctx.store, "SELECT COUNT(*) FROM unit WHERE instance_id=? AND character_id='cc-桑叶'", (info["id"],))

    await ctx.mgmt.call("runtime.rollback", instance_id=info["id"], timeline_id=timeline, commit_id=baseline["id"], confirm=True)
    joins_after = n(
        ctx.store,
        "SELECT COUNT(*) FROM character_join WHERE instance_id=? AND character_id='cc-桑叶' AND state='active'",
        (info["id"],),
    )
    joins_revoked = n(
        ctx.store,
        "SELECT COUNT(*) FROM character_join WHERE instance_id=? AND character_id='cc-桑叶' AND state='revoked'",
        (info["id"],),
    )
    units_after = n(ctx.store, "SELECT COUNT(*) FROM unit WHERE instance_id=? AND character_id='cc-桑叶'", (info["id"],))
    memory_after = n(ctx.store, "SELECT COUNT(*) FROM memory WHERE instance_id=? AND character_id='cc-桑叶'", (info["id"],))
    dialog_after = n(
        ctx.store,
        "SELECT COUNT(*) FROM message WHERE session_id IN (SELECT id FROM session WHERE instance_id=? AND character_id='cc-桑叶')",
        (info["id"],),
    )
    cards_rolled = [c["meta"]["card_id"] for c in ctx.world.cards(ctx.store.instance_get(info["id"]), timeline_id=timeline)]
    forks = (await ctx.mgmt.call(
        "runtime.fork", instance_id=info["id"], timeline_id=timeline, commit_id=after_join["id"], name="继承线"
    ))
    fork_id = forks["timeline"]["id"]
    fork_cards = [c["meta"]["card_id"] for c in ctx.world.cards(ctx.store.instance_get(info["id"]), timeline_id=fork_id)]

    ok = (
        "cc-桑叶" in cards_now
        and joins_after == 0 and joins_revoked == 1 and units_after == 0 and memory_after == 0 and dialog_after == 0
        and "cc-桑叶" not in cards_rolled and "cc-桑叶" in fork_cards
    )
    return ("PASS" if ok else "FAIL"), (
        f"补入后本线角色={cards_now}（单元 {units_before} 行）→ 回滚到补卡前提交：有效成员资格 {joins_after} 行（撤销留档 {joins_revoked} 行）、单元 {units_after} 行、"
        f"记忆 {memory_after} 行、对话 {dialog_after} 行、本线角色={cards_rolled}；"
        f"从补卡后提交分叉的角色={fork_cards}（继承={('cc-桑叶' in fork_cards)}）"
    )


async def f3_isolation(ctx: Ctx) -> tuple[str, str]:
    """附录 C16：补入角色与既有角色默认隔离；「已相识」声明不改变这一点。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    first = card["meta"]["card_id"]
    ctx.world.activate(info["id"], timeline, now_real=1.7e9)
    client_a, meta_a, _ = await ctx.send(instance=info["id"], timeline=timeline, character=first, text="只跟甲说的话-9f2c", thread="dm-a")
    await client_a.close()
    await ctx.mgmt.call(
        "runtime.card.add", instance_id=info["id"], timeline_id=timeline, card=mycard(package, "桑叶"), acquainted=True
    )
    client_b, meta_b, reply_b = await ctx.send(instance=info["id"], timeline=timeline, character="cc-桑叶", text="你是新来的吗", thread="dm-b")
    await client_b.close()

    sessions = q(ctx.store, "SELECT id, character_id FROM session WHERE instance_id=? AND timeline_id=?", (info["id"], timeline))
    b_prompt = ctx.world.system_prompt({
        "instance_id": info["id"], "timeline_id": timeline, "character_id": "cc-桑叶",
    }, now_real=1.7e9)
    a_prompt = ctx.world.system_prompt({
        "instance_id": info["id"], "timeline_id": timeline, "character_id": first,
    }, now_real=1.7e9)
    b_rows = q(
        ctx.store,
        "SELECT text FROM knowledge WHERE instance_id=? AND character_id='cc-桑叶'",
        (info["id"],),
    )
    b_memory_rows = q(
        ctx.store, "SELECT text FROM memory WHERE instance_id=? AND character_id='cc-桑叶'", (info["id"],)
    )
    a_dialog = q(
        ctx.store,
        "SELECT m.text FROM message m JOIN session s ON s.id=m.session_id WHERE s.character_id=?",
        (first,),
    )
    b_dialog = q(
        ctx.store,
        "SELECT m.text FROM message m JOIN session s ON s.id=m.session_id WHERE s.character_id=?",
        ("cc-桑叶",),
    )
    a_text = "只跟甲说的话-9f2c"
    ok = (
        len(sessions) == 2
        and a_text not in b_prompt
        and a_text not in " ".join(str(row["text"] or "") for row in b_rows + b_memory_rows)
        and "桑叶" not in a_prompt
        and a_text in " ".join(str(row["text"] or "") for row in a_dialog)
        and a_text not in " ".join(str(row["text"] or "") for row in b_dialog)
    )
    return ("PASS" if ok else "FAIL"), (
        f"两角色各自会话 {len(sessions)} 条（{[row['character_id'] for row in sessions]}）；"
        f"乙的扮演定义含甲的原话={a_text in b_prompt}；乙的知识 / 记忆里含甲的原话="
        f"{a_text in ' '.join(str(row['text'] or '') for row in b_rows + b_memory_rows)}；"
        f"甲的扮演定义含「桑叶」={'桑叶' in a_prompt}（已相识只加一条对话单元 unit:join-acquainted，不开放互见）；"
        f"甲会话里有原话={a_text in ' '.join(str(row['text'] or '') for row in a_dialog)}、乙会话里有={a_text in ' '.join(str(row['text'] or '') for row in b_dialog)}"
    )


async def g1_validation_limits(ctx: Ctx) -> tuple[str, str]:
    """§2.3：校验失败指出模板自身字段；文件规模 / 嵌套 / 文本长度有加载限额且超限明确拒绝。"""
    broken = mypkg()
    broken["calendar"]["day_seconds"] = 0
    broken["sources"][0]["reach"] = ""
    errors = validate_package(broken)
    named = [item for item in errors if "calendar.day_seconds" in item or "sources[0].reach" in item]

    deep = mypkg()
    node: Any = deep
    for _ in range(MAX_DEPTH + 3):
        node["nested"] = {}
        node = node["nested"]
    deep_errors = validate_package(deep)
    long_text = mypkg()
    long_text["world"]["geography"] = "长" * (MAX_STRING + 40)
    long_errors = validate_package(long_text)
    huge = mypkg()
    huge["canon"] = [{"id": f"cf-{i}", "statement": "x"} for i in range(MAX_COLLECTION + 10)]
    huge_errors = validate_package(huge)

    ok = (
        bool(named)
        and any("嵌套深度" in item for item in deep_errors)
        and any("文本长度" in item for item in long_errors)
        and any("条目数" in item or "超过上限" in item for item in huge_errors)
    )
    return ("PASS" if ok else "FAIL"), (
        f"字段级错误={named[:2]}；深度超限={[e for e in deep_errors if '深度' in e]}；"
        f"超长文本={[e for e in long_errors if '文本长度' in e][:1]}；超大集合={[e for e in huge_errors if '上限' in e][:2]}；"
        f"限额常量 depth={MAX_DEPTH} nodes={MAX_NODES} string={MAX_STRING} collection={MAX_COLLECTION}"
    )


async def g2_unknown_capability(ctx: Ctx) -> tuple[str, str]:
    """§2.5：未知必需能力必须在确认前报错，不能创建实例。"""
    package = mypkg(requires=["world.future.v9"])
    errors = validate_package(package)
    rejected = ""
    try:
        await ctx.mgmt.call("instance.create", package=package, cards=[mycard(package)], display_name="未来能力")
    except UmpError as exc:
        rejected = exc.message
    info = (await ctx.mgmt.call(
        "instance.create", package=mypkg(), cards=[mycard(mypkg())], display_name="正常能力"
    ))["instance"]
    ok = bool(errors) and any("能力" in item for item in errors) and bool(rejected) and bool(info["id"])
    return ("PASS" if ok else "FAIL"), (
        f"包声明未支持能力 → 校验错误={errors}；创建结果={rejected[:40]}…；无能力声明的包仍可创建={bool(info['id'])}"
    )


async def g3_export_watermark(ctx: Ctx) -> tuple[str, str]:
    """§7.1：一致水位（不导出「最新时钟 + 旧状态」）；中途失败不留伪装包、不破坏旧件。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    ctx.world.activate(info["id"], timeline, now_real=1.7e9)
    ctx.world.advance(info["id"], timeline, now_real=1.7e9 + DAY)
    row = ctx.store.clock_get(timeline)
    watermark = int(row["processed_world"])
    # 模拟「正在补算」：把时钟推到已完成水位之前
    ctx.store.clock_put({**row, "base_world": watermark + 5 * DAY, "processed_world": watermark, "catching_up": 1})
    target = tmpdir("g3") / "prior.isekai.json"
    target.write_text("旧导出件占位", encoding="utf-8")
    container = build_container(ctx.store, info["id"])
    exported_watermark = int(container["runtime"]["state"][timeline]["watermark"])
    clock_world = int(ctx.store.clock_get(timeline)["base_world"])
    old_intact = target.read_text(encoding="utf-8") == "旧导出件占位"

    # 中途失败：让校验步骤抛错，先前的导出件不能被破坏、也不能留 .tmp
    import isekai_core.world.portable as portable

    original = portable.verify_integrity
    portable.verify_integrity = lambda payload: (_ for _ in ()).throw(RuntimeError("探针注入失败"))
    failed = False
    try:
        write_export(ctx.store, info["id"], target)
    except Exception as exc:  # noqa: BLE001
        failed = "探针注入失败" in str(exc) or isinstance(exc, RuntimeError)
    finally:
        portable.verify_integrity = original
    leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    intact_after = target.read_text(encoding="utf-8")

    ok = exported_watermark == watermark and failed and not leftovers and intact_after == "旧导出件占位"
    return ("PASS" if ok else "FAIL"), (
        f"补算中（catching_up=1、时钟世界秒 {clock_world}）导出：件内水位={exported_watermark}、状态水位={watermark}"
        f"（导出取已完成水位、时钟重新锚定在它上面，不混「最新时钟 + 旧状态」）；"
        f"注入校验失败后：旧导出件内容={intact_after!r}、残留 .tmp={leftovers or '无'}"
    )


async def g4_compat_states(ctx: Ctx) -> tuple[str, str]:
    """§7.6：compatible / convertible / blocked 三态；blocked 阻断推进；检查本身不改状态。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    compatible = (await ctx.mgmt.call("instance.info", id=info["id"]))["instance"]["compatibility"]

    ctx.store._conn.execute("UPDATE instance SET data_format='0.0' WHERE id=?", (info["id"],))
    ctx.store._conn.commit()
    convertible = (await ctx.mgmt.call("instance.info", id=info["id"]))["instance"]
    ctx.store._conn.execute("UPDATE instance SET data_format='9.0' WHERE id=?", (info["id"],))
    ctx.store._conn.commit()
    blocked = (await ctx.mgmt.call("instance.info", id=info["id"]))["instance"]
    before_clock = dict(ctx.store.clock_get(timeline))
    blocked_advance = ""
    try:
        await ctx.mgmt.call("runtime.advance", instance_id=info["id"], timeline_id=timeline)
        blocked_advance = "未阻断"
    except UmpError as exc:
        blocked_advance = exc.message
    blocked_activate = ""
    try:
        await ctx.mgmt.call("runtime.activate", instance_id=info["id"], timeline_id=timeline)
        blocked_activate = "未阻断"
    except UmpError as exc:
        blocked_activate = exc.message
    after_clock = dict(ctx.store.clock_get(timeline))
    state_unchanged = before_clock == after_clock and ctx.store.timeline_get(timeline)["state"] == "frozen"

    ok = (
        compatible == "compatible" and convertible["compatibility"] == "convertible"
        and blocked["compatibility"] == "blocked" and blocked_advance != "未阻断" and blocked_activate != "未阻断"
        and state_unchanged
    )
    return ("PASS" if ok else "FAIL"), (
        f"三态实测：{compatible} / {convertible['compatibility']} / {blocked['compatibility']}；"
        f"blocked 时推进结果={blocked_advance[:36]}、激活结果={blocked_activate[:36]}；"
        f"检查前后时钟行不变={before_clock == after_clock}、线状态仍={ctx.store.timeline_get(timeline)['state']}"
    )


async def g5_hostile_payload(ctx: Ctx) -> tuple[str, str]:
    """§7.5：拒绝路径穿越 / 绝对路径 / 可执行载荷；不反序列化任意代码对象。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    export = tmpdir("g5") / "h.isekai.json"
    await ctx.mgmt.call("instance.export", id=info["id"], path=str(export))
    payload = json.loads(export.read_text(encoding="utf-8"))

    # 金丝雀：容器里声称的「转换脚本 / 可执行载荷」一旦被执行就会写下标记文件
    canary = tmpdir("g5canary")
    marker = canary / "EXECUTED"
    script = canary / "evil.py"
    script.write_text(f"open({str(marker)!r}, 'w').write('x')\n", encoding="utf-8")

    payload["setting"]["original_name"] = "../../逃逸到上层/evil"
    payload["setting"]["world_package"]["meta"]["original_name"] = "../../逃逸到上层/evil"
    payload["runtime"]["seed"] = "/etc/passwd\x00"
    payload["container"]["name"] = "C:\\Windows\\System32\\evil.exe"
    payload["container"]["converters"] = {"2.0->1.0": str(script)}
    payload["setting"]["conversion"] = {"script": str(script), "exec": [str(script)]}
    payload["setting"]["cards"][0]["appearance"] = "#!/bin/sh\nrm -rf /"
    import isekai_core.world.portable as portable

    payload["integrity"]["digest"] = portable._digest({"setting": payload["setting"], "runtime": payload["runtime"]})
    path = tmpdir("g5c") / "payload.isekai.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    outside = sorted(p.name for p in canary.iterdir())
    imported = (await ctx.mgmt.call("instance.import", path=str(path)))["instance"]
    stored = ctx.store.instance_get(imported["id"])
    settings = json.loads(stored["setting"])
    after = sorted(p.name for p in canary.iterdir())

    ok = not marker.exists() and outside == after and stored["name"] == "../../逃逸到上层/evil"
    return ("PASS" if ok else "FAIL"), (
        f"容器内声称的转换脚本 {script.name} 被执行={marker.exists()}（金丝雀目录内容 {outside}→{after}）；"
        f"路径型载荷导入后：实例名={stored['name']!r}（当普通名称存下，不在实例根外落盘）、"
        f"种子={stored['seed']!r}（原样存为不透明数据，未当路径使用）、"
        f"外观文本={settings['cards'][0]['appearance']!r}（原样存为文本，未执行）；"
        f"容器是单文件 JSON（无归档 / 符号链接 / 载荷字段），导入只走 json.loads"
    )


async def g6_black_box_surface(ctx: Ctx) -> tuple[str, str]:
    """§3.5：黑箱是产品接口约束——性格 / 记忆不在管理面暴露。"""
    package = mypkg()
    card = mycard(package)
    info = (await ctx.mgmt.call("instance.create", package=package, cards=[card]))["instance"]
    timeline = ctx.store.timeline_list(info["id"])[0]["id"]
    ctx.store.memory_add({
        "id": "mm-box", "instance_id": info["id"], "timeline_id": timeline,
        "character_id": card["meta"]["card_id"], "text": SECRET_MEMORY, "kind": "fact", "sources": [],
        "happened_world": 0, "learned_world": 0, "recorded_world": 0, "semantic_watermark": 0,
        "strength": 0.5, "confidence": 0.5,
    })
    detail = json.dumps(await ctx.mgmt.call("instance.info", id=info["id"]), ensure_ascii=False)
    clock = json.dumps(await ctx.mgmt.call("runtime.clock", instance_id=info["id"], timeline_id=timeline), ensure_ascii=False)
    commits = json.dumps(await ctx.mgmt.call("runtime.commits", instance_id=info["id"], timeline_id=timeline), ensure_ascii=False)
    setting = json.dumps(await ctx.mgmt.call("instance.setting", id=info["id"]), ensure_ascii=False)
    memory_leak = SECRET_MEMORY in (detail + clock + commits)
    # 管理面只该看到「锁了什么」的公开面：性格数值、实情层正文、卡片其余字段都不该出现
    payload = json.loads(setting)["setting"]
    blob = json.dumps(payload, ensure_ascii=False)
    exposed = [
        key for key in ("initial_units", "confidence", "canon", "narratives", "self_knowledge", "creator")
        if key in blob
    ]
    ok = not memory_leak and not exposed
    return ("PASS" if ok else "FAIL"), (
        f"instance.info / runtime.clock / runtime.commits 里出现记忆正文={memory_leak}；"
        f"instance.setting（mgmt 面可直呼，CLI 子命令 world_cli.py:47）返回整份锁定设定 {len(setting)} 字符："
        f"内部字段命中={exposed}；"
        f"§3.5 允许的是「实例 / 时间线 / 提交的必要管理元数据和操作」，性格数值与实情层不在其中"
        f"（桌面 UI 未调用该 op）"
    )


CHECKS: list[Check] = [
    Check("A1", "附录 C1 / §3.3", "改 / 删源模板与角色卡后既有实例内容不变", a1_source_decoupled, "world/instances.py:114-135, world/package.py:153-168"),
    Check("A2", "附录 C1 / §3.4", "锁定实例不能改历法或既有角色卡（无写入口）", a2_locked_no_writer, "world/ops.py:299-505（无设定写 op）"),
    Check("A3", "附录 C1 / §3.4", "补卡只扩充角色集合，不改既有设定", a3_add_card_extend_only, "world/ops.py:250-264, runtime/service.py:2889-2900"),
    Check("A4", "§3.7 版本与生命周期", "补卡三处留痕：快照角色定义 + 成员资格 + 加入提交", a4_join_trace_and_commit, "runtime/service.py:2889, store.py:492-502"),
    Check("A5", "§3.7 版本与生命周期", "同一补卡请求重试返回原子发布结果", a5_join_idempotent, "world/ops.py:250-264"),
    Check("A6", "§3.7 语义 / 目标线与加入事件", "补卡可自定义加入世界事件并登记", a6_join_event, "runtime/service.py:2835-2944"),
    Check("B1", "附录 C2", "新建与修订共用校验；失败不覆盖确认版本", b1_shared_validation, "world/generator.py, world/ops.py:341-348"),
    Check("C1", "附录 C3 / §7.4", "创建 / 导入 / 重命名并发下仍全局唯一", c1_naming_unique, "world/package.py:30-48, world/instances.py:203-216"),
    Check("C2", "§7.2", "原始名称记录独立，改名不改它，导入优先读它", c2_original_name, "world/package.py:127-135, world/instances.py:70-75"),
    Check("D1", "附录 C4 / §7.1", "多线导出再导入恢复对话 / 披露 / 记忆 / 引用", d1_roundtrip, "world/portable.py:40-121, store.py:2163-2272"),
    Check("D1b", "附录 C4 / §7.1 / §九阶段 4", "导入件的提交闭包可用（回滚 / 分叉不丢历史）", d1b_commit_closure, "world/portable.py:81-91, store.py:1730-1748"),
    Check("D2", "附录 C5 / §7.3", "导入全部冻结、无运输补算、不恢复凭据 / 绑定", d2_import_frozen, "world/portable.py:291-338, store.py:1658-1679"),
    Check("D3", "附录 C6 / §7.5", "不兼容 / 损坏 / 恶意容器原子失败", d3_atomic_failures, "world/portable.py:157-201"),
    Check("D4", "附录 C7 / §7.1", "包中无激活状态与凭据；日志不复制内部正文", d4_container_and_log_leak, "world/portable.py:98-121, store.py:1634-1656"),
    Check("D5", "附录 C7 / §3.5", "导入错误不泄露内部事件 / 性格 / 记忆", d5_import_error_no_leak, "world/portable.py:176-201"),
    Check("E1", "附录 C8", "普通聊天不隐式改公理 / 不建实例 / 不建时间线", e1_chat_does_not_rewrite, "session.py, world/ops.py:554-562"),
    Check("E2", "附录 C9 / §3.6", "回填不推翻已确认内容；非法引用不能创建实例", e2_backfill_respects_confirmed, "runtime/events.py:336-400, world/validate.py:475-520"),
    Check("E3", "附录 C10 / §2.2", "结构引用必须指向登记实体；署名不补造生平；在册不等于已知", e3_registry_references, "world/validate.py:614-686, 475-520"),
    Check("E4", "附录 C11 / 附录 D", "最小合法包不被拒；空壳史料 / 非法寿命 / 越权知识仍失败", e4_minimum_content, "world/validate.py:475-520, world/cards.py:145-201"),
    Check("E5", "附录 C12 / §2.4", "无 Key 可手编 / 草稿 / 导入；用量上限暂停并复用产物", e5_draft_and_budget, "world/ops.py:355-386, 719-779, runtime/service.py:1247-1300"),
    Check("E6", "附录 C13 / 附录 D", "未声明制度 / 惯例不拒题材也不隐式补造；声明的须自洽", e6_institutions_optional, "world/validate.py:208-292"),
    Check("F1", "附录 C14 / §3.7 时间锚定", "补卡锚定补入时刻；出生 / 死亡 / 知识越权被拒；失败不改实例", f1_join_anchoring, "runtime/service.py:2853-2900"),
    Check("F2", "附录 C15 / §3.7", "回滚跨过加入点角色一致退出；分叉则继承", f2_rollback_membership, "runtime/service.py:570-636, store.py:1880-1910"),
    Check("F3", "附录 C16 / §3.7 隔离", "补入角色与既有角色默认隔离；已相识不改变", f3_isolation, "runtime/service.py:2904-2920, cards()"),
    Check("G1", "§2.3", "校验指出模板字段；嵌套 / 文本 / 集合超限明确拒绝", g1_validation_limits, "world/validate.py:107-130"),
    Check("G2", "§2.5", "未知必需能力在确认前报错，不能创建实例", g2_unknown_capability, "world/validate.py:84-91"),
    Check("G3", "§7.1", "一致水位导出；中途失败不留伪装包、不破坏旧件", g3_export_watermark, "world/portable.py:124-141, 52-57"),
    Check("G4", "§7.6", "compatible / convertible / blocked 三态；blocked 阻断推进", g4_compat_states, "world/instances.py:141-158, runtime/service.py:1390-1397"),
    Check("G5", "§7.5", "路径穿越 / 绝对路径 / 可执行载荷不生效", g5_hostile_payload, "world/portable.py:144-154, ops.py:106-118"),
    Check("G6", "§3.5", "管理面不暴露性格数值 / 记忆 / 事件正文", g6_black_box_surface, "world/ops.py:427-454, 485-486"),
]

DEFERRED_NOTE: dict[str, str] = {
    "B2": (
        "附录 C2 尾句「两端解释同一包一致」：仓库内无安卓端实现（DESIGN.md 阶段 7 仍未落地），"
        "桌面端与 CLI 都走同一 world.ops 面（ops.py:38-103），没有第二套校验器可供实测；"
        "同一条款在 WORLD_SETTING_SPEC §2.5「共享生成器」处也只能核到「单端一致」。"
    ),
    "H1": (
        "§3.6 创建期历史回填的「一档文本」批量编纂与联合校验步骤："
        "本仓库的回填只把包内已写定的初始事件 / 传闻落成条目（runtime/events.py:336-400，不施加效果、"
        "不生成文本、不抽样骨架），没有可实测的「分时代抽样 / 10–20 条一批 / 传本选载范围」行为；"
        "该段设计属实现级待定项（§十），本次不作为失败计。"
    ),
    "H2": (
        "§2.3「文件规模」限额：结构限额（深度 / 节点 / 文本 / 集合）实测都生效，"
        "但读取前没有按字节数的文件规模上限（world/package.py:138-150 直接 read_text）；"
        "超过 1 MiB 的包只要能通过结构限额就会被读进来。属「数值实现时标定」范围，记 DEFERRED。"
    ),
    "H3": (
        "§7.5 / §7.6「可信转换器」：仓库里没有任何转换器实现（无注册表、无执行路径），"
        "instances.compatibility 只产出 compatible / convertible / blocked 判定，convertible 之后没有"
        "「在副本上转换 → 重新完整校验 → 原子发布」的行为可实测；§3.7「补卡后再次补入必须使用新的加入版本」"
        "里的旧成员资格撤销路径也走同一处缺口。属 §十「转换器注册格式」待定项，记 DEFERRED。"
    ),
}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    selected = {item.strip().upper() for item in args.only.split(",") if item.strip()}
    if args.list:
        for check in CHECKS:
            print(f"{check.cid:4} {check.clause}  {check.expected}")
        for cid, note in DEFERRED_NOTE.items():
            print(f"{cid:4} DEFERRED  {note[:60]}…")
        return 0

    logmod.setup_logging(tmpdir("logs") / "logs")
    results: list[tuple[str, str, str, str, str]] = []
    passed = failed = deferred = 0
    for check in CHECKS:
        if selected and check.cid.upper() not in selected:
            continue
        root = tmpdir(check.cid)
        status, evidence = "FAIL", ""
        try:
            async with core(root) as ctx:
                status, evidence = await asyncio.wait_for(check.fn(ctx), timeout=600)
        except asyncio.TimeoutError:
            evidence = "检查超时（600s）"
        except Exception as exc:  # noqa: BLE001
            evidence = f"探针自身异常：{type(exc).__name__}: {exc}；{traceback.format_exc().splitlines()[-3:]}"
        results.append((check.cid, status, check.clause, check.expected, evidence))
        print(f"[{check.cid}] {status}  {check.clause} — {check.expected}")
        print(f"      证据：{evidence}")
        print(f"      复现：.venv/Scripts/python.exe scripts/_audit2_ws.py --only {check.cid}")
        print()
        if status == "PASS":
            passed += 1
        else:
            failed += 1
    for cid, note in DEFERRED_NOTE.items():
        if selected and cid.upper() not in selected:
            continue
        results.append((cid, "DEFERRED", "—", "—", note))
        print(f"[{cid}] DEFERRED — {note}")
        print()
        deferred += 1
    print(f"TOTAL {passed + failed + deferred} PASS {passed} FAIL {failed} DEFERRED {deferred}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
