from __future__ import annotations

from datetime import UTC, date, datetime

from another_trade.time import close_time_ms, europe_session, universe_cutoff_ms, us_session


def test_close_time_is_exclusive_end_without_minus_one_ms() -> None:
    start = int(datetime(2026, 3, 20, 8, 45, tzinfo=UTC).timestamp() * 1000)
    assert close_time_ms(start, "15") == start + 15 * 60_000


def test_bar_closing_at_cutoff_is_excluded_by_strict_less_than() -> None:
    day = date(2026, 4, 3)
    cutoff = universe_cutoff_ms(day)
    start = cutoff - 15 * 60_000
    assert close_time_ms(start, "15") == cutoff
    assert not close_time_ms(start, "15") < cutoff


def test_spring_dst_mismatch_weeks_are_explicit() -> None:
    mar6 = us_session(date(2026, 3, 6)).start_ms - europe_session(date(2026, 3, 6)).start_ms
    mar20 = (
        us_session(date(2026, 3, 20)).start_ms - europe_session(date(2026, 3, 20)).start_ms
    )
    apr3 = us_session(date(2026, 4, 3)).start_ms - europe_session(date(2026, 4, 3)).start_ms
    assert mar6 == int(5.5 * 60 * 60_000)
    assert mar20 == int(4.5 * 60 * 60_000)
    assert apr3 == int(5.5 * 60 * 60_000)


def test_autumn_dst_mismatch_week_is_explicit() -> None:
    oct30 = (
        us_session(date(2026, 10, 30)).start_ms
        - europe_session(date(2026, 10, 30)).start_ms
    )
    nov6 = us_session(date(2026, 11, 6)).start_ms - europe_session(date(2026, 11, 6)).start_ms
    assert oct30 == int(4.5 * 60 * 60_000)
    assert nov6 == int(5.5 * 60 * 60_000)
