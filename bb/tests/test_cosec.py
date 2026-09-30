"""Matrix COSEC terminals, against a simulated device: Basic auth, an event
log addressed by (roll-over, sequence) that wraps, and one-at-a-time users."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta

import httpx
import pytest

from app.integrations.base import EmployeeRecord, ProviderError, SourceConfig
from app.integrations.providers import cosec as cm
from app.integrations.providers.cosec import CosecProvider, parse_events
from tests.test_platform_admin import api, head, signup  # noqa: F401

USER, PASSWORD, TZ = "admin", "1234", "Asia/Kolkata"


class FakeCosec:
    """A ring buffer of `size` events per roll-over, like the real log."""

    def __init__(self, size=1000):
        self.size = size
        self.log: list[dict] = []          # ordered, each with rollover/seq
        self.users: dict[str, str] = {"1001": "Jane Doe"}
        self.requests: list[dict] = []

    def add(self, when, event_id=101, user="1001", exit_=0):
        n = len(self.log)
        self.log.append({"rollover": n // self.size, "seq": n % self.size + 1, "when": when,
                         "event_id": event_id, "user": user, "exit": exit_})

    def _xml(self, rows):
        body = "".join(
            f"<Events><roll-over-count>{r['rollover']}</roll-over-count><seq-No>{r['seq']}</seq-No>"
            f"<date>{r['when'].day}/{r['when'].month}/{r['when'].year}</date>"
            f"<time>{r['when']:%H:%M:%S}</time><event-id>{r['event_id']}</event-id>"
            f"<detail-1>{r['user']}</detail-1><detail-2>0</detail-2><detail-3>{r['exit']}</detail-3>"
            f"<detail-4>0</detail-4><detail-5>0</detail-5></Events>" for r in rows)
        return f"<COSEC_API>{body}</COSEC_API>"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, text="Unauthorized")
        p = dict(request.url.params)
        self.requests.append({"path": request.url.path, **p})
        if request.url.path == "/device.cgi/device-basic-config":
            return httpx.Response(200, text="<COSEC_API><name>Main Gate ARGO</name><app>1</app></COSEC_API>")
        if request.url.path == "/device.cgi/events":
            start = next((i for i, r in enumerate(self.log)
                          if (r["rollover"], r["seq"]) >= (int(p["roll-over-count"]), int(p["seq-number"]))), None)
            if start is None or self.log[start]["rollover"] != int(p["roll-over-count"]):
                return httpx.Response(200, text="<COSEC_API><Response-Code>10</Response-Code></COSEC_API>")
            return httpx.Response(200, text=self._xml(self.log[start:start + int(p["no-of-events"])]))
        if request.url.path == "/device.cgi/users":
            if p["action"] == "get":
                if p["user-id"] in self.users:
                    return httpx.Response(200, text=f"<COSEC_API><user-id>{p['user-id']}</user-id>"
                                                    f"<name>{self.users[p['user-id']]}</name></COSEC_API>")
                return httpx.Response(200, text="<COSEC_API><Response-Code>10</Response-Code></COSEC_API>")
            self.users[p["user-id"]] = p["name"]
            return httpx.Response(200, text="<COSEC_API><Response-Code>0</Response-Code></COSEC_API>")
        return httpx.Response(404)


@pytest.fixture
def device(monkeypatch):
    fake = FakeCosec()
    monkeypatch.setattr(cm, "TRANSPORT", httpx.MockTransport(fake))
    return fake


def provider(password=PASSWORD, cursor=None):
    return CosecProvider(SourceConfig(base_url="http://10.0.0.80", username=USER, password=password,
                                      timezone=TZ, options={"cosec_cursor": cursor} if cursor else {}))


def test_parses_the_documented_sample():
    rows = parse_events("<COSEC_API><Events><roll-over-count>0</roll-over-count><seq-No>1</seq-No>"
                        "<date>16/4/2014</date><time>14:56:20</time><event-id>101</event-id>"
                        "<detail-1>5</detail-1><detail-2>0</detail-2><detail-3>0</detail-3></Events></COSEC_API>")
    assert rows == [{"roll-over-count": "0", "seq-No": "1", "date": "16/4/2014", "time": "14:56:20",
                     "event-id": "101", "detail-1": "5", "detail-2": "0", "detail-3": "0"}]


def test_connects_and_names_the_device(device):
    info = provider().test_connection()
    assert info.ok and "Main Gate ARGO" in info.message
    assert list(provider().fetch_terminals())[0].serial_number == "10.0.0.80"
    with pytest.raises(ProviderError, match="username or password"):
        provider("wrong").test_connection()


def test_reads_the_log_in_pages_and_keeps_only_allowed_users(device):
    base = datetime(2026, 9, 29, 8, 0)
    for i in range(250):
        device.add(base + timedelta(minutes=i), exit_=i % 2)
    device.add(base, event_id=151, user="0")         # a door event
    device.add(base, event_id=154, user="1002")      # user denied
    p = provider()
    punches = list(p.fetch_punches())
    assert len(punches) == 250
    assert len([r for r in device.requests if r["path"] == "/device.cgi/events"]) >= 3
    first = punches[0]
    assert first.emp_code == "1001" and first.external_id == "10.0.0.80:0:1"
    assert first.punch_time_local == base and first.direction is True
    assert punches[1].direction is False
    assert p.config_updates["cosec_cursor"] == {"rollover": 0, "seq": 252}, "past the skipped events too"


def test_the_cursor_reads_only_whats_new_and_crosses_a_roll_over(monkeypatch):
    fake = FakeCosec(size=10)
    monkeypatch.setattr(cm, "TRANSPORT", httpx.MockTransport(fake))
    base = datetime(2026, 9, 29, 8, 0)
    for i in range(8):
        fake.add(base + timedelta(minutes=i))
    first = provider()
    assert len(list(first.fetch_punches())) == 8
    cursor = first.config_updates["cosec_cursor"]
    assert cursor == {"rollover": 0, "seq": 8}

    for i in range(8, 15):                     # wraps: seq 9, 10, then roll-over 1 seq 1..5
        fake.add(base + timedelta(minutes=i))
    second = provider(cursor=cursor)
    got = list(second.fetch_punches())
    assert [p.external_id for p in got] == ["10.0.0.80:0:9", "10.0.0.80:0:10"] + \
        [f"10.0.0.80:1:{n}" for n in range(1, 6)]
    assert second.config_updates["cosec_cursor"] == {"rollover": 1, "seq": 5}
    assert list(provider(cursor=second.config_updates["cosec_cursor"]).fetch_punches()) == []


def test_creates_users_but_not_twice(device):
    p = provider()
    p.create_employee(EmployeeRecord(external_id=None, emp_code="2002", first_name="Anu", last_name="Raj"))
    assert device.users["2002"] == "Anu Raj"
    sets = [r for r in device.requests if r["path"] == "/device.cgi/users" and r["action"] == "set"]
    p.create_employee(EmployeeRecord(external_id=None, emp_code="1001"))
    assert len([r for r in device.requests if r.get("action") == "set"]) == len(sets), "1001 already exists"
    with pytest.raises(ProviderError, match="numeric"):
        p.create_employee(EmployeeRecord(external_id=None, emp_code="EMP7"))


def test_sync_saves_the_cursor_and_writes_attendance(api, device, monkeypatch):
    from sqlalchemy import select
    from zoneinfo import ZoneInfo

    from app.core.crypto import encrypt
    from app.models import DeviceSource, OdooConnection, Tenant
    from app.services import sync_engine as engine_mod
    from tests.conftest import FakeOdoo

    token = signup(api, "Acme", "owner@acme.example.com")
    source = api.post("/api/v1/sources", headers=head(token), json={
        "name": "Main gate", "provider": "cosec", "connection_kind": "device",
        "base_url": "http://10.0.0.80", "username": USER, "password": PASSWORD,
        "server_timezone": TZ}).json()
    assert source["status"] == "connected", source
    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://acme.odoo.com", db_name="acme",
                         username="bot@acme.com", api_key_enc=encrypt("key", tenant.crypto_key), is_active=True))
    s.commit(); s.close()
    odoo = FakeOdoo()
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)

    now = datetime.now(ZoneInfo(TZ)).replace(tzinfo=None, microsecond=0)
    device.add(now - timedelta(hours=3), exit_=0)
    device.add(now - timedelta(hours=2), event_id=151, user="0")
    device.add(now - timedelta(hours=1), exit_=1)
    run = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert run["status"] == "success", run
    assert run["punches_new"] == 2 and len(odoo.attendances) == 1

    s = api.session_factory()
    saved = s.get(DeviceSource, source["id"]).config
    s.close()
    assert saved["cosec_cursor"] == {"rollover": 0, "seq": 3}
    device.requests.clear()
    again = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert again["punches_new"] == 0
    first_read = [r for r in device.requests if r["path"] == "/device.cgi/events"][0]
    assert first_read["seq-number"] == "4", "resumes after the cursor, not from the start"
