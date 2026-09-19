#!/usr/bin/env python3
"""Measure what one tenant's sync actually costs, so capacity is arithmetic.

Runs the real ``SyncEngine`` against an in-memory Odoo and provider that count
every round trip. Nothing is estimated: the RPC counts and row counts come from
the code as it is, and the only thing added afterwards is the network latency a
real Odoo and a real BioTime would impose per call.

    python3 tools/capacity_bench.py                       # default shape
    python3 tools/capacity_bench.py --employees 5000 --punches-per-day 8
    python3 tools/capacity_bench.py --history-days 30     # ledger growth effect

The output is per-tenant. Multiply by the fleet, and divide by how many tenants
run at once (settings.scheduler_concurrency).
"""

from __future__ import annotations

import argparse
import os
import resource
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.crypto import encrypt  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    DeviceSource,
    OdooConnection,
    PunchRecord,
    Tenant,
)
from app.services import sync_engine as engine_mod  # noqa: E402
from app.integrations.base import PunchEvent  # noqa: E402

TZ = "Asia/Kolkata"


class CountingOdoo:
    """Odoo with every round trip counted, and none of them free."""

    def __init__(self, employees: dict[str, tuple[int, str]]) -> None:
        self.uid = 7
        self.employees = employees
        self.attendances: dict[int, dict] = {}
        self._next = 1000
        self.rpc = {
            "authenticate": 0, "find_employee": 0, "get_open_attendance": 0,
            "attendance_exists": 0, "create_attendance": 0, "close_attendance": 0,
        }

    def _hit(self, name: str) -> None:
        self.rpc[name] += 1

    def authenticate(self):
        self._hit("authenticate")
        return self.uid

    def find_employee(self, emp_code):
        self._hit("find_employee")
        if emp_code in self.employees:
            emp_id, name = self.employees[emp_code]
            return emp_id, name, "barcode"
        return None, None, None

    def get_open_attendance(self, employee_id):
        self._hit("get_open_attendance")
        for rec in sorted(self.attendances.values(),
                          key=lambda r: r["check_in"], reverse=True):
            if rec["employee_id"] == employee_id and rec["check_out"] is None:
                return {"id": rec["id"],
                        "check_in": rec["check_in"].strftime("%Y-%m-%d %H:%M:%S")}
        return None

    def attendance_exists(self, employee_id, check_in):
        self._hit("attendance_exists")
        for rec in self.attendances.values():
            if rec["employee_id"] == employee_id and rec["check_in"] == check_in:
                return rec["id"]
        return None

    def create_attendance(self, employee_id, check_in, check_out=None, biotime_ref=None):
        self._hit("create_attendance")
        self._next += 1
        self.attendances[self._next] = {
            "id": self._next, "employee_id": employee_id, "check_in": check_in,
            "check_out": check_out, "ref": biotime_ref,
        }
        return self._next

    def close_attendance(self, attendance_id, check_out):
        self._hit("close_attendance")
        self.attendances[attendance_id]["check_out"] = check_out
        return True

    def create_employee(self, name, emp_code):
        raise AssertionError("auto-create off")

    def fields_of(self, model):
        return {"check_in", "check_out", "employee_id"}

    @property
    def total(self) -> int:
        return sum(self.rpc.values())


class CountingProvider:
    """A BioTime that counts pages, so HTTP round trips are visible too."""

    label = "Bench"
    cached_token = "tok"

    def __init__(self, events: list[PunchEvent]) -> None:
        self.events = events
        self.pages = 0

    def fetch_punches(self, since=None, until=None):
        size = settings.default_page_size
        for index, event in enumerate(self.events):
            if index % size == 0:
                self.pages += 1
            yield event

    def close(self):
        pass


def build_events(employees: int, per_day: int, day: datetime) -> list[PunchEvent]:
    """A realistic day: each person in and out, `per_day` punches, staggered."""
    events: list[PunchEvent] = []
    pair_count = per_day // 2
    for emp in range(employees):
        code = f"{1000 + emp}"
        for pair in range(pair_count):
            check_in = day.replace(hour=8) + timedelta(hours=pair * 2, seconds=emp % 60)
            check_out = check_in + timedelta(hours=1, minutes=30)
            for moment, direction in ((check_in, True), (check_out, False)):
                events.append(
                    PunchEvent(
                        external_id=f"{emp}-{pair}-{int(direction)}",
                        emp_code=code,
                        punch_time_local=moment,
                        direction=direction,
                        terminal_sn="GATE-01",
                        first_name="E", last_name=code,
                        raw={},
                    )
                )
    return events


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--employees", type=int, default=500)
    parser.add_argument("--punches-per-day", type=int, default=8)
    parser.add_argument("--history-days", type=int, default=0,
                        help="Pre-fill the ledger with this many days of past punches")
    parser.add_argument("--odoo-rtt-ms", type=float, default=40.0,
                        help="Round-trip time to Odoo per XML-RPC call")
    parser.add_argument("--biotime-rtt-ms", type=float, default=150.0,
                        help="Round-trip time to BioTime per page")
    args = parser.parse_args()

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
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
    db.commit()

    roster = {f"{1000 + i}": (i + 1, f"Emp {i}") for i in range(args.employees)}
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) \
        - timedelta(days=1)

    # Optional history, to show what the ledger does to the run as it grows.
    if args.history_days:
        print(f"seeding {args.history_days} days of history...", flush=True)
        for back in range(1, args.history_days + 1):
            past = today - timedelta(days=back)
            rows = [
                {
                    "tenant_id": tenant.id, "source_id": source.id,
                    "external_id": f"h{back}-{e.external_id}", "emp_code": e.emp_code,
                    "punch_time_utc": e.punch_time_local - timedelta(days=back),
                    "punch_time_local": e.punch_time_local - timedelta(days=back),
                    "direction": "in" if e.direction else "out",
                    "process_state": "synced", "attempts": 0,
                }
                for e in build_events(args.employees, args.punches_per_day, past)
            ]
            db.bulk_insert_mappings(PunchRecord, rows)
        db.commit()
        seeded = db.scalar(select(func.count()).select_from(PunchRecord))
        print(f"  ledger now holds {seeded:,} rows\n", flush=True)

    events = build_events(args.employees, args.punches_per_day, today)
    odoo = CountingOdoo(roster)
    provider = CountingProvider(events)

    import app.services.sync_engine as se
    se.build_odoo_client = lambda t, c: odoo
    se.build_source_provider = lambda t, s: provider

    before = rss_mb()
    started = time.monotonic()
    run = engine_mod.SyncEngine(db, tenant, "bench").run_cycle()
    elapsed = time.monotonic() - started
    peak = rss_mb()

    stored = db.scalar(select(func.count()).select_from(PunchRecord))

    odoo_seconds = odoo.total * args.odoo_rtt_ms / 1000
    biotime_seconds = provider.pages * args.biotime_rtt_ms / 1000

    print("=" * 74)
    print(f"ONE TENANT — {args.employees:,} employees x {args.punches_per_day} "
          f"punches = {len(events):,} punches/day")
    print("=" * 74)
    print(f"  run status              {run.status}")
    print(f"  punches fetched / new   {run.punches_fetched:,} / {run.punches_new:,}")
    print(f"  ledger rows after       {stored:,}")
    print(f"  attendances in Odoo     {len(odoo.attendances):,}")
    print()
    print(f"  CPU/DB time (no network)  {elapsed:8.1f} s")
    print(f"  peak RSS                  {peak:8.0f} MB  (+{peak - before:.0f} MB)")
    print()
    print("  Odoo XML-RPC round trips:")
    for name, count in sorted(odoo.rpc.items(), key=lambda kv: -kv[1]):
        if count:
            print(f"    {name:22} {count:8,}")
    print(f"    {'TOTAL':22} {odoo.total:8,}   = {odoo_seconds:7.1f} s "
          f"at {args.odoo_rtt_ms:.0f} ms each")
    print()
    print(f"  BioTime pages             {provider.pages:8,}   = {biotime_seconds:7.1f} s "
          f"at {args.biotime_rtt_ms:.0f} ms each")
    print()
    wall = elapsed + odoo_seconds + biotime_seconds
    print(f"  WALL CLOCK PER TENANT     {wall:8.1f} s  ({wall / 60:.1f} min)")
    print()

    cap = settings.default_page_size * settings.max_pages_per_run
    if len(events) > cap:
        print(f"  !! page cap: max_pages_per_run({settings.max_pages_per_run}) x "
              f"default_page_size({settings.default_page_size}) = {cap:,} punches,")
        print(f"     but this tenant produces {len(events):,}/day. The rest waits "
              f"for the next run.")
        print()

    for fleet in (100, 500):
        for lanes in (settings.scheduler_concurrency, 32, 128):
            total = fleet * wall / lanes
            print(f"  {fleet:4} tenants at concurrency {lanes:4}: "
                  f"{total / 60:8.1f} min to drain")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
