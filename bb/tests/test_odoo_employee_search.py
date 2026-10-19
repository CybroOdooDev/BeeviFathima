"""Matching a badge by picking an Odoo employee by name, not typing an id."""

from __future__ import annotations

from sqlalchemy import select

import app.api.v1.connections as connections_mod
from app.integrations.odoo import OdooError
from app.models import EmployeeMapping, Tenant
from tests.test_connection_test_before_save import PING_OK, _StubOdooClient, _session
from tests.test_odoo_company_id_api import auth, client, signup  # noqa: F401


class _SearchOdoo(_StubOdooClient):
    def __init__(self, rows=None, error=None):
        super().__init__(PING_OK)
        self.rows = rows or []
        self.error = error
        self.calls = []

    def fields_of(self, model):
        return {"name", "department_id", "company_id"}

    def _company_domain(self, available):
        return []

    def execute(self, model, method, args, kwargs=None):
        if self.error:
            raise self.error
        self.calls.append((model, method, args, kwargs))
        return self.rows


def _connect(client, token, monkeypatch, fake):  # noqa: F811
    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda *_: fake)
    r = client.post("/api/v1/odoo-connections", json={"url": "https://acme.odoo.com", "db_name": "acme",
                    "username": "bot", "api_key": "k"}, headers=auth(token))
    assert r.status_code == 201, r.text


def test_search_finds_employees_by_name_and_flags_the_matched_ones(client, monkeypatch):  # noqa: F811
    token = signup(client, "Acme", "owner@acme.example.com")
    fake = _SearchOdoo(rows=[{"id": 14, "name": "Beevi", "department_id": [3, "Finance"]},
                             {"id": 51, "name": "Marc Dubois", "department_id": False}])
    _connect(client, token, monkeypatch, fake)
    db = _session(client)
    tenant = db.scalars(select(Tenant)).first()
    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="5", odoo_employee_id=14, status="mapped"))
    db.commit(); db.close()

    body = client.get("/api/v1/odoo-employees?q=bee", headers=auth(token)).json()
    assert body == [
        {"id": 14, "name": "Beevi", "department": "Finance", "matched_badge": "5"},
        {"id": 51, "name": "Marc Dubois", "department": None, "matched_badge": None},
    ]
    (_, method, args, kwargs) = fake.calls[-1]
    assert method == "search_read" and ("name", "ilike", "bee") in args[0]
    assert kwargs["limit"] <= 50


def test_search_needs_an_odoo_connection(client):  # noqa: F811
    token = signup(client, "Acme", "owner@acme.example.com")
    r = client.get("/api/v1/odoo-employees?q=x", headers=auth(token))
    assert r.status_code == 400 and "Connect Odoo" in r.json()["detail"]


def test_an_odoo_failure_is_reported_not_raised(client, monkeypatch):  # noqa: F811
    token = signup(client, "Acme", "owner@acme.example.com")
    fake = _SearchOdoo()
    _connect(client, token, monkeypatch, fake)
    fake.error = OdooError("Access denied")
    r = client.get("/api/v1/odoo-employees?q=x", headers=auth(token))
    assert r.status_code == 502 and "Access denied" in r.json()["detail"]


def test_matching_keeps_the_picked_name(client, monkeypatch):  # noqa: F811
    token = signup(client, "Acme", "owner@acme.example.com")
    db = _session(client)
    tenant = db.scalars(select(Tenant)).first()
    m = EmployeeMapping(tenant_id=tenant.id, emp_code="0042", status="unmapped")
    db.add(m); db.commit(); mid = m.id; db.close()
    r = client.patch(f"/api/v1/mappings/{mid}", json={"odoo_employee_id": 51, "odoo_employee_name": "Marc Dubois"},
                     headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["odoo_employee_name"] == "Marc Dubois" and r.json()["status"] == "mapped"
