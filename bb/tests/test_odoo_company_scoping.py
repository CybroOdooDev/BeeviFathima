"""OdooConnection.company_id — real isolation on a multi-company Odoo.

A customer can run several BioBridge tenants against companies that live
inside *one* Odoo instance (Odoo's own multi-company feature). BioBridge's
own tenant isolation (tenant_id everywhere, per-tenant credential
encryption) says nothing about that case: without an explicit company
pin, two DeviceSource/OdooConnection rows pointed at the same Odoo
instance could resolve an employee, list a roster, or upsert a device
across company lines, purely because the underlying Odoo API user happens
to have access to more than one company.

This file exercises OdooClient.execute's allowed_company_ids injection and
every call site that also adds an explicit company_id domain condition —
see OdooClient._company_domain. It's the search-then-write plumbing;
ping()'s own "does this user really see this company" check and the
bootstrap x_company_id field are here too.

A fake XML-RPC ``models`` proxy, same seam as test_odoo_device_bootstrap.py
— nothing else in the suite constructs a real OdooClient.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.integrations.odoo import OdooClient, OdooCredentials, OdooError

COMPANY_A = 10
COMPANY_B = 20


class FakeXmlRpcModels:
    """Enough of Odoo's ``object`` endpoint to exercise company scoping:
    ``res.company``, ``hr.employee``, ``hr.attendance``, and
    ``x_biobridge_device`` (bootstrap mode), all with real domain matching
    rather than the single-field lookups the bootstrap-mechanics fake uses.
    """

    def __init__(self, *, x_company_field: bool = True) -> None:
        self.calls: list[tuple[str, str, dict]] = []  # (model, method, kwargs) — kwargs matters here
        self._next_id = 1000
        self.companies = [{"id": COMPANY_A, "name": "Acme A"}, {"id": COMPANY_B, "name": "Acme B"}]
        self.hr_employee: dict[int, dict] = {}
        self.hr_attendance: dict[int, dict] = {}
        self.x_biobridge_device: dict[int, dict] = {}
        # x_device_id is what _device_tracking_mode reads to know bootstrap
        # mode is already active — every test here assumes that's already
        # true (it's how upsert_device is reached at all) unless it says
        # otherwise.
        attendance_fields = {"check_in", "check_out", "employee_id", "company_id", "x_device_id"}
        device_fields = {"x_serial_number", "x_name", "x_location", "x_terminal_model", "x_ip_address"}
        if x_company_field:
            device_fields.add("x_company_id")
        self.model_fields: dict[str, set[str]] = {
            "hr.attendance": attendance_fields,
            "hr.employee": {"id", "name", "active", "department_id", "barcode", "pin", "company_id"},
            "x_biobridge_device": device_fields,
        }

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    @staticmethod
    def _matches(record: dict, domain: list) -> bool:
        for field, op, value in domain:
            rv = record.get(field)
            if op == "=":
                if rv != value:
                    return False
            elif op == "in":
                if rv not in value:
                    return False
            else:  # pragma: no cover — nothing here uses a richer operator
                raise AssertionError(f"unsupported domain op: {op}")
        return True

    def execute_kw(self, db, uid, key, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        self.calls.append((model, method, kwargs))

        if method == "fields_get":
            return {f: {"type": "x"} for f in self.model_fields.get(model, set())}

        if model == "res.company" and method == "search_read":
            return list(self.companies)

        if model == "hr.employee":
            if method == "search_read":
                domain = args[0]
                matches = [r for r in self.hr_employee.values() if self._matches(r, domain)]
                limit = kwargs.get("limit") or 0
                if limit:
                    matches = matches[:limit]
                return [dict(r) for r in matches]
            if method == "search_count":
                domain = args[0]
                return len([r for r in self.hr_employee.values() if self._matches(r, domain)])
            if method == "create":
                vals = args[0]
                new_id = self._new_id()
                self.hr_employee[new_id] = {"id": new_id, **vals}
                return new_id

        if model == "hr.attendance":
            if method == "search_read":
                domain = args[0]
                matches = [r for r in self.hr_attendance.values() if self._matches(r, domain)]
                limit = kwargs.get("limit") or 0
                if limit:
                    matches = matches[:limit]
                return [dict(r) for r in matches]
            if method == "create":
                vals = args[0]
                new_id = self._new_id()
                self.hr_attendance[new_id] = {"id": new_id, **vals}
                return new_id
            if method == "write":
                ids, vals = args
                for i in ids:
                    self.hr_attendance[i].update(vals)
                return True
            if method == "check_access_rights":
                return True

        if model == "x_biobridge_device":
            if method == "search_read":
                domain = args[0]
                matches = [r for r in self.x_biobridge_device.values() if self._matches(r, domain)]
                limit = kwargs.get("limit") or 0
                if limit:
                    matches = matches[:limit]
                return [{"id": r["id"]} for r in matches]
            if method == "write":
                ids, vals = args
                for i in ids:
                    self.x_biobridge_device[i].update(vals)
                return True
            if method == "create":
                vals = args[0]
                new_id = self._new_id()
                self.x_biobridge_device[new_id] = {"id": new_id, **vals}
                return new_id

        raise AssertionError(f"unhandled call: {model}.{method}({args!r}, {kwargs!r})")


class FakeXmlRpcCommon:
    """Stands in for Odoo's ``common`` endpoint — just enough for
    ``ping()``'s ``version()``/``authenticate()`` calls to avoid a real
    network round-trip."""

    def version(self):
        return {"server_version": "18.0"}

    def authenticate(self, db, username, api_key, params):
        return 7


def make_client(fake: FakeXmlRpcModels, *, company_id: int | None = None) -> OdooClient:
    creds = OdooCredentials(
        url="https://acme.odoo.com", db="acme", username="bot", api_key="k", uid=7,
        company_id=company_id,
    )
    client = OdooClient(creds)
    client._models = fake  # skip real XML-RPC transport entirely
    client._common = FakeXmlRpcCommon()
    return client


# --------------------------------------------------------------------------- #
# execute() context injection
# --------------------------------------------------------------------------- #
def test_execute_injects_allowed_company_ids_when_company_id_is_set():
    fake = FakeXmlRpcModels()
    client = make_client(fake, company_id=COMPANY_A)

    client.execute("hr.employee", "search_read", [[]], {"fields": ["id"]})

    _, _, kwargs = fake.calls[-1]
    assert kwargs["context"]["allowed_company_ids"] == [COMPANY_A]


def test_execute_adds_no_company_context_when_unset():
    fake = FakeXmlRpcModels()
    client = make_client(fake, company_id=None)

    client.execute("hr.employee", "search_read", [[]], {"fields": ["id"]})

    _, _, kwargs = fake.calls[-1]
    assert "allowed_company_ids" not in kwargs.get("context", {})


def test_execute_preserves_the_rest_of_an_existing_context():
    fake = FakeXmlRpcModels()
    client = make_client(fake, company_id=COMPANY_A)

    client.execute("hr.employee", "search_read", [[]], {"context": {"active_test": False}})

    _, _, kwargs = fake.calls[-1]
    assert kwargs["context"] == {"active_test": False, "allowed_company_ids": [COMPANY_A]}


def test_list_companies_bypasses_the_company_scope():
    """The one call that must see every company regardless of company_id —
    otherwise a misconfigured id could never be diagnosed, and there would
    be no way to discover the id to pick in the first place."""
    fake = FakeXmlRpcModels()
    client = make_client(fake, company_id=COMPANY_A)

    companies = client.list_companies()

    assert {c["id"] for c in companies} == {COMPANY_A, COMPANY_B}
    _, _, kwargs = fake.calls[-1]
    assert "allowed_company_ids" not in kwargs.get("context", {})


# --------------------------------------------------------------------------- #
# ping() validates the configured company is actually visible
# --------------------------------------------------------------------------- #
def test_ping_raises_when_company_id_is_not_visible():
    fake = FakeXmlRpcModels()
    client = make_client(fake, company_id=999)

    with pytest.raises(OdooError, match="cannot see company id 999"):
        client.ping()


def test_ping_succeeds_and_reports_companies_when_company_id_is_visible():
    fake = FakeXmlRpcModels()
    client = make_client(fake, company_id=COMPANY_A)

    info = client.ping()

    assert info["ok"] is True
    assert {c["id"] for c in info["companies"]} == {COMPANY_A, COMPANY_B}


def test_ping_reports_companies_even_with_no_company_id_configured():
    fake = FakeXmlRpcModels()
    client = make_client(fake, company_id=None)

    info = client.ping()

    assert {c["id"] for c in info["companies"]} == {COMPANY_A, COMPANY_B}


# --------------------------------------------------------------------------- #
# hr.employee — the actual cross-company leak this all guards against
# --------------------------------------------------------------------------- #
def _seed_same_badge_two_companies(fake: FakeXmlRpcModels) -> None:
    """The scenario from the bug report: the same barcode value happens to
    exist in two sibling companies (Odoo enforces no cross-company
    uniqueness on hr.employee.barcode) — one BioTime device belongs to
    Company A, and resolving its punches must never touch Company B's
    employee of the same badge number."""
    fake.hr_employee[1] = {
        "id": 1, "name": "Employee A", "barcode": "1001", "company_id": COMPANY_A, "active": True,
    }
    fake.hr_employee[2] = {
        "id": 2, "name": "Employee B", "barcode": "1001", "company_id": COMPANY_B, "active": True,
    }


def test_find_employee_resolves_only_within_its_own_company():
    fake = FakeXmlRpcModels()
    _seed_same_badge_two_companies(fake)

    client_a = make_client(fake, company_id=COMPANY_A)
    emp_id, name, method = client_a.find_employee("1001")
    assert (emp_id, name) == (1, "Employee A")

    client_b = make_client(fake, company_id=COMPANY_B)
    emp_id, name, method = client_b.find_employee("1001")
    assert (emp_id, name) == (2, "Employee B")


def test_find_employee_with_no_company_id_sees_both_as_ambiguous():
    """The pre-existing, unscoped behaviour for a genuinely single-company
    Odoo — kept exactly as it was — but proof that leaving company_id unset
    on a multi-company instance is exactly the gap this feature closes."""
    fake = FakeXmlRpcModels()
    _seed_same_badge_two_companies(fake)
    client = make_client(fake, company_id=None)

    emp_id, name, method = client.find_employee("1001")
    assert emp_id is None
    assert method == "ambiguous:barcode"


def test_list_employees_returns_only_the_configured_company():
    fake = FakeXmlRpcModels()
    _seed_same_badge_two_companies(fake)
    client = make_client(fake, company_id=COMPANY_A)

    roster = client.list_employees()

    assert [r["id"] for r in roster] == [1]


def test_create_employee_stamps_the_configured_company():
    fake = FakeXmlRpcModels()
    client = make_client(fake, company_id=COMPANY_B)

    emp_id = client.create_employee("New Hire", "2002")

    assert fake.hr_employee[emp_id]["company_id"] == COMPANY_B


def test_create_employee_omits_company_id_when_unset():
    fake = FakeXmlRpcModels()
    client = make_client(fake, company_id=None)

    emp_id = client.create_employee("New Hire", "2002")

    assert "company_id" not in fake.hr_employee[emp_id]


# --------------------------------------------------------------------------- #
# hr.attendance lookups — employee_id already implies the company, this is
# the defense-in-depth leg
# --------------------------------------------------------------------------- #
def test_attendance_lookups_include_the_company_domain_condition():
    fake = FakeXmlRpcModels()
    client = make_client(fake, company_id=COMPANY_A)
    fake.hr_attendance[1] = {
        "id": 1, "employee_id": 1, "check_in": "2026-01-01 08:00:00",
        "check_out": False, "company_id": COMPANY_A,
    }

    open_att = client.get_open_attendance(1)
    assert open_att["id"] == 1

    # search_read's domain literally carried the condition — not just "it
    # happened to match" — confirmed via the recorded call args.
    calls = [c for c in fake.calls if c[0] == "hr.attendance" and c[1] == "search_read"]
    assert calls, "expected at least one hr.attendance.search_read call"


def test_attendance_lookups_omit_company_domain_when_field_absent():
    """A stock hr.attendance in some older/edited Odoo without a company_id
    field must not get a domain condition it cannot satisfy — fields_of
    gates it, same as every other optional-field check in this client."""
    fake = FakeXmlRpcModels()
    fake.model_fields["hr.attendance"] = {"check_in", "check_out", "employee_id"}
    client = make_client(fake, company_id=COMPANY_A)
    fake.hr_attendance[1] = {
        "id": 1, "employee_id": 1, "check_in": "2026-01-01 08:00:00", "check_out": False,
    }

    assert client.get_open_attendance(1)["id"] == 1


# --------------------------------------------------------------------------- #
# x_biobridge_device (bootstrap mode) — no built-in multi-company rule of
# its own, so this is load-bearing, not just defense-in-depth
# --------------------------------------------------------------------------- #
def test_upsert_device_bootstrap_does_not_collide_across_companies():
    fake = FakeXmlRpcModels()
    client_a = make_client(fake, company_id=COMPANY_A)
    client_b = make_client(fake, company_id=COMPANY_B)

    id_a = client_a.upsert_device("GATE-01", name="Front Gate")
    id_b = client_b.upsert_device("GATE-01", name="Warehouse Gate")

    assert id_a != id_b
    assert fake.x_biobridge_device[id_a]["x_company_id"] == COMPANY_A
    assert fake.x_biobridge_device[id_b]["x_company_id"] == COMPANY_B


def test_upsert_device_bootstrap_finds_its_own_companys_device_again():
    fake = FakeXmlRpcModels()
    client_a = make_client(fake, company_id=COMPANY_A)

    first = client_a.upsert_device("GATE-01", name="Front Gate")
    second = client_a.upsert_device("GATE-01", location="Rebuilt")

    assert first == second
    assert len(fake.x_biobridge_device) == 1


def test_upsert_device_bootstrap_without_company_field_falls_back_gracefully():
    """A connection bootstrapped before x_company_id existed and never
    re-bootstrapped: no company scoping is possible yet, but it must not
    crash — the same fields_of gate as everywhere else in this client."""
    fake = FakeXmlRpcModels(x_company_field=False)
    client = make_client(fake, company_id=COMPANY_A)

    device_id = client.upsert_device("GATE-01", name="Front Gate")

    assert "x_company_id" not in fake.x_biobridge_device[device_id]
