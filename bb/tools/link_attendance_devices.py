#!/usr/bin/env python3
"""Link each Odoo attendance record BioBridge created to the device it was punched on.

New attendance records get their device as they're pushed, once device
tracking is on. Records pushed before that — or from punches fetched before
their terminal was imported into BioBridge — went to Odoo with no device.
This finds them and fills the device in over XML-RPC, taking the terminal
from each record's check-in punch.

    python3 tools/link_attendance_devices.py                 # report only
    python3 tools/link_attendance_devices.py --tenant acme   # one account
    python3 tools/link_attendance_devices.py --apply         # write to Odoo

Only records BioBridge itself created are touched, and only where Odoo's
record has no device yet — a device someone set by hand is never
overwritten. Safe to run again: a second run finds nothing left to do.

Reads BioBridge's database directly, like check_source.py, since it needs
each account's stored Odoo credentials. Run it from the repo root.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.integrations.odoo import OdooError  # noqa: E402
from app.models import OdooConnection, Tenant  # noqa: E402
from app.services.connections import UnsafeTargetError, build_odoo_client  # noqa: E402
from app.services.device_links import link_attendance_devices  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tenant", help="Only this account (its slug).")
    parser.add_argument("--apply", action="store_true", help="Write the devices to Odoo.")
    args = parser.parse_args()

    db = SessionLocal()
    problems = 0
    try:
        query = select(Tenant).order_by(Tenant.slug)
        if args.tenant:
            query = query.where(Tenant.slug == args.tenant)
        tenants = db.scalars(query).all()
        if not tenants:
            print(f"No account with slug {args.tenant!r}." if args.tenant else "No accounts.")
            return 1

        for tenant in tenants:
            conn = db.scalars(
                select(OdooConnection).where(
                    OdooConnection.tenant_id == tenant.id,
                    OdooConnection.is_active.is_(True),
                )
            ).first()
            print(f"\n{tenant.slug}")
            if conn is None:
                print("  no active Odoo connection — skipped")
                continue
            if not conn.has_device_tracking:
                print("  device tracking is off for this Odoo connection — enable it "
                      "(Settings → Odoo → Enable device tracking), then run this again")
                continue

            try:
                odoo = build_odoo_client(tenant, conn)
                odoo.authenticate()
                report = link_attendance_devices(db, tenant, odoo, apply=args.apply)
            except (OdooError, UnsafeTargetError) as exc:
                db.rollback()
                print(f"  could not reach Odoo: {exc}")
                problems += 1
                continue

            print(f"  {report.attendances} attendance record(s) created by BioBridge")
            print(f"  {report.already_linked} already have a device (or are no longer in Odoo)")
            if report.no_terminal:
                print(f"  {report.no_terminal} can't be traced to a terminal (their punches "
                      "carry no serial number, or it matches no imported terminal) — "
                      "import terminals, then run this again")
            for serial, ids in sorted(report.missing.items()):
                print(f"  {'linked' if args.apply else 'to link'}: {len(ids)} → {serial}")
            for failure in report.failures:
                print(f"  FAILED {failure}")
                problems += 1

            if args.apply:
                db.commit()  # keep the punch→terminal links worked out along the way
                print(f"  done: {report.linked} record(s) now point at their device")
            else:
                db.rollback()  # report only: write nothing, here either
                if report.to_link:
                    print(f"  report only — run with --apply to link these {report.to_link}")
                elif report.attendances:
                    print("  nothing to do")
    finally:
        db.close()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
