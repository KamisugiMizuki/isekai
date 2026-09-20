"""历法：世界秒 ↔ 历法时刻的确定性纯函数（WORLD_RUNTIME_SPEC §2.1、附录 A）。

- 世界秒是唯一时间基元；日期、月份、时段只是视图。
- v1 无闰年、无闰月：年长 = 月表天数之和 × 日长，固定。
- 纪元对应世界秒 0；纪元之前的时刻为负数（出生时刻等），换算用向下取整。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..world.package import PackageError


@dataclass(frozen=True)
class Calendar:
    era: str
    day_seconds: int
    months: tuple[tuple[str, int], ...]
    week_days: int
    week_name: str
    segments: tuple[dict[str, Any], ...]
    initial_moment: int

    @property
    def year_days(self) -> int:
        return sum(days for _, days in self.months)

    @property
    def year_seconds(self) -> int:
        return self.year_days * self.day_seconds

    def day_index(self, world_seconds: int) -> int:
        """日序号：floor(t / day_seconds)（附录 A）。"""
        return world_seconds // self.day_seconds

    def part_of_day(self, world_seconds: int) -> int:
        return world_seconds - self.day_index(world_seconds) * self.day_seconds

    def segment_of(self, world_seconds: int) -> dict[str, Any]:
        offset = self.part_of_day(world_seconds)
        for segment in self.segments:
            if int(segment["start"]) <= offset < int(segment["end"]):
                return segment
        return self.segments[-1]

    def to_calendar(self, world_seconds: int) -> dict[str, Any]:
        """世界秒 → 视图：年 / 月 / 日 / 时段 / 时分秒（同一输入必得同一结果）。"""
        day_index = self.day_index(world_seconds)
        year_index, rest_days = divmod(day_index, self.year_days)
        month_index = len(self.months) - 1
        day_of_month = rest_days
        for index, (_, days) in enumerate(self.months):
            if rest_days < days:
                month_index = index
                day_of_month = rest_days
                break
            rest_days -= days
        offset = self.part_of_day(world_seconds)
        return {
            "world_seconds": world_seconds,
            "year": year_index + 1,
            "month": month_index + 1,
            "month_name": self.months[month_index][0],
            "day": day_of_month + 1,
            "day_index": day_index,
            "week_day": day_index % self.week_days + 1 if self.week_days else 0,
            "segment": self.segment_of(world_seconds)["name"],
            "hour": offset // 3600,
            "minute": (offset % 3600) // 60,
            "second": offset % 60,
        }

    def to_world_seconds(self, *, year: int, month: int, day: int, offset: int = 0) -> int:
        """视图 → 世界秒（与 to_calendar 互逆，便于生成器与测试构造时刻）。"""
        if not 1 <= month <= len(self.months):
            raise PackageError(f"月序号超出历法：{month}")
        days_before = sum(days for _, days in self.months[: month - 1])
        if not 1 <= day <= self.months[month - 1][1]:
            raise PackageError(f"日序号超出该月：{day}")
        return ((year - 1) * self.year_days + days_before + (day - 1)) * self.day_seconds + offset

    def describe(self, world_seconds: int) -> str:
        view = self.to_calendar(world_seconds)
        return (
            f"{self.era}{view['year']}年{view['month_name']}{view['day']}日"
            f"（{view['segment']}，{view['hour']:02d}:{view['minute']:02d}）"
        )


def calendar_from_package(package: dict[str, Any]) -> Calendar:
    """从锁定的世界包构造历法；包已通过校验，这里只做形状转换。"""
    raw = package["calendar"]
    return Calendar(
        era=str(raw["era"]),
        day_seconds=int(raw["day_seconds"]),
        months=tuple((str(item["name"]), int(item["days"])) for item in raw["months"]),
        week_days=int((raw.get("week") or {}).get("days") or 0),
        week_name=str((raw.get("week") or {}).get("name") or ""),
        segments=tuple(dict(item) for item in raw["segments"]),
        initial_moment=int(raw["initial_moment"]),
    )
