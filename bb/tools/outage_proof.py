#!/usr/bin/env python3
"""Show what a customer sees when their BioTime server is unreachable.

Not a test — a demonstration. It builds a real tenant, points a real
``BioTimeProvider`` at a port nothing is listening on, and runs the real sync
engine, then prints the three places the failure surfaces: the run row, the
source's status, and the failure streak.

Run it before and after a change to the error path to see the difference the way
the customer does:

    python3 tools/outage_proof.py
"""

from __future__ import annotations

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.crypto import encrypt  # noqa: E402
from app.models import Base, DeviceSource, OdooConnection, Tenant  # noqa: E402
from app.services.sync_engine import SyncEngine  # noqa: E402


def dead_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main() -> int:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    tenant = Tenant(
        name="Demo Company", slug="demo-company-2-2", status="active",
        timezone="Asia/Kolkata", pairing_mode="alternating",
        min_punch_interval_seconds=60, max_shift_hours=16,
    )
    db.add(tenant)
    db.flush()

    port = dead_port()
    url = f"http://127.0.0.1:{port}"
    db.add(
        OdooConnection(
            tenant_id=tenant.id, name="Odoo", url="https://demo.odoo.com",
            db_name="demo", username="bot@demo.com",
            api_key_enc=encrypt("key", tenant.crypto_key), is_active=True,
        )
    )
    db.add(
        DeviceSource(
            tenant_id=tenant.id, name="Primary BioTime", base_url=url,
            username="admin", password_enc=encrypt("pw", tenant.crypto_key),
            server_timezone="Asia/Kolkata", is_active=True,
        )
    )
    db.commit()

    print(f"\nBioTime configured at {url} — nothing is listening there.\n")
    print("Running a real sync cycle...\n")

    run = SyncEngine(db, tenant, "manual").run_cycle()
    source = db.scalars(select(DeviceSource)).first()

    print("=" * 72)
    print("What the customer sees\n")
    print(f"  Run status            {run.status}")
    print(f"  Run message           {run.error_message}")
    print(f"\n  Connection status     {source.status}")
    print(f"  Connection message    {source.status_message}")
    print(f"\n  Failure streak        {tenant.consecutive_failures}")
    print("=" * 72)

    problems = []
    if "Unexpected error" in (run.error_message or ""):
        problems.append("the message still reads as a BioBridge bug")
    if "Errno" in (run.error_message or ""):
        problems.append("an errno leaked into customer-facing text")
    if tenant.consecutive_failures == 0:
        problems.append("the outage did not count towards the failure streak")

    if problems:
        print("\nFAIL")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("\nOK — named cause, no stack trace, streak counted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
