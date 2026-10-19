"""Shared fixtures: an in-memory database and stubs for both external systems."""

from __future__ import annotations

import os
import tempfile

# Before anything imports app.core.config: the app's own start-up (create_all
# on settings.database_url) must never touch ./biobridge.db in the project, and
# parallel runs must not share one file. Each test process gets its own.
os.environ["DATABASE_URL"] = (
    "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="bb-tests-"), "app.db")
)

# A proxy in the environment would carry requests to outside hosts through a
# local port, past the network guard below. Tests never need one.
for _var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
    os.environ.pop(_var, None)

import importlib.util
import ipaddress
import socket
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

# --------------------------------------------------------------------------- #
# No network: a test may talk to servers it starts on this machine, nothing
# else. A test that tries (an Odoo URL like acme.odoo.com in a fixture) gets an
# immediate OSError — the same "cannot reach" path a real outage takes — rather
# than waiting on DNS or a proxy, so the suite is fast and gives the same result
# with or without internet access.
# --------------------------------------------------------------------------- #
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "testserver", ""}


def _is_local(host) -> bool:
    if host is None:
        return True
    host = host.decode() if isinstance(host, bytes) else str(host)
    host = host.strip("[]").lower()
    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


class NetworkBlocked(OSError):
    pass


_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_ip_literal(host) -> bool:
    try:
        ipaddress.ip_address((host.decode() if isinstance(host, bytes) else str(host)).strip("[]"))
        return True
    except ValueError:
        return False


def _guard_getaddrinfo(host, *args, **kwargs):
    # An IP literal resolves without any lookup; only a name needs DNS.
    if not _is_local(host) and not _is_ip_literal(host):
        raise socket.gaierror(socket.EAI_NONAME, f"network disabled in tests ({host})")
    return _real_getaddrinfo(host, *args, **kwargs)


def _target_host(address):
    if isinstance(address, tuple) and address:
        return address[0]
    return None  # AF_UNIX path


def _guard_connect(self, address):
    if not _is_local(_target_host(address)):
        raise NetworkBlocked(f"network disabled in tests ({address!r})")
    return _real_connect(self, address)


def _guard_connect_ex(self, address):
    if not _is_local(_target_host(address)):
        raise NetworkBlocked(f"network disabled in tests ({address!r})")
    return _real_connect_ex(self, address)


socket.getaddrinfo = _guard_getaddrinfo
socket.socket.connect = _guard_connect
socket.socket.connect_ex = _guard_connect_ex


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


@pytest.fixture(autouse=True)
def _no_console_stripe_keys():
    """Console-saved Stripe keys are process-wide (app.services.billing);
    start and end every test on the .env settings."""
    from app.services import billing

    billing._CONSOLE = None
    yield
    billing._CONSOLE = None


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
        #: Enabled company ids, or None for "no restriction" — what
        #: OdooClient.company_scope() answers. A test sets it (together with
        #: `roster` as the in-scope people) to drive the company switches.
        self.scope: list[int] | None = None
        #: Employees that exist but sit in a switched-off company; found only
        #: by an unscoped lookup.
        self.out_of_scope_employees: dict[str, tuple[int, str]] = {}
        #: Every company the Odoo user can see, as list_companies() answers;
        #: empty by default so scope-None means "a single-company Odoo".
        self.companies: list[dict] = []

    def list_companies(self):
        return self.companies

    def authenticate(self) -> int:
        return self.uid

    def company_scope(self):
        return self.scope

    def find_employee(self, emp_code, *, scoped=True):
        if not scoped and emp_code in self.out_of_scope_employees:
            emp_id, name = self.out_of_scope_employees[emp_code]
            return emp_id, name, "barcode"
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

    def attendances_by_id(self, attendance_ids):
        fmt = "%Y-%m-%d %H:%M:%S"
        return {
            i: {
                "id": i,
                "check_in": self.attendances[i]["check_in"].strftime(fmt),
                "check_out": self.attendances[i]["check_out"].strftime(fmt)
                if self.attendances[i]["check_out"] else False,
            }
            for i in attendance_ids if i in self.attendances
        }

    def attendance_covering(self, employee_id, moment):
        """Mirrors OdooClient.attendance_covering: the latest record starting at
        or before ``moment``, when it ends after it."""
        starts = [
            r for r in self.attendances.values()
            if r["employee_id"] == employee_id and r["check_in"] <= moment
        ]
        if not starts:
            return None
        last = max(starts, key=lambda r: r["check_in"])
        if last["check_out"] is None or last["check_out"] <= moment:
            return None
        return {
            "id": last["id"],
            "check_in": last["check_in"].strftime("%Y-%m-%d %H:%M:%S"),
            "check_out": last["check_out"].strftime("%Y-%m-%d %H:%M:%S"),
        }

    def create_attendance(
        self, employee_id, check_in, check_out=None, biotime_ref=None, device_id=None,
        pairing_mode=None, auto_closed=False,
    ):
        # ...and, closed or not, none that overlaps another (hr.attendance's
        # _check_validity). The engine must never ask for one.
        covering = self.attendance_covering(employee_id, check_in)
        if covering is not None:
            raise AssertionError(
                f"Cannot create new attendance record for {employee_id}, the employee was "
                f"already checked in on {check_in} (inside record {covering['id']})"
            )
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
            "pairing_mode": pairing_mode,
            "auto_closed": auto_closed,
        }
        self.calls.append(f"create:{self._next_id}")
        return self._next_id

    def close_attendance(self, attendance_id, check_out, pairing_mode=None, auto_closed=False):
        self.attendances[attendance_id]["check_out"] = check_out
        self.attendances[attendance_id]["auto_closed"] = auto_closed
        if pairing_mode:
            self.attendances[attendance_id]["pairing_mode"] = pairing_mode
        self.calls.append(f"close:{attendance_id}")
        return True

    def attendance_pairing_methods(self, attendance_ids):
        return {i: self.attendances[i].get("pairing_mode") for i in attendance_ids if i in self.attendances}

    def update_attendance(
        self, attendance_id, check_in=None, check_out=None, reopen=False, pairing_mode=None,
        auto_closed=False,
    ):
        rec = self.attendances[attendance_id]
        if pairing_mode:
            rec["pairing_mode"] = pairing_mode
        if check_in is not None:
            rec["check_in"] = check_in
        if check_out is not None:
            rec["check_out"] = check_out
        elif reopen:
            rec["check_out"] = None
        self.calls.append(f"update:{attendance_id}")
        return True

    def create_employee(self, name, emp_code):  # pragma: no cover
        raise AssertionError("auto-create should be off in these tests")

    # Introspection used by the engine's companion-addon check.
    def fields_of(self, model):
        return {"check_in", "check_out", "employee_id"}

    # Stands in for the optional biobridge_attendance add-on's model. Keyed
    # on serial number, same identity BioBridge's own Device row uses, so a
    # test can assert a terminal was only upserted once per run.
    def ensure_device_company_rule(self):
        return False

    def check_device_isolation(self):
        return []

    def upsert_device(
        self, serial_number, name=None, location=None, terminal_model=None, ip_address=None,
        company_id=None,
    ):
        self.device_companies = getattr(self, 'device_companies', {})
        self.device_companies[serial_number] = company_id
        self.calls.append(f"upsert_device:{serial_number}")
        if serial_number not in self.devices:
            self._next_device_id += 1
            self.devices[serial_number] = self._next_device_id
        return self.devices[serial_number]

    def delete_device(self, serial_number, company_id=None):
        self.calls.append(f"delete_device:{serial_number}")
        return self.devices.pop(serial_number, None) is not None

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


@pytest.fixture(autouse=True)
def _no_background_roster_import(monkeypatch):
    """The save/test hooks import device users on a best-effort basis; keep the
    API tests off the network. test_roster_import drives the service directly."""
    import app.api.v1.connections as connections_api

    monkeypatch.setattr(connections_api, "auto_import", lambda db, tenant: 0)
