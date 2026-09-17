"""Timezone conversion — the silent failure mode, so it gets its own suite."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services.timeutils import (
    get_zone,
    is_past,
    local_to_utc,
    shift_day,
    utc_to_local,
)


@pytest.mark.parametrize(
    "tz_name,local,expected_utc",
    [
        ("Asia/Dubai", datetime(2026, 3, 5, 8, 0), datetime(2026, 3, 5, 4, 0)),      # +4
        ("Asia/Kolkata", datetime(2026, 3, 5, 8, 0), datetime(2026, 3, 5, 2, 30)),   # +5:30
        ("UTC", datetime(2026, 3, 5, 8, 0), datetime(2026, 3, 5, 8, 0)),
        ("America/New_York", datetime(2026, 1, 15, 8, 0), datetime(2026, 1, 15, 13, 0)),  # -5
    ],
)
def test_local_to_utc(tz_name, local, expected_utc):
    assert local_to_utc(local, tz_name) == expected_utc


@pytest.mark.parametrize(
    "tz_name", ["Asia/Dubai", "Asia/Kolkata", "UTC", "America/New_York", "Europe/London"]
)
def test_round_trip(tz_name):
    local = datetime(2026, 6, 15, 14, 30, 15)
    assert utc_to_local(local_to_utc(local, tz_name), tz_name) == local


def test_unknown_zone_falls_back_to_utc_rather_than_raising():
    """One bad source config must not take down every other source's run."""
    assert get_zone("Mars/Olympus") == get_zone("UTC")
    assert local_to_utc(datetime(2026, 3, 5, 8, 0), "Not/AZone") == datetime(2026, 3, 5, 8, 0)


def test_dst_transition_is_handled():
    """New York moves to -4 in summer, so the same wall-clock maps differently."""
    winter = local_to_utc(datetime(2026, 1, 15, 8, 0), "America/New_York")
    summer = local_to_utc(datetime(2026, 7, 15, 8, 0), "America/New_York")
    assert winter.hour == 13
    assert summer.hour == 12


def test_shift_day_puts_the_small_hours_on_the_previous_day():
    assert shift_day(datetime(2026, 3, 6, 2, 0), 4) == datetime(2026, 3, 5).date()
    assert shift_day(datetime(2026, 3, 6, 5, 0), 4) == datetime(2026, 3, 6).date()


def test_is_past_handles_naive_and_aware():
    """SQLite returns naive, PostgreSQL returns aware — both must compare."""
    assert is_past(datetime(2000, 1, 1)) is True
    assert is_past(datetime(2099, 1, 1)) is False
    assert is_past(None) is False
