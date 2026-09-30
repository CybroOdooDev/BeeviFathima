"""ZKTeco ADMS push: a simulated terminal calling /iclock/ end to end —
handshake, refusal while unclaimed, claiming it in Settings, punches into the
ledger and on to Odoo, user list, command queue, provisioning, release."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.crypto import encrypt
from app.models import AdmsCommand, AdmsDevice, OdooConnection, PunchRecord, Tenant
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo
from tests.test_platform_admin import api, head, signup  # noqa: F401

SN = "CKJG201760123"


class Terminal:
    """Just enough of a ZKTeco push terminal to drive the receiver."""

    def __init__(self, client, serial=SN):
        self.c, self.sn = client, serial

    def handshake(self):
        return self.c.get(f"/iclock/cdata?SN={self.sn}&options=all&pushver=2.4.1&language=69")

    def attlog(self, *lines, stamp="1"):
        return self.c.post(f"/iclock/cdata?SN={self.sn}&table=ATTLOG&Stamp={stamp}",
                           content="\n".join(lines) + "\n")

    def users(self, *lines):
        return self.c.post(f"/iclock/cdata?SN={self.sn}&table=USERINFO", content="\n".join(lines))

    def heartbeat(self, info="Ver 8.0.4.2-20190708,3,2,120,192.168.1.201,10,7,15,11,111"):
        return self.c.get(f"/iclock/getrequest?SN={self.sn}&INFO={info}")

    def report(self, body):
        return self.c.post(f"/iclock/devicecmd?SN={self.sn}", content=body)


def rows(api, model, **where):
    s = api.session_factory()
    try:
        q = select(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        out = s.scalars(q).all()
        for o in out:
            s.expunge(o)
        return out
    finally:
        s.close()


def add_device(api, token, serial=SN, name="Front door"):
    return api.post("/api/v1/sources", headers=head(token), json={
        "name": name, "provider": "zk_adms", "connection_kind": "device",
        "base_url": f"adms://{serial}", "server_timezone": "Asia/Kolkata",
        "location": "Main entrance"})


def when(hours_ago):
    return (datetime.now() - timedelta(hours=hours_ago)).replace(microsecond=0)


def line(pin, moment, state=0, verify=1):
    return f"{pin}\t{moment:%Y-%m-%d %H:%M:%S}\t{state}\t{verify}\t0\t0\t0"


# --- before anyone claims it --------------------------------------------------
def test_an_unknown_terminal_is_heard_but_its_punches_are_refused(api):
    t = Terminal(api)
    r = t.handshake()
    assert r.status_code == 200 and r.text.startswith(f"GET OPTION FROM: {SN}")
    assert "ATTLOGStamp=None" in r.text and "Realtime=1" in r.text
    assert rows(api, AdmsDevice)[0].tenant_id is None

    refused = t.attlog(line("1001", when(1)))
    assert refused.status_code == 403, "refused, so the terminal keeps it and re-sends later"
    assert rows(api, PunchRecord) == []
    assert t.heartbeat().text == "OK", "no commands for an unclaimed terminal"


def test_bad_serials_are_rejected(api):
    assert api.get("/iclock/cdata?SN=../etc&options=all").status_code == 400
    assert api.get("/iclock/getrequest").status_code == 400


# --- claimed ------------------------------------------------------------------
def test_claiming_a_terminal_then_its_punches_reach_the_ledger(api):
    token = signup(api, "Acme", "owner@acme.example.com")
    t = Terminal(api)

    # Not heard from yet: the test says so, with setup instructions.
    draft = api.post("/api/v1/sources/test", headers=head(token), json={
        "provider": "zk_adms", "base_url": f"adms://{SN}", "server_timezone": "Asia/Kolkata"})
    assert draft.json()["ok"] is False and "Cloud Server Setting" in draft.json()["message"]

    t.handshake()
    t.heartbeat()
    draft = api.post("/api/v1/sources/test", headers=head(token), json={
        "provider": "zk_adms", "base_url": f"adms://{SN.lower()}", "server_timezone": "Asia/Kolkata"})
    assert draft.json()["ok"] is True, draft.json()

    made = add_device(api, token)
    assert made.status_code == 201, made.text
    source = made.json()
    assert source["status"] == "connected" and source["base_url"] == f"adms://{SN}"
    claim = rows(api, AdmsDevice)[0]
    assert claim.source_id == source["id"]
    # The terminal is on the Terminals page straight away, named and located.
    devices = api.get("/api/v1/devices", headers=head(token)).json()
    assert devices[0]["serial_number"] == SN and devices[0]["alias"] == "Front door"
    assert devices[0]["area"] == "Main entrance"

    # Its next heartbeat carries the "tell me about yourself" commands.
    reply = t.heartbeat().text
    assert "DATA QUERY USERINFO" in reply and ":INFO" in reply
    ids = [l.split(":")[1] for l in reply.splitlines() if l.startswith("C:")]
    assert t.report("\n".join(f"ID={i}&Return=0&CMD=DATA" for i in ids)).text == "OK"
    assert {c.status for c in rows(api, AdmsCommand)} == {"done"}

    t.users("USER PIN=1001\tName=Jane Doe\tPri=0", "USER PIN=1002\tName=Omar\tPri=0")
    assert rows(api, AdmsDevice)[0].users == {"1001": "Jane Doe", "1002": "Omar"}

    first, second = when(3), when(1)
    ok = t.attlog(line("1001", first, 0, 1), line("1001", second, 1, 15))
    assert ok.status_code == 200 and ok.text == "OK: 2"
    t.attlog(line("1001", first, 0, 1))  # a re-send
    punches = sorted(rows(api, PunchRecord), key=lambda p: p.punch_time_utc)
    assert len(punches) == 2, "re-sent punches are not stored twice"
    assert punches[0].direction == "in" and punches[1].direction == "out"
    assert punches[1].verify_type == "face"
    # Asia/Kolkata is UTC+5:30.
    assert punches[0].punch_time_utc == first - timedelta(hours=5, minutes=30)
    assert punches[0].first_seen_run_id is None, "claimed by the next sync run"
    terminal = api.get("/api/v1/devices", headers=head(token)).json()[0]
    assert terminal["punch_count"] == 2 and terminal["last_seen_at"]


def test_sync_claims_pushed_punches_and_writes_attendance(api, monkeypatch):
    token = signup(api, "Acme", "owner@acme.example.com")
    t = Terminal(api)
    t.handshake()
    source = add_device(api, token).json()
    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://acme.odoo.com",
                         db_name="acme", username="bot@acme.com",
                         api_key_enc=encrypt("key", tenant.crypto_key), is_active=True))
    s.commit(); s.close()
    odoo = FakeOdoo()
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t_, c: odoo)

    t.attlog(line("1001", when(3), 0), line("1001", when(1), 1))
    run = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert run["status"] == "success", run
    assert run["punches_new"] == 2
    assert len(odoo.attendances) == 1
    rec = next(iter(odoo.attendances.values()))
    assert rec["check_out"] is not None

    again = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert again["punches_new"] == 0


def test_offline_terminal_still_sends_what_already_arrived(api, monkeypatch):
    token = signup(api, "Acme", "owner@acme.example.com")
    t = Terminal(api)
    t.handshake()
    source = add_device(api, token).json()
    t.attlog(line("1001", when(2), 0))
    s = api.session_factory()
    d = s.scalars(select(AdmsDevice)).first()
    d.last_seen_at = datetime.now() - timedelta(hours=2)
    s.commit(); s.close()

    run = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert run["punches_new"] == 1, "not an 'unreachable' abort — the punch is in hand"
    listed = {x["id"]: x for x in api.get("/api/v1/sources", headers=head(token)).json()}
    assert listed[source["id"]]["status"] == "failed"
    assert "last called in" in listed[source["id"]]["status_message"]


def test_provisioning_queues_users_for_the_terminal(api):
    from app.integrations.base import EmployeeRecord
    from app.services.connections import build_source_provider
    from app.models import DeviceSource

    token = signup(api, "Acme", "owner@acme.example.com")
    t = Terminal(api)
    t.handshake()
    add_device(api, token)
    t.users("USER PIN=1001\tName=Jane Doe")
    s = api.session_factory()
    source = s.scalars(select(DeviceSource)).first()
    tenant = s.get(Tenant, source.tenant_id)
    provider = build_source_provider(tenant, source)
    assert {e.emp_code for e in provider.fetch_employees()} == {"1001"}
    provider.create_employee(EmployeeRecord(external_id=None, emp_code="2002",
                                            first_name="Anu", last_name="Raj"))
    s.commit(); s.close()

    reply = t.heartbeat().text
    assert "DATA UPDATE USERINFO PIN=2002\tName=Anu Raj" in reply
    assert "2002" in rows(api, AdmsDevice)[0].users, "counted as on the device from now"


def test_info_reply_names_the_model(api):
    token = signup(api, "Acme", "owner@acme.example.com")
    t = Terminal(api)
    t.handshake()
    add_device(api, token)
    info = [c for c in rows(api, AdmsCommand) if c.command == "INFO"][0]
    t.report(f"ID={info.id}&Return=0&CMD=INFO\n~DeviceName=SpeedFace-V5L\nFWVersion=Ver 6.60")
    device = rows(api, AdmsDevice)[0]
    assert device.model == "SpeedFace-V5L" and device.firmware == "Ver 6.60"


# --- ownership ------------------------------------------------------------------
def test_one_terminal_one_account_and_release_on_delete(api):
    a = signup(api, "Acme", "owner@acme.example.com")
    b = signup(api, "Globex", "owner@globex.example.com")
    t = Terminal(api)
    t.handshake()
    source = add_device(api, a).json()

    stolen = add_device(api, b)
    assert stolen.status_code == 409 and "another BioBridge account" in stolen.json()["detail"]
    dupe = add_device(api, a, name="Again")
    assert dupe.status_code == 409

    assert api.delete(f"/api/v1/sources/{source['id']}", headers=head(a)).status_code == 204
    assert rows(api, AdmsDevice)[0].tenant_id is None
    assert t.attlog(line("1001", when(1))).status_code == 403
    assert add_device(api, b).status_code == 201, "free to be claimed again"
