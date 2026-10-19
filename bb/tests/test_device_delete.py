"""Deleting one terminal: only its row goes, its punches stay (unlinked), it
is scoped to the caller's account, read-only users can't, and it's audited."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import AuditLog, Device, DeviceSource, PunchRecord, Tenant


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
        c.Session = Session
        yield c
    app.dependency_overrides.clear()


def signup(client, email="owner@acme.com", company="Acme"):
    r = client.post("/api/v1/auth/signup", json={
        "company_name": company, "email": email,
        "password": "a-long-enough-password", "timezone": "Asia/Kolkata"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def seed(client, email_domain="acme.com"):
    """A source with two terminals and a punch from the first."""
    db = client.Session()
    tenant = db.scalars(select(Tenant).order_by(Tenant.created_at.desc())).first()
    source = DeviceSource(tenant_id=tenant.id, name="BioTime", provider="biotime",
                          connection_kind="server", base_url="https://bt.example.test", username="u")
    db.add(source); db.flush()
    gate1 = Device(tenant_id=tenant.id, source_id=source.id, serial_number="GATE-01", alias="Main")
    gate2 = Device(tenant_id=tenant.id, source_id=source.id, serial_number="GATE-02", alias="Back")
    db.add_all([gate1, gate2]); db.flush()
    punch = PunchRecord(tenant_id=tenant.id, source_id=source.id, device_id=gate1.id,
                        external_id="p1", emp_code="1001", terminal_sn="GATE-01",
                        punch_time_utc=datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))
    db.add(punch); db.commit()
    ids = (gate1.id, gate2.id, punch.id)
    db.close()
    return ids


def test_deleting_a_terminal_keeps_its_punches_unlinked_and_audits_it(client):
    headers = signup(client)
    gate1, gate2, punch_id = seed(client)

    assert client.delete(f"/api/v1/devices/{gate1}", headers=headers).status_code == 204

    db = client.Session()
    assert {d.serial_number for d in db.scalars(select(Device)).all()} == {"GATE-02"}
    punch = db.get(PunchRecord, punch_id)
    assert punch is not None and punch.device_id is None and punch.terminal_sn == "GATE-01"
    log = db.scalars(select(AuditLog).where(AuditLog.action == "device.delete")).all()
    assert len(log) == 1 and "GATE-01" in (log[0].detail or "")
    db.close()
    assert [d["serial_number"] for d in client.get("/api/v1/devices", headers=headers).json()] == ["GATE-02"]


def test_unknown_device_is_a_404(client):
    headers = signup(client)
    assert client.delete("/api/v1/devices/nope", headers=headers).status_code == 404


def test_another_accounts_terminal_cannot_be_deleted(client):
    signup(client, "a@a.com", "Alpha Co")
    gate1, _, _ = seed(client)
    other = signup(client, "b@b.com", "Beta Co")
    assert client.delete(f"/api/v1/devices/{gate1}", headers=other).status_code == 404
    db = client.Session()
    assert db.get(Device, gate1) is not None
    db.close()


def test_requires_sign_in(client):
    signup(client)
    gate1, _, _ = seed(client)
    assert client.delete(f"/api/v1/devices/{gate1}").status_code in (401, 403)


def test_a_deleted_terminal_with_punches_is_restored_by_the_next_sync(client):
    from app.services.device_links import restore_terminals_from_punches

    headers = signup(client)
    gate1, gate2, punch_id = seed(client)
    assert client.delete(f"/api/v1/devices/{gate1}", headers=headers).status_code == 204

    db = client.Session()
    tenant = db.scalars(select(Tenant)).first()
    source = db.scalars(select(DeviceSource)).first()
    restored = restore_terminals_from_punches(db, tenant, source)

    assert [d.serial_number for d in restored] == ["GATE-01"]       # GATE-02 had none and was never deleted
    back = restored[0]
    assert back.punch_count == 1 and back.last_seen_at is not None
    assert db.get(PunchRecord, punch_id).device_id == back.id        # its punch is linked to it again
    assert restore_terminals_from_punches(db, tenant, source) == []  # nothing further to restore
    db.close()


def test_a_deleted_terminal_without_punches_stays_deleted(client):
    from app.services.device_links import restore_terminals_from_punches

    headers = signup(client)
    gate1, gate2, _ = seed(client)
    assert client.delete(f"/api/v1/devices/{gate2}", headers=headers).status_code == 204
    db = client.Session()
    tenant = db.scalars(select(Tenant)).first()
    source = db.scalars(select(DeviceSource)).first()
    assert restore_terminals_from_punches(db, tenant, source) == []
    assert {d.serial_number for d in db.scalars(select(Device)).all()} == {"GATE-01"}
    db.close()
