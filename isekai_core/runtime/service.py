"""世界运行层服务：时间线时钟、倍率、水位推进与角色状态。

职责（WORLD_RUNTIME_SPEC §2、§4、§10、§11）：
- 每线独立的时钟与倍率；激活即重锚（冻结期间不补算），冻结先结算已生效段并取消未生效请求；
- 水位推进按世界日分批、每批原子提交，重复执行不重复产生经历（幂等）；
- 角色状态（性格单元 / 生活线 / 经历）落库在运行层，会话层经认知接口只拿切片。
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from ..log import get_logger
from ..store import Store
from . import cognition, life, personality
from .calendar import Calendar, calendar_from_package
from .clock import DEFAULT_RATE_MAX, ClockState, RateCommand, describe, natural_second, settle, target_world

log = get_logger("isekai.runtime")

class RuntimeStateError(ValueError):
    """运行层拒绝该操作：状态不允许或参数非法。"""


class RuntimeService:
    def __init__(self, store: Store, *, rate_max: int | None = None) -> None:
        self.store = store
        #: 倍率上限：全局统一、仅开发者可配置（§2.2）
        self.rate_max = int(rate_max or DEFAULT_RATE_MAX)

    # ---------- 基础读取 ----------

    def _rows(self, instance_id: str, timeline_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        instance = self.store.instance_get(instance_id)
        if instance is None:
            raise RuntimeStateError(f"实例不存在：{instance_id}")
        timeline = next(
            (item for item in self.store.timeline_list(instance_id) if item["id"] == timeline_id), None
        )
        if timeline is None:
            raise RuntimeStateError(f"时间线不存在：{timeline_id}")
        return instance, timeline

    def setting(self, instance: dict[str, Any]) -> dict[str, Any]:
        return json.loads(instance["setting"])

    def calendar(self, instance: dict[str, Any]) -> Calendar:
        return calendar_from_package(self.setting(instance)["world_package"])

    def cards(self, instance: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self.setting(instance).get("cards") or [])

    def clock_row(self, timeline_id: str) -> dict[str, Any]:
        row = self.store.clock_get(timeline_id)
        if row is None:
            raise RuntimeStateError(f"时间线没有时钟记录：{timeline_id}")
        return row

    def state_of(self, row: dict[str, Any]) -> ClockState:
        return ClockState(
            base_real=float(row["base_real"]),
            base_world=int(row["base_world"]),
            rate=int(row["rate"]),
            high_water_real=float(row["high_water_real"]),
        )

    # ---------- 生命周期 ----------

    def init_timeline(self, instance_id: str, timeline_id: str, *, moment: int, now_real: float) -> dict[str, Any]:
        """创建 / 导入时为每条线建立时钟记录：冻结、倍率 1、水位 = 初始世界时刻。"""
        row = {
            "timeline_id": timeline_id,
            "base_real": float(now_real),
            "base_world": int(moment),
            "rate": 1,
            "high_water_real": float(now_real),
            "anchor_real": float(now_real),
            "processed_world": int(moment),
            "generation": 1,
        }
        self.store.clock_put(row)
        return row

    def activate(self, instance_id: str, timeline_id: str, *, now_real: float) -> dict[str, Any]:
        """激活：以当前现实时间重新锚定，不补算冻结期间的间隔（§2.4、§2.6）。"""
        _, timeline = self._rows(instance_id, timeline_id)
        if timeline["state"] == "active":
            return self.view(instance_id, timeline_id, now_real=now_real)
        row = self.clock_row(timeline_id)
        world_at_freeze = target_world(self.state_of(row), float(row["anchor_real"]))
        self.store.clock_put(
            {
                **row,
                "base_real": float(now_real),
                "base_world": int(world_at_freeze),
                "high_water_real": float(now_real),
                "anchor_real": float(now_real),
                "processed_world": max(int(row["processed_world"]), int(world_at_freeze)),
            }
        )
        self.store.timeline_set_state(timeline_id, "active")
        return self.view(instance_id, timeline_id, now_real=now_real)

    def freeze(self, instance_id: str, timeline_id: str, *, now_real: float) -> dict[str, Any]:
        """冻结：结算已生效倍率段、取消未生效请求；冻结线不推进也不接受倍率调整。"""
        row = self.clock_row(timeline_id)
        state, consumed = self._settle_due(timeline_id, row, now_real)
        self.store.rate_apply(consumed)
        cancelled = self.store.rate_cancel_pending(timeline_id)
        world = target_world(state, now_real)
        self.store.clock_put(
            {
                **row,
                "base_real": float(now_real),
                "base_world": int(world),
                "high_water_real": max(float(row["high_water_real"]), float(now_real)),
                "anchor_real": float(now_real),
                "processed_world": max(int(row["processed_world"]), int(world)),
            }
        )
        self.store.timeline_set_state(timeline_id, "frozen")
        instance = self.store.instance_get(instance_id) or {}
        calendar = self.calendar(instance) if instance else None
        return {
            "instance": instance_id,
            "timeline": timeline_id,
            "state": "frozen",
            "world_seconds": int(world),
            "label": calendar.describe(int(world)) if calendar else "",
            "processed_world": int(max(int(row["processed_world"]), int(world))),
            "cancelled_commands": cancelled,
        }

    def _settle_due(
        self, timeline_id: str, row: dict[str, Any], now_real: float
    ) -> tuple[ClockState, list[int]]:
        pending = self.store.rate_pending(timeline_id)
        id_of: dict[int, int] = {}
        commands: list[RateCommand] = []
        for item in pending:
            command = RateCommand(
                input_real=float(item["input_real"]),
                effective_real=int(item["effective_real"]),
                rate=int(item["rate"]),
                seq=int(item["seq"]),
            )
            id_of[id(command)] = int(item["id"])
            commands.append(command)
        state, consumed = settle(self.state_of(row), now_real, commands)
        ids = [id_of[id(command)] for command in consumed if id(command) in id_of]
        if ids or self.state_of(row) != state:
            self.store.clock_put(
                {
                    **row,
                    "base_real": state.base_real,
                    "base_world": state.base_world,
                    "rate": state.rate,
                    "high_water_real": max(float(row["high_water_real"]), state.high_water_real),
                }
            )
        return state, ids

    # ---------- 倍率 ----------

    def set_rate(self, instance_id: str, timeline_id: str, *, rate: int, now_real: float) -> dict[str, Any]:
        """倍率调整：权威输入时刻 → 严格晚于它的第一个自然整秒生效；先持久化再确认（§2.3）。"""
        _, timeline = self._rows(instance_id, timeline_id)
        if timeline["state"] != "active":
            raise RuntimeStateError("冻结线不接受倍率调整（需先激活）")
        if not isinstance(rate, int) or isinstance(rate, bool) or not 1 <= rate <= self.rate_max:
            raise RuntimeStateError(f"倍率必须是 1..{self.rate_max} 的整数")
        row = self.clock_row(timeline_id)
        state, consumed = self._settle_due(timeline_id, row, now_real)
        self.store.rate_apply(consumed)
        if int(row["rate"]) == rate and not self.store.rate_pending(timeline_id):
            return {"changed": False, "rate": rate, "effective_real": None, "command_id": None}
        effective = natural_second(now_real)
        pending = self.store.rate_pending(timeline_id)
        same = next(
            (
                item
                for item in pending
                if int(item["effective_real"]) == effective and int(item["rate"]) == rate
            ),
            None,
        )
        if same is not None:  # 重试同一请求：不产生第二次变更
            return {
                "changed": True,
                "rate": rate,
                "effective_real": effective,
                "command_id": int(same["id"]),
                "duplicate": True,
            }
        seq = 1 + max([int(item["seq"]) for item in pending], default=0)
        command_id = self.store.rate_add(
            timeline_id, input_real=float(now_real), effective_real=effective, rate=rate, seq=seq
        )
        return {
            "changed": True,
            "rate": rate,
            "effective_real": effective,
            "command_id": command_id,
            "rate_at_effect": int(state.rate),
        }

    # ---------- 视图 ----------

    def view(self, instance_id: str, timeline_id: str, *, now_real: float) -> dict[str, Any]:
        """管理面 / 通道可见的时钟视图：只有当前查看且激活的线才给时钟（§3）。"""
        instance, timeline = self._rows(instance_id, timeline_id)
        calendar = self.calendar(instance)
        row = self.clock_row(timeline_id)
        state = self.state_of(row)
        if timeline["state"] != "active":
            return {
                "instance": instance_id,
                "timeline": timeline_id,
                "state": "frozen",
                "processed_world": int(row["processed_world"]),
                "label": "已冻结",
            }
        # 视图按「已到期的倍率命令已生效」投影，但不落库（写入发生在 activate / advance / freeze）
        pending = [
            RateCommand(
                input_real=float(item["input_real"]),
                effective_real=int(item["effective_real"]),
                rate=int(item["rate"]),
                seq=int(item["seq"]),
            )
            for item in self.store.rate_pending(timeline_id)
        ]
        state, _ = settle(state, now_real, pending)
        world = target_world(state, now_real)
        return {
            "instance": instance_id,
            "timeline": timeline_id,
            "state": "active",
            "world_seconds": world,
            "calendar": calendar.to_calendar(world),
            "label": calendar.describe(world),
            "rate": state.rate,
            "processed_world": int(row["processed_world"]),
            "catching_up": int(row["processed_world"]) < world,
            "clock": describe(state, now_real),
        }

    # ---------- 水位推进 ----------

    def advance(
        self, instance_id: str, timeline_id: str, *, now_real: float, max_batches: int = 16
    ) -> dict[str, Any]:
        """把水位从已处理时刻推进到目标时刻，按世界日分批、每批原子（§2.6）。"""
        instance, timeline = self._rows(instance_id, timeline_id)
        if timeline["state"] != "active":
            return {"state": "frozen", "processed_world": int(self.clock_row(timeline_id)["processed_world"])}
        row = self.clock_row(timeline_id)
        state, consumed = self._settle_due(timeline_id, row, now_real)
        self.store.rate_apply(consumed)
        row = self.clock_row(timeline_id)
        target = target_world(state, now_real)
        processed = int(row["processed_world"])
        if target <= processed:
            return {"state": "current", "processed_world": processed, "target": target, "batches": 0}
        calendar = self.calendar(instance)
        cards = self.cards(instance)
        generation = int(row["generation"])
        produced = 0
        batches = 0
        while processed < target and batches < max_batches:
            day = calendar.day_index(processed)
            stop = min(target, (day + 1) * calendar.day_seconds)
            produced += self._step_batch(
                instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop, generation=generation
            )
            if not self.store.clock_set_processed(timeline_id, stop, generation=generation):
                break  # 世代不符：本批失效，交给下一轮
            processed = stop
            batches += 1
        return {
            "state": "current" if processed >= target else "catching_up",
            "processed_world": processed,
            "target": target,
            "batches": batches,
            "experiences": produced,
        }

    def _step_batch(
        self,
        instance_id: str,
        timeline_id: str,
        cards: list[dict[str, Any]],
        calendar: Calendar,
        *,
        from_world: int,
        to_world: int,
        generation: int,
    ) -> int:
        """一批：准备次日计划、按世界时长衰减、把已完成的窗口记为经历。全部先写库再报进度。"""
        produced = 0
        day_seconds = calendar.day_seconds
        for card in cards:
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id:
                continue
            # 当前日与下一日的计划（世界日界由历法决定，不由入睡重新定义）
            for day_index in {calendar.day_index(from_world), calendar.day_index(to_world)}:
                if self.store.plan_get(instance_id, timeline_id, character_id, day_index) is None:
                    self.store.plan_put(
                        life.expand_plan(
                            card,
                            calendar,
                            day_index=day_index,
                            instance_id=instance_id,
                            timeline_id=timeline_id,
                            created_world=from_world,
                        )
                    )
            units = self.store.unit_list(instance_id, timeline_id, character_id)
            if units:
                for row in personality.apply_time(
                    units, from_world=from_world, to_world=to_world, day_seconds=day_seconds
                ):
                    self.store.unit_put(row)
            plan = self.store.plan_get(instance_id, timeline_id, character_id, calendar.day_index(from_world))
            if plan is not None:
                try:
                    windows = json.loads(str(plan["windows"])).get("windows", [])
                except json.JSONDecodeError:
                    windows = []
                for window in windows:
                    if not (from_world < int(window["end"]) <= to_world):
                        continue
                    self.store.experience_add(
                        {
                            "id": f"xp-{character_id}-{int(window['start'])}",
                            "instance_id": instance_id,
                            "timeline_id": timeline_id,
                            "character_id": character_id,
                            "world_seconds": int(window["end"]),
                            "kind": "life",
                            "summary": f"{window.get('activity') or 'activity'}（{calendar.describe(int(window['start']))}）",
                            "source_ref": None,
                            "confidence": "experienced",
                        }
                    )
                    produced += 1
        return produced

    # ---------- 角色状态 ----------

    def bootstrap(self, instance_id: str, timeline_id: str, *, world_seconds: int) -> dict[str, int]:
        """创建 / 导入后把角色卡的初始单元、首日计划落进运行层。"""
        instance, _ = self._rows(instance_id, timeline_id)
        calendar = self.calendar(instance)
        units = plans = 0
        for card in self.cards(instance):
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id:
                continue
            if not self.store.unit_list(instance_id, timeline_id, character_id):
                for row in personality.initial_rows(
                    card, instance_id=instance_id, timeline_id=timeline_id, world_seconds=world_seconds
                ):
                    self.store.unit_put(row)
                    units += 1
            day_index = calendar.day_index(world_seconds)
            if self.store.plan_get(instance_id, timeline_id, character_id, day_index) is None:
                self.store.plan_put(
                    life.expand_plan(
                        card,
                        calendar,
                        day_index=day_index,
                        instance_id=instance_id,
                        timeline_id=timeline_id,
                        created_world=world_seconds,
                    )
                )
                plans += 1
        return {"units": units, "plans": plans}

    def character_snapshot(
        self, instance_id: str, timeline_id: str, character_id: str, *, world_seconds: int
    ) -> dict[str, Any]:
        """内部读取（会话 / 认知用）：单元与计划的最新视图，不对外暴露数值面板。"""
        units = self.store.unit_list(instance_id, timeline_id, character_id)
        plan = self.store.plan_latest(instance_id, timeline_id, character_id)
        experiences = self.store.experience_window(
            instance_id, timeline_id, character_id, until=world_seconds, limit=12
        )
        return {
            "units": personality.visible(units),
            "all_units": units,
            "plan": plan,
            "current_activity": life.activity_label(life.current_window(plan, world_seconds)),
            "experiences": experiences,
        }

    def ensure_instance(self, instance_id: str, *, now_real: float) -> dict[str, int]:
        """补齐运行层状态：缺时钟的时间线建时钟，缺角色状态的按初始水位补齐（幂等）。"""
        instance = self.store.instance_get(instance_id)
        if instance is None:
            raise RuntimeStateError(f"实例不存在：{instance_id}")
        clocks = 0
        for timeline in self.store.timeline_list(instance_id):
            row = self.store.clock_get(timeline["id"])
            if row is None:
                row = self.init_timeline(
                    instance_id, timeline["id"], moment=int(instance["moment"]), now_real=now_real
                )
                clocks += 1
            self.bootstrap(
                instance_id,
                timeline["id"],
                world_seconds=max(int(instance["moment"]), int(row["processed_world"])),
            )
        return {"timelines": len(self.store.timeline_list(instance_id)), "created_clocks": clocks}

    def active_timelines(self) -> list[tuple[str, str]]:
        """所有处于激活状态的时间线（实例, 时间线）。"""
        pairs = []
        for instance in self.store.instance_list():
            for timeline in self.store.timeline_list(instance["id"]):
                if timeline["state"] == "active":
                    pairs.append((instance["id"], timeline["id"]))
        return pairs

    def catch_up_all(self, *, now_real: float, max_batches: int = 8) -> dict[str, Any]:
        """推进所有激活线（核心启动与周期 tick 用；冻结线自动跳过）。"""
        advanced: dict[str, Any] = {}
        for instance_id, timeline_id in self.active_timelines():
            try:
                advanced[timeline_id] = self.advance(
                    instance_id, timeline_id, now_real=now_real, max_batches=max_batches
                )
            except Exception:  # 单线推进失败不影响其他线
                log.exception("advance failed timeline=%s", timeline_id)
        return advanced

    # ---------- 会话接入 ----------

    def world_moment(self, instance_id: str, timeline_id: str, *, now_real: float | None = None) -> int:
        """可对话的世界时刻 = 已处理水位（不把未推进的目标当既有状态，§2.6）。"""
        return int(self.clock_row(timeline_id)["processed_world"])

    def card_of(self, instance: dict[str, Any], character_id: str) -> dict[str, Any]:
        for card in self.cards(instance):
            if str((card.get("meta") or {}).get("card_id")) == character_id:
                return card
        raise RuntimeStateError(f"实例内没有该角色：{character_id}")

    def system_prompt(self, session: dict[str, Any], *, now_real: float | None = None) -> str:
        """会话层用的扮演定义：锁定设定 + 该角色截至当前水位的认知切片（无实情层注入）。"""
        import time as _time

        from . import cognition

        instance, _ = self._rows(session["instance_id"], session["timeline_id"])
        calendar = self.calendar(instance)
        world = self.world_moment(session["instance_id"], session["timeline_id"], now_real=now_real or _time.time())
        card = self.card_of(instance, str(session["character_id"]))
        snapshot = self.character_snapshot(
            session["instance_id"], session["timeline_id"], str(session["character_id"]), world_seconds=world
        )
        context = cognition.play_context(
            self.setting(instance)["world_package"],
            card,
            world_seconds=world,
            calendar_label=calendar.describe(world),
            current_activity=str(snapshot["current_activity"] or ""),
            units=snapshot["units"],
            experiences=snapshot["experiences"],
        )
        return cognition.render_prompt(context)
