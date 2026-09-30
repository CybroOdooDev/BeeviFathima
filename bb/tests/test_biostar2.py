"""Suprema BioStar 2, against a simulated server: session login (and
re-login when it expires), event search paged by time, device list, users."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.integrations.base import EmployeeRecord, ProviderError, SourceConfig
from app.integrations.providers import biostar2 as bs
from app.integrations.providers.biostar2 import BioStar2Provider, direction_of, is_punch
from tests.test_platform_admin import api, head, signup  # noqa: F401

LOGIN, PASSWORD, TZ = "admin", "Biostar1!", "Asia/Dubai"


class FakeBioStar:
    def __init__(self):
        self.sessions: set[str] = set()
        self.logins = 0
        self.events: list[dict] = []
        self.devices = [
            {"id": "541530990", "name": "BioStation 2 Front", "device_type_id": {"id": "10", "name": "BioStation 2"},
             "lan": {"ip": "192.168.1.20"}, "device_group_id": {"id": "1", "name": "HQ"}},
            {"id": "544110221", "name": "FaceStation F2 Store", "device_type_id": {"id": "25", "name": "FaceStation F2"},
             "lan": {"ip": "192.168.1.21"}},
        ]
        self.users = {"1001": "Jane Doe"}
        self.searches: list[dict] = []

    def expire_sessions(self):
        self.sessions.clear()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path == "/api/login":
            user = body.get("User") or {}
            if user.get("login_id") != LOGIN or user.get("password") != PASSWORD:
                return httpx.Response(401, json={"Response": {"code": "20", "message": "Login failed: invalid password"}})
            self.logins += 1
            session = f"s{self.logins}"
            self.sessions.add(session)
            return httpx.Response(200, headers={"bs-session-id": session},
                                  json={"User": {"user_id": "1"}, "Response": {"code": "0"}})
        if request.headers.get("bs-session-id") not in self.sessions:
            return httpx.Response(401, json={"Response": {"code": "10", "message": "Login required"}})
        if path == "/api/devices":
            return httpx.Response(200, json={"DeviceCollection": {"total": str(len(self.devices)), "rows": self.devices},
                                             "Response": {"code": "0"}})
        if path == "/api/events/search":
            query = body["Query"]
            self.searches.append(query)
            lo, hi = query["conditions"][0]["values"]
            rows = sorted((e for e in self.events if lo <= e["datetime"] <= hi), key=lambda e: e["datetime"])
            return httpx.Response(200, json={"EventCollection": {"rows": rows[:query["limit"]]},
                                             "Response": {"code": "0"}})
        if path == "/api/users" and request.method == "GET":
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            rows = [{"user_id": k, "name": v, "disabled": "false"} for k, v in self.users.items()]
            return httpx.Response(200, json={"UserCollection": {"total": str(len(rows)), "rows": rows[offset:offset + limit]},
                                             "Response": {"code": "0"}})
        if path == "/api/users" and request.method == "POST":
            user = body["User"]
            if user["user_id"] in self.users:
                return httpx.Response(400, json={"Response": {"code": "202", "message": "User ID already exists"}})
            self.users[user["user_id"]] = user["name"]
            return httpx.Response(200, json={"Response": {"code": "0"}})
        return httpx.Response(404, json={"Response": {"code": "404", "message": "Not found"}})


def event(event_id, user, when_utc, code=4867, tna=1, device="541530990"):
    return {"id": str(event_id), "datetime": when_utc.strftime("%Y-%m-%dT%H:%M:%S.00Z"),
            "server_datetime": when_utc.strftime("%Y-%m-%dT%H:%M:%S.00Z"),
            "user_id": {"user_id": user, "name": "Jane Doe" if user else ""} if user else {},
            "device_id": {"id": device, "name": "BioStation 2 Front"},
            "event_type_id": {"code": str(code)}, "tna_key": str(tna)}


@pytest.fixture
def server(monkeypatch):
    fake = FakeBioStar()
    monkeypatch.setattr(bs, "TRANSPORT", httpx.MockTransport(fake))
    return fake


def provider(password=PASSWORD):
    return BioStar2Provider(SourceConfig(base_url="https://biostar.acme.test", username=LOGIN,
                                         password=password, timezone=TZ, verify_ssl=False))


def test_code_and_key_helpers():
    assert is_punch(4867) and is_punch("4097") and is_punch(0x1501)
    assert not is_punch(4354) and not is_punch(6144) and not is_punch(20480)  # fail / unknown / door
    assert direction_of("1") is True and direction_of(2) is False and direction_of(4) is True
    assert direction_of("0") is None and direction_of(None) is None


def test_login_and_device_count(server):
    info = provider().test_connection()
    assert info.ok and "2 devices" in info.message
    terminals = list(provider().fetch_terminals())
    assert [t.serial_number for t in terminals] == ["541530990", "544110221"]
    assert terminals[0].model == "BioStation 2" and terminals[0].ip_address == "192.168.1.20"
    assert terminals[0].area == "HQ"


def test_wrong_password_is_explained(server):
    with pytest.raises(ProviderError, match="refused the username or password"):
        provider("nope").test_connection()


def test_expired_session_logs_in_again(server):
    p = provider()
    p.test_connection()
    server.expire_sessions()
    assert len(list(p.fetch_terminals())) == 2
    assert server.logins == 2


def test_punches_are_paged_by_time_and_filtered(server, monkeypatch):
    monkeypatch.setattr(bs, "PAGE_SIZE", 10)
    base = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)   # 08:00 in Dubai
    server.events = [event(i, "1001", base + timedelta(minutes=i), tna=1 if i % 2 else 2) for i in range(1, 26)]
    server.events += [event(100, "", base, code=20480),          # door opened: no user
                      event(101, "1002", base, code=4354),        # identify failed
                      event(102, "", base, code=6144)]            # unregistered credential
    punches = list(provider().fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0)))
    assert len(punches) == 25 and len({p.external_id for p in punches}) == 25
    assert len(server.searches) >= 3, "25 punches at 10 a page"
    first = punches[0]
    assert first.emp_code == "1001" and first.external_id == "1"
    assert first.punch_time_local == datetime(2026, 9, 29, 8, 1), "UTC shown in the site's zone"
    assert first.direction is True and first.verify_type == "face"
    assert first.terminal_sn == "541530990" and first.terminal_alias == "BioStation 2 Front"
    sent = server.searches[0]["conditions"][0]
    assert sent["operator"] == 3 and sent["values"][0] == "2026-09-28T20:00:00.000Z"


def test_a_full_page_within_one_second_still_advances(server, monkeypatch):
    monkeypatch.setattr(bs, "PAGE_SIZE", 5)
    moment = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
    server.events = [event(i, str(1000 + i), moment) for i in range(1, 6)]   # 5 in one second
    server.events.append(event(9, "1009", moment + timedelta(minutes=5)))
    punches = list(provider().fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0)))
    assert {p.external_id for p in punches} == {"1", "2", "3", "4", "5", "9"}


def test_users_read_and_created(server):
    p = provider()
    assert {e.emp_code for e in p.fetch_employees()} == {"1001"}
    p.create_employee(EmployeeRecord(external_id=None, emp_code="2002", first_name="Anu", last_name="Raj"))
    assert server.users["2002"] == "Anu Raj"
    p.create_employee(EmployeeRecord(external_id=None, emp_code="1001"))   # already there: fine
    with pytest.raises(ProviderError, match="numeric"):
        p.create_employee(EmployeeRecord(external_id=None, emp_code="EMP-7"))


def test_connect_import_and_sync_through_the_api(api, server, monkeypatch):
    from sqlalchemy import select

    from app.core.crypto import encrypt
    from app.models import OdooConnection, Tenant
    from app.services import sync_engine as engine_mod
    from tests.conftest import FakeOdoo

    token = signup(api, "Acme", "owner@acme.example.com")
    made = api.post("/api/v1/sources", headers=head(token), json={
        "name": "HQ BioStar", "provider": "biostar2", "connection_kind": "platform",
        "base_url": "https://biostar.acme.test", "username": LOGIN, "password": PASSWORD,
        "server_timezone": TZ, "verify_ssl": False})
    assert made.status_code == 201, made.text
    source = made.json()
    assert source["status"] == "connected"

    imported = api.post(f"/api/v1/sources/{source['id']}/discover-devices", headers=head(token))
    assert imported.status_code == 200, imported.text
    assert {d["serial_number"] for d in imported.json()} == {"541530990", "544110221"}

    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://acme.odoo.com", db_name="acme",
                         username="bot@acme.com", api_key_enc=encrypt("key", tenant.crypto_key), is_active=True))
    s.commit(); s.close()
    odoo = FakeOdoo()
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    server.events = [event(1, "1001", now - timedelta(hours=3), tna=1),
                     event(2, "", now - timedelta(hours=2), code=20480),
                     event(3, "1001", now - timedelta(hours=1), tna=2)]
    run = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert run["status"] == "success", run
    assert run["punches_new"] == 2 and len(odoo.attendances) == 1
    assert next(iter(odoo.attendances.values()))["check_out"] is not None
