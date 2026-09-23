"""OdooClient's two device-tracking paths: the installed add-on ("module")
and the no-install, external-API-only path that actually reaches Odoo
Online ("bootstrap") — see OdooClient.ensure_device_tracking_bootstrap.

Exercised directly against OdooClient with a fake XML-RPC ``models`` proxy,
since nothing else in the suite constructs a real OdooClient (everywhere
else stubs the whole client at the SyncEngine seam — see tests/conftest.py's
FakeOdoo). This is the one place the ir.model / ir.model.fields /
ir.model.access mechanics themselves are checked.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.integrations.odoo import OdooAuthError, OdooClient, OdooCredentials, OdooError


class FakeXmlRpcModels:
    """A tiny in-memory stand-in for Odoo's ``object`` XML-RPC endpoint.

    Only implements the handful of (model, method) pairs OdooClient's
    device-tracking code actually calls — anything else raises, on purpose,
    so a call this test doesn't expect shows up as a clear failure rather
    than a silently wrong return value.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._next_id = 100
        self.model_name_to_id: dict[str, int] = {"hr.attendance": 1}
        self.model_id_to_name: dict[int, str] = {1: "hr.attendance"}
        self.model_fields: dict[str, set[str]] = {
            "hr.attendance": {"check_in", "check_out", "employee_id"}
        }
        self.ir_model_fields_rows: list[tuple[int, str]] = []
        self.ir_model_access_rows: list[tuple[int, str]] = []
        self.ir_rule_rows: list[tuple[int, str, str]] = []  # (model_id, name, domain_force)
        self.xmlids: dict[tuple[str, str], int] = {
            ("base", "group_user"): 501,
            ("hr_attendance", "group_hr_attendance_manager"): 502,
        }
        self.x_biobridge_device_records: dict[int, dict] = {}
        self.biobridge_device_records: dict[int, dict] = {}
        self.hr_attendance_records: dict[int, dict] = {}
        self.settings_access = True

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    @staticmethod
    def _domain_value(domain, field):
        return next(v for f, _op, v in domain if f == field)

    def execute_kw(self, db, uid, key, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        self.calls.append(f"{model}.{method}")

        if method == "fields_get":
            return {f: {"type": "x"} for f in self.model_fields.get(model, set())}
        if method == "check_access_rights":
            return self.settings_access

        if model == "ir.model":
            if method == "search":
                name = self._domain_value(args[0], "model")
                return [self.model_name_to_id[name]] if name in self.model_name_to_id else []
            if method == "create":
                vals = args[0]
                new_id = self._new_id()
                self.model_name_to_id[vals["model"]] = new_id
                self.model_id_to_name[new_id] = vals["model"]
                self.model_fields.setdefault(vals["model"], set())
                return new_id

        if model == "ir.model.fields":
            if method == "search":
                domain = args[0]
                key_ = (self._domain_value(domain, "model_id"), self._domain_value(domain, "name"))
                return [1] if key_ in self.ir_model_fields_rows else []
            if method == "create":
                vals = args[0]
                self.ir_model_fields_rows.append((vals["model_id"], vals["name"]))
                model_name = self.model_id_to_name[vals["model_id"]]
                self.model_fields.setdefault(model_name, set()).add(vals["name"])
                return self._new_id()

        if model == "ir.model.access":
            if method == "search":
                domain = args[0]
                key_ = (self._domain_value(domain, "model_id"), self._domain_value(domain, "name"))
                return [1] if key_ in self.ir_model_access_rows else []
            if method == "create":
                vals = args[0]
                self.ir_model_access_rows.append((vals["model_id"], vals["name"]))
                return self._new_id()

        if model == "ir.rule":
            if method == "search":
                domain = args[0]
                key_ = (self._domain_value(domain, "model_id"), self._domain_value(domain, "name"))
                return [1] if any((mid, n) == key_ for mid, n, _df in self.ir_rule_rows) else []
            if method == "create":
                vals = args[0]
                self.ir_rule_rows.append((vals["model_id"], vals["name"], vals["domain_force"]))
                return self._new_id()

        if model == "ir.model.data" and method == "search_read":
            domain = args[0]
            res_id = self.xmlids.get(
                (self._domain_value(domain, "module"), self._domain_value(domain, "name"))
            )
            return [{"res_id": res_id}] if res_id else []

        if model == "x_biobridge_device":
            if method == "search_read":
                sn = self._domain_value(args[0], "x_serial_number")
                matches = [
                    r for r in self.x_biobridge_device_records.values()
                    if r["x_serial_number"] == sn
                ]
                return [{"id": r["id"]} for r in matches[:1]]
            if method == "write":
                ids, vals = args
                for i in ids:
                    self.x_biobridge_device_records[i].update(vals)
                return True
            if method == "create":
                vals = args[0]
                new_id = self._new_id()
                self.x_biobridge_device_records[new_id] = {"id": new_id, **vals}
                return new_id

        if model == "biobridge.device" and method == "_biobridge_upsert":
            serial_number, vals = args
            for r in self.biobridge_device_records.values():
                if r["serial_number"] == serial_number:
                    r.update({k: v for k, v in vals.items() if v})
                    return r["id"]
            new_id = self._new_id()
            merged = dict(vals)
            merged.setdefault("name", serial_number)
            merged["serial_number"] = serial_number
            self.biobridge_device_records[new_id] = {"id": new_id, **merged}
            return new_id

        if model == "hr.attendance" and method == "create":
            vals = args[0]
            new_id = self._new_id()
            self.hr_attendance_records[new_id] = {"id": new_id, **vals}
            return new_id

        raise AssertionError(f"unhandled call: {model}.{method}({args!r}, {kwargs!r})")


def make_client(fake: FakeXmlRpcModels) -> OdooClient:
    creds = OdooCredentials(url="https://acme.odoo.com", db="acme", username="bot", api_key="k", uid=7)
    client = OdooClient(creds)
    client._models = fake  # skip real XML-RPC transport entirely
    return client


# --------------------------------------------------------------------------- #
# Mode detection
# --------------------------------------------------------------------------- #
def test_no_device_tracking_by_default():
    client = make_client(FakeXmlRpcModels())
    assert client._device_tracking_mode() is None


def test_module_mode_detected_from_device_id_field():
    fake = FakeXmlRpcModels()
    fake.model_fields["hr.attendance"].add("device_id")
    client = make_client(fake)
    assert client._device_tracking_mode() == "module"


def test_bootstrap_mode_detected_from_x_device_id_field():
    fake = FakeXmlRpcModels()
    fake.model_fields["hr.attendance"].add("x_device_id")
    client = make_client(fake)
    assert client._device_tracking_mode() == "bootstrap"


# --------------------------------------------------------------------------- #
# The bootstrap itself
# --------------------------------------------------------------------------- #
def test_bootstrap_creates_model_fields_and_access():
    fake = FakeXmlRpcModels()
    client = make_client(fake)

    client.ensure_device_tracking_bootstrap()

    assert "x_biobridge_device" in fake.model_name_to_id
    device_model_id = fake.model_name_to_id["x_biobridge_device"]
    created_device_fields = {
        name for mid, name in fake.ir_model_fields_rows if mid == device_model_id
    }
    assert created_device_fields == {
        "x_name", "x_serial_number", "x_location", "x_terminal_model", "x_ip_address",
        "x_company_id",
    }

    attendance_id = fake.model_name_to_id["hr.attendance"]
    attendance_fields = {
        name for mid, name in fake.ir_model_fields_rows if mid == attendance_id
    }
    assert {"x_device_id", "x_device_location"} <= attendance_fields

    # Access granted to base.group_user (501), not left wide open with no
    # group at all, and not left ungranted either.
    assert (device_model_id, "x_biobridge_device.biobridge") in fake.ir_model_access_rows

    # A global (no groups_id) rule scoping rows by x_company_id — the part
    # that actually filters what's visible in Odoo's own UI when a company
    # is selected, distinct from ir.model.access's read/write grant above.
    rule = next(r for r in fake.ir_rule_rows if r[0] == device_model_id)
    assert rule[1] == "x_biobridge_device.biobridge_company"
    assert rule[2] == "[('x_company_id', 'in', company_ids)]"

    assert client._device_tracking_mode() == "bootstrap"


def test_bootstrap_is_idempotent():
    fake = FakeXmlRpcModels()
    client = make_client(fake)

    client.ensure_device_tracking_bootstrap()
    calls_after_first = list(fake.calls)
    client.ensure_device_tracking_bootstrap()

    assert fake.calls.count("ir.model.create") == 1, "must not create the model twice"
    assert fake.calls.count("ir.model.fields.create") == len(
        [c for c in calls_after_first if c == "ir.model.fields.create"]
    ), "must not create any field twice"
    assert fake.calls.count("ir.rule.create") == 1, "must not create the rule twice"


def test_bootstrap_backfills_x_company_id_for_a_connection_bootstrapped_before_it_existed():
    """The customer already has x_biobridge_device without x_company_id —
    the state anyone who bootstrapped before company scoping shipped is
    in. Re-running bootstrap (the same button, "Set up device tracking")
    must add the missing field rather than treating bootstrap mode as
    already-done-nothing-more-to-do."""
    fake = FakeXmlRpcModels()
    client = make_client(fake)
    client.ensure_device_tracking_bootstrap()
    # Simulate the pre-company-scoping world: drop the field this second
    # client would otherwise already see, so _device_tracking_mode still
    # reads "bootstrap" (that's keyed off hr.attendance.x_device_id, not
    # x_biobridge_device) while x_company_id is genuinely missing.
    device_model_id = fake.model_name_to_id["x_biobridge_device"]
    fake.ir_model_fields_rows = [
        (mid, name) for mid, name in fake.ir_model_fields_rows if name != "x_company_id"
    ]
    fake.model_fields["x_biobridge_device"].discard("x_company_id")

    second_client = make_client(fake)
    assert second_client._device_tracking_mode() == "bootstrap"
    second_client.ensure_device_tracking_bootstrap()

    assert (device_model_id, "x_company_id") in fake.ir_model_fields_rows
    assert "x_company_id" in fake.model_fields["x_biobridge_device"]


def test_bootstrap_backfills_company_rule_for_a_connection_bootstrapped_before_it_existed():
    """Same scenario as the x_company_id backfill test above, but for a
    customer who bootstrapped after that field shipped and before the
    ir.rule did — x_company_id is already on every device, but nothing was
    ever created to filter Odoo's own UI by it. Re-running bootstrap must
    add the missing rule."""
    fake = FakeXmlRpcModels()
    client = make_client(fake)
    client.ensure_device_tracking_bootstrap()
    fake.ir_rule_rows.clear()

    second_client = make_client(fake)
    assert second_client._device_tracking_mode() == "bootstrap"
    second_client.ensure_device_tracking_bootstrap()

    device_model_id = fake.model_name_to_id["x_biobridge_device"]
    assert any(
        (mid, name) == (device_model_id, "x_biobridge_device.biobridge_company")
        for mid, name, _df in fake.ir_rule_rows
    )


def test_bootstrap_is_a_true_no_op_in_module_mode():
    """Real add-on installed — biobridge.device already handles its own
    company scoping server-side (see the add-on's _biobridge_upsert and its
    own security/biobridge_device_security.xml ir.rule), so there is
    nothing for bootstrap to create, and it must not try."""
    fake = FakeXmlRpcModels()
    fake.model_fields["hr.attendance"].add("device_id")
    client = make_client(fake)

    client.ensure_device_tracking_bootstrap()

    assert "x_biobridge_device" not in fake.model_name_to_id
    assert fake.calls.count("ir.model.fields.create") == 0
    assert fake.calls.count("ir.rule.create") == 0


def test_bootstrap_refuses_without_settings_access():
    fake = FakeXmlRpcModels()
    fake.settings_access = False
    client = make_client(fake)

    with pytest.raises(OdooAuthError, match="Settings"):
        client.ensure_device_tracking_bootstrap()

    # Nothing half-created.
    assert "x_biobridge_device" not in fake.model_name_to_id


# --------------------------------------------------------------------------- #
# Using it once it exists
# --------------------------------------------------------------------------- #
def test_upsert_device_bootstrap_creates_then_updates():
    fake = FakeXmlRpcModels()
    client = make_client(fake)
    client.ensure_device_tracking_bootstrap()

    first_id = client.upsert_device("GATE-01", name="Front Gate", location="Main Entrance")
    assert fake.x_biobridge_device_records[first_id]["x_name"] == "Front Gate"
    assert fake.x_biobridge_device_records[first_id]["x_location"] == "Main Entrance"

    # Same serial number again, different location — found and updated, not
    # duplicated, and a blank field never blanks out what's already there.
    second_id = client.upsert_device("GATE-01", location="Rebuilt Gate")
    assert second_id == first_id
    assert fake.x_biobridge_device_records[first_id]["x_location"] == "Rebuilt Gate"
    assert fake.x_biobridge_device_records[first_id]["x_name"] == "Front Gate"
    assert len(fake.x_biobridge_device_records) == 1


def test_upsert_device_module_mode_delegates_to_addon_method():
    fake = FakeXmlRpcModels()
    fake.model_fields["hr.attendance"].add("device_id")
    client = make_client(fake)

    device_id = client.upsert_device("GATE-02", name="Warehouse Door")

    assert "biobridge.device._biobridge_upsert" in fake.calls
    assert fake.biobridge_device_records[device_id]["serial_number"] == "GATE-02"
    assert "x_biobridge_device.create" not in fake.calls


def test_upsert_device_raises_when_neither_mechanism_present():
    client = make_client(FakeXmlRpcModels())
    with pytest.raises(OdooError, match="not set up"):
        client.upsert_device("GATE-03")


def test_create_attendance_uses_x_prefixed_field_in_bootstrap_mode():
    fake = FakeXmlRpcModels()
    client = make_client(fake)
    client.ensure_device_tracking_bootstrap()

    attendance_id = client.create_attendance(11, datetime(2026, 1, 1, 8, 0), device_id=42)

    record = fake.hr_attendance_records[attendance_id]
    assert record.get("x_device_id") == 42
    assert "device_id" not in record


def test_create_attendance_uses_plain_field_in_module_mode():
    fake = FakeXmlRpcModels()
    fake.model_fields["hr.attendance"].add("device_id")
    client = make_client(fake)

    attendance_id = client.create_attendance(11, datetime(2026, 1, 1, 8, 0), device_id=42)

    record = fake.hr_attendance_records[attendance_id]
    assert record.get("device_id") == 42
    assert "x_device_id" not in record


def test_create_attendance_omits_device_field_when_tracking_absent():
    fake = FakeXmlRpcModels()
    client = make_client(fake)

    attendance_id = client.create_attendance(11, datetime(2026, 1, 1, 8, 0), device_id=42)

    record = fake.hr_attendance_records[attendance_id]
    assert "device_id" not in record
    assert "x_device_id" not in record
