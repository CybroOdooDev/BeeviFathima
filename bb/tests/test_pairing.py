"""Pairing rules, including the case that breaks a naive implementation."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.pairing import (
    Direction,
    Interval,
    OpenShift,
    PairingConfig,
    PairingMode,
    Punch,
    dedupe,
    pair_punches,
)

DAY = datetime(2026, 3, 5, 0, 0, 0)


def p(pid, hour, minute=0, direction=Direction.unknown, day=DAY):
    return Punch(
        punch_id=pid,
        emp_code="1001",
        time_utc=day.replace(hour=hour, minute=minute),
        direction=direction,
    )


# --------------------------------------------------------------------------- #
# The split-cycle case: check-in in one run, check-out in the next
# --------------------------------------------------------------------------- #
def test_lone_checkout_closes_the_open_shift_instead_of_opening_one():
    """The normal polling case, and the one a naive implementation gets wrong.

    Cycle 1 ingested the 08:00 check-in and opened Odoo attendance 1001. Cycle 2
    sees only the 17:00 check-out, because the check-in is already synced.
    """
    open_shift = OpenShift(attendance_id=1001, check_in=DAY.replace(hour=8))
    result = pair_punches([p("out", 17)], PairingConfig(), open_shift=open_shift)

    assert len(result.intervals) == 1, "must not create a second record"
    interval = result.intervals[0]
    assert interval.closes_attendance_id == 1001
    assert interval.check_in == DAY.replace(hour=8), "keeps the recorded check-in"
    assert interval.check_out == DAY.replace(hour=17)
    assert interval.duration_hours == 9.0


def test_lone_checkout_closes_open_shift_in_state_based_mode():
    open_shift = OpenShift(attendance_id=7, check_in=DAY.replace(hour=8))
    config = PairingConfig(mode=PairingMode.state_based)
    result = pair_punches(
        [p("out", 17, direction=Direction.outward)], config, open_shift=open_shift
    )

    assert len(result.intervals) == 1
    assert result.intervals[0].closes_attendance_id == 7
    assert result.intervals[0].check_out == DAY.replace(hour=17)


def test_a_new_checkin_while_a_shift_is_open_closes_it_first():
    """Somebody forgot to badge out yesterday and is badging in today."""
    open_shift = OpenShift(attendance_id=42, check_in=DAY.replace(hour=8))
    config = PairingConfig(mode=PairingMode.state_based)
    result = pair_punches(
        [p("in2", 9, direction=Direction.inward, day=DAY + timedelta(days=1))],
        config,
        open_shift=open_shift,
    )

    closes = [i for i in result.intervals if i.closes_attendance_id == 42]
    opens = [i for i in result.intervals if i.is_open]
    assert len(closes) == 1, "the stale shift is closed"
    assert closes[0].auto_closed is True
    assert len(opens) == 1, "and exactly one new shift is opened"


def test_out_of_order_punch_cannot_close_a_later_shift():
    """A punch earlier than the recorded check-in must not rewrite history."""
    open_shift = OpenShift(attendance_id=5, check_in=DAY.replace(hour=12))
    result = pair_punches([p("stray", 9)], PairingConfig(), open_shift=open_shift)

    assert all(i.closes_attendance_id is None for i in result.intervals)


def test_without_an_open_shift_a_single_punch_opens_one():
    result = pair_punches([p("in", 8)], PairingConfig())
    assert len(result.intervals) == 1
    assert result.intervals[0].is_open
    assert result.intervals[0].check_in == DAY.replace(hour=8)


# --------------------------------------------------------------------------- #
# Ordinary pairing
# --------------------------------------------------------------------------- #
def test_alternating_pairs_in_out_in_out():
    result = pair_punches([p("a", 8), p("b", 12), p("c", 13), p("d", 17)], PairingConfig())
    assert [i.duration_hours for i in result.intervals] == [4.0, 4.0]


def test_night_shift_stays_one_interval():
    punches = [
        p("in", 22, 40),
        Punch("out", "1001", (DAY + timedelta(days=1)).replace(hour=6, minute=15)),
    ]
    result = pair_punches(punches, PairingConfig())
    assert len(result.intervals) == 1
    assert result.intervals[0].duration_hours == pytest.approx(7.583, abs=0.01)


def test_state_based_falls_back_when_the_device_sends_no_outs():
    """A device with no OUT key stamps everything 'Check In'."""
    punches = [p(str(i), h, direction=Direction.inward) for i, h in enumerate([8, 12, 13, 17])]
    result = pair_punches(punches, PairingConfig(mode=PairingMode.state_based))

    assert any("without an OUT key" in w for w in result.warnings)
    assert [i.duration_hours for i in result.intervals] == [4.0, 4.0]


def test_first_last_discards_the_middle():
    config = PairingConfig(mode=PairingMode.first_last)
    result = pair_punches([p("a", 8), p("b", 12), p("c", 13), p("d", 17)], config)

    assert len(result.intervals) == 1
    assert result.intervals[0].duration_hours == 9.0
    assert "2 intermediate punch(es) ignored" in result.intervals[0].notes[0]


# --------------------------------------------------------------------------- #
# Dedupe
# --------------------------------------------------------------------------- #
def test_same_direction_double_tap_is_dropped():
    punches = [p("a", 8, 0), p("b", 8, 0)]
    kept, dropped = dedupe(punches, 60)
    assert len(kept) == 1 and dropped == ["b"]


def test_opposite_direction_inside_the_window_is_kept():
    """'In then straight out' is a real, if brief, visit."""
    punches = [
        p("a", 8, 0, direction=Direction.inward),
        Punch("b", "1001", DAY.replace(hour=8, minute=0, second=20), Direction.outward),
    ]
    kept, dropped = dedupe(punches, 60)
    assert len(kept) == 2 and dropped == []


# --------------------------------------------------------------------------- #
# Guard rails
# --------------------------------------------------------------------------- #
def test_runaway_shift_is_capped_and_flagged():
    punches = [p("in", 8), Punch("out", "1001", DAY + timedelta(days=3))]
    result = pair_punches(punches, PairingConfig(max_shift_hours=16))

    interval = result.intervals[0]
    assert interval.duration_hours == 16.0
    assert interval.auto_closed is True
    assert "Auto-closed at 16h" in interval.notes[0]


def test_orphan_checkout_flag_policy_creates_a_zero_length_record():
    config = PairingConfig(mode=PairingMode.state_based, orphan_out_policy="flag")
    result = pair_punches([p("out", 17, direction=Direction.outward)], config)

    assert len(result.intervals) == 1
    assert result.intervals[0].orphan_out is True
    assert result.intervals[0].duration_hours == 0.0


def test_orphan_checkout_ignore_policy_drops_it():
    config = PairingConfig(mode=PairingMode.state_based, orphan_out_policy="ignore")
    result = pair_punches([p("out", 17, direction=Direction.outward)], config)

    assert result.intervals == []
    assert result.skipped_punch_ids == ["out"]


def test_empty_stream_is_not_an_error():
    assert pair_punches([], PairingConfig()).intervals == []
