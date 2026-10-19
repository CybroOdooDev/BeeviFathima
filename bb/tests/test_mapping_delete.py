"""Deleting an unmatched badge from the list."""

from __future__ import annotations

from sqlalchemy import select

from app.db.session import get_db
from app.main import app
from app.models import EmployeeMapping, MappingStatus, PunchRecord
from tests.test_api_isolation import auth, client, signup  # noqa: F401


def _tenant_id(client, token):
    return client.get("/api/v1/tenant", headers=auth(token)).json()["id"]


def test_unmatched_badge_can_be_deleted_and_stays_gone(client):
    token = signup(client, "Acme", "owner@example.com")
    tid = _tenant_id(client, token)
    db = next(app.dependency_overrides[get_db]())
    m = EmployeeMapping(tenant_id=tid, emp_code="9001", status=MappingStatus.unmapped.value)
    db.add(m); db.commit()
    assert [x["emp_code"] for x in client.get("/api/v1/mappings", headers=auth(token)).json()] == ["9001"]

    r = client.delete(f"/api/v1/mappings/{m.id}", headers=auth(token))
    assert r.status_code == 200
    assert client.get("/api/v1/mappings", headers=auth(token)).json() == []
    db.expire_all()
    # Kept hidden so a later sync does not recreate it.
    assert db.get(EmployeeMapping, m.id).status == MappingStatus.removed.value


def test_a_matched_employee_cannot_be_deleted_this_way(client):
    token = signup(client, "Acme", "owner@example.com")
    tid = _tenant_id(client, token)
    db = next(app.dependency_overrides[get_db]())
    m = EmployeeMapping(tenant_id=tid, emp_code="9002", status=MappingStatus.mapped.value,
                        odoo_employee_id=5)
    db.add(m); db.commit()
    assert client.delete(f"/api/v1/mappings/{m.id}", headers=auth(token)).status_code == 400


def test_another_tenants_badge_is_not_deletable(client):
    a = signup(client, "A Co", "a@example.com")
    b = signup(client, "B Co", "b@example.org")
    db = next(app.dependency_overrides[get_db]())
    m = EmployeeMapping(tenant_id=_tenant_id(client, a), emp_code="1", status="unmapped")
    db.add(m); db.commit()
    assert client.delete(f"/api/v1/mappings/{m.id}", headers=auth(b)).status_code == 404
