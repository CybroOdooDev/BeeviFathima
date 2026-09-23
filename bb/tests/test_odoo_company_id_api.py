"""OdooConnection.company_id, exercised through the real HTTP stack:
create/update round-trips it, Test Connection fills in company_name from
whatever OdooClient.ping() reports and fails loudly when the configured id
isn't actually visible to the connected Odoo user.

The OdooClient-level mechanics (allowed_company_ids injection, the domain
conditions, the bootstrap x_company_id field) are covered directly against
a fake XML-RPC transport in test_odoo_company_scoping.py; this file is the
one layer up — does the API wire company_id through end to end.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.v1.connections as connections_mod
from app.db.session import get_db
from app.integrations.odoo import OdooError
from app.main import app
from app.models import Base


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


class _StubOdooClient:
    """Stands in at the ``build_odoo_client`` seam — ``_probe_odoo`` only
    ever calls ``.ping()`` on what that returns, so nothing deeper than
    that needs to be real here."""

    def __init__(self, ping_result=None, ping_error=None):
        self._ping_result = ping_result
        self._ping_error = ping_error

    def ping(self):
        if self._ping_error:
            raise self._ping_error
        return self._ping_result


def _ping_ok(companies, uid=7):
    return {
        "ok": True,
        "server_version": "18.0",
        "uid": uid,
        "employee_count": 3,
        "can_create_attendance": True,
        "has_companion_addon": False,
        "has_device_tracking": False,
        "device_tracking_mode": None,
        "companies": companies,
    }


def test_create_and_read_round_trip_company_id(client, monkeypatch):
    token = signup(client, "Acme", "owner@acme.com")
    monkeypatch.setattr(
        connections_mod, "build_odoo_client",
        lambda t, c: _StubOdooClient(_ping_ok([{"id": 10, "name": "Acme A"}])),
    )

    created = client.post(
        "/api/v1/odoo-connections",
        headers=auth(token),
        json={
            "name": "Primary Odoo", "url": "https://acme.odoo.com", "db_name": "acme",
            "username": "bot@acme.com", "api_key": "key", "company_id": 10,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["company_id"] == 10

    listed = client.get("/api/v1/odoo-connections", headers=auth(token)).json()
    assert listed[0]["company_id"] == 10


def test_company_id_defaults_to_none(client, monkeypatch):
    token = signup(client, "Acme", "owner@acme.com")
    monkeypatch.setattr(
        connections_mod, "build_odoo_client",
        lambda t, c: _StubOdooClient(_ping_ok([{"id": 10, "name": "Acme A"}])),
    )

    created = client.post(
        "/api/v1/odoo-connections",
        headers=auth(token),
        json={
            "name": "Primary Odoo", "url": "https://acme.odoo.com", "db_name": "acme",
            "username": "bot@acme.com", "api_key": "key",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["company_id"] is None


def test_test_connection_fills_in_company_name_from_the_probe(client, monkeypatch):
    token = signup(client, "Acme", "owner@acme.com")
    companies = [{"id": 10, "name": "Acme A"}, {"id": 20, "name": "Acme B"}]
    monkeypatch.setattr(
        connections_mod, "build_odoo_client", lambda t, c: _StubOdooClient(_ping_ok(companies)),
    )
    created = client.post(
        "/api/v1/odoo-connections",
        headers=auth(token),
        json={
            "name": "Primary Odoo", "url": "https://acme.odoo.com", "db_name": "acme",
            "username": "bot@acme.com", "api_key": "key", "company_id": 20,
        },
    ).json()
    # create_odoo probes immediately, so this is already filled in.
    assert created["company_name"] == "Acme B"

    result = client.post(f"/api/v1/odoo-connections/{created['id']}/test", headers=auth(token))
    assert result.status_code == 200
    assert result.json()["ok"] is True

    refreshed = client.get("/api/v1/odoo-connections", headers=auth(token)).json()[0]
    assert refreshed["company_name"] == "Acme B"


def test_test_connection_fails_when_company_id_is_not_visible(client, monkeypatch):
    token = signup(client, "Acme", "owner@acme.com")
    monkeypatch.setattr(
        connections_mod, "build_odoo_client",
        lambda t, c: _StubOdooClient(
            ping_error=OdooError("This Odoo user cannot see company id 999.")
        ),
    )
    created = client.post(
        "/api/v1/odoo-connections",
        headers=auth(token),
        json={
            "name": "Primary Odoo", "url": "https://acme.odoo.com", "db_name": "acme",
            "username": "bot@acme.com", "api_key": "key", "company_id": 999,
        },
    ).json()

    result = client.post(f"/api/v1/odoo-connections/{created['id']}/test", headers=auth(token))
    assert result.status_code == 200
    assert result.json()["ok"] is False
    assert "cannot see company id 999" in result.json()["message"]

    refreshed = client.get("/api/v1/odoo-connections", headers=auth(token)).json()[0]
    assert refreshed["status"] == "failed"
    assert "cannot see company id 999" in refreshed["status_message"]


def test_updating_company_id_clears_the_stale_display_name(client, monkeypatch):
    token = signup(client, "Acme", "owner@acme.com")
    companies = [{"id": 10, "name": "Acme A"}, {"id": 20, "name": "Acme B"}]
    monkeypatch.setattr(
        connections_mod, "build_odoo_client", lambda t, c: _StubOdooClient(_ping_ok(companies)),
    )
    created = client.post(
        "/api/v1/odoo-connections",
        headers=auth(token),
        json={
            "name": "Primary Odoo", "url": "https://acme.odoo.com", "db_name": "acme",
            "username": "bot@acme.com", "api_key": "key", "company_id": 10,
        },
    ).json()
    client.post(f"/api/v1/odoo-connections/{created['id']}/test", headers=auth(token))
    assert client.get("/api/v1/odoo-connections", headers=auth(token)).json()[0]["company_name"] == "Acme A"

    updated = client.patch(
        f"/api/v1/odoo-connections/{created['id']}", headers=auth(token),
        json={"company_id": 20},
    )
    assert updated.status_code == 200
    assert updated.json()["company_id"] == 20
    assert updated.json()["company_name"] is None  # stale "Acme A" must not survive the change

    client.post(f"/api/v1/odoo-connections/{created['id']}/test", headers=auth(token))
    assert client.get("/api/v1/odoo-connections", headers=auth(token)).json()[0]["company_name"] == "Acme B"
