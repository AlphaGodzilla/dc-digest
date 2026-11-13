from datetime import datetime

import pytest

from dc_digest.timewin import (
    DAY_MS,
    MAX_WINDOW_HOURS,
    MAX_WINDOW_MS,
    TimeWindowError,
    clamp_window,
    describes_single_day,
    parse_bound,
    parse_window,
    zone,
)

TZ = "Asia/Shanghai"


def _now() -> datetime:
    return datetime(2026, 9, 20, 12, 0, tzinfo=zone(TZ))


def test_date_only_to_covers_whole_day() -> None:
    start, end = parse_window("2026-09-08", "2026-09-08", tz_name=TZ, now=_now())
    assert end - start == 86_400_000
    assert describes_single_day(start, end, TZ)


def test_relative_window() -> None:
    start, end = parse_window("-2h", "now", tz_name=TZ, now=_now())
    assert end - start == 7_200_000


def test_datetime_bounds() -> None:
    start, end = parse_window("2026-09-08T10:30", "2026-09-08 12:00", tz_name=TZ, now=_now())
    assert end - start == 90 * 60 * 1000


def test_reversed_window_rejected() -> None:
    with pytest.raises(TimeWindowError):
        parse_window("2026-09-09", "2026-09-08T08:00", tz_name=TZ, now=_now())


def test_unknown_format_rejected() -> None:
    with pytest.raises(TimeWindowError):
        parse_bound("前天下午", tz=zone(TZ), now=_now())


def test_today_and_yesterday() -> None:
    today = parse_bound("today", tz=zone(TZ), now=_now())
    yesterday = parse_bound("yesterday", tz=zone(TZ), now=_now())
    assert today - yesterday == 86_400_000


def test_clamp_window_keeps_recent_span() -> None:
    end = 1_800_000_000_000
    start = end - 10 * DAY_MS
    clamped_start, clamped_end = clamp_window(start, end)
    assert clamped_end == end
    assert clamped_end - clamped_start == MAX_WINDOW_MS


def test_clamp_window_leaves_short_span_untouched() -> None:
    end = 1_800_000_000_000
    start = end - 3 * 3_600_000
    assert clamp_window(start, end) == (start, end)


def test_clamp_window_defaults_to_48_hours() -> None:
    assert MAX_WINDOW_HOURS == 48
