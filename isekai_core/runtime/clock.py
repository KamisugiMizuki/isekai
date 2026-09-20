"""世界时钟与倍率：纯状态机，不碰数据库（WORLD_RUNTIME_SPEC §2.2、§2.3）。

- 目标世界时间 = 基准世界时间 + (当前现实时间 − 基准现实时间) × 当前倍率；
- 倍率变更按**分段累计**结算，不用最新倍率乘整个离线区间；
- 生效点 = 严格晚于输入时刻的第一个自然整秒；同生效点以最后请求为准；
- 系统时钟倒拨不允许世界倒退：保留现实时间高水位，追平前不增加世界时间。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

#: 倍率上限：世界秒 / 现实秒，全局统一、仅开发者可配置（§2.2）
DEFAULT_RATE_MAX = 2592000


@dataclass(frozen=True)
class ClockState:
    """当前倍率段的起点与流速；现实时间统一按 UTC 秒（float）记。"""

    base_real: float
    base_world: int
    rate: int = 1
    #: 现实时间高水位：时钟倒拨时用它，避免世界倒退或重复推进
    high_water_real: float = 0.0


@dataclass(frozen=True)
class RateCommand:
    """一条待生效的倍率请求：权威输入时刻 + 请求顺序 + 生效整秒。"""

    input_real: float
    effective_real: int
    rate: int
    seq: int


def natural_second(now_real: float) -> int:
    """严格晚于输入时刻的第一个自然整秒；恰在整秒输入也属于下一整秒（§2.3）。"""
    return math.floor(now_real) + 1


def target_world(state: ClockState, now_real: float) -> int:
    """当前倍率段下的目标世界时间（不改变状态）。"""
    elapsed = max(0.0, now_real - max(state.base_real, state.high_water_real))
    return state.base_world + int(elapsed * state.rate)


def settle(
    state: ClockState, now_real: float, commands: list[RateCommand]
) -> tuple[ClockState, list[RateCommand]]:
    """先按旧倍率结算到各生效整秒，再更新基准与倍率（§2.3 第 4 步）。

    返回 (新状态, 已消费的命令)；未到生效点的命令保留给下次结算。
    """
    ordered = sorted(commands, key=lambda item: (item.effective_real, item.seq))
    consumed: list[RateCommand] = []
    current = state
    for command in ordered:
        if command.effective_real > now_real:
            break
        world_at_boundary = current.base_world + int(
            (command.effective_real - current.base_real) * current.rate
        )
        current = replace(
            current,
            base_real=float(command.effective_real),
            base_world=world_at_boundary,
            rate=command.rate,
            high_water_real=max(current.high_water_real, float(command.effective_real)),
        )
        consumed.append(command)
    return current, consumed


def apply_command(state: ClockState, command: RateCommand, now_real: float) -> ClockState:
    """已到期命令立即结算到当前时刻（用于「先结算已到期变更」的路径）。"""
    settled, _ = settle(state, now_real, [command])
    return settled


def freeze(state: ClockState, now_real: float, commands: list[RateCommand]) -> ClockState:
    """冻结：结算到当下并把倍率归零语义交给时间线状态（不用 rate=0 表达冻结，§2.2）。"""
    settled, _ = settle(state, now_real, commands)
    return replace(settled, base_real=now_real, base_world=target_world(settled, now_real))


def describe(state: ClockState, now_real: float) -> dict[str, Any]:
    """管理面可见的时钟视图（含追赶状态提示所需信息，不含世界内部内容）。"""
    world = target_world(state, now_real)
    return {
        "world_seconds": world,
        "rate": state.rate,
        "base_real": state.base_real,
        "base_world": state.base_world,
        "behind": state.high_water_real > now_real,
    }
