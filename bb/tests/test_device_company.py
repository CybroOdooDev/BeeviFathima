"""A terminal belongs to an Odoo company; a punch that does not fit waits for review.

Unassigned terminals pass on a single-company Odoo and are held on a
multi-company one; a terminal whose company differs from the employee's is
held on either.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Device, DeviceSource, EmployeeMapping, PunchRecord, PunchState
from tests.conftest import FakeOdoo
from tests.test_sync_engine import punch, run

A, B = 10, 20
COMPANIES = [{"id": A, "name": "Acme A"}, {"id": B, "name": "Acme B"}]


def _punch_row(db):
    return db.scalars(select(PunchRecord)).one()


def _terminal(db, tenant, **fields):
    source = db.scalars(select(DeviceSource).where(DeviceSource.tenant_id == tenant.id)).first()
    device = db.scalar(select(Device).where(Device.source_id == source.id))
    if device is None:
        device = Device(tenant_id=tenant.id, source_id=source.id, serial_number="GATE-01")
        db.add(device)
    for key, value in fields.items():
        setattr(device, key, value)
    db.commit()
    return source, device


def _employee_in(db, tenant, company_id, name="Acme X"):
    for m in db.scalars(select(EmployeeMapping).where(EmployeeMapping.tenant_id == tenant.id)):
        m.odoo_company_id = company_id
        m.odoo_company_name = name
    db.commit()


def test_single_company_odoo_lets_an_unassigned_terminal_through(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    odoo.companies = [COMPANIES[0]]
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)
    assert _punch_row(db).process_state == PunchState.synced.value


def test_multi_company_odoo_holds_an_unassigned_terminal(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    odoo.companies = COMPANIES
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)
    row = _punch_row(db)
    assert row.process_state == PunchState.held.value
    assert row.error_message.startswith("Held: company not set")
    assert not odoo.attendances


def test_assigning_the_terminal_releases_the_held_punch(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    odoo.companies = COMPANIES
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)
    assert _punch_row(db).process_state == PunchState.held.value

    _terminal(db, tenant, company_id=A, company_name="Acme A")
    _employee_in(db, tenant, A)
    run(db, tenant, odoo, [], monkeypatch)
    row = _punch_row(db)
    assert row.process_state == PunchState.synced.value
    assert row.error_message is None
    assert len(odoo.attendances) == 1


def test_connection_default_company_covers_its_terminals(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    odoo.companies = COMPANIES
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)
    source, _ = _terminal(db, tenant)
    source.config = {**(source.config or {}), "company_id": A, "company_name": "Acme A"}
    db.commit()
    _employee_in(db, tenant, A)
    run(db, tenant, odoo, [], monkeypatch)
    assert _punch_row(db).process_state == PunchState.synced.value


def test_employee_in_another_company_is_held_for_review(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    odoo.companies = COMPANIES
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)
    _terminal(db, tenant, company_id=A, company_name="Acme A")
    _employee_in(db, tenant, B, "Acme B")
    run(db, tenant, odoo, [], monkeypatch)
    row = _punch_row(db)
    assert row.process_state == PunchState.held.value
    assert "company mismatch" in row.error_message
    assert "Acme A" in row.error_message and "Acme B" in row.error_message
    assert not odoo.attendances


def test_a_terminal_own_company_beats_the_connection_default(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    odoo.companies = COMPANIES
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)
    source, _ = _terminal(db, tenant, company_id=B, company_name="Acme B")
    source.config = {**(source.config or {}), "company_id": A, "company_name": "Acme A"}
    db.commit()
    _employee_in(db, tenant, B, "Acme B")
    run(db, tenant, odoo, [], monkeypatch)
    assert _punch_row(db).process_state == PunchState.synced.value


# --------------------------------------------------------------------------- #
# The API that sets it
# --------------------------------------------------------------------------- #
from tests.test_device_delete import client, seed, signup  # noqa: F401


def test_company_can_be_set_and_cleared_on_a_terminal(client):
    headers = signup(client)
    gate1, _gate2, punch_id = seed(client)

    r = client.patch(f"/api/v1/devices/{gate1}", headers=headers,
                     json={"company_id": A, "company_name": "Acme A"})
    assert r.status_code == 200, r.text
    assert (r.json()["company_id"], r.json()["company_name"]) == (A, "Acme A")

    r = client.patch(f"/api/v1/devices/{gate1}", headers=headers, json={"company_id": None})
    assert r.json()["company_id"] is None and r.json()["company_name"] is None


def test_connection_default_company_round_trips(client):
    headers = signup(client)
    seed(client)
    source = client.get("/api/v1/sources", headers=headers).json()[0]

    r = client.put(f"/api/v1/sources/{source['id']}/default-company", headers=headers,
                   json={"company_id": B, "company_name": "Acme B"})
    assert r.status_code == 200, r.text
    assert (r.json()["default_company_id"], r.json()["default_company_name"]) == (B, "Acme B")

    r = client.put(f"/api/v1/sources/{source['id']}/default-company", headers=headers,
                   json={"company_id": None})
    assert r.json()["default_company_id"] is None


def test_held_punches_are_counted_on_the_terminal(client):
    headers = signup(client)
    gate1, _g2, punch_id = seed(client)
    db = client.Session()
    p = db.get(PunchRecord, punch_id)
    p.process_state = PunchState.held.value
    p.error_message = "Held: company not set for terminal Main."
    db.commit(); db.close()
    rows = {d["id"]: d for d in client.get("/api/v1/devices", headers=headers).json()}
    assert rows[gate1]["held_punch_count"] == 1
