"""Shared fixtures: an in-memory database and stubs for both external systems."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.crypto import encrypt
from app.integrations.base import PunchEvent
from app.models import Base, Device, DeviceSource, OdooConnection, Tenant

TZ = "Asia/Dubai"  # UTC+4, no DST, so the offset arithmetic is checkable by eye


class FakeOdoo:
    """In-memory hr.attendance that honours Odoo's real constraints.

    Enforcing them here is the point: a stub that accepts anything would let the
    engine's constraint handling regress without a test noticing.
    """

    def __init__(self, employees: dict[str, tuple[int, str]] | None = None) -> None:
        self.uid = 7
        self.attendances: dict[int, dict] = {}
        self._next_id = 1000
        # `or` would treat an explicitly empty roster as "use the default",
        # which is exactly the case a test needs in order to model an Odoo that
        # does not know this badge yet.
        self.employees = (
            {"1001": (11, "Jane Doe"), "1002": (12, "Omar Haddad")}
            if employees is None
            else employees
        )
        self.calls: list[str] = []

    def authenticate(self) -> int:
        return self.uid

    def find_employee(self, emp_code):
        if emp_code in self.employees:
            emp_id, name = self.employees[emp_code]
            return emp_id, name, "barcode"
        return None, None, None

    def get_open_attendance(self, employee_id):
        for rec in sorted(
            self.attendances.values(), key=lambda r: r["check_in"], reverse=True
        ):
            if rec["employee_id"] == employee_id and rec["check_out"] is None:
                return {
                    "id": rec["id"],
                    "check_in": rec["check_in"].strftime("%Y-%m-%d %H:%M:%S"),
                }
        return None

    def attendance_exists(self, employee_id, check_in):
        for rec in self.attendances.values():
            if rec["employee_id"] == employee_id and rec["check_in"] == check_in:
                return rec["id"]
        return None

    def create_attendance(self, employee_id, check_in, check_out=None, biotime_ref=None):
        # Odoo allows at most one open record per employee.
        if check_out is None:
            for rec in self.attendances.values():
                if rec["employee_id"] == employee_id and rec["check_out"] is None:
                    raise AssertionError(
                        f"would open a second attendance for {employee_id} while "
                        f"{rec['id']} is still open — Odoo rejects this"
                    )
        self._next_id += 1
        self.attendances[self._next_id] = {
            "id": self._next_id,
            "employee_id": employee_id,
            "check_in": check_in,
            "check_out": check_out,
            "ref": biotime_ref,
        }
        self.calls.append(f"create:{self._next_id}")
        return self._next_id

    def close_attendance(self, attendance_id, check_out):
        self.attendances[attendance_id]["check_out"] = check_out
        self.calls.append(f"close:{attendance_id}")
        return True

    def create_employee(self, name, emp_code):  # pragma: no cover
        raise AssertionError("auto-create should be off in these tests")

    # Introspection used by the engine's companion-addon check.
    def fields_of(self, model):
        return {"check_in", "check_out", "employee_id"}


class FakeProvider:
    """Stands in at the provider seam, so the seam itself is exercised."""

    label = "Fake"
    cached_token = "tok"

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def fetch_punches(self, since=None, until=None):
        for row in self.rows:
            moment = datetime.strptime(row["punch_time"], "%Y-%m-%d %H:%M:%S")
            if since and moment < since:
                continue
            if until and moment > until:
                continue
            yield PunchEvent(
                external_id=str(row["id"]),
                emp_code=row["emp_code"],
                punch_time_local=moment,
                direction=row.get("direction"),
                terminal_sn=row.get("terminal_sn", "GATE-01"),
                raw=row,
            )

    def close(self):
        pass


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


@pytest.fixture
def tenant(db):
    row = Tenant(
        name="Gulf Steel",
        slug="gulf-steel",
        status="active",
        timezone=TZ,
        pairing_mode="alternating",
        min_punch_interval_seconds=60,
        max_shift_hours=16,
        work_start_time="08:30",
        late_grace_minutes=15,
    )
    db.add(row)
    db.flush()

    db.add(
        OdooConnection(
            tenant_id=row.id,
            name="Odoo",
            url="https://acme.odoo.com",
            db_name="acme",
            username="bot@acme.com",
            api_key_enc=encrypt("key", row.crypto_key),
            is_active=True,
        )
    )
    source = DeviceSource(
        tenant_id=row.id,
        name="BioTime",
        base_url="https://bio.test",
        username="a",
        password_enc=encrypt("p", row.crypto_key),
        server_timezone=TZ,
        is_active=True,
    )
    db.add(source)
    db.flush()
    db.add(
        Device(
            tenant_id=row.id,
            source_id=source.id,
            serial_number="GATE-01",
            is_enabled=True,
        )
    )
    db.commit()
    return row


@pytest.fixture
def local_day():
    """A fixed local day, recent enough to sit inside the backfill window."""
    base = datetime.utcnow() - timedelta(days=1)
    return base.replace(hour=0, minute=0, second=0, microsecond=0)
