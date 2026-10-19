"""Hik-Connect for Teams, against a simulated cloud: token/get with app key
and secret, the ``Token`` header, paged attendance records, token expiry,
and records whose person sits in a nested object."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.integrations.base import ProviderError, SourceConfig
from app.integrations.providers import hikconnect as hc
from app.integrations.providers.hikconnect import HikConnectProvider, direction_of, parse_time
from tests.test_platform_admin import api, head, signup  # noqa: F401

KEY, SECRET, TZ = "ak-team-1", "sk-team-1", "Asia/Dubai"
EU = "https://ieu.hikcentralconnect.com"


class FakeHikConnect:
    def __init__(self):
        self.tokens: set[str] = set()
        self.issued = 0
        self.records: list[dict] = []
        self.calls: list[tuple[str, str, dict]] = []   # (host, path, body)
        self.area_domain: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        body = json.loads(request.content or b"{}")
        path = request.url.path.removeprefix("/api/hccgw/")
        self.calls.append((request.url.host, path, body))
        if path == "platform/v1/token/get":
            if body != {"appKey": KEY, "secretKey": SECRET}:
                return httpx.Response(200, json={"errorCode": "OPEN000006", "message": "appKey or secretKey error"})
            self.issued += 1
            token = f"tok{self.issued}"
            self.tokens.add(token)
            data = {"accessToken": token, "expireTime": 1893456000}
            if self.area_domain:
                data["areaDomain"] = self.area_domain
            return httpx.Response(200, json={"errorCode": "0", "message": "success", "data": data})
        if request.headers.get("Token") not in self.tokens:
            return httpx.Response(200, json={"errorCode": "OPEN000003", "message": "token expired"})
        if path == "attendance/v1/records/get":
            lo, hi = datetime.fromisoformat(body["startTime"]), datetime.fromisoformat(body["endTime"])
            rows = [r for r in self.records if lo <= datetime.fromisoformat(r["_when"]) <= hi]
            size, page = body["pageSize"], body["pageNo"]
            chunk = [{k: v for k, v in r.items() if k != "_when"} for r in rows[(page - 1) * size: page * size]]
            return httpx.Response(200, json={"errorCode": "0", "data": {"total": len(rows), "list": chunk}})
        return httpx.Response(404, json={"errorCode": "404", "message": "not found"})


def record(code, when, status="checkIn", serial="FA1234567", rid=None):
    return {"recordId": rid, "personCode": code, "firstName": "Sara", "lastName": "Ali",
            "clockTime": when.isoformat(timespec="seconds"), "attendanceStatus": status,
            "deviceSerialNo": serial, "deviceName": "Reception DS-K1T343", "_when": when.isoformat()}


def nested(code, when, clock_type="1"):
    """Another plausible shape: the person nested, the time in epoch ms."""
    return {"guid": None, "personInfo": {"baseInfo": {"personCode": code, "firstName": "Omar"}},
            "eventTime": int(when.timestamp() * 1000), "clockType": clock_type,
            "deviceInfo": {"deviceSerial": "FB7654321"}, "_when": when.isoformat()}


@pytest.fixture
def cloud(monkeypatch):
    fake = FakeHikConnect()
    monkeypatch.setattr(hc, "TRANSPORT", httpx.MockTransport(fake))
    return fake


def provider(secret=SECRET, token=None, base=EU):
    return HikConnectProvider(SourceConfig(base_url=base, username=KEY, password=secret, timezone=TZ, token=token))


def test_helpers():
    zone = hc.ZoneInfo(TZ)
    assert parse_time("2026-09-29T08:00:00+04:00", zone) == datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
    assert parse_time("2026-09-29 08:00:00", zone).utcoffset() == timedelta(hours=4)
    assert parse_time(1790000000000, zone) == datetime.fromtimestamp(1790000000, tz=timezone.utc)
    assert (direction_of("checkIn"), direction_of("checkOut"), direction_of("0"), direction_of("1"),
            direction_of("breakOut"), direction_of(None)) == (True, False, True, False, False, None)
    assert provider(base="eu").base == EU and provider(base=EU + "/api/hccgw/").base == EU


def test_connects_with_the_app_key(cloud):
    cloud.records = [record("1001", datetime.now(timezone.utc) - timedelta(hours=1))]
    info = provider().test_connection()
    assert info.ok and "1 attendance record" in info.message
    assert cloud.calls[-1][2]["pageSize"] == 5
    with pytest.raises(ProviderError, match="refused the app key or secret key"):
        provider("wrong").test_connection()


def test_unrecognised_record_fields_are_reported(cloud):
    cloud.records = [{"foo": "bar", "_when": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}]
    info = provider().test_connection()
    assert not info.ok and info.detail["fields"] == ["foo"]


def test_pages_records_and_maps_them(cloud, monkeypatch):
    monkeypatch.setattr(hc, "PAGE_SIZE", 10)
    base = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)          # 08:00 in Dubai
    cloud.records = [record("1001", base + timedelta(minutes=i), status="checkIn" if i % 2 == 0 else "checkOut",
                            rid=f"r{i}") for i in range(25)]
    punches = list(provider().fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0)))
    assert len(punches) == 25
    reads = [c for c in cloud.calls if c[1] == "attendance/v1/records/get"]
    assert [c[2]["pageNo"] for c in reads] == [1, 2, 3]
    assert reads[0][2]["startTime"] == "2026-09-29T00:00:00+04:00"
    first = punches[0]
    assert first.emp_code == "1001" and first.punch_time_local == datetime(2026, 9, 29, 8, 0)
    assert first.direction is True and punches[1].direction is False
    assert first.terminal_sn == "FA1234567" and first.terminal_alias == "Reception DS-K1T343"
    assert first.external_id == "hc:r0" and (first.first_name, first.last_name) == ("Sara", "Ali")


def test_a_nested_shape_without_an_id(cloud):
    when = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)
    cloud.records = [nested("E-77", when)]
    (p,) = provider().fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0))
    assert p.emp_code == "E-77" and p.first_name == "Omar"
    assert p.punch_time_local == datetime(2026, 9, 29, 17, 30) and p.direction is False
    assert p.external_id == "FB7654321:E-77:20260929133000", "keyed on UTC"


def test_an_expired_token_is_renewed_once(cloud):
    cloud.records = [record("1001", datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc))]
    stale = provider(token="tok-old")
    assert len(list(stale.fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0)))) == 1
    assert stale.cached_token == "tok1" and cloud.issued == 1


def test_area_domain_is_followed_only_to_hik_connect(cloud):
    cloud.area_domain = "https://isgp.hikcentralconnect.com"
    p = provider()
    p.test_connection()
    assert p.base == "https://isgp.hikcentralconnect.com" and cloud.calls[-1][0] == "isgp.hikcentralconnect.com"
    cloud.area_domain = "https://evil.example.com"
    q = provider()
    q.test_connection()
    assert q.base == EU and cloud.calls[-1][0] == "ieu.hikcentralconnect.com"


def test_two_teams_in_one_region_are_two_connections(api, cloud):
    token = signup(api, "Acme", "owner@acme.example.com")
    body = {"provider": "hikconnect", "connection_kind": "platform", "base_url": EU,
            "username": KEY, "password": SECRET, "server_timezone": TZ}
    assert api.post("/api/v1/sources", headers=head(token), json={**body, "name": "Team A"}).status_code == 201
    assert api.post("/api/v1/sources", headers=head(token), json={**body, "name": "Again"}).status_code == 409
    other = api.post("/api/v1/sources", headers=head(token), json={**body, "name": "Team B", "username": "ak-2"})
    assert other.status_code == 201


def test_sync_brings_terminals_in_with_the_punches(api, cloud, monkeypatch):
    from sqlalchemy import select

    from app.core.crypto import encrypt
    from app.models import OdooConnection, Tenant
    from app.services import sync_engine as engine_mod
    from tests.conftest import FakeOdoo

    token = signup(api, "Acme", "owner@acme.example.com")
    source = api.post("/api/v1/sources", headers=head(token), json={
        "name": "Hik-Connect", "provider": "hikconnect", "connection_kind": "platform", "base_url": EU,
        "username": KEY, "password": SECRET, "server_timezone": TZ}).json()
    assert source["status"] == "connected", source
    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://acme.odoo.com", db_name="acme",
                         username="bot@acme.com", api_key_enc=encrypt("key", tenant.crypto_key), is_active=True))
    s.commit(); s.close()
    odoo = FakeOdoo(employees={"1001": (11, "Sara Ali")})
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    cloud.records = [record("1001", now - timedelta(hours=3), "checkIn", rid="a"),
                     record("1001", now - timedelta(hours=1), "checkOut", rid="b")]
    run = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert run["status"] == "success", run
    assert run["punches_new"] == 2 and len(odoo.attendances) == 1
    devices = api.get("/api/v1/devices", headers=head(token)).json()
    assert [d["serial_number"] for d in devices] == ["FA1234567"]
    again = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert again["punches_new"] == 0
