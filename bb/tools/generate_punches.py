#!/usr/bin/env python3
"""Generate a punches.json for tools/mock_biotime.py.

Hand-authoring this file bit us once already: a punch timestamped for 17:00
today, written at 15:00, sits in the future relative to whenever a sync
actually runs. BioTime's own fetch window silently drops anything past "now"
(``app/services/sync_engine.py:_fetch_and_ingest`` caps ``end_utc`` at
``utcnow + 5 minutes`` of clock-skew tolerance) — so the punch is simply never
fetched, and it looks exactly like a pairing bug rather than a clock problem.
That is how "attendance records were matched only for check-ins" happens.

This computes every timestamp relative to *now*, at the moment it runs — so run
it again whenever the punches feel stale rather than keeping one file around.
A shift still in progress today (check-in written, check-out time not yet
reached) is left with no check-out on purpose: that is a real, common state
worth having in the fixture, not a bug in the generator.

    python3 tools/generate_punches.py
    python3 tools/generate_punches.py --days 10 --emp-codes 1001,0042,A7,9001
    python3 tools/generate_punches.py --tz Asia/Kolkata --check-in 08:30 --check-out 17:30
    python3 tools/generate_punches.py --company 2
    python3 tools/generate_punches.py --company 4

Then:
    python3 tools/mock_biotime.py --punches punches.json
    python3 tools/mock_biotime.py --company 2 --punches punches_company2.json

--tz must match whatever the *device source* is configured with in BioBridge
("BioTime server timezone" on the Connections screen) — punch_time is a naive
local string, interpreted in that zone, not UTC and not the tenant's display
timezone unless the source leaves its own zone unset. Getting this wrong does
not error; it just shifts every punch by the difference and can push them
outside the fetch window, which again looks like a pairing bug.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - this project's floor is 3.10+
    ZoneInfo = None

# The rosters, matching tools/mock_biotime.py's DATASETS exactly. Any code
# still ingests fine even if it's not in either list below — mapping is
# punch-driven (app/services/sync_engine.py's _register_badges reads codes
# out of the punches themselves), not read from BioTime's employee list. But
# only a mock server started with the matching --company shows these codes on
# its /personnel/api/employees/ with a name and department, which is what
# makes a demo look real. Duplicated here rather than imported — same as
# TERMINALS always was — so this script has no import-time dependency on
# mock_biotime.py; keep the two in sync by hand if either roster changes.
COMPANY_EMP_CODES = {
    1: ["1001", "0042", "A7"],          # Ahmed Sharma, Sara Tanaka, Jane Haddad
    2: ["2001", "2002", "2003"],        # Liam Okafor, Priya Nakamura, Noah Fernandes
    4: ["5", "6001"],                   # Beevi, Marc
}

# Matches tools/mock_biotime.py's TERMINALS for each company. Employees
# alternate across them so one sync run exercises more than one device.
COMPANY_TERMINALS = {
    1: ["MOCK-GATE-01", "MOCK-GATE-02"],
    2: ["MOCK-GATE-03", "MOCK-GATE-04"],
    4: ["MOCK-GATE-05", "MOCK-GATE-06"],
}

# Kept for backward compatibility — anything importing this script's old
# module-level default still gets company 1's codes.
DEFAULT_EMP_CODES = COMPANY_EMP_CODES[1]

STATE_IN = "0"   # Check In
STATE_OUT = "1"  # Check Out
TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _resolve_tz(name: str):
    if ZoneInfo is None:
        raise SystemExit("Python's zoneinfo module is unavailable — use Python 3.9+.")
    try:
        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 - turned into a plain CLI error
        raise SystemExit(f"Unknown --tz {name!r}: {exc}")


def _parse_hhmm(value: str, flag: str) -> tuple[int, int]:
    try:
        hour, minute = (int(p) for p in value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour, minute
    except ValueError:
        raise SystemExit(f"{flag} must be HH:MM, got {value!r}")


def build(
    emp_codes: list[str],
    days: int,
    check_in: str,
    check_out: str,
    tz,
    jitter_minutes: int,
    skip_weekends: bool,
    seed: int | None,
    terminals: list[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Return (rows, still_clocked_in_codes).

    Oldest day first, matching how BioTime itself would hand them back in
    ascending order — not load-bearing for the mock server (it sorts anyway),
    but it makes the written file readable top to bottom.
    """
    terminals = terminals if terminals is not None else COMPANY_TERMINALS[1]
    rng = random.Random(seed)
    now = datetime.now(tz)
    ci_h, ci_m = _parse_hhmm(check_in, "--check-in")
    co_h, co_m = _parse_hhmm(check_out, "--check-out")

    def jitter(base: datetime) -> datetime:
        if jitter_minutes <= 0:
            return base
        return base + timedelta(minutes=rng.randint(-jitter_minutes, jitter_minutes))

    def row(id_: int, emp_code: str, at: datetime, state: str, terminal: str) -> dict:
        return {
            "id": id_,
            "emp_code": emp_code,
            "punch_time": at.strftime(TIME_FMT),
            "punch_state": state,
            "verify_type": "15",
            "terminal_sn": terminal,
            "first_name": "",
            "last_name": "",
        }

    rows: list[dict] = []
    still_in: list[str] = []
    next_id = 1

    for day_offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=day_offset)).date()
        if skip_weekends and day.weekday() >= 5:  # Saturday, Sunday
            continue

        for i, code in enumerate(emp_codes):
            terminal = terminals[i % len(terminals)]
            check_in_at = jitter(
                datetime(day.year, day.month, day.day, ci_h, ci_m, tzinfo=tz)
            )
            check_out_at = jitter(
                datetime(day.year, day.month, day.day, co_h, co_m, tzinfo=tz)
            )

            if check_in_at > now:
                continue  # this employee's day has not started yet

            rows.append(row(next_id, code, check_in_at, STATE_IN, terminal))
            next_id += 1

            if check_out_at <= now:
                rows.append(row(next_id, code, check_out_at, STATE_OUT, terminal))
                next_id += 1
            else:
                still_in.append(code)  # only possible on the most recent day

    return rows, still_in


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a punches.json for tools/mock_biotime.py, timed "
                     "relative to right now."
    )
    parser.add_argument(
        "--out", default=None,
        help="Where to write the punches (default: punches.json for --company 1, "
             "punches_company<N>.json otherwise, so generating for one company "
             "never clobbers another's file)",
    )
    parser.add_argument(
        "--company", type=int, default=1, choices=sorted(COMPANY_EMP_CODES),
        help="Which tools/mock_biotime.py roster to generate for: 1 (default) "
             "is Ahmed Sharma / Sara Tanaka / Jane Haddad on MOCK-GATE-01/02; "
             "2 is Liam Okafor / Priya Nakamura / Noah Fernandes on "
             "MOCK-GATE-03/04; 4 is Beevi (5) / Marc (6001) on "
             "MOCK-GATE-05/06. Sets the --emp-codes and terminal defaults; "
             "an explicit --emp-codes still overrides this.",
    )
    parser.add_argument(
        "--emp-codes", default=None,
        help="Comma-separated badge codes (default: the three --company's mock "
             "server already knows by name)",
    )
    parser.add_argument("--days", type=int, default=5,
                        help="How many days back, today included (default: 5)")
    parser.add_argument("--check-in", default="09:00")
    parser.add_argument("--check-out", default="17:00")
    parser.add_argument(
        "--tz", default="Asia/Dubai",
        help="Must match the device source's configured server timezone in "
             "BioBridge, not your own machine's (default: Asia/Dubai)",
    )
    parser.add_argument(
        "--jitter-minutes", type=int, default=4,
        help="Random spread around each time, so punches are not all "
             "identical to the second (default: 4; 0 to disable)",
    )
    parser.add_argument("--include-weekends", action="store_true",
                        help="By default Saturday and Sunday are skipped")
    parser.add_argument("--seed", type=int, default=None,
                        help="Fix the jitter for a reproducible file")
    args = parser.parse_args()

    emp_codes_str = args.emp_codes if args.emp_codes is not None else ",".join(COMPANY_EMP_CODES[args.company])
    emp_codes = [c.strip() for c in emp_codes_str.split(",") if c.strip()]
    if not emp_codes:
        raise SystemExit("--emp-codes produced no codes")
    if args.days < 1:
        raise SystemExit("--days must be at least 1")

    out = args.out if args.out is not None else (
        "punches.json" if args.company == 1 else f"punches_company{args.company}.json"
    )

    tz = _resolve_tz(args.tz)
    rows, still_in = build(
        emp_codes=emp_codes,
        days=args.days,
        check_in=args.check_in,
        check_out=args.check_out,
        tz=tz,
        jitter_minutes=args.jitter_minutes,
        skip_weekends=not args.include_weekends,
        seed=args.seed,
        terminals=COMPANY_TERMINALS[args.company],
    )

    if not rows:
        # Every day fell in the future or on a skipped weekend — most likely
        # someone running this before their configured check-in time with
        # --days 1. Nothing written, so it is obvious rather than a silent
        # empty file that then produces "nothing synced" three steps later.
        print("No punches generated — every requested day was in the future or "
              "skipped as a weekend. Try a larger --days, or pass "
              "--include-weekends.", file=sys.stderr)
        return 1

    with open(out, "w") as handle:
        json.dump(rows, handle, indent=2)
        handle.write("\n")

    span_start = min(r["punch_time"] for r in rows)
    span_end = max(r["punch_time"] for r in rows)
    print(f"Wrote {len(rows)} punches for {len(emp_codes)} employee(s) to {out}")
    print(f"  {span_start}  ->  {span_end}   ({args.tz})")
    if still_in:
        print(f"  still clocked in (no check-out yet, on purpose): "
              f"{', '.join(sorted(set(still_in)))}")
    print()
    company_flag = "" if args.company == 1 else f" --company {args.company}"
    print(f"  python3 tools/mock_biotime.py{company_flag} --punches {out}")
    print()
    print("Re-run this whenever the punches feel stale — every timestamp is "
          "computed relative to right now, so an old file is the only thing "
          "that goes wrong, never this script.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
