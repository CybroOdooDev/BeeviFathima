#!/usr/bin/env python3
"""What one sync run costs *before* it has done any work.

A short interval does not mean big runs — a 1-minute run fetches one minute of
punches. So the question "can every tenant poll every minute" is not about daily
volume at all. It is about the fixed cost of a run, multiplied by the number of
runs the fleet makes per day.

    500 tenants x 1440 runs/day = 720,000 runs/day = 8.3 runs/second, forever.

At that rate anything a run does unconditionally matters. This measures the
biggest one: ``SyncEngine._known_external_ids``, which loads every external id
the tenant has ever stored into a Python set on every single run, so its cost
grows with the age of the account rather than with the work in front of it.

    python3 tools/tick_bench.py
"""

from __future__ import annotations

import os
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta  # noqa: E402

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.crypto import encrypt  # noqa: E402
from app.models import Base, DeviceSource, PunchRecord, Tenant  # noqa: E402
from app.services.sync_engine import SyncEngine  # noqa: E402

LEDGER_SIZES = [10_000, 100_000, 500_000, 1_000_000]
TZ = "Asia/Kolkata"


def main() -> int:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False)()

    tenant = Tenant(name="Bench", slug="bench", status="active", timezone=TZ,
                    pairing_mode="alternating", min_punch_interval_seconds=60,
                    max_shift_hours=16)
    db.add(tenant)
    db.flush()
    source = DeviceSource(tenant_id=tenant.id, name="BioTime", base_url="https://b.test",
                          username="a", password_enc=encrypt("p", tenant.crypto_key),
                          server_timezone=TZ, is_active=True)
    db.add(source)
    db.commit()

    engine_obj = SyncEngine(db, tenant, "bench")
    base = datetime.utcnow() - timedelta(days=400)

    print()
    print("Cost of ONE run's _known_external_ids, by how much history the tenant has")
    print("=" * 78)
    print(f"{'ledger rows':>14} {'seconds':>10} {'set MB':>10} "
          f"{'runs/s (1 core)':>17} {'fleet CPU needed':>18}")
    print("-" * 78)

    written = 0
    for target in LEDGER_SIZES:
        rows = []
        for i in range(written, target):
            rows.append({
                "tenant_id": tenant.id, "source_id": source.id,
                "external_id": f"bt-{i}", "emp_code": f"{1000 + i % 5000}",
                "punch_time_utc": base + timedelta(seconds=i),
                "punch_time_local": base + timedelta(seconds=i),
                "direction": "in", "process_state": "synced", "attempts": 0,
            })
            if len(rows) >= 50_000:
                db.bulk_insert_mappings(PunchRecord, rows)
                rows = []
        if rows:
            db.bulk_insert_mappings(PunchRecord, rows)
        db.commit()
        written = target

        # Warm, then measure — we want the steady-state cost, not a cold cache.
        engine_obj._known_external_ids(source)  # noqa: SLF001
        tracemalloc.start()
        started = time.monotonic()
        ids = engine_obj._known_external_ids(source)  # noqa: SLF001
        elapsed = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        del ids

        per_second = 1 / elapsed if elapsed else float("inf")
        # 500 tenants polling every minute = 8.33 runs/second across the fleet.
        cores = 8.333 * elapsed
        print(f"{target:>14,} {elapsed:>10.3f} {peak / 1024 / 1024:>10.0f} "
              f"{per_second:>17.1f} {cores:>15.1f} cores")

    print("-" * 78)
    print()
    print("The last column is how many CPU cores 500 tenants polling every minute")
    print("would need for this ONE query alone — before fetching, parsing, pairing")
    print("or talking to Odoo.")
    print()
    print("40,000 punches/day reaches 1.2M rows in a month and 14.6M in a year.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
