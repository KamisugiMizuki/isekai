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
from . import cognition, environment, events, intents, life, personality, planning
from .calendar import Calendar, calendar_from_package
from .clock import DEFAULT_RATE_MAX, ClockState, RateCommand, describe, natural_second, settle, target_world

log = get_logger("isekai.runtime")

class RuntimeStateError(ValueError):
    """运行层拒绝该操作：状态不允许或参数非法。"""


class RuntimeService:
    def __init__(
        self,
        store: Store,
        *,
        rate_max: int | None = None,
        max_active_timelines: int = 4,
        catch_up_batches: int = 8,
        catch_up_lag_seconds: int = 0,
        render_calls_per_day: int = 20,
    ) -> None:
        self.store = store
        #: 倍率上限：全局统一、仅开发者可配置（§2.2），默认 2592000 世界秒 / 现实秒
        self.rate_max = int(rate_max or DEFAULT_RATE_MAX)
        #: 同时激活的时间线上限（§4）
        self.max_active_timelines = max(1, int(max_active_timelines))
        #: 单次推进的批数上限（§2.6 预算）
        self.catch_up_batches = max(1, int(catch_up_batches))
        #: 滞后超过该世界秒数即进入「追赶受限」（§2.6）；0 = 与目标同步才退出受限
        self.catch_up_lag_seconds = max(0, int(catch_up_lag_seconds))
        #: 表述 / 展开 / 自主提案的现实日调用上限（§2.8 单任务预算）
        self.render_calls_per_day = max(0, int(render_calls_per_day))

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

    def cards(
        self, instance: dict[str, Any], *, timeline_id: str | None = None, world_seconds: int | None = None
    ) -> list[dict[str, Any]]:
        """实例锁定的角色卡；给了线与水位时并入已补入的角色（§九 / 附录 B #18）。"""
        cards = list(self.setting(instance).get("cards") or [])
        if timeline_id is None or world_seconds is None:
            return cards
        joined = self.store.character_join_list(instance["id"], timeline_id, until=int(world_seconds))
        for row in joined:
            try:
                cards.append(json.loads(str(row["card"])))
            except json.JSONDecodeError:
                log.warning("补入角色卡损坏 card=%s", row.get("character_id"))
        return cards

    def seed_of(self, instance: dict[str, Any]) -> str:
        """锁定种子：候选抽样只依赖它 + 规则版本 + 历法日 + 槽序（附录 A）。"""
        return str(instance.get("seed") or "")

    def rules_of(self, instance: dict[str, Any]) -> str:
        return str(instance.get("rules_version") or "")

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

    def activate(
        self, instance_id: str, timeline_id: str, *, now_real: float, rate: int | None = None
    ) -> dict[str, Any]:
        """激活：以当前现实时间重新锚定，不补算冻结期间的间隔（§2.4、§2.6）。

        倍率超过当前上限（上限被调低 / 导入端上限更低）时不静默改写：保持冻结，
        要求调用方在激活操作中确认一个合法倍率（§2.4）。
        """
        instance, timeline = self._rows(instance_id, timeline_id)
        if timeline["state"] == "active":
            return self.view(instance_id, timeline_id, now_real=now_real)
        active = self.active_timelines()
        if len(active) >= self.max_active_timelines:
            raise RuntimeStateError(f"同时激活的时间线已达上限 {self.max_active_timelines} 条")
        row = self.clock_row(timeline_id)
        stored_rate = int(row["rate"])
        effective_rate = stored_rate
        if rate is not None:
            effective_rate = self._check_rate(rate)
        elif stored_rate > self.rate_max:
            raise RuntimeStateError(
                f"该线倍率 {stored_rate} 超过当前上限 {self.rate_max}，需要在激活时确认一个合法倍率"
            )
        world_at_freeze = target_world(self.state_of(row), float(row["anchor_real"]))
        self.store.rate_cancel_pending(timeline_id)  # 冻结期间的待生效请求不跨重启恢复（§2.3.6）
        self.store.clock_put(
            {
                **row,
                "base_real": float(now_real),
                "base_world": int(world_at_freeze),
                "rate": effective_rate,
                "high_water_real": float(now_real),
                "anchor_real": float(now_real),
                "processed_world": max(int(row["processed_world"]), int(world_at_freeze)),
                "generation": int(row["generation"]) + 1,  # 旧世代任务一律失效（§4）
                "catching_up": 0,
                "limited": 0,
            }
        )
        self.store.timeline_set_state(timeline_id, "active")
        result = self.view(instance_id, timeline_id, now_real=now_real)
        result["confirmed_rate"] = effective_rate if rate is not None else None
        _ = instance
        return result

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
                "generation": int(row["generation"]) + 1,  # 冻结使旧世代任务失效（§4）
                "catching_up": 0,
                "limited": 0,
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

    def _check_rate(self, rate: int) -> int:
        """倍率范围校验：正整数、受全局上限约束（§2.2）。"""
        if not isinstance(rate, int) or isinstance(rate, bool) or not 1 <= rate <= self.rate_max:
            raise RuntimeStateError(f"倍率必须是 1..{self.rate_max} 的整数")
        return int(rate)

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
        rate = self._check_rate(rate)
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
        self, instance_id: str, timeline_id: str, *, now_real: float, max_batches: int | None = None
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
        if processed > target:
            # 附录 B #20：处理水位超过合法目标另记为一致性错误，不静默回退、不假装追平
            log.error(
                "watermark ahead of target timeline=%s processed=%s target=%s", timeline_id, processed, target
            )
            return {"state": "inconsistent", "processed_world": processed, "target": target, "batches": 0}
        if target <= processed:
            if int(row.get("catching_up") or 0) or int(row.get("limited") or 0):
                self.store.clock_put({**row, "catching_up": 0, "limited": 0})
            return {"state": "current", "processed_world": processed, "target": target, "batches": 0}
        calendar = self.calendar(instance)
        cards = self.cards(instance, timeline_id=timeline_id, world_seconds=processed)
        generation = int(row["generation"])
        # 追赶受限（§2.6）：滞后超过预算即进入，停止扩大目标、只按已完成水位回答，不跳过事实效果
        limited = (target - processed) > self.catch_up_lag_seconds
        budget = self.catch_up_batches if max_batches is None else max(1, int(max_batches))
        produced = 0
        batches = 0
        while processed < target and batches < budget:
            day = calendar.day_index(processed)
            stop = min(target, (day + 1) * calendar.day_seconds)
            plans, units, experiences = self._collect_batch(
                instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            world_rows = self._world_event_rows(
                instance, instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            death_rows = self._death_rows(
                instance, instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            environment_rows = self._environment_rows(
                instance,
                instance_id,
                timeline_id,
                calendar,
                world_rows["effects"] + [dict(item) for item in []],
                from_world=processed,
                to_world=stop,
            )
            intent_rows = self._revise_intents(
                instance, instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            spread = self._propagate_and_clear(
                instance, instance_id, timeline_id, cards, calendar, from_world=processed, to_world=stop
            )
            committed = self.store.apply_runtime_batch(
                timeline_id=timeline_id,
                generation=generation,
                processed_world=stop,
                catching_up=stop < target,
                limited=limited,
                plans=plans,
                units=units,
                experiences=experiences + intent_rows['experiences'],
                events=world_rows['events'] + intent_rows['events'] + death_rows['events'],
                claims=world_rows['claims'] + death_rows['claims'],
                knowledge=world_rows['knowledge'] + death_rows['knowledge'] + spread['knowledge'],
                effects=world_rows['effects'] + intent_rows['effects'],
                intents=intent_rows['intents'],
                environment=environment_rows,
                clear_effects=spread['clear_effects'],
            )
            if not committed:
                # 世代已变（冻结 / 重启后迟到）或水位已被别的批次推过：本批整批不落盘
                return {
                    "state": "stale",
                    "processed_world": int(self.clock_row(timeline_id)["processed_world"]),
                    "target": target,
                    "batches": batches,
                }
            processed = stop
            batches += 1
            produced += len(experiences)
        return {
            "state": "current" if processed >= target else "catching_up",
            "processed_world": processed,
            "target": target,
            "batches": batches,
            "experiences": produced,
            "limited": limited and processed < target,
        }

    def _collect_batch(
        self,
        instance_id: str,
        timeline_id: str,
        cards: list[dict[str, Any]],
        calendar: Calendar,
        *,
        from_world: int,
        to_world: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """收集一批事实转移（不落盘）：事件与后果、次日计划、单元衰减、已完成窗口的经历。"""
        plans: list[dict[str, Any]] = []
        units: list[dict[str, Any]] = []
        experiences: list[dict[str, Any]] = []
        day_seconds = calendar.day_seconds
        dead = {
            str((card.get("meta") or {}).get("card_id"))
            for card in cards
            if events.is_dead(
                instance_id,
                timeline_id,
                str((card.get("meta") or {}).get("card_id") or ""),
                self.store.event_window(instance_id, timeline_id, until=to_world, limit=400),
            )
        }
        for card in cards:
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id or character_id in dead:
                continue  # 已身故的角色不再产生计划与经历
            # 当前日与下一日的计划（世界日界由历法决定，不由入睡重新定义）
            for day_index in {calendar.day_index(from_world), calendar.day_index(to_world)}:
                if self.store.plan_get(instance_id, timeline_id, character_id, day_index) is None:
                    plans.append(
                        life.expand_plan(
                            card,
                            calendar,
                            day_index=day_index,
                            instance_id=instance_id,
                            timeline_id=timeline_id,
                            created_world=from_world,
                        )
                    )
            rows = self.store.unit_list(instance_id, timeline_id, character_id)
            if rows:
                units.extend(
                    personality.apply_time(
                        rows, from_world=from_world, to_world=to_world, day_seconds=day_seconds
                    )
                )
            # 收割昨日与今日的计划：跨日窗口属于昨日，其尾部落在今日（§11 附录 B #7）
            day_here = calendar.day_index(from_world)
            constraints = self.store.effect_window(
                instance_id,
                timeline_id,
                until=to_world,
                targets=[character_id, str(card.get("role_id") or ""), str((card.get("identity") or {}).get("region") or "")],
            )
            note = life.effect_note(
                constraints,
                character_id,
                str(card.get("role_id") or ""),
                str((card.get("identity") or {}).get("region") or ""),
            )
            for day_index in (day_here - 1, day_here):
                plan = self.store.plan_get(instance_id, timeline_id, character_id, day_index)
                if plan is None:
                    continue
                try:
                    windows = json.loads(str(plan["windows"])).get("windows", [])
                except json.JSONDecodeError:
                    continue
                items = self._harvest(instance_id, timeline_id, character_id, calendar, windows, from_world, to_world)
                if note:
                    for item in items:
                        item["summary"] = f"{item['summary']}（受影响的后果：{note}）"
                        item["source_ref"] = constraints[0]["id"] if constraints else None
                experiences.extend(items)
        return plans, units, experiences

    def _environment_rows(
        self,
        instance: dict[str, Any],
        instance_id: str,
        timeline_id: str,
        calendar: Calendar,
        effects: list[dict[str, Any]],
        *,
        from_world: int,
        to_world: int,
    ) -> list[dict[str, Any]]:
        """环境状态推进（§11.2）：声明的自然变化 + 本批事件里的环境效果，同一批提交。"""
        package = self.setting(instance)["world_package"]
        types = environment.env_types(package)
        if not types:
            return []
        rows = self.store.environment_list(instance_id, timeline_id)
        if not rows:
            rows = environment.initial_rows(
                package, instance_id=instance_id, timeline_id=timeline_id, world_seconds=from_world
            )
        changed: dict[str, dict[str, Any]] = {}
        for row in environment.advance_rows(
            rows, types, from_world=from_world, to_world=to_world, day_seconds=calendar.day_seconds
        ):
            changed[str(row["type_id"])] = row
        for row in environment.apply_effects(rows, effects, types, world_seconds=to_world):
            changed[str(row["type_id"])] = row
        return list(changed.values())

    def _revise_intents(
        self,
        instance: dict[str, Any],
        instance_id: str,
        timeline_id: str,
        cards: list[dict[str, Any]],
        calendar: Calendar,
        *,
        from_world: int,
        to_world: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """打算重议与受支持行动（§11.3）：条件不足就等，窗口过了先延期后放弃，条件满足才提交事件。"""
        rows: dict[str, list[dict[str, Any]]] = {"intents": [], "events": [], "effects": [], "experiences": []}
        known_events = self.store.event_ids(instance_id, timeline_id)
        active_effects = self.store.effect_window(instance_id, timeline_id, until=to_world)
        for card in cards:
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id:
                continue
            dead = events.is_dead(
                instance_id,
                timeline_id,
                character_id,
                self.store.event_window(instance_id, timeline_id, until=to_world, limit=400),
            )
            for row in self.store.intent_list(instance_id, timeline_id, character_id):
                if str(row["stage"]) in ("done", "abandoned"):
                    continue
                if dead:
                    rows["intents"].append(
                        {**row, "stage": "abandoned", "note": "她已不在了", "updated_world": int(to_world)}
                    )
                    continue
                decision = intents.decide(
                    row,
                    world_seconds=to_world,
                    events_present=known_events,
                    active_effects=active_effects,
                )
                if decision == "keep":
                    continue
                updated = {**row, "updated_world": int(to_world)}
                if decision == "wait":
                    _, effect = intents.parse(row)
                    blocked = intents.blockers(row, active_effects)
                    reasons = []
                    unmet = events.unmet_preconditions(
                        intents.parse(row)[0], events=known_events, effects=set()
                    )
                    if unmet:
                        reasons.append(f"条件未到：{', '.join(unmet)}")
                    if blocked:
                        reasons.append(f"受阻于：{', '.join(str(item['kind']) for item in blocked)}")
                    note = "；".join(reasons) or str(row.get("note") or "")
                    if str(row["stage"]) != "waiting" or note != str(row.get("note") or ""):
                        updated["stage"] = "waiting"
                        updated["note"] = note
                        rows["intents"].append(updated)
                    continue
                if decision == "defer":
                    updated["stage"] = "deferred"
                    updated["note"] = determined_note = "目标时间窗已过且条件未满足：延期"
                    rows["intents"].append(updated)
                    _ = determined_note
                    continue
                if decision == "abandon":
                    updated["stage"] = "abandoned"
                    updated["note"] = "延期后仍未满足条件：放弃"
                    rows["intents"].append(updated)
                    continue
                if decision == "act":
                    _, effect = intents.parse(row)
                    action = intents.action_event(
                        row,
                        instance_id=instance_id,
                        timeline_id=timeline_id,
                        world_seconds=int(to_world),
                        calendar=calendar,
                    )
                    action.pop("_calendar", None)
                    rows["events"].append(action)
                    known_events.add(str(action["id"]))
                    if effect:
                        rows["effects"].extend(
                            events.effect_rows(
                                {"effects": [effect]},
                                instance_id=instance_id,
                                timeline_id=timeline_id,
                                event_ident=str(action["id"]),
                                world_seconds=int(to_world),
                                family="",
                            )
                        )
                    rows["experiences"].append(
                        {
                            "id": f"xp-act-{character_id}-{row['id']}",
                            "instance_id": instance_id,
                            "timeline_id": timeline_id,
                            "character_id": character_id,
                            "world_seconds": int(to_world),
                            "kind": "action",
                            "summary": f"自己动手了：{row.get('object')}",
                            "source_ref": str(row["id"]),
                            "confidence": "experienced",
                        }
                    )
                    updated["stage"] = "done"
                    updated["note"] = "已按受支持的行动效果提交事件"
                    rows["intents"].append(updated)
        return rows

    def _world_event_rows(
        self,
        instance: dict[str, Any],
        instance_id: str,
        timeline_id: str,
        cards: list[dict[str, Any]],
        calendar: Calendar,
        *,
        from_world: int,
        to_world: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """当日世界级事件：固定节庆先占名额 → 随机候选 → 前置条件（§3.1、§四）。

        事件只在**已推进到的那一段**落成：当日更晚的时刻留到下一批再判，重算不重抽。
        """
        out: dict[str, list[dict[str, Any]]] = {"events": [], "claims": [], "knowledge": [], "effects": []}
        package = self.setting(instance)["world_package"]
        seed, rules = self.seed_of(instance), self.rules_of(instance)
        day_index = calendar.day_index(from_world)
        known_events = self.store.event_ids(instance_id, timeline_id)
        known_effects = self.store.effect_active_ids(instance_id, timeline_id)
        for candidate in events.plan_day(
            package,
            seed=seed,
            rules_version=rules,
            day_index=day_index,
            calendar=calendar,
            events=known_events,
            effects=known_effects,
        ):
            ident = events.event_id(seed, rules, day_index, str(candidate["slot"]))
            at = events.event_moment(seed, rules, day_index, str(candidate["slot"]), calendar.day_seconds)
            if ident in known_events or at > to_world:
                continue
            row = {
                "id": ident,
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "world_seconds": at,
                "seq": int(events.stable_key(ident)[:6], 16),
                "kind": "world",
                "family": str(candidate["family"]),
                "template": str(candidate["template"]),
                "source": "engine",
                "summary": str(candidate["summary"]),
                "detail": events.detail_text(candidate),
                "text_source": "template",
                "effects": events.as_json(candidate["effects"]),
                "share_value": 1 if candidate.get("fixed") else 0,
                "importance": 0.6 if candidate.get("fixed") else 0.4,
                "created_real": 0.0,
            }
            if not events.share_qualified(row):
                row["importance"] = 0.5
            out["events"].append(row)
            out["effects"].extend(
                events.effect_rows(
                    candidate,
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    event_ident=ident,
                    world_seconds=at,
                    family=str(candidate["family"]),
                )
            )
            claims = events.dump_rows(
                events.claim_rows(
                    candidate,
                    package=package,
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    event_ident=ident,
                    world_seconds=at,
                    calendar=calendar,
                )
            )
            out["claims"].extend(claims)
            for card in cards:
                out["knowledge"].extend(events.grants(row, claims, card, world_seconds=at, calendar=calendar))
        return out

    def _death_rows(
        self,
        instance: dict[str, Any],
        instance_id: str,
        timeline_id: str,
        cards: list[dict[str, Any]],
        calendar: Calendar,
        *,
        from_world: int,
        to_world: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """寿终事件（§四）：由寿命模型与世界时刻推出，单独记账、可产生死讯说法。"""
        out: dict[str, list[dict[str, Any]]] = {"events": [], "claims": [], "knowledge": []}
        package = self.setting(instance)["world_package"]
        known = self.store.event_window(instance_id, timeline_id, until=to_world, limit=400)
        for card in cards:
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id or events.is_dead(instance_id, timeline_id, character_id, known):
                continue
            moment = events.death_moment(card, package, calendar)
            if moment is None or not (from_world < moment <= to_world):
                continue
            row = events.death_event(
                card,
                instance_id=instance_id,
                timeline_id=timeline_id,
                world_seconds=moment,
                calendar=calendar,
                seed=self.seed_of(instance),
            )
            row.pop("_seed", None)
            out["events"].append(row)
            claims = events.dump_rows(
                events.claim_rows(
                    {"summary": row["summary"], "effects": []},
                    package=package,
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    event_ident=str(row["id"]),
                    world_seconds=moment,
                    calendar=calendar,
                )
            )
            out["claims"].extend(claims)
            for other in cards:
                if str((other.get("meta") or {}).get("card_id")) == character_id:
                    continue  # 死者不需要自己的死讯
                out["knowledge"].extend(
                    events.grants(row, claims, other, world_seconds=moment, calendar=calendar)
                )
        return out

    def _propagate_and_clear(
        self,
        instance: dict[str, Any],
        instance_id: str,
        timeline_id: str,
        cards: list[dict[str, Any]],
        calendar: Calendar,
        *,
        from_world: int,
        to_world: int,
    ) -> dict[str, list[Any]]:
        """传播到达的获知（§五）与后果的解除（§六）。"""
        rows: dict[str, list[Any]] = {"knowledge": [], "clear_effects": []}
        for claim in self.store.claim_list(instance_id, timeline_id):
            earliest = int(claim.get("earliest_world") or 0)
            if not (from_world < earliest <= to_world):
                continue
            for card in cards:
                grant = events.claim_grant(claim, card, world_seconds=earliest)
                if grant is not None:
                    rows["knowledge"].append(grant)
        day_seconds = calendar.day_seconds
        # 同族的后续事件（自然恢复的判定依据）：按世界时刻排一次即可
        later_by_family: dict[str, int] = {}
        for item in self.store.event_window(instance_id, timeline_id, until=to_world, limit=200):
            family = str(item.get("family") or "")
            if family:
                later_by_family[family] = max(later_by_family.get(family, 0), int(item["world_seconds"]))
        for effect in self.store.effect_window(instance_id, timeline_id, until=to_world):
            expiry = str(effect.get("expiry"))
            started = int(effect["from_world"])
            if expiry == "with_cause" and started + day_seconds <= to_world:
                rows["clear_effects"].append((str(effect["id"]), instance_id))
            elif expiry == "natural_recovery":
                family = str(effect.get("family") or "")
                # 同族在该后果之后仍有新事件 → 声明的自然条件成立；没有依据就保持有效
                if family and later_by_family.get(family, 0) > started:
                    rows["clear_effects"].append((str(effect["id"]), instance_id))
        _ = instance
        return rows

    def _harvest(
        self,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        calendar: Calendar,
        windows: list[dict[str, Any]],
        from_world: int,
        to_world: int,
    ) -> list[dict[str, Any]]:
        """把本批内已结束的活动窗记为经历（幂等：同一窗口的 id 固定）。"""
        out: list[dict[str, Any]] = []
        for window in windows:
            if not (from_world < int(window["end"]) <= to_world):
                continue
            out.append(
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
        return out

    # ---------- 角色状态 ----------

    def bootstrap(self, instance_id: str, timeline_id: str, *, world_seconds: int) -> dict[str, int]:
        """创建 / 导入后把角色卡的初始单元、首日计划落进运行层。"""
        instance, _ = self._rows(instance_id, timeline_id)
        calendar = self.calendar(instance)
        units = plans = 0
        intents_created = 0
        for card in self.cards(instance, timeline_id=timeline_id, world_seconds=world_seconds):
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id:
                continue
            if not self.store.unit_list(instance_id, timeline_id, character_id):
                for row in personality.initial_rows(
                    card, instance_id=instance_id, timeline_id=timeline_id, world_seconds=world_seconds
                ):
                    self.store.unit_put(row)
                    units += 1
            if not self.store.environment_list(instance_id, timeline_id):
                for row in environment.initial_rows(
                    self.setting(instance)["world_package"],
                    instance_id=instance_id,
                    timeline_id=timeline_id,
                    world_seconds=world_seconds,
                ):
                    self.store.environment_put(row)
            for row in intents.initial_rows(
                card, instance_id=instance_id, timeline_id=timeline_id, world_seconds=world_seconds
            ):
                if not self.store.intent_list(instance_id, timeline_id, character_id):
                    self.store.intent_put(row)
                    intents_created += 1
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
        knowledge = self.store.knowledge_window(
            instance_id, timeline_id, character_id, until=world_seconds, limit=20
        )
        card = self.card_of(
            self.store.instance_get(instance_id) or {},
            character_id,
            timeline_id=timeline_id,
            world_seconds=world_seconds,
        )
        effects = self.store.effect_window(
            instance_id,
            timeline_id,
            until=world_seconds,
            targets=[
                character_id,
                str(card.get("role_id") or ""),
                str((card.get("identity") or {}).get("region") or ""),
            ],
        )
        activity = life.activity_label(life.current_window(plan, world_seconds))
        note = life.effect_note(
            effects,
            character_id,
            str(card.get("role_id") or ""),
            str((card.get("identity") or {}).get("region") or ""),
        )
        if note and activity:
            activity = f"{activity}（受影响的后果：{note}）"
        return {
            "units": personality.visible(units),
            "all_units": units,
            "plan": plan,
            "current_activity": activity,
            "experiences": experiences,
            "knowledge": knowledge,
            "effects": effects,
            "intents": [
                row
                for row in self.store.intent_list(instance_id, timeline_id, character_id)
                if str(row["stage"]) in ("adopted", "waiting", "deferred")
            ],
            "observations": environment.observations(
                self.store.environment_list(instance_id, timeline_id),
                environment.env_types(self.setting(self.store.instance_get(instance_id) or {}).get("world_package", {})),
                card,
                world_seconds=world_seconds,
            ),
        }

    def backfill(self, instance_id: str, timeline_id: str) -> int:
        """历史回填（§3.3）：把包内既定的史料与初始事实落成历史条目，不施加效果、不产生获知。"""
        instance, _ = self._rows(instance_id, timeline_id)
        if self.store.event_ids(instance_id, timeline_id):
            return 0
        rows, claims = events.backfill_rows(
            self.setting(instance)["world_package"],
            instance_id=instance_id,
            timeline_id=timeline_id,
            seed=self.seed_of(instance),
            rules_version=self.rules_of(instance),
        )
        if not rows:
            return 0
        return self.store.runtime_load(
            instance_id, timeline_id, {"watermark": 0, "events": rows, "claims": claims}
        )

    def ensure_instance(self, instance_id: str, *, now_real: float) -> dict[str, int]:
        """补齐运行层状态：缺时钟的时间线建时钟，缺角色状态的按初始水位补齐（幂等）。"""
        instance = self.store.instance_get(instance_id)
        if instance is None:
            raise RuntimeStateError(f"实例不存在：{instance_id}")
        clocks = 0
        for timeline in self.store.timeline_list(instance_id):
            self.backfill(instance_id, timeline["id"])  # 幂等：已有条目即跳过
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

    # ---------- 性格驱动入口 ----------

    def drive_unit(
        self,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        *,
        driver: str,
        semantic: str,
        basis: str,
        strength: float,
        direction: int,
        source_key: str,
        world_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        """把一次驱动落到单元上（§10.1）：来源键去重、变化连续、迁移隐式。

        - 同一来源只消费一次：重试、补算、重新载入都返回 None，不重复强化（§10.3）；
        - 已有同义单元按其累积稳定度调整，后来的弱驱动不会把它裁到本次驱动的生成上限；
        - 驱动水位不得超过已完成水位（不能拿尚未发生的影响改角色）。

        阶段 2 的调用方：对话驱动由会话层整理后调用（§10.1 的「AI 命名 + 确定性数值」分界），
        事件驱动由阶段 3 的事件引擎调用；本方法只做落库与规则，不反问模型。
        """
        if driver not in personality.MODES:
            raise RuntimeStateError(f"未知驱动：{driver}")
        watermark = int(self.clock_row(timeline_id)["processed_world"])
        at = watermark if world_seconds is None else int(world_seconds)
        if at > watermark:
            raise RuntimeStateError("驱动水位不能超过已完成水位")
        rows = self.store.unit_list(instance_id, timeline_id, character_id)
        if any(personality.has_consumed(row, source_key) for row in rows):
            return None
        updated = personality.apply_drive(
            rows,
            mode=driver,
            source_key=source_key,
            semantic=semantic or None,
            strength=float(strength),
            positive=direction >= 0,
            world_seconds=at,
            basis=basis,
            identity={
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "character_id": character_id,
            },
        )
        touched = next(
            (
                row
                for row in updated
                if str(row.get("semantic")) == semantic and personality.has_consumed(row, source_key)
            ),
            None,
        )
        for row in updated:
            if personality.has_consumed(row, source_key):
                self.store.unit_put(row)
        return touched

    # ---------- 角色自主生成打算（§11.3，需模型） ----------

    async def propose_intents(
        self,
        instance_id: str,
        timeline_id: str,
        *,
        llm: Any,
        now_real: float,
    ) -> dict[str, Any]:
        """后台替在世角色想一步：闭集校验 + 可知目标 + 预算记账，失败就当没想过。

        只有在世、未竟之事未满、且距离上次提案超过一个世界日的角色才提案。
        """
        from ..log import get_logger

        log = get_logger("isekai.runtime.planning")
        instance, _ = self._rows(instance_id, timeline_id)
        timeline = next(item for item in self.store.timeline_list(instance_id) if item["id"] == timeline_id)
        if timeline["state"] != "active":
            return {"proposed": 0, "reason": "frozen"}
        row = self.clock_row(timeline_id)
        watermark = int(row["processed_world"])
        calendar = self.calendar(instance)
        package = self.setting(instance)["world_package"]
        known_events = self.store.event_window(instance_id, timeline_id, until=watermark, limit=400)
        budget_bucket = int(now_real // 86400)
        remaining = int(self.render_calls_per_day)
        proposed: list[dict[str, Any]] = []
        for card in self.cards(instance, timeline_id=timeline_id, world_seconds=watermark):
            character_id = str((card.get("meta") or {}).get("card_id") or "")
            if not character_id or events.is_dead(instance_id, timeline_id, character_id, known_events):
                continue
            live = [
                item
                for item in self.store.intent_list(instance_id, timeline_id, character_id)
                if str(item["stage"]) in ("adopted", "waiting")
            ]
            if len(live) >= planning.MAX_LIVE_INTENTS:
                continue
            last = max((int(item["updated_world"]) for item in live), default=0)
            if last and watermark - last < calendar.day_seconds:
                continue  # 同一世界日内不重复提案
            used = self.store.call_ledger_get(instance_id, timeline_id, "intent_propose", bucket=budget_bucket)
            if used >= remaining:
                log.info("intent proposal paused by budget line=%s used=%s", timeline_id, used)
                break
            snapshot = self.character_snapshot(
                instance_id, timeline_id, character_id, world_seconds=watermark
            )
            allowed = planning.allowed_targets(
                package,
                card,
                knowledge=snapshot["knowledge"],
                observations=snapshot["observations"],
            )
            if not any(allowed.values()):
                continue  # 没有她可知的目标可指，就不提案
            messages = planning.prompt(
                name=str((card.get("identity") or {}).get("name") or ""),
                occupation=str((card.get("identity") or {}).get("occupation") or ""),
                world_label=calendar.describe(watermark),
                aims=snapshot["intents"],
                knowledge=snapshot["knowledge"],
                effects=self.store.effect_window(
                    instance_id,
                    timeline_id,
                    until=watermark,
                    targets=[character_id, str(card.get("role_id") or "")],
                ),
                observations=snapshot["observations"],
                allowed=allowed,
            )
            try:
                text = await llm.chat(messages, temperature=0.7, timeout=60.0)
            except Exception:  # 模型不可用不该影响世界推进
                log.exception("intent proposal failed character=%s", character_id)
                self.store.call_ledger_add(
                    instance_id, timeline_id, "intent_propose", bucket=budget_bucket, calls=1
                )
                continue
            self.store.call_ledger_add(
                instance_id, timeline_id, "intent_propose", bucket=budget_bucket, calls=1
            )
            decision = planning.parse(text, allowed)
            if decision is None:
                continue
            fresh = self.clock_row(timeline_id)  # 提案期间世界可能已推进：按最新水位与世代提交
            watermark = int(fresh["processed_world"])
            ident = f"in-auto-{events.stable_key(instance_id, timeline_id, character_id, watermark)[:10]}"
            applied = self.store.apply_runtime_batch(
                timeline_id=timeline_id,
                generation=int(fresh["generation"]),
                processed_world=watermark,
                catching_up=False,
                intents=[
                    {
                        "id": ident,
                        "instance_id": instance_id,
                        "timeline_id": timeline_id,
                        "character_id": character_id,
                        "object": decision["object"],
                        "basis": decision["basis"],
                        "strength": decision["strength"],
                        "window_from": watermark,
                        "window_to": watermark + 7 * calendar.day_seconds,
                        "preconditions": "[]",
                        "effect": json.dumps(decision["effect"], ensure_ascii=False),
                        "stage": "adopted",
                        "note": "",
                        "source_world": watermark,
                        "updated_world": watermark,
                    }
                ],
            )
            if not applied:
                log.info("intent proposal discarded (stale batch) character=%s", character_id)
                continue
            proposed.append({"character": character_id, "intent": ident, **decision})
            if len(proposed) >= 8:
                break
        return {"proposed": len(proposed), "items": proposed, "budget": {"calls": self.store.call_ledger_get(instance_id, timeline_id, "intent_propose", bucket=budget_bucket), "limit": remaining}}

    # ---------- 补卡（角色集合扩充） ----------

    def add_character(
        self,
        instance_id: str,
        timeline_id: str,
        card: dict[str, Any],
        *,
        now_real: float,
        joined_world: int | None = None,
        note: str = "",
        acquainted: bool = False,
    ) -> dict[str, Any]:
        """把角色锚定补入该线（§九 / 附录 B #18）。

        - 新角色一直存在：卡片自带个人史，补入只决定她自哪一刻起出现在本线；
        - 个人史与初始知识按同一认知契约投影（卡片先在设定层过校验，此处只做锚定）；
        - 补入不激活该线：冻结线锚定冻结时刻（调用方随后自行决定是否激活）；
        - 加入点之前的经历与水位不变，回滚跨越加入点即一致退出（回滚属阶段 4）。
        """
        instance, timeline = self._rows(instance_id, timeline_id)
        row = self.clock_row(timeline_id)
        watermark = int(row["processed_world"])
        if joined_world is None:
            joined_world = (
                watermark
                if timeline["state"] == "active"
                else target_world(self.state_of(row), float(row["anchor_real"]))
            )
        joined_world = int(joined_world)
        if joined_world > watermark:
            raise RuntimeStateError("补入时刻不能晚于已完成水位（不能从尚未发生的时刻开始）")
        if joined_world < int(instance["moment"]):
            raise RuntimeStateError("补入时刻不能早于实例初始时刻")
        character_id = str((card.get("meta") or {}).get("card_id") or "")
        if not character_id:
            raise RuntimeStateError("角色卡缺少 card_id")
        if int((card.get("identity") or {}).get("born") or 0) > joined_world:
            raise RuntimeStateError("补入时刻早于角色出生时刻")
        existing = {
            str((item.get("meta") or {}).get("card_id"))
            for item in self.cards(instance, timeline_id=timeline_id, world_seconds=watermark)
        }
        if character_id in existing:
            raise RuntimeStateError(f"该角色已在本线：{character_id}")
        from ..world.cards import validate_card  # 局部导入：设定层与运行层不互相依赖

        errors = validate_card(card, self.setting(instance)["world_package"], moment=joined_world)
        if not bool((card.get("meta") or {}).get("confirmed")):
            errors.append("meta: 角色卡未确认，不能补入")
        if errors:
            raise RuntimeStateError("补入校验未通过：" + "；".join(str(item) for item in errors[:6]))
        calendar = self.calendar(instance)
        self.store.character_join_add(
            {
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "character_id": character_id,
                "joined_world": joined_world,
                "card": json.dumps(card, ensure_ascii=False, sort_keys=True),
                "note": note,
                "acquainted": 1 if acquainted else 0,
                "created_real": float(now_real),
            }
        )
        units = personality.initial_rows(
            card, instance_id=instance_id, timeline_id=timeline_id, world_seconds=joined_world
        )
        if acquainted:  # 「已相识」声明：只补一条对话单元，不改任何既有角色状态
            units.append(
                {
                    "id": "join-acquainted",
                    "instance_id": instance_id,
                    "timeline_id": timeline_id,
                    "character_id": character_id,
                    "mode": "dialog",
                    "semantic": "与联络者已相识",
                    "basis": "补卡时的已相识声明",
                    "confidence": 0.6,
                    "stability": 0.0,
                    "archived": 0,
                    "consumed": "[]",
                    "updated_world": joined_world,
                }
            )
        for unit in units:
            self.store.unit_put(unit)
        self.store.plan_put(
            life.expand_plan(
                card,
                calendar,
                day_index=calendar.day_index(joined_world),
                instance_id=instance_id,
                timeline_id=timeline_id,
                created_world=joined_world,
            )
        )
        return {
            "instance": instance_id,
            "timeline": timeline_id,
            "character": character_id,
            "name": str((card.get("identity") or {}).get("name") or ""),
            "joined_world": joined_world,
            "joined_label": calendar.describe(joined_world),
            "units": len(units),
            "acquainted": bool(acquainted),
            "timeline_state": timeline["state"],
            "note": note,
        }

    # ---------- 会话接入 ----------

    def world_moment(self, instance_id: str, timeline_id: str, *, now_real: float | None = None) -> int:
        """可对话的世界时刻 = 已处理水位（不把未推进的目标当既有状态，§2.6）。"""
        return int(self.clock_row(timeline_id)["processed_world"])

    def card_of(
        self,
        instance: dict[str, Any],
        character_id: str,
        *,
        timeline_id: str | None = None,
        world_seconds: int | None = None,
    ) -> dict[str, Any]:
        for card in self.cards(instance, timeline_id=timeline_id, world_seconds=world_seconds):
            if str((card.get("meta") or {}).get("card_id")) == character_id:
                return card
        raise RuntimeStateError(f"实例内没有该角色：{character_id}")

    def system_prompt(
        self, session: dict[str, Any], *, topic: str | None = None, now_real: float | None = None
    ) -> str:
        """会话层用的扮演定义：锁定设定 + 该角色截至当前水位的认知切片（无实情层注入）。"""
        import time as _time

        from . import cognition

        instance, _ = self._rows(session["instance_id"], session["timeline_id"])
        calendar = self.calendar(instance)
        world = self.world_moment(session["instance_id"], session["timeline_id"], now_real=now_real or _time.time())
        card = self.card_of(
            instance,
            str(session["character_id"]),
            timeline_id=session["timeline_id"],
            world_seconds=world,
        )
        snapshot = self.character_snapshot(
            session["instance_id"], session["timeline_id"], str(session["character_id"]), world_seconds=world
        )
        context = cognition.play_context(
            self.setting(instance)["world_package"],
            card,
            world_seconds=world,
            calendar_label=calendar.describe(world),
            topic=topic,
            current_activity=str(snapshot["current_activity"] or ""),
            units=snapshot["units"],
            experiences=snapshot["experiences"],
            knowledge=snapshot["knowledge"],
            intents=snapshot["intents"],
            observations=snapshot["observations"],
        )
        return cognition.render_prompt(context)
