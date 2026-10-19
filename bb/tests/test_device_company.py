"""A terminal belongs to an Odoo company; a punch that does not fit waits for review.

Unassigned terminals pass on a single-company Odoo and are held on a
multi-company one; a terminal whose company differs from the employee's is
held on either.
"""

from __future__ import annotations

import pytest

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
from tests.test_device_delete import client, seed, signup  # noqa: E402,F401


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


# --------------------------------------------------------------------------- #
# Working a terminal's company out from its people, and no auto-provisioning
# when several companies are on
# --------------------------------------------------------------------------- #
from app.services.company_scope import infer_device_companies  # noqa: E402


def _mappings_in(db, tenant, *pairs):
    """pairs: (emp_code, company_id, company_name)"""
    for code, cid, cname in pairs:
        m = db.scalar(select(EmployeeMapping).where(
            EmployeeMapping.tenant_id == tenant.id, EmployeeMapping.emp_code == code))
        m.odoo_company_id, m.odoo_company_name = cid, cname
    db.commit()


def test_terminal_takes_the_company_its_matched_people_belong_to(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    odoo.companies = COMPANIES
    rows = [punch(1, "1001", local_day.replace(hour=8)), punch(2, "1002", local_day.replace(hour=9))]
    run(db, tenant, odoo, rows, monkeypatch)        # unassigned -> held
    assert all(p.process_state == PunchState.held.value for p in db.scalars(select(PunchRecord)))
    _terminal(db, tenant)
    _mappings_in(db, tenant, ("1001", A, "Acme A"), ("1002", A, "Acme A"))

    run(db, tenant, odoo, [], monkeypatch)          # the sync works it out and releases them
    device = db.scalar(select(Device))
    assert (device.company_id, device.company_name) == (A, "Acme A")
    assert all(p.process_state == PunchState.synced.value for p in db.scalars(select(PunchRecord)))


def test_terminal_used_by_two_companies_is_left_for_a_person_to_choose(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    odoo.companies = COMPANIES
    rows = [punch(1, "1001", local_day.replace(hour=8)), punch(2, "1002", local_day.replace(hour=9))]
    run(db, tenant, odoo, rows, monkeypatch)
    _terminal(db, tenant)
    _mappings_in(db, tenant, ("1001", A, "Acme A"), ("1002", B, "Acme B"))

    notes = infer_device_companies(db, tenant)
    assert db.scalar(select(Device)).company_id is None
    assert len(notes) == 1 and "several companies" in notes[0]


def test_a_company_set_by_hand_is_never_overwritten(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    odoo.companies = COMPANIES
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)
    _terminal(db, tenant, company_id=B, company_name="Acme B")
    _mappings_in(db, tenant, ("1001", A, "Acme A"))
    assert infer_device_companies(db, tenant) == []
    assert db.scalar(select(Device)).company_id == B


def test_nothing_is_created_on_devices_when_several_companies_are_on(client):
    """Switching auto-provisioning on is refused on a multi-company Odoo."""
    from app.api.v1 import connections as connections_mod

    headers = signup(client)
    seed(client)
    source = client.get("/api/v1/sources", headers=headers).json()[0]

    class Multi:
        def authenticate(self): return 7
        def company_scope(self): return None
        def list_companies(self): return COMPANIES

    db = client.Session()
    from app.models import OdooConnection, Tenant
    tenant = db.scalars(select(Tenant)).first()
    db.add(OdooConnection(tenant_id=tenant.id, url="https://x.odoo.com", db_name="x",
                          username="u", is_active=True))
    db.commit(); db.close()

    original = connections_mod.build_odoo_client
    connections_mod.build_odoo_client = lambda *a: Multi()
    try:
        r = client.patch(f"/api/v1/sources/{source['id']}", headers=headers,
                         json={"auto_provision_employees": True})
    finally:
        connections_mod.build_odoo_client = original
    assert r.status_code == 400 and "more than one company" in r.json()["detail"]


def test_sync_creates_no_device_employees_when_several_companies_are_on(db, tenant, monkeypatch, caplog):
    """The per-source auto-provision flag is ignored; the note goes to the
    server log, not the attendance run log."""
    from tests.test_sync_engine import FakeProvider  # noqa: F401
    from app.services import sync_engine as engine_mod

    source = db.scalars(select(DeviceSource).where(DeviceSource.tenant_id == tenant.id)).first()
    source.auto_provision_employees = True
    db.commit()
    odoo = FakeOdoo()
    odoo.companies = COMPANIES
    odoo.roster = [{"id": 1, "name": "Jane", "active": True, "barcode": "1001"}]

    engine = engine_mod.SyncEngine(db, tenant, "test")
    engine._provision_employees(odoo, [source])
    assert "more than one company" in caplog.text
    assert not any("more than one company" in line for line in engine._log_lines)
    assert engine.run.employees_provisioned in (0, None)


def test_terminal_company_is_sent_to_odoo_device_upsert(client_factory=None):
    """The device registered in Odoo carries the terminal's company."""
    from app.services.sync_engine import SyncEngine

    class D:  # minimal stand-in: own company wins
        company_id = 10
        source_id = None

    assert SyncEngine._terminal_company_id(SyncEngine.__new__(SyncEngine), D()) == 10


def test_terminal_with_only_unmapped_employees_is_not_registered_in_odoo(
    db, tenant, local_day, monkeypatch
):
    """Held back until at least one of its employees is mapped."""
    from app.services.device_links import terminal_may_register_in_odoo

    odoo = FakeOdoo()
    odoo.roster = []  # nobody in Odoo, so the badge stays unmapped
    run(db, tenant, odoo, [punch(1, "9999", local_day.replace(hour=8))], monkeypatch)
    source = db.scalars(select(DeviceSource).where(DeviceSource.tenant_id == tenant.id)).first()
    device = db.scalar(select(Device).where(Device.source_id == source.id))
    assert device is not None
    assert terminal_may_register_in_odoo(db, device) is False
    assert "upsert_device:GATE-01" not in odoo.calls

    mapping = db.scalar(select(EmployeeMapping).where(
        EmployeeMapping.tenant_id == tenant.id, EmployeeMapping.emp_code == "9999"))
    mapping.status = "mapped"
    db.flush()
    assert terminal_may_register_in_odoo(db, device) is True
