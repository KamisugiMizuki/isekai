"""行为探针：docs/EVENT_ENGINE_SPEC.md（附录 B + 正文相关条款）逐条行为级审计。

只读项目代码：本脚本只创建自己，不改任何项目文件。
库落在临时目录（不碰 data/isekai.db）；单写入者；不起核心；不调真实 LLM（假 LLM / 纯函数）。

运行：
    cd /d/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_audit_event_engine.py

输出每条 `PASS/FAIL/DEFERRED <摘要> — 证据`，末行 `TOTAL n PASS p FAIL f DEFERRED d`。
"""

from __future__ import annotations

import asyncio
import copy
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
from isekai_core.world.instances import create_instance  # noqa: E402
from isekai_core.world.validate import (  # noqa: E402
    DENSITY_TARGETS,
    SUPPORTED_EFFECTS,
    validate_package,
)

T0 = 1.7e9
SERVICE_KW: dict[str, Any] = {
    "instance_tokens_per_day": 400_000,
    "timeline_tokens_per_day": 150_000,
    "task_tokens_per_day": 60_000,
    "autocommit_enabled": False,
    "catch_up_batches": 64,
    "catch_up_lag_seconds": 10**12,
}


class Fail(Exception):
    """行为不符：带最小复现与代码位置。"""

    def __init__(self, why: str, *, repro: str = "", where: str = "") -> None:
        super().__init__(why)
        self.why, self.repro, self.where = why, repro, where


class Line:
    """一个临时实例：自己的临时库 + 时间线 + 运行层服务。"""

    def __init__(
        self,
        *,
        seed: str | None = None,
        moment: int = DAY * 1500,
        cards: list[dict[str, Any]] | None = None,
        package: dict[str, Any] | None = None,
    ) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="ee-audit-"))
        self.store = Store(self.dir / "isekai.db")
        self.store.ensure_schema()
        self.package = package or example_package("灰潮纪", moment=moment)
        self.cards = cards or [example_card(self.package)]
        info = create_instance(self.store, self.package, self.cards, seed=seed)
        self.instance_id = str(info["id"])
        self.timeline_id = str(self.store.timeline_list(self.instance_id)[0]["id"])
        self.service = RuntimeService(self.store, **SERVICE_KW)
        self.service.ensure_instance(self.instance_id, now_real=T0)
        self.seed = str(self.store.instance_get(self.instance_id)["seed"])
        #: 每条线自己的现实时间游标（相对 T0）：advance 一定要单调递增，否则水位会倒退
        self.now_real: dict[str, float] = {}

    # ---- 基础操作 ----
    def activate(self, tl: str | None = None, *, days: float = 0.0) -> None:
        line = tl or self.timeline_id
        now = T0 + days * DAY if days else self.now_real.get(line, T0)
        self.service.activate(self.instance_id, line, now_real=now)
        self.now_real[line] = now

    def advance(self, *, days: float = 0.0, tl: str | None = None) -> dict[str, Any]:
        line = tl or self.timeline_id
        self.now_real[line] = self.now_real.get(line, T0) + float(days) * DAY
        return self.service.advance(self.instance_id, line, now_real=self.now_real[line])

    def advance_to(self, *, days: float, tl: str | None = None) -> dict[str, Any]:
        """推进到「距本线激活点 days 天」的绝对目标（重放同一区间用）。"""
        line = tl or self.timeline_id
        self.now_real[line] = T0 + float(days) * DAY
        return self.service.advance(self.instance_id, line, now_real=self.now_real[line])

    def watermark(self, tl: str | None = None) -> int:
        return int(self.store.clock_get(tl or self.timeline_id)["processed_world"])

    # ---- 读取 ----
    def event_rows(self, tl: str | None = None, *, until: int = 10**15, limit: int = 2000) -> list[dict[str, Any]]:
        return self.store.event_window(self.instance_id, tl or self.timeline_id, until=until, limit=limit)

    def effect_rows(self, tl: str | None = None, *, until: int = 10**15) -> list[dict[str, Any]]:
        return self.store.effect_window(self.instance_id, tl or self.timeline_id, until=until)

    def raw_effects(self, tl: str | None = None) -> list[dict[str, Any]]:
        line = tl or self.timeline_id
        rows = self.store._conn.execute(
            "SELECT * FROM effect_state WHERE timeline_id=? ORDER BY from_world, id", (line,)
        ).fetchall()
        return [dict(row) for row in rows]

    def knowledge(self, character_id: str, tl: str | None = None) -> list[dict[str, Any]]:
        return self.store.knowledge_window(
            self.instance_id, tl or self.timeline_id, character_id, until=10**15, limit=2000
        )

    def experiences(self, character_id: str, tl: str | None = None) -> list[dict[str, Any]]:
        return self.store.experience_window(
            self.instance_id, tl or self.timeline_id, character_id, until=10**15, limit=2000
        )

    def card(self, name: str) -> dict[str, Any]:
        for card in self.cards:
            if name in str(card["meta"]["card_id"]):
                return card
        raise Fail(f"没有这张卡：{name}")

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    # ---- 注入（走真实的批次边界：与引擎同形的事件 / 效果 / 说法 / 获知） ----
    def inject(
        self,
        *,
        summary: str,
        effects: list[dict[str, Any]] | None = None,
        claims: list[dict[str, Any]] | None = None,
        knowledge: list[dict[str, Any]] | None = None,
        ident: str | None = None,
        at: int | None = None,
        tl: str | None = None,
    ) -> str:
        line = tl or self.timeline_id
        row = self.store.clock_get(line)
        world = int(row["processed_world"]) if at is None else int(at)
        ident = ident or f"ev-audit-{events.stable_key(line, summary, world)[:10]}"
        self.store.apply_runtime_batch(
            timeline_id=line,
            generation=int(row["generation"]),
            processed_world=int(row["processed_world"]),
            catching_up=False,
            events=[
                {
                    "id": ident,
                    "instance_id": self.instance_id,
                    "timeline_id": line,
                    "world_seconds": world,
                    "seq": 7,
                    "kind": "world",
                    "family": "ef-1",
                    "template": "audit.inject",
                    "source": "engine",
                    "summary": summary,
                    "detail": summary,
                    "text_source": "template",
                    "effects": json.dumps(effects or [], ensure_ascii=False),
                    "share_value": 0,
                    "importance": 0.5,
                    "created_real": 0.0,
                }
            ],
            effects=[
                {
                    "id": f"fx-audit-{events.stable_key(ident, index)[:8]}",
                    "instance_id": self.instance_id,
                    "timeline_id": line,
                    "event_id": ident,
                    "target": str(item["target"]),
                    "kind": str(item["kind"]),
                    "family": str(item.get("family") or "ef-1"),
                    "value": item.get("value"),
                    "from_world": world,
                    "expiry": str(item.get("expiry") or "with_cause"),
                    "recovery": str(item.get("recovery") or ""),
                    "active": 1,
                    "cleared_at": None,
                }
                for index, item in enumerate(effects or [])
            ],
            claims=[
                {
                    "id": str(item["id"]),
                    "instance_id": self.instance_id,
                    "timeline_id": line,
                    "event_id": ident,
                    "source_id": str(item.get("source_id") or "src-1"),
                    "text": str(item["text"]),
                    "audience": str(item.get("audience") or "公开"),
                    "earliest_world": int(item.get("earliest_world") or world),
                    "credibility": float(item.get("credibility") or 0.6),
                    "derived_from": None,
                }
                for item in claims or []
            ],
            knowledge=[
                {
                    "id": str(item["id"]),
                    "instance_id": self.instance_id,
                    "timeline_id": line,
                    "character_id": str(item["character_id"]),
                    "world_seconds": int(item.get("world_seconds") or world),
                    "kind": "claim",
                    "target": str(item["target"]),
                    "source": str(item.get("source") or "src-1"),
                    "stance": "recorded",
                    "text": str(item["text"]),
                }
                for item in knowledge or []
            ],
        )
        return ident


# ---------------------------------------------------------------- 检查注册


CHECKS: list[tuple[str, str, Callable[[], Any]]] = []


def check(cid: str, title: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    def deco(fn: Callable[[], Any]) -> Callable[[], Any]:
        CHECKS.append((cid, title, fn))
        return fn

    return deco


def _facts(line: Line, tl: str | None = None, characters: list[str] | None = None) -> dict[str, Any]:
    """线的事实切片（不含现实时间戳）：事件 / 效果 / 经历 / 获知。"""
    line_id = tl or line.timeline_id
    chars = characters or [str(card["meta"]["card_id"]) for card in line.cards]
    return {
        "events": [
            (int(r["world_seconds"]), int(r["seq"]), str(r["id"]), str(r["template"]), str(r["summary"]),
             str(r["effects"]), int(r["share_value"]), float(r["importance"]))
            for r in line.event_rows(line_id)
        ],
        "effects": [
            (str(r["id"]), str(r["target"]), str(r["kind"]), int(r["from_world"]), str(r["expiry"]),
             int(r["active"]), r["cleared_at"], r.get("value"), str(r.get("recovery") or ""))
            for r in line.raw_effects(line_id)
        ],
        "experiences": [
            (str(r["id"]), str(r["character_id"]), int(r["world_seconds"]), str(r["kind"]), str(r["summary"]))
            for card_id in chars
            for r in line.experiences(card_id, line_id)
        ],
        "knowledge": [
            (str(r["id"]), str(r["character_id"]), int(r["world_seconds"]), str(r["kind"]), str(r["target"]),
             str(r["source"]), str(r["text"]))
            for card_id in chars
            for r in line.knowledge(card_id, line_id)
        ],
    }


def _diff(left: Any, right: Any, limit: int = 4) -> str:
    out = []
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            a, b = left.get(key), right.get(key)
            if a != b:
                out.append(f"{key}: 左 {len(a) if hasattr(a, '__len__') else a} / 右 {len(b) if hasattr(b, '__len__') else b} 项不同")
                if isinstance(a, list) and isinstance(b, list):
                    only_a = [item for item in a if item not in b][:limit]
                    only_b = [item for item in b if item not in a][:limit]
                    if only_a:
                        out.append(f"  仅左有：{only_a}")
                    if only_b:
                        out.append(f"  仅右有：{only_b}")
    return "；".join(out) or "（无可见差异）"


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
        index = min(self.calls - 1, len(self.replies) - 1)
        return self.replies[index]


def _engine_rows(line: Line, tl: str | None = None) -> list[dict[str, Any]]:
    return [row for row in line.event_rows(tl) if row["source"] == "engine"]


# ---------------------------------------------------------------- 附录 B 逐条


@check("B1a", "附录B#1 同一输入跨进程 / 不同哈希种子得到相同骨架、效果与事件顺序")
def b1a() -> str:
    code = (
        "import json, sys;"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "from isekai_core.runtime import events;"
        "from isekai_core.runtime.calendar import calendar_from_package;"
        "from isekai_core.world.example import example_package;"
        "p = example_package(); c = calendar_from_package(p);"
        "plan = events.plan_day(p, seed='seed-fixed', rules_version='1.0', day_index=1562, calendar=c,"
        " events=set(), effects=set());"
        "print(json.dumps([[i['slot'], i['template'], i['summary'], events.event_moment('seed-fixed','1.0',1562,i['slot'],c.day_seconds)]"
        " for i in plan], ensure_ascii=False));"
        "print(events.event_id('seed-fixed','1.0',1562,'s0'), events.stable_key('seed', 0.1, 1562))"
    )
    outs: list[tuple[str, str]] = []
    for hash_seed in ("1", "2", "random"):
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
        )
        if proc.returncode != 0:
            raise Fail(f"子进程失败：{proc.stderr.strip()[-200:]}", repro="python -c '<stable_key/plan_day>'")
        outs.append((hash_seed, proc.stdout.strip()))
    if len({item[1] for item in outs}) != 1:
        raise Fail(f"不同进程 / 哈希种子结果不一致：{outs}", where="isekai_core/runtime/events.py:29 stable_key")
    planned = json.loads(outs[0][1].splitlines()[0])
    if not planned:
        raise Fail("固定种子下当日没有候选（应至少抽出槽）", where="isekai_core/runtime/events.py:125 plan_day")
    plan = events.plan_day(
        example_package(), seed="seed-fixed", rules_version="1.0", day_index=1562,
        calendar=calendar_from_package(example_package()), events=set(), effects=set(),
    )
    inproc = [[item["slot"], item["template"], item["summary"]] for item in plan]
    if [item[:3] for item in planned] != inproc:
        raise Fail(f"进程内与子进程候选不一致：{inproc} vs {planned}")
    return (
        f"PYTHONHASHSEED=1/2/random 三个进程输出逐字相同；候选 {[item[:2] for item in planned]}，"
        f"id/key={outs[0][1].splitlines()[1]}（稳定哈希，不用进程随机化的 hash()；"
        f"安卓端维度本机无法执行，只覆盖进程 / 哈希种子维度）"
    )


def _semantics(line: Line) -> dict[str, list[Any]]:
    """跨实例可比的事实切片：去掉带实例 / 线身份的 id（ev-act-* 的 id 由实例与线决定）。"""
    return {
        "events": sorted(
            (int(row["world_seconds"]), str(row["template"]), str(row["summary"]), str(row["effects"]),
             str(row["source"]), int(row["share_value"]), float(row["importance"]))
            for row in line.event_rows()
        ),
        "effect_states": sorted(
            (str(row["target"]), str(row["kind"]), int(row["from_world"]), str(row["expiry"]),
             str(row.get("value")), str(row.get("recovery") or ""), int(row["active"]))
            for row in line.raw_effects()
        ),
        "knowledge": sorted(
            (str(row["kind"]), str(row["source"]), str(row["text"]), int(row["world_seconds"]))
            for card in line.cards for row in line.knowledge(str(card["meta"]["card_id"]))
        ),
        "experiences": sorted(
            (str(row["kind"]), str(row["summary"]), int(row["world_seconds"]))
            for card in line.cards for row in line.experiences(str(card["meta"]["card_id"]))
        ),
    }


@check("B1b", "附录B#1 不同补算分批（含跨日部分批）得到相同骨架、效果与事件顺序")
def b1b() -> str:
    left, right = Line(seed="seed-batch"), Line(seed="seed-batch")
    try:
        left.activate()
        right.activate()
        left.advance(days=20)
        right.advance(days=5.54)  # 5 天 + 13 小时：制造跨日部分批
        right.advance(days=14.46)
        a, b = _semantics(left), _semantics(right)
        if a != b:
            raise Fail(f"分批结果不同：{_diff(a, b)}", repro="同种子两实例：一次 advance(20 天) vs advance(5.54 天)+advance(14.46 天)")
        return (
            f"两种分批下事实切片逐行相同：事件 {len(a['events'])} / 效果 {len(a['effect_states'])} / 经历 "
            f"{len(a['experiences'])} / 获知 {len(a['knowledge'])} 行（左 20 批，右先到部分批边界再补齐）"
        )
    finally:
        left.close()
        right.close()


@check("B1c", "附录B#1 分批边界不改变解除时刻的留档（同种子、同区间）")
def b1c() -> str:
    def cleared(line: Line) -> list[tuple[Any, ...]]:
        return sorted(
            (str(row["target"]), str(row["kind"]), int(row["from_world"]), int(row["active"]), row["cleared_at"])
            for row in line.raw_effects()
        )

    left, right = Line(seed="seed-batch-clear"), Line(seed="seed-batch-clear")
    try:
        left.activate()
        right.activate()
        left.advance(days=20)
        right.advance(days=5.54)
        right.advance(days=14.46)
        a, b = cleared(left), cleared(right)
        diff = [(x, y) for x, y in zip(a, b) if x != y]
        if diff or len(a) != len(b):
            raise Fail(
                f"同一区间、同种子下解除时刻随分批边界不同：{diff[:2]}",
                repro=(
                    "同种子两实例：advance(20 天) vs advance(5.54 天)+advance(14.46 天)；"
                    "比较 effect_state 的 (target, kind, from_world, active, cleared_at)"
                ),
                where="isekai_core/runtime/service.py:1955 _propagate_and_clear（解除判定用批边界 to_world，同族后续事件在批边界才被看到）",
            )
        return f"{len(a)} 条效果的 (target, kind, from_world, active, cleared_at) 在两种分批下完全一致"
    finally:
        left.close()
        right.close()


@check("B2a", "附录B#2 分叉两线的共同过去逐行一致")
def b2a() -> str:
    line = Line(seed="seed-fork")
    try:
        line.activate()
        line.advance(days=3)
        commit = line.service.commit(line.instance_id, line.timeline_id, note="分叉点")
        branch = line.service.fork(line.instance_id, line.timeline_id, commit_id=commit["id"], name="分叉线")
        new_line = str(branch["timeline"]["id"])
        base, same = _facts(line), _facts(line, new_line)
        if base != same:
            raise Fail(f"分叉后共同过去不一致：{_diff(base, same)}", repro="commit → fork → 比较事件/效果/经历/获知")
        state = str(line.store.timeline_get(new_line)["state"])
        return (
            f"分叉线共同过去逐行一致（事件 {len(base['events'])} / 效果 {len(base['effects'])} / "
            f"获知 {len(base['knowledge'])} / 经历 {len(base['experiences'])} 行）；新线默认 state={state}"
        )
    finally:
        line.close()


@check("B2b", "附录B#2 一线引入事件后两线事实分化、原线不被改写")
def b2b() -> str:
    line = Line(seed="seed-fork2")
    try:
        line.activate()
        line.advance(days=3)
        commit = line.service.commit(line.instance_id, line.timeline_id, note="分叉点")
        branch = line.service.fork(line.instance_id, line.timeline_id, commit_id=commit["id"], name="引入前")
        branch_line = str(branch["timeline"]["id"])
        origin_before = _facts(line)
        branch_before = _facts(line, branch_line)

        draft = asyncio.run(
            line.service.draft_user_event(
                line.instance_id, branch_line, intent="堤务吏换人：柳氏接任堤长",
                payload={
                    "intent": "堤务吏换人：柳氏接任堤长",
                    "when": "now",
                    "effects": [{"kind": "institution_state", "target": "rl-1", "expiry": "until_cleared"}],
                    "claims": [{"text": "堤务吏换人，柳氏接任堤长", "source_id": "src-1", "audience": "public"}],
                },
            )
        )
        if not draft.get("accepted"):
            raise Fail(f"草案被拒：{draft.get('reason')}", repro="draft_user_event(payload=…institution_state rl-1)")
        new_line = str(line.service.confirm_user_event(line.instance_id, draft["draft"]["draft_id"], name="引入线")["timeline_id"])

        if _facts(line) != origin_before:
            raise Fail("原线被改写", repro="confirm_user_event 后再比较原线事实切片")
        if _facts(line, branch_line) != branch_before:
            raise Fail("来源线被改写", repro="confirm_user_event 后再比较来源线事实切片")
        injected = [row for row in line.event_rows(new_line) if row["source"] == "user"]
        if len(injected) != 1:
            raise Fail(f"新线应有 1 条用户事件，实际 {len(injected)}", repro="确认后按 source=user 过滤")
        origin, after = _facts(line, new_line), _facts(line)
        if after["events"] == origin["events"]:
            raise Fail("引入事件后新线与原线的事实仍然完全相同（应已分化）")
        return (
            f"共同过去一致后分化：新线多出 1 条用户事件（{injected[0]['id']}）与 1 条效果；"
            f"原线事件 {len(after['events'])} 行、来源线 {len(branch_before['events'])} 行均逐行不变"
        )
    finally:
        line.close()


@check("B3a", "附录B#3 每日随机事件不越预算（含固定节庆同记名额）")
def b3a() -> str:
    line = Line(seed="seed-budget")
    try:
        line.activate()
        line.advance(days=20)
        density = str(line.package["events"]["density"])
        top = DENSITY_TARGETS[density][1]
        by_day: dict[int, int] = {}
        for row in _engine_rows(line):
            day = int(row["world_seconds"]) // DAY
            by_day[day] = by_day.get(day, 0) + 1
        if not by_day:
            raise Fail("推进 20 日没有任何世界级事件", repro="advance(20 天) 后按 source=engine 统计")
        over = {day: count for day, count in by_day.items() if count > top}
        if over:
            raise Fail(f"越预算：{over}（上限 {top}）", where="isekai_core/runtime/events.py:35 daily_budget")
        mismatched = []
        for day, count in sorted(by_day.items()):
            planned = events.plan_day(
                line.package, seed=line.seed, rules_version=line.service.rules_of(line.store.instance_get(line.instance_id)),
                day_index=day, calendar=calendar_from_package(line.package),
                events=set(), effects=set(),
            )
            if count > len(planned):
                mismatched.append((day, count, len(planned)))
        if mismatched:
            raise Fail(f"实际事件多于当日候选：{mismatched}", where="isekai_core/runtime/service.py:1822 _world_event_rows")
        return f"密度档 {density}（上限 {top}）：{len(by_day)} 天里最大 {max(by_day.values())} 条，逐日 ≤ 上限且 ≤ 当日候选数"
    finally:
        line.close()


@check("附录B#3 后半", "附录B#3 无合法来源时可少于目标下限，且不重抽到成功")
def b3b() -> str:
    package = example_package()
    package["events"]["families"][0]["templates"][0]["preconditions"] = ["ev-missing"]
    calendar = calendar_from_package(package)
    planned_days, budget_days = 0, []
    for day in range(1500, 1530):
        budget = events.daily_budget("seed-fixed", "1.0", day, "常规")
        budget_days.append(budget)
        rows = events.plan_day(
            package, seed="seed-fixed", rules_version="1.0", day_index=day, calendar=calendar,
            events=set(), effects=set(),
        )
        planned_days += len([row for row in rows if not row["fixed"]])
    if planned_days != 0:
        raise Fail(f"前置条件不足却发生了 {planned_days} 条事件")
    if max(budget_days) < 1:
        raise Fail("预算下限没有成立，这次检查无法证明「可少于下限」")
    return (
        f"唯一模板的前置事件不存在：30 天里随机事件 {planned_days} 条，而每日预算 {min(budget_days)}–"
        f"{max(budget_days)} 条（下限 1）—— 无合法来源就不发生，候选落空不重抽"
    )


@check("B3c", "附录B#3 固定节日日期不漂移，且优先占有当日名额")
def b3c() -> str:
    calendar = calendar_from_package(example_package())
    days = [
        day for day in range(1500, 1600)
        if events.fixed_events(example_package(), day_index=day, calendar=calendar)
    ]
    if days != [1562]:
        raise Fail(f"固定节日出现在 {days}（应恰为历法第 1562 日 = 雾月 3 日）", where="isekai_core/runtime/events.py:43 fixed_events")
    day_one = next(day for day in range(1500, 1600) if events.fixed_events(example_package(), day_index=day, calendar=calendar))
    rows = events.fixed_events(example_package(), day_index=day_one, calendar=calendar)
    if rows[0]["summary"] != "开滩祭":
        raise Fail(f"节日名不符：{rows[0]['summary']}")
    date = calendar.to_calendar(day_one * DAY)
    planned = events.plan_day(
        example_package(), seed="seed-fixed", rules_version="0.1", day_index=day_one, calendar=calendar,
        events=set(), effects=set(),
    )
    if not planned or not planned[0]["slot"].startswith("fixed-"):
        raise Fail(f"固定节日没有优先占有名额：{planned}")
    budget_rows = [item for item in planned if not item["fixed"]]
    return (
        f"100 个历法日里只有第 {day_one} 日（{date['month']} 月 {date['day']} 日）报出开滩祭；"
        f"当日候选首项为 {planned[0]['slot']}（固定事件先占名额，随机事件 {len(budget_rows)} 条补足剩余额度）"
    )


@check("B3e", "附录B#3 固定节庆当天的世界推进能落成（端到端）")
def b3e() -> str:
    moment = DAY * 1561
    line = Line(seed="seed-festival", moment=moment)
    try:
        line.activate()
        try:
            line.advance(days=4)
        except Exception as exc:
            raise Fail(
                f"固定节庆当天推进直接抛错：{type(exc).__name__}: {exc} —— 节日当天的事件没有落成，世界推进中断",
                repro="moment=DAY*1561（次日即包内固定节日开滩祭）→ activate → advance(4 天)",
                where=(
                    "isekai_core/runtime/events.py:59 固定事件把 effects 写成字符串 '[]'；"
                    "isekai_core/runtime/events.py:171 effect_rows 按列表迭代 → isekai_core/runtime/service.py:1878"
                ),
            ) from exc
        fixed_rows = [row for row in line.event_rows() if str(row["template"]) == "fc-1"]
        if len(fixed_rows) != 1:
            raise Fail(f"开滩祭应恰好发生 1 次，实际 {len(fixed_rows)}")
        landed = int(fixed_rows[0]["world_seconds"])
        if landed // DAY != 1562 or landed >= 1563 * DAY:
            raise Fail(f"节日落到第 {landed // DAY} 日（应为 1562 日）")
        day_rows = [row for row in _engine_rows(line) if int(row["world_seconds"]) // DAY == 1562]
        if not any(str(row["template"]) == "fc-1" for row in day_rows):
            raise Fail("节日当天被随机事件挤掉")
        return (
            f"开滩祭恰在第 1562 日发 1 次、第 1561 日不发生；当天世界级事件 {len(day_rows)} 条含节日；"
            f"推进后水位 {line.watermark() // DAY} 日"
        )
    finally:
        line.close()


@check("B3d", "附录B#3 包内固定事件超密度档上限 → 创建前校验报错")
def b3d() -> str:
    package = example_package()
    package["events"]["density"] = "稀疏"
    family = package["events"]["families"][0]["id"]
    package["events"]["calendar"] = [
        {"id": "fc-a", "name": "开滩节", "month": 1, "day": 1, "family": family},
        {"id": "fc-b", "name": "祭堤日", "month": 1, "day": 1, "family": family},
    ]
    errors = [item for item in validate_package(package) if "密度档" in item and "固定事件" in item]
    if not errors:
        raise Fail("同日两个固定事件在稀疏档（上限 1）未被拒绝")
    package["events"]["calendar"] = package["events"]["calendar"][:1]
    if [item for item in validate_package(package) if "密度档" in item]:
        raise Fail("合法包被误报")
    return f"稀疏档同日 2 个固定事件被挡在创建前：{errors[0][:70]}；删到一个后通过"
    # 不删节日：运行时也不悄悄漏（同一条规则）


@check("B4a", "附录B#4 骨架外事实（改数字 / 非 JSON）在文本校验层被拒")
def b4a() -> str:
    if render.facts_preserved("堤长身故，享年 12 岁", "堤长身故，享年 34"):
        raise Fail("改了数字的表述被判为合规", where="isekai_core/runtime/render.py:35 facts_preserved")
    if not render.facts_preserved("堤长身故，享年 34 岁", "堤长身故，享年 34"):
        raise Fail("同数字的改写被误判")
    if render.parse_render("这里没有 JSON", []) is not None:
        raise Fail("非 JSON 回包被当成功")
    parsed = render.parse_render('{"detail": "堤长身故，享年 34"}', [{"source_id": "src-1"}])
    if parsed is None or parsed["claims"] != {}:
        raise Fail("缺来源说法时未按契约返回")
    return "digits 集合双向比对：骨架外数字 / 缺失数字都不过；非 JSON 与缺字段一律判失败（退化到模板）"


@check("B4b", "附录B#4 LLM 超时 / 报错时世界状态逐字段不变")
def b4b() -> str:
    line = Line(seed="seed-llm")
    try:
        cfg = load_config(line.dir / "cfg")
        ident = line.inject(summary="堤长身故，享年 34")
        before = line.store.event_get(line.instance_id, line.timeline_id, ident)
        claims_before = line.store.claim_list(line.instance_id, line.timeline_id, event_id=ident)
        llm = ScriptedLLM(error=TimeoutError("llm timeout"))
        outcome = ""
        try:
            asyncio.run(world_ops.dispatch_async(
                cfg, llm, "event.render",
                {"instance_id": line.instance_id, "timeline_id": line.timeline_id, "event_id": ident},
                store=line.store,
            ))
        except Exception as exc:  # 超时可以被上报为操作错误，但不能改世界
            outcome = f"{type(exc).__name__}"
        after = line.store.event_get(line.instance_id, line.timeline_id, ident)
        claims_after = line.store.claim_list(line.instance_id, line.timeline_id, event_id=ident)
        if dict(before) != dict(after) or claims_before != claims_after:
            raise Fail(
                f"模型超时改动了世界状态：event {dict(before) != dict(after)}，claims {claims_before != claims_after}",
                where="isekai_core/world/ops.py:685 _render_event",
            )
        if after["text_source"] != "template":
            raise Fail(f"文本来源被改成 {after['text_source']}", where="isekai_core/store.py:2139 event_render_save")
        return (
            f"模型抛 TimeoutError（{outcome or '被调用方捕获'}）：事件行 detail/text_source/summary/effects 与说法逐字段不变，"
            f"text_source 仍为 template"
        )
    finally:
        line.close()


@check("B4c", "附录B#4 已固化文本不重生成；实情文本不原地改写事实骨架")
def b4c() -> str:
    line = Line(seed="seed-llm2")
    try:
        cfg = load_config(line.dir / "cfg")
        ident = line.inject(
            summary="退潮延误，驿站停摆 2 日",
            claims=[{"id": "cl-audit-render", "text": "驿站传：退潮延误，驿站停摆 2 日", "source_id": "src-1"}],
        )
        good = '{"detail": "（信报抄存）退潮延误，驿站停摆 2 日", "claims": {"src-1": "驿站传：停摆 2 日"}}'
        llm = ScriptedLLM([good])
        first = asyncio.run(world_ops.dispatch_async(
            cfg, llm, "event.render",
            {"instance_id": line.instance_id, "timeline_id": line.timeline_id, "event_id": ident},
            store=line.store,
        ))
        if first.get("text_source") != "llm":
            raise Fail(f"合规表述没有固化：{first}")
        saved = line.store.event_get(line.instance_id, line.timeline_id, ident)
        if saved["summary"] != "退潮延误，驿站停摆 2 日" or int(saved["seq"]) != 7:
            raise Fail(f"事实骨架被语言产物改写：{saved['summary']!r} / seq={saved['seq']}")
        second = asyncio.run(world_ops.dispatch_async(
            cfg, llm, "event.render",
            {"instance_id": line.instance_id, "timeline_id": line.timeline_id, "event_id": ident},
            store=line.store,
        ))
        if not second.get("reused") or llm.calls != 1:
            raise Fail(f"已固化文本被重新生成：reused={second.get('reused')} calls={llm.calls}")
        bad = '{"detail": "退潮延误，驿站停摆 5 日", "claims": {}}'
        ident2 = line.inject(
            summary="退潮延误，驿站停摆 2 日", ident="ev-audit-num2",
            claims=[{"id": "cl-audit-render2", "text": "驿站传：退潮延误，驿站停摆 2 日", "source_id": "src-1"}],
        )
        llm2 = ScriptedLLM([bad, bad])
        third = asyncio.run(world_ops.dispatch_async(
            cfg, llm2, "event.render",
            {"instance_id": line.instance_id, "timeline_id": line.timeline_id, "event_id": ident2},
            store=line.store,
        ))
        if third.get("text_source") != "template" or "未过校验" not in str(third.get("note")):
            raise Fail(f"骨架外数字被采纳：{third}")
        return (
            f"合规表述固化（detail={first['detail'][:18]}…），summary/seq 不变；二次调用 reused=True 且模型只调 1 次；"
            f"改成 5 日的表述连续两次未过校验 → 退回模板（{third['note']}）"
        )
    finally:
        line.close()


@check("B5a", "附录B#5 不同角色只获知相应说法（渠道硬约束）")
def b5a() -> str:
    package = example_package()
    holder = example_card(package, name="堤禾")
    deaf = example_card(package, name="碑拓者")
    deaf["channels"] = [{"source_id": "src-2", "conditions": "只在碑拓上读到旧事"}]
    line = Line(seed="seed-know", cards=[holder, deaf], package=package)
    try:
        line.activate()
        line.advance(days=4)
        channels = {"cc-堤禾": {"src-1"}, "cc-碑拓者": {"src-2"}}
        seen: dict[str, set[str]] = {}
        for character_id, allowed in channels.items():
            rows = [row for row in line.knowledge(character_id) if row["kind"] == "claim"]
            if not rows:
                raise Fail(f"{character_id} 什么也没获知（共 {len(line.knowledge(character_id))} 条获知），无法证明渠道生效")
            seen[character_id] = {str(row["source"]) for row in rows}
            foreign = seen[character_id] - allowed
            if foreign:
                raise Fail(
                    f"{character_id} 拿到了不属于自己渠道的说法：{sorted(foreign)}",
                    where="isekai_core/runtime/events.py:233 grants（按卡片渠道发获知）",
                )
        if seen["cc-堤禾"] & seen["cc-碑拓者"]:
            raise Fail(f"两个角色的获知来源出现交集：{sorted(seen['cc-堤禾'] & seen['cc-碑拓者'])}")
        return (
            f"4 天后：cc-堤禾（src-1）获知来源 {sorted(seen['cc-堤禾'])}；cc-碑拓者（src-2）获知来源 "
            f"{sorted(seen['cc-碑拓者'])}；互不交叉（「公开」标签不构成获知资格）"
        )
    finally:
        line.close()


@check("B5b", "附录B#5 未接触的说法不进入扮演定义与素材来源；实情文本不注入")
def b5b() -> str:
    package = example_package()
    holder = example_card(package, name="堤禾")
    deaf = example_card(package, name="碑拓者")
    deaf["channels"] = [{"source_id": "src-2", "conditions": "只在碑拓上读到旧事"}]
    line = Line(seed="seed-prompt", cards=[holder, deaf], package=package)
    try:
        line.activate()
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        marker = "ZZTEST：驿站信报载北堤旧闻一则"
        ident = line.inject(
            summary="北堤旧闻",
            claims=[{"id": "cl-audit-marker", "text": marker, "source_id": "src-1", "earliest_world": watermark + 1}],
        )
        line.advance(days=2)
        holders = line.store.knowledge_holders(line.instance_id, line.timeline_id, "cl-audit-marker")
        knows = [row for row in line.knowledge("cc-堤禾") if marker in str(row["text"])]
        if not knows:
            raise Fail(f"持渠道角色没有获知该说法（holders={holders}）", where="isekai_core/runtime/service.py:1955 _propagate_and_clear")
        prompt_holder = line.service.system_prompt(
            {"instance_id": line.instance_id, "timeline_id": line.timeline_id, "character_id": "cc-堤禾"}
        )
        prompt_deaf = line.service.system_prompt(
            {"instance_id": line.instance_id, "timeline_id": line.timeline_id, "character_id": "cc-碑拓者"}
        )
        if marker not in prompt_holder:
            raise Fail("已获知的说法没有进入扮演定义", where="isekai_core/runtime/service.py:2571 system_prompt")
        if marker in prompt_deaf:
            raise Fail("未接触的角色拿到了该说法", where="isekai_core/runtime/cognition.py:40 play_context")
        line.service.queue_world_sources(line.instance_id, line.timeline_id, since_world=0)
        tasks = line.store.memory_tasks(line.instance_id, line.timeline_id, state="pending")
        bad = [
            task for task in tasks
            if str(task["character_id"]) == "cc-碑拓者" and "cl-audit-marker" in str(task["source_ref"])
        ]
        if bad:
            raise Fail("未接触的事件进入了她（碑拓者）的素材来源")
        # 实情文本（detail）不注入扮演上下文
        line.store.event_render_save(
            line.instance_id, line.timeline_id, ident,
            detail="SECRET-DETAIL：内部一致性用的实情文本", claims={},
        )
        prompt_holder2 = line.service.system_prompt(
            {"instance_id": line.instance_id, "timeline_id": line.timeline_id, "character_id": "cc-堤禾"}
        )
        if "SECRET-DETAIL" in prompt_holder2:
            raise Fail("实情文本被注入角色上下文", where="isekai_core/runtime/cognition.py:40 play_context")
        return (
            f"同一条说法：cc-堤禾 获知并出现在扮演定义；cc-碑拓者既不在扮演定义里、也没有对应素材任务"
            f"（{len([t for t in tasks if str(t['character_id']) == 'cc-碑拓者'])} 条任务全与它无关）；"
            f"事件的实情文本（detail）不进入扮演定义"
        )
    finally:
        line.close()


@check("B5c", "附录B#5 尚未传播到达的说法不产生获知（未来槽不进当前认知）")
def b5c() -> str:
    line = Line(seed="seed-future")
    try:
        line.activate()
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        line.inject(
            summary="迟来的消息",
            claims=[{
                "id": "cl-audit-future", "text": "ZZFUTURE：三日后才见报", "source_id": "src-1",
                "earliest_world": watermark + 3 * DAY,
            }],
        )
        line.advance(days=1)
        early = [row for row in line.knowledge("cc-堤禾") if str(row["target"]) == "cl-audit-future"]
        if early:
            raise Fail("传播时刻未到就给了获知", where="isekai_core/runtime/events.py:300 claim_grant")
        line.advance(days=4)
        late = [row for row in line.knowledge("cc-堤禾") if str(row["target"]) == "cl-audit-future"]
        if not late:
            raise Fail("传播时刻过后仍未获知")
        return (
            f"最早传播时刻 {watermark + 3 * DAY}：第 1 天获知 {len(early)} 条；推进到第 5 天后获知 {len(late)} 条"
            f"（获知时刻 {late[0]['world_seconds']} ≥ 传播时刻）"
        )
    finally:
        line.close()


@check("B6a", "附录B#6 同一区间重放不重复产事件 / 经历 / 获知（含换一个服务实例）")
def b6a() -> str:
    line = Line(seed="seed-idem")
    try:
        line.activate()
        line.advance_to(days=5)
        first = _facts(line)
        line.advance_to(days=5)  # 重放同一区间
        second = _facts(line)
        if first != second:
            raise Fail(f"同区间重放产生新行：{_diff(first, second)}", where="isekai_core/runtime/service.py:1457 advance")
        line.service = RuntimeService(line.store, **SERVICE_KW)
        line.advance_to(days=5)  # 换一个服务实例重放
        third = _facts(line)
        if first != third:
            raise Fail(f"换一个服务实例后重放产生新行：{_diff(first, third)}")
        return (
            f"5 日区间重放两次 + 新服务实例重放一次：事件 {len(first['events'])} / 经历 {len(first['experiences'])} / "
            f"获知 {len(first['knowledge'])} 行完全不变"
        )
    finally:
        line.close()


@check("B6b", "附录B#6 跨日补算不改过去（已有事件 / 经历 / 计划逐行不变）")
def b6b() -> str:
    line = Line(seed="seed-past")
    try:
        line.activate()
        line.advance(days=5)
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        past_events = [dict(row) for row in line.event_rows() if int(row["world_seconds"]) <= watermark]
        past_exp = [dict(row) for row in line.experiences("cc-堤禾") if int(row["world_seconds"]) <= watermark]
        past_plans = {
            int(row["day_index"]): str(row["windows"])
            for row in line.store._conn.execute(
                "SELECT * FROM life_plan WHERE timeline_id=? ORDER BY day_index", (line.timeline_id,)
            ).fetchall()
            if int(row["day_index"]) <= watermark // DAY
        }
        line.advance(days=4)
        now_events = {str(row["id"]): dict(row) for row in line.event_rows()}
        now_exp = {str(row["id"]): dict(row) for row in line.experiences("cc-堤禾")}
        for row in past_events:
            if now_events.get(str(row["id"])) != row:
                raise Fail(f"过去的事件行被改写：{row['id']}", where="isekai_core/runtime/service.py:1457 advance")
        for row in past_exp:
            if now_exp.get(str(row["id"])) != row:
                raise Fail(f"过去的经历行被改写：{row['id']}")
        now_plans = {
            int(row["day_index"]): str(row["windows"])
            for row in line.store._conn.execute(
                "SELECT * FROM life_plan WHERE timeline_id=? ORDER BY day_index", (line.timeline_id,)
            ).fetchall()
        }
        for day, windows in past_plans.items():
            if now_plans.get(day) != windows:
                raise Fail(f"第 {day} 日的计划被改写")
        return f"再推进 4 天后：{len(past_events)} 条过去事件、{len(past_exp)} 条经历、{len(past_plans)} 份计划逐行不变"
    finally:
        line.close()


@check("B6c", "附录B#6 经历 / 计划不重复，派生说法不成环")
def b6c() -> str:
    line = Line(seed="seed-cycle")
    try:
        line.activate()
        line.advance(days=10)
        exp = line.experiences("cc-堤禾")
        ids = [str(row["id"]) for row in exp]
        if len(ids) != len(set(ids)):
            dup = [item for item in ids if ids.count(item) > 1][:3]
            raise Fail(f"经历出现重复 id：{dup}", where="isekai_core/runtime/service.py:1996 _harvest")
        plans = line.store._conn.execute(
            "SELECT day_index, COUNT(*) AS n FROM life_plan WHERE timeline_id=? GROUP BY day_index HAVING n>1",
            (line.timeline_id,),
        ).fetchall()
        if plans:
            raise Fail(f"同一角色同一世界日有多份计划：{[dict(r) for r in plans]}")
        graph: dict[str, str] = {}
        for row in line.store.claim_list(line.instance_id, line.timeline_id):
            if row.get("derived_from"):
                graph[str(row["id"])] = str(row["derived_from"])
        for start in graph:
            seen, node = set(), start
            while node in graph:
                if node in seen:
                    raise Fail(f"派生说法成环：{start}", where="isekai_core/world/ops.py:744 _expand_claim")
                seen.add(node)
                node = graph[node]
        event_ids = [str(row["id"]) for row in line.event_rows()]
        if len(event_ids) != len(set(event_ids)):
            raise Fail("事件 id 重复")
        return f"10 天后：经历 {len(ids)} 条 id 全不同、每角色每日 1 份计划、事件 id 唯一、派生说法图无环（{len(graph)} 条派生记录）"
    finally:
        line.close()


@check("B7a", "附录B#7 草案拒绝不可表达意图 / 未支持效果 / 未登记目标")
def b7a() -> str:
    line = Line(seed="seed-draft")
    try:
        line.activate()
        line.advance(days=2)
        no_effect = asyncio.run(line.service.draft_user_event(line.instance_id, line.timeline_id, intent="让潮水永远退去"))
        if no_effect.get("accepted") is not False:
            raise Fail(f"无效果的意图被接受：{no_effect}")
        bad_kind = asyncio.run(line.service.draft_user_event(
            line.instance_id, line.timeline_id, intent="改天气",
            payload={"effects": [{"kind": "weather_magic", "target": "rl-1"}]},
        ))
        if bad_kind.get("accepted") is not False or "不支持" not in str(bad_kind.get("reason")):
            raise Fail(f"未支持的效果类型被接受：{bad_kind}", where="isekai_core/runtime/drafts.py:48 normalize_draft")
        unknown = asyncio.run(line.service.draft_user_event(
            line.instance_id, line.timeline_id, intent="换人",
            payload={"effects": [{"kind": "institution_state", "target": "rl-99"}]},
        ))
        if unknown.get("accepted") is not False or "未登记" not in str(unknown.get("reason")):
            raise Fail(f"未登记目标被接受：{unknown}")
        return (
            f"无效果 → 拒绝（{str(no_effect['reason'])[:24]}…）；未支持类型 → 拒绝（{bad_kind['reason']}）；"
            f"未登记目标 → 拒绝（{unknown['reason']}）；三次都不落草案"
        )
    finally:
        line.close()


@check("B7b", "附录B#7 确认后只作用新线：原线不动、新线冻结、重试复用同一次创建")
def b7b() -> str:
    line = Line(seed="seed-confirm")
    try:
        line.activate()
        line.advance(days=2)
        before = _facts(line)
        draft = asyncio.run(line.service.draft_user_event(
            line.instance_id, line.timeline_id, intent="堤务吏换人：柳氏接任堤长",
            payload={
                "intent": "堤务吏换人：柳氏接任堤长", "when": "now",
                "effects": [{"kind": "institution_state", "target": "rl-1", "expiry": "until_cleared"}],
                "claims": [{"text": "堤务吏换人，柳氏接任堤长", "source_id": "src-1", "audience": "public"}],
            },
        ))
        confirmed = line.service.confirm_user_event(line.instance_id, draft["draft"]["draft_id"], name="柳氏线")
        new_line = str(confirmed["timeline_id"])
        if str(line.store.timeline_get(new_line)["state"]) != "frozen":
            raise Fail("新线没有默认冻结")
        if _facts(line) != before:
            raise Fail("原线被改写", where="isekai_core/runtime/service.py:334 confirm_user_event")
        injected = [row for row in line.event_rows(new_line) if row["source"] == "user"]
        effects = [row for row in line.raw_effects(new_line) if str(row["event_id"]) == str(injected[0]["id"])]
        if len(injected) != 1 or not effects:
            raise Fail(f"新线没有注入事件 / 效果：events={len(injected)} effects={len(effects)}")
        again = line.service.confirm_user_event(line.instance_id, draft["draft"]["draft_id"])
        if not again.get("reused") or str(again["timeline_id"]) != new_line:
            raise Fail(f"重试没有复用同一次创建：{again}")
        if len(line.store.timeline_list(line.instance_id)) != 2:
            raise Fail("重试重复造线")
        return (
            f"新线 state=frozen，含 1 条 user 事件 + {len(effects)} 条效果；原线 {len(before['events'])} 条事件逐行不变；"
            f"重试 reused=True 且线数仍为 2"
        )
    finally:
        line.close()


@check("附录B#7 第6条", "附录B#7 注入失败不留下半条线（原子性）")
def b7c() -> str:
    line = Line(seed="seed-atomic")
    try:
        line.activate()
        line.advance(days=2)
        before = _facts(line)
        lines_before = len(line.store.timeline_list(line.instance_id))
        draft = asyncio.run(line.service.draft_user_event(
            line.instance_id, line.timeline_id, intent="堤务吏换人：柳氏接任堤长",
            payload={
                "intent": "堤务吏换人：柳氏接任堤长", "when": "now",
                "effects": [{"kind": "institution_state", "target": "rl-1", "expiry": "until_cleared"}],
            },
        ))
        draft_id = str(draft["draft"]["draft_id"])
        real = line.store.apply_runtime_batch

        def _refuse(**kwargs: Any) -> bool:
            return False  # 模拟世代已变：整批不落盘

        line.store.apply_runtime_batch = _refuse  # type: ignore[method-assign]
        raised = ""
        try:
            line.service.confirm_user_event(line.instance_id, draft_id)
        except RuntimeStateError as exc:
            raised = str(exc)
        finally:
            line.store.apply_runtime_batch = real  # type: ignore[method-assign]
        if not raised:
            raise Fail("注入被拒时没有报错")
        if len(line.store.timeline_list(line.instance_id)) != lines_before:
            raise Fail(f"留下了半条线：{len(line.store.timeline_list(line.instance_id))} 条（应为 {lines_before}）",
                       where="isekai_core/runtime/service.py:356 confirm_user_event")
        if _facts(line) != before:
            raise Fail("失败路径改写了分叉来源线")
        if str(line.store.draft_get(draft_id)["state"]) != "draft":
            raise Fail("失败的草案被标成已确认")
        return f"注入被拒（{raised}）→ 线数仍为 {lines_before}、来源线事实不变、草案仍为 draft（失败不留下半条线）"
    finally:
        line.close()


@check("附录B#7 第5条", "附录B#7 校验提示不泄露世界秘密（只谈用户自己的输入）")
def b7d() -> str:
    package = example_package("灰潮纪")
    line = Line(package=package)
    try:
        line.activate()
        line.advance(days=2)
        secrets = [str(item.get("statement") or "") for item in package.get("canon") or []]
        secrets += [str(item.get("text") or "") for item in package.get("narratives") or []]
        secrets += [str(item.get("question") or "") for item in (package.get("initial_state") or {}).get("mysteries") or []]
        secrets = [item for item in secrets if item]
        reasons: list[str] = []
        for payload in (
            {"effects": [{"kind": "institution_state", "target": "rl-99"}]},
            {"effects": [{"kind": "weather_magic", "target": "rl-1"}]},
            {"effects": [{"kind": "institution_state", "target": "rl-1", "value": "en-9"}]},
        ):
            result = asyncio.run(line.service.draft_user_event(
                line.instance_id, line.timeline_id, intent="我要北堤的真相", payload=payload,
            ))
            reasons.append(str(result.get("reason") or ""))
        for reason in reasons:
            leaked = [item[:16] for item in secrets if item[:16] and item[:16] in reason]
            if leaked:
                raise Fail(f"拒绝理由泄露了既有隐藏内容：{leaked}", where="isekai_core/runtime/service.py:314 draft_user_event")
        ok = asyncio.run(line.service.draft_user_event(
            line.instance_id, line.timeline_id, intent="堤务吏换人：柳氏接任堤长",
            payload={"intent": "堤务吏换人：柳氏接任堤长", "when": "now",
                     "effects": [{"kind": "institution_state", "target": "rl-1", "expiry": "until_cleared"}]},
        ))
        blob = json.dumps(ok["draft"], ensure_ascii=False)
        leaked = [item[:16] for item in secrets if item[:16] and item[:16] in blob]
        if leaked:
            raise Fail(f"草案展示泄露了既有隐藏内容：{leaked}")
        return f"3 条拒绝提示 + 1 份通过草案都不含秘密文本（比对 {len(secrets)} 条 canon/narratives/mysteries 片段）"
    finally:
        line.close()


@check("B8a", "附录B#8 完全虚构的指控可入史料并被角色读到，但不成为实情、不执行效果")
def b8a() -> str:
    package = example_package()
    keyword = "私吞修堤的粮"
    package["canon"].append({"id": "cf-3", "statement": f"坊间称堤长{keyword}，为灾年编年所不收。", "tags": ["指控"]})
    package["narratives"].append({
        "id": "nv-3", "text": f"有匿名投书称堤长{keyword}。", "source_id": "src-1", "canon_ref": "cf-3",
        "obtain": ["在城邦驿站读到投书抄件"], "confidence": "doubted",
    })
    package["initial_state"]["events"] = list(package["initial_state"]["events"]) + ["cf-3"]
    package["initial_state"]["rumors"] = list(package["initial_state"]["rumors"]) + ["nv-3"]
    card = example_card(package)
    card["initial_knowledge"] = list(card["initial_knowledge"]) + [
        {"ref_type": "narrative", "ref_id": "nv-3", "obtained_at": DAY * 1400}
    ]
    line = Line(seed="seed-accuse", package=package, cards=[card])
    try:
        line.activate()
        line.advance(days=3)
        effects = line.raw_effects()
        hit_effects = [
            row for row in effects
            if keyword in str(row.get("value") or "") or keyword in str(row.get("recovery") or "")
        ]
        if hit_effects:
            raise Fail(f"指控直接产生了事实效果：{hit_effects}", where="isekai_core/runtime/events.py:159 effect_rows")
        facts = [
            row for row in line.event_rows()
            if keyword in str(row["summary"]) and str(row["source"]) != "backfill"
        ]
        if facts:
            raise Fail(f"指控被当成实情写进运行期事件：{[(r['id'], r['source']) for r in facts]}")
        prompt = line.service.system_prompt(
            {"instance_id": line.instance_id, "timeline_id": line.timeline_id, "character_id": str(card["meta"]["card_id"])}
        )
        if keyword not in prompt:
            raise Fail("角色读不到该指控，无法证明它影响判断")
        record = [row for row in line.event_rows() if keyword in str(row["summary"])]
        if not record or any(str(row["effects"]) not in ("[]", "") for row in record):
            raise Fail("史料条目不是「不施加效果」的记录形态")
        return (
            f"史料形态入册（{record[0]['id']}，effects=[]，来源 {record[0]['source']}）；角色扮演定义里读得到该指控"
            f"（判断输入成立）；{len(effects)} 条效果里 0 条由它产生，也没有任何运行期事件把它写成实情"
        )
    finally:
        line.close()


@check("附录3.3", "附录B#10 回填不施加效果、不产生获知，且重复调用不再写入")
def b8b() -> str:
    line = Line(seed="seed-backfill")
    try:
        backfilled = [row for row in line.event_rows() if row["source"] == "backfill"]
        if not backfilled:
            raise Fail("创建期没有落成包内既定的历史条目")
        if line.effect_rows() != [] or line.raw_effects() != []:
            raise Fail(f"回填施加了效果：{line.raw_effects()[:2]}", where="isekai_core/runtime/events.py:336 backfill_rows")
        if line.knowledge("cc-堤禾") != []:
            raise Fail("回填直接产生了获知")
        if line.store.claim_list(line.instance_id, line.timeline_id) == []:
            raise Fail("回填的说法没有落进说法集合")
        added = line.service.backfill(line.instance_id, line.timeline_id)
        if added != 0:
            raise Fail(f"重复回填又写入 {added} 行")
        prior = len(backfilled)
        line.service.queue_world_sources(line.instance_id, line.timeline_id, since_world=0)
        tasks = line.store.memory_tasks(line.instance_id, line.timeline_id, state="pending")
        kinds = sorted({str(task["source_kind"]) for task in tasks})
        unexpected = [kind for kind in kinds if kind != "intent"]
        if unexpected:
            raise Fail(
                f"回填产物变成了素材任务：{unexpected}（{[(t['source_kind'], t['source_ref']) for t in tasks][:3]}）",
                where="isekai_core/runtime/service.py:682 queue_world_sources",
            )
        return (
            f"回填 {prior} 条历史事件（effects 全为 []）、效果 0 行、获知 0 行；重复回填新增 0 行；"
            f"素材来源只剩角色自己的打算（{kinds or '无'}），没有 claim / 经历类回填素材"
        )
    finally:
        line.close()


@check("B9a", "附录B#9 展开只对已持有的记载；产出派生记录、原记录不改、重复提问复用")
def b9a() -> str:
    package = example_package()
    holder = example_card(package, name="堤禾")
    deaf = example_card(package, name="碑拓者")
    deaf["channels"] = [{"source_id": "src-2", "conditions": "只在碑拓上读到旧事"}]
    line = Line(seed="seed-expand", cards=[holder, deaf], package=package)
    try:
        cfg = load_config(line.dir / "cfg")
        line.activate()
        line.advance(days=3)
        claim = next(
            row for row in line.store.claim_list(line.instance_id, line.timeline_id)
            if str(row["source_id"]) == "src-1" and line.store.knowledge_holders(
                line.instance_id, line.timeline_id, str(row["id"])
            )
        )
        holders = line.store.knowledge_holders(line.instance_id, line.timeline_id, str(claim["id"]))
        others = line.store.knowledge_holders(line.instance_id, line.timeline_id, str(claim["id"]))
        if "cc-堤禾" not in holders:
            raise Fail("持渠道角色没有该记载，检查前提不成立")
        denied = ""
        try:
            asyncio.run(world_ops.dispatch_async(
                cfg, ScriptedLLM(["随便展开"]), "event.expand",
                {"instance_id": line.instance_id, "timeline_id": line.timeline_id,
                 "claim_id": str(claim["id"]), "character_id": "cc-碑拓者"},
                store=line.store,
            ))
        except Exception as exc:
            denied = str(exc)
        if not denied or "没有这条记载" not in denied:
            raise Fail(f"未持有记载的角色也能展开（物化≠获知）：denied={denied!r}",
                       where="isekai_core/world/ops.py:744 _expand_claim")
        text_before = str(claim["text"])
        llm = ScriptedLLM([f"{text_before}（另一本抄本记法略异）"])
        first = asyncio.run(world_ops.dispatch_async(
            cfg, llm, "event.expand",
            {"instance_id": line.instance_id, "timeline_id": line.timeline_id,
             "claim_id": str(claim["id"]), "character_id": "cc-堤禾", "question": "还写了什么？"},
            store=line.store,
        ))
        derived = str(first.get("derived") or "")
        if not derived or first.get("calls") != 1:
            raise Fail(f"展开没有产出派生记录：{first}")
        now = next(row for row in line.store.claim_list(line.instance_id, line.timeline_id)
                   if str(row["id"]) == str(claim["id"]))
        if str(now["text"]) != text_before:
            raise Fail("展开改写了原记载", where="isekai_core/store.py:2166 claim_put")
        row = next(row for row in line.store.claim_list(line.instance_id, line.timeline_id) if str(row["id"]) == derived)
        if str(row["derived_from"]) != str(claim["id"]):
            raise Fail("派生记录没有关联原条目")
        again = asyncio.run(world_ops.dispatch_async(
            cfg, ScriptedLLM(["另一个问题的答案"]), "event.expand",
            {"instance_id": line.instance_id, "timeline_id": line.timeline_id,
             "claim_id": str(claim["id"]), "character_id": "cc-堤禾", "question": "换个问法呢？"},
            store=line.store,
        ))
        if not again.get("reused") or str(again.get("derived")) != derived or again.get("calls") != 0:
            raise Fail(f"同一传本没有复用已采纳的展开：{again}")
        _ = others
        return (
            f"未持有者被拒（{denied[:24]}…）；持有者展开得派生记录 {derived}（derived_from={claim['id']}），"
            f"原记载文本不变；换问法重问 → reused=True、calls=0、派生记录不变"
        )
    finally:
        line.close()


@check("B9b", "附录B#9 展开不得临场补出未确定的事实（依据不足就丢弃）")
def b9b() -> str:
    package = example_package()
    line = Line(seed="seed-expand2", package=package)
    try:
        cfg = load_config(line.dir / "cfg")
        line.activate()
        line.advance(days=3)
        claim = next(
            row for row in line.store.claim_list(line.instance_id, line.timeline_id)
            if str(row["source_id"]) == "src-1" and line.store.knowledge_holders(
                line.instance_id, line.timeline_id, str(row["id"])
            )
        )
        invented = "另有 3 人知情，其中一个是堤南史馆的文书。"
        before = len(line.store.claim_list(line.instance_id, line.timeline_id))
        result = asyncio.run(world_ops.dispatch_async(
            cfg, ScriptedLLM([invented]), "event.expand",
            {"instance_id": line.instance_id, "timeline_id": line.timeline_id,
             "claim_id": str(claim["id"]), "character_id": "cc-堤禾", "question": "谁干的？"},
            store=line.store,
        ))
        after = len(line.store.claim_list(line.instance_id, line.timeline_id))
        if after != before:
            raise Fail("补出死亡原因 / 责任人的展开被写进了派生记录", where="isekai_core/runtime/render.py:89 expansion_is_grounded")
        if "丢弃" not in str(result.get("note")):
            raise Fail(f"非法展开没有留下丢弃说明：{result}")
        if not render.expansion_is_grounded("追问者得不到答案，只记下「不可考」。", claim):
            raise Fail("「不可考」的合规展开被误拒")
        return f"补出责任人 / 人数的展开（{invented[:14]}…）被丢弃且未落盘（说法数仍 {after} 条），合规的「不可考」表述可以通过"
    finally:
        line.close()


@check("B10", "附录B#10 「尚未生成」与「缺载」的区分、缺载不等于删改证据")
def b10() -> Any:
    hits: list[str] = []
    for path in sorted((ROOT / "isekai_core").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for keyword in ("缺载", "尚未生成", "未展开"):
            if keyword in text:
                hits.append(f"{path.relative_to(ROOT)}:{keyword}")
    return (
        "DEFERRED",
        "实现里没有「已确认缺载 / 尚未生成 / 已展开」的区分状态：全仓 isekai_core 下 "
        f"「缺载 / 尚未生成 / 未展开」命中 {len(hits)} 处；史料覆盖区间只有 historiography.coverage 的静态校验"
        "（isekai_core/world/validate.py:501），运行期没有缺载判定与「缺载≠删改」的推导。"
        "SPEC 自列为待模块设计项（docs/EVENT_ENGINE_SPEC.md §十一「惰性展开的触发条件、预算与派生表示的存储方式」）"
        "与阶段 6 深化推导（docs/EVENT_ENGINE_SPEC.md §十）",
    )


@check("B11a", "附录B#11 效果目标指向未登记对象 → 校验失败；传闻里的未登记名字仍可获知")
def b11a() -> str:
    from isekai_core.world.validate import validate_package as vp

    package = example_package()
    package["events"]["families"][0]["templates"][0]["effects"][0]["target"] = "src-99"
    errors = [item for item in vp(package) if "src-99" in item]
    if not errors:
        raise Fail("未登记的效果目标没有被校验拦下", where="isekai_core/world/validate.py:647")
    line = Line(seed="seed-unreg")
    try:
        line.activate()
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        name = "未登记之「何九」"
        line.inject(
            summary="传闻",
            claims=[{"id": "cl-audit-name", "text": f"听说{name}在北堤出现过", "source_id": "src-1",
                     "earliest_world": watermark + 1}],
        )
        line.advance(days=2)
        learned = [row for row in line.knowledge("cc-堤禾") if name in str(row["text"])]
        if not learned:
            raise Fail("含未登记名字的传闻没有被获知 / 讨论")
        effects = [
            row for row in line.raw_effects()
            if name in str(row.get("value") or "") or name in str(row.get("recovery") or "")
        ]
        if effects:
            raise Fail(f"未登记名字取得了事实资格：{effects}")
        facts = [row for row in line.event_rows() if name in str(row["summary"]) and str(row["source"]) != "engine"]
        if facts:
            raise Fail(f"未登记名字进了运行期事件：{facts}")
        return (
            f"包里把效果目标改成 src-99 → 校验报错（{errors[0][:46]}…）；含「{name}」的传闻被角色获知"
            f"（{learned[0]['id']}），但没有产生任何效果或事实条目"
        )
    finally:
        line.close()


@check("12a", "附录B#12 持续后果进入后续生活安排与角色处境（改写计划属禁止项）")
def b12a() -> str:
    line = Line(seed="seed-effect")
    try:
        line.activate()
        line.advance(days=2)
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        plan_before = {
            int(row["day_index"]): str(row["windows"])
            for row in line.store._conn.execute(
                "SELECT * FROM life_plan WHERE timeline_id=?", (line.timeline_id,)
            ).fetchall()
        }
        line.inject(
            summary="堤道封闭一日",
            effects=[{"kind": "activity_constraint", "target": "rl-1", "expiry": "until_cleared", "family": "ef-1"}],
        )
        snapshot = line.service.character_snapshot(
            line.instance_id, line.timeline_id, "cc-堤禾", world_seconds=watermark
        )
        if not snapshot["effects"]:
            raise Fail("仍有效的后果没有进入角色状态", where="isekai_core/runtime/service.py:2090 character_snapshot")
        if "受影响的后果" not in str(snapshot["current_activity"]):
            raise Fail(f"当前处境没有体现后果：{snapshot['current_activity']!r}")
        line.advance(days=3)
        raw = [row for row in line.raw_effects() if str(row["kind"]) == "activity_constraint"]
        if not raw or int(raw[0]["active"]) != 1:
            raise Fail("后果在后续日子里消失了（until_cleared 应持续到被解除）")
        noted = [row for row in line.experiences("cc-堤禾") if "受影响的后果" in str(row["summary"])]
        if not noted:
            raise Fail("后果没有进入后续经历 / 生活安排", where="isekai_core/runtime/service.py:1566 _collect_batch")
        plan_after = {
            int(row["day_index"]): str(row["windows"])
            for row in line.store._conn.execute(
                "SELECT * FROM life_plan WHERE timeline_id=?", (line.timeline_id,)
            ).fetchall()
        }
        rewritten = {day: (windows, plan_after.get(day)) for day, windows in plan_before.items() if plan_after.get(day) != windows}
        if rewritten:
            raise Fail(f"已固化的计划被后果改写：{list(rewritten)[:3]}")
        return (
            f"后果（activity_constraint rl-1，until_cleared）进入角色处境（current_activity 带「受影响的后果」）与 "
            f"{len(noted)} 条后续经历；3 天后仍 active=1；计划逐日不变"
        )
    finally:
        line.close()


@check("12b", "附录B#12 三类失效方式可区分：随诱因结束 / 持续到被解除 / 有条件的自然恢复")
def b12b() -> str:
    line = Line(seed="seed-expiry")
    try:
        line.activate()
        line.advance(days=10)
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        rows = line.raw_effects()
        kinds = {str(row["expiry"]) for row in rows}
        if not {"with_cause", "natural_recovery", "until_cleared"} <= kinds:
            raise Fail(f"没有同时出现三类失效方式：{kinds}", where="isekai_core/runtime/events.py:174 effect_rows")
        moments: dict[str, list[int]] = {}
        for row in line.event_rows():
            family = str(row["family"] or "")
            if family:
                moments.setdefault(family, []).append(int(row["world_seconds"]))
        problems = []
        for row in rows:
            expiry, active, started = str(row["expiry"]), int(row["active"]), int(row["from_world"])
            if expiry == "with_cause" and started + DAY <= watermark and (active != 0 or row["cleared_at"] is None):
                problems.append(f"with_cause 未随诱因结束：{row['id']}")
            if expiry == "natural_recovery":
                family = str(row["family"] or "")
                seen = max(0, watermark - DAY)
                later = [item for item in moments.get(family, []) if started < item <= seen]
                if later and active != 0:
                    problems.append(f"natural_recovery 有依据却未解除：{row['id']}")
                if not later and active != 1:
                    problems.append(f"natural_recovery 无依据却被解除：{row['id']}")
            if expiry == "until_cleared" and active != 1:
                problems.append(f"until_cleared 被自动清掉：{row['id']}")
        if problems:
            raise Fail("；".join(problems[:3]), where="isekai_core/runtime/service.py:1955 _propagate_and_clear")
        counts = {kind: sum(1 for row in rows if str(row["expiry"]) == kind) for kind in sorted(kinds)}
        cleared = sum(1 for row in rows if int(row["active"]) == 0)
        return (
            f"10 天后 {len(rows)} 条后果：{counts}；已解除 {cleared} 条（with_cause 留 cleared_at 时刻，"
            f"natural_recovery 只在同族后续事件被观察到时解除，until_cleared 一律保留）"
        )
    finally:
        line.close()


@check("12c", "附录B#12 澄清只改认知，不自动撤销已施行的措施")
def b12c() -> str:
    line = Line(seed="seed-clarify")
    try:
        line.activate()
        line.advance(days=3)
        office_before = {
            row["office_id"]: dict(row) for row in line.store.institution_list(line.instance_id, line.timeline_id)
        }
        if not str(office_before["off-2"]["holder"]):
            raise Fail("前提不成立：还没有已施行的制度措施")
        effects_before_keys = {
            (str(row["id"]), str(row["target"]), str(row["value"])) for row in line.raw_effects()
        }
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        line.inject(
            summary="澄清：先前信报所述措施有误",
            claims=[{"id": "cl-audit-retract", "text": "澄清：先前信报所述措施有误，实未施行",
                     "source_id": "src-1", "earliest_world": watermark + 1}],
            ident="ev-audit-retract",
        )
        line.advance(days=2)
        office_after = {
            row["office_id"]: dict(row) for row in line.store.institution_list(line.instance_id, line.timeline_id)
        }
        if office_after["off-2"] != office_before["off-2"]:
            raise Fail(
                f"澄清自动撤销了措施：{office_before['off-2']['holder']} → {office_after['off-2']['holder']}",
                where="isekai_core/runtime/service.py:1955 _propagate_and_clear（澄清没有撤销措施的分支，这里验的是「确实没有」）",
            )
        effects_after = {
            (str(row["id"]), str(row["target"]), str(row["value"])) for row in line.raw_effects()
        }
        if not effects_before_keys <= effects_after:
            raise Fail(f"澄清抹掉了已生效的后果：{sorted(effects_before_keys - effects_after)[:2]}")
        known = [row for row in line.knowledge("cc-堤禾") if "澄清" in str(row["text"])]
        if not known:
            raise Fail("澄清没有进入获知者的认知")
        return (
            f"措施（off-2 在任者={office_after['off-2']['holder']}，来源 {office_after['off-2']['source']}）与澄清前 "
            f"{len(effects_before_keys)} 条后果行逐行不变（解除仍只按各自声明的失效条件发生）；"
            f"澄清作为新说法进入获知（{len(known)} 条）"
        )
    finally:
        line.close()


@check("13a", "附录B#13 空缺期间事务按声明规则分别延续 / 暂停，未声明的不给默认答案")
def b13a() -> str:
    line = Line(seed="seed-vacancy")
    try:
        office = {row["office_id"]: row for row in line.store.institution_list(line.instance_id, line.timeline_id)}
        vacant = office["off-2"]
        if str(vacant["holder"]):
            raise Fail("前提不成立：off-2 不是空缺")
        cases = {
            "日常堤务": institutions.matter_status(vacant, "日常堤务"),
            "通行牌发放": institutions.matter_status(vacant, "通行牌发放"),
            "发放盐引": institutions.matter_status(vacant, "发放盐引"),
            "有在任者时": institutions.matter_status(office["off-1"], "通行牌发放"),
        }
        expect = {
            "日常堤务": institutions.CONTINUES,
            "通行牌发放": institutions.SUSPENDED,
            "发放盐引": None,
            "有在任者时": institutions.ACTIVE,
        }
        if cases != expect:
            raise Fail(f"空缺判定与声明不符：{cases}", where="isekai_core/runtime/institutions.py:70 matter_status")
        snapshot = line.service.character_snapshot(
            line.instance_id, line.timeline_id, "cc-堤禾", world_seconds=line.watermark()
        )
        row = next(
            (item for item in snapshot["institutions"] if "守碑人" in str(item["name"])), None
        )
        if row is None or "空缺" not in str(row["value"]) or "照旧" not in str(row["note"]):
            raise Fail(f"角色视角没有可判定的空缺说明：{row}")
        return (
            f"空缺职位：日常堤务={cases['日常堤务']}、通行牌发放={cases['通行牌发放']}、未声明的发放盐引={cases['发放盐引']}"
            f"（不默认照旧也不默认停摆）；有在任者时={cases['有在任者时']}；角色视角读到「{row['value']}」（{row['note']}）"
        )
    finally:
        line.close()


@check("13b", "附录B#13 制度变化只在声明范围内，且带来源与发生时刻")
def b13b() -> str:
    line = Line(seed="seed-office")
    try:
        line.activate()
        line.advance(days=6)
        office = {row["office_id"]: dict(row) for row in line.store.institution_list(line.instance_id, line.timeline_id)}
        row = office["off-2"]
        if str(row["holder"]) != "en-1":
            raise Fail(
                "声明范围内的制度变化（引擎事件里的 institution_state）没有落到制度状态",
                where="isekai_core/runtime/service.py:1675 _institution_rows",
            )
        source = str(row["source"])
        event = next((item for item in line.event_rows() if str(item["id"]) == source), None)
        if event is None or int(row["updated_world"]) < int(event["world_seconds"]):
            raise Fail(f"变化没有来源 / 发生时刻：source={source} updated={row['updated_world']}")
        if not row["continues"] or not row["suspended"]:
            raise Fail("制度状态行没带声明的事务清单（延续 / 暂停不可判定）")
        package = example_package()
        package["events"]["families"][0]["templates"][0]["effects"][3]["target"] = "off-9"
        if not [item for item in validate_package(package) if "未声明的职位" in item]:
            raise Fail("校验期没有拦住未声明的职位")
        rows = institutions.office_rows(example_package(), instance_id="i", timeline_id="t", world_seconds=0)
        applied, _ = institutions.apply_effects(
            rows, [], [{"kind": "institution_state", "target": "off-9", "value": "en-2", "event_id": "ev-x"}],
            example_package(), world_seconds=1,
        )
        if any(str(item["office_id"]) == "off-9" for item in applied):
            raise Fail("未声明的职位被凭空创建")
        return (
            f"off-2 由空缺承接为 en-1：来源={source}、updated_world={row['updated_world']}（事件刻 {event['world_seconds']}）；"
            f"状态行带照旧 {row['continues']} / 暂停 {row['suspended']}；未声明的 off-9 校验期被拒且运行期不落状态"
        )
    finally:
        line.close()


@check("13c", "附录B#13 制度变化不让所有角色自动知晓（按认知路径）")
def b13c() -> str:
    package = example_package()
    holder = example_card(package, name="堤禾")
    late = example_card(package, name="后到者")
    line = Line(seed="seed-office-know", cards=[holder], package=package)
    try:
        line.activate()
        line.advance(days=4)
        office = {row["office_id"]: dict(row) for row in line.store.institution_list(line.instance_id, line.timeline_id)}
        source = str(office["off-2"]["source"])
        if not source:
            raise Fail("前提不成立：制度还没有发生过变化")
        line.service.add_character(
            line.instance_id, line.timeline_id, late, now_real=line.now_real[line.timeline_id],
            joined_world=int(office["off-2"]["from_world"]) + 1, note="变化之后才到本线",
        )
        late_id = str(late["meta"]["card_id"])
        world_now = line.watermark()

        def _view(character_id: str) -> dict[str, Any]:
            snapshot = line.service.character_snapshot(
                line.instance_id, line.timeline_id, character_id, world_seconds=world_now
            )
            return next(item for item in snapshot["institutions"] if "守碑人" in str(item["name"]))

        holder_view, late_view = _view("cc-堤禾"), _view(late_id)
        if "堤禾在任" not in str(holder_view["value"]):
            raise Fail(f"知道该变化的角色没有经认知路径看到它：{holder_view}")
        if "空缺" not in str(late_view["value"]):
            raise Fail(f"后到的角色凭空知道了变化：{late_view}")
        late_knows = [row for row in line.knowledge(late_id) if str(row["target"]) == source]
        if late_knows:
            raise Fail("后到的角色拿到了该变化的获知记录")
        return (
            f"知道该事件的角色看到「{holder_view['value']}」（since {holder_view['since']}）；"
            f"变化之后才补入本线的角色仍按声明的初始状态看到「{late_view['value']}」，获知记录 0 条"
        )
    finally:
        line.close()


@check("13d", "附录B#13 职位持有者离任 / 死亡后，空缺与继任按声明规则推进")
def b13d() -> str:
    moment = DAY * 1500
    package = example_package(moment=moment)
    # 让职位在任者与角色卡是同一个登记对象：包内加一个 id 与卡片一致的实体，并把它设为 off-1 在任者
    package["entities"].append({"id": "cc-堤禾", "kind": "person", "name": "堤禾", "race_id": "rc-1", "born": 0, "died": None})
    package["world"]["institutions"][0]["offices"][0]["holder"] = "cc-堤禾"
    card = example_card(package)
    card["identity"]["died"] = moment + 2 * DAY  # 卡片固化的死亡：她确实会在这一刻身故
    line = Line(seed="seed-death-office", moment=moment, package=package, cards=[card])
    try:
        line.activate()
        line.advance(days=5)
        deaths = [row for row in line.event_rows() if str(row["template"]).startswith("death:")]
        if not deaths:
            raise Fail("前提不成立：没有产生身故事件（用 entity 声明的 died 重跑）",
                       repro="card['identity']['died']=moment+2*DAY → advance(5 天)")
        office = {row["office_id"]: dict(row) for row in line.store.institution_list(line.instance_id, line.timeline_id)}
        row = office["off-1"]
        if str(row["holder"]) != "cc-堤禾":
            raise Fail(f"职位在任者被无声改动：{row['holder']!r}")
        status = institutions.matter_status(row, "通行牌发放")
        raise Fail(
            f"身故事件已发生（{deaths[0]['id']} @ {deaths[0]['world_seconds']}），但职位状态不被推出：off-1 在任者仍为 "
            f"{row['holder']!r}（updated_world={row['updated_world']}），matter_status(通行牌发放)={status} → "
            f"声明的 successor / vacancy_policy（堤长议会 succession：身故或去职时推举接任，空缺期间日常堤务照旧、通行牌暂停发放）"
            f"永不启用，也没有继任或空缺记录",
            repro=(
                "package: entities += {id:'cc-堤禾'}，offices[off-1].holder='cc-堤禾'；"
                "card['identity']['died']=moment+2*DAY；advance(5 天) → 观察 institution_state.off-1 与 matter_status"
            ),
            where=(
                "isekai_core/runtime/service.py:1903 _death_rows（只登记角色卡身故，不触达制度状态）；"
                "isekai_core/runtime/institutions.py:84 apply_effects（只沿事件效果改 holder）"
            ),
        )
    finally:
        line.close()


@check("B14", "附录B#14 惯例形态可变、日期与预算不变、不扩大任何角色的已知范围")
def b14a() -> str:
    from isekai_core.world.validate import change_allowed

    package = example_package()
    alternative = str((package["world"]["customs"][0]["forms"] or [])[1])
    ok, _ = change_allowed(package, kind="custom_state", target="cus-1", value=alternative)
    denied, reason = change_allowed(package, kind="custom_state", target="cus-1", value="随便编个做法")
    if not ok or denied:
        raise Fail(f"惯例允许范围判定不对：ok={ok} denied={denied}/{reason}")
    # 把「按声明范围改换惯例做法」写进事件模板：变化仍只由合法事件效果产生
    package["events"]["families"][0]["templates"][0]["effects"].append(
        {"kind": "custom_state", "target": "cus-1", "value": alternative, "expiry": "until_cleared"}
    )
    if validate_package(package):
        raise Fail(f"声明内的惯例效果被误判为非法：{validate_package(package)[:2]}")
    calendar = calendar_from_package(package)
    before_festival = events.fixed_events(package, day_index=1562, calendar=calendar)
    before_budget = events.daily_budget("seed-fixed", "0.1", 1562, str(package["events"]["density"]))

    holder = example_card(package, name="堤禾")
    late = example_card(package, name="后到者")
    line = Line(seed="seed-custom", cards=[holder], package=package)
    try:
        line.activate()
        line.advance(days=6)
        customs = {row["custom_id"]: dict(row) for row in line.store.custom_list(line.instance_id, line.timeline_id)}
        if customs["cus-1"]["form"] != alternative:
            raise Fail(
                "声明范围内的惯例变化没有生效", where="isekai_core/runtime/service.py:1675 _institution_rows"
            )
        source = str(customs["cus-1"]["source"])
        event = next((item for item in line.event_rows() if str(item["id"]) == source), None)
        if event is None:
            raise Fail(f"惯例变化没有来源事件：source={source}")
        if events.fixed_events(package, day_index=1562, calendar=calendar) != before_festival:
            raise Fail("惯例变化改动了固定节日")
        if events.daily_budget("seed-fixed", "0.1", 1562, str(package["events"]["density"])) != before_budget:
            raise Fail("惯例变化改动了当日预算")
        # 变更不改写旧记录：变化前后既有事件 / 史料逐行一致
        history = {str(row["id"]): dict(row) for row in line.event_rows() if row["source"] == "backfill"}
        if not history:
            raise Fail("没有可对照的历史记录")
        line.service.add_character(
            line.instance_id, line.timeline_id, late, now_real=line.now_real[line.timeline_id],
            joined_world=int(customs["cus-1"]["from_world"]) + 1, note="变更之后才到本线",
        )
        late_id = str(late["meta"]["card_id"])
        world_now = line.watermark()

        def _view(character_id: str) -> dict[str, Any]:
            snapshot = line.service.character_snapshot(
                line.instance_id, line.timeline_id, character_id, world_seconds=world_now
            )
            return next(item for item in snapshot["institutions"] if "退潮祭" in str(item["name"]))

        holder_view, late_view = _view("cc-堤禾"), _view(late_id)
        if alternative not in str(holder_view["value"]):
            raise Fail(f"知道该变化的角色看不到新做法：{holder_view}")
        if alternative in str(late_view["value"]) or "大退潮首日在滩口设盐与旧堤砖" not in str(late_view["value"]):
            raise Fail(f"后到的角色凭空知道了新做法：{late_view}")
        after_history = {str(row["id"]): dict(row) for row in line.event_rows() if row["source"] == "backfill"}
        if after_history != history:
            raise Fail("惯例变化改写了旧记录")
        return (
            f"现行做法改为声明内的备选（来源事件 {source}）；固定节日 {before_festival[0]['summary']} 与当日预算 "
            f"{before_budget} 不变；{len(history)} 条旧记录逐行不变；持事件者看到新做法，变更后才补入的角色仍按声明初始做法"
        )
    finally:
        line.close()


@check("B15", "附录B#15 代表性因果链：诱因 → 持续后果 → 合法响应 → 解除 / 新常态")
def b15() -> str:
    line = Line(seed="seed-chain")
    try:
        line.activate()
        line.advance(days=20)
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        induces = [row for row in _engine_rows(line) if str(row["effects"]) not in ("[]", "")]
        if not induces:
            raise Fail("没有带事实效果的诱因事件")
        first = induces[0]
        effects = line.raw_effects()
        persistent = [
            row for row in effects
            if row["event_id"] == first["id"] and int(row["from_world"]) + DAY <= watermark and int(row["active"]) == 1
        ]
        responses = [row for row in line.event_rows() if str(row["source"]) == "character_action"]
        cleared = [row for row in effects if int(row["active"]) == 0 and row["cleared_at"] is not None]
        new_normal = [row for row in effects if int(row["active"]) == 1 and row["expiry"] == "until_cleared"]
        if not persistent:
            raise Fail(f"诱因 {first['id']} 的后果没有跨日持续", where="isekai_core/runtime/events.py:159 effect_rows")
        if not responses:
            raise Fail("没有出现后续合法响应（角色行动事件）", where="isekai_core/runtime/service.py:1711 _revise_intents")
        if not cleared and not new_normal:
            raise Fail("后果既没有被合法解除，也没有形成持续的新常态")
        return (
            f"诱因 {first['id']}（{first['summary'][:14]}）→ 后果跨 1 日以上仍 active（{len(persistent)} 条）；"
            f"响应 {responses[0]['id']}（source={responses[0]['source']}，{responses[0]['summary'][:12]}…）；"
            f"解除留档 {len(cleared)} 条、持续新常态 {len(new_normal)} 条"
        )
    finally:
        line.close()


@check("B16", "附录B#16 身体类 / 未声明效果只在闭集内：临场数值系统被拒")
def b16a() -> str:
    package = example_package()
    package["events"]["families"][0]["templates"][0]["effects"] = [
        {"kind": "health_pool", "target": "rl-1", "expiry": "with_cause"}
    ]
    errors = [item for item in validate_package(package) if "未支持的效果类型" in item]
    if not errors:
        raise Fail("未声明效果类型没有被创建前校验拦下", where="isekai_core/world/validate.py:650")
    rows = events.effect_rows(
        {"effects": [{"kind": "health_pool", "target": "rl-1", "expiry": "with_cause"}]},
        instance_id="in-x", timeline_id="tl-x", event_ident="ev-x", world_seconds=0,
    )
    if rows:
        raise Fail("引擎层放行了闭集外的效果", where="isekai_core/runtime/events.py:172 effect_rows")
    line = Line(seed="seed-closed")
    try:
        line.activate()
        line.advance(days=2)
        snapshot = line.service.character_snapshot(
            line.instance_id, line.timeline_id, "cc-堤禾", world_seconds=line.watermark()
        )
        numeric = [
            key for key, value in snapshot.items()
            if isinstance(value, (int, float)) and key not in ("plan",)
        ]
        kinds = set(SUPPORTED_EFFECTS)
        if any("health" in kind or "hunger" in kind or "stamina" in kind for kind in kinds):
            raise Fail(f"闭集里混进了通用数值系统：{kinds}")
        if numeric:
            raise Fail(f"角色视图出现数值面板字段：{numeric}")
        return (
            f"health_pool 在校验期被拒（{errors[0][:40]}…）且引擎层再兜一道；闭集恰为 {len(kinds)} 类"
            f"（{', '.join(sorted(kinds))}）；角色状态视图没有数值面板字段"
        )
    finally:
        line.close()


@check("B17a", "附录B#17 预约事件在目标时刻前不产生效果 / 经历 / 获知")
def b17a() -> str:
    line = Line(seed="seed-pending")
    try:
        line.activate()
        line.advance(days=2)
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        draft = asyncio.run(line.service.draft_user_event(
            line.instance_id, line.timeline_id, intent="七日后堤务吏换人",
            payload={
                "intent": "七日后堤务吏换人", "when": "scheduled", "at_world": watermark + 7 * DAY,
                "effects": [{"kind": "institution_state", "target": "rl-1", "expiry": "until_cleared"}],
                "claims": [{"text": "七日后堤务吏换人", "source_id": "src-1", "audience": "public"}],
            },
        ))
        new_line = str(line.service.confirm_user_event(line.instance_id, draft["draft"]["draft_id"], name="预约线")["timeline_id"])
        if [row for row in line.event_rows(new_line) if row["source"] == "user"]:
            raise Fail("到点前就注入了事件")
        if [row for row in line.raw_effects(new_line) if int(row["from_world"]) > watermark]:
            raise Fail("到点前就施加了效果")
        if [row for row in line.knowledge("cc-堤禾", new_line) if "堤务吏换人" in str(row["text"])]:
            raise Fail("到点前就产生获知")
        if line.store.pending_events_due(line.instance_id, new_line, until=10**15) == []:
            raise Fail("没有登记待执行状态")
        return f"预约至 {watermark + 7 * DAY}：事件 0 条、效果 0 条、获知 0 条，待执行记录在册（新线默认冻结）"
    finally:
        line.close()


@check("17b", "附录B#17 到点复核：条件成立则施加，条件失效记取消而不是强行执行")
def b17b() -> str:
    line = Line(seed="seed-due")
    try:
        line.activate()
        line.advance(days=2)
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        draft = asyncio.run(line.service.draft_user_event(
            line.instance_id, line.timeline_id, intent="三日后堤务吏换人",
            payload={
                "intent": "三日后堤务吏换人", "when": "scheduled", "at_world": watermark + 3 * DAY,
                "effects": [{"kind": "institution_state", "target": "rl-1", "expiry": "until_cleared"}],
                "claims": [{"text": "三日后堤务吏换人", "source_id": "src-1", "audience": "public"}],
            },
        ))
        new_line = str(line.service.confirm_user_event(line.instance_id, draft["draft"]["draft_id"], name="到点线")["timeline_id"])
        line.activate(new_line)
        line.advance(days=4, tl=new_line)
        landed = [row for row in line.event_rows(new_line) if row["source"] == "user"]
        if not landed or int(landed[0]["world_seconds"]) != watermark + 3 * DAY:
            raise Fail(f"到点没有在预约时刻落事件：{[(r['id'], r['world_seconds']) for r in landed]}")
        if line.store.pending_events_due(line.instance_id, new_line, until=10**15) != []:
            raise Fail("到点后仍是待执行")
        row = next(
            item for item in line.store._conn.execute(
                "SELECT * FROM pending_event WHERE timeline_id=?", (new_line,)
            ).fetchall()
        )
        if str(row["state"]) != "applied":
            raise Fail(f"待执行状态不是 applied：{dict(row)['state']}")
        # 条件失效：给一条指向本线之外对象（未登记目标）的待执行记录
        line.store.pending_event_add({
            "id": "pe-audit-stale", "instance_id": line.instance_id, "timeline_id": new_line,
            "at_world": line.watermark(new_line) + DAY,
            "payload": json.dumps({
                "intent": "越界改动", "when": "scheduled", "at_world": line.watermark(new_line) + DAY,
                "effects": [{"kind": "institution_state", "target": "cc-outsider", "expiry": "until_cleared"}],
                "claims": [],
            }, ensure_ascii=False),
            "state": "pending", "note": "", "created_world": line.watermark(new_line), "created_at": 1.7e9,
        })
        line.advance(days=2, tl=new_line)
        stale = line.store._conn.execute(
            "SELECT * FROM pending_event WHERE id='pe-audit-stale'"
        ).fetchone()
        if stale is None or str(stale["state"]) != "cancelled":
            raise Fail(f"条件失效的预约没有被记为取消：{dict(stale) if stale else None}",
                       where="isekai_core/runtime/service.py:433 apply_due_pending_events")
        if [row for row in line.event_rows(new_line) if "越界改动" in str(row["summary"])]:
            raise Fail("条件失效却强行执行")
        return (
            f"到点：事件恰落在 {watermark + 3 * DAY}、待执行状态 applied；条件失效（目标非本线登记对象）→ "
            f"state=cancelled（note={stale['note'][:18]}…）且不落事件"
        )
    finally:
        line.close()


@check("17c", "附录B#17 重启与重试不重复施加效果")
def b17c() -> str:
    line = Line(seed="seed-restart")
    try:
        line.activate()
        line.advance(days=2)
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        draft = asyncio.run(line.service.draft_user_event(
            line.instance_id, line.timeline_id, intent="两日后堤务吏换人",
            payload={
                "intent": "两日后堤务吏换人", "when": "scheduled", "at_world": watermark + 2 * DAY,
                "effects": [{"kind": "institution_state", "target": "rl-1", "expiry": "until_cleared"}],
                "claims": [{"text": "两日后堤务吏换人", "source_id": "src-1", "audience": "public"}],
            },
        ))
        new_line = str(line.service.confirm_user_event(line.instance_id, draft["draft"]["draft_id"], name="重试线")["timeline_id"])
        line.activate(new_line)
        line.advance(days=3, tl=new_line)
        events_after = [row for row in line.event_rows(new_line) if row["source"] == "user"]
        effects_after = [row for row in line.raw_effects(new_line) if str(row["event_id"]) == str(events_after[0]["id"])]
        line.advance(days=3, tl=new_line)
        line.service = RuntimeService(line.store, **SERVICE_KW)  # 模拟重启：换一个服务实例
        line.advance(days=3, tl=new_line)
        events_again = [row for row in line.event_rows(new_line) if row["source"] == "user"]
        effects_again = [row for row in line.raw_effects(new_line) if str(row["event_id"]) == str(events_again[0]["id"])]
        if len(events_again) != 1:
            raise Fail(f"重启后重复注入事件：{len(events_again)} 条")
        if len(effects_again) != len(effects_after) or len(effects_again) == 0:
            raise Fail(f"重启后重复施加效果：{len(effects_after)} → {len(effects_again)}",
                       where="isekai_core/runtime/service.py:433 apply_due_pending_events")
        return (
            f"到期施加后事件 1 条、效果 {len(effects_again)} 条；再推进 3 天 + 换服务实例（模拟重启）后再推进 3 天，"
            f"事件与效果数量不变、状态未回退"
        )
    finally:
        line.close()


@check("17d", "附录B#17 回滚撤销待执行记录及其后果")
def b17d() -> str:
    line = Line(seed="seed-rollback")
    try:
        line.activate()
        line.advance(days=2)
        commit = line.service.commit(line.instance_id, line.timeline_id, note="预约前")
        watermark = int(line.store.clock_get(line.timeline_id)["processed_world"])
        draft = asyncio.run(line.service.draft_user_event(
            line.instance_id, line.timeline_id, intent="五日后堤务吏换人",
            payload={
                "intent": "五日后堤务吏换人", "when": "scheduled", "at_world": watermark + 5 * DAY,
                "effects": [{"kind": "institution_state", "target": "rl-1", "expiry": "until_cleared"}],
                "claims": [{"text": "五日后堤务吏换人", "source_id": "src-1", "audience": "public"}],
            },
        ))
        new_line = str(line.service.confirm_user_event(line.instance_id, draft["draft"]["draft_id"], name="预约线2")["timeline_id"])
        base = str(line.store.commit_list(line.instance_id, new_line)[0]["id"])
        if line.store.pending_events_due(line.instance_id, new_line, until=10**15) == []:
            raise Fail("前提不成立：没有待执行记录")
        line.service.rollback(line.instance_id, new_line, commit_id=base, now_real=T0 + 5 * DAY)
        if line.store.pending_events_due(line.instance_id, new_line, until=10**15) != []:
            raise Fail("回滚后仍残留待执行记录", where="isekai_core/runtime/service.py:566 rollback")
        pending_rows = line.store._conn.execute(
            "SELECT COUNT(*) AS n FROM pending_event WHERE timeline_id=?", (new_line,)
        ).fetchone()
        if int(pending_rows["n"]) != 0:
            raise Fail(f"回滚后待执行表仍有 {pending_rows['n']} 行")
        return f"回滚到预约前的提交：待执行记录 {pending_rows['n']} 行、待执行队列为空（未生效的预约不残留）"
    finally:
        line.close()


@check("3.2", "正文§3.2 迟到 / 回滚后的生成结果不得写回（世代校验）")
def s32() -> str:
    line = Line(seed="seed-gen")
    try:
        line.activate()
        line.advance(days=2)
        row = line.store.clock_get(line.timeline_id)
        before = _facts(line)
        accepted = line.store.apply_runtime_batch(
            timeline_id=line.timeline_id,
            generation=int(row["generation"]) - 1,
            processed_world=int(row["processed_world"]),
            catching_up=False,
            events=[{
                "id": "ev-audit-late", "instance_id": line.instance_id, "timeline_id": line.timeline_id,
                "world_seconds": int(row["processed_world"]), "seq": 1, "kind": "world", "family": "ef-1",
                "template": "audit.late", "source": "engine", "summary": "迟到的结果", "detail": "迟到的结果",
                "text_source": "template", "effects": "[]", "share_value": 0, "importance": 0.5,
                "created_real": 0.0,
            }],
        )
        if accepted:
            raise Fail("过期世代的结果被采纳", where="isekai_core/store.py:1652 apply_runtime_batch")
        if _facts(line) != before:
            raise Fail("过期世代的结果改写了状态")
        forward = line.store.apply_runtime_batch(
            timeline_id=line.timeline_id, generation=int(row["generation"]),
            processed_world=int(row["processed_world"]) - 1, catching_up=False,
        )
        if forward:
            raise Fail("水位回退的批次被采纳")
        return "过期世代（generation-1）与水位回退的批次都返回 False 且逐行未落盘（并发命中同一槽只有一份产物被采纳）"
    finally:
        line.close()


def main() -> int:
    wanted = [item for item in sys.argv[1:] if not item.startswith("-")]
    selected = [
        entry for entry in CHECKS
        if not wanted or any(word.lower() in entry[0].lower() or word.lower() in entry[1].lower() for word in wanted)
    ]
    statuses: dict[str, int] = {"PASS": 0, "FAIL": 0, "DEFERRED": 0}
    fail_items: list[str] = []
    for cid, title, fn in selected:
        try:
            result = fn()
        except Fail as exc:
            status, evidence = "FAIL", f"{exc.why}"
            if exc.repro:
                evidence += f"｜最小复现：{exc.repro}"
            if exc.where:
                evidence += f"｜位置：{exc.where}"
        except Exception:
            tb = traceback.format_exc().strip().splitlines()
            last = next((line for line in reversed(tb) if "isekai_core" in line or "audit_event_engine" in line), tb[-1])
            status, evidence = "FAIL", f"检查自身异常：{tb[-1]}｜{last.strip()}"
        else:
            if isinstance(result, tuple) and result and result[0] == "DEFERRED":
                status, evidence = "DEFERRED", str(result[1])
            else:
                status, evidence = "PASS", str(result)
        statuses[status] += 1
        print(f"{status} [{cid}] {title} — {evidence}")
        if status == "FAIL":
            fail_items.append(f"{cid} {title}: {evidence}")
    total = len(selected)
    print(f"TOTAL {total} PASS {statuses['PASS']} FAIL {statuses['FAIL']} DEFERRED {statuses['DEFERRED']}")
    return 1 if statuses["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
