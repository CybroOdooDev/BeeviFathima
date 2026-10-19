"""HikCentral Professional, against a simulated OpenAPI (Artemis) server that
verifies every request's HMAC signature, pages door events and the person
list, and — in one mode — refuses a search without an eventType."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.integrations.base import ProviderError, SourceConfig
from app.integrations.providers import hikcentral as hcp
from app.integrations.providers.hikcentral import HikCentralProvider, sign, string_to_sign
from tests.test_platform_admin import api, head, signup  # noqa: F401

AK, SK, TZ = "24681357", "Kx9mQ2vT7pL4wZ1r", "Asia/Riyadh"
URL = "https://hcp.acme.example"


class FakeHCP:
    def __init__(self, require_event_type=False):
        self.require_event_type = require_event_type
        self.people = [{"personId": "11", "personCode": "EMP001", "personGivenName": "Layla", "personFamilyName": "Hassan"},
                       {"personId": "12", "personCode": "EMP002", "personGivenName": "Yusuf", "personFamilyName": "Karim"},
                       {"personId": "13", "personCode": "", "personGivenName": "No", "personFamilyName": "Code"}]
        self.events: list[dict] = []
        self.calls: list[tuple[str, dict]] = []

    def _verify(self, request: httpx.Request) -> bool:
        h = request.headers
        if h.get("x-ca-key") != AK or h.get("x-ca-signature-headers") != "x-ca-key,x-ca-nonce,x-ca-timestamp":
            return False
        if h.get("accept") != "*/*" or h.get("content-type") != "application/json":
            return False
        text = ("POST\n*/*\napplication/json\n"
                f"x-ca-key:{AK}\nx-ca-nonce:{h['x-ca-nonce']}\nx-ca-timestamp:{h['x-ca-timestamp']}\n"
                f"{request.url.path}")
        expected = base64.b64encode(hmac.new(SK.encode(), text.encode(), hashlib.sha256).digest()).decode()
        return hmac.compare_digest(expected, h.get("x-ca-signature", ""))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self._verify(request):
            return httpx.Response(401, json={"code": "0x02401007", "msg": "signature error"})
        body = json.loads(request.content)
        path = request.url.path
        self.calls.append((path, body))
        size, page = body["pageSize"], body["pageNo"]
        if path == "/artemis/api/resource/v1/person/personList":
            chunk = self.people[(page - 1) * size: page * size]
            return httpx.Response(200, json={"code": "0", "msg": "Success",
                                             "data": {"total": len(self.people), "pageNo": page, "list": chunk}})
        if path == "/artemis/api/acs/v1/door/events":
            if self.require_event_type and "eventType" not in body:
                return httpx.Response(200, json={"code": "0x00052102", "msg": "eventType is required"})
            lo, hi = datetime.fromisoformat(body["startTime"]), datetime.fromisoformat(body["endTime"])
            rows = [e for e in self.events if lo <= datetime.fromisoformat(e["eventTime"]) <= hi
                    and ("eventType" not in body or e["eventType"] == body["eventType"])]
            return httpx.Response(200, json={"code": "0", "msg": "Success",
                                             "data": {"total": len(rows), "list": rows[(page - 1) * size: page * size]}})
        return httpx.Response(404)

    def add(self, pid, when, in_out=1, event_type=198914, door="1", event_id=None):
        self.events.append({"eventId": event_id or f"ev{len(self.events) + 1}", "eventType": event_type,
                            "eventTime": when.isoformat(timespec="seconds"), "personId": pid,
                            "personName": "", "doorName": f"Main Door {door}", "doorIndexCode": door,
                            "inAndOutType": in_out})


@pytest.fixture
def server(monkeypatch):
    fake = FakeHCP()
    monkeypatch.setattr(hcp, "TRANSPORT", httpx.MockTransport(fake))
    return fake


def provider(secret=SK, base=URL):
    return HikCentralProvider(SourceConfig(base_url=base, username=AK, password=secret, timezone=TZ))


def test_signature_matches_the_published_recipe():
    text = string_to_sign("/artemis/api/acs/v1/door/events", "k", "n", "1")
    assert text == "POST\n*/*\napplication/json\nx-ca-key:k\nx-ca-nonce:n\nx-ca-timestamp:1\n/artemis/api/acs/v1/door/events"
    assert sign("secret", text) == base64.b64encode(
        hmac.new(b"secret", text.encode(), hashlib.sha256).digest()).decode()
    assert provider(base="hcp.acme.example/artemis/").base == URL


def test_connects_and_counts(server):
    server.add("11", datetime.now(timezone.utc) - timedelta(hours=2))
    info = provider().test_connection()
    assert info.ok and "3 persons" in info.message and "1 door event" in info.message
    with pytest.raises(ProviderError, match="partner key or secret"):
        provider("wrong").test_connection()
    with pytest.raises(ProviderError, match="No HikCentral OpenAPI"):
        HikCentralProvider(SourceConfig(base_url=URL, username=AK, password=SK, timezone=TZ))._post("nope", {"pageNo": 1, "pageSize": 1})


def test_events_map_to_employee_ids(server, monkeypatch):
    monkeypatch.setattr(hcp, "PAGE_SIZE", 2)
    base = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)       # 08:00 in Riyadh
    server.add("11", base, in_out=1)
    server.add("12", base + timedelta(minutes=5), in_out=1)
    server.add("0", base + timedelta(minutes=6))                  # a door event, no person
    server.add("13", base + timedelta(minutes=7))                 # a person without an Employee ID
    server.add("11", base + timedelta(hours=9), in_out=0, door="2")
    punches = list(provider().fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0)))
    assert [p.emp_code for p in punches] == ["EMP001", "EMP002", "EMP001"]
    first = punches[0]
    assert first.punch_time_local == datetime(2026, 9, 29, 8, 0) and first.direction is True
    assert punches[2].direction is False and punches[2].terminal_sn == "hcp.acme.example:door2"
    assert first.terminal_alias == "Main Door 1" and first.external_id == "hcp.acme.example:ev1"
    assert (first.first_name, first.last_name) == ("Layla", "Hassan")
    event_reads = [b for p, b in server.calls if p.endswith("door/events")]
    assert [b["pageNo"] for b in event_reads] == [1, 2, 3]
    assert event_reads[0]["startTime"] == "2026-09-29T00:00:00+03:00"


def test_people_are_not_read_when_there_are_no_events(server):
    assert list(provider().fetch_punches(datetime(2026, 9, 29), datetime(2026, 9, 30))) == []
    assert not [p for p, _ in server.calls if p.endswith("personList")]


def test_a_server_that_requires_an_event_type(monkeypatch):
    fake = FakeHCP(require_event_type=True)
    monkeypatch.setattr(hcp, "TRANSPORT", httpx.MockTransport(fake))
    base = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)
    fake.add("11", base, event_type=198914)                       # card
    fake.add("12", base + timedelta(minutes=1), event_type=196893)  # face
    fake.add("12", base + timedelta(minutes=2), event_type=199999)  # something else
    punches = list(provider().fetch_punches(datetime(2026, 9, 29), datetime(2026, 9, 30)))
    assert sorted(p.emp_code for p in punches) == ["EMP001", "EMP002"]
    assert provider().test_connection().ok


def test_sync_writes_attendance(api, server, monkeypatch):
    from sqlalchemy import select

    from app.core.crypto import encrypt
    from app.models import OdooConnection, Tenant
    from app.services import sync_engine as engine_mod
    from tests.conftest import FakeOdoo

    token = signup(api, "Acme", "owner@acme.example.com")
    source = api.post("/api/v1/sources", headers=head(token), json={
        "name": "HCP", "provider": "hikcentral", "connection_kind": "platform", "base_url": URL,
        "username": AK, "password": SK, "server_timezone": TZ, "verify_ssl": False}).json()
    assert source["status"] == "connected", source
    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://acme.odoo.com", db_name="acme",
                         username="bot@acme.com", api_key_enc=encrypt("key", tenant.crypto_key), is_active=True))
    s.commit(); s.close()
    odoo = FakeOdoo(employees={"EMP001": (11, "Layla Hassan")})
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    server.add("11", now - timedelta(hours=3), in_out=1)
    server.add("11", now - timedelta(hours=1), in_out=0)
    run = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert run["status"] == "success", run
    assert run["punches_new"] == 2 and len(odoo.attendances) == 1
    devices = api.get("/api/v1/devices", headers=head(token)).json()
    assert [d["serial_number"] for d in devices] == ["hcp.acme.example:door1"]
    assert api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()["punches_new"] == 0
