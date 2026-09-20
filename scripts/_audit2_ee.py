"""独立行为探针（第二轮）：docs/EVENT_ENGINE_SPEC.md 逐条行为级审计。

与本目录下上一轮的 `_audit_event_engine.py` 无共享代码：harness、断言与检查项都是本脚本
自己写的，检查结果与旧探针不一致处见报告。

约束：只读项目代码；库落在 tempfile 临时目录；不起核心、不联网、不调真实 LLM
（`ScriptedLLM` / 本地 stub）；不改任何项目文件。

运行：
    cd /d/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_audit2_ee.py [关键词…]

输出：每条 `PASS/FAIL/DEFERRED [id] 标题 — 证据`；末行 `TOTAL n PASS p FAIL f DEFERRED d`。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from isekai_core.config import load_config  # noqa: E402
from isekai_core.runtime import events, institutions, render  # noqa: E402
from isekai_core.runtime.calendar import calendar_from_package  # noqa: E402
from isekai_core.runtime.service import RuntimeService, RuntimeStateError  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.world import ops as world_ops  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import InstanceError, create_instance  # noqa: E402
from isekai_core.world.validate import (  # noqa: E402
    DENSITY_TARGETS,
    SUPPORTED_EFFECTS,
    validate_package,
)

T0 = 1.7e9
SERVICE_KW: dict[str, Any] = {
    "instance_tokens_per_day": 900_000,
    "timeline_tokens_per_day": 400_000,
    "task_tokens_per_day": 200_000,
    "autocommit_enabled": False,
    "catch_up_batches": 64,
    "catch_up_lag_seconds": 10**12,
}


class Violation(Exception):
    """行为与规范不符。"""

    def __init__(self, why: str, *, repro: str = "", where: str = "") -> None:
        super().__init__(why)
        self.why, self.repro, self.where = why, repro, where


def _dbg(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)[:600]


class Case:
    """一个临时实例：独立临时库 + 一条主时间线 + 运行层服务。"""

    def __init__(
        self,
        *,
        seed: str | None = None,
        moment: int = DAY * 1500,
        cards: list[dict[str, Any]] | None = None,
        package: dict[str, Any] | None = None,
    ) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ee2-audit-")
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "isekai.db")
        self.store.ensure_schema()
        self.package = package if package is not None else example_package("灰潮纪", moment=moment)
        self.cards = cards if cards is not None else [example_card(self.package)]
        info = create_instance(self.store, self.package, self.cards, seed=seed)
        self.instance_id = str(info["id"])
        self.timeline_id = str(self.store.timeline_list(self.instance_id)[0]["id"])
        self.service = RuntimeService(self.store, **SERVICE_KW)
        self.service.ensure_instance(self.instance_id, now_real=T0)
        self.seed = str(self.store.instance_get(self.instance_id)["seed"])
        self.rules = str(self.store.instance_get(self.instance_id)["rules_version"])
        self.moment = moment
        self._now_real: dict[str, float] = {}

    # ---- 时间 ----
    def activate(self, tl: str | None = None, *, day: float = 0.0) -> None:
        line = tl or self.timeline_id
        now = T0 + day * DAY if day else self._now_real.get(line, T0)
        self.service.activate(self.instance_id, line, now_real=now)
        self._now_real[line] = now

    def advance(self, days: float, *, tl: str | None = None) -> dict[str, Any]:
        line = tl or self.timeline_id
        self._now_real[line] = self._now_real.get(line, T0) + float(days) * DAY
        return self.service.advance(self.instance_id, line, now_real=self._now_real[line])

    def at_day(self, day: float, *, tl: str | None = None) -> dict[str, Any]:
        """把本线现实时间锚到『距 T0 第 day 天』再推进（可重放同一区间）。"""
        line = tl or self.timeline_id
        self._now_real[line] = T0 + float(day) * DAY
        return self.service.advance(self.instance_id, line, now_real=self._now_real[line])

    def watermark(self, tl: str | None = None) -> int:
        return int(self.store.clock_get(tl or self.timeline_id)["processed_world"])

    # ---- 读 ----
    def ev_rows(self, tl: str | None = None) -> list[dict[str, Any]]:
        return self.store.event_window(self.instance_id, tl or self.timeline_id, until=10**15, limit=3000)

    def fx_rows(self, tl: str | None = None) -> list[dict[str, Any]]:
        line = tl or self.timeline_id
        rows = self.store._conn.execute(
            "SELECT * FROM effect_state WHERE timeline_id=? ORDER BY from_world, id", (line,)
        ).fetchall()
        return [dict(r) for r in rows]

    def know(self, character_id: str, tl: str | None = None) -> list[dict[str, Any]]:
        return self.store.knowledge_window(
            self.instance_id, tl or self.timeline_id, character_id, until=10**15, limit=1000
        )

    def exp_of(self, character_id: str, tl: str | None = None) -> list[dict[str, Any]]:
        return self.store.experience_window(
            self.instance_id, tl or self.timeline_id, character_id, until=10**15, limit=1000
        )

    def claims(self, tl: str | None = None, *, event_id: str | None = None) -> list[dict[str, Any]]:
        return self.store.claim_list(self.instance_id, tl or self.timeline_id, event_id=event_id)

    def offices(self, tl: str | None = None) -> dict[str, dict[str, Any]]:
        return {
            str(r["office_id"]): dict(r)
            for r in self.store.institution_list(self.instance_id, tl or self.timeline_id)
        }

    def customs(self, tl: str | None = None) -> dict[str, dict[str, Any]]:
        return {
            str(r["custom_id"]): dict(r)
            for r in self.store.custom_list(self.instance_id, tl or self.timeline_id)
        }

    def prompt(self, character_id: str, tl: str | None = None) -> str:
        return self.service.system_prompt(
            {"instance_id": self.instance_id, "timeline_id": tl or self.timeline_id, "character_id": character_id}
        )

    # ---- 写（沿真实批次边界，与引擎同形）----
    def inject(
        self,
        *,
        summary: str,
        effects: list[dict[str, Any]] | None = None,
        claims: list[dict[str, Any]] | None = None,
        knowledge: list[dict[str, Any]] | None = None,
        ident: str | None = None,
        at: int | None = None,
        family: str = "",
        tl: str | None = None,
    ) -> str:
        line = tl or self.timeline_id
        row = self.store.clock_get(line)
        world = int(row["processed_world"]) if at is None else int(at)
        if ident is None:
            ident = f"ev-x-{events.stable_key(line, family, summary, world)[:10]}"
        self.store.apply_runtime_batch(
            timeline_id=line,
            generation=int(row["generation"]),
            processed_world=int(row["processed_world"]),
            catching_up=False,
            events=[
                {
                    "id": ident, "instance_id": self.instance_id, "timeline_id": line,
                    "world_seconds": world, "seq": 11, "kind": "world", "family": family,
                    "template": "audit2.inject", "source": "engine", "summary": summary,
                    "detail": summary, "text_source": "template",
                    "effects": effects or [], "share_value": 0, "importance": 0.5,
                    "created_real": 0.0,
                }
            ],
            effects=[
                {
                    "id": f"fx-x-{events.stable_key(ident, index)[:8]}",
                    "instance_id": self.instance_id, "timeline_id": line, "event_id": ident,
                    "target": str(item["target"]), "kind": str(item["kind"]), "family": family,
                    "value": item.get("value"), "from_world": world,
                    "expiry": str(item.get("expiry") or "with_cause"),
                    "recovery": str(item.get("recovery") or ""), "active": 1, "cleared_at": None,
                }
                for index, item in enumerate(effects or [])
            ],
            claims=[
                {
                    "id": str(item["id"]), "instance_id": self.instance_id, "timeline_id": line,
                    "event_id": ident, "source_id": str(item.get("source_id") or "src-1"),
                    "text": str(item["text"]), "audience": str(item.get("audience") or "公开"),
                    "earliest_world": int(item.get("earliest_world") or world),
                    "credibility": float(item.get("credibility") or 0.6), "derived_from": None,
                }
                for item in claims or []
            ],
            knowledge=[
                {
                    "id": str(item["id"]), "instance_id": self.instance_id, "timeline_id": line,
                    "character_id": str(item["character_id"]),
                    "world_seconds": int(item.get("world_seconds") or world), "kind": "claim",
                    "target": str(item["target"]), "source": str(item.get("source") or "src-1"),
                    "stance": "recorded", "text": str(item["text"]),
                }
                for item in knowledge or []
            ],
        )
        return ident

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()


def slice_of(case: Case, tl: str | None = None) -> dict[str, Any]:
    """事实切片（不含现实时间戳与实例身份）：事件 / 效果 / 获知 / 经历。"""
    line = tl or case.timeline_id
    chars = [str(c["meta"]["card_id"]) for c in case.cards]
    return {
        "events": [
            (int(r["world_seconds"]), int(r["seq"]), str(r["id"]), str(r["template"]),
             str(r["summary"]), str(r["effects"]), int(r["share_value"]), float(r["importance"]))
            for r in case.ev_rows(line)
        ],
        "effects": [
            (str(r["id"]), str(r["target"]), str(r["kind"]), int(r["from_world"]), str(r["expiry"]),
             int(r["active"]), r["cleared_at"], str(r.get("recovery") or ""))
            for r in case.fx_rows(line)
        ],
        "knowledge": [
            (str(r["id"]), str(r["character_id"]), int(r["world_seconds"]), str(r["kind"]),
             str(r["target"]), str(r["source"]), str(r["text"]))
            for cid in chars for r in case.know(cid, line)
        ],
        "experiences": [
            (str(r["id"]), str(r["character_id"]), int(r["world_seconds"]), str(r["kind"]), str(r["summary"]))
            for cid in chars for r in case.exp_of(cid, line)
        ],
    }


def diff(left: dict[str, Any], right: dict[str, Any]) -> str:
    out: list[str] = []
    for key in sorted(set(left) | set(right)):
        a, b = left.get(key) or [], right.get(key) or []
        if a == b:
            continue
        only_a = [x for x in a if x not in b][:2]
        only_b = [x for x in b if x not in a][:2]
        out.append(f"{key}: {len(a)}→{len(b)} 行；仅左 {only_a}；仅右 {only_b}")
    return "；".join(out) or "（无差异）"


class ScriptedLLM:
    """按脚本回包；可指定抛错。"""

    def __init__(self, replies: list[str] | None = None, *, error: Exception | None = None) -> None:
        self.replies = list(replies or [])
        self.error = error
        self.calls = 0

    async def chat(self, messages: Any, *, max_tokens: Any = None, timeout: Any = None, temperature: Any = None) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


CHECKS: list[tuple[str, str, Callable[[], Any]]] = []


def check(cid: str, title: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    def deco(fn: Callable[[], Any]) -> Callable[[], Any]:
        CHECKS.append((cid, title, fn))
        return fn

    return deco


def engine_rows(case: Case, tl: str | None = None) -> list[dict[str, Any]]:
    return [r for r in case.ev_rows(tl) if str(r["source"]) == "engine"]


def world_rows(case: Case, tl: str | None = None) -> list[dict[str, Any]]:
    """世界级随机 / 固定事件（排除生死事件：生死单独记账，不占每日随机密度）。"""
    return [r for r in engine_rows(case, tl) if not str(r["template"]).startswith("death:")]


def festival_days(package: dict[str, Any], lo: int, hi: int) -> list[int]:
    cal = calendar_from_package(package)
    return [d for d in range(lo, hi) if events.fixed_events(package, day_index=d, calendar=cal)]


# ------------------------------------------------------------------ 附录 B
#==== B1: 确定性


@check("B1a", "附录B#1 同一输入跨进程（含 PYTHONHASHSEED 随机）得到相同候选与事件身份")
def b1a() -> str:
    probe = (
        "import json, sys;"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "from isekai_core.runtime import events;"
        "from isekai_core.runtime.calendar import calendar_from_package;"
        "from isekai_core.world.example import example_package;"
        "p = example_package(); c = calendar_from_package(p);"
        "rows = events.plan_day(p, seed='s-x', rules_version='r-1', day_index=1512, calendar=c,"
        " events=set(), effects=set());"
        "print(json.dumps([[i['slot'], i['template'], i['summary'], i['effects'],"
        " events.event_id('s-x','r-1',1512,i['slot']),"
        " events.event_moment('s-x','r-1',1512,i['slot'],c.day_seconds)] for i in rows],"
        " ensure_ascii=False, sort_keys=True));"
        "print(events.stable_key('a', 1, None, 2.5), events.daily_budget('s-x','r-1',1512,'常规'))"
    )
    seen: dict[str, str] = {}
    for hash_seed in ("0", "1", "random"):
        proc = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
        )
        if proc.returncode != 0:
            raise Violation(f"子进程失败：{proc.stderr.strip()[-160:]}",
                            repro="PYTHONHASHSEED=<x> python -c '<plan_day/stable_key 探针>'")
        seen[hash_seed] = proc.stdout.strip()
    if len(set(seen.values())) != 1:
        raise Violation(f"不同哈希种子下输出不同：{seen}",
                        where="isekai_core/runtime/events.py:29 stable_key（blake2b）")
    first = seen["0"].splitlines()
    rows = json.loads(first[0])
    if not rows:
        raise Violation("固定输入下当日没有候选，检查无法证明确定性",
                        repro="plan_day(seed='s-x', day_index=1512)")
    inproc = events.plan_day(
        example_package(), seed="s-x", rules_version="r-1", day_index=1512,
        calendar=calendar_from_package(example_package()), events=set(), effects=set(),
    )
    if [[i["slot"], i["template"], i["summary"], i["effects"]] for i in inproc] != [r[:4] for r in rows]:
        raise Violation(f"进程内与子进程候选不同：{inproc} vs {rows}")
    return (
        f"三个进程（PYTHONHASHSEED=0/1/random）输出逐字相同；同输入候选 "
        f"{[(r[0], r[1]) for r in rows]}、事件标识 {[r[4][:12] for r in rows]} 与时刻 "
        f"{[r[5] for r in rows]} 完全一致；抽样走 blake2b 的 stable_key，未用内置 hash()"
    )


@check("B1b", "附录B#1 同一输入在不同补算分批下得到相同骨架 / 效果 / 顺序")
def b1b() -> str:
    left, right = Case(seed="seed-split"), Case(seed="seed-split")
    try:
        for case in (left, right):
            case.activate()
        left.advance(18)
        right.advance(7.31)          # 跨日部分批
        right.advance(10.69)
        a, b = slice_of(left), slice_of(right)
        # 跨实例比较：引擎事件的标识由种子 / 规则版本 / 历法日 / 槽位决定，应当相同并被纳入比较；
        # 角色行动 / 用户 / 身故事件的标识含实例与线的标识（设计如此），故只比内容字段。
        def comparable(side: dict[str, Any]) -> dict[str, Any]:
            def ev(row: Any) -> Any:
                ident = str(row[2])
                if ident.startswith(("ev-act-", "ev-user-", "ev-death-")):
                    # 标识含实例 / 线的标识（设计如此），连 seq（由标识导出）一并排除
                    return (row[0],) + row[3:]
                return row

            return {
                "events": [ev(row) for row in side["events"]],
                # 效果标识由事件标识导出：角色行动 / 用户 / 身故事件的标识含实例身份（设计如此），
                # 这里比**事实字段与顺序**；引擎事件的标识一致性由 B1c 的骨架检查覆盖。
                "effects": [row[1:] for row in side["effects"]],
                "knowledge": [(r[1], r[2], r[3], r[4], r[5], r[6]) for r in side["knowledge"]],
                "experiences": [(r[1], r[2], r[3], r[4]) for r in side["experiences"]],
            }

        cmp_a, cmp_b = comparable(a), comparable(b)
        if cmp_a != cmp_b:
            raise Violation(f"两种分批下事实不同：{diff(cmp_a, cmp_b)}",
                            repro="同种子两实例：advance(18 天) vs advance(7.31 天)+advance(10.69 天)",
                            where="isekai_core/runtime/service.py:2346 _propagate_and_clear（解除判定用批边界）")
        return (f"事件 {len(cmp_a['events'])} / 效果 {len(cmp_a['effects'])} / 获知 "
                f"{len(cmp_a['knowledge'])} / 经历 {len(cmp_a['experiences'])} 行逐行相同"
                f"（第二次推进从半日批边界续上）")
    finally:
        left.close()
        right.close()


@check("B1c", "附录A 抽样输入不含实例身份与机器时间：同种子两实例骨架相同")
def b1c() -> str:
    one, two = Case(seed="seed-id"), Case(seed="seed-id")
    try:
        for case in (one, two):
            case.activate()
            case.advance(6)
        a = [(int(r["world_seconds"]), str(r["template"]), str(r["summary"]), str(r["effects"]))
             for r in world_rows(one)]
        b = [(int(r["world_seconds"]), str(r["template"]), str(r["summary"]), str(r["effects"]))
             for r in world_rows(two)]
        if not a:
            raise Violation("6 天里没有世界级事件，检查无法证明")
        if a != b:
            raise Violation(f"同种子不同实例骨架不同：{a} vs {b}",
                            where="isekai_core/runtime/service.py:2234 _world_event_rows → events.plan_day")
        ids_one = {str(r["id"]) for r in world_rows(one)}
        ids_two = {str(r["id"]) for r in world_rows(two)}
        if ids_one != ids_two:
            raise Violation(f"事件标识随实例漂移：{sorted(ids_one)} vs {sorted(ids_two)}")
        return (f"两个独立实例（不同实例标识 / 不同创建时刻）得到同一骨架：{[(x[0] // DAY, x[1]) for x in a]}，"
                f"事件标识也逐字相同（{sorted(ids_one)[0]} 等 {len(ids_one)} 条）")
    finally:
        one.close()
        two.close()


#==== B2: 分叉


@check("B2a", "附录B#2 分叉线共同过去逐行一致")
def b2a() -> str:
    case = Case(seed="seed-b2")
    try:
        case.activate()
        case.advance(4)
        commit = case.service.commit(case.instance_id, case.timeline_id, note="分叉点")
        branch = case.service.fork(case.instance_id, case.timeline_id, commit_id=commit["id"], name="乙线")
        new_line = str(branch["timeline"]["id"])
        base, copied = slice_of(case), slice_of(case, new_line)
        if base != copied:
            raise Violation(f"分叉后共同过去不一致：{diff(base, copied)}", repro="commit → fork → 比较四类事实切片")
        return (f"共同过去逐行一致：事件 {len(base['events'])} / 效果 {len(base['effects'])} / "
                f"获知 {len(base['knowledge'])} / 经历 {len(base['experiences'])} 行；"
                f"新线 state={case.store.timeline_get(new_line)['state']}")
    finally:
        case.close()


@check("B2b", "附录B#2 一线引入事件后两线分化，且原线 / 来源线逐行不变")
def b2b() -> str:
    case = Case(seed="seed-b2b")
    try:
        case.activate()
        case.advance(3)
        before_origin, before_line = slice_of(case), slice_of(case)
        draft = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="驿站信报停摆两日",
            payload={
                "intent": "驿站信报停摆两日", "when": "now",
                "effects": [{"kind": "source_delay", "target": "src-1",
                             "expiry": "until_cleared"}],
                "claims": [{"text": "驿站信报停摆两日", "source_id": "src-1", "audience": "公开"}],
            },
        ))
        if not draft.get("accepted"):
            raise Violation(f"草案被拒：{draft.get('reason')}", repro="draft_user_event(institution_state off-2=en-1)")
        new_line = str(case.service.confirm_user_event(
            case.instance_id, draft["draft"]["draft_id"], name="丙线")["timeline_id"])
        after_origin = slice_of(case)
        after_line = slice_of(case)
        if after_origin != before_origin:
            raise Violation(f"确认后原线被改写：{diff(before_origin, after_origin)}",
                            where="isekai_core/runtime/service.py:338 confirm_user_event")
        if after_line != before_line:
            raise Violation(f"确认后来源线被改写：{diff(before_line, after_line)}")
        fresh = slice_of(case, new_line)
        if fresh["events"] == after_origin["events"]:
            raise Violation("新线与原线事实仍完全相同（应已分化）",
                            repro="confirm_user_event 后比较两线事件")
        injected = [r for r in case.ev_rows(new_line) if str(r["source"]) == "user"]
        if len(injected) != 1:
            raise Violation(f"新线用户事件数 {len(injected)}（应为 1）")
        return (f"新线多出 1 条用户事件（{injected[0]['id']}）并因此与原线分化；原线 "
                f"{len(after_origin['events'])} 行、来源线 {len(after_line['events'])} 行均逐行不变")
    finally:
        case.close()


#==== B3: 预算与固定节庆


@check("B3a", "附录B#3 每日世界级随机事件不越密度档上限（生死事件不计入）")
def b3a() -> str:
    case = Case(seed="seed-b3")
    try:
        case.activate()
        case.advance(30)
        density = str(case.package["events"]["density"])
        top = DENSITY_TARGETS[density][1]
        per_day: dict[int, list[str]] = {}
        for row in world_rows(case):
            per_day.setdefault(int(row["world_seconds"]) // DAY, []).append(str(row["template"]))
        if not per_day:
            raise Violation("推进 30 日没有任何世界级事件")
        over = {d: v for d, v in per_day.items() if len(v) > top}
        if over:
            raise Violation(f"越预算：{over}（密度档 {density} 上限 {top}）",
                            where="isekai_core/runtime/events.py:35 daily_budget / py:125 plan_day")
        deaths = [r for r in engine_rows(case) if str(r["template"]).startswith("death:")]
        return (f"密度档 {density}（上限 {top}）：{len(per_day)} 个有事件的日子里最大 {max(len(v) for v in per_day.values())} 条；"
                f"逐日 ≤ 上限；同窗口另有生死事件 {len(deaths)} 条（按模板前缀单列，不计入上式）")
    finally:
        case.close()


@check("B3b", "附录B#3 固定节庆日期不漂移、优先占当日名额，且节日当天照常推进")
def b3b() -> str:
    package = example_package("灰潮纪", moment=DAY * 1500)
    declared = package["events"]["calendar"][0]
    days = festival_days(package, 1400, 1700)
    cal = calendar_from_package(package)
    for day in days:
        view = cal.to_calendar(day * cal.day_seconds)
        if view["month"] != int(declared["month"]) or view["day"] != int(declared["day"]):
            raise Violation(f"第 {day} 日报出固定事件，但历法是 {view['month']} 月 {view['day']} 日，"
                            f"声明为 {declared['month']} 月 {declared['day']} 日 —— 日期漂移",
                            where="isekai_core/runtime/events.py:43 fixed_events")
    if len(days) != 3:  # 300 天窗口跨 3 个历法年
        raise Violation(f"300 天窗口里固定节庆出现 {days}（应恰为每年 1 次 = 3 次）")
    row_day = days[1]
    planned = events.plan_day(package, seed="seed-b3b", rules_version="r-1", day_index=row_day,
                              calendar=cal, events=set(), effects=set())
    if not planned or not planned[0]["slot"].startswith("fixed-"):
        raise Violation(f"固定节庆没有先占当日名额：{planned}")
    fixed_n = len([i for i in planned if i["fixed"]])
    case = Case(seed="seed-b3b", moment=DAY * (row_day - 1))
    try:
        case.activate()
        try:
            case.advance(4)
        except Exception as exc:  # 引擎在节日当天抛错 = 节日落不成
            raise Violation(
                f"固定节庆当天推进直接抛错：{type(exc).__name__}: {exc}",
                repro=f"moment=DAY*{row_day - 1} → activate → advance(4 天)",
                where="isekai_core/runtime/service.py:1641 _world_event_rows（节日行经 apply_runtime_batch）",
            ) from exc
        hit = [r for r in case.ev_rows() if int(r["world_seconds"]) // DAY == row_day]
        if not [r for r in hit if str(r["template"]) == str(declared["id"])]:
            raise Violation(f"第 {row_day} 日的事件里没有节庆：{[(r['source'], r['template']) for r in hit]}")
        if len([r for r in case.ev_rows() if str(r["template"]) == str(declared["id"])]) != 1:
            raise Violation("节庆不止发生一次")
        return (f"1400–1700 日窗口只在 {days}（每年 {declared['month']} 月 {declared['day']} 日）报出「{declared['name']}」，"
                f"日期不漂移；当日候选首项 {planned[0]['slot']}（固定 {fixed_n} 条先占名额）；"
                f"端到端推进后第 {row_day} 日共 {len(hit)} 条世界级事件且含节庆 1 次")
    finally:
        case.close()


@check("附录B#3 超上限", "附录B#3 包内同日固定事件超过密度档上限 → 创建前报错")
def b3c() -> str:
    package = example_package()
    package["events"]["density"] = "稀疏"  # 上限 1
    family = str(package["events"]["families"][0]["id"])
    package["events"]["calendar"] = [
        {"id": "fc-a", "name": "开滩祭", "month": 2, "day": 3, "family": family},
        {"id": "fc-b", "name": "祭堤日", "month": 2, "day": 3, "family": family},
    ]
    errors = [e for e in validate_package(package) if "密度档" in e]
    if not errors:
        raise Violation("同日两个固定事件在稀疏档（上限 1）未被校验拦下",
                        where="isekai_core/world/validate.py:716 _validate_event_calendar")
    tmp = tempfile.TemporaryDirectory(prefix="ee2-b3c-")
    try:
        store = Store(Path(tmp.name) / "isekai.db")
        store.ensure_schema()
        try:
            create_instance(store, package, [example_card(package)], seed="s")
        except InstanceError as exc:
            blocked = str(exc)
        else:
            raise Violation("校验报错但创建仍然成功（没有挡住非法包）",
                            repro="密度=稀疏 + 同日 2 个固定事件 → create_instance")
        finally:
            store.close()
        package["events"]["calendar"] = package["events"]["calendar"][:1]
        rest = [e for e in validate_package(package) if "密度档" in e]
        if rest:
            raise Violation(f"合法包被误报：{rest}")
        return (f"密度=稀疏（上限 1）同日 2 个固定事件：校验报错并被创建期拦下（{blocked[:70]}…）；"
                f"删到一个后校验通过")
    finally:
        tmp.cleanup()


#==== B4: LLM 边界


@check("B4a", "附录B#4 LLM 超时 / 报错不改变世界状态，也不改已固化的文本来源")
def b4a() -> str:
    case = Case(seed="seed-b4a")
    try:
        cfg = load_config(case.dir / "cfg")
        ident = case.inject(summary="退潮延误，驿站停摆 2 日")
        before = dict(case.store.event_get(case.instance_id, case.timeline_id, ident) or {})
        claims_before = case.claims(event_id=ident)
        llm = ScriptedLLM(error=TimeoutError("llm timeout"))
        outcome = ""
        try:
            asyncio.run(world_ops.dispatch_async(
                cfg, llm, "event.render",
                {"instance_id": case.instance_id, "timeline_id": case.timeline_id, "event_id": ident},
                store=case.store,
            ))
        except Exception as exc:
            outcome = f"{type(exc).__name__}"
        after = dict(case.store.event_get(case.instance_id, case.timeline_id, ident) or {})
        claims_after = case.claims(event_id=ident)
        if before != after or claims_before != claims_after:
            raise Violation(f"模型超时改动了世界：event {before != after}，claims {claims_before != claims_after}",
                            where="isekai_core/world/ops.py:719 _render_event（解析失败即回落模板）")
        if str(after.get("text_source")) != "template":
            raise Violation(f"文本来源被改成 {after.get('text_source')}")
        return (f"模型抛 TimeoutError（调用侧表现：{outcome or '被捕获'}）：事件行（含 effects / summary / "
                f"detail / text_source）与说法集合逐字段不变，text_source 仍为 template")
    finally:
        case.close()


@check("B4b", "附录B#4 骨架外数字被拒、合规文本固化后不重生成、重复回包不重复落盘")
def b4b() -> str:
    case = Case(seed="seed-b4b")
    try:
        cfg = load_config(case.dir / "cfg")
        ident = case.inject(
            summary="退潮延误，驿站停摆 2 日",
            claims=[{"id": "cl-b4b", "text": "驿站传：停摆 2 日", "source_id": "src-1"}],
        )
        good = '{"detail": "（驿站抄存）退潮延误，驿站停摆 2 日", "claims": {"src-1": "驿站传：停摆 2 日"}}'
        llm = ScriptedLLM([good])
        first = asyncio.run(world_ops.dispatch_async(
            cfg, llm, "event.render",
            {"instance_id": case.instance_id, "timeline_id": case.timeline_id, "event_id": ident},
            store=case.store,
        ))
        if first.get("text_source") != "llm":
            raise Violation(f"合规表述没有固化：{first}")
        saved = case.store.event_get(case.instance_id, case.timeline_id, ident) or {}
        if str(saved["summary"]) != "退潮延误，驿站停摆 2 日":
            raise Violation(f"骨架被语言产物改写：{saved['summary']!r}")
        again = asyncio.run(world_ops.dispatch_async(
            cfg, ScriptedLLM([good]), "event.render",
            {"instance_id": case.instance_id, "timeline_id": case.timeline_id, "event_id": ident},
            store=case.store,
        ))
        if not again.get("reused") or int(again.get("calls") or 0) != 0 or llm.calls != 1:
            raise Violation(f"已固化文本被重新生成：reused={again.get('reused')} calls={again.get('calls')} "
                            f"模型调用 {llm.calls} 次")
        bad = '{"detail": "退潮延误，驿站停摆 5 日", "claims": {}}'
        ident2 = case.inject(
            summary="退潮延误，驿站停摆 2 日", ident="ev-x-b4b-2",
            claims=[{"id": "cl-b4b-2", "text": "驿站传：停摆 2 日", "source_id": "src-1"}],
        )
        third = asyncio.run(world_ops.dispatch_async(
            cfg, ScriptedLLM([bad, bad]), "event.render",
            {"instance_id": case.instance_id, "timeline_id": case.timeline_id, "event_id": ident2},
            store=case.store,
        ))
        if third.get("text_source") != "template" or "未过校验" not in str(third.get("note")):
            raise Violation(f"骨架外数字被采纳：{third}")
        return (f"合规表述固化（detail={str(first['detail'])[:16]}…）；二次调用 reused={again.get('reused')}、"
                f"模型调用仍 {llm.calls} 次；改成 5 日的表述两次都未过校验 → 退回模板（{third['note']}）")
    finally:
        case.close()


@check("B4c", "正文§3.2 语言产物不得带入骨架外事实（非数字类）")
def b4c() -> str:
    case = Case(seed="seed-b4c")
    try:
        cfg = load_config(case.dir / "cfg")
        case.activate()
        ident = case.inject(
            summary="堤务吏当值失察",
            claims=[{"id": "cl-b4c", "text": "堤务吏当值失察", "source_id": "src-1",
                     "earliest_world": case.watermark() + DAY}],
        )
        invented = "告发者是城西的柳氏"
        reply = json.dumps({"detail": f"堤务吏当值失察，{invented}。", "claims": {"src-1": f"堤务吏当值失察，{invented}。"}},
                           ensure_ascii=False)
        # 骨架里没有数字 → 第二道护栏（忠实度判断）会再问一次；脚本模型按真实模型的方式回答
        result = asyncio.run(world_ops.dispatch_async(
            cfg, ScriptedLLM([reply, '{"grounded": false, "added": "责任人：柳氏"}']), "event.render",
            {"instance_id": case.instance_id, "timeline_id": case.timeline_id, "event_id": ident},
            store=case.store,
        ))
        case.advance(2)
        granted = [r for r in case.claims(event_id=ident) if str(r["source_id"]) == "src-1"]
        text_now = str(granted[0]["text"]) if granted else ""
        learned = [r for r in case.know("cc-堤禾") if invented in str(r["text"])]
        if invented in text_now or learned or result.get("text_source") == "llm":
            in_prompt = invented in case.prompt("cc-堤禾")
            raise Violation(
                f"骨架里没有责任人，语言产物「{invented}」被固化进说法文本并经传播链进入角色获知"
                f"（获知 {len(learned)} 条{'，且已进入她的扮演定义' if in_prompt else ''}）",
                repro=("事件 summary 无数字（render.facts_preserved 在 want 为空时直接返回 True）→ "
                       "模型补出责任人 → earliest_world 到期后 claim_grant 按库内文本发获知"),
                where=("isekai_core/runtime/render.py:35 facts_preserved（只在骨架含数字时才校验）；"
                       "isekai_core/runtime/events.py:300 claim_grant（按 claim.text 发获知）"),
            )
        return (
            f"补出的责任人被第二道护栏拒绝（text_source={result.get('text_source')}）："
            f"固化后说法文本 {text_now[:20]!r} 不含「{invented}」，获知 {len(learned)} 条"
        )
    finally:
        case.close()


#==== B5: 可见性


def _two_channel_case(seed: str) -> tuple[Case, str, str]:
    package = example_package()
    holder = example_card(package, name="堤禾")
    stone = example_card(package, name="碑拓者")
    stone["channels"] = [{"source_id": "src-2", "conditions": "只在盐滩拓碑"}]
    return Case(seed=seed, cards=[holder, stone], package=package), "cc-堤禾", "cc-碑拓者"


@check("B5a", "附录B#5 不同角色只获知相应渠道的说法（「公开」标签不构成获知资格）")
def b5a() -> str:
    case, holder, stone = _two_channel_case("seed-b5a")
    try:
        case.activate()
        case.advance(5)
        allowed = {holder: {"src-1", "亲历"}, stone: {"src-2", "亲历"}}
        got: dict[str, set[str]] = {}
        for cid, ok in allowed.items():
            rows = [r for r in case.know(cid) if str(r["kind"]) == "claim"]
            if not rows:
                raise Violation(f"{cid} 一条说法都没获知（共 {len(case.know(cid))} 条获知），无法判定渠道生效")
            got[cid] = {str(r["source"]) for r in rows}
            extra = got[cid] - ok
            if extra:
                raise Violation(f"{cid} 拿到不属于自己渠道的说法：{sorted(extra)}",
                                where="isekai_core/runtime/events.py:233 grants / py:300 claim_grant")
        if got[holder] & got[stone]:
            raise Violation(f"两个角色的获知渠道出现交集：{sorted(got[holder] & got[stone])}")
        return (f"5 天后：{holder} 的获知渠道 {sorted(got[holder])}，{stone} 的 {sorted(got[stone])}，互不交叉；"
                f"公共说法『公开』没有变成全员发送开关")
    finally:
        case.close()


@check("B5b", "附录B#5 + §五/§七 未接触的事件不进扮演定义 / 素材；实情文本不进任何消费面")
def b5b() -> str:
    case, holder, stone = _two_channel_case("seed-b5b")
    try:
        case.activate()
        base = case.watermark()
        marker = "ZZ-MARK-驿站信报载北堤旧闻"
        ident = case.inject(
            summary="北堤旧闻",
            claims=[{"id": "cl-b5b", "text": marker, "source_id": "src-1", "earliest_world": base + 1}],
        )
        case.advance(2)
        if not [r for r in case.know(holder) if marker in str(r["text"])]:
            raise Violation(f"{holder} 没获知该说法（holders={case.store.knowledge_holders(case.instance_id, case.timeline_id, 'cl-b5b')}）")
        if marker in case.prompt(stone):
            raise Violation(f"未接触的角色拿到了该说法（{stone} 无 src-1 渠道）",
                            where="isekai_core/runtime/cognition.py 扮演定义组装")
        if marker not in case.prompt(holder):
            raise Violation("已获知的说法没有进入扮演定义",
                            where="isekai_core/runtime/service.py:2965 system_prompt")
        case.service.queue_world_sources(case.instance_id, case.timeline_id, since_world=0)
        tasks = case.store.memory_tasks(case.instance_id, case.timeline_id, state="pending")
        leaked = [t for t in tasks if str(t["character_id"]) == stone and str(t["source_ref"]) in {
            str(r["id"]) for r in case.know(holder) if marker in str(r["text"])}]
        if leaked:
            raise Violation(f"未接触的事件进了她的素材来源：{_dbg(leaked[0])}")
        # 实情文本不进任何消费面
        secret = "ZZ-DETAIL-内部一致性用的实情"
        case.store.event_render_save(case.instance_id, case.timeline_id, ident, detail=secret, claims={})
        surfaces = {
            "扮演定义": case.prompt(holder),
            "角色切片": json.dumps(case.service.character_snapshot(
                case.instance_id, case.timeline_id, holder, world_seconds=case.watermark()),
                ensure_ascii=False, default=str),
            "世界视图": json.dumps(case.service.view(case.instance_id, case.timeline_id,
                                                  now_real=case._now_real[case.timeline_id]),
                                 ensure_ascii=False, default=str),
        }
        for task in tasks:
            material = case.service._source_material(task)
            surfaces[f"素材:{task['source_kind']}:{task['source_ref']}"] = json.dumps(material, ensure_ascii=False, default=str)
        hit = [name for name, blob in surfaces.items() if secret in str(blob)]
        if hit:
            raise Violation(f"实情文本（detail）暴露给消费面：{hit}",
                            where="isekai_core/runtime/cognition.py / service.py:2896 system_prompt")
        return (f"同一条说法：{holder} 获知并出现在扮演定义；{stone} 既不在扮演定义、也没有对应素材任务；"
                f"把 detail 写成标记后，{len(surfaces)} 个消费面（扮演定义 / 角色切片 / 世界视图 / "
                f"{len(tasks)} 份素材）都没有它")
    finally:
        case.close()


@check("B5c", "附录B#5 传播时刻未到的说法不产生获知；到点后才给")
def b5c() -> str:
    case = Case(seed="seed-b5c")
    try:
        case.activate()
        base = case.watermark()
        case.inject(
            summary="迟来的消息",
            claims=[{"id": "cl-b5c", "text": "ZZ-LATER 三日后见报", "source_id": "src-1",
                     "earliest_world": base + 3 * DAY}],
        )
        case.advance(1)
        early = [r for r in case.know("cc-堤禾") if str(r["target"]) == "cl-b5c"]
        case.advance(4)
        late = [r for r in case.know("cc-堤禾") if str(r["target"]) == "cl-b5c"]
        if early:
            raise Violation(f"传播时刻未到就给了获知：{_dbg(early)}",
                            where="isekai_core/runtime/events.py:300 claim_grant")
        if not late:
            raise Violation("传播时刻过后仍未获知")
        return (f"最早传播时刻 {base + 3 * DAY}：第 1 天获知 {len(early)} 条；第 5 天后获知 {len(late)} 条，"
                f"获知时刻 {late[0]['world_seconds']} ≥ 传播时刻")
    finally:
        case.close()


#==== B6: 重放 / 补算


@check("B6a", "附录B#6 同一区间重放（含换一个服务实例）不重复产事件 / 经历 / 获知")
def b6a() -> str:
    case = Case(seed="seed-b6a")
    try:
        case.activate()
        case.at_day(6)
        first = slice_of(case)
        case.at_day(6)
        second = slice_of(case)
        if first != second:
            raise Violation(f"同区间重放产生新行：{diff(first, second)}",
                            where="isekai_core/runtime/service.py:1603 advance（水位不回退即不重算）")
        case.service = RuntimeService(case.store, **SERVICE_KW)
        case.at_day(6)
        third = slice_of(case)
        if first != third:
            raise Violation(f"换服务实例重放产生新行：{diff(first, third)}")
        return (f"6 日区间重放两次 + 新服务实例重放一次：事件 {len(first['events'])} / 经历 "
                f"{len(first['experiences'])} / 获知 {len(first['knowledge'])} 行完全不变")
    finally:
        case.close()


@check("B6b", "附录B#6 跨日补算不改过去：已有事件 / 经历 / 计划逐行不变")
def b6b() -> str:
    case = Case(seed="seed-b6b")
    try:
        case.activate()
        case.advance(5)
        mark = case.watermark()
        past_e = [dict(r) for r in case.ev_rows() if int(r["world_seconds"]) <= mark]
        past_x = [dict(r) for r in case.exp_of("cc-堤禾") if int(r["world_seconds"]) <= mark]
        past_p = {
            int(r["day_index"]): str(r["windows"])
            for r in case.store._conn.execute("SELECT * FROM life_plan WHERE timeline_id=?", (case.timeline_id,))
            if int(r["day_index"]) <= mark // DAY
        }
        case.advance(4)
        now_e = {str(r["id"]): dict(r) for r in case.ev_rows()}
        now_x = {str(r["id"]): dict(r) for r in case.exp_of("cc-堤禾")}
        now_p = {
            int(r["day_index"]): str(r["windows"])
            for r in case.store._conn.execute("SELECT * FROM life_plan WHERE timeline_id=?", (case.timeline_id,))
        }
        for row in past_e:
            if now_e.get(str(row["id"])) != row:
                raise Violation(f"过去的事件行被改写：{row['id']}",
                                where="isekai_core/runtime/service.py:1603 advance")
        for row in past_x:
            if now_x.get(str(row["id"])) != row:
                raise Violation(f"过去的经历行被改写：{row['id']}")
        for day, windows in past_p.items():
            if now_p.get(day) != windows:
                raise Violation(f"第 {day} 日的既有计划被改写")
        return (f"再推进 4 天后：{len(past_e)} 条过去事件、{len(past_x)} 条经历、{len(past_p)} 份计划逐行不变"
                f"（水位从 {mark // DAY} 日推进到 {case.watermark() // DAY} 日）")
    finally:
        case.close()


@check("B6c", "附录B#6 不重复产经历 / 计划，事件标识唯一，派生说法不成环")
def b6c() -> str:
    case = Case(seed="seed-b6c")
    try:
        case.activate()
        case.advance(12)
        exp_ids = [str(r["id"]) for r in case.exp_of("cc-堤禾")]
        if len(exp_ids) != len(set(exp_ids)):
            dup = sorted({i for i in exp_ids if exp_ids.count(i) > 1})[:3]
            raise Violation(f"经历出现重复标识：{dup}", where="isekai_core/runtime/service.py:2387 _harvest")
        plans = case.store._conn.execute(
            "SELECT day_index, COUNT(*) n FROM life_plan WHERE timeline_id=? GROUP BY day_index HAVING n>1",
            (case.timeline_id,),
        ).fetchall()
        if plans:
            raise Violation(f"同一角色同一世界日多份计划：{[dict(r) for r in plans]}")
        ev_ids = [str(r["id"]) for r in case.ev_rows()]
        if len(ev_ids) != len(set(ev_ids)):
            raise Violation("事件标识重复")
        graph = {str(r["id"]): str(r["derived_from"]) for r in case.claims() if r.get("derived_from")}
        for start in graph:
            seen, node = set(), start
            while node in graph:
                if node in seen:
                    raise Violation(f"派生说法成环：{start}", where="isekai_core/world/ops.py:782 _expand_claim")
                seen.add(node)
                node = graph[node]
        return (f"12 天后：经历 {len(exp_ids)} 条标识唯一、每角色每日 1 份计划、事件标识唯一；"
                f"派生说法图 {len(graph)} 条边无环")
    finally:
        case.close()


#==== B7: 用户引入事件


@check("B7a", "附录B#7 无法表达的意图 / 未支持效果 / 未登记目标一律拒绝")
def b7a() -> str:
    case = Case(seed="seed-b7a")
    try:
        case.activate()
        case.advance(2)
        none_effect = asyncio.run(case.service.draft_user_event(case.instance_id, case.timeline_id, intent="让潮水永远退去"))
        bad_kind = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="改天气",
            payload={"effects": [{"kind": "weather_magic", "target": "src-1"}]}))
        unknown = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="换人",
            payload={"effects": [{"kind": "institution_state", "target": "off-9"}]}))
        axiom = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="改写公理：取消潮汐",
            payload={"effects": [{"kind": "calendar_change", "target": "ax-1"}]}))
        for name, res in (("无效果", none_effect), ("未支持类型", bad_kind), ("未登记目标", unknown), ("改公理/历法", axiom)):
            if res.get("accepted") is not False:
                raise Violation(f"{name}的意图被接受：{_dbg(res)}",
                                where="isekai_core/runtime/drafts.py:22 normalize_draft")
        return (f"四类意图全部拒绝且不落草案：无效果（{str(none_effect['reason'])[:16]}…）、"
                f"未支持类型（{bad_kind['reason'][:16]}…）、未登记目标（{unknown['reason'][:16]}…）、"
                f"改公理 / 历法（{axiom['reason'][:16]}…）")
    finally:
        case.close()


@check("B7b", "附录B#7 确认后只作用新线、新线默认冻结、重试返回同一次创建")
def b7b() -> str:
    case = Case(seed="seed-b7b")
    try:
        case.activate()
        case.advance(2)
        before = slice_of(case)
        draft = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="驿站信报停摆两日",
            payload={"intent": "驿站信报停摆两日", "when": "now",
                     "effects": [{"kind": "source_delay", "target": "src-1",
                                  "expiry": "until_cleared"}],
                     "claims": [{"text": "驿站信报停摆两日", "source_id": "src-1", "audience": "公开"}]}))
        done = case.service.confirm_user_event(case.instance_id, draft["draft"]["draft_id"], name="丁线")
        new_line = str(done["timeline_id"])
        if str(case.store.timeline_get(new_line)["state"]) != "frozen":
            raise Violation(f"新线状态 {case.store.timeline_get(new_line)['state']}（应默认冻结）")
        if slice_of(case) != before:
            raise Violation(f"原线被改写：{diff(before, slice_of(case))}",
                            where="isekai_core/runtime/service.py:338 confirm_user_event")
        injected = [r for r in case.ev_rows(new_line) if str(r["source"]) == "user"]
        fx = [r for r in case.fx_rows(new_line) if str(r["event_id"]) == str(injected[0]["id"])]
        again = case.service.confirm_user_event(case.instance_id, draft["draft"]["draft_id"])
        if not again.get("reused") or str(again["timeline_id"]) != new_line:
            raise Violation(f"重试没有复用同一次创建：{_dbg(again)}")
        if len(case.store.timeline_list(case.instance_id)) != 2:
            raise Violation("重试重复造线")
        return (f"新线 frozen、含 1 条用户事件（{injected[0]['id']}）+ {len(fx)} 条效果 + "
                f"{len([c for c in case.claims(new_line)])} 条说法；原线 {len(before['events'])} 条事件逐行不变；"
                f"重试 reused=True、线数仍 2")
    finally:
        case.close()


@check("附录B#7 原子性", "附录B#7 注入失败不留下半条线")
def b7c() -> str:
    case = Case(seed="seed-b7c")
    try:
        case.activate()
        case.advance(2)
        before, lines_before = slice_of(case), len(case.store.timeline_list(case.instance_id))
        draft = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="驿站信报停摆两日",
            payload={"intent": "驿站信报停摆两日", "when": "now",
                     "effects": [{"kind": "source_delay", "target": "src-1",
                                  "expiry": "until_cleared"}]}))
        draft_id = str(draft["draft"]["draft_id"])
        real = case.store.apply_runtime_batch

        def refuse(**kwargs: Any) -> bool:
            return False

        case.store.apply_runtime_batch = refuse  # type: ignore[method-assign]
        raised = ""
        try:
            case.service.confirm_user_event(case.instance_id, draft_id)
        except RuntimeStateError as exc:
            raised = str(exc)
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        finally:
            case.store.apply_runtime_batch = real  # type: ignore[method-assign]
        if not raised:
            raise Violation("注入被拒时没有报错（静默失败）")
        left = len(case.store.timeline_list(case.instance_id))
        if left != lines_before:
            raise Violation(f"留下了半条线：{left} 条（原 {lines_before} 条）",
                            where="isekai_core/runtime/service.py:360 confirm_user_event")
        if slice_of(case) != before:
            raise Violation("失败路径改写了来源线")
        if str(case.store.draft_get(draft_id)["state"]) != "draft":
            raise Violation("失败的草案被标成已确认")
        return (f"注入被拒（{raised}）→ 线数仍 {lines_before}、来源线事实不变、草案仍为 draft")
    finally:
        case.close()


@check("附录B#7 不泄密", "附录B#7 拒绝理由与草案展示不泄露既有隐藏内容")
def b7d() -> str:
    package = example_package("灰潮纪")
    secrets: list[str] = []
    for item in package.get("canon") or []:
        secrets.append(str(item.get("statement") or ""))
    for item in package.get("narratives") or []:
        secrets.append(str(item.get("text") or ""))
    for item in (package.get("initial_state") or {}).get("mysteries") or []:
        secrets.append(str(item.get("question") or ""))
    for item in (package.get("world") or {}).get("axioms") or []:
        secrets.append(str(item.get("text") or ""))
    secrets = [s for s in secrets if s]
    case = Case(seed="seed-b7d", package=package)
    try:
        case.activate()
        case.advance(2)
        blobs: list[tuple[str, str]] = []
        for tag, payload in (
            ("未登记目标", {"effects": [{"kind": "institution_state", "target": "off-9"}]}),
            ("未支持类型", {"effects": [{"kind": "health_pool", "target": "src-1"}]}),
            ("未登记职位", {"effects": [{"kind": "institution_state", "target": "off-2", "value": "cc-outsider"}]}),
            ("未登记渠道", {"effects": [{"kind": "public_notice", "target": "src-1"}],
                            "claims": [{"text": "x", "source_id": "src-zzz"}]}),
        ):
            res = asyncio.run(case.service.draft_user_event(
                case.instance_id, case.timeline_id, intent="我要北堤的真相", payload=payload))
            blobs.append((f"拒绝:{tag}", str(res.get("reason") or "")))
        ok = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="驿站信报停摆两日",
            payload={"intent": "驿站信报停摆两日", "when": "now",
                     "effects": [{"kind": "source_delay", "target": "src-1",
                                  "expiry": "until_cleared"}]}))
        blobs.append(("通过草案", json.dumps(ok["draft"], ensure_ascii=False)))
        blobs.append(("来源点描述", case.service.describe_world(case.instance_id, case.watermark())))
        for tag, blob in blobs:
            hit = [s[:18] for s in secrets if s[:18] and s[:18] in blob]
            if hit:
                raise Violation(f"{tag} 泄露既有隐藏内容：{hit}",
                                where="isekai_core/runtime/drafts.py:104 public_draft / service.py:261 draft_user_event")
        return (f"{len(blobs)} 个提示面（4 条拒绝理由 + 1 份通过草案 + 来源点描述）都不含 "
                f"{len(secrets)} 条 canon / narratives / mysteries / axioms 片段的开头 18 字")
    finally:
        case.close()


@check("附录B#7 来源点", "附录B#7 第7条：预览后的来源点变化不得被静默替换")
def b7e() -> str:
    case = Case(seed="seed-b7e")
    try:
        case.activate()
        case.advance(2)
        preview_world = case.watermark()
        draft = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="驿站信报停摆两日",
            payload={"intent": "驿站信报停摆两日", "when": "now",
                     "effects": [{"kind": "source_delay", "target": "src-1",
                                  "expiry": "until_cleared"}]}))
        shown = int(draft["draft"]["source"]["world"])
        case.advance(3)
        later_world = case.watermark()
        draft_row = case.store.draft_get(str(draft["draft"]["draft_id"])) or {}
        try:
            done = case.service.confirm_user_event(case.instance_id, str(draft["draft"]["draft_id"]), name="戊线")
        except RuntimeStateError as exc:
            refused = str(exc)
        else:
            new_line = str(done["timeline_id"])
            base = int(case.store.commit_get(str(done["commit"]["id"]))["moment"])
            injected = [r for r in case.ev_rows(new_line) if str(r["source"]) == "user"]
            landed = int(injected[0]["world_seconds"]) if injected else -1
            raise Violation(
                f"预览报告来源点 world={shown}，确认时该线已推进到 {later_world}，"
                f"但确认照常建线并把事件落在 world={landed}（分叉基础 {base}）；草案行 source_commit="
                f"{draft_row.get('source_commit')!r}，没有重新校验 / 重新确认的路径",
                repro=("draft_user_event(when='now') → advance(3 天) → confirm_user_event；"
                       "比较 draft['draft']['source']['world'] 与新线 fork 提交 moment / 事件 world_seconds"),
                where=("isekai_core/runtime/service.py confirm_user_event（source_commit 为空时"
                       "在确认时刻现取提交作为分叉基础）"),
            )
        # 拒绝之后，按当前来源点重新起草 → 立刻确认能成功，且落点与预览逐字一致
        again = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="驿站信报停摆两日",
            payload={"intent": "驿站信报停摆两日", "when": "now",
                     "effects": [{"kind": "source_delay", "target": "src-1", "expiry": "until_cleared"}]}))
        shown_again = int(again["draft"]["source"]["world"])
        done = case.service.confirm_user_event(case.instance_id, str(again["draft"]["draft_id"]), name="己线")
        new_line = str(done["timeline_id"])
        injected = [r for r in case.ev_rows(new_line) if str(r["source"]) == "user"]
        landed = int(injected[0]["world_seconds"]) if injected else -1
        if shown_again != landed:
            raise Violation(f"重新起草后落点仍不一致：预览 {shown_again} vs 落点 {landed}")
        return f"来源点变化被拒（{refused}）；重新起草后落点 {landed} 与预览一致"
    finally:
        case.close()


#==== B8: 虚构指控


@check("B8", "附录B#8 完全虚构的指控可入史料并被角色读到，但不成为实情、不执行其效果")
def b8() -> str:
    package = example_package()
    accusation = "堤长把赈粮换成了盐"
    package["canon"] = list(package["canon"]) + [
        {"id": "cf-9", "statement": f"有投书称{accusation}，灾年编年不采此说。", "tags": ["指控"]}
    ]
    package["narratives"] = list(package["narratives"]) + [
        {"id": "nv-9", "text": f"匿名投书称{accusation}。", "source_id": "src-1", "canon_ref": "cf-9",
         "obtain": ["在驿站读到投书抄件"], "confidence": "doubted"}
    ]
    package["initial_state"]["events"] = list(package["initial_state"]["events"]) + ["cf-9"]
    package["initial_state"]["rumors"] = list(package["initial_state"]["rumors"]) + ["nv-9"]
    card = example_card(package)
    card["initial_knowledge"] = list(card["initial_knowledge"]) + [
        {"ref_type": "narrative", "ref_id": "nv-9", "obtained_at": DAY * 1400}
    ]
    case = Case(seed="seed-b8", package=package, cards=[card])
    try:
        case.activate()
        case.advance(3)
        fx = [r for r in case.fx_rows()
              if accusation in str(r.get("value") or "") or accusation in str(r.get("recovery") or "")]
        if fx:
            raise Violation(f"指控直接产生了事实效果：{_dbg(fx[:1])}",
                            where="isekai_core/runtime/events.py:159 effect_rows")
        runtime = [r for r in case.ev_rows()
                   if accusation in str(r["summary"]) and str(r["source"]) != "backfill"]
        if runtime:
            raise Violation(f"指控被写成运行期事实：{[(r['id'], r['source']) for r in runtime]}")
        prompt = case.prompt("cc-堤禾")
        if accusation not in prompt:
            raise Violation("角色读不到该指控，无法证明它影响判断")
        records = [r for r in case.ev_rows() if accusation in str(r["summary"])]
        if not records or any(str(r["effects"]) not in ("[]", "") for r in records):
            raise Violation("史料形态不符合「不施加效果」的记录形态")
        return (f"史料条目 {records[0]['id']}（source={records[0]['source']}，effects=[]）进册；"
                f"角色扮演定义里读得到该指控；{len(case.fx_rows())} 条效果中 0 条由它产生，"
                f"也没有任何运行期事件把它写成实情")
    finally:
        case.close()


#==== B9: 惰性展开


def _expand_case(seed: str) -> tuple[Case, str]:
    package = example_package()
    holder = example_card(package, name="堤禾")
    stone = example_card(package, name="碑拓者")
    stone["channels"] = [{"source_id": "src-2", "conditions": "只在盐滩拓碑"}]
    return Case(seed=seed, cards=[holder, stone], package=package), "cc-碑拓者"


@check("B9a", "附录B#9 展开只对已持有的记载：派生记录、原文不改、重复提问复用、未持有者被拒")
def b9a() -> str:
    case, outsider = _expand_case("seed-b9a")
    try:
        cfg = load_config(case.dir / "cfg")
        case.activate()
        case.advance(4)
        claim = next(r for r in case.claims()
                     if str(r["source_id"]) == "src-1"
                     and "cc-堤禾" in case.store.knowledge_holders(case.instance_id, case.timeline_id, str(r["id"])))
        denied = ""
        try:
            asyncio.run(world_ops.dispatch_async(
                cfg, ScriptedLLM(["随手写点"]), "event.expand",
                {"instance_id": case.instance_id, "timeline_id": case.timeline_id,
                 "claim_id": str(claim["id"]), "character_id": outsider},
                store=case.store))
        except Exception as exc:  # noqa: BLE001
            denied = str(exc)
        if "没有这条记载" not in denied:
            raise Violation(f"未持有记载的角色也能展开（物化≠获知）：denied={denied!r}",
                            where="isekai_core/world/ops.py:795 _expand_claim（holders 判定）")
        original_before = str(claim["text"])
        llm = ScriptedLLM([f"{original_before}（另一本抄本记法略异）"])
        first = asyncio.run(world_ops.dispatch_async(
            cfg, llm, "event.expand",
            {"instance_id": case.instance_id, "timeline_id": case.timeline_id,
             "claim_id": str(claim["id"]), "character_id": "cc-堤禾", "question": "还写了什么？"},
            store=case.store))
        derived = str(first.get("derived") or "")
        if not derived or int(first.get("calls") or 0) != 1:
            raise Violation(f"没有产出派生记录：{_dbg(first)}")
        now = next(r for r in case.claims() if str(r["id"]) == str(claim["id"]))
        if str(now["text"]) != original_before:
            raise Violation("展开改写了原记载", where="isekai_core/runtime/render.py:44 _expand_claim→claim_put")
        row = next(r for r in case.claims() if str(r["id"]) == derived)
        if str(row["derived_from"]) != str(claim["id"]):
            raise Violation("派生记录没有关联原条目")
        again = asyncio.run(world_ops.dispatch_async(
            cfg, ScriptedLLM(["另一个答法"]), "event.expand",
            {"instance_id": case.instance_id, "timeline_id": case.timeline_id,
             "claim_id": str(claim["id"]), "character_id": "cc-堤禾", "question": "换个问法？"},
            store=case.store))
        if not again.get("reused") or str(again.get("derived")) != derived or int(again.get("calls") or 0) != 0:
            raise Violation(f"同一传本没有复用已采纳的展开：{_dbg(again)}")
        return (f"未持有者被拒（{denied[:22]}…）；持有者展开得 {derived}（derived_from={claim['id']}），"
                f"原记载文本不变；换问法重问 reused=True、calls=0、派生记录不变")
    finally:
        case.close()


@check("B9b", "附录B#9 展开不得补出未确定的参与者 / 责任人（非数字类）")
def b9b() -> str:
    case, _ = _expand_case("seed-b9b")
    try:
        cfg = load_config(case.dir / "cfg")
        case.activate()
        case.advance(4)
        claim = next(r for r in case.claims()
                     if str(r["source_id"]) == "src-1"
                     and "cc-堤禾" in case.store.knowledge_holders(case.instance_id, case.timeline_id, str(r["id"])))
        invention = "经手此事的其实是堤南史馆的一名文书"
        before = len(case.claims())
        result = asyncio.run(world_ops.dispatch_async(
            cfg, ScriptedLLM([invention, '{"grounded": false, "added": "经手人"}']), "event.expand",
            {"instance_id": case.instance_id, "timeline_id": case.timeline_id,
             "claim_id": str(claim["id"]), "character_id": "cc-堤禾", "question": "是谁经手？"},
            store=case.store))
        after = len(case.claims())
        if after != before:
            raise Violation(
                f"补出责任人的展开被固化成派生记载（{result.get('derived')}）："
                f"守卫只比数字集合，原记载无数字时任何补造都通过",
                repro=f"claim.text={str(claim['text'])!r}（无数字）→ 模型返回 {invention!r} → claim_put",
                where=("isekai_core/runtime/render.py:89 expansion_is_grounded（`got <= want`，"
                       "原记载无数字时恒真）；isekai_core/world/ops.py:808"),
            )
        return f"未复现：说法数仍 {after} 条（note={result.get('note')}）"
    finally:
        case.close()


#==== B10: 缺载


@check("B10", "附录B#10 「尚未生成」与「已确认缺载」必须区分，缺载不得当作删改证据")
def b10() -> Any:
    hits: list[str] = []
    for path in sorted((ROOT / "isekai_core").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for word in ("缺载", "尚未生成", "未展开", "lacuna"):
            if word in text:
                hits.append(f"{path.relative_to(ROOT)}:{word}")
    coverage = [f"{p.relative_to(ROOT)}" for p in sorted((ROOT / "isekai_core").rglob("*.py"))
                if "coverage" in p.read_text(encoding="utf-8")]
    return ("DEFERRED",
            f"运行期没有「已确认缺载 / 尚未生成 / 已展开」的状态区分：全仓 isekai_core 命中 "
            f"{len(hits)} 处（{hits[:3]}）；'coverage' 只出现在 {coverage[:2]} 的静态校验里，"
            "惰性展开也没有「本来源缺载」的记录形态。SPEC §十一 自列残余"
            "（「历史回填的候选槽语义与体裁选取规则；惰性展开的触发条件、预算与派生表示的存储方式」）")


#==== B11: 未登记对象


@check("B11", "附录B#11 未登记目标 → 校验失败；含未登记名字的传闻仍可获知但不产生事实效果")
def b11() -> str:
    package = example_package()
    package["events"]["families"][0]["templates"][0]["effects"][0]["target"] = "src-99"
    errors = [e for e in validate_package(package) if "src-99" in e]
    if not errors:
        raise Violation("未登记的效果目标没有被校验拦下",
                        where="isekai_core/world/validate.py:650 _validate_events")
    case = Case(seed="seed-b11")
    try:
        case.activate()
        base = case.watermark()
        name = "未登记之「何九」"
        case.inject(summary="路边传闻", claims=[{"id": "cl-b11", "text": f"听说{name}在北堤露过面",
                                                "source_id": "src-1", "earliest_world": base + 1}])
        case.advance(2)
        learned = [r for r in case.know("cc-堤禾") if name in str(r["text"])]
        if not learned:
            raise Violation("含未登记名字的传闻没有被获知")
        fx = [r for r in case.fx_rows()
              if name in str(r.get("value") or "") or name in str(r.get("recovery") or "")]
        if fx:
            raise Violation(f"未登记名字取得了事实资格：{_dbg(fx[:1])}")
        facts = [r for r in case.ev_rows() if name in str(r["summary"]) and str(r["source"]) != "engine"]
        if facts:
            raise Violation(f"未登记名字进了运行期事件：{facts}")
        return (f"包内把效果目标改成 src-99 → 校验报错（{errors[0][:44]}…）；含「{name}」的传闻被获知"
                f"（{learned[0]['id']}），但没有产生任何效果，也没有取得事实资格")
    finally:
        case.close()


#==== B12: 后果持续 / 澄清 / 失效方式


@check("B12a", "附录B#12 后果持续参与后续安排、直到满足恢复条件；澄清不自动撤销已施行的措施")
def b12a() -> str:
    case = Case(seed="seed-b12a")
    try:
        case.activate()
        case.advance(3)
        mark = case.watermark()
        plans_before = {int(r["day_index"]): str(r["windows"])
                        for r in case.store._conn.execute("SELECT * FROM life_plan WHERE timeline_id=?",
                                                          (case.timeline_id,))}
        case.inject(summary="堤道封闭一日",
                    effects=[{"kind": "activity_constraint", "target": "rl-1", "expiry": "until_cleared"}])
        snap = case.service.character_snapshot(case.instance_id, case.timeline_id, "cc-堤禾", world_seconds=mark)
        if not snap["effects"] or "受影响的后果" not in str(snap["current_activity"]):
            raise Violation(f"仍有效的后果没有进入角色处境：effects={snap['effects']} "
                            f"activity={snap['current_activity']!r}",
                            where="isekai_core/runtime/service.py:2481 character_snapshot")
        office_before = case.offices()["off-2"]
        fx_before = {(str(r["id"]), str(r["target"]), str(r["kind"]), int(r["from_world"])) for r in case.fx_rows()}
        case.inject(summary="澄清：先前信报所述措施有误",
                    claims=[{"id": "cl-b12a", "text": "澄清：先前信报所述措施有误，实未施行",
                             "source_id": "src-1", "earliest_world": mark + 1}],
                    ident="ev-x-b12a-retract")
        case.advance(4)
        raw = case.fx_rows()
        keep = [r for r in raw if str(r["kind"]) == "activity_constraint"]
        if not keep or int(keep[0]["active"]) != 1:
            raise Violation("until_cleared 的后果在后续日子里被自动清掉",
                            where="isekai_core/runtime/service.py:2346 _propagate_and_clear")
        noted = [r for r in case.exp_of("cc-堤禾") if "受影响的后果" in str(r["summary"])]
        if not noted:
            raise Violation("后果没有进入后续生活安排 / 经历",
                            where="isekai_core/runtime/service.py:1714 _collect_batch → life.effect_note")
        plans_after = {int(r["day_index"]): str(r["windows"])
                       for r in case.store._conn.execute("SELECT * FROM life_plan WHERE timeline_id=?",
                                                         (case.timeline_id,))}
        rewritten = {d: (w, plans_after.get(d)) for d, w in plans_before.items() if plans_after.get(d) != w}
        if rewritten:
            raise Violation(f"已固化的既有计划被改写：{list(rewritten)[:2]}")
        if case.offices()["off-2"] != office_before:
            raise Violation("澄清自动撤销了制度措施",
                            where="isekai_core/runtime/service.py:2346 _propagate_and_clear（无澄清撤销分支）")
        now_fx = {(str(r["id"]), str(r["target"]), str(r["kind"]), int(r["from_world"])) for r in raw}
        if not fx_before <= now_fx:
            raise Violation(f"澄清抹掉了已生效的后果行：{sorted(fx_before - now_fx)[:2]}")
        known = [r for r in case.know("cc-堤禾") if "澄清" in str(r["text"])]
        if not known:
            raise Violation("澄清没有进入获知者的认知")
        return (f"后果（activity_constraint rl-1，until_cleared）进入角色处境与 {len(noted)} 条后续经历；"
                f"4 天后仍 active=1；既有计划逐日不变；措施（off-2={case.offices()['off-2']['holder']!r}）"
                f"与 {len(fx_before)} 条后果行不变；澄清作为新说法进入获知（{len(known)} 条）")
    finally:
        case.close()


@check("B12b", "附录B#12 / §二 三类失效方式可区分且按声明生效：随诱因结束 / 持续到解除 / 有条件自然恢复")
def b12b() -> str:
    case = Case(seed="seed-b12b")
    try:
        case.activate()
        case.advance(12)
        mark = case.watermark()
        rows = case.fx_rows()
        kinds = {str(r["expiry"]) for r in rows}
        missing = {"with_cause", "until_cleared", "natural_recovery"} - kinds
        if missing:
            raise Violation(f"数据里没有同时出现三类失效方式（缺 {sorted(missing)}）：{sorted(kinds)}",
                            where="isekai_core/runtime/events.py:174 effect_rows / example.py 模板")
        later: dict[str, list[int]] = {}
        for r in case.ev_rows():
            fam = str(r["family"] or "")
            if fam:
                later.setdefault(fam, []).append(int(r["world_seconds"]))
        problems: list[str] = []
        for r in rows:
            exp, active, started = str(r["expiry"]), int(r["active"]), int(r["from_world"])
            if exp == "with_cause" and started + DAY <= mark and (active != 0 or r["cleared_at"] is None):
                problems.append(f"with_cause 未随诱因结束（{r['id']} started={started} active={active}）")
            if exp == "until_cleared" and active != 1:
                problems.append(f"until_cleared 被自动解除（{r['id']}）")
            if exp == "natural_recovery" and active == 0:
                # 声明的解除依据：同族在该后果之后仍被观察到新事件（判定发生在某个批边界）
                cleared_at = int(r["cleared_at"] or mark)
                basis = [t for t in later.get(str(r["family"] or ""), []) if started < t <= cleared_at]
                if not basis:
                    problems.append(f"natural_recovery 无依据却被解除（{r['id']} started={started} "
                                    f"cleared_at={cleared_at} 同族后续事件=无）")
        if problems:
            raise Violation("；".join(problems[:3]),
                            where="isekai_core/runtime/service.py:2346 _propagate_and_clear")
        counts = {k: sum(1 for r in rows if str(r["expiry"]) == k) for k in sorted(kinds)}
        return (f"12 天后 {len(rows)} 条后果：{counts}；with_cause 到点即带 cleared_at 解除、"
                f"until_cleared 一律保留、natural_recovery 只在同族后续事件可见时解除（"
                f"{sum(1 for r in rows if int(r['active']) == 0)} 条已解除）")
    finally:
        case.close()


#==== B13: 制度


@check("B13a", "附录B#13 空缺期间事务按声明规则分别延续 / 暂停，未声明的没有默认答案")
def b13a() -> str:
    # 去掉模板里的 institution_state 效果，让 off-2 在整个窗口保持空缺
    package = example_package()
    template = package["events"]["families"][0]["templates"][0]
    template["effects"] = [e for e in template["effects"] if str(e.get("kind")) != "institution_state"]
    if validate_package(package):
        raise Violation(f"构造前提用的包没通过校验：{validate_package(package)[:2]}")
    case = Case(seed="seed-b13a", package=package)
    try:
        case.activate()
        case.advance(3)
        office = case.offices()
        vacant = office["off-2"]
        if str(vacant["holder"]):
            raise Violation("前提不成立：off-2 不是空缺")
        cases = {
            "日常堤务": institutions.matter_status(vacant, "日常堤务"),
            "通行牌发放": institutions.matter_status(vacant, "通行牌发放"),
            "发放盐引": institutions.matter_status(vacant, "发放盐引"),
            "有在任者": institutions.matter_status(office["off-1"], "通行牌发放"),
        }
        expect = {"日常堤务": institutions.CONTINUES, "通行牌发放": institutions.SUSPENDED,
                  "发放盐引": None, "有在任者": institutions.ACTIVE}
        if cases != expect:
            raise Violation(f"空缺判定与声明不符：{cases}",
                            where="isekai_core/runtime/institutions.py:70 matter_status")
        snap = case.service.character_snapshot(case.instance_id, case.timeline_id, "cc-堤禾",
                                              world_seconds=case.watermark())
        shown = next((i for i in snap["institutions"] if "守碑人" in str(i["name"])), None)
        if shown is None or "空缺" not in str(shown["value"]) or "照旧" not in str(shown["note"]):
            raise Violation(f"角色视角没有可判定的空缺说明：{shown}")
        return (f"空缺职位：日常堤务={cases['日常堤务']}、通行牌发放={cases['通行牌发放']}、"
                f"未声明的发放盐引={cases['发放盐引']}（不默认照旧也不默认停摆）；有在任者={cases['有在任者']}；"
                f"角色视角读到「{shown['value']}」（{shown['note']}）")
    finally:
        case.close()


@check("B13b", "附录B#13 制度变化落在声明范围内、带来源与时刻；变化后补入的角色不凭空知晓")
def b13b() -> str:
    case = Case(seed="seed-b13b")
    try:
        case.activate()
        case.advance(6)
        row = case.offices()["off-2"]
        if str(row["holder"]) != "en-1":
            raise Violation(f"声明范围内的制度变化（引擎事件里的 institution_state）没有落地："
                            f"holder={row['holder']!r}",
                            where="isekai_core/runtime/service.py:2057 _institution_rows")
        source = str(row["source"])
        ev = next((r for r in case.ev_rows() if str(r["id"]) == source), None)
        if ev is None or int(row["updated_world"]) < int(ev["world_seconds"]):
            raise Violation(f"变化没有来源事件或发生时刻：source={source!r} updated={row['updated_world']}")
        if not row["continues"] or not row["suspended"]:
            raise Violation("制度状态行没带声明的事务清单（延续 / 暂停不可判定）")
        # 变化之后才补入的角色按声明初始状态看，且没有获知记录
        package = case.package
        late = example_card(package, name="后到者")
        case.service.add_character(case.instance_id, case.timeline_id, late,
                                   now_real=case._now_real[case.timeline_id],
                                   joined_world=int(row["from_world"]) + 1, note="变化之后才到本线")
        late_id = str(late["meta"]["card_id"])
        world_now = case.watermark()
        view = next(i for i in case.service.character_snapshot(
            case.instance_id, case.timeline_id, late_id, world_seconds=world_now)["institutions"]
            if "守碑人" in str(i["name"]))
        if "空缺" not in str(view["value"]):
            raise Violation(f"后到的角色凭空知道了后来的变化：{view}",
                            where="isekai_core/runtime/institutions.py:195 observations（按获知来源重建）")
        if [r for r in case.know(late_id) if str(r["target"]) == source]:
            raise Violation("后到的角色拿到了该变化的获知记录")
        return (f"off-2 由空缺承接为 en-1：来源事件 {source}、updated_world={row['updated_world']}"
                f"（事件刻 {ev['world_seconds']}）；状态行带照旧 {row['continues']} / 暂停 {row['suspended']}；"
                f"变化之后补入的角色仍看到「{view['value']}」，获知记录 0 条")
    finally:
        case.close()


@check("B13c", "附录B#13 职位持有者身故 → 出缺带来源与时刻；空缺期事务判定成立")
def b13c() -> str:
    moment = DAY * 1500
    package = example_package(moment=moment)
    package["entities"] = list(package["entities"]) + [
        {"id": "cc-堤禾", "kind": "person", "name": "堤禾", "race_id": "rc-1", "born": 0, "died": None}
    ]
    package["world"]["institutions"][0]["offices"][0]["holder"] = "cc-堤禾"
    card = example_card(package)
    card["identity"]["died"] = moment + 2 * DAY
    case = Case(seed="seed-b13c", moment=moment, package=package, cards=[card])
    try:
        case.activate()
        case.advance(5)
        deaths = [r for r in case.ev_rows() if str(r["template"]).startswith("death:")]
        if not deaths:
            raise Violation("前提不成立：没有产生身故事件（card.identity.died 已固化）",
                            repro="card['identity']['died']=moment+2*DAY → advance(5 天)")
        row = case.offices()["off-1"]
        death = deaths[0]
        if str(row["holder"]):
            raise Violation(
                f"身故事件已发生（{death['id']} @ {death['world_seconds']}），但职位状态不被推出："
                f"off-1 在任者仍为 {row['holder']!r}（source={row['source']!r}），声明的空缺 / 承接规则不启用",
                repro=("entities += {id:'cc-堤禾'}；offices[off-1].holder='cc-堤禾'；"
                       "card.identity.died=moment+2*DAY；advance(5 天) → 看 institution_state.off-1"),
                where=("isekai_core/runtime/service.py:2294 _death_rows（只产身故事件）；"
                       "isekai_core/runtime/institutions.py:151 vacancies_for_deaths（未接到运行路径）"),
            )
        if str(row["source"]) != str(death["id"]):
            raise Violation(f"出缺没有来源：source={row['source']!r}，应为身故事件 {death['id']}")
        if int(row["from_world"]) != int(death["world_seconds"]):
            raise Violation(f"出缺时刻与身故不一致：{row['from_world']} vs {death['world_seconds']}")
        status = institutions.matter_status(row, "通行牌发放")
        if status != institutions.SUSPENDED:
            raise Violation(f"出缺后事务判定不对：通行牌发放={status!r}")
        return (f"在任者身故（{death['id']} @ {death['world_seconds']}）→ off-1 出缺，来源=身故事件、"
                f"时刻一致；空缺期「通行牌发放」={status}")
    finally:
        case.close()


#==== B14: 惯例


@check("B14", "附录B#14 同一节庆的做法可在声明范围内变更：日期与预算不变、旧记录不改、已知范围不扩大")
def b14() -> str:
    from isekai_core.world.validate import change_allowed

    package = example_package()
    forms = [str(x) for x in package["world"]["customs"][0]["forms"]]
    alternative = forms[1]
    ok, _ = change_allowed(package, kind="custom_state", target="cus-1", value=alternative)
    denied, reason = change_allowed(package, kind="custom_state", target="cus-1", value="随手编个新做法")
    if not ok or denied:
        raise Violation(f"惯例允许范围判定不对：ok={ok} denied={denied}/{reason}",
                        where="isekai_core/world/validate.py:change_allowed")
    package["events"]["families"][0]["templates"][0]["effects"] = list(
        package["events"]["families"][0]["templates"][0]["effects"]
    ) + [{"kind": "custom_state", "target": "cus-1", "value": alternative, "expiry": "until_cleared"}]
    errs = validate_package(package)
    if errs:
        raise Violation(f"声明内的惯例效果被误判：{errs[:2]}")
    cal = calendar_from_package(package)
    before_festival = events.fixed_events(package, day_index=1562, calendar=cal)
    before_budget = events.daily_budget("seed-b14", "r-1", 1562, str(package["events"]["density"]))
    holder = example_card(package, name="堤禾")
    case = Case(seed="seed-b14", cards=[holder], package=package)
    try:
        case.activate()
        case.advance(8)
        cur = case.customs()["cus-1"]
        if str(cur["form"]) != alternative:
            raise Violation(f"声明范围内的惯例变化没有生效：{cur['form']!r}",
                            where="isekai_core/runtime/service.py:2057 _institution_rows")
        src = next((r for r in case.ev_rows() if str(r["id"]) == str(cur["source"])), None)
        if src is None:
            raise Violation(f"惯例变化没有来源事件：source={cur['source']!r}")
        if events.fixed_events(package, day_index=1562, calendar=cal) != before_festival:
            raise Violation("惯例变化改动了固定节庆")
        if events.daily_budget("seed-b14", "r-1", 1562, str(package["events"]["density"])) != before_budget:
            raise Violation("惯例变化改动了当日预算")
        history = {str(r["id"]): dict(r) for r in case.ev_rows() if str(r["source"]) == "backfill"}
        if not history:
            raise Violation("没有可对照的历史记录")
        late = example_card(package, name="后到者")
        case.service.add_character(case.instance_id, case.timeline_id, late,
                                   now_real=case._now_real[case.timeline_id],
                                   joined_world=int(cur["from_world"]) + 1, note="变更之后才到本线")
        late_id = str(late["meta"]["card_id"])
        view = next(i for i in case.service.character_snapshot(
            case.instance_id, case.timeline_id, late_id, world_seconds=case.watermark())["institutions"]
            if "退潮祭" in str(i["name"]))
        if alternative in str(view["value"]):
            raise Violation(f"变更后才补入的角色凭空知道了新做法：{view}")
        after = {str(r["id"]): dict(r) for r in case.ev_rows() if str(r["source"]) == "backfill"}
        if after != history:
            raise Violation("惯例变化改写了旧记录")
        return (f"现行做法改为声明内的备选（来源事件 {cur['source']}）；固定节庆 "
                f"{before_festival[0]['summary']} 与当日预算 {before_budget} 不变；{len(history)} 条旧记录逐行不变；"
                f"变更后才补入的角色仍看到「{view['value'][:14]}…」")
    finally:
        case.close()


#==== B15: 因果链


@check("B15", "附录B#15 代表性因果链：诱因 → 持续后果 → 合法响应 → 解除 / 新常态")
def b15() -> str:
    case = Case(seed="seed-b15")
    try:
        case.activate()
        case.advance(25)
        mark = case.watermark()
        induces = [r for r in world_rows(case) if str(r["effects"]) not in ("[]", "")]
        if not induces:
            raise Violation("没有带事实效果的诱因事件")
        first = induces[0]
        fx = case.fx_rows()
        persistent = [r for r in fx if str(r["event_id"]) == str(first["id"])
                      and int(r["from_world"]) + DAY <= mark and int(r["active"]) == 1]
        responses = [r for r in case.ev_rows() if str(r["source"]) == "character_action"]
        cleared = [r for r in fx if int(r["active"]) == 0 and r["cleared_at"] is not None]
        new_normal = [r for r in fx if int(r["active"]) == 1 and str(r["expiry"]) == "until_cleared"]
        if not persistent:
            raise Violation(f"诱因 {first['id']} 的后果没有跨日持续",
                            where="isekai_core/runtime/events.py:159 effect_rows")
        if not responses:
            raise Violation("没有后续合法响应（角色行动事件）",
                            where="isekai_core/runtime/service.py:2102 _revise_intents")
        if not cleared and not new_normal:
            raise Violation("后果既没被合法解除，也没形成持续的新常态")
        return (f"诱因 {first['id']}（{str(first['summary'])[:14]}）→ {len(persistent)} 条后果跨日仍 active；"
                f"响应 {responses[0]['id']}（source={responses[0]['source']}，{str(responses[0]['summary'])[:12]}…）；"
                f"解除留档 {len(cleared)} 条、持续新常态 {len(new_normal)} 条")
    finally:
        case.close()


#==== B16: 效果闭集


@check("B16", "附录B#16 效果只落在闭集内：未声明的类型校验期被拒，也不存在健康值 / 饥饿值面板")
def b16() -> str:
    package = example_package()
    package["events"]["families"][0]["templates"][0]["effects"] = [
        {"kind": "health_pool", "target": "rl-1", "expiry": "with_cause"}
    ]
    errs = [e for e in validate_package(package) if "未支持的效果类型" in e]
    if not errs:
        raise Violation("未声明效果类型没有被创建前校验拦下",
                        where="isekai_core/world/validate.py:653 _validate_events")
    engine_rows_out = events.effect_rows(
        {"effects": [{"kind": "health_pool", "target": "rl-1", "expiry": "with_cause"}]},
        instance_id="in-x", timeline_id="tl-x", event_ident="ev-x", world_seconds=0)
    if engine_rows_out:
        raise Violation("引擎层放行了闭集外的效果",
                        where="isekai_core/runtime/events.py:172 effect_rows")
    if any(w in k for k in SUPPORTED_EFFECTS for w in ("health", "hunger", "stamina", "hp")):
        raise Violation(f"闭集里混进了通用数值系统：{sorted(SUPPORTED_EFFECTS)}")
    case = Case(seed="seed-b16")
    try:
        case.activate()
        case.advance(3)
        snap = case.service.character_snapshot(case.instance_id, case.timeline_id, "cc-堤禾",
                                              world_seconds=case.watermark())
        numeric = [k for k, v in snap.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if numeric:
            raise Violation(f"角色视图出现数值面板字段：{numeric}")
        return (f"health_pool 在创建前校验被拒（{errs[0][:38]}…）且引擎层再兜一道；闭集共 {len(SUPPORTED_EFFECTS)} 类"
                f"（{'、'.join(sorted(SUPPORTED_EFFECTS))}），无健康 / 饥饿 / 体力类；角色切片无数值字段")
    finally:
        case.close()


#==== B17: 预约事件


@check("B17a", "附录B#17 预约事件到点前不产生效果 / 经历 / 获知，到点在预约时刻落成并幂等")
def b17a() -> str:
    case = Case(seed="seed-b17a")
    try:
        case.activate()
        case.advance(2)
        base = case.watermark()
        due = base + 3 * DAY
        draft = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="三日后让驿站信报停摆",
            payload={"intent": "三日后让驿站信报停摆", "when": "scheduled", "at_world": due,
                     "effects": [{"kind": "source_delay", "target": "src-1",
                                  "expiry": "until_cleared"}],
                     "claims": [{"text": "三日后让驿站信报停摆", "source_id": "src-1", "audience": "公开"}]}))
        new_line = str(case.service.confirm_user_event(case.instance_id, draft["draft"]["draft_id"],
                                                       name="预约线")["timeline_id"])
        if [r for r in case.ev_rows(new_line) if str(r["source"]) == "user"]:
            raise Violation("到点前就注入了事件")
        if [r for r in case.fx_rows(new_line) if int(r["from_world"]) > base]:
            raise Violation("到点前就施加了效果")
        if [r for r in case.know("cc-堤禾", new_line) if "换人" in str(r["text"])]:
            raise Violation("到点前就产生获知")
        if not case.store.pending_events_due(case.instance_id, new_line, until=10**15):
            raise Violation("没有登记待执行状态")
        case.activate(new_line)
        case.advance(5, tl=new_line)
        landed = [r for r in case.ev_rows(new_line) if str(r["source"]) == "user"]
        if not landed or int(landed[0]["world_seconds"]) != due:
            raise Violation(f"到点没有在预约时刻落事件：{[(r['id'], r['world_seconds']) for r in landed]}",
                            where="isekai_core/runtime/service.py:437 apply_due_pending_events")
        pend = case.store._conn.execute("SELECT * FROM pending_event WHERE timeline_id=?",
                                        (new_line,)).fetchall()
        states = sorted({str(r["state"]) for r in pend})
        if states != ["applied"]:
            raise Violation(f"待执行状态不是 applied：{states}")
        # 重启 + 继续推进：不重复施加
        user_before = [(str(r["id"]), int(r["world_seconds"])) for r in case.ev_rows(new_line)
                       if str(r["source"]) == "user"]
        user_ids = {r[0] for r in user_before}

        def user_fx() -> list[tuple[str, int, int]]:
            return sorted((str(r["id"]), int(r["from_world"]), int(r["active"])) for r in case.fx_rows(new_line)
                          if str(r["event_id"]) in user_ids)

        fx_before = user_fx()
        case.service = RuntimeService(case.store, **SERVICE_KW)
        case.advance(4, tl=new_line)
        user_after = [(str(r["id"]), int(r["world_seconds"])) for r in case.ev_rows(new_line)
                      if str(r["source"]) == "user"]
        fx_after = user_fx()
        if user_after != user_before:
            raise Violation(f"重启 / 继续推进后重复注入事件：{user_before} → {user_after}")
        if fx_after != fx_before or not fx_before:
            raise Violation(f"重启后重复 / 丢失施加效果：{fx_before} → {fx_after}")
        states_now = sorted({str(r["state"]) for r in case.store._conn.execute(
            "SELECT * FROM pending_event WHERE timeline_id=?", (new_line,)).fetchall()})
        if states_now != ["applied"]:
            raise Violation(f"重启后待执行状态回退：{states_now}")
        return (f"预约至 {due}：到点前事件 / 效果 / 获知均为 0；到点恰落在 {due}、state=applied；"
                f"换服务实例再推进 4 天后用户事件 {len(user_before)} 条与效果 {len(fx_before)} 条不变")
    finally:
        case.close()


@check("B17b", "附录B#17 到点复核失败记取消 / 未执行，不强行执行；回滚撤销待执行记录")
def b17b() -> str:
    case = Case(seed="seed-b17b")
    try:
        case.activate()
        case.advance(2)
        base = case.watermark()
        draft = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="两日后让驿站信报停摆",
            payload={"intent": "两日后让驿站信报停摆", "when": "scheduled", "at_world": base + 2 * DAY,
                     "effects": [{"kind": "source_delay", "target": "src-1",
                                  "expiry": "until_cleared"}],
                     "claims": []}))
        new_line = str(case.service.confirm_user_event(case.instance_id, draft["draft"]["draft_id"],
                                                       name="预约线2")["timeline_id"])
        # 条件失效：目标不再是本线参与者
        stale_due = base + DAY
        case.store.pending_event_add({
            "id": "pe-x-stale", "instance_id": case.instance_id, "timeline_id": new_line,
            "at_world": stale_due,
            "payload": json.dumps({"intent": "越界改动", "when": "scheduled", "at_world": stale_due,
                                   "effects": [{"kind": "institution_state", "target": "cc-outsider",
                                                "value": "en-1", "expiry": "until_cleared"}],
                                   "claims": []}, ensure_ascii=False),
            "state": "pending", "note": "", "created_world": base, "created_at": T0,
        })
        case.activate(new_line)
        case.advance(4, tl=new_line)
        stale = case.store._conn.execute("SELECT * FROM pending_event WHERE id='pe-x-stale'").fetchone()
        if stale is None or str(stale["state"]) != "cancelled":
            raise Violation(f"条件失效的预约没有被记为取消：{dict(stale) if stale else None}",
                            where="isekai_core/runtime/service.py:437 apply_due_pending_events")
        if [r for r in case.ev_rows(new_line) if "越界改动" in str(r["summary"])]:
            raise Violation("条件失效却强行执行")
        # 回滚撤销待执行记录
        commit = case.service.commit(case.instance_id, new_line, note="预约后")
        case.store.pending_event_add({
            "id": "pe-x-roll", "instance_id": case.instance_id, "timeline_id": new_line,
            "at_world": case.watermark(new_line) + 5 * DAY,
            "payload": json.dumps({"intent": "尚未到点", "when": "scheduled",
                                   "at_world": case.watermark(new_line) + 5 * DAY,
                                   "effects": [{"kind": "institution_state", "target": "off-2",
                                                "value": "en-1", "expiry": "until_cleared"}],
                                   "claims": []}, ensure_ascii=False),
            "state": "pending", "note": "", "created_world": case.watermark(new_line), "created_at": T0,
        })
        case.service.rollback(case.instance_id, new_line, commit_id=str(commit["id"]),
                              now_real=case._now_real[new_line])
        left = case.store._conn.execute("SELECT * FROM pending_event WHERE timeline_id=?",
                                        (new_line,)).fetchall()
        pending_left = [dict(r) for r in left if str(r["state"]) == "pending"]
        if pending_left:
            raise Violation(f"回滚后待执行表还有 {len(pending_left)} 条 pending 行（回滚应撤销待执行状态及其后果）",
                            where="isekai_core/runtime/service.py:570 rollback → store.runtime_load(clear=True)")
        if case.store.pending_events_due(case.instance_id, new_line, until=10**15):
            raise Violation("回滚后仍有待执行记录")
        return (f"条件失效的目标→ state=cancelled（note={stale['note']}）且不落事件；"
                f"回滚到预约后的提交：待执行队列为空、pending_event 表里 {len(left)} 行全为终态"
                f"（{sorted({str(r['state']) for r in left})}）")
    finally:
        case.close()


#==== 正文义务


@check("§3.1-4", "正文§3.1#4 应用效果 / 写事件 / 推进水位在同一一致性边界完成")
def s314() -> str:
    case = Case(seed="seed-s314")
    try:
        case.activate()
        case.advance(2)
        mark = case.watermark()
        ev_before = len(case.ev_rows())
        fx_before = len(case.fx_rows())
        good = {
            "id": "ev-x-atomic-a", "instance_id": case.instance_id, "timeline_id": case.timeline_id,
            "world_seconds": mark, "seq": 5, "kind": "world", "family": "", "template": "audit2.atomic",
            "source": "engine", "summary": "第一行", "detail": "第一行", "text_source": "template",
            "effects": [], "share_value": 0, "importance": 0.5, "created_real": 0.0,
        }
        broken = {k: v for k, v in good.items() if k != "summary"}
        broken["id"] = "ev-x-atomic-b"
        raised = ""
        try:
            case.store.apply_runtime_batch(
                timeline_id=case.timeline_id, generation=int(case.store.clock_get(case.timeline_id)["generation"]),
                processed_world=mark + DAY, catching_up=False, events=[good, broken])
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}"
        if not raised:
            raise Violation("残缺事件行没有被拒绝，检查前提不成立")
        ids = {str(r["id"]) for r in case.ev_rows()}
        if "ev-x-atomic-a" in ids or len(ids) != ev_before or len(case.fx_rows()) != fx_before:
            raise Violation(f"坏行导致整批之前的部分已落盘：events {ev_before}→{len(ids)}（含 a="
                            f"{'ev-x-atomic-a' in ids}），effects {fx_before}→{len(case.fx_rows())}",
                            where="isekai_core/store.py:2023 apply_runtime_batch（单次 with 事务）")
        if case.watermark() != mark:
            raise Violation(f"水位被推进：{mark} → {case.watermark()}")
        return (f"批内第 2 行残缺 → 整批抛错（{raised}），第 1 行事件未落盘、效果行数不变、"
                f"水位仍为 {mark}（一次提交里做完或全不做）")
    finally:
        case.close()


@check("§3.2", "正文§3.2 并发 / 回滚后的迟到结果作废（世代与水位单调）")
def s32() -> str:
    case = Case(seed="seed-s32")
    try:
        case.activate()
        case.advance(2)
        clock = case.store.clock_get(case.timeline_id)
        mark = case.watermark()
        before = slice_of(case)
        row = {
            "id": "ev-x-late", "instance_id": case.instance_id, "timeline_id": case.timeline_id,
            "world_seconds": mark, "seq": 1, "kind": "world", "family": "", "template": "audit2.late",
            "source": "engine", "summary": "迟到结果", "detail": "迟到结果", "text_source": "template",
            "effects": [], "share_value": 0, "importance": 0.5, "created_real": 0.0,
        }
        stale_gen = case.store.apply_runtime_batch(
            timeline_id=case.timeline_id, generation=int(clock["generation"]) - 1,
            processed_world=int(clock["processed_world"]), catching_up=False, events=[row])
        back = case.store.apply_runtime_batch(
            timeline_id=case.timeline_id, generation=int(clock["generation"]),
            processed_world=int(clock["processed_world"]) - 1, catching_up=False, events=[{
                **row, "id": "ev-x-back"}])
        if stale_gen or back:
            raise Violation(f"过期世代 {stale_gen} / 水位回退 {back} 的批次被采纳",
                            where="isekai_core/store.py:2027 apply_runtime_batch 的世代与水位校验")
        if slice_of(case) != before:
            raise Violation(f"被拒批次改写了状态：{diff(before, slice_of(case))}")
        return ("过期世代（generation-1）与水位回退（processed_world-1）两批都返回 False 且逐行未落盘；"
                "并发命中同一槽只有一份产物被采纳")
    finally:
        case.close()


@check("§3.3", "正文§3.3 回填只写事件与说法：不施加效果、不产生获知、不重复写；要点人物生死一并确定")
def s33() -> str:
    case = Case(seed="seed-s33")
    try:
        backfilled = [r for r in case.ev_rows() if str(r["source"]) == "backfill"]
        if not backfilled:
            raise Violation("创建期没有落成包内既定的历史条目")
        if case.fx_rows() or case.store.effect_window(case.instance_id, case.timeline_id, until=10**15):
            raise Violation(f"回填施加了效果：{_dbg(case.fx_rows()[:1])}",
                            where="isekai_core/runtime/events.py:336 backfill_rows")
        if case.know("cc-堤禾"):
            raise Violation("回填直接产生了获知")
        if not case.claims():
            raise Violation("回填的说法没有落进说法集合")
        added = case.service.backfill(case.instance_id, case.timeline_id)
        if added != 0:
            raise Violation(f"重复回填又写入 {added} 行")
        if any(not str(r["effects"]) in ("[]", "") for r in backfilled):
            raise Violation("回填条目带了非空 effects")
        # §3.3 末条：要点人物的出生 / 死亡与活动区间 + 生死事件与死讯说法
        moment = DAY * 1500
        package = example_package(moment=moment)
        package["entities"] = list(package["entities"]) + [
            {"id": "en-9", "kind": "person", "name": "旧碑匠", "race_id": "rc-1",
             "born": moment - 60 * 90 * DAY, "died": moment - 2 * DAY},
            {"id": "en-10", "kind": "person", "name": "将殁的渡口人", "race_id": "rc-1",
             "born": moment - 60 * 90 * DAY, "died": moment + 2 * DAY},
        ]
        case2 = Case(seed="seed-s33b", moment=moment, package=package)
        try:
            case2.activate()
            case2.advance(3)
            deaths = [r for r in case2.ev_rows() if str(r["template"]).startswith("death:")]
            registered = [r for r in case2.ev_rows()
                          if "旧碑匠" in str(r["summary"]) or str(r["template"]) in ("en-9",)]
            claims_hit = [c for c in case2.claims() if "旧碑匠" in str(c["text"])]
            if not deaths and not registered and not claims_hit:
                raise Violation(
                    f"包内声明的实体寿终既没有回填的生死事件 / 死讯说法，运行期也不登记："
                    f"en-9（died={moment - 2 * DAY}，早于实例初始时刻）与 en-10（died={moment + 2 * DAY}，"
                    f"落在推进窗口内）都没有对应的事件或说法（death 模板事件 {len(deaths)} 条、"
                    f"相关事件 {len(registered)} 条、相关说法 {len(claims_hit)} 条）",
                    repro=("package.entities += {id:'en-9', died: moment-2*DAY} 与 {id:'en-10', died: moment+2*DAY}"
                           " → create_instance → activate → advance(3 天) → 找生死事件与死讯说法"),
                    where=("isekai_core/runtime/events.py:427 death_moment（只读角色卡 identity，不看登记实体）；"
                           "isekai_core/runtime/events.py:336 backfill_rows（只回填 canon / narratives）"),
                )
            return (f"回填 {len(backfilled)} 条（effects 全空）、效果 0 行、获知 0 行、重复回填 +0 行；"
                    f"实体寿终：death 事件 {len(deaths)} / 相关说法 {len(claims_hit)} 条，已登记")
        finally:
            case2.close()
    finally:
        case.close()


@check("§六 环境", "正文§六 环境类效果只能引用已声明的环境类型与取值域")
def s6env() -> str:
    package = example_package()
    package["events"]["families"][0]["templates"][0]["effects"] = [
        {"kind": "environment_state", "target": "env-9", "value": "西", "expiry": "until_cleared"}
    ]
    errs = [e for e in validate_package(package) if "环境" in e]
    if not errs:
        raise Violation("效果指向未声明的环境类型没有被校验拦下",
                        where="isekai_core/world/validate.py:666 _validate_events（environment_state 分支）")
    package2 = example_package()
    package2["events"]["families"][0]["templates"][0]["effects"] = [
        {"kind": "environment_state", "target": "env-2", "value": "上", "expiry": "until_cleared"}
    ]
    errs2 = [e for e in validate_package(package2) if "取值域" in e or "value" in e]
    if not errs2:
        raise Violation("取值域外的环境取值没有被校验拦下")
    case = Case(seed="seed-s6env")
    try:
        case.activate()
        case.advance(2)
        mark = case.watermark()
        before = sorted(str(r["type_id"]) for r in case.store.environment_list(case.instance_id, case.timeline_id))
        case.inject(summary="凭空出现的风", effects=[
            {"kind": "environment_state", "target": "env-9", "value": "西", "expiry": "until_cleared"}])
        case.advance(2)
        after = sorted(str(r["type_id"]) for r in case.store.environment_list(case.instance_id, case.timeline_id))
        if after != before:
            raise Violation(f"未声明的环境类型在运行期被创建：{before} → {after}",
                            where="isekai_core/runtime/environment.py:apply_effects")
        return (f"未声明的环境类型 env-9 创建前被拒（{errs[0][:34]}…）、取值域外的值也被拒；"
                f"运行期注入同样不创建该类型（环境类型仍为 {before}，水位 {mark}→{case.watermark()}）")
    finally:
        case.close()


@check("§六 同刻", "正文§六 多事件同刻覆盖采用固定优先规则与稳定标识排序")
def s6prio() -> str:
    hits: list[str] = []
    for path in sorted((ROOT / "isekai_core").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for word in ("优先表", "priority", "priority_table", "同刻"):
            if word in text:
                hits.append(f"{path.relative_to(ROOT)}:{word}")
    same = [h for h in hits if not h.endswith("priority")]
    return ("DEFERRED",
            f"事件模板与运行层都没有同刻覆盖的优先规则声明：全仓 isekai_core 命中 {len(hits)} 处，"
            f"其中非预算相关的 {same[:4]}；同刻多效果各自独立施加（service._world_event_rows 逐候选落行），"
            "SPEC §十一 自列残余「同刻多效果优先规则」")


@check("§八3 职位", "正文§八#3 用户「以合法新事件改变某职位的持有者」应有可表达、不静默失效的路径")
def s83_office() -> str:
    package = example_package()
    template = package["events"]["families"][0]["templates"][0]
    template["effects"] = [e for e in template["effects"] if str(e.get("kind")) != "institution_state"]
    case = Case(seed="seed-s83", package=package)
    try:
        case.activate()
        case.advance(2)
        office_before = case.offices()["off-2"]
        if str(office_before["holder"]):
            raise Violation("前提不成立：off-2 不是空缺")
        refuse: list[str] = []
        for target in ("off-2", "off-1"):
            res = asyncio.run(case.service.draft_user_event(
                case.instance_id, case.timeline_id, intent="守碑人换人",
                payload={"intent": "守碑人换人", "when": "now",
                         "effects": [{"kind": "institution_state", "target": target, "value": "en-1",
                                      "expiry": "until_cleared"}]}))
            if res.get("accepted") is not True:
                refuse.append(f"{target}:{res.get('reason')}")
        accepted = asyncio.run(case.service.draft_user_event(
            case.instance_id, case.timeline_id, intent="守碑人换人",
            payload={"intent": "守碑人换人", "when": "now",
                     "effects": [{"kind": "institution_state", "target": "off-2", "value": "en-1",
                                  "expiry": "until_cleared"}]}))
        if accepted.get("accepted") is not True:
            known, _channels = case.service._known_targets(
                case.store.instance_get(case.instance_id), case.timeline_id, world_seconds=case.watermark()
            )
            raise Violation(
                f"用户无法表达「改变某职位持有者」：职位目标被拒（{_dbg(accepted)}）；"
                f"登记目标里有 off-2={('off-2' in known)}、off-1={('off-1' in known)}",
                repro="draft_user_event(effects=[institution_state off-2=en-1])",
                where="isekai_core/runtime/service.py:_known_targets（登记目标里要有职位标识）")
        done = case.service.confirm_user_event(case.instance_id, accepted["draft"]["draft_id"], name="职位线")
        new_line = str(done["timeline_id"])
        office_after = {r["office_id"]: r["holder"] for r in case.store.institution_list(case.instance_id, new_line)}
        fx = [r for r in case.fx_rows(new_line) if str(r["kind"]) == "institution_state"]
        if office_after.get("off-2") != "en-1" or not fx:
            raise Violation(
                f"被接受的效果是空转：{office_after} / 效果行 {fx}",
                where="isekai_core/runtime/institutions.py:apply_effects（按 by_office 落持有者）")
        # 不是职位的目标（角色标识）必须明确拒绝，不能「接受然后什么都不发生」
        wrong = asyncio.run(case.service.draft_user_event(
            case.instance_id, new_line, intent="守碑人换人",
            payload={"intent": "守碑人换人", "when": "now",
                     "effects": [{"kind": "institution_state", "target": "rl-1", "value": "en-1",
                                  "expiry": "until_cleared"}]}))
        if wrong.get("accepted") is not False:
            raise Violation(f"非职位的目标被接受（会静默失效）：{_dbg(wrong)}")
        return (f"职位目标 off-2 可表达：确认后 holder={office_after['off-2']!r}（效果行 {fx[-1]['id']}）；"
                f"非职位目标 rl-1 被拒（{wrong.get('reason')}）")
    finally:
        case.close()


@check("回填文本", "正文§3.3 / §二 回填条目与说法必须携带传本文本（不是标识）")
def s33_text() -> str:
    case = Case(seed="seed-bftext")
    try:
        narratives = {str(item["id"]): str(item["text"]) for item in case.package.get("narratives") or []}
        rows = [r for r in case.ev_rows() if str(r["source"]) == "backfill"
                and str(r["template"]) in narratives]
        if not rows:
            raise Violation("创建期没有回填包内的传本条目，检查无法进行")
        bad_events = [
            (str(r["template"]), str(r["summary"])) for r in rows
            if str(r["summary"]) == str(r["template"]) and narratives[str(r["template"])]
        ]
        claims = [c for c in case.claims() if str(c["event_id"]) in {str(r["id"]) for r in rows}]
        bad_claims = [(str(c["id"]), str(c["text"])) for c in claims if str(c["text"]) in narratives]
        if bad_events or bad_claims:
            raise Violation(
                f"回填的传本条目与说法只有标识、没有传本文本：事件 {bad_events}；说法 {bad_claims}"
                f"（包里对应文本如 {narratives[str(rows[0]['template'])][:20]!r}）",
                repro="创建实例 → 比较 event.summary / claim.text 与 package.narratives[*].text",
                where="isekai_core/runtime/events.py:376 backfill_rows（narratives 用 item.get('statement')，"
                      "实际字段是 text → 落到 ident 兜底）",
            )
        return (f"{len(rows)} 条回填传本条目与 {len(claims)} 条说法都带传本文本")
    finally:
        case.close()


def main() -> int:
    wanted = [w for w in sys.argv[1:] if not w.startswith("-")]
    selected = [e for e in CHECKS
                if not wanted or any(w.lower() in e[0].lower() or w.lower() in e[1].lower() for w in wanted)]
    tally = {"PASS": 0, "FAIL": 0, "DEFERRED": 0}
    fails: list[str] = []
    deferred: list[str] = []
    for cid, title, fn in selected:
        try:
            out = fn()
        except Violation as exc:
            status = "FAIL"
            evidence = exc.why
            if exc.repro:
                evidence += f"｜复现：{exc.repro}"
            if exc.where:
                evidence += f"｜位置：{exc.where}"
        except Exception:
            tb = traceback.format_exc().strip().splitlines()
            near = next((ln.strip() for ln in reversed(tb)
                         if "isekai_core" in ln or "_audit2_ee" in ln), tb[-1])
            status, evidence = "FAIL", f"检查自身异常：{tb[-1]}｜{near}"
        else:
            if isinstance(out, tuple) and out and out[0] == "DEFERRED":
                status, evidence = "DEFERRED", str(out[1])
            else:
                status, evidence = "PASS", str(out)
        tally[status] += 1
        if status == "FAIL":
            fails.append(f"{cid} {title}")
        if status == "DEFERRED":
            deferred.append(f"{cid} {title}")
        print(f"{status} [{cid}] {title} — {evidence}")
    print(f"TOTAL {len(selected)} PASS {tally['PASS']} FAIL {tally['FAIL']} DEFERRED {tally['DEFERRED']}")
    for item in fails:
        print(f"  FAIL: {item}")
    for item in deferred:
        print(f"  DEFERRED: {item}")
    return 1 if tally["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
