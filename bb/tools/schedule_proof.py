#!/usr/bin/env python3
"""Prove the sync runs on its own, against a live Odoo, with nothing pressed.

Unit tests prove the rules. This proves the product: a tenant is created,
both sides are connected, an interval is set — and then the script does
nothing but watch. Every sync it reports is one the scheduler started.

The distinction that matters is ``triggered_by``. A run tagged ``manual`` proves
only that the button works; this script fails unless it sees ``schedule``.

    # a mock device platform, so no hardware is needed
    python3 tools/mock_biotime.py --port 8099 --punches /tmp/punches.json &

    # --punches must be the SAME path the mock was started with; this script
    # writes the shift there for the mock to serve.
    python3 tools/schedule_proof.py \\
        --base http://127.0.0.1:8000 \\
        --odoo-url https://your.odoo.com --odoo-db yourdb \\
        --odoo-user you@example.com --odoo-key <api-key> \\
        --biotime http://127.0.0.1:8099 --punches /tmp/punches.json

Run the API with a short tick so this finishes in a minute rather than fifteen:

    SCHEDULER_TICK_SECONDS=5 uvicorn app.main:app
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

RESULTS: list[tuple[str, bool, str]] = []


def _unreachable(exc: Exception) -> str:
    """Explain an unreachable API in one line.

    A refused connection otherwise arrives as twenty lines of urllib3 internals,
    which reads like a bug in this script rather than "nothing is listening
    there" — and the usual cause is simply that the API is on another port, or
    not started.

    --base is read back out of argv because this runs from the top-level
    exception handler, where the parsed arguments are long out of scope.
    """
    base = "http://127.0.0.1:8000"
    for i, arg in enumerate(sys.argv):
        if arg == "--base" and i + 1 < len(sys.argv):
            base = sys.argv[i + 1]
        elif arg.startswith("--base="):
            base = arg.split("=", 1)[1]
    return (
        f"\nCannot reach BioBridge at {base} — nothing is listening there "
        f"({type(exc).__name__}).\n\n"
        f"  Is it up?          curl -sS {base}/health\n"
        f"  On another port?   ss -ltnp | grep -E 'uvicorn|:80[0-9][0-9]'\n"
        f"  Start it:          uvicorn app.main:app --port 8000\n"
        f"  Then pass the port you are actually using with --base.\n"
    )


def check(label: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((label, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


class Client:
    """The smallest thing that can hold a bearer token."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token: str | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def post(self, path: str, payload: dict | None = None) -> dict:
        response = requests.post(
            f"{self.base}/api/v1{path}", json=payload, headers=self._headers(), timeout=60
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def patch(self, path: str, payload: dict) -> dict:
        response = requests.patch(
            f"{self.base}/api/v1{path}", json=payload, headers=self._headers(), timeout=60
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def get(self, path: str) -> dict:
        response = requests.get(
            f"{self.base}/api/v1{path}", headers=self._headers(), timeout=60
        )
        response.raise_for_status()
        return response.json()


def seed_punches(
    path: str, tz_offset_hours: int, *, day_offset: int, hour: int, length_hours: int
) -> list[dict]:
    """Write a fresh in/out pair, timestamped as the device would.

    BioTime reports local wall-clock with no offset, so the file has to be
    written in the device's zone — the same conversion the engine has to undo.

    The window is a parameter and not "a few hours ago" for a reason: Odoo
    refuses an ``hr.attendance`` that overlaps an existing one for the same
    employee, so a window landing on top of a record some other test left behind
    fails the proof for a reason that has nothing to do with scheduling. Point it
    at hours where badge 1001 has no attendance.
    """
    local_day = (datetime.now(timezone.utc) + timedelta(hours=tz_offset_hours)).date()
    local_day -= timedelta(days=day_offset)
    check_in = datetime.combine(local_day, datetime.min.time()).replace(hour=hour)
    check_out = check_in + timedelta(hours=length_hours)
    stamp = int(time.time())

    punches = [
        {
            "id": stamp,
            "emp_code": "1001",
            "punch_time": check_in.strftime("%Y-%m-%d %H:%M:%S"),
            "punch_state": "0",
            "verify_type": "1",
            "terminal_sn": "MOCK-GATE-01",
            "first_name": "Ahmed",
            "last_name": "Sharma",
        },
        {
            "id": stamp + 1,
            "emp_code": "1001",
            "punch_time": check_out.strftime("%Y-%m-%d %H:%M:%S"),
            "punch_state": "1",
            "verify_type": "1",
            "terminal_sn": "MOCK-GATE-01",
            "first_name": "Ahmed",
            "last_name": "Sharma",
        },
    ]
    with open(path, "w") as handle:
        json.dump(punches, handle, indent=2)
    return punches


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--odoo-url", required=True)
    parser.add_argument("--odoo-db", required=True)
    parser.add_argument("--odoo-user", required=True)
    parser.add_argument("--odoo-key", required=True)
    parser.add_argument("--biotime", default="http://127.0.0.1:8099")
    parser.add_argument("--punches", default="/tmp/punches.json")
    parser.add_argument("--timezone", default="Asia/Dubai")
    parser.add_argument("--tz-offset-hours", type=int, default=4)
    parser.add_argument(
        "--wait",
        type=int,
        default=180,
        help="Seconds to wait for a scheduled run before giving up.",
    )
    parser.add_argument(
        "--punch-day-offset",
        type=int,
        default=1,
        help="Days back for the seeded shift (default: yesterday).",
    )
    parser.add_argument(
        "--punch-hour",
        type=int,
        default=18,
        help="Local hour the seeded shift starts. Choose hours where badge 1001 "
             "has no attendance in the target Odoo — an overlap is refused.",
    )
    parser.add_argument("--punch-length-hours", type=int, default=2)
    args = parser.parse_args()

    client = Client(args.base)
    try:
        return _proof(client, args)
    finally:
        # The tenant this script created has a one-minute interval. Leaving it
        # enabled after a crash means a stray account keeps syncing the shared
        # punch file behind the next run's back — which is how a later proof
        # fails with an Odoo overlap error and nothing to do with scheduling.
        if client.token:
            try:
                client.patch("/tenant", {"sync_enabled": False})
                print("\n(cleanup: the proof tenant's automatic sync is off)")
            except Exception as exc:  # noqa: BLE001
                print(f"\n(cleanup failed: {exc} — disable it by hand)")


def _proof(client: Client, args) -> int:
    # --- the scheduler must be alive before anything else is worth doing ----
    print("\n--- scheduler ---")
    health = requests.get(f"{args.base}/health/scheduler", timeout=30)
    body = health.json()
    if not check(
        "the scheduler reports itself running",
        health.status_code == 200 and body.get("status") == "ok",
        f"HTTP {health.status_code} {body}",
    ):
        print(
            "\nNothing is running the clock, so there is no point waiting for a "
            "scheduled sync. Check SCHEDULER_MODE and the service log."
        )
        return 1
    print(f"         mode={body.get('mode')} owner={body.get('owner')}")

    # --- a tenant with both sides connected ---------------------------------
    print("\n--- setup ---")
    email = f"sched{int(time.time())}@example.com"
    tokens = client.post(
        "/auth/signup",
        {
            "company_name": "Schedule Proof Ltd",
            "email": email,
            "password": "a-long-enough-password",
            "timezone": args.timezone,
        },
    )
    client.token = tokens["access_token"]
    check("a tenant exists", bool(client.token))

    client.post(
        "/odoo-connections",
        {
            "name": "Odoo",
            "url": args.odoo_url,
            "db_name": args.odoo_db,
            "username": args.odoo_user,
            "api_key": args.odoo_key,
        },
    )
    source = client.post(
        "/sources",
        {
            "name": "BioTime",
            "provider": "biotime",
            "base_url": args.biotime,
            "username": "mock",
            "password": "mock",
            "server_timezone": args.timezone,
        },
    )
    found = client.post(f"/sources/{source['id']}/discover-devices")
    # The endpoint answers with the device list, not a message.
    count = len(found) if isinstance(found, list) else found.get("count", "?")
    check("both sides are connected", True, f"{count} terminal(s) imported")

    # --- one minute, and then hands off -------------------------------------
    client.patch("/tenant", {"sync_interval_minutes": 1, "sync_enabled": True})
    seeded = seed_punches(
        args.punches,
        args.tz_offset_hours,
        day_offset=args.punch_day_offset,
        hour=args.punch_hour,
        length_hours=args.punch_length_hours,
    )
    check(
        "punches are waiting on the device platform",
        True,
        f"{len(seeded)} punch(es), {seeded[0]['punch_time']} → {seeded[-1]['punch_time']} local",
    )

    baseline = client.get("/sync/runs?limit=50")
    print(f"\n--- waiting up to {args.wait}s. Nothing below is triggered by this script ---")

    deadline = time.time() + args.wait
    scheduled_run = None
    while time.time() < deadline:
        time.sleep(5)
        runs = client.get("/sync/runs?limit=50")
        fresh = [r for r in runs if r["id"] not in {b["id"] for b in baseline}]
        by_schedule = [r for r in fresh if r["triggered_by"] == "schedule"]
        # Only a finished run is worth asserting on. A run read mid-flight still
        # has zeroed counters and status "running", so taking the first one that
        # appears fails the checks below for timing reasons alone.
        finished = [r for r in by_schedule if r.get("finished_at")]
        waited = int(args.wait - (deadline - time.time()))
        print(
            f"    {waited:>3}s  {len(fresh)} new run(s), "
            f"{len(by_schedule)} scheduled, {len(finished)} finished"
        )
        if finished:
            scheduled_run = finished[0]
            break

    if not check(
        "a sync ran without anyone triggering it",
        scheduled_run is not None,
        f"waited {args.wait}s" if scheduled_run is None else
        f"run {scheduled_run['id'][:8]} at {scheduled_run['started_at']}",
    ):
        return 1

    check(
        "the scheduled run succeeded",
        scheduled_run["status"] == "success",
        f"status={scheduled_run['status']} error={scheduled_run.get('error_message')}",
    )
    check(
        "it pulled the punches",
        scheduled_run["punches_new"] >= len(seeded),
        f"{scheduled_run['punches_new']} new punch(es)",
    )

    # ...and they are *these* punches. Without this the proof passes on whatever
    # the device platform happened to be serving — including a stale punch file
    # from an earlier run, which is how "it synced" can be true while the file
    # this script just wrote was never read at all. The usual cause is
    # --punches pointing somewhere other than the path the mock was started with.
    ledger = client.get("/punches?limit=200")
    # The API serialises datetimes as ISO ("2026-09-13T19:00:00"); the punch file
    # uses BioTime's space-separated form. Normalise, or nothing ever matches.
    minute = lambda value: str(value or "").replace("T", " ")[:16]  # noqa: E731
    seen = {minute(p.get("punch_time_local")) for p in ledger}
    expected = {minute(p["punch_time"]) for p in seeded}
    check(
        "the punches it pulled are the ones just seeded",
        expected <= seen,
        f"missing {sorted(expected - seen)}"
        if expected - seen
        else f"{sorted(expected)} present",
    )

    # --- and it reached Odoo ------------------------------------------------
    print("\n--- what landed in Odoo ---")
    attendance = client.get("/attendance?limit=20")
    written = [a for a in attendance if a.get("odoo_attendance_id")]
    check(
        "attendance was written to Odoo by the scheduled run",
        bool(written),
        f"{len(written)} interval(s), first odoo id "
        f"{written[0]['odoo_attendance_id'] if written else '—'}",
    )

    dashboard = client.get("/dashboard")
    schedule = dashboard.get("schedule", {})
    check(
        "the dashboard reports the schedule as live",
        schedule.get("running") is True,
        f"mode={schedule.get('mode')} next={schedule.get('next_run_at')}",
    )

    # --- turning it off actually turns it off -------------------------------
    print("\n--- the off switch ---")
    client.patch("/tenant", {"sync_enabled": False})
    after = client.get("/dashboard")
    check(
        "a disabled account has no next run",
        after["schedule"]["next_run_at"] is None,
        "next_run_at is null",
    )

    return _report()


def _report() -> int:
    print("\n" + "=" * 66)
    failed = [label for label, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS)} checks, {len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for label in failed:
        print(f"  FAILED: {label}")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.ConnectionError as exc:
        print(_unreachable(exc), file=sys.stderr)
        sys.exit(3)
    except requests.HTTPError as exc:
        print(f"\nHTTP error: {exc}\n{exc.response.text[:500]}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
