"""GET /odoo-connections/{id}/companies — what fills the company dropdown."""
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
from tests.conftest import FakeOdoo


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _setup(client, email="owner@acme.com"):
    r = client.post("/api/v1/auth/signup", json={
        "company_name": email.split("@")[1], "email": email,
        "password": "a-long-enough-password", "timezone": "UTC"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = client.post("/api/v1/odoo-connections", headers=headers, json={
        "name": "Odoo", "url": "https://acme.odoo.com", "db_name": "acme",
        "username": "bot@acme.com", "api_key": "k", "company_id": 4})
    assert r.status_code == 201, r.text
    return headers, r.json()["id"]


class CompaniesOdoo(FakeOdoo):
    def list_companies(self):
        return [{"id": 1, "name": "HQ", "extra": "x"}, {"id": 4, "name": "DEMO 6"}]


def test_lists_every_company_the_login_can_reach(client, monkeypatch):
    headers, conn_id = _setup(client)
    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: CompaniesOdoo())
    r = client.get(f"/api/v1/odoo-connections/{conn_id}/companies", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == [{"id": 1, "name": "HQ"}, {"id": 4, "name": "DEMO 6"}]


def test_odoo_failure_is_a_502_not_a_crash(client, monkeypatch):
    headers, conn_id = _setup(client)

    class Down(FakeOdoo):
        def authenticate(self):
            raise OdooError("unreachable")

    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: Down())
    r = client.get(f"/api/v1/odoo-connections/{conn_id}/companies", headers=headers)
    assert r.status_code == 502


def test_another_tenants_connection_is_a_404(client, monkeypatch):
    _, conn_id = _setup(client)
    other, _ = _setup(client, "owner@globex.com")
    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: CompaniesOdoo())
    r = client.get(f"/api/v1/odoo-connections/{conn_id}/companies", headers=other)
    assert r.status_code == 404
