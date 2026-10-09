#!/usr/bin/env python3
"""Merge the duplicate daily records first/last pairing used to leave behind.

Before first/last paired the whole day, every sync cycle with activity opened
its own attendance record, so one person's day could be many short records.
This folds each such day into one: the earliest record is kept and stretched to
the day's first check-in and last check-out, and the others are deleted — in
Odoo and in BioBridge's own mirror. Punches that pointed at a deleted record are
repointed to the kept one.

    python3 tools/merge_first_last_days.py --tenant acme            # dry run
    python3 tools/merge_first_last_days.py --tenant acme --apply
    python3 tools/merge_first_last_days.py --tenant acme --since 2026-10-01 --apply

A dry run (the default) only prints what would change. --apply deletes
hr.attendance records in the customer's Odoo — permanent — so look at the plan
first. Only accounts whose pairing mode is first-in/last-out are touched, and only
days whose records were all written under that method (BioBridge's own record,
and Odoo's Pairing Method field where it exists). Days mixing methods are
reported and left alone. Run it from the repo root.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.integrations.odoo import OdooError, fmt_dt  # noqa: E402
from app.models import AttendanceRecord, OdooConnection, PunchRecord, Tenant  # noqa: E402
from app.services.connections import UnsafeTargetError, build_odoo_client  # noqa: E402
from app.services.pairing import PairingConfig, PairingMode, day_of  # noqa: E402
from app.services.timeutils import utc_to_local  # noqa: E402


def plan_for(db, tenant, since: date | None, skipped: list | None = None):
    config = PairingConfig(
        mode=PairingMode.first_last,
        day_boundary_hour=tenant.day_boundary_hour,
        timezone=tenant.timezone or "UTC",
    )
    rows = db.scalars(
        select(AttendanceRecord)
        .where(AttendanceRecord.tenant_id == tenant.id, AttendanceRecord.odoo_attendance_id.is_not(None))
        .order_by(AttendanceRecord.emp_code, AttendanceRecord.check_in)
    ).all()
    days: dict[tuple, list[AttendanceRecord]] = defaultdict(list)
    for r in rows:
        day = day_of(r.check_in, config)
        if since and day < since:
            continue
        days[(r.emp_code, day)].append(r)

    plan = []
    for (emp, day), recs in sorted(days.items()):
        if len(recs) < 2:
            continue
        # Only days written entirely under first/last pairing are merged. A day
        # with records from another method (the account changed method part-way
        # through, or the method is unknown) is not this tool's to fold together.
        methods = {r.pairing_mode for r in recs}
        if methods != {"first_last"}:
            if skipped is not None:
                skipped.append((emp, day, sorted(m or "unknown" for m in methods)))
            continue
        recs.sort(key=lambda r: r.check_in)
        keep, drop = recs[0], recs[1:]
        last = recs[-1]
        closed = [r.check_out for r in recs if r.check_out is not None]
        new_out = None if last.check_out is None else max(closed)
        plan.append({"emp": emp, "day": day, "keep": keep, "drop": drop,
                     "check_in": keep.check_in, "check_out": new_out})
    return plan


class Skipped(Exception):
    """A day left untouched because merging it would collide with another record."""


def _overlapping(odoo, item):
    keep, drop = item["keep"], item["drop"]
    ids = [keep.odoo_attendance_id] + [r.odoo_attendance_id for r in drop]
    domain: list = [("employee_id", "=", keep.odoo_employee_id), ("id", "not in", ids)]
    if item["check_out"] is not None:
        domain.append(("check_in", "<", fmt_dt(item["check_out"])))
    domain += ["|", ("check_out", ">", fmt_dt(item["check_in"])), ("check_out", "=", False)]
    return odoo.execute(
        "hr.attendance", "search_read", [domain], {"fields": ["id", "check_in", "check_out"]}
    )


def apply_plan(db, tenant, odoo, item) -> None:
    keep, drop = item["keep"], item["drop"]
    keep_id = keep.odoo_attendance_id
    drop_ids = [r.odoo_attendance_id for r in drop]

    # Odoo refuses overlapping records for one person, so the span can only be
    # stretched once the day's other records are gone — which also means a record
    # belonging to a *different* day that the stretch would run into has to be
    # ruled out first, before anything is deleted.
    # Odoo's own record of which method wrote each one, where it keeps one.
    ids = [keep_id] + drop_ids
    other = {
        m for m in odoo.attendance_pairing_methods(ids).values() if m and m != "first_last"
    } if hasattr(odoo, "attendance_pairing_methods") else set()
    if other:
        raise Skipped(
            "Odoo shows records written under another pairing method "
            f"({', '.join(sorted(other))}) — left alone"
        )

    clash = _overlapping(odoo, item)
    if clash:
        c = clash[0]
        raise Skipped(
            f"would overlap Odoo record {c['id']} ({c['check_in']} to {c.get('check_out') or 'open'}) "
            "from another day — left alone"
        )

    odoo.execute("hr.attendance", "unlink", [drop_ids])
    try:
        odoo.update_attendance(
            keep_id, item["check_in"], item["check_out"],
            reopen=item["check_out"] is None, pairing_mode="first_last",
        )
    except OdooError as exc:
        gone = "; ".join(
            f"{r.check_in:%Y-%m-%d %H:%M} to {r.check_out:%H:%M}" if r.check_out
            else f"{r.check_in:%Y-%m-%d %H:%M} open"
            for r in drop
        )
        raise OdooError(
            f"{exc} — the other records were already deleted ({gone} UTC); "
            f"record {keep_id} was NOT stretched"
        ) from exc

    tz = tenant.timezone or "UTC"
    keep.check_in = item["check_in"]
    keep.check_out = item["check_out"]
    keep.check_in_local = utc_to_local(keep.check_in, tz)
    keep.check_out_local = utc_to_local(keep.check_out, tz) if keep.check_out else None
    keep.worked_hours = (
        round((keep.check_out - keep.check_in).total_seconds() / 3600, 2) if keep.check_out else None
    )
    for punch in db.scalars(
        select(PunchRecord).where(
            PunchRecord.tenant_id == tenant.id, PunchRecord.odoo_attendance_id.in_(drop_ids)
        )
    ).all():
        punch.odoo_attendance_id = keep_id
    for r in drop:
        db.delete(r)
    db.commit()


def reopen_day(db, tenant, emp: str, day_text: str) -> int:
    """Put back an open shift that an earlier version of this tool closed.

    That version merged a day whose latest record was still open, but never
    cleared the kept record's check-out — so Odoo shows it closed while
    BioBridge's mirror says open. This clears it in Odoo to match.
    """
    day = datetime.strptime(day_text, "%Y-%m-%d").date()
    config = PairingConfig(
        mode=PairingMode.first_last,
        day_boundary_hour=tenant.day_boundary_hour,
        timezone=tenant.timezone or "UTC",
    )
    rec = next(
        (r for r in db.scalars(select(AttendanceRecord).where(
            AttendanceRecord.tenant_id == tenant.id,
            AttendanceRecord.emp_code == emp,
            AttendanceRecord.check_out.is_(None),
            AttendanceRecord.odoo_attendance_id.is_not(None),
        )).all() if day_of(r.check_in, config) == day),
        None,
    )
    if rec is None:
        print(f"  no open record for {emp} on {day} in BioBridge — nothing to reopen")
        return 1
    conn = db.scalars(select(OdooConnection).where(
        OdooConnection.tenant_id == tenant.id, OdooConnection.is_active.is_(True))).first()
    odoo = build_odoo_client(tenant, conn)
    odoo.authenticate()
    odoo.update_attendance(rec.odoo_attendance_id, reopen=True)
    print(f"  reopened Odoo record {rec.odoo_attendance_id} ({emp}, {day})")
    return 0


def run(db, tenant, since, apply: bool) -> int:
    print(f"\n=== {tenant.slug}  (pairing: {tenant.pairing_mode})")
    if tenant.pairing_mode != "first_last":
        print("  not on first-in/last-out pairing — this tool only applies to those accounts")
        return 0
    mixed: list = []
    plan = plan_for(db, tenant, since, mixed)
    for emp, day, methods in mixed:
        print(f"  {emp:>10}  {day}  left alone: records from more than one pairing method "
              f"({', '.join(methods)})")
    if not plan:
        print("  nothing to merge")
        return 0
    removed = sum(len(p["drop"]) for p in plan)
    for p in plan:
        out = p["check_out"].strftime("%H:%M") if p["check_out"] else "open"
        print(f"  {p['emp']:>10}  {p['day']}  {len(p['drop']) + 1} records -> 1  "
              f"({p['check_in']:%H:%M} UTC to {out} UTC)")
    print(f"  {len(plan)} day(s), {removed} record(s) to delete")
    if not apply:
        print("  dry run — nothing changed. Re-run with --apply.")
        return 0

    conn = db.scalars(
        select(OdooConnection).where(
            OdooConnection.tenant_id == tenant.id, OdooConnection.is_active.is_(True)
        )
    ).first()
    if conn is None:
        print("  no active Odoo connection — cannot apply")
        return 1
    try:
        odoo = build_odoo_client(tenant, conn)
        odoo.authenticate()
    except (OdooError, UnsafeTargetError) as exc:
        print(f"  could not reach Odoo: {exc}")
        return 1
    failed = skipped = 0
    for p in plan:
        try:
            apply_plan(db, tenant, odoo, p)
        except Skipped as exc:
            db.rollback()
            skipped += 1
            print(f"  SKIPPED {p['emp']} {p['day']}: {exc}")
        except OdooError as exc:
            db.rollback()
            failed += 1
            print(f"  FAILED {p['emp']} {p['day']}: {exc}")
    print(f"  merged {len(plan) - failed - skipped} day(s)"
          + (f", {skipped} skipped" if skipped else "") + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", help="account slug; omit for every account on first/last")
    ap.add_argument("--since", help="only days on or after YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="actually merge and delete")
    ap.add_argument("--reopen", nargs=2, metavar=("EMP_CODE", "YYYY-MM-DD"),
                    help="re-open that employee's open shift on that day, in Odoo "
                         "(repairs a day merged by the first version of this tool)")
    args = ap.parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d").date() if args.since else None

    status = 0
    with SessionLocal() as db:
        q = select(Tenant)
        if args.tenant:
            q = q.where(Tenant.slug == args.tenant)
        tenants = db.scalars(q).all()
        if not tenants:
            print("no matching account")
            return 1
        if args.reopen:
            if len(tenants) != 1:
                print("--reopen needs --tenant")
                return 1
            return reopen_day(db, tenants[0], *args.reopen)
        for t in tenants:
            status |= run(db, t, since, args.apply)
    return status


if __name__ == "__main__":
    sys.exit(main())
