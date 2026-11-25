"""UTC time and exact decimal helpers."""
from __future__ import annotations

from calendar import timegm
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

CENT = Decimal("1")


def parse_ts(value) -> int:
    """Parse an ISO-8601 UTC timestamp (or integer epoch seconds)."""
    if isinstance(value, (int, float, Decimal)):
        return int(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # The exchange contract uses whole seconds and half-open intervals.
    return timegm(dt.utctimetuple())


def iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def month_of(ts: int) -> str:
    return iso(ts)[:7]  # type: ignore[index]


def month_bounds(month: str) -> tuple[int, int]:
    year, mon = map(int, month.split("-"))
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=timezone.utc)
    return timegm(start.utctimetuple()), timegm(end.utctimetuple())


def next_month(month: str) -> str:
    year, mon = map(int, month.split("-"))
    if mon == 12:
        return f"{year + 1}-01"
    return f"{year}-{mon + 1:02d}"


def month_intersect(start: int, end: int, month: str) -> tuple[int, int] | None:
    ms, me = month_bounds(month)
    s, e = max(start, ms), min(end, me)
    return (s, e) if s < e else None


def round_cents(value: Decimal) -> int:
    """Round a cent amount to whole cents, halves away from zero."""
    return int(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP))


def dec(value) -> Decimal:
    return Decimal(str(value))


def decimal_json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)!r} is not JSON serializable")


def intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return max(a_start, b_start) < min(a_end, b_end)


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((s, e) for s, e in intervals if s < e)
    out: list[tuple[int, int]] = []
    for s, e in ordered:
        if not out or s > out[-1][1]:
            out.append((s, e))
        elif e > out[-1][1]:
            out[-1] = (out[-1][0], e)
    return out
