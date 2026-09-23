"""Import terminals -> Odoo device push.

Regression coverage for a real customer report: three terminals imported,
only one ever showed up in Odoo's device model. The cause was that a device
only ever got pushed to Odoo the slow way — the first time one of its
punches made it into a closed, pushed attendance record — and a
PunchRecord's device link is decided once, at ingest time, from whatever
local Device rows existed *then*. A terminal discovered after its punches
were already ingested (or already synced) could sit forever with no Odoo
device record. discover_devices now pushes every terminal it finds straight
to Odoo, independent of punch history — see
app.api.v1.connections._push_devices_to_odoo.

That push can still fail for reasons the customer needs to see (Odoo
unreachable, a permission error, a stale schema) rather than only a
server-side log line — so a failure is surfaced on the source's
``status_message``, the same "Last error" banner a failed sync run already
uses.
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
from app.integrations.odoo import OdooError
from app.main import app
from app.models import Base, OdooConnection
from tests.conftest import FakeOdoo

DISCOVERABLE_SLUG = "test_discoverable_device"


@register
class _FakeDiscoverableProvider(AttendanceProvider):
    """Registered only for this file — LIST_TERMINALS backed by a
    class-level list a test sets directly, reset by the fixture below."""

    slug = DISCOVERABLE_SLUG
    label = "Test Discoverable Device"
    capabilities = frozenset({Capability.READ_PUNCHES, Capability.LIST_TERMINALS})
    terminals: list[dict] = []

    def test_connection(self) -> ConnectionInfo:
        return ConnectionInfo(ok=True, message="ok")

    def fetch_punches(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> Iterator[PunchEvent]:
        return iter(())

    def fetch_terminals(self) -> Iterator[TerminalRecord]:
        for row in type(self).terminals:
            yield TerminalRecord(**row)


@pytest.fixture(autouse=True)
def _reset_terminals():
    _FakeDiscoverableProvider.terminals = []
    yield
    _FakeDiscoverableProvider.terminals = []


@pytest.fixture(autouse=True, scope="module")
def _unregister_after_module():
    yield
    _REGISTRY.pop(DISCOVERABLE_SLUG, None)


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
        test_client.SessionLocal = TestingSession  # for direct DB manipulation
        yield test_client
    app.dependency_overrides.clear()


def signup(client, company, email):
    response = client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": company,
            "email": email,
            "password": "a-long-enough-password",
            "timezone": "Asia/Dubai",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def make_odoo(client, token):
    response = client.post(
        "/api/v1/odoo-connections",
        headers=auth(token),
        json={
            "name": "Primary Odoo",
            "url": "https://acme.odoo.com",
            "db_name": "acme",
            "username": "bot@acme.com",
            "api_key": "super-secret-key",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_source(client, token):
    response = client.post(
        "/api/v1/sources",
        headers=auth(token),
        json={"provider": DISCOVERABLE_SLUG, "base_url": "https://device.test"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _source_status_message(client, token, source_id):
    response = client.get("/api/v1/sources", headers=auth(token))
    assert response.status_code == 200, response.text
    (source,) = [s for s in response.json() if s["id"] == source_id]
    return source["status_message"]


def _set_device_tracking(client, on: bool = True):
    """Flip has_device_tracking directly in the DB — the same shortcut the
    device-tracking UI smoke test uses, since Test Connection can't be made
    to genuinely detect a fake Odoo's device support in this harness."""
    db = client.SessionLocal()
    try:
        conn = db.scalars(select(OdooConnection)).first()
        conn.has_device_tracking = on
        db.commit()
    finally:
        db.close()


TERMINALS = [
    {"serial_number": "GATE-01", "alias": "Front Gate", "ip_address": "10.0.0.1"},
    {"serial_number": "GATE-02", "alias": "Back Gate", "ip_address": "10.0.0.2"},
    {"serial_number": "GATE-03", "alias": "Warehouse", "ip_address": "10.0.0.3"},
]


def test_discover_devices_pushes_every_terminal_to_odoo_when_tracking_is_on(
    client, monkeypatch
):
    token = signup(client, "Acme", "owner@acme.com")
    make_odoo(client, token)
    source = make_source(client, token)
    _set_device_tracking(client, True)
    _FakeDiscoverableProvider.terminals = list(TERMINALS)

    fake_odoo = FakeOdoo()
    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: fake_odoo)

    response = client.post(
        f"/api/v1/sources/{source['id']}/discover-devices", headers=auth(token)
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == 3

    assert set(fake_odoo.devices) == {"GATE-01", "GATE-02", "GATE-03"}
    # A clean push leaves no "Last error" banner on the source card.
    assert _source_status_message(client, token, source["id"]) is None


def test_a_terminal_imported_before_tracking_was_on_gets_pushed_on_the_next_import(
    client, monkeypatch
):
    """The exact customer scenario: terminals discovered first, device
    tracking enabled later. Re-running Import terminals must catch up every
    terminal, not just ones seen for the first time this call."""
    token = signup(client, "Acme", "owner@acme.com")
    make_odoo(client, token)
    source = make_source(client, token)
    _FakeDiscoverableProvider.terminals = list(TERMINALS)

    fake_odoo = FakeOdoo()
    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: fake_odoo)

    # First import: device tracking isn't on yet, so nothing reaches Odoo —
    # but all three terminals are still imported into BioBridge itself.
    first = client.post(
        f"/api/v1/sources/{source['id']}/discover-devices", headers=auth(token)
    )
    assert len(first.json()) == 3
    assert fake_odoo.devices == {}

    # Tracking turns on, and the customer clicks Import terminals again.
    _set_device_tracking(client, True)
    second = client.post(
        f"/api/v1/sources/{source['id']}/discover-devices", headers=auth(token)
    )
    assert len(second.json()) == 3
    assert set(fake_odoo.devices) == {"GATE-01", "GATE-02", "GATE-03"}


def test_discover_devices_skips_the_odoo_push_when_tracking_is_off(client, monkeypatch):
    token = signup(client, "Acme", "owner@acme.com")
    make_odoo(client, token)
    source = make_source(client, token)
    _FakeDiscoverableProvider.terminals = list(TERMINALS)
    # device tracking left at its default (off)

    def explode(*_args, **_kwargs):  # pragma: no cover — asserts it is not called
        raise AssertionError("Odoo must not be contacted when tracking is off")

    monkeypatch.setattr(connections_mod, "build_odoo_client", explode)

    response = client.post(
        f"/api/v1/sources/{source['id']}/discover-devices", headers=auth(token)
    )
    assert response.status_code == 200
    assert len(response.json()) == 3
    # Nothing was attempted, so there's nothing to report — not even a
    # stale message from an earlier attempt.
    assert _source_status_message(client, token, source["id"]) is None


def test_discover_devices_still_succeeds_when_odoo_is_unreachable(client, monkeypatch):
    """The terminals import into BioBridge regardless — the Odoo push is a
    nice-to-have layered on top, never a reason to fail the whole action.
    But the failure must not be silent: it has to land somewhere the
    customer can actually see it, or "no new records" is undiagnosable."""
    token = signup(client, "Acme", "owner@acme.com")
    make_odoo(client, token)
    source = make_source(client, token)
    _set_device_tracking(client, True)
    _FakeDiscoverableProvider.terminals = list(TERMINALS)

    class BrokenOdoo(FakeOdoo):
        def authenticate(self):
            raise OdooError("Odoo is unreachable right now")

    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: BrokenOdoo())

    response = client.post(
        f"/api/v1/sources/{source['id']}/discover-devices", headers=auth(token)
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == 3

    message = _source_status_message(client, token, source["id"])
    assert message is not None
    assert "Odoo is unreachable right now" in message


def test_discover_devices_reports_which_devices_odoo_rejected(client, monkeypatch):
    """The exact gap this fix closes: previously a per-device upsert
    failure was only ever a server-log line — invisible to the customer,
    who just saw "no new records" with no way to tell why."""
    token = signup(client, "Acme", "owner@acme.com")
    make_odoo(client, token)
    source = make_source(client, token)
    _set_device_tracking(client, True)
    _FakeDiscoverableProvider.terminals = list(TERMINALS)

    class PartlyBrokenOdoo(FakeOdoo):
        def upsert_device(self, serial_number, **kwargs):
            if serial_number == "GATE-02":
                raise OdooError("no access rights to 'biobridge.device'")
            return super().upsert_device(serial_number, **kwargs)

    fake_odoo = PartlyBrokenOdoo()
    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: fake_odoo)

    response = client.post(
        f"/api/v1/sources/{source['id']}/discover-devices", headers=auth(token)
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == 3

    # The two good devices still made it through — one bad apple doesn't
    # sink the rest.
    assert set(fake_odoo.devices) == {"GATE-01", "GATE-03"}

    message = _source_status_message(client, token, source["id"])
    assert message is not None
    assert "GATE-02" in message
    assert "no access rights" in message
