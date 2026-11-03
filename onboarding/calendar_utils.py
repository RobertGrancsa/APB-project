from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


MAX_BUSY_SPAN_DAYS = 62


def is_working_day(day: date, holidays: frozenset[date]) -> bool:
    """Working day: Monday..Friday (0..4) and not in the public holiday set."""
    return day.weekday() < 5 and day not in holidays


def add_working_days(start_date: date, working_days: int, holidays: frozenset[date]) -> date:
    """Rule B-R11: return the `working_days`-th working day strictly after `start_date`."""
    current = start_date
    remaining = working_days
    while remaining > 0:
        current += timedelta(days=1)
        if is_working_day(current, holidays):
            remaining -= 1
    return current


def get_meeting_window_days(
    start_date: date, window_working_days: int, holidays: frozenset[date]
) -> list[date]:
    """Rule B-R12 step 2: first `window_working_days` working days from `start_date` inclusive."""
    if window_working_days <= 0:
        return []
    days: list[date] = []
    current = start_date
    while len(days) < window_working_days:
        if is_working_day(current, holidays):
            days.append(current)
        current += timedelta(days=1)
    return days


def build_busy_query_ranges(first_day: date, last_day: date) -> list[tuple[date, date]]:
    """Split [first_day, last_day] into inclusive chunks where `to` is at most 62 days after `from`."""
    ranges: list[tuple[date, date]] = []
    cur = first_day
    while cur <= last_day:
        chunk_end = min(last_day, cur + timedelta(days=MAX_BUSY_SPAN_DAYS))
        ranges.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return ranges


def parse_iso_datetime(value: str) -> datetime:
    """Parse ISO 8601 timestamp with offset (compatible with Python 3.9+ including trailing 'Z')."""
    s = value.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _parse_hhmm_minutes(hhmm: str) -> int:
    parts = hhmm.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _minutes_to_local_dt(day: date, total_minutes: int, tz: ZoneInfo) -> datetime:
    extra_days, rem_minutes = divmod(total_minutes, 1440)
    hour, minute = divmod(rem_minutes, 60)
    target_day = day + timedelta(days=extra_days)
    return datetime.combine(target_day, time(hour, minute, 0), tzinfo=tz)


def find_first_free_meeting_slot(
    window_days: list[date],
    *,
    day_start: str,
    day_end: str,
    slot_minutes: int,
    duration_minutes: int,
    timezone_name: str,
    busy_intervals: list[tuple[datetime, datetime]],
) -> tuple[str, str] | None:
    """Rule B-R12 steps 3-6: return (start_iso, end_iso) for the earliest non-overlapping slot."""
    if not window_days or slot_minutes <= 0 or duration_minutes <= 0:
        return None

    tz = ZoneInfo(timezone_name)
    start_min = _parse_hhmm_minutes(day_start)
    end_min = _parse_hhmm_minutes(day_end)

    for day in window_days:
        cur_min = start_min
        while cur_min + duration_minutes <= end_min:
            cand_start = _minutes_to_local_dt(day, cur_min, tz)
            cand_end = _minutes_to_local_dt(day, cur_min + duration_minutes, tz)
            # Half-open interval [cand_start, cand_end) vs [busy_start, busy_end)
            overlaps = any(
                cand_start < busy_end and busy_start < cand_end
                for busy_start, busy_end in busy_intervals
            )
            if not overlaps:
                return (
                    cand_start.isoformat(timespec="seconds"),
                    cand_end.isoformat(timespec="seconds"),
                )
            cur_min += slot_minutes

    return None
