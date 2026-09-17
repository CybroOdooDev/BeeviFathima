"""Punch stream -> attendance intervals.

This is the part of the product that earns the subscription. Raw punches are
messy: devices are configured without IN/OUT keys, people double-tap, night
shifts cross midnight, and somebody always forgets to punch out.

Pure functions only — no database, no network — so the rules can be tested
exhaustively and cheaply.

The one structural difference from the naive design
---------------------------------------------------
``pair_punches`` takes the employee's **currently open shift** as an input.

Without it, a check-in that arrived in an earlier cycle is already marked synced
and no longer in the working set, so the next cycle sees a lone check-out, pairs
it as a fresh *open* interval starting at the check-out time, and writes a
phantom record. That is not an edge case: it is what happens on every normal
shift, because the whole point of polling every few minutes is that the check-out
has not happened yet when the check-in is ingested.

Passing the open shift in makes a lone check-out do the only sensible thing —
close the shift it belongs to.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.services.timeutils import shift_day


class PairingMode(str, enum.Enum):
    state_based = "state_based"   # trust the device's own in/out coding
    alternating = "alternating"   # in, out, in, out… chronologically
    first_last = "first_last"     # first punch in, last out, middle discarded


class Direction(str, enum.Enum):
    inward = "in"
    outward = "out"
    unknown = "unknown"


@dataclass(frozen=True)
class Punch:
    """Minimal input to pairing. Times are naive UTC."""

    punch_id: str
    emp_code: str
    time_utc: datetime
    direction: Direction = Direction.unknown
    terminal_sn: str | None = None


@dataclass(frozen=True)
class OpenShift:
    """A shift already open in Odoo, carried in from the previous cycle."""

    attendance_id: int
    check_in: datetime


@dataclass
class Interval:
    """A resolved interval, ready to push."""

    emp_code: str
    check_in: datetime
    check_out: datetime | None = None
    check_in_punch_id: str | None = None
    check_out_punch_id: str | None = None
    #: Set when this closes a shift opened in an earlier cycle, in which case
    #: check_in is the *recorded* check-in, not a punch from this batch.
    closes_attendance_id: int | None = None
    auto_closed: bool = False
    orphan_out: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.check_out is None

    @property
    def duration_hours(self) -> float | None:
        if self.check_out is None:
            return None
        return (self.check_out - self.check_in).total_seconds() / 3600


@dataclass
class PairingConfig:
    mode: PairingMode = PairingMode.alternating
    day_boundary_hour: int = 4
    min_punch_interval_seconds: int = 60
    max_shift_hours: int = 16
    orphan_out_policy: str = "flag"        # flag | create | ignore
    orphan_out_default_minutes: int = 480  # used when policy == "create"


@dataclass
class PairingResult:
    intervals: list[Interval] = field(default_factory=list)
    skipped_punch_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Dedupe
# --------------------------------------------------------------------------- #
def dedupe(punches: list[Punch], min_interval_seconds: int) -> tuple[list[Punch], list[str]]:
    """Drop device double-taps: punches too close to the previous accepted one.

    A same-direction punch inside the window is noise. A *different*-direction
    punch inside the window is kept, because "in then straight back out" is a
    legitimate if brief visit, and dropping it silently loses real data.
    """
    if min_interval_seconds <= 0:
        return punches, []

    kept: list[Punch] = []
    dropped: list[str] = []
    window = timedelta(seconds=min_interval_seconds)

    for punch in sorted(punches, key=lambda p: (p.time_utc, p.punch_id)):
        if kept:
            previous = kept[-1]
            same_direction = (
                punch.direction == previous.direction
                or punch.direction is Direction.unknown
                or previous.direction is Direction.unknown
            )
            if punch.time_utc - previous.time_utc < window and same_direction:
                dropped.append(punch.punch_id)
                continue
        kept.append(punch)

    return kept, dropped


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def pair_punches(
    punches: list[Punch],
    config: PairingConfig,
    open_shift: OpenShift | None = None,
) -> PairingResult:
    """Pair one employee's punches into intervals.

    ``open_shift`` is the shift already open in Odoo for this person. When it is
    set, the first punch that reads as a check-out closes it rather than opening
    something new.
    """
    result = PairingResult()
    if not punches:
        return result

    clean, dropped = dedupe(punches, config.min_punch_interval_seconds)
    result.skipped_punch_ids.extend(dropped)
    if not clean:
        return result

    emp_code = clean[0].emp_code

    if config.mode is PairingMode.first_last:
        # The only mode where "a day" is part of the definition, so the only one
        # that groups. day_boundary_hour decides which day a punch belongs to.
        days: dict[object, list[Punch]] = {}
        for punch in clean:
            days.setdefault(shift_day(punch.time_utc, config.day_boundary_hour), []).append(punch)
        carried = open_shift
        for day in sorted(days):
            same_day = sorted(days[day], key=lambda p: p.time_utc)
            result.intervals.extend(_pair_first_last(emp_code, same_day, carried))
            carried = None  # only the earliest group can close the carried shift
    elif config.mode is PairingMode.state_based:
        result.intervals.extend(_pair_state_based(emp_code, clean, config, result, open_shift))
    else:
        # Alternating and state-based walk the stream continuously, so a shift
        # crossing midnight stays one interval. Grouping by day here would split
        # every night shift into two open records. Runaway intervals are caught
        # by max_shift_hours below.
        result.intervals.extend(_pair_alternating(emp_code, clean, open_shift))

    _apply_guard_rails(result, config)
    return result


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
def _closing_interval(emp_code: str, open_shift: OpenShift, punch: Punch) -> Interval:
    """Turn a lone check-out into the closure of an already-open shift."""
    return Interval(
        emp_code,
        check_in=open_shift.check_in,
        check_out=punch.time_utc,
        check_out_punch_id=punch.punch_id,
        closes_attendance_id=open_shift.attendance_id,
    )


def _pair_alternating(
    emp_code: str, punches: list[Punch], open_shift: OpenShift | None
) -> list[Interval]:
    intervals: list[Interval] = []
    current: Interval | None = None

    for punch in punches:
        if current is None and open_shift is not None:
            # A shift is already running: this punch ends it. Anything earlier
            # than the recorded check-in is out of order and cannot close it.
            if punch.time_utc > open_shift.check_in:
                intervals.append(_closing_interval(emp_code, open_shift, punch))
                open_shift = None
                continue
            open_shift = None  # give up on it; treat the stream as fresh

        if current is None:
            current = Interval(emp_code, punch.time_utc, check_in_punch_id=punch.punch_id)
        else:
            current.check_out = punch.time_utc
            current.check_out_punch_id = punch.punch_id
            intervals.append(current)
            current = None

    if current is not None:
        intervals.append(current)  # odd punch count: leave the last one open
    return intervals


def _pair_state_based(
    emp_code: str,
    punches: list[Punch],
    config: PairingConfig,
    result: PairingResult,
    open_shift: OpenShift | None,
) -> list[Interval]:
    # A device configured without an OUT function key stamps every punch "Check
    # In". Trusting that opens one attendance per punch, and each one then
    # blocks the next. Detect it and fall back rather than produce that.
    if len(punches) > 1 and not any(p.direction is Direction.outward for p in punches):
        result.warnings.append(
            f"{emp_code}: no check-out states in this batch — the device is "
            f"probably configured without an OUT key. Falling back to alternating."
        )
        return _pair_alternating(emp_code, punches, open_shift)

    intervals: list[Interval] = []
    current: Interval | None = None

    for punch in punches:
        direction = punch.direction
        if direction is Direction.unknown:
            direction = Direction.outward if (current or open_shift) else Direction.inward

        if direction is Direction.inward:
            if open_shift is not None:
                # Forgot to badge out. Close the old shift where the new one
                # starts, so the employee is not recorded present through the
                # night and the overlap constraint stays satisfied.
                if punch.time_utc > open_shift.check_in:
                    closing = Interval(
                        emp_code,
                        check_in=open_shift.check_in,
                        check_out=punch.time_utc,
                        closes_attendance_id=open_shift.attendance_id,
                        notes=["Auto-closed: no check-out before the next check-in"],
                        auto_closed=True,
                    )
                    intervals.append(closing)
                open_shift = None
            if current is not None:
                current.notes.append("Missing check-out before next check-in")
                intervals.append(current)
            current = Interval(emp_code, punch.time_utc, check_in_punch_id=punch.punch_id)
            continue

        # Outward
        if current is not None:
            current.check_out = punch.time_utc
            current.check_out_punch_id = punch.punch_id
            intervals.append(current)
            current = None
            continue
        if open_shift is not None and punch.time_utc > open_shift.check_in:
            intervals.append(_closing_interval(emp_code, open_shift, punch))
            open_shift = None
            continue
        orphan = _handle_orphan_out(emp_code, punch, config)
        if orphan is not None:
            intervals.append(orphan)
        else:
            result.skipped_punch_ids.append(punch.punch_id)
            result.warnings.append(
                f"{emp_code}: check-out at {punch.time_utc} has no matching check-in"
            )

    if current is not None:
        intervals.append(current)
    return intervals


def _pair_first_last(
    emp_code: str, punches: list[Punch], open_shift: OpenShift | None
) -> list[Interval]:
    intervals: list[Interval] = []

    if open_shift is not None and punches and punches[-1].time_utc > open_shift.check_in:
        # Close the carried shift at the last punch of this group, then treat
        # anything after it as a new day.
        intervals.append(_closing_interval(emp_code, open_shift, punches[-1]))
        return intervals

    first = punches[0]
    if len(punches) == 1:
        return [Interval(emp_code, first.time_utc, check_in_punch_id=first.punch_id)]

    last = punches[-1]
    intervals.append(
        Interval(
            emp_code,
            first.time_utc,
            last.time_utc,
            check_in_punch_id=first.punch_id,
            check_out_punch_id=last.punch_id,
            notes=(
                [f"{len(punches) - 2} intermediate punch(es) ignored (first/last mode)"]
                if len(punches) > 2
                else []
            ),
        )
    )
    return intervals


def _handle_orphan_out(emp_code: str, punch: Punch, config: PairingConfig) -> Interval | None:
    if config.orphan_out_policy == "ignore":
        return None
    if config.orphan_out_policy == "create":
        return Interval(
            emp_code,
            punch.time_utc - timedelta(minutes=config.orphan_out_default_minutes),
            punch.time_utc,
            check_out_punch_id=punch.punch_id,
            orphan_out=True,
            notes=["Check-in inferred: orphan check-out"],
        )
    return Interval(  # policy == "flag"
        emp_code,
        punch.time_utc,
        punch.time_utc,
        check_in_punch_id=punch.punch_id,
        check_out_punch_id=punch.punch_id,
        orphan_out=True,
        notes=["Orphan check-out — zero-length record flagged for review"],
    )


# --------------------------------------------------------------------------- #
# Guard rails
# --------------------------------------------------------------------------- #
def _apply_guard_rails(result: PairingResult, config: PairingConfig) -> None:
    """Never let a forgotten punch-out write a 300-hour attendance."""
    limit = timedelta(hours=config.max_shift_hours)

    for interval in result.intervals:
        if interval.check_out is None:
            continue
        if interval.check_out - interval.check_in > limit:
            raw = interval.check_out
            interval.check_out = interval.check_in + limit
            interval.auto_closed = True
            interval.notes.append(
                f"Auto-closed at {config.max_shift_hours}h "
                f"(raw check-out was {raw.isoformat(sep=' ')})"
            )
            result.warnings.append(
                f"{interval.emp_code}: shift starting {interval.check_in} exceeded "
                f"{config.max_shift_hours}h and was auto-closed"
            )


def close_stale_at(check_in: datetime, now: datetime, config: PairingConfig) -> datetime | None:
    """When to auto-close a shift left open past the limit, if it is time."""
    limit = timedelta(hours=config.max_shift_hours)
    return check_in + limit if now - check_in > limit else None
