#!/usr/bin/env python3
"""Re-push punches to Odoo after fixing something on the Odoo side.

Read this first, because the obvious plan does not work:

**"Delete the attendance in Odoo and sync again" re-creates nothing.** A punch
that reached Odoo is marked ``synced`` and the push step never looks at it
again — by design, since that is what stops every sync rewriting the whole
history. Delete the Odoo records and the next sync reports success, having done
nothing. The punches have to be moved back to ``pending`` first, which is what
this script does.

**A punch that failed five times is abandoned silently.** The push only
considers ``attempts < 5``. Past that the run reports **success** with zero
errors while the punches sit in ``error`` forever. Deleting the conflicting Odoo
record does not revive them; only resetting the counter does.

**Re-pushing duplicates the local mirror.** Attendance rows are keyed on the
Odoo record id, so a shift re-created under a new id appears twice on the
Attendance screen — once for the dead id, once for the new one. ``--prune``
reports these; clearing them needs a database change, so it is deliberately
not automated here.

Nothing is destructive without a flag. The default is a report.

    # what is stuck, and why
    python3 tools/resync.py --base http://127.0.0.1:8000 --email you@example.com

    # put the failed punches back in the queue, then sync
    python3 tools/resync.py --email you@example.com --reset-errors --sync

    # re-push one badge's already-synced days (after deleting them in Odoo)
    python3 tools/resync.py --email you@example.com --badge 1002 \\
        --from 2026-09-14 --to 2026-09-14 --reset-synced --sync
"""

from __future__ import annotations

import argparse
import collections
import getpass
import sys

import requests


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


class Client:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def get(self, path: str, **params):
        r = requests.get(f"{self.base}/api/v1{path}", params=params,
                         headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, **params):
        r = requests.post(f"{self.base}/api/v1{path}", params=params,
                          headers=self.headers, timeout=300)
        r.raise_for_status()
        return r.json() if r.content else {}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default="http://127.0.0.1:8000")
    p.add_argument("--email", required=True)
    p.add_argument("--password")
    p.add_argument("--badge", help="Limit to one emp_code.")
    p.add_argument("--device", help="Limit to one terminal serial.")
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD, inclusive, UTC.")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD, inclusive, UTC.")
    p.add_argument("--reset-errors", action="store_true",
                   help="Queue punches stuck in error, clearing the attempt counter.")
    p.add_argument("--reset-synced", action="store_true",
                   help="Queue punches that ALREADY reached Odoo, so they are written "
                        "again. Only after deleting the matching Odoo records, or Odoo "
                        "will refuse them as overlaps.")
    p.add_argument("--sync", action="store_true", help="Run a cycle when done.")
    p.add_argument("--prune", action="store_true",
                   help="Report local attendance rows whose Odoo record is gone.")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = p.parse_args()

    password = args.password or getpass.getpass(f"Password for {args.email}: ")
    auth = requests.post(f"{args.base.rstrip('/')}/api/v1/auth/login",
                         json={"email": args.email, "password": password}, timeout=60)
    if auth.status_code != 200:
        print(f"Login failed: HTTP {auth.status_code} {auth.text[:200]}", file=sys.stderr)
        return 2
    api = Client(args.base, auth.json()["access_token"])

    scope = {k: v for k, v in {
        "emp_code": args.badge, "terminal_sn": args.device,
        "date_from": args.date_from, "date_to": args.date_to,
    }.items() if v}

    punches = api.get("/punches", limit=500, **scope)
    if not punches:
        print("No punches match that scope.")
        return 0

    states = collections.Counter(x["process_state"] for x in punches)
    print(f"\n{len(punches)} punch(es) in scope: "
          + ", ".join(f"{s} {n}" for s, n in states.most_common()))

    errored = [x for x in punches if x["process_state"] == "error"]
    if errored:
        attempts = collections.Counter(x["attempts"] for x in errored)
        print(f"\n{len(errored)} in error, by attempt count: {dict(sorted(attempts.items()))}")
        abandoned = [x for x in errored if x["attempts"] >= 5]
        if abandoned:
            print(f"  {len(abandoned)} of them have hit the 5-attempt cap — the sync no "
                  f"longer tries these at all, and reports success while it skips them.")
        seen = set()
        for x in errored:
            message = x.get("error_message") or "(none recorded)"
            if message in seen:
                continue
            seen.add(message)
            print(f"\n  {x['emp_code']} at {str(x['punch_time_utc'])[:19]}Z")
            print(f"    {message[:220]}")

    if args.prune:
        attendance = api.get("/attendance", limit=500,
                             **{k: v for k, v in scope.items() if k != "terminal_sn"})
        by_shift = collections.defaultdict(list)
        for a in attendance:
            by_shift[(a["emp_code"], a["shift_date"])].append(a["odoo_attendance_id"])
        dupes = {k: v for k, v in by_shift.items() if len(v) > 1}
        print(f"\n{len(dupes)} shift(s) mirrored more than once"
              + (" — each extra row points at an Odoo record that no longer exists:"
                 if dupes else "."))
        for (badge, day), ids in sorted(dupes.items()):
            print(f"  {badge}  {day}  odoo ids {sorted(i for i in ids if i)}")

    targets: list[dict] = []
    if args.reset_errors:
        targets += errored
    if args.reset_synced:
        targets += [x for x in punches if x["process_state"] == "synced"]

    if not targets:
        if not (args.reset_errors or args.reset_synced):
            print("\nReport only. Add --reset-errors (and --sync) to act on it.")
        return 0

    print(f"\nAbout to queue {len(targets)} punch(es) for re-push.")
    if args.reset_synced:
        print("  --reset-synced is included: punches that already reached Odoo will be")
        print("  written AGAIN. If their Odoo records still exist, Odoo refuses them as")
        print("  overlaps and they land in error instead. Delete those records first.")
    if not args.yes:
        if input("Type 'yes' to continue: ").strip().lower() != "yes":
            print("aborted")
            return 1

    done = failed = 0
    for punch in targets:
        try:
            api.post(f"/punches/{punch['id']}/retry")
            done += 1
        except requests.HTTPError as exc:
            failed += 1
            print(f"  could not queue {punch['id'][:8]}: {exc}")
    print(f"queued {done} punch(es)" + (f", {failed} failed" if failed else ""))

    if args.sync:
        run = api.post("/sync/run-inline")
        print(f"\nsync: {run['status']} — {run['attendances_created']} created, "
              f"{run['attendances_closed']} closed, {run['error_count']} error(s)")
        if run.get("error_message"):
            print(f"  {run['error_message']}")
        after = collections.Counter(
            x["process_state"] for x in api.get("/punches", limit=500, **scope)
        )
        print("ledger now: " + ", ".join(f"{s} {n}" for s, n in after.most_common()))
    else:
        print("\nQueued only. They go out on the next sync, or add --sync.")

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
