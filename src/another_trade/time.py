from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
NEW_YORK = ZoneInfo("America/New_York")

INTERVAL_MS: dict[str, int] = {
    "1": 60_000,
    "3": 180_000,
    "5": 300_000,
    "15": 900_000,
    "30": 1_800_000,
    "60": 3_600_000,
    "120": 7_200_000,
    "240": 14_400_000,
    "360": 21_600_000,
    "720": 43_200_000,
}


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError as exc:
        raise ValueError(f"unsupported fixed interval: {interval}") from exc


def close_time_ms(start_time_ms: int, interval: str) -> int:
    """Canonical half-open bar end: [start, close), never -1 ms."""
    return start_time_ms + interval_ms(interval)


def is_closed_bar(start_time_ms: int, interval: str, now_ms: int) -> bool:
    return close_time_ms(start_time_ms, interval) <= now_ms


def utc_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return int(value.astimezone(UTC).timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class SessionWindow:
    start_ms: int
    end_ms: int

    def contains_close(self, close_ms: int) -> bool:
        return self.start_ms <= close_ms < self.end_ms


def _local_window(day: date, zone: ZoneInfo, start: time, end: time) -> SessionWindow:
    start_dt = datetime.combine(day, start, tzinfo=zone)
    end_dt = datetime.combine(day, end, tzinfo=zone)
    return SessionWindow(utc_ms(start_dt), utc_ms(end_dt))


def europe_session(day: date) -> SessionWindow:
    return _local_window(day, AMSTERDAM, time(9, 0), time(12, 0))


def us_session(day: date) -> SessionWindow:
    return _local_window(day, NEW_YORK, time(8, 30), time(13, 0))


def universe_cutoff_ms(day: date) -> int:
    return utc_ms(datetime.combine(day, time(9, 0), tzinfo=AMSTERDAM))
