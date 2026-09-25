"""Shared fixtures: an in-memory database and stubs for both external systems."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.crypto import encrypt
from app.integrations.base import Capability, EmployeeRecord, PunchEvent
from app.models import Base, Device, DeviceSource, OdooConnection, Tenant


def pytest_configure(config):
    """Refuse to run a suite that would quietly skip the async tests.

    Without pytest-asyncio, pytest *skips* every async test and still exits 0.
    The whole of test_scheduler_loop.py is async, so the result is a green run
    with the scheduling loop — the thing that makes any sync happen at all —
    completely untested, reported as success. A missing dev dependency must not
    be able to hide a broken scheduler, so this is a hard failure with the fix
    in the message.
    """
    if importlib.util.find_spec("pytest_asyncio") is None:
        raise pytest.UsageError(
            "pytest-asyncio is not installed, so the async scheduler tests would "
            "be SKIPPED and this run would still report success.\n"
            "    pip install -r requirements-dev.txt"
        )

TZ = "Asia/Dubai"  # UTC+4, no DST, so the offset arithmetic is checkable by eye


@pytest.fixture(autouse=True)
def _no_network_email_checks(monkeypatch):
    """The MX/A lookup in app.services.email_check needs outbound DNS, which
    this suite must not depend on. Every test gets syntax-only checking;
    tests/test_email_genuineness.py exercises the deliverability path itself
    against a mocked resolver rather than real DNS.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "verify_email_deliverability", False)


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
        self.devices: dict[str, int] = {}
        self._next_device_id = 500
        #: The Odoo-side roster, as list_employees() would return it — a
        #: test sets this directly to drive _provision_employees. Empty by
        #: default so every test not about that feature sees no roster and
        #: it stays a no-op.
        self.roster: list[dict] = []

    def authenticate(self) -> int:
        return self.uid

    def find_employee(self, emp_code):
        if emp_code in self.employees:
            emp_id, name = self.employees[emp_code]
            return emp_id, name, "barcode"
        return None, None, None

    def list_employees(self, limit=0):
        return self.roster

    def employee_code_for(self, employee_row):
        # Mirrors OdooClient.employee_code_for's MATCH_FIELDS priority
        # order, reimplemented independently rather than imported — the
        # point of a fake is to not share a bug with the thing it stands in
        # for.
        for field in ("barcode", "pin", "registration_number", "work_email"):
            value = employee_row.get(field)
            if value:
                return str(value).strip()
        return None

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

    def attendance_closed_at(self, employee_id, check_out):
        for rec in self.attendances.values():
            if rec["employee_id"] == employee_id and rec["check_out"] == check_out:
                return {"id": rec["id"], "check_in": rec["check_in"].strftime("%Y-%m-%d %H:%M:%S")}
        return None

    def create_attendance(
        self, employee_id, check_in, check_out=None, biotime_ref=None, device_id=None
    ):
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
            "device_id": device_id,
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

    # Stands in for the optional biobridge_attendance add-on's model. Keyed
    # on serial number, same identity BioBridge's own Device row uses, so a
    # test can assert a terminal was only upserted once per run.
    def upsert_device(
        self, serial_number, name=None, location=None, terminal_model=None, ip_address=None
    ):
        self.calls.append(f"upsert_device:{serial_number}")
        if serial_number not in self.devices:
            self._next_device_id += 1
            self.devices[serial_number] = self._next_device_id
        return self.devices[serial_number]

    # Linking past attendance to its device (app.services.device_links).
    def attendance_ids_without_device(self, attendance_ids):
        return [
            i for i in attendance_ids
            if i in self.attendances and not self.attendances[i].get("device_id")
        ]

    def set_attendance_device(self, attendance_ids, device_id):
        self.calls.append(f"set_attendance_device:{device_id}:{len(attendance_ids)}")
        for i in attendance_ids:
            self.attendances[i]["device_id"] = device_id


class FakeProvider:
    """Stands in at the provider seam, so the seam itself is exercised."""

    label = "Fake"
    cached_token = "tok"

    def __init__(
        self,
        rows: list[dict],
        employees: list[EmployeeRecord] | None = None,
        supports_employees: bool = True,
    ) -> None:
        self.rows = rows
        #: What fetch_employees() already knows about, mutated in place by
        #: create_employee() — a test reads this afterwards to see what
        #: _provision_employees actually pushed.
        self.employees: list[EmployeeRecord] = list(employees or [])
        self.created_employees: list[EmployeeRecord] = []
        #: Lets a test model a vendor with no employee-provisioning
        #: capability at all, same as ZKDeviceProvider before this
        #: session's work or any future read-only integration.
        self.supports_employees = supports_employees

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

    def supports(self, capability) -> bool:
        if capability in (Capability.READ_EMPLOYEES, Capability.WRITE_EMPLOYEES):
            return self.supports_employees
        return True

    def fetch_employees(self):
        yield from self.employees

    def create_employee(self, record: EmployeeRecord) -> EmployeeRecord:
        created = EmployeeRecord(
            external_id=str(len(self.employees) + 1),
            emp_code=record.emp_code,
            first_name=record.first_name,
            last_name=record.last_name,
            is_active=True,
        )
        self.employees.append(created)
        self.created_employees.append(created)
        return created

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
