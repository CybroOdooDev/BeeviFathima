"""Anviz CrossChex Cloud, against a simulated service: one-body requests
(header / authorize / payload), token expiry, paged records."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.integrations.base import ProviderError, SourceConfig
from app.integrations.providers import crosschex as cx
from app.integrations.providers.crosschex import CrossChexProvider
from tests.test_platform_admin import api, head, signup  # noqa: F401

KEY, SECRET, TZ = "ak_live_123", "sk_live_456", "Asia/Dubai"
US = "https://api.us.crosschexcloud.com"


class FakeCrossChex:
    def __init__(self):
        self.tokens: set[str] = set()
        self.issued = 0
        self.records: list[dict] = []
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/", "every call goes to the API root"
        body = json.loads(request.content)
        header = body["header"]
        assert header["version"] == "1.0" and header["requestId"] and header["timestamp"]
        self.calls.append(body)
        if (header["nameSpace"], header["nameAction"]) == ("authorize.token", "token"):
            if body["payload"] != {"api_key": KEY, "api_secret": SECRET}:
                return httpx.Response(200, json={"code": 400, "message": "api_key or api_secret error"})
            self.issued += 1
            token = f"jwt{self.issued}"
            self.tokens.add(token)
            return httpx.Response(200, json={"code": 200, "data": {"payload": {
                "token": token, "expires": "2030-01-01T00:00:00+00:00"}}})
        if (body.get("authorize") or {}).get("token") not in self.tokens:
            return httpx.Response(200, json={"code": 401, "message": "token expired"})
        if (header["nameSpace"], header["nameAction"]) == ("attendance.record", "getrecord"):
            p = body["payload"]
            lo, hi = datetime.fromisoformat(p["begin_time"]), datetime.fromisoformat(p["end_time"])
            rows = [r for r in self.records if lo <= datetime.fromisoformat(r["checktime"]) <= hi]
            per = p["per_page"]
            pages = max(1, -(-len(rows) // per))
            chunk = rows[(p["page"] - 1) * per: p["page"] * per]
            return httpx.Response(200, json={"code": 200, "data": {"payload": {
                "count": len(rows), "pageCount": pages, "page": p["page"], "perPage": per, "list": chunk}}})
        return httpx.Response(200, json={"code": 404, "message": "unknown action"})


def record(workno, when_utc, checktype=128, serial="1750120622290025"):
    return {"checktype": checktype, "checktime": when_utc.isoformat(timespec="seconds"),
            "device": {"serial_number": serial, "name": "FaceDeep3-IRT10"},
            "employee": {"workno": workno, "first_name": "John", "last_name": "Doe"}}


@pytest.fixture
def cloud(monkeypatch):
    fake = FakeCrossChex()
    monkeypatch.setattr(cx, "TRANSPORT", httpx.MockTransport(fake))
    return fake


def provider(secret=SECRET, token=None):
    return CrossChexProvider(SourceConfig(base_url=US, username=KEY, password=secret, timezone=TZ, token=token))


def test_connects_with_the_api_key(cloud):
    info = provider().test_connection()
    assert info.ok and "CrossChex Cloud" in info.message
    with pytest.raises(ProviderError, match="refused the API key or secret"):
        provider("wrong").test_connection()


def test_pages_records_and_maps_them(cloud, monkeypatch):
    monkeypatch.setattr(cx, "PAGE_SIZE", 10)
    base = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)      # 08:00 in Dubai
    cloud.records = [record("1", base + timedelta(minutes=i), checktype=i % 2) for i in range(25)]
    punches = list(provider().fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0)))
    assert len(punches) == 25
    reads = [c for c in cloud.calls if c["header"]["nameAction"] == "getrecord"]
    assert [c["payload"]["page"] for c in reads] == [1, 2, 3]
    assert reads[0]["payload"]["begin_time"] == "2026-09-29T00:00:00+04:00"
    first = punches[0]
    assert first.emp_code == "1" and first.punch_time_local == datetime(2026, 9, 29, 8, 0)
    assert first.direction is True and punches[1].direction is False
    assert first.terminal_sn == "1750120622290025" and first.terminal_alias == "FaceDeep3-IRT10"
    assert first.external_id == "1750120622290025:1:20260929040000", "keyed on UTC, so a timezone change is harmless"


def test_unknown_checktype_leaves_direction_to_pairing(cloud):
    cloud.records = [record("7", datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc), checktype=128)]
    (p,) = provider().fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0))
    assert p.direction is None


def test_an_expired_token_is_renewed_once(cloud):
    cloud.records = [record("1", datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc))]
    stale = provider(token="jwt-old")
    assert len(list(stale.fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0)))) == 1
    assert stale.cached_token == "jwt1" and cloud.issued == 1


def test_two_accounts_in_one_region_are_two_connections(api, cloud):
    token = signup(api, "Acme", "owner@acme.example.com")
    body = {"provider": "crosschex", "connection_kind": "platform", "base_url": US,
            "username": KEY, "password": SECRET, "server_timezone": TZ}
    assert api.post("/api/v1/sources", headers=head(token), json={**body, "name": "Anviz A"}).status_code == 201
    again = api.post("/api/v1/sources", headers=head(token), json={**body, "name": "Anviz again"})
    assert again.status_code == 409, "same key twice is refused"
    other = api.post("/api/v1/sources", headers=head(token),
                     json={**body, "name": "Anviz B", "username": "ak_live_other"})
    assert other.status_code == 201, "a different account in the same region is fine"


def test_sync_brings_terminals_in_with_the_punches(api, cloud, monkeypatch):
    from sqlalchemy import select

    from app.core.crypto import encrypt
    from app.models import OdooConnection, Tenant
    from app.services import sync_engine as engine_mod
    from tests.conftest import FakeOdoo

    token = signup(api, "Acme", "owner@acme.example.com")
    source = api.post("/api/v1/sources", headers=head(token), json={
        "name": "Anviz", "provider": "crosschex", "connection_kind": "platform", "base_url": US,
        "username": KEY, "password": SECRET, "server_timezone": TZ}).json()
    assert source["status"] == "connected", source
    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://acme.odoo.com", db_name="acme",
                         username="bot@acme.com", api_key_enc=encrypt("key", tenant.crypto_key), is_active=True))
    s.commit(); s.close()
    odoo = FakeOdoo(employees={"1": (11, "John Doe")})
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    cloud.records = [record("1", now - timedelta(hours=3), checktype=0),
                     record("1", now - timedelta(hours=1), checktype=1)]
    run = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert run["status"] == "success", run
    assert run["punches_new"] == 2 and len(odoo.attendances) == 1
    devices = api.get("/api/v1/devices", headers=head(token)).json()
    assert [d["serial_number"] for d in devices] == ["1750120622290025"]
