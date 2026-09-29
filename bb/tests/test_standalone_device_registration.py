"""A standalone device is recorded as a terminal as soon as it is reached.

There is only one terminal behind a standalone connection, so there is
nothing to "import": Connect (which follows a successful test) and every
later Test connection record it locally — with the name and location the
customer gave it — and push it to Odoo's device model. See
app.api.v1.connections._register_standalone_device.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.v1.connections as connections_mod
from app.db.session import get_db
from app.integrations.base import (
    _REGISTRY,
    AttendanceProvider,
    Capability,
    ConnectionInfo,
    PunchEvent,
    TerminalRecord,
    register,
)
from app.main import app
from app.models import Base, Device, OdooConnection
from tests.conftest import FakeOdoo

SLUG = "test_standalone_unit"


@register
class _FakeUnit(AttendanceProvider):
    slug = SLUG
    label = "Test Standalone Unit"
    kinds = ("device",)
    capabilities = frozenset({Capability.READ_PUNCHES, Capability.LIST_TERMINALS})
    serial = "OIN7010066122100033"
    reachable = True

    def test_connection(self) -> ConnectionInfo:
        if not type(self).reachable:
            return ConnectionInfo(ok=False, message="no answer")
        return ConnectionInfo(ok=True, message=f"Connected to device {type(self).serial}")

    def fetch_punches(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> Iterator[PunchEvent]:
        return iter(())

    def fetch_terminals(self) -> Iterator[TerminalRecord]:
        yield TerminalRecord(serial_number=type(self).serial, alias="uFace202/ID",
                             ip_address="10.0.11.43")


class RecordingOdoo(FakeOdoo):
    def __init__(self):
        super().__init__()
        self.upserts: list[dict] = []

    def upsert_device(self, serial_number, **kwargs):
        self.upserts.append({"serial": serial_number, **kwargs})
        return super().upsert_device(serial_number, **kwargs)


@pytest.fixture(autouse=True)
def _reset():
    _FakeUnit.serial = "OIN7010066122100033"
    _FakeUnit.reachable = True
    yield


@pytest.fixture(autouse=True, scope="module")
def _unregister_after_module():
    yield
    _REGISTRY.pop(SLUG, None)


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
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


def _token(client):
    r = client.post("/api/v1/auth/signup", json={
        "company_name": "Acme", "email": "owner@acme.com",
        "password": "a-long-enough-password", "timezone": "Asia/Kolkata"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _odoo_with_tracking(client, headers):
    r = client.post("/api/v1/odoo-connections", headers=headers, json={
        "name": "Odoo", "url": "https://acme.odoo.com", "db_name": "acme",
        "username": "bot@acme.com", "api_key": "k"})
    assert r.status_code == 201, r.text
    db = client.SessionLocal()
    db.scalars(select(OdooConnection)).first().has_device_tracking = True
    db.commit(); db.close()


def _devices(client):
    db = client.SessionLocal()
    try:
        return db.scalars(select(Device)).all()
    finally:
        db.close()


def _add(client, headers, **extra):
    r = client.post("/api/v1/sources", headers=headers, json={
        "provider": SLUG, "connection_kind": "device", "name": "Front door",
        "base_url": "https://10.0.11.43.test", **extra})
    assert r.status_code == 201, r.text
    return r.json()


def test_connect_records_the_device_and_pushes_it_to_odoo(client, monkeypatch):
    headers = _token(client)
    _odoo_with_tracking(client, headers)
    odoo = RecordingOdoo()
    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: odoo)

    source = _add(client, headers, location="Main entrance")
    assert source["location"] == "Main entrance"

    (device,) = _devices(client)
    assert device.serial_number == "OIN7010066122100033"
    assert device.alias == "Front door"
    assert device.area == "Main entrance"
    assert device.ip_address == "10.0.11.43"
    assert device.model == "uFace202/ID"
    assert odoo.upserts == [{
        "serial": "OIN7010066122100033", "name": "Front door",
        "location": "Main entrance", "terminal_model": "uFace202/ID",
        "ip_address": "10.0.11.43",
    }]


def test_rename_and_relocate_then_test_updates_the_same_record(client, monkeypatch):
    headers = _token(client)
    _odoo_with_tracking(client, headers)
    odoo = RecordingOdoo()
    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: odoo)
    source = _add(client, headers, location="Main entrance")

    r = client.patch(f"/api/v1/sources/{source['id']}", headers=headers,
                     json={"name": "Lobby door", "location": "Lobby"})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/sources/{source['id']}/test", headers=headers)
    assert r.json()["ok"] and "Updated terminal" in r.json()["message"]

    (device,) = _devices(client)
    assert (device.alias, device.area) == ("Lobby door", "Lobby")
    assert odoo.upserts[-1]["name"] == "Lobby door" and odoo.upserts[-1]["location"] == "Lobby"
    assert len(odoo.devices) == 1  # same Odoo record, not a second one


def test_unreachable_device_records_nothing(client, monkeypatch):
    headers = _token(client)
    _FakeUnit.reachable = False
    _add(client, headers)
    assert _devices(client) == []


def test_swapped_unit_flags_the_old_serial_missing(client, monkeypatch):
    headers = _token(client)
    source = _add(client, headers)
    _FakeUnit.serial = "NEW-SERIAL-1"
    client.post(f"/api/v1/sources/{source['id']}/test", headers=headers)
    by_serial = {d.serial_number: d for d in _devices(client)}
    assert by_serial["NEW-SERIAL-1"].missing_since is None
    assert by_serial["OIN7010066122100033"].missing_since is not None


def test_platform_connections_are_left_to_import_terminals(client):
    headers = _token(client)
    r = client.post("/api/v1/sources", headers=headers, json={
        "provider": "biotime", "connection_kind": "platform", "name": "BT",
        "base_url": "https://bt.test", "username": "u", "password": "p"})
    assert r.status_code == 201, r.text
    assert _devices(client) == []
