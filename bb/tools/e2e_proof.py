#!/usr/bin/env python3
"""End-to-end proof: mock BioTime -> BioBridge -> a real Odoo 19.

No stubs on either side. This drives the shipped engine against a live Odoo over
XML-RPC and a BioTime look-alike over HTTP, across three cycles, and asserts the
outcome that a naive implementation gets wrong.

    python3 tools/e2e_proof.py --odoo-url http://127.0.0.1:8069 \
        --odoo-db mydb --odoo-user admin --odoo-key admin
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.crypto import encrypt  # noqa: E402
from app.integrations.odoo import OdooClient, OdooCredentials  # noqa: E402
from app.models import (  # noqa: E402
    AttendanceRecord,
    Base,
    Device,
    DeviceSource,
    EmployeeMapping,
    OdooConnection,
    PunchRecord,
    Tenant,
)
from app.services.sync_engine import SyncEngine  # noqa: E402
from app.services.timeutils import utc_to_local, utcnow_naive  # noqa: E402

TZ = "Asia/Dubai"  # UTC+4, no DST, so the offset is checkable by eye
MOCK = "http://127.0.0.1:8099"
PUNCH_FILE = "/tmp/bb_punches.json"

RESULTS: list[tuple[str, bool]] = []


def check(label, condition, detail=""):
    RESULTS.append((label, bool(condition)))
    mark = "PASS" if condition else "FAIL"
    print(f"  {mark}  {label}" + (f"  -- {detail}" if detail else ""))


def set_punches(rows):
    with open(PUNCH_FILE, "w") as handle:
        json.dump(rows, handle)


def punch(pid, emp_code, local_dt, state="0"):
    return {
        "id": pid,
        "emp_code": emp_code,
        "punch_time": local_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "punch_state": state,
        "verify_type": "1",
        "terminal_sn": "MOCK-GATE-01",
        "first_name": "Ahmed",
        "last_name": "Sharma",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--odoo-url", required=True)
    parser.add_argument("--odoo-db", required=True)
    parser.add_argument("--odoo-user", required=True)
    parser.add_argument("--odoo-key", required=True)
    args = parser.parse_args()

    # --- a real employee in the real Odoo, carrying the badge ----------------
    odoo = OdooClient(
        OdooCredentials(
            url=args.odoo_url, db=args.odoo_db, username=args.odoo_user, api_key=args.odoo_key
        )
    )
    info = odoo.ping()
    print(f"\nOdoo {info['server_version']}, uid={info['uid']}, "
          f"can_create_attendance={info['can_create_attendance']}")

    found = odoo.execute(
        "hr.employee", "search_read", [[("barcode", "=", "1001")]], {"fields": ["id"], "limit": 1}
    )
    employee_id = found[0]["id"] if found else odoo.create_employee("Ahmed Sharma", "1001")
    print(f"Odoo employee id={employee_id} carries badge 1001")

    # Clear any attendance left by an earlier run, so the proof starts clean.
    old = odoo.execute(
        "hr.attendance", "search", [[("employee_id", "=", employee_id)]]
    )
    if old:
        odoo.execute("hr.attendance", "unlink", [old])
        print(f"cleared {len(old)} pre-existing attendance record(s)")

    # --- BioBridge, on its own in-memory database ---------------------------
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    tenant = Tenant(name="Proof Co", slug="proof", status="active", timezone=TZ,
                    pairing_mode="alternating", max_shift_hours=16, work_start_time="08:30")
    db.add(tenant)
    db.flush()

    db.add(OdooConnection(
        tenant_id=tenant.id, name="Odoo", url=args.odoo_url, db_name=args.odoo_db,
        username=args.odoo_user, api_key_enc=encrypt(args.odoo_key, tenant.crypto_key),
        is_active=True,
    ))
    source = DeviceSource(
        tenant_id=tenant.id, name="BioTime", provider="biotime", base_url=MOCK,
        username="mock", password_enc=encrypt("mock", tenant.crypto_key),
        server_timezone=TZ, is_active=True,
    )
    db.add(source)
    db.flush()
    db.add(Device(tenant_id=tenant.id, source_id=source.id,
                  serial_number="MOCK-GATE-01", is_enabled=True))
    db.commit()

    # Uses the app's own conversion helper rather than a second implementation,
    # so the proof cannot pass because the test agrees with itself.
    now_utc = utcnow_naive().replace(microsecond=0)
    now_local = utc_to_local(now_utc, TZ)
    check_in_local = (now_local - timedelta(hours=3)).replace(second=0, microsecond=0)
    check_out_local = (now_local - timedelta(hours=1)).replace(second=0, microsecond=0)
    expected_in = check_in_local - timedelta(hours=4)
    expected_out = check_out_local - timedelta(hours=4)

    def attendances():
        return odoo.execute(
            "hr.attendance", "search_read", [[("employee_id", "=", employee_id)]],
            {"fields": ["id", "check_in", "check_out", "worked_hours"], "order": "check_in"},
        )

    # --- cycle 1: only the check-in exists yet ------------------------------
    print("\n--- cycle 1: the employee has checked in, nothing more ---")
    set_punches([punch(5001, "1001", check_in_local)])
    run1 = SyncEngine(db, tenant, "proof").run_cycle()
    check("run succeeded", run1.status == "success", run1.error_message or "")
    check("badge matched to the Odoo employee", run1.employees_matched == 1)

    rows = attendances()
    check("exactly one attendance in Odoo", len(rows) == 1, f"got {len(rows)}")
    check("check-in stored as UTC, not wall-clock",
          rows and rows[0]["check_in"] == expected_in.strftime("%Y-%m-%d %H:%M:%S"),
          rows[0]["check_in"] if rows else "-")
    check("left open awaiting the check-out", rows and rows[0]["check_out"] is False)

    # --- cycle 2: the check-out arrives -------------------------------------
    print("\n--- cycle 2: the check-out arrives in a later poll ---")
    set_punches([punch(5001, "1001", check_in_local), punch(5002, "1001", check_out_local, "1")])
    run2 = SyncEngine(db, tenant, "proof").run_cycle()
    check("run succeeded", run2.status == "success", run2.error_message or "")

    rows = attendances()
    check("STILL exactly one attendance — no phantom record", len(rows) == 1, f"got {len(rows)}")
    check("the original record was closed",
          rows and rows[0]["check_out"] == expected_out.strftime("%Y-%m-%d %H:%M:%S"),
          rows[0]["check_out"] if rows else "-")
    check("Odoo computed 2 worked hours",
          rows and round(rows[0]["worked_hours"], 2) == 2.0,
          f"{rows[0]['worked_hours']:.2f}" if rows else "-")
    check("nothing is left open",
          not [r for r in rows if r["check_out"] is False])

    # --- cycle 3: nothing new ----------------------------------------------
    print("\n--- cycle 3: nothing new (idempotency) ---")
    before_punches = db.scalar(select(PunchRecord).exists().select()) and len(
        db.scalars(select(PunchRecord)).all())
    run3 = SyncEngine(db, tenant, "proof").run_cycle()
    check("run succeeded", run3.status == "success", run3.error_message or "")
    check("no duplicate punches ingested", run3.punches_new == 0)
    check("no duplicate attendance created", len(attendances()) == 1)
    check("ledger unchanged", len(db.scalars(select(PunchRecord)).all()) == before_punches)

    # --- local mirror -------------------------------------------------------
    print("\n--- the local mirror agrees with Odoo ---")
    mirror = db.scalars(select(AttendanceRecord)).all()
    check("one mirrored interval", len(mirror) == 1, f"got {len(mirror)}")
    check("mirror points at the Odoo record",
          mirror and mirror[0].odoo_attendance_id == rows[0]["id"])
    check("mirror worked hours match", mirror and abs(mirror[0].worked_hours - 2.0) < 0.01)
    check("mirror keeps the local wall-clock for reports",
          mirror and mirror[0].check_in_local == check_in_local,
          str(mirror[0].check_in_local) if mirror else "-")

    mapping = db.scalars(select(EmployeeMapping)).first()
    check("open shift cleared once the record closed",
          mapping is not None and mapping.open_attendance_id is None)

    print("\n" + "=" * 64)
    failed = [label for label, ok in RESULTS if not ok]
    print(f"{len(RESULTS)} checks, {len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for label in failed:
        print(f"  FAILED: {label}")
    print("=" * 64)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
