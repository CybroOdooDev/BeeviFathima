"""Cams Biometrics gateway (Web API 3.0), against a simulated gateway: stgid in
the query, AuthToken in the body, LoadLog windows, GMT-offset times, status codes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.integrations.base import ProviderError, SourceConfig
from app.integrations.providers import cams as cm
from app.integrations.providers.cams import CamsProvider, direction_of, fmt_time, parse_time
from tests.test_platform_admin import api, head, signup  # noqa: F401

STG, TOKEN, TZ = "STG-1001", "COJJ7eiiPBGUfmIQPvh2PJWWDLX7OuKs", "Asia/Kolkata"
URL = "https://api.camsgateway.example/rest"


class FakeCams:
    def __init__(self):
        self.logs: list[dict] = []
        self.calls: list[dict] = []
        self.status_override: int | None = None

    def add(self, user, when_utc, kind="CheckIn", input_type="Face"):
        ist = when_utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.logs.append({"Type": kind, "InputType": input_type, "UserID": user, "Temperature": 36.8,
                          "FaceMask": False, "LogTime": fmt_time(ist), "_utc": when_utc})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        if request.url.params.get("stgid") != STG:
            return httpx.Response(200, json={"Status": "error", "StatusCode": 2, "OperationID": body["OperationID"]})
        if body.get("AuthToken") != TOKEN:
            return httpx.Response(200, json={"Status": "error", "StatusCode": 7, "OperationID": body["OperationID"]})
        assert body["OperationID"] and parse_time(body["Time"]) is not None
        if self.status_override:
            return httpx.Response(200, json={"Status": "error", "StatusCode": self.status_override,
                                             "OperationID": body["OperationID"]})
        if body.get("Load", {}).get("DeviceInformation") == "All":
            return httpx.Response(200, json={"Status": "done", "StatusCode": 0, "OperationID": body["OperationID"],
                                             "DeviceInformation": {"DeviceModel": "Hawking Plus (f38+)"}})
        f = body["Load"]["PunchLog"]["Filter"]
        lo, hi = parse_time(f["StartTime"]), parse_time(f["EndTime"])
        rows = [{k: v for k, v in r.items() if k != "_utc"} for r in self.logs if lo <= r["_utc"] <= hi]
        return httpx.Response(200, json={"Status": "done", "OperationID": body["OperationID"],
                                         "PunchLog": {"ReturnRowCount": str(len(rows)), "Log": rows}})


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeCams()
    monkeypatch.setattr(cm, "TRANSPORT", httpx.MockTransport(fake))
    return fake


def provider(token=TOKEN, stg=STG, base=URL):
    return CamsProvider(SourceConfig(base_url=base, username=stg, password=token, timezone=TZ))


def test_time_format_round_trips():
    moment = parse_time("2020-09-17 16:53:43 GMT +0530")
    assert moment.astimezone(timezone.utc) == datetime(2020, 9, 17, 11, 23, 43, tzinfo=timezone.utc)
    assert fmt_time(moment) == "2020-09-17 16:53:43 GMT +0530"
    assert fmt_time(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-3, minutes=-30)))) == "2026-01-01 00:00:00 GMT -0330"
    assert parse_time("garbage") is None
    assert (direction_of("CheckIn"), direction_of("MealOut"), direction_of("BreakIn"), direction_of("x")) == (True, False, True, None)


def test_the_stgid_pasted_with_the_url_is_not_doubled():
    assert provider(base=URL + "?stgid=OLD").endpoint == URL
    assert provider(base="api.camsgateway.example/rest").endpoint == URL


def test_connects_and_names_the_device(gateway):
    gateway.add("5", datetime.now(timezone.utc) - timedelta(hours=1))
    info = provider().test_connection()
    assert info.ok and "Hawking Plus" in info.message and "1 punch " in info.message + " "
    assert list(provider().fetch_terminals())[0].serial_number == STG


def test_gateway_status_codes_are_explained(gateway):
    with pytest.raises(ProviderError, match="AuthToken"):
        provider(token="wrong").test_connection()
    with pytest.raises(ProviderError, match="Service Tag ID"):
        provider(stg="nope").test_connection()
    gateway.status_override = 3
    with pytest.raises(ProviderError, match="allowed origin"):
        provider().test_connection()
    gateway.status_override = 38
    with pytest.raises(ProviderError, match="isn't allowed"):
        list(provider().fetch_punches(datetime(2026, 9, 29), datetime(2026, 9, 30)))


def test_reads_a_week_at_a_time_and_maps_punches(gateway):
    base = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)                     # 09:30 IST
    for day in range(0, 20, 3):
        gateway.add("5", base + timedelta(days=day), "CheckIn", "Fingerprint")
        gateway.add("5", base + timedelta(days=day, hours=9), "CheckOut")
    punches = list(provider().fetch_punches(datetime(2026, 9, 1), datetime(2026, 9, 21)))
    loads = [c for c in gateway.calls if "PunchLog" in c.get("Load", {})]
    assert len(loads) == 3, "20 days in 7-day chunks"
    assert len(punches) == 14
    first = punches[0]
    assert first.emp_code == "5" and first.punch_time_local == datetime(2026, 9, 1, 9, 30)
    assert first.direction is True and punches[1].direction is False
    assert first.terminal_sn == STG and first.verify_type == "Fingerprint"
    assert first.external_id == "STG-1001:5:20260901040000"


def test_the_same_stgid_cannot_be_added_twice(api, gateway):
    token = signup(api, "Acme", "owner@acme.example.com")
    body = {"provider": "cams", "connection_kind": "device", "base_url": URL, "username": STG,
            "password": TOKEN, "server_timezone": TZ}
    assert api.post("/api/v1/sources", headers=head(token), json={**body, "name": "Door 1"}).status_code == 201
    assert api.post("/api/v1/sources", headers=head(token), json={**body, "name": "Door 1 again"}).status_code == 409


def test_sync_registers_the_terminal_and_writes_attendance(api, gateway, monkeypatch):
    from sqlalchemy import select

    from app.core.crypto import encrypt
    from app.models import OdooConnection, Tenant
    from app.services import sync_engine as engine_mod
    from tests.conftest import FakeOdoo

    token = signup(api, "Acme", "owner@acme.example.com")
    source = api.post("/api/v1/sources", headers=head(token), json={
        "name": "Front door", "location": "Lobby", "provider": "cams", "connection_kind": "device",
        "base_url": URL, "username": STG, "password": TOKEN, "server_timezone": TZ}).json()
    assert source["status"] == "connected", source
    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://acme.odoo.com", db_name="acme",
                         username="bot@acme.com", api_key_enc=encrypt("key", tenant.crypto_key), is_active=True))
    s.commit(); s.close()
    odoo = FakeOdoo(employees={"5": (11, "Asha Nair")})
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    gateway.add("5", now - timedelta(hours=3), "CheckIn")
    gateway.add("5", now - timedelta(hours=1), "CheckOut")
    run = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert run["status"] == "success", run
    assert run["punches_new"] == 2 and len(odoo.attendances) == 1
    devices = api.get("/api/v1/devices", headers=head(token)).json()
    assert [d["serial_number"] for d in devices] == [STG]
    assert api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()["punches_new"] == 0
