"""Tenant isolation, checked across the whole API and the newer back-end paths.

Two accounts are set up with deliberately colliding data — the same terminal
serial number, the same badge, the same record numbers — so that a query or a
cache keyed on anything but the account would show up here:

* every route either needs a login or is on a short public allowlist;
* every route that takes an object id answers 404 to another account's id
  (indistinguishable from "does not exist") and leaves the object untouched;
* the Odoo device-removal queue only ever reads and works off its own rows;
* the record-id cursor and the punch ledger stay per account and per source;
* a push (ADMS) terminal that changes hands carries nothing of its old owner.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.api.deps import get_current_user, get_platform_admin, get_principal
from app.core.crypto import encrypt
from app.main import app
from app.models import (
    AdmsCommand,
    AdmsDevice,
    Device,
    DeviceSource,
    EmployeeMapping,
    OdooConnection,
    OdooDeviceRemoval,
    PunchRecord,
    Tenant,
    User,
)
from app.services import adms, device_removals
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo
from tests.test_platform_admin import api, head, signup  # noqa: F401

SERIAL = "SHARED-SERIAL-0001"


def _tenant_id(api, email):
    s = api.session_factory()
    try:
        return s.scalars(select(User).where(User.email == email)).first().tenant_id
    finally:
        s.close()


def _seed(api, email, company):
    """An account with one of everything a route can address by id."""
    token = signup(api, company, email)
    tid = _tenant_id(api, email)
    s = api.session_factory()
    tenant = s.get(Tenant, tid)
    conn = OdooConnection(tenant_id=tid, name="Odoo", url=f"https://{company.lower()}.odoo.com",
                          db_name=company.lower(), username="bot@x.com",
                          api_key_enc=encrypt("k", tenant.crypto_key), is_active=True)
    source = DeviceSource(tenant_id=tid, name="Lobby", provider="dahua", connection_kind="device",
                          base_url="http://10.0.0.108", username="admin",
                          password_enc=encrypt("pw", tenant.crypto_key), server_timezone="Asia/Dubai",
                          config={"record_cursor": 41})
    s.add_all([conn, source])
    s.flush()
    device = Device(tenant_id=tid, source_id=source.id, serial_number=SERIAL, ip_address="10.0.0.108")
    mapping = EmployeeMapping(tenant_id=tid, emp_code="1001", status="unmapped")
    punch = PunchRecord(tenant_id=tid, source_id=source.id, external_id=f"{SERIAL}:1",
                        emp_code="1001", terminal_sn=SERIAL, punch_time_utc=datetime(2026, 10, 1, 4, 0),
                        process_state="pending")
    s.add_all([device, mapping, punch])
    s.commit()
    ids = {"conn_id": conn.id, "source_id": source.id, "device_id": device.id,
           "mapping_id": mapping.id, "punch_id": punch.id}
    s.close()
    return token, tid, ids


# -- 1. every route needs a login, bar a public allowlist ---------------------------
PUBLIC_PREFIXES = ("/api/v1/auth/", "/api/v1/public/", "/iclock/", "/health")
PUBLIC_EXACT = {"/api/v1/billing/webhook", "/", "/favicon.ico"}


def _dependency_calls(dependant):
    calls = {dependant.call}
    for sub in dependant.dependencies:
        calls |= _dependency_calls(sub)
    return calls


def test_every_route_needs_a_login_unless_it_is_public_on_purpose():
    guards = {get_principal, get_platform_admin, get_current_user}
    open_routes = [
        f"{sorted(r.methods)} {r.path}"
        for r in app.routes
        if isinstance(r, APIRoute)
        and not (r.path in PUBLIC_EXACT or r.path.startswith(PUBLIC_PREFIXES))
        and not (_dependency_calls(r.dependant) & guards)
    ]
    assert open_routes == [], "routes reachable without a login"


def test_staff_routes_need_a_staff_login():
    loose = [r.path for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/api/v1/admin")
             and get_platform_admin not in _dependency_calls(r.dependant)]
    assert loose == []


# -- 2. another account's ids are 404, and nothing changes -------------------------------
BODIES = {"/api/v1/sources/{source_id}/default-company": {"company_id": None, "company_name": None}}
NOT_ADDRESSABLE = {"/api/v1/billing/invoices/{invoice_id}/pay"}   # Stripe checks the customer itself


def test_another_accounts_ids_are_not_found_on_every_route(api):
    token_a, _, ids_a = _seed(api, "owner@a.example.com", "Alpha")
    token_b, _, _ = _seed(api, "owner@b.example.com", "Beta")
    probed = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or "{" not in route.path or route.path.startswith("/api/v1/admin"):
            continue
        if route.path in NOT_ADDRESSABLE:
            continue
        params = {name: ids_a[name] for name in route.param_convertors}
        url = route.path.format(**params)
        for method in route.methods:
            body = BODIES.get(route.path, {}) if method in {"POST", "PUT", "PATCH"} else None
            r = api.request(method, url, headers=head(token_b), json=body)
            assert r.status_code == 404, f"{method} {route.path} answered {r.status_code}: {r.text[:200]}"
            probed += 1
    assert probed >= 18

    s = api.session_factory()
    try:
        assert s.get(OdooConnection, ids_a["conn_id"]) is not None
        assert s.get(DeviceSource, ids_a["source_id"]).config == {"record_cursor": 41}
        assert s.get(Device, ids_a["device_id"]) is not None
        assert s.get(EmployeeMapping, ids_a["mapping_id"]).status == "unmapped"
        assert s.get(PunchRecord, ids_a["punch_id"]).process_state == "pending"
    finally:
        s.close()
    # ...and the owner still reaches all of it.
    assert len(api.get("/api/v1/sources", headers=head(token_a)).json()) == 1


def test_lists_show_only_the_callers_own_rows(api):
    token_a, _, ids_a = _seed(api, "owner@a.example.com", "Alpha")
    token_b, _, ids_b = _seed(api, "owner@b.example.com", "Beta")
    for token, ids in ((token_a, ids_a), (token_b, ids_b)):
        h = head(token)
        assert [x["id"] for x in api.get("/api/v1/sources", headers=h).json()] == [ids["source_id"]]
        assert [x["id"] for x in api.get("/api/v1/devices", headers=h).json()] == [ids["device_id"]]
        assert [x["id"] for x in api.get("/api/v1/punches", headers=h).json()] == [ids["punch_id"]]
        assert [x["id"] for x in api.get("/api/v1/odoo-connections", headers=h).json()] == [ids["conn_id"]]
        assert ids["mapping_id"] in [x["id"] for x in api.get("/api/v1/mappings", headers=h).json()]
        # The same badge and serial in the other account are not counted here.
        assert len(api.get(f"/api/v1/punches?terminal_sn={SERIAL}", headers=h).json()) == 1


def test_a_body_carried_source_id_from_another_account_is_refused(api):
    _, _, ids_a = _seed(api, "owner@a.example.com", "Alpha")
    token_b, _, _ = _seed(api, "owner@b.example.com", "Beta")
    r = api.post("/api/v1/sources/test", headers=head(token_b), json={
        "source_id": ids_a["source_id"], "provider": "dahua", "base_url": "http://10.9.9.9",
        "username": "admin", "server_timezone": "UTC"})
    assert r.status_code == 404   # A's stored password is never borrowed


# -- 3. the Odoo device-removal queue -----------------------------------------------
def test_removal_queue_is_per_account(api):
    _, tid_a, ids_a = _seed(api, "owner@a.example.com", "Alpha")
    _, tid_b, _ = _seed(api, "owner@b.example.com", "Beta")
    s = api.session_factory()
    try:
        source_a = s.get(DeviceSource, ids_a["source_id"])
        # B has a terminal with the same serial: that must not keep A's Odoo record.
        assert device_removals.queue_source_terminals(s, tid_a, source_a) == 1
        s.add(OdooDeviceRemoval(tenant_id=tid_b, serial_number="ONLY-B", source_name="x"))
        s.commit()

        s.delete(s.get(Device, ids_a["device_id"]))
        s.flush()
        odoo_a = FakeOdoo()
        deleted = []
        odoo_a.delete_device = lambda serial, company_id=None: deleted.append(serial) or True
        removed, errors = device_removals.work_off(s, tid_a, odoo_a)
        s.commit()
        assert (removed, errors, deleted) == (1, [], [SERIAL])
        left = s.scalars(select(OdooDeviceRemoval)).all()
        assert [(r.tenant_id, r.serial_number) for r in left] == [(tid_b, "ONLY-B")]
    finally:
        s.close()


def test_a_terminal_in_another_account_does_not_count_as_in_use(api):
    _, tid_a, ids_a = _seed(api, "owner@a.example.com", "Alpha")
    _seed(api, "owner@b.example.com", "Beta")
    s = api.session_factory()
    try:
        assert not device_removals.terminal_in_use(s, tid_a, SERIAL, "10.0.0.108",
                                                   exclude_source_id=ids_a["source_id"])
    finally:
        s.close()


# -- 4. the ledger and the record-id cursor --------------------------------------------
def test_identical_record_numbers_in_two_accounts_stay_apart(api, monkeypatch):
    """Two accounts, each with a Dahua terminal at the same private address and
    the same serial and record numbers: each sync stores its own punches and
    moves only its own cursor."""
    from app.integrations.providers import dahua as dh
    from tests.test_dahua import FakeDahua, PASSWORD, USER

    terminal = FakeDahua()
    monkeypatch.setattr(dh, "TRANSPORT", httpx.MockTransport(terminal))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    terminal.add("1001", now - timedelta(hours=2))
    terminal.add("1001", now - timedelta(hours=1))

    accounts = []
    for email, company in (("owner@a.example.com", "Alpha"), ("owner@b.example.com", "Beta")):
        token = signup(api, company, email)
        source = api.post("/api/v1/sources", headers=head(token), json={
            "name": "Lobby", "provider": "dahua", "connection_kind": "device",
            "base_url": "http://10.0.0.108", "username": USER, "password": PASSWORD,
            "server_timezone": "Asia/Dubai"}).json()
        assert "id" in source, source
        tid = _tenant_id(api, email)
        s = api.session_factory()
        tenant = s.get(Tenant, tid)
        s.add(OdooConnection(tenant_id=tid, name="Odoo", url=f"https://{company.lower()}.odoo.com",
                             db_name=company.lower(), username="bot@x.com",
                             api_key_enc=encrypt("k", tenant.crypto_key), is_active=True))
        s.commit(); s.close()
        accounts.append((token, tid, source["id"]))

    odoo = FakeOdoo(employees={"1001": (11, "Jane Doe")})
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    token_a, tid_a, source_a = accounts[0]
    token_b, tid_b, source_b = accounts[1]

    assert api.post(f"/api/v1/sources/{source_a}/sync", headers=head(token_a)).json()["punches_new"] == 2
    s = api.session_factory()
    assert s.get(DeviceSource, source_b).config.get("record_cursor") is None   # B untouched
    s.close()
    # B's first sync is not deduplicated against A's identical external ids.
    assert api.post(f"/api/v1/sources/{source_b}/sync", headers=head(token_b)).json()["punches_new"] == 2

    s = api.session_factory()
    try:
        for tid, source_id in ((tid_a, source_a), (tid_b, source_b)):
            punches = s.scalars(select(PunchRecord).where(PunchRecord.tenant_id == tid)).all()
            assert {p.source_id for p in punches} == {source_id} and len(punches) == 2
            assert s.get(DeviceSource, source_id).config.get("record_cursor") == 2
    finally:
        s.close()


# -- 5. a push terminal changing hands ------------------------------------------------
def test_a_released_push_terminal_carries_nothing_to_its_next_owner(api):
    from tests.test_adms_push import Terminal, add_device

    sn = "CKJG201760999"
    token_a = signup(api, "Alpha", "owner@a.example.com")
    assert add_device(api, token_a, serial=sn).status_code == 201
    t = Terminal(api, serial=sn)
    t.handshake()
    t.users("PIN=1001\tName=Alice Alpha\tPri=0", "PIN=1002\tName=Bob Alpha\tPri=0")
    s = api.session_factory()
    device = s.scalars(select(AdmsDevice).where(AdmsDevice.serial_number == sn)).first()
    assert device.users, "the terminal reported its users to A"
    adms.queue(s, sn, "DATA UPDATE USERINFO PIN=1003\tName=Carol Alpha")
    s.commit(); s.close()

    source_a = api.get("/api/v1/sources", headers=head(token_a)).json()[0]["id"]
    assert api.delete(f"/api/v1/sources/{source_a}", headers=head(token_a)).status_code in (200, 204)

    s = api.session_factory()
    device = s.scalars(select(AdmsDevice).where(AdmsDevice.serial_number == sn)).first()
    assert (device.tenant_id, device.source_id, device.users) == (None, None, {})
    assert device.operlog_stamp is None and device.attlog_stamp is None
    waiting = s.scalars(select(AdmsCommand).where(AdmsCommand.serial_number == sn,
                                                  AdmsCommand.status.in_(("queued", "sent")))).all()
    assert waiting == [], "A's queued user writes must not reach the terminal for its next owner"
    s.close()

    token_b = signup(api, "Beta", "owner@b.example.com")
    assert add_device(api, token_b, serial=sn).status_code == 201
    reply = t.heartbeat().text
    assert "Alpha" not in reply
    names = [m.get("odoo_employee_name") or m.get("emp_code")
             for m in api.get("/api/v1/mappings", headers=head(token_b)).json()]
    assert not any("Alpha" in str(n) for n in names)


def test_a_push_connection_cannot_reach_a_terminal_another_account_holds(api):
    from app.integrations.base import SourceConfig
    from app.integrations.providers.zkteco_adms import ZKAdmsProvider
    from tests.test_adms_push import Terminal, add_device

    sn = "CKJG201760777"
    token_a = signup(api, "Alpha", "owner@a.example.com")
    assert add_device(api, token_a, serial=sn).status_code == 201
    t = Terminal(api, serial=sn)
    t.handshake()
    t.users("PIN=1001\tName=Alice Alpha\tPri=0")
    tid_b = _tenant_id(api, signup(api, "Beta", "owner@b.example.com") and "owner@b.example.com")

    s = api.session_factory()
    try:
        provider = ZKAdmsProvider(SourceConfig(
            base_url=f"adms://{sn}", timezone="UTC",
            options={"_db": s, "_tenant_id": tid_b, "_source_id": "not-a's-source"}))
        assert provider._device() is None
        assert list(provider.fetch_employees()) == []
        assert list(provider.fetch_terminals()) == []
    finally:
        s.close()


@pytest.mark.parametrize("held_by,asking,source,allowed", [
    (None, "T1", None, True),                 # unclaimed: a test before saving
    ("T1", "T1", None, True),
    ("T1", "T1", "S1", True),
    ("T1", "T1", "S2", False),                # another connection of the same account
    ("T1", "T2", None, False),
    ("T1", "T2", "S9", False),
])
def test_owned_by(held_by, asking, source, allowed):
    device = AdmsDevice(serial_number="X", tenant_id=held_by, source_id="S1" if held_by else None)
    assert adms.owned_by(device, asking, source) is allowed
