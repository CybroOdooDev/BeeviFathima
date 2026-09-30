"""Hikvision ISAPI terminals, against a simulated terminal that does real
HTTP Digest auth, pages its event log 30 at a time, and mixes door and
stranger events in with real punches."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

import httpx
import pytest

from app.integrations.base import EmployeeRecord, ProviderError, SourceConfig
from app.integrations.providers import hikvision as hik
from app.integrations.providers.hikvision import HikvisionProvider, direction_of, parse_event_time
from tests.test_platform_admin import api, head, signup  # noqa: F401

USER, PASSWORD, REALM, NONCE = "admin", "Hik12345", "DS-K1T343", "nonce-4f1d"
SERIAL = "DS-K1T343MFX20240101AAWRF12345678"
TZ = "Asia/Kolkata"


class FakeTerminal:
    def __init__(self, events=None, reject_minor_zero=False):
        self.events = events or []
        self.users = {"1001": "Jane Doe"}
        self.reject_minor_zero = reject_minor_zero
        self.failed_logins = 0
        self.requests: list[tuple[str, str, dict]] = []

    # Digest, as the device checks it (RFC 2617, qop=auth, MD5).
    def _authorised(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Digest "):
            return False
        f = dict(re.findall(r'(\w+)="?([^",]+)"?', header[7:]))
        ha1 = hashlib.md5(f"{f['username']}:{REALM}:{PASSWORD}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{request.method}:{f['uri']}".encode()).hexdigest()
        want = hashlib.md5(f"{ha1}:{f['nonce']}:{f['nc']}:{f['cnonce']}:{f['qop']}:{ha2}".encode()).hexdigest()
        return f.get("username") == USER and f.get("response") == want

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self._authorised(request):
            if request.headers.get("authorization"):
                self.failed_logins += 1
            return httpx.Response(401, headers={
                "WWW-Authenticate": f'Digest qop="auth", realm="{REALM}", nonce="{NONCE}", stale="FALSE"'})
        body = json.loads(request.content or b"{}")
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/ISAPI/System/deviceInfo":
            return httpx.Response(200, text=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<DeviceInfo version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
                "<deviceName>Front door</deviceName><model>DS-K1T343MFX</model>"
                f"<serialNumber>{SERIAL}</serialNumber><firmwareVersion>V3.2.30</firmwareVersion>"
                "</DeviceInfo>"))
        if path == "/ISAPI/AccessControl/AcsEvent":
            cond = body["AcsEventCond"]
            if cond["minor"] == 0 and self.reject_minor_zero:
                return httpx.Response(400, json={"statusCode": 6, "statusString": "Invalid Content",
                                                 "subStatusCode": "badParameters"})
            rows = [e for e in self.events if cond["minor"] in (0, e["minor"])]
            page = rows[cond["searchResultPosition"]:cond["searchResultPosition"] + cond["maxResults"]]
            more = cond["searchResultPosition"] + len(page) < len(rows)
            return httpx.Response(200, json={"AcsEvent": {
                "searchID": cond["searchID"], "totalMatches": len(rows),
                "responseStatusStrg": "MORE" if more else ("OK" if page else "NO MATCH"),
                "numOfMatches": len(page), "InfoList": page}})
        if path == "/ISAPI/AccessControl/UserInfo/Search":
            cond = body["UserInfoSearchCond"]
            rows = [{"employeeNo": k, "name": v, "Valid": {"enable": True}} for k, v in self.users.items()]
            page = rows[cond["searchResultPosition"]:cond["searchResultPosition"] + cond["maxResults"]]
            more = cond["searchResultPosition"] + len(page) < len(rows)
            return httpx.Response(200, json={"UserInfoSearch": {
                "responseStatusStrg": "MORE" if more else "OK", "numOfMatches": len(page), "UserInfo": page}})
        if path == "/ISAPI/AccessControl/UserInfo/Record":
            info = body["UserInfo"]
            if info["employeeNo"] in self.users:
                return httpx.Response(400, json={"statusCode": 6, "statusString": "Invalid Content",
                                                 "subStatusCode": "employeeNoAlreadyExist"})
            self.users[info["employeeNo"]] = info["name"]
            return httpx.Response(200, json={"statusCode": 1, "statusString": "OK"})
        return httpx.Response(404, text="<ResponseStatus><statusString>Invalid Operation</statusString></ResponseStatus>")


def ev(serial_no, employee, when, status="checkIn", minor=75):
    return {"major": 5, "minor": minor, "time": when.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            "employeeNoString": employee, "name": "Jane Doe" if employee else "",
            "serialNo": serial_no, "attendanceStatus": status, "currentVerifyMode": "cardOrFace"}


@pytest.fixture
def terminal(monkeypatch):
    fake = FakeTerminal()
    monkeypatch.setattr(hik, "TRANSPORT", httpx.MockTransport(fake))
    return fake


def provider(password=PASSWORD):
    return HikvisionProvider(SourceConfig(base_url="http://10.0.0.64", username=USER,
                                          password=password, timezone=TZ))


# --- helpers ------------------------------------------------------------------
def test_time_and_direction_parsing():
    assert parse_event_time("2026-09-30T08:59:12+05:30", TZ) == datetime(2026, 9, 30, 8, 59, 12)
    assert parse_event_time("2026-09-30T03:29:12Z", TZ) == datetime(2026, 9, 30, 8, 59, 12)
    assert direction_of("checkIn") is True and direction_of("overtimeOut") is False
    assert direction_of("undefined") is None


# --- the provider -------------------------------------------------------------
def test_connects_with_digest_and_names_the_device(terminal):
    info = provider().test_connection()
    assert info.ok and "DS-K1T343MFX" in info.message and SERIAL in info.message
    terminals = list(provider().fetch_terminals())
    assert terminals[0].serial_number == SERIAL and terminals[0].ip_address == "10.0.0.64"


def test_wrong_password_is_explained(terminal):
    with pytest.raises(ProviderError, match="refused the username or password"):
        provider("nope").test_connection()


def test_pages_through_the_log_and_keeps_only_real_punches(terminal):
    base = datetime(2026, 9, 29, 7, 0)
    events = [ev(i, "1001", base + timedelta(minutes=i), "checkIn" if i % 2 else "checkOut")
              for i in range(1, 71)]                     # 70 punches → three pages of 30
    events.insert(10, ev(900, "", base, minor=5))       # a door event: no employee
    events.insert(20, ev(901, "", base, minor=76))      # an unknown face
    terminal.events = events
    punches = list(provider().fetch_punches(base - timedelta(hours=1), base + timedelta(days=1)))
    assert len(punches) == 70
    pages = [r for r in terminal.requests if r[1] == "/ISAPI/AccessControl/AcsEvent"]
    assert len(pages) == 3 and pages[1][2]["AcsEventCond"]["searchResultPosition"] == 30
    assert len({r[2]["AcsEventCond"]["searchID"] for r in pages}) == 1, "one searchID across pages"
    first = punches[0]
    assert first.external_id == f"{SERIAL}:1" and first.emp_code == "1001"
    assert first.punch_time_local == base + timedelta(minutes=1) and first.direction is True
    assert first.verify_type == "face" and first.terminal_sn == SERIAL
    sent = pages[0][2]["AcsEventCond"]
    assert sent["major"] == 5 and sent["startTime"].endswith("+05:30")


def test_falls_back_when_minor_zero_is_rejected(terminal):
    terminal.reject_minor_zero = True
    base = datetime(2026, 9, 29, 7, 0)
    terminal.events = [ev(1, "1001", base, minor=75), ev(2, "1002", base + timedelta(hours=1), minor=38)]
    punches = list(provider().fetch_punches(base - timedelta(hours=1), base + timedelta(hours=3)))
    assert {p.emp_code for p in punches} == {"1001", "1002"}
    assert {p.verify_type for p in punches} == {"face", "finger"}


def test_unreachable_device_fails_rather_than_returning_nothing(monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    monkeypatch.setattr(hik, "TRANSPORT", httpx.MockTransport(down))
    with pytest.raises(ProviderError, match="Cannot reach the Hikvision device"):
        list(provider().fetch_punches(datetime(2026, 9, 1), datetime(2026, 9, 2)))


def test_reads_and_creates_users(terminal):
    p = provider()
    assert {e.emp_code for e in p.fetch_employees()} == {"1001"}
    p.create_employee(EmployeeRecord(external_id=None, emp_code="2002", first_name="Anu", last_name="Raj"))
    assert terminal.users["2002"] == "Anu Raj"
    p.create_employee(EmployeeRecord(external_id=None, emp_code="1001"))  # already there: fine
    with pytest.raises(ProviderError):
        p.create_employee(EmployeeRecord(external_id=None, emp_code="x" * 40))


# --- through the API -------------------------------------------------------------
def test_added_from_settings_registers_the_terminal(api, terminal):
    token = signup(api, "Acme", "owner@acme.example.com")
    made = api.post("/api/v1/sources", headers=head(token), json={
        "name": "Front door", "provider": "hik_isapi", "connection_kind": "device",
        "base_url": "http://10.0.0.64", "username": USER, "password": PASSWORD,
        "server_timezone": TZ, "location": "Lobby"})
    assert made.status_code == 201, made.text
    assert made.json()["status"] == "connected"
    devices = api.get("/api/v1/devices", headers=head(token)).json()
    assert devices[0]["serial_number"] == SERIAL and devices[0]["ip_address"] == "10.0.0.64"
    assert devices[0]["alias"] == "Front door" and devices[0]["area"] == "Lobby"

    missing = api.post("/api/v1/sources", headers=head(token), json={
        "name": "No password", "provider": "hik_isapi", "connection_kind": "device",
        "base_url": "http://10.0.0.65", "username": USER, "server_timezone": TZ})
    assert missing.status_code == 400 and "Password" in missing.json()["detail"]


def test_a_sync_turns_terminal_events_into_odoo_attendance(api, terminal, monkeypatch):
    from zoneinfo import ZoneInfo

    from sqlalchemy import select

    from app.core.crypto import encrypt
    from app.models import OdooConnection, Tenant
    from app.services import sync_engine as engine_mod
    from tests.conftest import FakeOdoo

    token = signup(api, "Acme", "owner@acme.example.com")
    source = api.post("/api/v1/sources", headers=head(token), json={
        "name": "Front door", "provider": "hik_isapi", "connection_kind": "device",
        "base_url": "http://10.0.0.64", "username": USER, "password": PASSWORD,
        "server_timezone": TZ}).json()
    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://acme.odoo.com", db_name="acme",
                         username="bot@acme.com", api_key_enc=encrypt("key", tenant.crypto_key), is_active=True))
    s.commit(); s.close()
    odoo = FakeOdoo()
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)

    now = datetime.now(ZoneInfo(TZ)).replace(tzinfo=None, microsecond=0)
    terminal.events = [ev(1, "1001", now - timedelta(hours=3), "checkIn"),
                       ev(2, "", now - timedelta(hours=2), minor=76),
                       ev(3, "1001", now - timedelta(hours=1), "checkOut")]
    run = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert run["status"] == "success", run
    assert run["punches_new"] == 2 and len(odoo.attendances) == 1
    rec = next(iter(odoo.attendances.values()))
    assert rec["check_out"] is not None

    again = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert again["punches_new"] == 0, "the overlap window re-reads, the ledger dedupes"
