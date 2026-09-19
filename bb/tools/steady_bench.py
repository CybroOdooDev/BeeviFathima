#!/usr/bin/env python3
"""BioBridge's OWN cost in steady state — not Odoo's, not BioTime's.

The fleet runs 500 tenants against 500 separate Odoo instances, so Odoo load is
not shared and is not the constraint: each instance sees a handful of writes per
minute. What IS shared is this process and its database. So this measures only
what BioBridge spends: CPU, resident memory and its own SQL, for one short
polling run of the kind a 1-minute interval actually produces.

The distinction that matters: a run's cost splits into work that scales with the
punches in front of it, and work that scales with how much history the tenant has
accumulated. The first is unavoidable and small. The second is the one that
decides whether 500 tenants on a 1-minute tick fits on one box or fifty.

    python3 tools/steady_bench.py
    python3 tools/steady_bench.py --batch 67 --tenants 500 --interval 60
"""

from __future__ import annotations

import argparse
import os
import resource
import sys
import time
import tracemalloc
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, event, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.crypto import encrypt  # noqa: E402
from app.integrations.base import PunchEvent  # noqa: E402
from app.models import (  # noqa: E402
    Base, DeviceSource, EmployeeMapping, OdooConnection, PunchRecord, Tenant,
)
from app.services import sync_engine as engine_mod  # noqa: E402

TZ = "Asia/Kolkata"
LEDGER_SIZES = [50_000, 250_000, 1_000_000, 5_000_000]


class QuietOdoo:
    """Each tenant has its own Odoo, and it is not the bottleneck.

    Returns instantly on purpose: this bench measures BioBridge, so any time
    spent here would be noise. Round trips are counted only to confirm the
    per-run Odoo load really is small in steady state.
    """

    def __init__(self, roster):
        self.uid = 7
        self.roster = roster
        self.attendances = {}
        self._next = 1000
        self.calls = 0

    def authenticate(self):
        self.calls += 1
        return self.uid

    def find_employee(self, emp_code):
        self.calls += 1
        hit = self.roster.get(emp_code)
        return (hit[0], hit[1], "barcode") if hit else (None, None, None)

    def get_open_attendance(self, employee_id):
        self.calls += 1
        for rec in sorted(self.attendances.values(),
                          key=lambda r: r["check_in"], reverse=True):
            if rec["employee_id"] == employee_id and rec["check_out"] is None:
                return {"id": rec["id"],
                        "check_in": rec["check_in"].strftime("%Y-%m-%d %H:%M:%S")}
        return None

    def attendance_exists(self, employee_id, check_in):
        self.calls += 1
        return None

    def create_attendance(self, employee_id, check_in, check_out=None, biotime_ref=None):
        self.calls += 1
        self._next += 1
        self.attendances[self._next] = {"id": self._next, "employee_id": employee_id,
                                        "check_in": check_in, "check_out": check_out}
        return self._next

    def close_attendance(self, attendance_id, check_out):
        self.calls += 1
        self.attendances[attendance_id]["check_out"] = check_out
        return True

    def create_employee(self, name, emp_code):
        raise AssertionError("off")

    def fields_of(self, model):
        return {"check_in", "check_out", "employee_id"}


class BatchProvider:
    label = "Bench"
    cached_token = "tok"

    def __init__(self, events):
        self.events = events

    def fetch_punches(self, since=None, until=None):
        yield from self.events

    def close(self):
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=67,
                        help="New punches per run (40k/day over 10h ~= 67/min)")
    parser.add_argument("--tenants", type=int, default=500)
    parser.add_argument("--interval", type=int, default=60, help="Seconds between runs")
    parser.add_argument("--employees", type=int, default=5000)
    args = parser.parse_args()

    runs_per_day = args.tenants * 86_400 / args.interval

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)

    statements = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        statements["n"] += 1

    db = sessionmaker(bind=engine, autoflush=False)()

    tenant = Tenant(name="Bench", slug="bench", status="active", timezone=TZ,
                    pairing_mode="alternating", min_punch_interval_seconds=60,
                    max_shift_hours=16)
    db.add(tenant)
    db.flush()
    db.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://x.odoo.com",
                          db_name="x", username="bot",
                          api_key_enc=encrypt("k", tenant.crypto_key), is_active=True))
    source = DeviceSource(tenant_id=tenant.id, name="BioTime", base_url="https://b.test",
                          username="a", password_enc=encrypt("p", tenant.crypto_key),
                          server_timezone=TZ, is_active=True)
    db.add(source)
    db.flush()

    roster = {f"{1000 + i}": (i + 1, f"Emp {i}") for i in range(args.employees)}
    # Pre-map everyone: in steady state the mapping work is already done, and
    # leaving it out would make the fixed cost look larger than it is.
    for code, (emp_id, name) in roster.items():
        db.add(EmployeeMapping(tenant_id=tenant.id, emp_code=code,
                               odoo_employee_id=emp_id, odoo_employee_name=name,
                               status="mapped", match_method="barcode"))
    db.commit()

    odoo = QuietOdoo(roster)
    engine_mod.build_odoo_client = lambda t, c: odoo

    base = datetime.utcnow() - timedelta(days=500)
    print()
    print(f"ONE POLLING RUN — {args.batch} new punches, {args.employees:,}-employee tenant")
    print(f"Fleet: {args.tenants} tenants every {args.interval}s "
          f"= {runs_per_day:,.0f} runs/day = {runs_per_day / 86_400:.1f} runs/second")
    print("=" * 88)
    print(f"{'ledger rows':>13} {'CPU s/run':>11} {'peak MB':>9} {'SQL':>6} "
          f"{'cores (fleet)':>15} {'RAM/run':>10}")
    print("-" * 88)

    written = 0
    serial = 0
    for target in LEDGER_SIZES:
        rows = []
        for i in range(written, target):
            rows.append({
                "tenant_id": tenant.id, "source_id": source.id,
                "external_id": f"bt-{i}", "emp_code": f"{1000 + i % args.employees}",
                "punch_time_utc": base + timedelta(seconds=i),
                "punch_time_local": base + timedelta(seconds=i),
                "direction": "in", "process_state": "synced", "attempts": 0,
            })
            if len(rows) >= 100_000:
                db.bulk_insert_mappings(PunchRecord, rows)
                db.commit()
                rows = []
        if rows:
            db.bulk_insert_mappings(PunchRecord, rows)
            db.commit()
        written = target

        now = datetime.utcnow()
        events = []
        for k in range(args.batch):
            serial += 1
            events.append(PunchEvent(
                external_id=f"live-{serial}", emp_code=f"{1000 + k % args.employees}",
                punch_time_local=now - timedelta(seconds=30 - (k % 20)),
                direction=(k % 2 == 0), terminal_sn="GATE-01", raw={},
            ))
        engine_mod.build_source_provider = lambda t, s: BatchProvider(events)

        # Two passes on purpose. tracemalloc roughly doubles CPU time, so
        # timing a traced run reports the profiler as if it were the workload —
        # it made the first version of this bench overstate CPU by ~40%.
        statements["n"] = 0
        cpu_before = resource.getrusage(resource.RUSAGE_SELF)
        engine_mod.SyncEngine(db, tenant, "bench").run_cycle()
        cpu_after = resource.getrusage(resource.RUSAGE_SELF)
        cpu = ((cpu_after.ru_utime - cpu_before.ru_utime)
               + (cpu_after.ru_stime - cpu_before.ru_stime))

        engine_mod.build_source_provider = lambda t, s: BatchProvider([
            PunchEvent(external_id=f"m-{serial}-{k}", emp_code=e.emp_code,
                       punch_time_local=e.punch_time_local, direction=e.direction,
                       terminal_sn=e.terminal_sn, raw={})
            for k, e in enumerate(events)
        ])
        tracemalloc.start()
        engine_mod.SyncEngine(db, tenant, "bench").run_cycle()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak_mb = peak / 1024 / 1024
        cores = runs_per_day * cpu / 86_400

        print(f"{target:>13,} {cpu:>11.3f} {peak_mb:>9.0f} {statements['n']:>6} "
              f"{cores:>12.1f} cores {peak_mb:>8.0f} MB")

    print("-" * 88)
    total_rows = args.tenants * args.employees * 8
    print()
    print(f"BioBridge's OWN ledger grows by {total_rows:,} rows/day "
          f"({total_rows * 365 / 1e9:.1f} billion/year).")
    print("Every run's cost above is paid against that, on every tick, per tenant.")
    print()
    print(f"Per-punch CPU is the floor: {args.tenants * args.employees * 8:,} punches/day")
    print("must be parsed, paired and written whatever the interval is.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
