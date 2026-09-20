"""WORLD_RUNTIME_SPEC（世界运行层）第二轮行为级审计探针。

独立于 scripts/_audit_design.py（那是 DESIGN.md §7.1 的探针）：本文件只按
docs/WORLD_RUNTIME_SPEC.md 的正文条款（§2、§3、§4、§5、§6、§7、§8、§10、§11、
§13、§2.8）与附录 B（行为验收 26 条）逐条核对实现。

- 只读项目代码；不修改 isekai_core/ tests/ desktop/ docs/；
- 所有状态写在 tempfile.TemporaryDirectory() 里，不碰 data/isekai.db、config/config.yaml；
- 不联网、不调真实 LLM：一处用 isekai_core.llm.FakeLLM 起真核心（真 WS + 真 SQLite），
  其余场景直接跑 RuntimeService + Store（同一份实现代码，只是没有 WS 壳）；
- 用法：`.venv/Scripts/python.exe scripts/_audit2_wr.py`（可选 `--json` 只输出 JSON）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.client import MgmtClient, UmpClient  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402
from isekai_core.runtime import (  # noqa: E402
    budget as budget_mod,
    cognition,
    environment,
    events,
    intents as intents_mod,
    life,
    personality,
)
from isekai_core.runtime.clock import ClockState, RateCommand, natural_second, settle, target_world  # noqa: E402
from isekai_core.runtime.service import RuntimeService, RuntimeStateError  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.ump import UmpError, make  # noqa: E402
from isekai_core.world import ops  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402

log = logging.getLogger("isekai.wr_audit")

SPEC = "docs/WORLD_RUNTIME_SPEC.md"
RESULTS: list[dict[str, Any]] = []


def check(
    clause: str,
    expected: str,
    observed: str,
    status: str,
    evidence: str = "",
    code_ref: str = "",
) -> None:
    RESULTS.append(
        {
            "clause": clause,
            "status": status,
            "expected": expected,
            "observed": observed,
            "evidence": evidence,
            "code_ref": code_ref,
        }
    )
    print(f"[{status:8}] {clause} :: {observed[:150]}")


# --------------------------------------------------------------- 场景脚手架


@contextlib.contextmanager
def scenario(label: str, **world_kwargs: Any):
    """临时根目录 + 真 SQLite + 真 RuntimeService（只有模型被换掉 / 不涉及模型）。"""
    with tempfile.TemporaryDirectory(prefix=f"wr-audit-{label}-") as tmp:
        cfg = load_config(tmp)
        store = Store(cfg.paths.db)
        store.ensure_schema()
        kwargs: dict[str, Any] = {"autocommit_enabled": False}
        kwargs.update(world_kwargs)
        world = RuntimeService(store, **kwargs)
        try:
            yield SimpleNamespace(cfg=cfg, store=store, world=world, root=Path(tmp))
        finally:
            store.close()


def mk(
    env: Any,
    name: str = "灰潮纪",
    *,
    moment: int = DAY * 1500,
    cards: list[dict[str, Any]] | None = None,
    package: dict[str, Any] | None = None,
    seed: str | None = "audit-seed",
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    package = package if package is not None else example_package(name, moment=moment)
    cards = list(cards) if cards else [example_card(package)]
    info = create_instance(env.store, package, cards, seed=seed)
    timeline = env.store.timeline_list(info["id"])[0]
    env.world.ensure_instance(info["id"], now_real=time.time())
    return info, timeline["id"], str((cards[0].get("meta") or {}).get("card_id")), package


def say(env: Any, iid: str, tlid: str, cid: str, *, env_id: str, text: str, reply: str) -> str:
    """固化一轮对话（直接经 store 的会话接口，不经模型）。"""
    session = env.store.session_ensure(iid, tlid, cid)
    env.store.inbound_put(
        session_id=session["id"], channel_id="cli-dev", thread_id=f"t-{cid}",
        env_id=env_id, text=text, binding_version=1,
    )
    out = env.store.outbound_put(
        session_id=session["id"], message_id=f"m-{env_id}", reply_to=env_id, covers=[env_id],
        batches=[[reply]], target_channel="cli-dev", target_thread=f"t-{cid}",
        binding_version=1, binding_token="tok",
    )
    return str(out["message_id"])


def life_card(package: dict[str, Any], *, name: str, windows: list[dict[str, Any]], sleep: bool = True,
              channels: list[dict[str, Any]] | None = None, knowledge: list[dict[str, Any]] | None = None,
              intents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    card = example_card(package, name=name)
    card["meta"]["card_id"] = f"cc-{name}"
    card["life_template"] = {
        "sleep": sleep, "routine_note": "审计用日程", "windows": windows,
    }
    if channels is not None:
        card["channels"] = channels
    if knowledge is not None:
        card["initial_knowledge"] = knowledge
    if intents is not None:
        card["intents"] = intents
    return card


# --------------------------------------------------------------- §2 时钟与倍率


def c_clock_pure() -> None:
    # §2.3.2 严格晚于输入时刻的第一个自然整秒
    boundary = [natural_second(100.0), natural_second(100.4), natural_second(100.999)]
    check("§2.3 条2 生效点为严格晚于输入时刻的第一个自然整秒", "整秒输入归下一整秒：101/101/101",
          f"{boundary}", "PASS" if boundary == [101, 101, 101] else "FAIL",
          code_ref="isekai_core/runtime/clock.py:40-42")

    # §2.2 时钟倒拨不倒退（纯函数层）
    state = ClockState(base_real=1000.0, base_world=10, rate=1, high_water_real=1000.0)
    back = target_world(state, 900.0)
    ahead = target_world(state, 1100.0)
    check("§2.2 条7 时钟倒拨不允许世界倒退、重复执行", "倒拨时维持 10；追平后继续 +100",
          f"倒拨={back}，追平后={ahead}", "PASS" if (back, ahead) == (10, 110) else "FAIL",
          code_ref="isekai_core/runtime/clock.py:45-48")

    # §2.3 条4/5 分段累计 + 调度迟到仍在原定整秒切分
    left = ClockState(base_real=1000.0, base_world=0, rate=1, high_water_real=1000.0)
    cmds = [
        RateCommand(input_real=1005.0, effective_real=1010, rate=10, seq=1),
        RateCommand(input_real=1012.0, effective_real=1020, rate=100, seq=2),
    ]
    settled, consumed = settle(left, 1030.0, cmds)
    expect = 10 + (1020 - 1010) * 10 + (1030 - 1020) * 100
    check("§2.3 条4/5 分段累计、调度迟到仍按原定整秒切分", f"world={expect}",
          f"world={target_world(settled, 1030.0)}，消耗命令={[c.rate for c in consumed]}",
          "PASS" if target_world(settled, 1030.0) == expect else "FAIL",
          code_ref="isekai_core/runtime/clock.py:51-75")


def c_rate_service() -> None:
    with scenario("rate") as env:
        info, tl, _cid, _pkg = mk(env)
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)

        # §2.2 rate_max 全局配置、默认 2592000；rate=0 不是冻结表达
        rmax = env.world.rate_max
        rejects = []
        for bad in (0, -1, rmax + 1):
            try:
                env.world.set_rate(iid, tl, rate=bad, now_real=base)
                rejects.append(f"{bad}:接受")
            except RuntimeStateError:
                rejects.append(f"{bad}:拒绝")
        check("§2.2 条1/2/3 倍率正整数字段 [1, rate_max]、rate_max 默认 2592000、冻结不用 0",
              "0/-1/超上限都被拒绝，rate_max=2592000",
              f"rate_max={rmax}，{rejects}",
              "PASS" if rmax == 2592000 and all("拒绝" in item for item in rejects) else "FAIL",
              code_ref="isekai_core/runtime/service.py:1478-1482、clock.py:16")

        # §2.3 条2 整秒上 / 整秒前：都在下一整秒生效
        before = env.world.view(iid, tl, now_real=base + 0.9)
        r1 = env.world.set_rate(iid, tl, rate=60, now_real=base)          # 恰在整秒
        r2 = env.world.set_rate(iid, tl, rate=60.0 and 60, now_real=base)  # 重试同一请求
        check("§2.3 条2/条3 整秒输入归下一秒；重试同一请求不产生第二次变更",
              "effective=floor(base)+1；第二次调用返回 duplicate 且 pending 只有一条",
              f"effective={r1['effective_real']}，duplicate={r2.get('duplicate')}，pending={len(env.store.rate_pending(tl))}",
              "PASS" if (r1["effective_real"] == int(base) + 1 and r2.get("duplicate") is True
                         and before["rate"] == 1 and len(env.store.rate_pending(tl)) == 1) else "FAIL",
              code_ref="isekai_core/runtime/service.py:1515-1554")

        # §2.3 条3 同一生效点多次调整：最后一个有效请求为准（不能覆盖前一倍率段）
        e1 = int(base) + 1
        env.world.advance(iid, tl, now_real=e1 + 4)               # 先结算到旧段
        r3 = env.world.set_rate(iid, tl, rate=120, now_real=e1 + 4.2)
        r4 = env.world.set_rate(iid, tl, rate=240, now_real=e1 + 4.6)
        e2 = int(e1 + 4.6) + 1
        view = env.world.view(iid, tl, now_real=e2 + 10)
        # e1 段按旧倍率 60 结算（120 与 240 同生效点，按 §2.3 条3 前者被后者取代、从未生效）
        expect_world = DAY * 1500 + (e1 - int(base)) * 1 + (e2 - e1) * 60 + 10 * 240
        check("§2.3 条3/条4 同一生效点以最后请求为准、生效时先按旧倍率结算",
              f"world={expect_world}（e1 段按 60 结算；同生效点的 120 被 240 取代）",
              f"world={view['world_seconds']}，rate={view['rate']}，"
              f"命令={[(r3['effective_real'], r3['rate']), (r4['effective_real'], r4['rate'])]}",
              "PASS" if view["world_seconds"] == expect_world and view["rate"] == 240 else "FAIL",
              code_ref="isekai_core/runtime/service.py:1484-1511")

        # §2.3 条6 待生效请求先持久化（重启后仍在）
        row = env.store.clock_get(tl)
        pending = env.store.rate_pending(tl)
        check("§2.3 条6 待生效请求先持久化才确认成功（可跨重启恢复）",
              "clock 视图与 rate_command 表里能看到尚未生效的请求",
              f"pending_in_db={[(item['effective_real'], item['rate']) for item in pending]}，"
              f"未生效请求数={len(pending)}",
              "PASS" if pending else "FAIL",
              code_ref="isekai_core/store.py:3265-3280")

        # §2.2 时钟倒拨：高水位 + 非剧情性提示
        env.store.clock_put({**env.store.clock_get(tl), "high_water_real": float(e2 + 5000)})
        behind = env.world.view(iid, tl, now_real=e2 + 10)
        check("§2.2 条7 倒拨时保留现实时间高水位并给出非剧情性异常提示",
              "view.clock.behind=True 且世界时间不倒退",
              f"behind={behind['clock']['behind']}，world={behind['world_seconds']}",
              "PASS" if behind["clock"]["behind"] is True else "FAIL",
              code_ref="isekai_core/runtime/clock.py:90-99")

        # §2.3 条6 / §4 冻结：结算已生效段、取消未生效请求、冻结线不接受倍率调整
        env.store.clock_put({**env.store.clock_get(tl), "high_water_real": 0.0})
        env.world.set_rate(iid, tl, rate=30, now_real=e2 + 20)
        frozen = env.world.freeze(iid, tl, now_real=e2 + 20.5)
        frozen_view = env.world.view(iid, tl, now_real=e2 + 20.5)
        rejected = False
        try:
            env.world.set_rate(iid, tl, rate=5, now_real=e2 + 21)
        except RuntimeStateError:
            rejected = True
        check("§2.3 条6 / §3 冻结前结算、取消未生效请求；冻结线不接受倍率调整且视图只显示已冻结",
              "cancelled_commands=1、pending 清空、set_rate 被拒、view 无 world_seconds 且 label=已冻结",
              f"cancelled={frozen['cancelled_commands']}，pending={len(env.store.rate_pending(tl))}，"
              f"拒={rejected}，view.state={frozen_view['state']}，label={frozen_view.get('label')}，"
              f"有world_seconds={'world_seconds' in frozen_view}",
              "PASS" if (frozen["cancelled_commands"] >= 1 and not env.store.rate_pending(tl)
                         and rejected and frozen_view["state"] == "frozen"
                         and frozen_view.get("label") == "已冻结" and "world_seconds" not in frozen_view)
              else "FAIL",
              code_ref="isekai_core/runtime/service.py:1445-1476、1558-1595")


def c_rate_persist_restart() -> None:
    """§2.3 条6：待生效请求在**重启**（重开 Store）后按原定时刻恢复。"""
    with tempfile.TemporaryDirectory(prefix="wr-audit-restart-") as tmp:
        cfg = load_config(tmp)
        store = Store(cfg.paths.db)
        store.ensure_schema()
        world = RuntimeService(store, autocommit_enabled=False)
        package = example_package()
        cards = [example_card(package)]
        info = create_instance(store, package, cards, seed="r")
        tl = store.timeline_list(info["id"])[0]["id"]
        world.ensure_instance(info["id"], now_real=time.time())
        base = 1_700_000_000.0
        world.activate(info["id"], tl, now_real=base)
        world.set_rate(info["id"], tl, rate=45, now_real=base + 0.5)
        store.close()

        store2 = Store(cfg.paths.db)
        world2 = RuntimeService(store2, autocommit_enabled=False)
        pending = store2.rate_pending(tl)
        resumed = world2.advance(info["id"], tl, now_real=base + 3)
        rate_now = store2.clock_get(tl)["rate"]
        store2.close()
        check("§2.3 条6 重启后按原定整秒恢复待生效倍率",
              "重启后仍能看到该请求，并在原定整秒（非重启时刻）生效",
              f"重启后 pending={len(pending)}，推进后 rate={rate_now}（期望 45），"
              f"watermark={resumed['processed_world']}",
              "PASS" if pending and rate_now == 45 else "FAIL",
              code_ref="isekai_core/runtime/service.py:1484-1511、store.py:3265-3280")


def c_multiline() -> None:
    with scenario("multiline") as env:
        info, tl_a, cid, _pkg = mk(env)
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl_a, now_real=base)
        env.world.advance(iid, tl_a, now_real=base + 2 * DAY, max_batches=20)
        mark = env.world.commit(iid, tl_a, note="分叉点")
        branch = env.world.fork(iid, tl_a, commit_id=mark["id"], name="另一条线")
        tl_b = branch["timeline"]["id"]
        check("§4 条2/§6 创建分叉本身不等于激活", "分叉线创建后 state=frozen",
              f"forked_state={branch['timeline']['state']}",
              "PASS" if branch["timeline"]["state"] == "frozen" else "FAIL",
              code_ref="isekai_core/runtime/service.py:526-568")

        env.world.activate(iid, tl_b, now_real=base + 3 * DAY)
        env.world.set_rate(iid, tl_a, rate=99, now_real=base + 3 * DAY)
        rate_b = env.world.view(iid, tl_b, now_real=base + 3 * DAY)["rate"]
        wm_b_before = env.store.clock_get(tl_b)["processed_world"]
        env.world.advance(iid, tl_a, now_real=base + 4 * DAY, max_batches=20)
        wm_b_after = env.store.clock_get(tl_b)["processed_world"]
        check("§2.4 条1/§4 每条激活线独立持有倍率、互不影响",
              "A 改倍率不影响 B；A 推进不改变 B 的水位",
              f"B.rate={rate_b}（期望 1），B 水位 {wm_b_before}→{wm_b_after}",
              "PASS" if rate_b == 1 and wm_b_before == wm_b_after else "FAIL",
              code_ref="isekai_core/runtime/service.py:1515-1554")

        # §2.4 条2 冻结间隔不被补算（重新激活以当前现实时间重锚）
        env.world.freeze(iid, tl_b, now_real=base + 4 * DAY)
        world_at_freeze = env.world.view(iid, tl_a, now_real=base + 4 * DAY)  # 无关，仅避免误用
        _ = world_at_freeze
        frozen_world = env.store.clock_get(tl_b)["processed_world"]
        reactivated = env.world.activate(iid, tl_b, now_real=base + 30 * DAY)
        after = env.world.advance(iid, tl_b, now_real=base + 30 * DAY + 10)
        check("§2.4 条2/附录B#2 重新激活以当前现实时间重锚、不补算冻结间隔",
              f"重激活后 world={frozen_world}，再过 10 现实秒只 +10 世界秒",
              f"重激活 world={reactivated['world_seconds']}，再过 10 秒后={after['processed_world']}",
              "PASS" if (reactivated["world_seconds"] == frozen_world
                         and after["processed_world"] <= frozen_world + 10) else "FAIL",
              code_ref="isekai_core/runtime/service.py:1399-1443")

        # §3 只切换查看不改变激活集合
        env.world.view(iid, tl_a, now_real=base + 30 * DAY)
        env.world.view(iid, tl_b, now_real=base + 30 * DAY)
        states = {row["id"]: row["state"] for row in env.store.timeline_list(iid)}
        check("§3 条1/条4 切换查看不改变激活集合",
              "两条线仍各自保持 active / active",
              f"states={states}", "PASS" if set(states.values()) == {"active"} else "FAIL",
              code_ref="isekai_core/runtime/service.py:1558-1595")

        # §4 同时激活上限（配置默认 4）：已有 A、B 两条激活，再补两条到上限
        for index in range(2):
            extra = env.world.fork(iid, tl_a, commit_id=mark["id"], name=f"上限{index}")
            env.world.activate(iid, extra["timeline"]["id"], now_real=base + 31 * DAY)
        overflow = None
        try:
            over = env.world.fork(iid, tl_a, commit_id=mark["id"], name="上限溢出")
            env.world.activate(iid, over["timeline"]["id"], now_real=base + 31 * DAY)
        except RuntimeStateError as exc:
            overflow = str(exc)
        check("§4 条2 允许同时激活多条，并可配置激活数量上限",
              f"max_active_timelines={env.world.max_active_timelines} 时第 5 条被拒",
              f"溢出结果={overflow}",
              "PASS" if overflow and str(env.world.max_active_timelines) in overflow else "FAIL",
              code_ref="isekai_core/runtime/service.py:1411-1413")

        # §4 归档先冻结、不删除数据
        env.world.archive_timeline(iid, tl_a, now_real=base + 32 * DAY)
        archived = env.store.timeline_get(tl_a)
        check("§4 条5 归档先冻结、不删除数据",
              "归档线 state=archived，事件与水位仍在",
              f"state={archived['state']}，水位={env.store.clock_get(tl_a)['processed_world']}",
              "PASS" if archived["state"] == "archived" else "FAIL",
              code_ref="isekai_core/runtime/service.py:496-502")

        # §4 删除其它线不影响本线可读状态；最后一条线不能删
        counts_before = len(env.store.event_window(iid, tl_b, until=10**12, limit=500))
        lines = env.store.timeline_list(iid)
        victim = next(row["id"] for row in lines if row["id"] not in (tl_b,))
        env.world.delete_timeline(iid, victim)
        counts_after = len(env.store.event_window(iid, tl_b, until=10**12, limit=500))
        check("§4 条5/§6 删除一条线不改变其他线的可读状态",
              "被删线的数据消失，本线事件数不变",
              f"本线事件 {counts_before}→{counts_after}",
              "PASS" if counts_before == counts_after else "FAIL",
              code_ref="isekai_core/runtime/service.py:504-510")


# --------------------------------------------------------------- §2.6 补算与水位


def c_catchup_equivalence() -> None:
    """附录 B #3：同一固定输入，连续推进与分批补算的事实状态一致、不重复。"""
    with scenario("catchup") as env:
        info, tl, cid, _pkg = mk(env)
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        env.world.advance(iid, tl, now_real=base + 10 * DAY, max_batches=30)
        mark = env.world.commit(iid, tl, note="等价性分叉点")
        f1 = env.world.fork(iid, tl, commit_id=mark["id"], name="小步")
        f2 = env.world.fork(iid, tl, commit_id=mark["id"], name="大步")
        tl_1, tl_2 = f1["timeline"]["id"], f2["timeline"]["id"]
        t0 = base + 20 * DAY
        env.world.activate(iid, tl_1, now_real=t0)
        env.world.activate(iid, tl_2, now_real=t0)
        for step in range(1, 6):
            env.world.advance(iid, tl_1, now_real=t0 + step * DAY, max_batches=1)
        big = env.world.advance(iid, tl_2, now_real=t0 + 5 * DAY, max_batches=30)

        def facts(tlid: str) -> dict[str, Any]:
            events_rows = env.store.event_window(iid, tlid, until=10**12, limit=900)
            return {
                "processed": env.store.clock_get(tlid)["processed_world"],
                "events": sorted((int(r["world_seconds"]), str(r["id"]), str(r["summary"])) for r in events_rows),
                "experiences": sorted(
                    (str(r["character_id"]), int(r["world_seconds"]), str(r["id"]), str(r["summary"]))
                    for r in env.store.experience_window(iid, tlid, cid, until=10**12, limit=900)
                ),
                "units": sorted((str(r["id"]), round(float(r["confidence"]), 6), str(r["mode"]), int(r["archived"]))
                                for r in env.store.unit_list(iid, tlid, cid)),
                "knowledge": sorted((str(r["id"]), int(r["world_seconds"])) for r in
                                    env.store.knowledge_window(iid, tlid, cid, until=10**12, limit=900)),
                "environment": sorted((str(r["type_id"]), str(r["value"]), int(r["from_world"]))
                                      for r in env.store.environment_list(iid, tlid)),
                "intents": sorted((str(r["id"]), str(r["stage"])) for r in env.store.intent_list(iid, tlid, cid)),
            }

        a, b = facts(tl_1), facts(tl_2)
        diffs = [key for key in a if a[key] != b[key]]
        check("附录B#3/§2.6 条3 分批补算与连续推进的事实状态等价",
              "事件、经历、性格单元、获知、环境、打算全部一致",
              f"processed {a['processed']} vs {b['processed']}，差异字段={diffs or '无'}"
              + (f"，其中 events 差集={sorted(set(a['events']) ^ set(b['events']))[:2]}" if "events" in diffs else ""),
              "PASS" if not diffs and big["state"] == "current" else "FAIL",
              code_ref="isekai_core/runtime/service.py:1603-1712")

        # 重复推进 / 重启后不重复产生经历
        before = {key: a[key] for key in ("events", "experiences", "knowledge")}
        again = env.world.advance(iid, tl_1, now_real=t0 + 5 * DAY, max_batches=5)
        after = facts(tl_1)
        dup = [key for key in before if before[key] != after[key]]
        check("附录B#10/§2.6 条2 重复推进不重复产生经历（幂等）",
              "再次推进 batches=0 且事件 / 经历 / 获知集合不变",
              f"batches={again['batches']}，集合差异={dup or '无'}",
              "PASS" if again["batches"] == 0 and not dup else "FAIL",
              code_ref="isekai_core/runtime/service.py:1623-1626")


def c_catchup_restart_idempotent() -> None:
    with tempfile.TemporaryDirectory(prefix="wr-audit-restart2-") as tmp:
        cfg = load_config(tmp)
        store = Store(cfg.paths.db)
        store.ensure_schema()
        world = RuntimeService(store, autocommit_enabled=False)
        package = example_package()
        cards = [example_card(package)]
        info = create_instance(store, package, cards, seed="idem")
        iid = info["id"]
        tl = store.timeline_list(iid)[0]["id"]
        world.ensure_instance(iid, now_real=time.time())
        base = 1_700_000_000.0
        world.activate(iid, tl, now_real=base)
        world.advance(iid, tl, now_real=base + 6 * DAY, max_batches=20)
        snapshot = {
            "events": len(store.event_window(iid, tl, until=10**12, limit=900)),
            "experiences": len(store.experience_window(iid, tl, "cc-堤禾", until=10**12, limit=900)),
            "watermark": store.clock_get(tl)["processed_world"],
        }
        store.close()

        store2 = Store(cfg.paths.db)
        world2 = RuntimeService(store2, autocommit_enabled=False)
        world2.ensure_instance(iid, now_real=time.time())
        resumed = world2.catch_up_all(now_real=base + 6 * DAY)
        after = {
            "events": len(store2.event_window(iid, tl, until=10**12, limit=900)),
            "experiences": len(store2.experience_window(iid, tl, "cc-堤禾", until=10**12, limit=900)),
            "watermark": store2.clock_get(tl)["processed_world"],
        }
        store2.close()
        check("附录B#10/附录B#3 重启（补算）不重复执行历史效果",
              "重启前后事件数、经历数、水位一致",
              f"{snapshot} → {after}（resumed={list(resumed)}）",
              "PASS" if snapshot == after else "FAIL",
              code_ref="isekai_core/app.py:229-231、service.py:2622-2632")


def c_limited() -> None:
    """附录 B #20：追赶受限状态与恢复。"""
    with scenario("limited", catch_up_lag_seconds=1000, catch_up_batches=1) as env:
        info, tl, _cid, _pkg = mk(env)
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        first = env.world.advance(iid, tl, now_real=base + 5 * DAY)
        clock = env.store.clock_get(tl)
        view = env.world.view(iid, tl, now_real=base + 5 * DAY)
        limited_flag = int(clock.get("limited") or 0)

        catch = env.world.advance(iid, tl, now_real=base + 5 * DAY, max_batches=60)
        clock_after_catch = env.store.clock_get(tl)
        view_after_catch = env.world.view(iid, tl, now_real=base + 5 * DAY)
        next_tick = env.world.advance(iid, tl, now_real=base + 5 * DAY)
        clock_next_tick = env.store.clock_get(tl)
        check("附录B#20 目标持续领先时进入追赶受限；追平后退出、无积压时目标与水位相等",
              "受限时 limited=1 且 view.catching_up=True；追平的那一批即把 limited 清 0，"
              "world==processed",
              f"受限={limited_flag}/catching_up={view['catching_up']}（batches={first['batches']}）；"
              f"追平批后 limited={int(clock_after_catch.get('limited') or 0)}（processed="
              f"{clock_after_catch['processed_world']} target={int(base + 5 * DAY)}），"
              f"下一次推进后 limited={int(clock_next_tick.get('limited') or 0)}；"
              f"world==processed：{view_after_catch['world_seconds'] == view_after_catch['processed_world']}"
              f"（catch={catch['state']}，next={next_tick['state']}）",
              "PASS" if (limited_flag == 1 and view["catching_up"] is True
                         and int(clock_after_catch.get("limited") or 0) == 0
                         and view_after_catch["world_seconds"] == view_after_catch["processed_world"])
              else "FAIL",
              code_ref="isekai_core/runtime/service.py:1630-1638、1705-1712")

        # 处理水位超过合法目标 → 记为一致性错误，不静默回退
        now = base + 5 * DAY
        row = env.store.clock_get(tl)
        env.store.clock_put({
            **row,
            "base_world": int(row["processed_world"]) - 500,
            "base_real": float(now + 1000),
            "high_water_real": float(now + 1000),
        })
        bad = env.world.advance(iid, tl, now_real=now)
        check("附录B#20 尾句 处理水位超过合法目标另记为一致性错误",
              "state=inconsistent，不静默回退水位",
              f"state={bad['state']}，processed={bad['processed_world']}，target={bad['target']}",
              "PASS" if bad["state"] == "inconsistent" else "FAIL",
              code_ref="isekai_core/runtime/service.py:1617-1622")


def c_persistence_fault() -> None:
    """附录 B #22：一批失败整批回滚、水位可读、恢复后从该水位继续。"""
    with scenario("fault") as env:
        info, tl, _cid, _pkg = mk(env)
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        good = env.world.advance(iid, tl, now_real=base + 2 * DAY, max_batches=10)
        watermark_before = env.store.clock_get(tl)["processed_world"]
        events_before = len(env.store.event_window(iid, tl, until=10**12, limit=900))

        original = env.store.apply_runtime_batch

        def boom(**kwargs: Any) -> bool:
            raise sqlite3.OperationalError("disk I/O error (审计注入)")

        env.store.apply_runtime_batch = boom  # type: ignore[method-assign]
        raised = None
        try:
            env.world.advance(iid, tl, now_real=base + 4 * DAY, max_batches=10)
        except Exception as exc:  # noqa: BLE001
            raised = type(exc).__name__
        finally:
            env.store.apply_runtime_batch = original  # type: ignore[method-assign]

        watermark_after = env.store.clock_get(tl)["processed_world"]
        events_after = len(env.store.event_window(iid, tl, until=10**12, limit=900))
        recovered = env.world.advance(iid, tl, now_real=base + 4 * DAY, max_batches=10)
        check("附录B#22/§2.7 条4 一批失败整批回滚、最后完整水位可读、恢复后继续",
              "失败时水位与事件数不变（批不半落盘），恢复后从该水位继续到目标",
              f"异常={raised}，水位 {watermark_before}→{watermark_after}，"
              f"事件 {events_before}→{events_after}，恢复后 state={recovered['state']} "
              f"watermark={recovered['processed_world']}",
              "PASS" if (raised and watermark_before == watermark_after and events_before == events_after
                         and recovered["state"] in ("current", "catching_up")
                         and recovered["processed_world"] > watermark_after) else "FAIL",
              code_ref="isekai_core/store.py:1998-2030、runtime/service.py:1672-1701")


def c_backup_restore() -> None:
    """附录 B #14：备份恢复先冻结、不补算旧间隔、重新锚定。"""
    with scenario("backup") as env:
        info, tl, _cid, _pkg = mk(env)
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        env.world.advance(iid, tl, now_real=base + 2 * DAY, max_batches=10)
        env.world.set_rate(iid, tl, rate=12, now_real=base + 2 * DAY)
        backup_path = env.root / "backup.db"
        created = env.store.backup_create(backup_path, note="审计")
        env.world.advance(iid, tl, now_real=base + 5 * DAY, max_batches=10)
        restored = env.store.backup_restore(backup_path, safety=None)
        states = {row["id"]: row["state"] for row in env.store.timeline_list(iid)}
        pending = env.store.rate_pending(tl)
        clock = env.store.clock_get(tl)
        advanced_after = env.world.catch_up_all(now_real=base + 40 * DAY)
        clock_after = env.store.clock_get(tl)
        check("附录B#14/§2.6 条8 备份恢复后全部线先冻结、不补算备份日至今、不恢复待生效命令",
              "state=frozen、rate_command 清空、catch_up_all 不推进该线",
              f"恢复后 state={states}，pending={len(pending)}，"
              f"catch_up_all={list(advanced_after)}，水位 {clock['processed_world']}→{clock_after['processed_world']}",
              "PASS" if (set(states.values()) == {"frozen"} and not pending
                         and tl not in advanced_after
                         and clock["processed_world"] == clock_after["processed_world"]) else "FAIL",
              code_ref="isekai_core/store.py:815-843、app.py:229-231")


# --------------------------------------------------------------- §5/§6/§7 版本管理


def c_versioning() -> None:
    with scenario("version") as env:
        info, tl, cid, _pkg = mk(env)
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        env.world.advance(iid, tl, now_real=base + 3 * DAY, max_batches=12)
        say(env, iid, tl, cid, env_id="e-1", text="第一轮对话", reply="收到第一句。")
        mark = env.world.commit(iid, tl, note="回滚点")
        watermark = env.store.clock_get(tl)["processed_world"]
        snapshot = env.store.commit_snapshot_get(mark["id"]) or {}
        runtime_payload = snapshot.get("runtime") or {}
        keys = sorted(runtime_payload.keys())
        listed = env.world.commits(iid, tl)
        leaks = [item for item in listed if any(k in json.dumps(item, ensure_ascii=False) for k in ("summary", "事件", "剧情"))]
        check("§5.1 条1/条5 提交快照含世界运行状态与语义元数据；列表只给管理元数据",
              "快照含 events/units/experiences/intents/dialog 与 rules_version；列表无剧情摘要",
              f"snapshot keys={keys}，dialog={len(snapshot.get('dialog') or [])} 条，"
              f"rules={snapshot.get('rules_version')}，列表字段={sorted(listed[0].keys()) if listed else []}，泄漏项={len(leaks)}",
              "PASS" if ({"events", "units", "experiences", "intents"} <= set(keys)
                         and snapshot.get("rules_version") and not leaks) else "FAIL",
              code_ref="isekai_core/runtime/versioning.py:20-70、service.py:466-487")

        # 回滚：覆盖语义 + 重新锚定 + 不瞬间追赶
        env.world.advance(iid, tl, now_real=base + 6 * DAY, max_batches=12)
        say(env, iid, tl, cid, env_id="e-2", text="第二轮对话", reply="收到第二句。")
        pre_rollback_generation = env.store.clock_get(tl)["generation"]
        progress_before = env.store.clock_get(tl)["processed_world"]
        rollback_now = base + 10 * DAY
        env.world.rollback(iid, tl, commit_id=mark["id"], now_real=rollback_now)
        after = env.world.view(iid, tl, now_real=rollback_now)
        messages = [env.store.message_text(row) for row in
                    env.store.history_page(env.store.session_ensure(iid, tl, cid)["id"], limit=100)["messages"]]
        later = env.world.view(iid, tl, now_real=rollback_now + 10)
        check("§7 条1/条4/附录B#4 回滚覆盖到提交点、以回滚时刻重锚、不瞬间追赶",
              f"回滚后 world={watermark}（不是 {progress_before}），再过 10 秒只 +10×rate；对话回退",
              f"回滚后 world={after['world_seconds']}，10 秒后={later['world_seconds']}，"
              f"对话={messages}",
              "PASS" if (after["world_seconds"] == watermark
                         and later["world_seconds"] == watermark + 10 * int(after["rate"])
                         and "第二轮对话" not in "".join(messages)
                         and "第一轮对话" in "".join(messages)) else "FAIL",
              code_ref="isekai_core/runtime/service.py:570-636")

        # 旧世代迟到批次不能写回
        stolen = env.store.apply_runtime_batch(
            timeline_id=tl, generation=pre_rollback_generation, processed_world=progress_before,
            catching_up=False,
            events=[{
                "id": "ev-late-audit", "instance_id": iid, "timeline_id": tl,
                "world_seconds": progress_before, "seq": 1, "kind": "world", "family": "",
                "template": "late", "source": "engine", "summary": "迟到事件（不应写回）",
                "detail": "", "text_source": "template", "effects": "[]", "share_value": 0,
                "importance": 0.4, "created_real": 0.0,
            }],
        )
        late_rows = [row for row in env.store.event_window(iid, tl, until=10**12, limit=900)
                     if str(row["id"]) == "ev-late-audit"]
        check("§7 条3/附录B#4 回滚后旧世代的迟到结果不能写回",
              "apply_runtime_batch 返回 False 且事件未落库",
              f"committed={stolen}，迟到事件行数={len(late_rows)}",
              "PASS" if stolen is False and not late_rows else "FAIL",
              code_ref="isekai_core/store.py:1998-2030、service.py:1630-1698")

        # 分叉指向不可变来源提交：来源线继续推进 / 回滚后，分叉线共同过去不变
        fork = env.world.fork(iid, tl, commit_id=mark["id"], name="冻结分支")
        tl_f = fork["timeline"]["id"]
        fork_events_1 = sorted(str(row["id"]) for row in env.store.event_window(iid, tl_f, until=10**12, limit=900))
        env.world.activate(iid, tl, now_real=rollback_now + DAY)
        env.world.advance(iid, tl, now_real=rollback_now + 4 * DAY, max_batches=12)
        env.world.rollback(iid, tl, commit_id=mark["id"], now_real=rollback_now + 5 * DAY)
        fork_events_2 = sorted(str(row["id"]) for row in env.store.event_window(iid, tl_f, until=10**12, limit=900))
        check("§6 条3/附录B#5 新线指向不可变来源提交，来源线推进 / 回滚不改变分叉线的共同过去",
              "分叉线事件集合在来源线推进与回滚前后完全一致",
              f"分叉事件 {len(fork_events_1)}→{len(fork_events_2)}，一致={fork_events_1 == fork_events_2}",
              "PASS" if fork_events_1 == fork_events_2 and fork_events_1 else "FAIL",
              code_ref="isekai_core/runtime/service.py:526-568、store.py:2274-2300")

        # 删除来源线：被引用的不可变提交仍可读
        source_events = len(env.store.event_window(iid, tl, until=10**12, limit=900))
        env.world.delete_timeline(iid, tl)
        fork_after_delete = len(env.store.event_window(iid, tl_f, until=10**12, limit=900))
        check("§4 条5/§6 条5 删除来源线不连带删除被引用的不可变数据",
              "分叉线仍可读且事件数不变",
              f"来源线事件 {source_events}，删后分叉线事件 {fork_after_delete}",
              "PASS" if fork_after_delete == len(fork_events_1) else "FAIL",
              code_ref="isekai_core/runtime/service.py:504-510")


def c_patch_card_rollback() -> None:
    """附录 B #18：回滚跨越补卡加入点使角色一致退出；冻结线补入不激活该线。"""
    with scenario("patchcard") as env:
        package = example_package()
        first = example_card(package)
        info, tl, cid, _pkg = mk(env, cards=[first])
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        env.world.advance(iid, tl, now_real=base + 2 * DAY, max_batches=10)
        mark = env.world.commit(iid, tl, note="补卡之前")
        env.world.advance(iid, tl, now_real=base + 4 * DAY, max_batches=10)

        second = example_card(package, name="第二人")
        added = env.world.add_character(iid, tl, second, now_real=base + 4 * DAY, note="审计补卡")
        cards_after = {str((card["meta"] or {}).get("card_id"))
                       for card in env.world.cards(env.store.instance_get(iid), timeline_id=tl)}
        env.world.rollback(iid, tl, commit_id=mark["id"], now_real=base + 5 * DAY)
        cards_rolled = {str((card["meta"] or {}).get("card_id"))
                        for card in env.world.cards(env.store.instance_get(iid), timeline_id=tl)}
        joins = env.store.character_join_list(iid, tl, until=10**12)
        units_gone = env.store.unit_list(iid, tl, "cc-第二人")
        check("§7 条10/附录B#18 回滚跨越补卡加入点：角色在本线一致退出",
              f"回滚后 {added['character']} 从卡片集合、character_join、单元中一并消失",
              f"补入后卡片={sorted(cards_after)}，回滚后={sorted(cards_rolled)}，join 行={len(joins)}，单元={len(units_gone)}",
              "PASS" if ("cc-第二人" in cards_after and "cc-第二人" not in cards_rolled
                         and not joins and not units_gone) else "FAIL",
              code_ref="isekai_core/runtime/service.py:2835-2944、570-636")

        # 冻结线补卡：锚定冻结时刻，且不激活该线
        env.world.freeze(iid, tl, now_real=base + 6 * DAY)
        frozen = env.store.timeline_get(tl)
        clock = env.store.clock_get(tl)
        anchor_world = target_world(
            ClockState(base_real=float(clock["base_real"]), base_world=int(clock["base_world"]),
                       rate=int(clock["rate"]), high_water_real=float(clock["high_water_real"])),
            float(clock["anchor_real"]),
        )
        third = example_card(package, name="第三人")
        joined = env.world.add_character(iid, tl, third, now_real=base + 7 * DAY, note="冻结线补卡")
        state_after = env.store.timeline_get(tl)["state"]
        check("§4/附录B#18 冻结线补入角色锚定冻结时刻、不激活该线",
              f"joined_world={anchor_world}、状态仍 frozen",
              f"joined_world={joined['joined_world']}，期望={anchor_world}，state={state_after}",
              "PASS" if joined["joined_world"] == anchor_world and state_after == frozen["state"] == "frozen" else "FAIL",
              code_ref="isekai_core/runtime/service.py:2845-2866")


def c_compression_and_diff() -> None:
    """§8 压缩 / §6 diff：当前实现是阶段 4 之前的全量快照。"""
    with scenario("compress") as env:
        info, tl, _cid, _pkg = mk(env)
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        env.world.advance(iid, tl, now_real=base + 2 * DAY, max_batches=10)
        env.world.commit(iid, tl, note="一次")
        env.world.advance(iid, tl, now_real=base + 3 * DAY, max_batches=10)
        env.world.commit(iid, tl, note="二次")
        size = env.root.joinpath("data", "isekai.db").stat().st_size
        snapshot_dump = env.store.runtime_dump(iid, tl, watermark=10**12)
        tables = {
            str(row["name"])
            for row in env.store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()  # noqa: SLF001
        }
        check("§8 压缩策略（自动压缩减少 diff 链长度、合并物理存储）", "存在可触发的压缩路径",
              f"无压缩相关表（{len(tables)} 张表）；提交为全量快照；库大小 {size} 字节",
              "DEFERRED",
              evidence="§六 / §15 允许「阶段 4 前先用全量快照验证相同语义」，实现尚未进入 diff/压缩阶段",
              code_ref="isekai_core/runtime/versioning.py:1-6")
        _ = snapshot_dump


# --------------------------------------------------------------- §10 性格单元


def c_personality() -> None:
    with scenario("personality") as env:
        info, tl, cid, _pkg = mk(env)
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        units = env.store.unit_list(iid, tl, cid)
        anchor_id = next(row["id"] for row in units if row["mode"] == "anchor")
        dialog_id = next(row["id"] for row in units if row["mode"] == "dialog")
        anchor_semantic = next(row["semantic"] for row in units if row["id"] == anchor_id)
        dialog_semantic = next(row["semantic"] for row in units if row["id"] == dialog_id)

        first = env.world.drive_unit(
            iid, tl, cid, driver="dialog", semantic=str(dialog_semantic), basis="审计对话",
            strength=1.0, direction=1, source_key="src-1",
        )
        repeat = env.world.drive_unit(
            iid, tl, cid, driver="dialog", semantic=str(dialog_semantic), basis="审计对话",
            strength=1.0, direction=1, source_key="src-1",
        )
        weak = env.world.drive_unit(
            iid, tl, cid, driver="time", semantic=str(anchor_semantic), basis="审计弱驱动",
            strength=1.0, direction=-1, source_key="src-weak",
        )
        after = {row["id"]: row for row in env.store.unit_list(iid, tl, cid)}
        check("§10.1/§10.3 条3 驱动按步长连续变化；同一来源只消费一次；弱驱动不把高置信单元裁到本次生成上限",
              f"dialog 单元 +0.06（一次生效）、重复来源返回 None、锚点仍 > {personality.BANDS['time'][1]}",
              f"dialog {float(after[dialog_id]['confidence'])}（原 0.45），重复={repeat}，"
              f"锚点 {float(after[anchor_id]['confidence'])}，弱驱动返回值={None if weak is None else weak['id']}",
              "PASS" if (abs(float(after[dialog_id]["confidence"]) - 0.51) < 1e-9 and repeat is None
                         and float(after[anchor_id]["confidence"]) > personality.BANDS["time"][1]) else "FAIL",
              code_ref="isekai_core/runtime/personality.py:101-162、service.py:2636-2694")

        # 时间衰减按世界时长结算：小步 / 大步等价（同一祖先的两个分叉）
        mark = env.world.commit(iid, tl, note="衰减分叉点")
        f1 = env.world.fork(iid, tl, commit_id=mark["id"], name="小步衰减")
        f2 = env.world.fork(iid, tl, commit_id=mark["id"], name="大步衰减")
        t0 = base + DAY
        env.world.activate(iid, f1["timeline"]["id"], now_real=t0)
        env.world.activate(iid, f2["timeline"]["id"], now_real=t0)
        for step in range(1, 61):
            env.world.advance(iid, f1["timeline"]["id"], now_real=t0 + step * DAY, max_batches=1)
        env.world.advance(iid, f2["timeline"]["id"], now_real=t0 + 60 * DAY, max_batches=90)
        u1 = {row["id"]: round(float(row["confidence"]), 9) for row in env.store.unit_list(iid, f1["timeline"]["id"], cid)}
        u2 = {row["id"]: round(float(row["confidence"]), 9) for row in env.store.unit_list(iid, f2["timeline"]["id"], cid)}
        a1 = {row["id"]: int(row["archived"]) for row in env.store.unit_list(iid, f1["timeline"]["id"], cid)}
        check("§10.3 条2/附录B#6 时间衰减按世界时长结算：在线小步与离线大步等价",
              "60 天小步与 1 次大步得到同一组单元置信度",
              f"差异={ {k: (u1[k], u2.get(k)) for k in u1 if u1[k] != u2.get(k)} }",
              "PASS" if u1 == u2 else "FAIL",
              code_ref="isekai_core/runtime/personality.py:80-93、service.py:1756-1762")

        # 普通单元可归档、锚点被保护
        dialog_left = u1.get(dialog_id)
        anchor_left = u1.get(anchor_id)
        check("§10.3 条1/§10.2 条3/附录B#6 普通单元可衰减到归档、锚点不归档且受保护",
              "dialog 单元 archived=1；锚点 archived=0 且置信度 ≥ 保护下限",
              f"dialog={dialog_left}/archived={a1.get(dialog_id)}，锚点={anchor_left}/archived={a1.get(anchor_id)}",
              "PASS" if (a1.get(dialog_id) == 1 and a1.get(anchor_id) == 0
                         and float(anchor_left or 0) >= personality.ANCHOR_FLOOR) else "FAIL",
              code_ref="isekai_core/runtime/personality.py:165-177")

        # 隐式迁移：稳定 + 高置信 → mode 就地变 anchor，不产生迁移记录 / 迁移操作
        rows = env.store.unit_list(iid, f2["timeline"]["id"], cid)
        promoted = personality.promote([{
            **rows[0], "mode": "event", "stability": personality.PROMOTE_STABILITY,
            "confidence": personality.PROMOTE_CONFIDENCE + 0.05,
        }])
        op_names = set(ops.SYNC_OPS) | set(ops.ASYNC_OPS)
        migration_ops = [name for name in op_names if "migrat" in name or "promote" in name]
        check("§10.2 条1 驱动迁移隐式形成、不另设可见的迁移事件或迁移日志",
              "mode 就地变为 anchor，管理面无迁移入口",
              f"promote 后 mode={promoted[0]['mode']}，迁移相关 op={migration_ops or '无'}",
              "PASS" if promoted[0]["mode"] == "anchor" and not migration_ops else "FAIL",
              code_ref="isekai_core/runtime/personality.py:180-188、world/ops.py:38-105")


# --------------------------------------------------------------- §11 生活线 / 环境 / 打算


def c_life_and_death() -> None:
    with scenario("life") as env:
        package = example_package()
        awaker = example_card(package, name="夜班")
        awaker["meta"]["card_id"] = "cc-夜班"
        awaker["life_template"] = {
            "sleep": True,
            "routine_note": "夜班",
            "windows": [
                {"start": 7200, "end": 21600, "activity": "duty"},
                {"start": 21600, "end": 28800, "activity": "sleep"},
                {"start": 28800, "end": 79200, "activity": "duty"},
                {"start": 79200, "end": 93600, "activity": "rest"},  # 跨午夜：次日 [0,7200)
            ],
        }
        sleeper = example_card(package, name="常人")
        sleeper["meta"]["card_id"] = "cc-常人"
        info, tl, _cid, _pkg = mk(env, cards=[awaker, sleeper])
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        calendar = env.world.calendar(env.store.instance_get(iid))
        start_day = calendar.day_index(DAY * 1500)
        plans = {
            cid: env.store.plan_get(iid, tl, cid, start_day)
            for cid in ("cc-夜班", "cc-常人")
        }
        payloads = {cid: json.loads(str(row["windows"])) for cid, row in plans.items() if row}
        same_boundary = len({(item["day_start"], item["day_end"]) for item in payloads.values()}) == 1
        cross = any(int(window["end"]) > payloads["cc-夜班"]["day_end"]
                    for window in payloads["cc-夜班"]["windows"])
        check("§11 条3/附录B#7 夜班与常人共用一个世界日界；跨午夜睡眠按世界秒展开",
              "两份计划的 day_start/day_end 相同，跨午夜窗口 end 超过当日日长",
              f"边界一致={same_boundary}，跨午夜窗口={cross}，"
              f"夜班窗口数={len(payloads.get('cc-夜班', {}).get('windows', []))}",
              "PASS" if same_boundary and cross else "FAIL",
              code_ref="isekai_core/runtime/life.py:18-57、service.py:1744-1755")

        # 计划中的未来活动不算经历；跨入下一世界日后准备次日计划、计划不重抽
        before = env.store.experience_window(iid, tl, "cc-夜班", until=DAY * 1500 + 30000, limit=100)
        env.world.advance(iid, tl, now_real=base + 30000, max_batches=10)
        midnight_after = env.store.experience_window(iid, tl, "cc-夜班", until=DAY * 1500 + 30000, limit=100)
        plan_now = env.store.plan_get(iid, tl, "cc-夜班", start_day)
        env.world.advance(iid, tl, now_real=base + 40000, max_batches=10)
        plan_again = env.store.plan_get(iid, tl, "cc-夜班", start_day)
        env.world.advance(iid, tl, now_real=base + DAY + 100, max_batches=10)
        future_plan = env.store.plan_get(iid, tl, "cc-夜班", start_day + 1)
        too_early = [row for row in env.store.experience_window(iid, tl, "cc-夜班", until=10**12, limit=900)
                     if int(row["world_seconds"]) > env.store.clock_get(tl)["processed_world"]]
        check("§11 条3/条4、附录B#7 每个角色每个世界日一份计划、次日计划由入睡触发；未来活动不算经历",
              "同日计划标识不变（不重抽）；跨日后次日计划存在；经历水位不超过已完成水位",
              f"推进前经历={len(before)}，当日内={len(midnight_after)}，"
              f"同日计划未重抽={plan_now['id'] == plan_again['id'] if plan_now and plan_again else None}，"
              f"次日计划={'有' if future_plan else '无'}，越界经历={len(too_early)}",
              "PASS" if (not too_early and future_plan is not None
                         and plan_now and plan_again and plan_now["id"] == plan_again["id"]) else "FAIL",
              code_ref="isekai_core/runtime/service.py:1744-1755、2387-2415")

        # 寿终：由出生 + 寿命推导（年龄是推导值），死亡事件与状态在同一批里一致
        short = example_card(package, name="寿终者")
        short["meta"]["card_id"] = "cc-寿终者"
        package2 = json.loads(json.dumps(example_package("短命纪")))
        package2["races"][0]["lifespan"] = {"min_years": 55, "max_years": 60}
        # 死亡时刻 = 出生 + 60 年 = 初始时刻 + 10 天
        short["identity"]["born"] = DAY * 1500 - 60 * 90 * DAY + 10 * DAY
        info2, tl2, _cid2, _pkg2 = mk(env, package=package2, cards=[short])
        iid2 = info2["id"]
        env.world.activate(iid2, tl2, now_real=base)
        early = env.world.advance(iid2, tl2, now_real=base + 5 * DAY, max_batches=20)
        death_before = [row for row in env.store.event_window(iid2, tl2, until=10**12, limit=900)
                        if str(row["template"]).startswith("death:")]
        env.world.advance(iid2, tl2, now_real=base + 20 * DAY, max_batches=60)
        death = [row for row in env.store.event_window(iid2, tl2, until=10**12, limit=900)
                 if str(row["template"]).startswith("death:")]
        post = [row for row in env.store.experience_window(iid2, tl2, "cc-寿终者", until=10**12, limit=900)
                if death and int(row["world_seconds"]) > int(death[0]["world_seconds"])]
        expected_death = DAY * 1500 + 10 * DAY
        check("§6 条9/附录B#12 寿终由出生 + 寿命推导（年龄为推导值）；死亡事件与状态同一提交内一致",
              f"死亡时刻={expected_death}（出生 + 60 年）；死亡前无该事件、死亡后无新经历",
              f"death 事件={len(death)}（时刻={death[0]['world_seconds'] if death else None}，"
              f"摘要={death[0]['summary'] if death else None}），死亡前={len(death_before)}，"
              f"死亡后经历={len(post)}，早批次 state={early['state']}",
              "PASS" if (death and int(death[0]["world_seconds"]) == expected_death
                         and not death_before and not post) else "FAIL",
              code_ref="isekai_core/runtime/events.py:427-496、service.py:2294-2344")


def c_environment() -> None:
    with scenario("environment") as env:
        package = example_package()
        package = json.loads(json.dumps(package))
        # 加一个未声明观察者的类型：对谁都不可观察
        package["environment"]["types"].append({
            "id": "env-blind", "name": "暗流", "unit": "级", "values": [0, 1], "initial": 0,
            "sources": ["natural:暗流"], "observe": "没人能观测", "scope": "水下", "expiry": "until_cleared",
        })
        card = example_card(package)
        info, tl, cid, _pkg = mk(env, package=package, cards=[card])
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        env.world.advance(iid, tl, now_real=base + 4 * DAY, max_batches=20)
        rows = {row["type_id"]: row for row in env.store.environment_list(iid, tl)}
        snapshot = env.world.character_snapshot(iid, tl, cid, world_seconds=env.store.clock_get(tl)["processed_world"])
        observed_ids = {item["type_id"] for item in snapshot["observations"]}
        blind = [item for item in snapshot["observations"] if item["type_id"] == "env-blind"]
        check("§11.2 条1/条3/附录B#24 未声明的观察者 = 对谁都不可观察；观察只给声明的投影",
              "env-blind 不在观察结果里；env-1/env-2 在（卡片 role_id 在声明内）",
              f"观察到的类型={sorted(observed_ids)}，暗流命中={len(blind)}",
              "PASS" if "env-blind" not in observed_ids and {"env-1", "env-2"} & observed_ids else "FAIL",
              code_ref="isekai_core/runtime/environment.py:132-183")

        # 语言生成改不动环境：对话里出现"下雨"不产生环境真值
        before = {row["type_id"]: (str(row["value"]), int(row["updated_world"]))
                  for row in env.store.environment_list(iid, tl)}
        say(env, iid, tl, cid, env_id="e-rain", text="外面下雨了吗？", reply="雨声很大，潮位该涨了。")
        text_before = env.store.clock_get(tl)["processed_world"]
        _ = text_before
        after = {row["type_id"]: (str(row["value"]), int(row["updated_world"]))
                 for row in env.store.environment_list(iid, tl)}
        check("§11.2 条2/条5 文本生成不能直接改变环境（只提交声明来源的变化）",
              "对话前后环境行（值 / 更新水位）完全不变",
              f"变化={ {k: (before[k], after.get(k)) for k in before if before[k] != after.get(k)} or '无'}",
              "PASS" if before == after else "FAIL",
              code_ref="isekai_core/runtime/environment.py:98-129、service.py:1793-1824")

        # 自然变化确定性：两个同种子分叉得到同一值
        mark = env.world.commit(iid, tl, note="环境等价分叉")
        f1 = env.world.fork(iid, tl, commit_id=mark["id"], name="环境A")
        f2 = env.world.fork(iid, tl, commit_id=mark["id"], name="环境B")
        t0 = base + 10 * DAY
        for line in (f1, f2):
            env.world.activate(iid, line["timeline"]["id"], now_real=t0)
            env.world.advance(iid, line["timeline"]["id"], now_real=t0 + 5 * DAY, max_batches=20)
        v1 = {row["type_id"]: (str(row["value"]), int(row["from_world"]))
              for row in env.store.environment_list(iid, f1["timeline"]["id"])}
        v2 = {row["type_id"]: (str(row["value"]), int(row["from_world"]))
              for row in env.store.environment_list(iid, f2["timeline"]["id"])}
        check("§11.2 条2/§12 确定性 自然变化按世界时刻确定地走，同种子分叉结果相同",
              "两条线的环境值与生效水位一致",
              f"A={v1}，B={v2}", "PASS" if v1 == v2 else "FAIL",
              code_ref="isekai_core/runtime/environment.py:55-95")

        # 效果只能落在取值域内、只改已声明类型
        types = environment.env_types(package)
        rows_now = env.store.environment_list(iid, tl)
        applied = environment.apply_effects(rows_now, [
            {"kind": "environment_state", "target": "env-1", "value": 99},          # 越域
            {"kind": "environment_state", "target": "env-未知", "value": 1},        # 未声明
        ], types, world_seconds=10**12)
        check("§11.2 条1/条4 只有世界包声明的类型与取值域内的效果才生效",
              "越域值与未声明类型都不产生环境变化",
              f"越权效果导致的变化行数={len(applied)}", "PASS" if not applied else "FAIL",
              code_ref="isekai_core/runtime/environment.py:98-129")


def c_intents() -> None:
    package = example_package()
    with scenario("intents") as env:
        # 行动路径：窗口到达 + 条件满足 + 有受支持效果 → 提交事件与经历
        actor = example_card(package, name="行动计划")
        actor["meta"]["card_id"] = "cc-行动计划"
        actor["intents"] = [{
            "id": "in-act", "object": "把潮位抄件送到驿站", "basis": "她自己经手的抄件",
            "strength": 0.8, "window": {"from": DAY * 1501, "to": DAY * 1510},
            "preconditions": ["cf-1"],
            "effect": {"kind": "public_notice", "target": "src-1", "expiry": "with_cause"},
        }]
        info, tl, _cid, _pkg = mk(env, cards=[actor])
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        env.world.advance(iid, tl, now_real=base + 2 * DAY, max_batches=20)
        rows = {row["id"]: row for row in env.store.intent_list(iid, tl, "cc-行动计划")}
        events_now = [row for row in env.store.event_window(iid, tl, until=10**12, limit=900)
                      if str(row["id"]).startswith("ev-act-")]
        exp = [row for row in env.store.experience_window(iid, tl, "cc-行动计划", until=10**12, limit=900)
               if str(row["kind"]) == "action"]
        check("§11.3 条4/附录B#15 条件满足且有效果时，无用户输入也能执行打算并留下经历",
              "in-act 阶段 done，出现角色行动事件与经历",
              f"stage={rows.get('in-act', {}).get('stage')}，行动事件={len(events_now)}，经历={len(exp)}",
              "PASS" if rows.get("in-act", {}).get("stage") == "done" and events_now and exp else "FAIL",
              code_ref="isekai_core/runtime/service.py:2102-2211、intents.py:87-143")

    with scenario("intents-blocked") as env:
        blocked = example_card(package, name="受阻计划")
        blocked["meta"]["card_id"] = "cc-受阻计划"
        blocked["intents"] = [{
            "id": "in-blocked", "object": "把退潮通行的告示发出去", "basis": "她自己经手的通行牌",
            "strength": 0.5, "window": {"from": DAY * 1501, "to": DAY * 1502},
            "preconditions": ["cf-1"],
            "effect": {"kind": "public_notice", "target": "src-1", "expiry": "with_cause"},
        }]
        info, tl, _cid, _pkg = mk(env, cards=[blocked])
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        # 注入一条仍有效的后果（直到被解除）：挡住在 src-1 上的行动
        watermark = env.store.clock_get(tl)["processed_world"]
        env.store.apply_runtime_batch(
            timeline_id=tl, generation=env.store.clock_get(tl)["generation"],
            processed_world=watermark, catching_up=False,
            events=[{
                "id": "ev-audit-block", "instance_id": iid, "timeline_id": tl,
                "world_seconds": watermark, "seq": 9, "kind": "world", "family": "",
                "template": "audit", "source": "engine", "summary": "驿站停摆（审计注入）",
                "detail": "", "text_source": "template",
                "effects": json.dumps([{"kind": "route_blocked", "target": "src-1", "expiry": "until_cleared"}]),
                "share_value": 0, "importance": 0.4, "created_real": 0.0,
            }],
            effects=[{
                "id": "fx-audit-block", "instance_id": iid, "timeline_id": tl, "event_id": "ev-audit-block",
                "target": "src-1", "kind": "route_blocked", "family": "", "value": None,
                "from_world": watermark, "expiry": "until_cleared", "recovery": "", "active": 1,
                "cleared_at": None,
            }],
        )
        env.world.advance(iid, tl, now_real=base + DAY + 100, max_batches=5)     # 窗口内：等待
        mid = {row["id"]: row for row in env.store.intent_list(iid, tl, "cc-受阻计划")}
        env.world.advance(iid, tl, now_real=base + 3 * DAY, max_batches=20)   # 窗口后：延期
        late = {row["id"]: row for row in env.store.intent_list(iid, tl, "cc-受阻计划")}
        env.world.advance(iid, tl, now_real=base + 5 * DAY, max_batches=20)   # 再一轮：放弃
        final = {row["id"]: row for row in env.store.intent_list(iid, tl, "cc-受阻计划")}
        act_events = [row for row in env.store.event_window(iid, tl, until=10**12, limit=900)
                      if str(row["id"]).startswith("ev-act-")]
        check("§11.3 条3/条4、附录B#25 受阻时等条件、超窗先延期后放弃；不伪报完成、不产生事件",
              "阶段依次 waiting → deferred → abandoned，且无行动事件",
              f"窗口内={mid.get('in-blocked', {}).get('stage')}（note={mid.get('in-blocked', {}).get('note')}），"
              f"超窗={late.get('in-blocked', {}).get('stage')}，"
              f"再一轮={final.get('in-blocked', {}).get('stage')}，行动事件={len(act_events)}",
              "PASS" if (mid.get("in-blocked", {}).get("stage") == "waiting"
                         and late.get("in-blocked", {}).get("stage") == "deferred"
                         and final.get("in-blocked", {}).get("stage") == "abandoned"
                         and not act_events) else "FAIL",
              code_ref="isekai_core/runtime/intents.py:75-111、service.py:2144-2171")

    # 无受支持效果：不提交事件（不建通用规划器）
    story = intents_mod.story_units(
        [{"id": "in-x", "character_id": "c", "object": "想一想", "basis": "b", "stage": "adopted", "note": ""}],
        [],
    )
    check("§11.4 条2/条3 局部故事单元是可追溯组合视图，终态只由合法结果产生",
          "story_units 输出终态标签（持续中）且引用事件为空，不产生新事实",
          f"terminal={story[0]['terminal']}，events={story[0]['events']}",
          "PASS" if story and story[0]["terminal"] == "持续中" and not story[0]["events"] else "FAIL",
          code_ref="isekai_core/runtime/intents.py:146-171")


def c_misclaimed_era() -> None:
    """附录 B #11：史料自称的年代与相对年代不改变事件实际时刻。"""
    with scenario("era") as env:
        plain, tl_a, cid_a, _p1 = mk(env, name="原纪", seed="era-seed")
        claimed = example_package("改纪")
        claimed["historiography"][0]["contributors"][0]["period"] = "自称：纪元前 三百年前"
        claimed["historiography"][0]["title"] = "三百年后的追述"
        claimed["narratives"][0]["text"] = "三百年前北堤崩塌被记作天罚——（自称年代）"
        claimed["calendar"]["initial_moment"] = DAY * 1500
        info_b, tl_b, _cid_b, _p2 = mk(env, package=claimed, cards=[example_card(claimed)], seed="era-seed")
        base = 1_700_000_000.0
        for line_id, line_tl in ((plain["id"], tl_a), (info_b["id"], tl_b)):
            env.world.activate(line_id, line_tl, now_real=base)
            env.world.advance(line_id, line_tl, now_real=base + 6 * DAY, max_batches=20)
        events_a = sorted((int(row["world_seconds"]), str(row["template"]))
                          for row in env.store.event_window(plain["id"], tl_a, until=10**12, limit=900))
        events_b = sorted((int(row["world_seconds"]), str(row["template"]))
                          for row in env.store.event_window(info_b["id"], tl_b, until=10**12, limit=900))
        know_a = sorted((str(row["id"]), int(row["world_seconds"])) for row in
                        env.store.knowledge_window(plain["id"], tl_a, cid_a, until=10**12, limit=900))
        know_b = sorted((str(row["id"]), int(row["world_seconds"])) for row in
                        env.store.knowledge_window(info_b["id"], tl_b, "cc-堤禾", until=10**12, limit=900))
        check("附录B#11/§2.1 条8 史料自称年代属于说法，不覆盖权威事件时间，也不随查询改写原文",
              "改动自称年代文本后，事件实际时刻与获知时刻逐条相同（原文文本未被改写）",
              f"事件时刻一致={events_a == events_b}（{len(events_a)} 条），获知一致={know_a == know_b}"
              f"（{len(know_a)} 条）",
              "PASS" if events_a == events_b and know_a == know_b and events_a else "FAIL",
              code_ref="isekai_core/runtime/events.py:294-297、336-414、service.py:2574-2590")


def c_short_term_reactions() -> None:
    """§11.1 当前处境与短期反应 / 附录 B #19、#26。"""
    with scenario("reaction") as env:
        tables = {
            str(row["name"])
            for row in env.store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()  # noqa: SLF001
        }
        hit = [name for name in tables if "reaction" in name or "short" in name]
        check("§11.1/附录B#19、#26 短期反应（候选→采纳→活跃→减弱/暂停→失效）与来源边界",
              "存在承载「短期反应」（来源 / 强度区间 / 失效条件）的状态",
              f"{len(tables)} 张表中与短期反应相关的={hit or '无'}；"
              "runtime/ 内无 reaction 相关实现",
              "FAIL",
              evidence="全仓仅 docs/WORLD_RUNTIME_SPEC.md 提到「短期反应」；验收 19/26 要求的状态域缺失",
              code_ref="isekai_core/runtime/（无对应模块）")


# --------------------------------------------------------------- §13 认知接口


def c_cognition() -> None:
    with scenario("cognition") as env:
        package = example_package()
        package = json.loads(json.dumps(package))
        package["canon"][1]["statement"] = "SECRET-CF2-未公开的告警来源"
        package["world"]["axioms"][0]["text"] = "SECRET-AXIOM-潮汐公理"
        card = example_card(package)
        info, tl, cid, _pkg = mk(env, package=package, cards=[card])
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        env.world.advance(iid, tl, now_real=base + 2 * DAY, max_batches=10)
        watermark = env.store.clock_get(tl)["processed_world"]
        # 未来事件：直接落一行更晚的事件（不产生获知）
        env.store.apply_runtime_batch(
            timeline_id=tl, generation=env.store.clock_get(tl)["generation"],
            processed_world=watermark, catching_up=False,
            events=[{
                "id": "ev-future-audit", "instance_id": iid, "timeline_id": tl,
                "world_seconds": watermark + 10 * DAY, "seq": 2, "kind": "world", "family": "",
                "template": "future", "source": "engine", "summary": "SECRET-FUTURE-未来事件",
                "detail": "", "text_source": "template", "effects": "[]", "share_value": 1,
                "importance": 0.6, "created_real": 0.0,
            }],
        )
        say(env, iid, tl, cid, env_id="e-a", text="你今天量了水位吗", reply="SECRET-DIALOG-只属于A的对话")
        session = env.store.session_ensure(iid, tl, cid)
        prompt = env.world.system_prompt(session)
        leaked = [marker for marker in ("SECRET-CF2", "SECRET-AXIOM", "SECRET-FUTURE") if marker in prompt]
        check("§13.2 条1/§13.3、附录B#8 实情层、未获知秘密、未来事件不进生成上下文",
              "系统提示不含未获知的 cf-2、公理原文与未来事件",
              f"泄漏标记={leaked or '无'}；提示长度={len(prompt)}",
              "PASS" if not leaked else "FAIL",
              code_ref="isekai_core/runtime/cognition.py:235-312、171-232")

        # 角色自己的对话在上下文中（合法），别人的对话与自己的未披露内容不串
        others = example_card(package, name="另一人")
        others["meta"]["card_id"] = "cc-另一人"
        others["channels"] = [{"source_id": "src-2", "conditions": "退潮后实地拓印"}]
        env.world.add_character(iid, tl, others, now_real=base + 2 * DAY, note="审计第二角色")
        session_b = env.store.session_ensure(iid, tl, "cc-另一人")
        prompt_b = env.world.system_prompt(session_b)
        check("§14/附录B#8 角色间对话不共享：B 的上下文里没有 A 与用户的对话",
              "A 的对话正文不出现在 B 的系统提示里",
              f"B 提示含 A 正文={'SECRET-DIALOG' in prompt_b}",
              "PASS" if "SECRET-DIALOG" not in prompt_b else "FAIL",
              code_ref="isekai_core/runtime/service.py:2965-2998、cognition.py:304-311")

        # 未接触来源者不能获知：A（src-1）能拿到信报说法，B（src-2）拿不到
        claims = env.store.claim_list(iid, tl)
        grants_a = [row for row in env.store.knowledge_window(iid, tl, cid, until=10**12, limit=900)
                    if str(row["kind"]) == "claim"]
        grants_b = [row for row in env.store.knowledge_window(iid, tl, "cc-另一人", until=10**12, limit=900)
                    if str(row["kind"]) == "claim"]
        src_a = {str(row["source"]) for row in grants_a}
        src_b = {str(row["source"]) for row in grants_b}
        check("§13.4 条2/附录B#8 未接触史料来源者不能获知（渠道校验）",
              "A 的信报类获知只走 src-1；B（只有 src-2）不出现 src-1 获知",
              f"总说法={len(claims)}，A 获知来源={sorted(src_a)}，B 获知来源={sorted(src_b)}，"
              f"A 获知={len(grants_a)}，B 获知={len(grants_b)}",
              "PASS" if "src-1" not in src_b and (not grants_b or src_b <= {"src-2"}) else "FAIL",
              code_ref="isekai_core/runtime/events.py:233-291、claim_grant:300-322")

        # 认知切片只含 ≤ 水位的内容，且「可能听到」不算「已经听到」
        slice_before_death = env.world.character_snapshot(iid, tl, cid, world_seconds=DAY * 1500 + 1)
        future_rows = [row for row in slice_before_death["experiences"]
                       if int(row["world_seconds"]) > DAY * 1500 + 1]
        check("§13.1 输入必含查询水位；输出只含截至该水位已可接触的子集",
              "水位=初始时刻+1 时，经历 / 获知里没有更晚的行",
              f"早水位下经历={len(slice_before_death['experiences'])}，越界={len(future_rows)}",
              "PASS" if not future_rows else "FAIL",
              code_ref="isekai_core/runtime/service.py:2481-2572、cognition.py:35-168")

        # §12 条2：同一事件 / 说法同时供认知、经历使用，消费方不另造文本
        claims_text = {str(row["id"]): str(row["text"]) for row in env.store.claim_list(iid, tl)}
        event_text = {str(row["id"]): str(row["summary"]) for row in
                      env.store.event_window(iid, tl, until=10**12, limit=900)}
        knowledge_rows = env.store.knowledge_window(iid, tl, cid, until=10**12, limit=900)
        mismatched = []
        for row in knowledge_rows:
            kind = str(row["kind"])
            source_text = claims_text.get(str(row["target"])) if kind == "claim" else event_text.get(str(row["target"]))
            if source_text is None or str(row.get("text")) != source_text:
                mismatched.append(str(row["id"]))
        check("§12 条2/§13.1 同一事件与说法同时供认知使用；各消费方不分别生成文本",
              "获知行的正文与其来源（事件摘要 / 说法原文）逐字一致、指向同一标识",
              f"获知行={len(knowledge_rows)}（claim {sum(1 for r in knowledge_rows if str(r['kind']) == 'claim')} 条 / "
              f"observation {sum(1 for r in knowledge_rows if str(r['kind']) == 'observation')} 条），"
              f"文本不一致={len(mismatched)}",
              "PASS" if knowledge_rows and not mismatched else "FAIL",
              code_ref="isekai_core/runtime/events.py:233-291、store.py:2437-2450")


# --------------------------------------------------------------- §2.8 调用预算


def c_budget() -> None:
    with scenario("budget") as env:
        info, tl, cid, _pkg = mk(env)
        iid = info["id"]
        base = 1_700_000_000.0
        env.world.activate(iid, tl, now_real=base)
        now = 1_700_000_000.0
        bucket = int(now // 86400)

        # 三层逐层拒绝（该桶还没记过账）
        env.store.budget_policy_set(iid, instance_tokens_per_day=1000,
                                    timeline_tokens_per_day=800, task_tokens_per_day=500)
        instance_blocked = env.world.reserve_call(iid, tl, "memory_extract", tokens_est=2000, now_real=now)
        line_blocked = env.world.reserve_call(iid, tl, "memory_extract", tokens_est=900, now_real=now)
        check("§2.8 条2 预算分三层（实例 / 时间线 / 单任务），任一层不够即拒",
              "超实例与超时间线的预占被拒并标出层",
              f"实例层={instance_blocked.get('blocked')}，时间线层={line_blocked.get('blocked')}",
              "PASS" if (instance_blocked.get("ok") is False and "instance" in instance_blocked["blocked"]
                         and line_blocked.get("ok") is False and "timeline" in line_blocked["blocked"]) else "FAIL",
              code_ref="isekai_core/runtime/budget.py:56-94、store.py:2949-2990")

        ok = env.world.reserve_call(iid, tl, "memory_extract", tokens_est=100, now_real=now)
        settled = env.world.settle_call(ok, prompt_text="审计提问正文", reply="审计回答正文", outcome="timeout")
        rows = env.store.budget_rows(iid, bucket=bucket)
        raw = json.dumps(rows, ensure_ascii=False, default=str)
        usage = env.store.budget_usage(iid, bucket=bucket)
        check("§2.8 条2/条4 预占成功即记账；失败 / 超时都按真实消耗结算；账本不含正文",
              "结算后有 calls/tokens 量级，账本与用量里没有 prompt / 回复正文",
              f"账本行={rows}，用量={usage}，正文出现在账本={'审计提问' in raw or '审计回答' in raw}",
              "PASS" if (settled and rows and "审计提问" not in raw and "审计回答" not in raw) else "FAIL",
              code_ref="isekai_core/store.py:3068-3100、2986-3019")

        # 低优先级被「给更高优先级保留的额度」挡住（换一个干净的现实日桶）
        later = now + 5 * 86400
        env.store.budget_policy_set(iid, instance_tokens_per_day=1000,
                                    timeline_tokens_per_day=5000, task_tokens_per_day=5000)
        env.store.call_ledger_add(iid, tl, "safety", bucket=int(later // 86400), calls=1, tokens=800)
        low = env.world.reserve_call(iid, tl, "embedding", tokens_est=150, now_real=later)
        high = env.world.reserve_call(iid, tl, "safety", tokens_est=100, now_real=later)
        check("§2.8 条3 优先级固定：预算紧张时先停最低优先级的新调用",
              "embedding（最低档）被 reserved_for_higher 拦下，高优先级仍可发起",
              f"embedding={ {k: low.get(k) for k in ('ok', 'blocked')} }，"
              f"safety={ {k: high.get(k) for k in ('ok', 'blocked')} }",
              "PASS" if (low.get("ok") is False and "reserved_for_higher" in low.get("blocked", [])
                         and high.get("ok") is True) else "FAIL",
              code_ref="isekai_core/runtime/budget.py:14-28、80-83")

        # 预算耗尽 / 暂停：事实推进继续，派生任务不重复调用
        env.store.budget_policy_set(iid, paused_tasks=["proactive_text"])
        paused = env.world.reserve_call(iid, tl, "proactive_text", tokens_est=10, now_real=now)
        progress = env.world.advance(iid, tl, now_real=base + DAY, max_batches=10)
        check("§2.8 条5/附录B#23 预算耗尽 / 暂停：事实推进继续，派生任务不重复调用",
              "proactive_text 预占被拒（paused），世界推进照常",
              f"paused={paused.get('blocked')}，推进 state={progress['state']} "
              f"watermark={progress['processed_world']}",
              "PASS" if paused.get("ok") is False and progress["processed_world"] > DAY * 1500 else "FAIL",
              code_ref="isekai_core/runtime/service.py:1247-1281、1603-1712")

        # 跨端导入不带本机预算消耗与凭据
        env.store.call_ledger_add(iid, tl, "memory_extract", bucket=bucket, calls=3, tokens=333)
        dump = env.store.runtime_dump(iid, tl, watermark=10**12)
        payload = json.dumps(dump, ensure_ascii=False, default=str)
        check("§2.8 条6 跨端导入不带本机预算消耗与凭据（只带必要的待处理逻辑状态）",
              "runtime_dump 不含 call_ledger / budget_policy 数据",
              f"dump 含 call_ledger={'call_ledger' in payload}，含 paused_tasks={'paused_tasks' in payload}",
              "PASS" if "call_ledger" not in payload and "paused_tasks" not in payload else "FAIL",
              code_ref="isekai_core/store.py:2163-2272")


# --------------------------------------------------------------- WS 真核心


async def ws_core_checks() -> None:
    with tempfile.TemporaryDirectory(prefix="wr-audit-core-") as tmp:
        cfg = load_config(tmp)
        fake = FakeLLM(["收到。"])
        runtime = await build_runtime(cfg, llm=fake)
        endpoint = await runtime.server.start()
        try:
            mgmt = MgmtClient(endpoint, runtime.server.mgmt_token)
            await mgmt.connect()
            # 实例时刻选在「值守」时段，避开睡眠期等待（SESSION_CORE_SPEC §4.5 的 30–120 秒等待）
            package = example_package(moment=DAY * 1500 + 40000)
            card = example_card(package)
            created = await mgmt.call("instance.create", package=package, cards=[card])
            iid = created["instance"]["id"]
            info = await mgmt.call("instance.info", id=iid)
            tl = info["timelines"][0]["id"]
            world = runtime.world

            # §2.2 rate_max 全局配置
            cfg_rmax = cfg.runtime.rate_max
            bad = None
            try:
                await mgmt.call("runtime.rate", instance_id=iid, timeline_id=tl, rate=0)
            except UmpError as exc:
                bad = exc.code
            check("§2.2 条2 倍率上限是世界级配置、管理面路径同样拒绝非法倍率",
                  f"rate_max={cfg_rmax}，rate=0 经 WS 管理面被拒",
                  f"service.rate_max={world.rate_max}，WS 拒绝码={bad}",
                  "PASS" if world.rate_max == cfg_rmax == 2592000 and bad is not None else "FAIL",
                  code_ref="isekai_core/runtime/service.py:1478-1482、world/ops.py:245-247")

            # 真实 WS 对话：环境真值不被文本改写；对话水位取已完成水位
            issued = await mgmt.call("channel.ensure", name="builtin", capabilities={})
            session = (await mgmt.call("session.ensure", instance_id=iid, timeline_id=tl,
                                       character_id="cc-堤禾"))["session"]
            thread = (await mgmt.call("thread.bind", channel="builtin", thread_id="dm-audit",
                                      session_id=session["id"]))["thread"]
            client = UmpClient(endpoint=endpoint, channel_id="builtin", name="builtin",
                               credential=issued["credential"], status=True, segments=True)
            await client.connect()
            try:
                await mgmt.call("runtime.activate", instance_id=iid, timeline_id=tl)
                env_before = {row["type_id"]: str(row["value"])
                              for row in runtime.store.environment_list(iid, tl)}
                plan_before = {row["id"]: str(row["windows"])
                               for row in [runtime.store.plan_latest(iid, tl, "cc-堤禾")] if row}
                awaiting: list[Any] = []
                await client.send(make("user_message", {"text": "外面下雨了吗？"},
                                       thread_id="dm-audit", binding_token=thread["binding_token"],
                                       id="e-ws-1"))
                reply = await client.expect(lambda item: item.type == "reply", timeout=150, collect=awaiting)
                env_after = {row["type_id"]: str(row["value"])
                             for row in runtime.store.environment_list(iid, tl)}
                plan_after = {row["id"]: str(row["windows"])
                              for row in [runtime.store.plan_latest(iid, tl, "cc-堤禾")] if row}
                check("§11.2 条2 真 WS 对话不改变环境真值（语言生成改不动环境）",
                      "对话前后环境行不变，且回复成功返回",
                      f"环境 {env_before}→{env_after}，回复={'有' if reply else '无'}，"
                      f"回复正文={reply.payload.get('batches') if reply else None}，"
                      f"计划未改={plan_before == plan_after}",
                      "PASS" if (env_before == env_after and reply is not None
                                 and plan_before == plan_after) else "FAIL",
                      code_ref="isekai_core/runtime/environment.py:98-129、session.py:88-147")

                # §2.6 条5：目标时刻远领先时，对话应捕获目标水位并等待推进（或报告追赶中）
                await mgmt.call("runtime.rate", instance_id=iid, timeline_id=tl, rate=100000)
                await asyncio.sleep(1.3)  # 让该倍率命令越过生效整秒，世界目标确实远远领先
                lag_started = time.time()
                await client.send(make("user_message", {"text": "现在是什么时辰？"},
                                       thread_id="dm-audit", binding_token=thread["binding_token"],
                                       id="e-ws-3"))
                lag_events: list[Any] = []
                lagged = await client.expect(
                    lambda item: item.type in ("reply", "error", "system_notice"), timeout=60, collect=lag_events)
                lag_elapsed = time.time() - lag_started
                now_view = world.view(iid, tl, now_real=time.time())
                lag = int(now_view.get("world_seconds") or 0) - int(now_view.get("processed_world") or 0)
                check("§2.6 条5 目标水位远领先时：对话捕获目标水位、待推进后再取快照；超预算报「追赶中」",
                      "回复应等待世界推进到接收时的目标水位，或明确报告追赶中",
                      f"目标={now_view.get('world_seconds')} vs 已完成={now_view.get('processed_world')}"
                      f"（滞后 {lag} 世界秒）；回信耗时={lag_elapsed:.2f}s，信封类型={lagged.type}"
                      + ("（滞后中仍直接作答、无追赶提示）" if lagged.type == "reply" and lag > 100000 else ""),
                      "PASS" if (lagged.type != "reply" or lag_elapsed > 30) else "FAIL",
                      code_ref="isekai_core/runtime/service.py:2948-2950、session.py:434")

                # 冻结线：管理面视图只显示已冻结，且冻结线不再推进、不接受对话
                await mgmt.call("runtime.freeze", instance_id=iid, timeline_id=tl)
                clock = await mgmt.call("runtime.clock", instance_id=iid, timeline_id=tl)
                advanced = await mgmt.call("runtime.advance", instance_id=iid, timeline_id=tl)
                fenced: list[Any] = []
                await client.send(make("user_message", {"text": "还在吗？"},
                                       thread_id="dm-audit", binding_token=thread["binding_token"],
                                       id="e-ws-2"))
                refusal = await client.expect(lambda item: item.type in ("error", "system_notice"),
                                              timeout=30, collect=fenced)
                check("§3 条3/§4 条2 冻结线仅显示「已冻结」、不继续跳时，也不接受新对话",
                      "clock.state=frozen、label=已冻结；advance 返回 frozen；对话被拒（error 信封）",
                      f"clock.state={clock['clock'].get('state')}，label={clock['clock'].get('label')}，"
                      f"advance={advanced['advance']}，对话拒绝={refusal.type}/"
                      f"{refusal.payload.get('code')}",
                      "PASS" if (clock["clock"].get("state") == "frozen"
                                 and clock["clock"].get("label") == "已冻结"
                                 and advanced["advance"]["state"] == "frozen"
                                 and refusal.type == "error") else "FAIL",
                      code_ref="isekai_core/runtime/service.py:1558-1595、session.py:117-126")

                # 导出 / 导入：只带世界运行状态，不带本机激活状态与通道绑定
                export_path = Path(tmp) / "export.bin"
                source_watermark = runtime.store.clock_get(tl)["processed_world"]
                source_active = runtime.store.timeline_get(tl)["state"]
                exported = await mgmt.call("instance.export", id=iid, path=str(export_path))
                await mgmt.call("runtime.activate", instance_id=iid, timeline_id=tl)
                imported = await mgmt.call("instance.import", path=str(export_path))
                new_id = imported["instance"]["id"]
                new_info = await mgmt.call("instance.info", id=new_id)
                new_states = {row["id"]: row["state"] for row in new_info["timelines"]}
                new_clock = runtime.store.clock_get(new_info["timelines"][0]["id"])
                check("§2.6 条8/附录B#14 导出只含世界运行状态（不含激活集合），导入后先冻结并重锚",
                      "导入实例的全部线为 frozen，水位 = 导出时水位",
                      f"导出时本线 state={source_active}；导入线状态={new_states}；"
                      f"导出水位={source_watermark}，导入水位={new_clock['processed_world']}",
                      "PASS" if (set(new_states.values()) == {"frozen"}
                                 and int(new_clock["processed_world"]) == int(source_watermark)) else "FAIL",
                      code_ref="isekai_core/world/portable.py、ops.py:492-500")

                # 导入实例重新激活：以当下现实时间锚定，不补算导出日至今
                new_tl = new_info["timelines"][0]["id"]
                now_real = time.time()
                reactivated = await mgmt.call("runtime.activate", instance_id=new_id, timeline_id=new_tl)
                gap = await mgmt.call("runtime.clock", instance_id=new_id, timeline_id=new_tl)
                check("§2.6 条8 导入后激活：从备份内世界时刻重新锚定、不补算备份日至今的间隔",
                      "激活后世界时刻 = 导出水位（不随现实间隔增长）",
                      f"激活 world={gap['clock'].get('world_seconds')}，导出水位={source_watermark}",
                      "PASS" if int(gap["clock"].get("world_seconds") or -1) == int(source_watermark) else "FAIL",
                      code_ref="isekai_core/runtime/service.py:1399-1443")
            finally:
                await client.close()
            await mgmt.close()
        finally:
            await runtime.service.shutdown()
            await runtime.server.close()
            runtime.store.close()


# --------------------------------------------------------------- 主流程


def run_sync() -> None:
    for fn in (
        c_clock_pure,
        c_rate_service,
        c_rate_persist_restart,
        c_multiline,
        c_catchup_equivalence,
        c_catchup_restart_idempotent,
        c_limited,
        c_persistence_fault,
        c_backup_restore,
        c_versioning,
        c_patch_card_rollback,
        c_compression_and_diff,
        c_personality,
        c_life_and_death,
        c_environment,
        c_misclaimed_era,
        c_intents,
        c_short_term_reactions,
        c_cognition,
        c_budget,
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            check(f"探针内部错误 {fn.__name__}", "该组检查全部完成", f"{type(exc).__name__}: {exc}", "FAIL",
                  evidence=traceback.format_exc()[-600:], code_ref="scripts/_audit2_wr.py")


def main() -> int:
    logging.getLogger("isekai").setLevel(logging.CRITICAL)
    run_sync()
    try:
        asyncio.run(ws_core_checks())
    except Exception as exc:  # noqa: BLE001
        import traceback

        check("真核心 WS 组检查", "WS 场景执行完毕", f"{type(exc).__name__}: {exc}", "FAIL",
              evidence=traceback.format_exc()[-600:], code_ref="scripts/_audit2_wr.py")

    counts = {"PASS": 0, "FAIL": 0, "DEFERRED": 0}
    for row in RESULTS:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    payload = {
        "spec": SPEC,
        "probe_script": "scripts/_audit2_wr.py",
        "total": len(RESULTS),
        **counts,
        "findings": RESULTS,
    }
    print("=" * 60)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
