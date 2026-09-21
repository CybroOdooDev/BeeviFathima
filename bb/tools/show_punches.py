#!/usr/bin/env python3
"""Print the punch ledger from a running BioBridge, grouped by terminal.

The ledger is what you reach for when Odoo has rejected something: it holds
every punch ever pulled, with the time as the device reported it, the time in
UTC, what BioBridge decided it was, and the Odoo record it ended up in — or the
error it hit.

    python3 tools/show_punches.py --base http://127.0.0.1:8000 \\
        --email you@example.com --password '...'

    # one badge, around the day Odoo complained about
    python3 tools/show_punches.py --badge 1002 --from 2026-09-13 --to 2026-09-15

    # one terminal, only what failed
    python3 tools/show_punches.py --device MOCK-GATE-01 --state error

    --csv punches.csv  writes the same rows out for a spreadsheet.

Times print in UTC and in the device's local wall-clock, side by side. That
pairing is the point: BioTime reports local time with no offset and Odoo stores
naive UTC, so nearly every "the times are wrong" report is visible right here as
a constant offset in the wrong direction.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import sys
from collections import Counter, defaultdict

import requests

STATE_ORDER = ["synced", "pending", "unmapped", "error", "skipped"]


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


def fetch_all(base: str, token: str, params: dict, cap: int) -> list[dict]:
    """Page through the ledger. The endpoint caps a page at 500."""
    out: list[dict] = []
    offset = 0
    while len(out) < cap:
        page_size = min(500, cap - len(out))
        response = requests.get(
            f"{base}/api/v1/punches",
            params={**params, "limit": page_size, "offset": offset},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        response.raise_for_status()
        page = response.json()
        out.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", help="Prompted for if omitted.")
    parser.add_argument("--badge", help="emp_code, exactly as the device reports it.")
    parser.add_argument("--device", help="Terminal serial number.")
    parser.add_argument("--state", choices=STATE_ORDER, help="Only this process state.")
    parser.add_argument("--run", help="Only punches this sync run first ingested. "
                                      "Take the id from --runs.")
    parser.add_argument("--runs", action="store_true",
                        help="List recent sync runs with their ids and counts, then exit.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="With --runs, also print each run's error message and its "
                             "full log — the exact fetch window and timezone it used, "
                             "the same way sync_engine wrote it. This is where 'fetched 0 "
                             "but the source clearly has punches' gets explained: compare "
                             "the printed window against the timestamps in your source data.")
    parser.add_argument("--from", dest="date_from", help="YYYY-MM-DD, inclusive, UTC.")
    parser.add_argument("--to", dest="date_to", help="YYYY-MM-DD, inclusive, UTC.")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--csv", help="Also write the rows to this file.")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    password = args.password or getpass.getpass(f"Password for {args.email}: ")

    auth = requests.post(
        f"{base}/api/v1/auth/login",
        json={"email": args.email, "password": password},
        timeout=60,
    )
    if auth.status_code != 200:
        print(f"Login failed: HTTP {auth.status_code} {auth.text[:200]}", file=sys.stderr)
        return 2
    token = auth.json()["access_token"]

    if args.runs:
        runs = requests.get(
            f"{base}/api/v1/sync/runs", params={"limit": 25},
            headers={"Authorization": f"Bearer {token}"}, timeout=60,
        ).json()
        print(f"\n{'run id':<34} {'started (UTC)':<21} {'result':<9} "
              f"{'fetched':>7} {'new':>4}  trigger")
        print("-" * 92)
        for r in runs:
            print(f"{r['id']:<34} {str(r['started_at'])[:19]:<21} {r['status']:<9} "
                  f"{r['punches_fetched']:>7} {r['punches_new']:>4}  {r['triggered_by']}")
            if args.verbose:
                if r.get("error_message"):
                    print(f"    ! {r['error_message']}")
                for line in r.get("log") or []:
                    print(f"    {line}")
                print()
        print("\nFetched minus new is the overlap re-read: every run deliberately "
              "re-reads a\nwindow of known punches, which stay listed under the run "
              "that first saw them.")
        print("\nThen: --run <id> for the punches one run brought in"
              + ("." if args.verbose else ", or rerun with -v for the fetch window and log."))
        return 0

    params = {
        k: v
        for k, v in {
            "emp_code": args.badge,
            "terminal_sn": args.device,
            "state": args.state,
            "run_id": args.run,
            "date_from": args.date_from,
            "date_to": args.date_to,
        }.items()
        if v
    }
    punches = fetch_all(base, token, params, args.limit)

    if not punches:
        print("No punches match those filters.")
        print("The ledger only holds what a sync has pulled — if a sync has not run "
              "since these punches were recorded, they are still on the device platform.")
        return 0

    # Oldest first: reading a shift top to bottom is how you spot a missing
    # check-out, and the API returns newest first.
    punches.sort(key=lambda p: p["punch_time_utc"])

    by_device: dict[str, list[dict]] = defaultdict(list)
    for punch in punches:
        by_device[punch.get("terminal_sn") or "(no terminal reported)"].append(punch)

    for serial in sorted(by_device):
        rows = by_device[serial]
        states = Counter(r["process_state"] for r in rows)
        summary = "  ".join(f"{s}={states[s]}" for s in STATE_ORDER if states[s])
        print(f"\n{'=' * 104}")
        print(f"{serial}   {len(rows)} punch(es)   {summary}")
        print("=" * 104)
        print(f"{'punch time (UTC)':<20} {'device local':<20} {'badge':<10} "
              f"{'dir':<5} {'state':<9} {'odoo':>6}  detail")
        print("-" * 104)
        for r in rows:
            detail = r.get("error_message") or ""
            if r.get("attempts"):
                detail = f"[try {r['attempts']}] {detail}" if detail else f"attempts: {r['attempts']}"
            print(
                f"{str(r['punch_time_utc'])[:19]:<20} "
                f"{str(r.get('punch_time_local') or '—')[:19]:<20} "
                f"{r['emp_code']:<10} {r['direction']:<5} {r['process_state']:<9} "
                f"{str(r.get('odoo_attendance_id') or '—'):>6}  {detail[:34]}"
            )

    totals = Counter(p["process_state"] for p in punches)
    print(f"\n{len(punches)} punch(es) across {len(by_device)} terminal(s): "
          + ", ".join(f"{state} {count}" for state, count in totals.most_common()))

    # The errors are the reason anyone opens this, so repeat them in full rather
    # than truncated into a column.
    errored = [p for p in punches if p["process_state"] == "error"]
    if errored:
        print(f"\n--- {len(errored)} punch(es) in error, with the full message ---")
        seen: set[str] = set()
        for punch in errored:
            message = punch.get("error_message") or "(no message recorded)"
            if message in seen:
                continue
            seen.add(message)
            print(f"\n  {punch['emp_code']} at {str(punch['punch_time_utc'])[:19]} UTC")
            print(f"    {message}")
        print("\nRetry them from Activity in the dashboard, or POST "
              "/api/v1/punches/<id>/retry — the punches are kept, so nothing is lost.")

    if args.csv:
        fields = ["punch_time_utc", "punch_time_local", "emp_code", "direction",
                  "terminal_sn", "process_state", "odoo_attendance_id",
                  "attempts", "error_message"]
        with open(args.csv, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(punches)
        print(f"\nwrote {len(punches)} row(s) to {args.csv}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.ConnectionError as exc:
        print(_unreachable(exc), file=sys.stderr)
        sys.exit(3)
    except requests.HTTPError as exc:
        print(f"\nHTTP error: {exc}\n{exc.response.text[:400]}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)