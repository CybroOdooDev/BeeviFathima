"""Dahua terminals, against a simulated device that does real HTTP Digest (or
Basic, for old firmware), answers key=value replies, and pages recordFinder."""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.integrations.base import ProviderError, SourceConfig
from app.integrations.providers import dahua as dh
from app.integrations.providers.dahua import DahuaProvider, direction_of, parse_created, parse_kv, parse_records
from tests.test_platform_admin import api, head, signup  # noqa: F401

USER, PASSWORD, REALM, NONCE = "admin", "Dahua@123", "Login to ASI7214", "nonce-77a1"
SERIAL, MODEL, TZ = "9A0B1C2D3E4F5G6", "ASI7214S-W", "Asia/Dubai"


class FakeDahua:
    def __init__(self, basic_only=False, text_times=False):
        self.basic_only = basic_only
        self.text_times = text_times            # firmware that refuses epoch StartTime/EndTime
        self.records: list[dict] = []
        self.queries: list[dict] = []

    def add(self, user, when_utc, status="1", method="15", state=None, door="1", card=""):
        rec = {"RecNo": str(len(self.records) + 1), "CreateTime": int(when_utc.timestamp()), "UserID": user,
               "CardNo": card, "Method": method, "Status": status, "Door": door, "ReaderID": "1"}
        if state:
            rec["AttendanceState"] = state
        self.records.append(rec)

    def _authorised(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        if self.basic_only:
            return header == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if not header.startswith("Digest "):
            return False
        f = dict(re.findall(r'(\w+)="?([^",]+)"?', header[7:]))
        ha1 = hashlib.md5(f"{f['username']}:{REALM}:{PASSWORD}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{request.method}:{f['uri']}".encode()).hexdigest()
        want = hashlib.md5(f"{ha1}:{f['nonce']}:{f['nc']}:{f['cnonce']}:{f['qop']}:{ha2}".encode()).hexdigest()
        return f.get("username") == USER and f.get("response") == want

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self._authorised(request):
            challenge = (f'Basic realm="{REALM}"' if self.basic_only else
                         f'Digest realm="{REALM}", qop="auth", nonce="{NONCE}", opaque="x"')
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        p = dict(request.url.params)
        if request.url.path == "/cgi-bin/magicBox.cgi":
            if p["action"] == "getSystemInfo":
                return httpx.Response(200, text=f"serialNumber={SERIAL}\r\ndeviceType={MODEL}\r\n")
            return httpx.Response(200, text=f"sn={SERIAL}\r\n")
        if request.url.path == "/cgi-bin/recordFinder.cgi":
            self.queries.append(p)
            if p["name"] != "AccessControlCardRec":
                return httpx.Response(200, text="Error\r\nBad Request!\r\n")
            if self.text_times != (not p["StartTime"].isdigit()):
                return httpx.Response(200, text="Error\r\nBad Request!\r\n")
            if self.text_times:      # device-local text, as the firmware wants it
                zone = timezone(timedelta(hours=4))
                lo = datetime.strptime(p["StartTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone).timestamp()
                hi = datetime.strptime(p["EndTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone).timestamp()
            else:
                lo, hi = int(p["StartTime"]), int(p["EndTime"])
            rows = [r for r in self.records if lo <= r["CreateTime"] <= hi]
            page = rows[:int(p["count"])]
            lines = [f"totalCount={len(rows)}", f"found={len(page)}"]
            for i, r in enumerate(page):
                lines += [f"records[{i}].{k}={v}" for k, v in r.items()]
            return httpx.Response(200, text="\r\n".join(lines) + "\r\n")
        return httpx.Response(404)


@pytest.fixture
def device(monkeypatch):
    fake = FakeDahua()
    monkeypatch.setattr(dh, "TRANSPORT", httpx.MockTransport(fake))
    return fake


def provider(password=PASSWORD):
    return DahuaProvider(SourceConfig(base_url="http://10.0.0.108", username=USER, password=password, timezone=TZ))


def test_parsers():
    kv = parse_kv("serialNumber=ABC\r\ndeviceType=ASI7214\r\n\r\nnoise")
    assert kv == {"serialNumber": "ABC", "deviceType": "ASI7214"}
    total, rows = parse_records("totalCount=1000\nfound=2\nrecords[0].RecNo=1\nrecords[0].UserID=Zhang\n"
                                "records[1].RecNo=2\nrecords[1].UserID=Li\n")
    assert total == 1000 and rows == [{"RecNo": "1", "UserID": "Zhang"}, {"RecNo": "2", "UserID": "Li"}]
    zone = dh.ZoneInfo(TZ)
    assert parse_created("1790000000", zone) == datetime.fromtimestamp(1790000000, tz=timezone.utc)
    assert parse_created("2026-09-29 08:00:00", zone).utcoffset() == timedelta(hours=4)
    assert parse_created("nope", zone) is None
    assert direction_of({"AttendanceState": "CheckOut"}) is False and direction_of({"Type": "Entry"}) is True
    assert direction_of({}) is None


def test_connects_with_digest_and_names_the_device(device):
    device.add("1001", datetime.now(timezone.utc) - timedelta(hours=1))
    info = provider().test_connection()
    assert info.ok and MODEL in info.message and "1 access record" in info.message
    assert list(provider().fetch_terminals())[0].serial_number == SERIAL
    with pytest.raises(ProviderError, match="username or password"):
        provider("wrong").test_connection()


def test_old_firmware_that_only_answers_basic(monkeypatch):
    fake = FakeDahua(basic_only=True)
    monkeypatch.setattr(dh, "TRANSPORT", httpx.MockTransport(fake))
    assert provider().test_connection().ok
    with pytest.raises(ProviderError, match="username or password"):
        provider("wrong").test_connection()


def test_maps_records_and_skips_failures(device):
    base = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)              # 08:00 in Dubai
    device.add("1001", base, method="15", state="CheckIn")
    device.add("1001", base + timedelta(minutes=1), status="0")           # a failed attempt
    device.add("", base + timedelta(minutes=2), card="777")               # a card with no user
    device.add("1002", base + timedelta(minutes=3), method="6")           # fingerprint, no direction
    device.add("1001", base + timedelta(hours=9), method="1", state="CheckOut")
    punches = list(provider().fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0)))
    assert [p.emp_code for p in punches] == ["1001", "1002", "1001"]
    first = punches[0]
    assert first.punch_time_local == datetime(2026, 9, 29, 8, 0) and first.direction is True
    assert first.verify_type == "face" and punches[1].verify_type == "fingerprint" and punches[1].direction is None
    assert punches[2].direction is False and punches[2].verify_type == "card"
    assert first.terminal_sn == SERIAL and first.external_id == f"{SERIAL}:1"


def test_pages_by_last_create_time_and_drops_the_overlap(device, monkeypatch):
    monkeypatch.setattr(dh, "PAGE_SIZE", 10)
    base = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
    for i in range(25):
        device.add("1001", base + timedelta(minutes=i))
    punches = list(provider().fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0)))
    assert len(punches) == 25 and len({p.external_id for p in punches}) == 25
    starts = [int(q["StartTime"]) for q in device.queries]
    assert len(starts) == 3 and starts[1] == int((base + timedelta(minutes=9)).timestamp())


def test_a_day_at_a_time(device):
    start = datetime(2026, 9, 26, 0, 0)
    list(provider().fetch_punches(start, start + timedelta(days=3)))
    assert len(device.queries) == 3


def test_firmware_that_wants_local_text_times(monkeypatch):
    fake = FakeDahua(text_times=True)
    monkeypatch.setattr(dh, "TRANSPORT", httpx.MockTransport(fake))
    fake.add("1001", datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc))
    p = provider()
    (punch,) = p.fetch_punches(datetime(2026, 9, 29, 0, 0), datetime(2026, 9, 30, 0, 0))
    assert punch.punch_time_local == datetime(2026, 9, 29, 8, 0)
    assert not p._epoch and fake.queries[-1]["StartTime"] == "2026-09-29 00:00:00"


def test_sync_registers_the_terminal_and_writes_attendance(api, device, monkeypatch):
    from sqlalchemy import select

    from app.core.crypto import encrypt
    from app.models import OdooConnection, Tenant
    from app.services import sync_engine as engine_mod
    from tests.conftest import FakeOdoo

    token = signup(api, "Acme", "owner@acme.example.com")
    source = api.post("/api/v1/sources", headers=head(token), json={
        "name": "Lobby", "location": "Ground floor", "provider": "dahua", "connection_kind": "device",
        "base_url": "http://10.0.0.108", "username": USER, "password": PASSWORD,
        "server_timezone": TZ}).json()
    assert source["status"] == "connected", source
    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://acme.odoo.com", db_name="acme",
                         username="bot@acme.com", api_key_enc=encrypt("key", tenant.crypto_key), is_active=True))
    s.commit(); s.close()
    odoo = FakeOdoo(employees={"1001": (11, "Jane Doe")})
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    device.add("1001", now - timedelta(hours=3), state="CheckIn")
    device.add("1001", now - timedelta(hours=1), state="CheckOut")
    run = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert run["status"] == "success", run
    assert run["punches_new"] == 2 and len(odoo.attendances) == 1
    devices = api.get("/api/v1/devices", headers=head(token)).json()
    assert [d["serial_number"] for d in devices] == [SERIAL]
    assert api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()["punches_new"] == 0
