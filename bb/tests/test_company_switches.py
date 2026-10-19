"""Per-company switches on the Odoo connection.

Every company the Odoo user can see is on unless it was switched off; the
companies on are the ones BioBridge reads employees from and writes
attendance to. Three layers: the client's scope (the allowed_company_ids
context and the domain), the API that stores the switches, and the sync
engine setting aside — and bringing back — people whose company flips.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.integrations.odoo import OdooClient, OdooCredentials, OdooError
from app.models import EmployeeMapping, MappingStatus, PunchRecord, PunchState
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo, FakeProvider
from tests.test_odoo_company_scoping import (
    COMPANY_A, COMPANY_B, FakeXmlRpcCommon, FakeXmlRpcModels,
)
from tests.test_sync_engine import punch, run

COMPANY_C = 30


def client_with(fake, *, disabled=(), company_id=None) -> OdooClient:
    creds = OdooCredentials(
        url="https://acme.odoo.com", db="acme", username="bot", api_key="k", uid=7,
        company_id=company_id, disabled_company_ids=list(disabled),
    )
    client = OdooClient(creds)
    client._models = fake
    client._common = FakeXmlRpcCommon()
    return client


def three_companies():
    fake = FakeXmlRpcModels()
    fake.companies.append({"id": COMPANY_C, "name": "Acme C"})
    return fake


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #
def test_everything_is_on_by_default():
    client = client_with(three_companies())
    assert client.company_scope() is None
    client.execute("hr.employee", "search_read", [[]], {"fields": ["id"]})
    assert "allowed_company_ids" not in client._models.calls[-1][2].get("context", {})


def test_switching_one_off_leaves_the_others_on():
    fake = three_companies()
    client = client_with(fake, disabled=[COMPANY_B])
    assert client.company_scope() == [COMPANY_A, COMPANY_C]
    client.execute("hr.employee", "search_read", [[]], {"fields": ["id"]})
    assert fake.calls[-1][2]["context"]["allowed_company_ids"] == [COMPANY_A, COMPANY_C]


def test_employees_of_every_enabled_company_are_listed_and_the_disabled_one_is_not():
    fake = three_companies()
    for i, company in enumerate((COMPANY_A, COMPANY_B, COMPANY_C), start=1):
        fake.hr_employee[i] = {"id": i, "name": f"E{i}", "active": True, "company_id": company, "barcode": str(i)}
    client = client_with(fake, disabled=[COMPANY_B])
    assert {e["id"] for e in client.list_employees()} == {1, 3}


def test_a_badge_in_a_disabled_company_is_not_found():
    fake = three_companies()
    fake.hr_employee[1] = {"id": 1, "name": "E1", "active": True, "company_id": COMPANY_B, "barcode": "77"}
    client = client_with(fake, disabled=[COMPANY_B])
    assert client.find_employee("77") == (None, None, None)
    assert client.find_employee("77", scoped=False)[0] == 1


def test_a_company_created_in_odoo_later_starts_on():
    fake = three_companies()
    disabled = [COMPANY_B]
    assert client_with(fake, disabled=disabled).company_scope() == [COMPANY_A, COMPANY_C]
    fake.companies.append({"id": 40, "name": "Acme D"})
    assert client_with(fake, disabled=disabled).company_scope() == [COMPANY_A, COMPANY_C, 40]


def test_switching_every_company_off_is_an_error_not_everything():
    fake = FakeXmlRpcModels()
    client = client_with(fake, disabled=[COMPANY_A, COMPANY_B])
    with pytest.raises(OdooError, match="switched off"):
        client.company_scope()


def test_new_employees_land_in_the_first_enabled_company():
    fake = three_companies()
    client = client_with(fake, disabled=[COMPANY_A])
    new_id = client.create_employee("Sam", "555")
    assert fake.hr_employee[new_id]["company_id"] == COMPANY_B


def test_a_device_found_in_one_enabled_company_is_not_moved_to_another():
    fake = three_companies()
    client = client_with(fake, disabled=[COMPANY_A])  # B and C on
    dev = client.upsert_device("GATE-1", name="Gate")
    fake.x_biobridge_device[dev]["x_company_id"] = COMPANY_C
    assert client.upsert_device("GATE-1", location="Lobby") == dev
    assert fake.x_biobridge_device[dev]["x_company_id"] == COMPANY_C


def test_the_older_single_company_pin_still_means_that_company_only():
    fake = three_companies()
    assert client_with(fake, company_id=COMPANY_B).company_scope() == [COMPANY_B]


# --------------------------------------------------------------------------- #
# The sync engine
# --------------------------------------------------------------------------- #
def mapping_for(db, tenant, code):
    return db.scalar(select(EmployeeMapping).where(
        EmployeeMapping.tenant_id == tenant.id, EmployeeMapping.emp_code == code))


def test_an_employee_whose_company_is_switched_off_is_set_aside_then_restored(
    db, tenant, local_day, monkeypatch
):
    odoo = FakeOdoo()
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)
    assert mapping_for(db, tenant, "1001").status == MappingStatus.mapped.value

    # Their company is switched off: the roster no longer contains them.
    odoo.scope = [1]
    odoo.roster = [{"id": 99, "name": "Someone else"}]
    run(db, tenant, odoo, [punch(2, "1001", local_day.replace(hour=17))], monkeypatch)
    held = mapping_for(db, tenant, "1001")
    assert held.status == MappingStatus.out_of_scope.value
    assert held.odoo_employee_id == 11, "the link is kept so switching back on is instant"
    late = db.scalar(select(PunchRecord).where(PunchRecord.external_id == "2"))
    assert late.process_state == PunchState.unmapped.value, "waits; nothing is written to Odoo"

    # Switched back on.
    odoo.scope = None
    odoo.roster = []
    run(db, tenant, odoo, [], monkeypatch)
    assert mapping_for(db, tenant, "1001").status == MappingStatus.mapped.value
    assert db.scalar(select(PunchRecord).where(PunchRecord.external_id == "2")).process_state \
        == PunchState.synced.value


def test_a_new_badge_from_a_disabled_company_is_never_auto_created_elsewhere(
    db, tenant, local_day, monkeypatch
):
    tenant.auto_create_employees = True
    db.flush()
    odoo = FakeOdoo(employees={})
    odoo.scope = [1]
    odoo.out_of_scope_employees = {"2001": (55, "Far Away")}
    run(db, tenant, odoo, [punch(1, "2001", local_day.replace(hour=8))], monkeypatch)
    m = mapping_for(db, tenant, "2001")
    assert m.status == MappingStatus.out_of_scope.value
    assert m.odoo_employee_id == 55
    assert not odoo.attendances

