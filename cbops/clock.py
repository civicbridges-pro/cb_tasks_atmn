"""Business-hours math for SLA clocks.

Two rules make a clock honest, and both are easy to get wrong:

1.  A response clock runs against the *owner's* coverage hours, not wall time. Telling
    Taj he breached an SLA at 3am his time is how a team learns to ignore the system.
2.  A deadline-driven clock ignores intervals entirely and works backward from a real
    external date. Chasing a vendor every 72 hours when the solicitation closes tomorrow
    is theater.

Where coverage hours are unconfirmed, this module says so rather than inventing a window.
A clock computed on a placeholder is worse than no clock, because it looks authoritative.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable
from zoneinfo import ZoneInfo

from .config import TODO, Config

WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """nth weekday of a month, weekday 0=Monday."""
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    last_day = (dt.date(year, month, 28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - dt.timedelta(days=offset)


def _observed(day: dt.date) -> dt.date:
    """Federal observance: Saturday shifts back to Friday, Sunday forward to Monday."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def us_federal_holidays(year: int) -> set[dt.date]:
    """Observed federal holidays. Contracting officers are not reading mail on these."""
    return {
        _observed(dt.date(year, 1, 1)),                  # New Year's Day
        _nth_weekday(year, 1, 0, 3),                     # MLK Jr Day
        _nth_weekday(year, 2, 0, 3),                     # Washington's Birthday
        _last_weekday(year, 5, 0),                       # Memorial Day
        _observed(dt.date(year, 6, 19)),                 # Juneteenth
        _observed(dt.date(year, 7, 4)),                  # Independence Day
        _nth_weekday(year, 9, 0, 1),                     # Labor Day
        _nth_weekday(year, 10, 0, 2),                    # Columbus Day
        _observed(dt.date(year, 11, 11)),                # Veterans Day
        _nth_weekday(year, 11, 3, 4),                    # Thanksgiving
        _observed(dt.date(year, 12, 25)),                # Christmas Day
    }


@dataclass
class Coverage:
    """One person's working window. `known` is False when config says TODO_CONFIRM."""

    timezone: str
    start_minute: int
    end_minute: int
    workdays: frozenset[int]
    holidays_calendar: str | None
    known: bool

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_workday(self, day: dt.date) -> bool:
        if day.weekday() not in self.workdays:
            return False
        if self.holidays_calendar == "us_federal" and day in us_federal_holidays(day.year):
            return False
        return True


# Used only when coverage is unconfirmed, and always reported as degraded.
FALLBACK_COVERAGE = Coverage(
    timezone="UTC",
    start_minute=0,
    end_minute=24 * 60,
    workdays=frozenset(range(7)),
    holidays_calendar=None,
    known=False,
)


def coverage_for(cfg: Config, person_id: str) -> Coverage:
    person = cfg.person(person_id)
    hours = str(person.get("coverage_hours", TODO))
    tzname = str(person.get("timezone", TODO))
    if hours == TODO or tzname == TODO:
        return FALLBACK_COVERAGE

    start_text, end_text = hours.split("-", 1)
    start_h, start_m = (int(x) for x in start_text.split(":"))
    end_h, end_m = (int(x) for x in end_text.split(":"))

    defaults = cfg.people.get("defaults", {}) or {}
    week = person.get("workweek") or defaults.get("workweek") or WEEKDAY_NAMES[:5]
    workdays = frozenset(WEEKDAY_NAMES.index(str(d).lower()[:3]) for d in week)

    return Coverage(
        timezone=tzname,
        start_minute=start_h * 60 + start_m,
        end_minute=end_h * 60 + end_m,
        workdays=workdays,
        holidays_calendar=person.get("holidays_calendar") or defaults.get("holidays_calendar"),
        known=True,
    )


def _minutes_in_day(coverage: Coverage, day: dt.date, window_start: dt.datetime,
                    window_end: dt.datetime) -> float:
    """Overlap, in minutes, between a coverage day and an arbitrary window."""
    if not coverage.is_workday(day):
        return 0.0
    day_start = dt.datetime.combine(day, dt.time(), tzinfo=coverage.tz) + dt.timedelta(
        minutes=coverage.start_minute
    )
    day_end = dt.datetime.combine(day, dt.time(), tzinfo=coverage.tz) + dt.timedelta(
        minutes=coverage.end_minute
    )
    lo = max(day_start, window_start)
    hi = min(day_end, window_end)
    return max(0.0, (hi - lo).total_seconds() / 60.0)


def business_hours_between(coverage: Coverage, start: dt.datetime, end: dt.datetime) -> float:
    """Coverage hours elapsed between two instants. Zero if end precedes start."""
    if end <= start:
        return 0.0
    start = start.astimezone(coverage.tz)
    end = end.astimezone(coverage.tz)
    total = 0.0
    day = start.date()
    while day <= end.date():
        total += _minutes_in_day(coverage, day, start, end)
        day += dt.timedelta(days=1)
    return total / 60.0


def add_business_hours(coverage: Coverage, start: dt.datetime, hours: float) -> dt.datetime:
    """The instant `hours` of coverage time after `start`.

    Walks day by day. Cheap enough for the volumes here, and obviously correct, which
    matters more than clever for anything that decides whether a person is late.
    """
    remaining = hours * 60.0
    cursor = start.astimezone(coverage.tz)
    day = cursor.date()
    guard = 0

    while remaining > 0:
        guard += 1
        if guard > 3650:  # ten years of days; a bug, not a long SLA
            raise RuntimeError("add_business_hours failed to converge; check coverage config")
        if coverage.is_workday(day):
            day_start = dt.datetime.combine(day, dt.time(), tzinfo=coverage.tz) + dt.timedelta(
                minutes=coverage.start_minute
            )
            day_end = dt.datetime.combine(day, dt.time(), tzinfo=coverage.tz) + dt.timedelta(
                minutes=coverage.end_minute
            )
            window_start = max(day_start, cursor)
            available = max(0.0, (day_end - window_start).total_seconds() / 60.0)
            if available >= remaining:
                return window_start + dt.timedelta(minutes=remaining)
            remaining -= available
        day += dt.timedelta(days=1)
        cursor = dt.datetime.combine(day, dt.time(), tzinfo=coverage.tz)

    return cursor


def backward_checkpoints(deadline: dt.datetime, hours_before: Iterable[float],
                         now: dt.datetime | None = None) -> list[dt.datetime]:
    """Chase checkpoints planned backward from a real external deadline.

    This is the Marotta pattern fix. Cadence hangs off the solicitation close date, so a
    quote request that lands three days before close gets chased three times, not once.
    Checkpoints already in the past are dropped.
    """
    points = sorted(
        (deadline - dt.timedelta(hours=float(h)) for h in hours_before),
    )
    if now is not None:
        points = [p for p in points if p > now]
    return points


def next_chase_at(coverage: Coverage, waiting_since: dt.datetime, cadence_hours: list[float],
                  chase_count: int) -> dt.datetime | None:
    """Next chase for a waiting_external obligation with no external deadline.

    Returns None once the cadence is exhausted, which is the signal to escalate rather
    than keep chasing into silence.
    """
    if chase_count >= len(cadence_hours):
        return None
    return add_business_hours(coverage, waiting_since, cadence_hours[chase_count])
