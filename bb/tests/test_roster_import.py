"""Device users already matched to Odoo employees are imported before any punch."""

from __future__ import annotations

from sqlalchemy import select

from app.integrations.base import EmployeeRecord
from app.models import EmployeeMapping, MappingStatus
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo, FakeProvider


def _run(db, tenant, odoo, users, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(
        engine_mod, "build_source_provider", lambda t, s: FakeProvider([], employees=users)
    )
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


def _maps(db):
    return {m.emp_code: m for m in db.scalars(select(EmployeeMapping)).all()}


def test_matched_device_users_are_imported_without_a_punch(db, tenant, monkeypatch):
    odoo = FakeOdoo()
    odoo.roster = [
        {"id": 11, "name": "Jane Doe", "active": True, "barcode": "1001"},
        {"id": 12, "name": "Omar H", "active": True, "pin": "1002"},
    ]
    users = [
        EmployeeRecord(None, "1001", "Jane", "Doe"),
        EmployeeRecord(None, "1002", "Omar", "H"),
        EmployeeRecord(None, "7777", "Nobody", ""),
    ]
    run = _run(db, tenant, odoo, users, monkeypatch)
    maps = _maps(db)
    assert maps["1001"].status == MappingStatus.mapped.value
    assert maps["1001"].odoo_employee_id == 11
    assert maps["1002"].odoo_employee_id == 12 and maps["1002"].match_method == "pin"
    assert "7777" not in maps, "a device user with no Odoo match is not imported"
    assert run.employees_matched == 2


def test_ambiguous_and_inactive_are_not_imported(db, tenant, monkeypatch):
    odoo = FakeOdoo()
    odoo.roster = [
        {"id": 1, "name": "A", "active": True, "barcode": "5"},
        {"id": 2, "name": "B", "active": True, "barcode": "5"},
        {"id": 3, "name": "C", "active": False, "barcode": "6"},
    ]
    _run(db, tenant, odoo, [EmployeeRecord(None, "5"), EmployeeRecord(None, "6")], monkeypatch)
    assert _maps(db) == {}


def test_existing_mapped_rows_are_left_alone(db, tenant, monkeypatch):
    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="1001", status="mapped",
                           odoo_employee_id=99, odoo_employee_name="Hand mapped"))
    db.commit()
    odoo = FakeOdoo()
    odoo.roster = [{"id": 11, "name": "Jane", "active": True, "barcode": "1001"}]
    _run(db, tenant, odoo, [EmployeeRecord(None, "1001")], monkeypatch)
    assert _maps(db)["1001"].odoo_employee_id == 99


def test_auto_import_runs_without_a_sync_once_both_ends_exist(db, tenant, monkeypatch):
    from app.services import roster_import

    odoo = FakeOdoo()
    odoo.roster = [{"id": 11, "name": "Jane", "active": True, "barcode": "1001"}]
    monkeypatch.setattr(roster_import, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(
        roster_import, "build_source_provider",
        lambda t, s: FakeProvider([], employees=[EmployeeRecord(None, "1001", "Jane", "")]),
    )
    assert roster_import.auto_import(db, tenant) == 1
    assert _maps(db)["1001"].status == MappingStatus.mapped.value


def test_auto_import_is_silent_when_a_side_is_missing(db, tenant):
    from app.services import roster_import

    assert roster_import.auto_import(db, tenant) == 0


def test_mapped_employees_get_their_odoo_company(db, tenant):
    from app.services.roster_import import refresh_companies

    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="1", status="mapped", odoo_employee_id=11))
    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="2", status="mapped", odoo_employee_id=12))
    db.commit()
    odoo = FakeOdoo()
    odoo.roster = [
        {"id": 11, "name": "A", "active": True, "company_id": [1, "Acme UAE"]},
        {"id": 12, "name": "B", "active": True, "company_id": [2, "Acme KSA"]},
    ]
    assert refresh_companies(db, tenant, odoo) == 2
    maps = _maps(db)
    assert (maps["1"].odoo_company_id, maps["1"].odoo_company_name) == (1, "Acme UAE")
    assert maps["2"].odoo_company_name == "Acme KSA"
    assert refresh_companies(db, tenant, odoo) == 0, "nothing left to fill, no second roster read"


def test_company_is_read_from_every_many2one_spelling():
    from app.services.roster_import import _company_of

    assert _company_of({"company_id": [3, "A"]}) == (3, "A")
    assert _company_of({"company_id": {"id": 3, "display_name": "A"}}) == (3, "A")
    assert _company_of({"company_id": 3}, {3: "A"}) == (3, "A")
    assert _company_of({"company_id": False}) == (None, None)


def test_employees_of_a_switched_off_company_are_not_listed(db, tenant):
    from sqlalchemy import select as _select

    from app.api.v1.sync import list_mappings
    from app.models import OdooConnection

    conn = db.scalars(_select(OdooConnection).where(OdooConnection.tenant_id == tenant.id)).first()
    conn.is_active = True
    conn.company_id = None
    conn.disabled_company_ids = [2]
    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="1", status="mapped",
                           odoo_employee_id=11, odoo_company_id=1, odoo_company_name="My Company"))
    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="2", status="mapped",
                           odoo_employee_id=12, odoo_company_id=2, odoo_company_name="My Company 2"))
    db.commit()

    class P:  # the bits of Principal the endpoint reads
        pass

    p = P()
    p.tenant = tenant
    codes = [m.emp_code for m in list_mappings(None, 200, p, db)]
    assert codes == ["1"]


def test_department_and_manager_are_cached_on_mapped_employees(db, tenant):
    from app.services.roster_import import refresh_companies

    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="1", status="mapped", odoo_employee_id=11))
    db.commit()
    odoo = FakeOdoo()
    odoo.roster = [
        {"id": 11, "name": "A", "active": True, "company_id": [1, "Acme"],
         "department_id": [5, "Sales"], "parent_id": [9, "Boss"]},
        {"id": 9, "name": "Boss", "active": True, "company_id": [1, "Acme"]},
    ]
    assert refresh_companies(db, tenant, odoo, force=True) == 1
    m = _maps(db)["1"]
    assert (m.odoo_department_name, m.odoo_manager_id, m.odoo_manager_name) == ("Sales", 9, "Boss")
