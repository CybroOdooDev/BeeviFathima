"""Import terminals -> create unmapped Odoo employees on the device.

The rule (app.services.provisioning): an active Odoo employee who isn't
mapped to a device user yet and has a Badge ID or PIN is created on the
device, with that value as their user id. Registration number and work email
never qualify.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.v1.connections as connections_mod
from app.db.session import get_db
from app.integrations.base import (
    _REGISTRY,
    AttendanceProvider,
    Capability,
    ConnectionInfo,
    EmployeeRecord,
    PunchEvent,
    ProviderError,
    register,
)
from app.integrations.odoo import OdooError
from app.integrations.providers.zkteco import ZKDeviceProvider
from app.integrations.base import SourceConfig
from app.main import app
from app.models import Base, EmployeeMapping, MappingStatus, Tenant
from app.services.provisioning import provision_code, provision_unmapped
from tests.conftest import FakeOdoo
from tests.test_zkteco_protocol import FakeZKDevice, _encode_user_record, _encode_user_record_28

SLUG = "test_provisionable_device"


class _FakeDeviceUsers:
    """A device's user table, for the unit tests: fetch/create only."""

    def __init__(self, codes=(), refuse=()):
        self.users = [EmployeeRecord(external_id=str(i), emp_code=c) for i, c in enumerate(codes)]
        self.refuse = set(refuse)

    def fetch_employees(self):
        return iter(list(self.users))

    def create_employee(self, record):
        if record.emp_code in self.refuse:
            raise ProviderError(f"device refused {record.emp_code}")
        self.users.append(record)
        return record


def emp(id_, name, barcode=False, pin=False, active=True, **other):
    return {"id": id_, "name": name, "barcode": barcode, "pin": pin, "active": active, **other}


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
def test_badge_id_is_used_first_then_pin():
    assert provision_code(emp(1, "A", barcode="1001", pin="77")) == "1001"
    assert provision_code(emp(1, "A", pin="77")) == "77"
    assert provision_code(emp(1, "A", barcode="  ", pin=" 77 ")) == "77"


def test_registration_number_and_work_email_never_qualify():
    row = emp(1, "A", registration_number="R-9", work_email="a@x.com")
    assert provision_code(row) is None


def test_creates_only_the_employees_the_rule_allows():
    device = _FakeDeviceUsers(codes=["1003"])
    roster = [
        emp(1, "Ann Lee", barcode="1001"),               # created
        emp(2, "Bo Chan", pin="1002"),                   # created, from PIN
        emp(3, "Cy Diaz", barcode="1003"),               # already on the device
        emp(4, "Di Eze", barcode="1004"),                # mapped in BioBridge already
        emp(5, "Ed Fox", registration_number="R-5"),     # no badge or PIN
        emp(6, "Flo Gu", barcode="1006", active=False),  # archived: ignored
    ]
    result = provision_unmapped(device, roster, mapped_odoo_ids={4})

    assert [c["emp_code"] for c in result.created] == ["1001", "1002"]
    assert result.already_on_device == 1
    assert result.already_mapped == 1
    assert result.no_badge_or_pin == 1
    assert result.failed == []
    created = {u.emp_code: u for u in device.users}
    assert (created["1001"].first_name, created["1001"].last_name) == ("Ann", "Lee")


def test_a_badge_shared_by_two_employees_creates_neither():
    device = _FakeDeviceUsers()
    roster = [emp(1, "Ann", barcode="1001"), emp(2, "Bo", barcode="1001"), emp(3, "Cy", barcode="1003")]
    result = provision_unmapped(device, roster, set())
    assert [c["emp_code"] for c in result.created] == ["1003"]
    assert {f["name"] for f in result.failed} == {"Ann", "Bo"}
    assert all("share" in f["error"] for f in result.failed)


def test_one_refused_employee_does_not_stop_the_rest():
    device = _FakeDeviceUsers(refuse={"1001"})
    result = provision_unmapped(
        device, [emp(1, "Ann", barcode="1001"), emp(2, "Bo", barcode="1002")], set()
    )
    assert [c["emp_code"] for c in result.created] == ["1002"]
    assert result.failed == [{"emp_code": "1001", "name": "Ann", "error": "device refused 1001"}]


def test_running_it_twice_creates_nobody_the_second_time():
    device = _FakeDeviceUsers()
    roster = [emp(1, "Ann", barcode="1001")]
    assert len(provision_unmapped(device, roster, set()).created) == 1
    second = provision_unmapped(device, roster, set())
    assert second.created == [] and second.already_on_device == 1


# --------------------------------------------------------------------------- #
# Against the real ZKTeco provider and the fake terminal
# --------------------------------------------------------------------------- #
def _zk(device):
    return ZKDeviceProvider(SourceConfig(base_url=f"zk://{device.host}:{device.port}", timezone="UTC"))


def test_creates_users_on_a_zkteco_terminal():
    with FakeZKDevice(user_records=[_encode_user_record(1, "1001", "Ann Lee")]) as device:
        roster = [emp(1, "Ann Lee", barcode="1001"), emp(2, "Bo Chan", pin="1002")]
        result = provision_unmapped(_zk(device), roster, set())
        assert [c["emp_code"] for c in result.created] == ["1002"]
        assert sorted(u.emp_code for u in _zk(device).fetch_employees()) == ["1001", "1002"]


def test_a_code_the_terminal_cant_hold_is_reported_not_raised():
    # ZK6 firmware (28-byte user table) stores numeric user ids only.
    with FakeZKDevice(user_records=[_encode_user_record_28(1, 1001, "Ann")]) as device:
        result = provision_unmapped(_zk(device), [emp(2, "Bo", barcode="EMP-2")], set())
        assert result.created == []
        assert result.failed[0]["emp_code"] == "EMP-2"
        assert "plain numbers" in result.failed[0]["error"]


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #
@register
class _FakeProvisionableProvider(AttendanceProvider):
    slug = SLUG
    label = "Test Provisionable Device"
    capabilities = frozenset({
        Capability.READ_PUNCHES, Capability.LIST_TERMINALS,
        Capability.READ_EMPLOYEES, Capability.WRITE_EMPLOYEES,
    })
    users: list[str] = []

    def test_connection(self) -> ConnectionInfo:
        return ConnectionInfo(ok=True, message="ok")

    def fetch_punches(self, since: datetime | None = None, until: datetime | None = None) -> Iterator[PunchEvent]:
        return iter(())

    def fetch_employees(self):
        for code in type(self).users:
            yield EmployeeRecord(external_id=code, emp_code=code)

    def create_employee(self, record):
        type(self).users.append(record.emp_code)
        return record


@pytest.fixture(autouse=True)
def _reset_users():
    _FakeProvisionableProvider.users = []
    yield


@pytest.fixture(autouse=True, scope="module")
def _unregister_after_module():
    yield
    _REGISTRY.pop(SLUG, None)


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        test_client.SessionLocal = TestingSession
        yield test_client
    app.dependency_overrides.clear()


def _setup(client, odoo=True):
    r = client.post("/api/v1/auth/signup", json={
        "company_name": "Acme", "email": "owner@acme.com",
        "password": "a-long-enough-password", "timezone": "Asia/Dubai",
    })
    assert r.status_code == 201, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    if odoo:
        r = client.post("/api/v1/odoo-connections", headers=headers, json={
            "name": "Primary Odoo", "url": "https://acme.odoo.com", "db_name": "acme",
            "username": "bot@acme.com", "api_key": "super-secret-key",
        })
        assert r.status_code == 201, r.text
    r = client.post("/api/v1/sources", headers=headers, json={"provider": SLUG, "base_url": "https://device.test"})
    assert r.status_code == 201, r.text
    return headers, r.json()["id"]


def test_endpoint_creates_unmapped_employees_and_skips_mapped_ones(client, monkeypatch):
    headers, source_id = _setup(client)
    fake_odoo = FakeOdoo()
    fake_odoo.roster = [emp(11, "Ann Lee", barcode="1001"), emp(12, "Bo Chan", pin="1002"),
                        emp(13, "Cy Diaz", work_email="cy@acme.com")]
    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: fake_odoo)

    # Ann is already mapped in BioBridge (say, matched by hand to badge 7).
    db = client.SessionLocal()
    tenant = db.query(Tenant).one()
    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="7", odoo_employee_id=11,
                           status=MappingStatus.mapped.value))
    db.commit()
    db.close()

    r = client.post(f"/api/v1/sources/{source_id}/provision-employees", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["emp_code"] for c in body["created"]] == ["1002"]
    assert body["already_mapped"] == 1
    assert body["no_badge_or_pin"] == 1
    assert _FakeProvisionableProvider.users == ["1002"]


def test_endpoint_needs_an_odoo_connection(client):
    headers, source_id = _setup(client, odoo=False)
    r = client.post(f"/api/v1/sources/{source_id}/provision-employees", headers=headers)
    assert r.status_code == 400
    assert "Connect Odoo" in r.json()["detail"]


def test_endpoint_reports_an_unreachable_odoo(client, monkeypatch):
    headers, source_id = _setup(client)

    class Down(FakeOdoo):
        def authenticate(self):
            raise OdooError("Odoo is down")

    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: Down())
    r = client.post(f"/api/v1/sources/{source_id}/provision-employees", headers=headers)
    assert r.status_code == 502
    assert "Odoo is down" in r.json()["detail"]
    assert _FakeProvisionableProvider.users == []
