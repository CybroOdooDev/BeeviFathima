"""Matrix COSEC CENTRA, against a simulated server: semicolon-separated
queries, sa-only Basic auth, a window read one day at a time, and two
different customer-defined API templates."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta

import httpx
import pytest

from app.integrations.base import ProviderError, SourceConfig
from app.integrations.providers import cosec_centra as cc
from app.integrations.providers.cosec_centra import CosecCentraProvider, parse_when, records
from tests.test_platform_admin import api, head, signup  # noqa: F401

PASSWORD, TZ = "matrix@123", "Asia/Kolkata"
FMT = "%d%m%Y%H%M%S"


def template_a(e):
    """A template with one date-time column and an index."""
    return (f"<EventTA><IndexNo>{e['index']}</IndexNo><UserID>{e['user']}</UserID>"
            f"<UserName>{e['name']}</UserName><EventDateTime>{e['when']:%d/%m/%Y %H:%M:%S}</EventDateTime>"
            f"<EntryExitType>{e['exit']}</EntryExitType><DeviceName>{e['device']}</DeviceName></EventTA>")


def template_b(e):
    """Another customer's template: split date and time, employee code, no index."""
    return (f"<Row><EmpCode>{e['user']}</EmpCode><EDate>{e['when']:%d-%m-%Y}</EDate>"
            f"<ETime>{e['when']:%H:%M:%S}</ETime><IOType>{'OUT' if e['exit'] else 'IN'}</IOType>"
            f"<Panel>{e['device']}</Panel></Row>")


def template_bare(e):
    """A template that forgot the user id."""
    return f"<Row><EventDateTime>{e['when']:%d/%m/%Y %H:%M:%S}</EventDateTime><Door>Gate</Door></Row>"


class FakeCentra:
    #: How the simulated server reads eventta's index: "from" it, "after" it,
    #: "ignore" it (every event), or "none" (no eventta endpoint at all).
    index_mode = "from"

    def __init__(self, template=template_a):
        self.template = template
        self.index_reads: list[int] = []
        self.events: list[dict] = []
        self.windows: list[tuple[datetime, datetime]] = []
        self.urls: list[str] = []

    def add(self, when, user="1001", exit_=0, device="Main Gate"):
        self.events.append({"index": len(self.events) + 1, "user": user, "name": "Jane Doe",
                            "when": when, "exit": exit_, "device": device})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        expected = "Basic " + base64.b64encode(f"sa:{PASSWORD}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, text="Unauthorized")
        query = request.url.query.decode()
        assert "&" not in query, "COSEC wants semicolons, not ampersands"
        params = dict(part.split("=", 1) for part in query.split(";"))
        if request.url.path == "/cosec/api.svc/eventta" and self.index_mode != "none":
            index, count = int(params["index"]), int(params["count"])
            self.index_reads.append(index)
            keep = {"from": lambda e: e["index"] >= index, "after": lambda e: e["index"] > index,
                    "ignore": lambda e: True}[self.index_mode]
            picked = sorted((e for e in self.events if keep(e)), key=lambda e: e["index"])[:count]
            return httpx.Response(200, text=f"<COSEC_API>{''.join(self.template(e) for e in picked)}</COSEC_API>")
        if request.url.path != "/cosec/api.svc/event-ta-date":
            return httpx.Response(404, text="Endpoint not found.")
        assert params["action"] == "get" and params["format"] == "xml"
        lo, hi = (datetime.strptime(x, FMT) for x in params["daterange"].split("-"))
        self.windows.append((lo, hi))
        rows = "".join(self.template(e) for e in self.events if lo <= e["when"] <= hi)
        return httpx.Response(200, text=f"<COSEC_API>{rows}</COSEC_API>",
                              headers={"content-type": "application/xml"})


@pytest.fixture
def server(monkeypatch):
    fake = FakeCentra()
    monkeypatch.setattr(cc, "TRANSPORT", httpx.MockTransport(fake))
    return fake


def provider(base="http://cosec-srv/cosec", password=PASSWORD, username="sa"):
    return CosecCentraProvider(SourceConfig(base_url=base, username=username, password=password, timezone=TZ))


def test_the_api_address_is_built_from_the_server_url():
    assert provider("http://cosec-srv/cosec").api == "http://cosec-srv/cosec/api.svc"
    assert provider("cosec-srv").api == "http://cosec-srv/cosec/api.svc"
    assert provider("http://10.0.0.5:8080/cosec/api.svc/").api == "http://10.0.0.5:8080/cosec/api.svc"


def test_record_parsing_tolerates_templates():
    rows = records("<COSEC_API><Row><User-ID>7</User-ID><Event-Date>01/10/2026</Event-Date>"
                   "<Event-Time>09:05</Event-Time></Row></COSEC_API>")
    assert rows == [{"userid": "7", "eventdate": "01/10/2026", "eventtime": "09:05"}]
    assert parse_when(rows[0]) == datetime(2026, 10, 1, 9, 5)
    with pytest.raises(ProviderError, match="isn't XML"):
        records("<html")
    with pytest.raises(ProviderError, match="error code 13"):
        records("<COSEC_API><Response-Code>13</Response-Code><Message>Template not found</Message></COSEC_API>")
    assert records("<COSEC_API><Response-Code>0</Response-Code></COSEC_API>") == []


def test_connects_and_counts_the_last_day(server):
    server.add(datetime.now() - timedelta(hours=2))
    info = provider().test_connection()
    assert info.ok and "Connected to COSEC" in info.message
    assert ";daterange=" in server.urls[-1]


def test_refusals_explain_themselves(server):
    with pytest.raises(ProviderError, match="only the System Administrator"):
        provider(password="wrong").test_connection()
    with pytest.raises(ProviderError, match="No COSEC API"):
        provider("http://cosec-srv/other").test_connection()


def test_a_template_missing_the_user_id_is_flagged(server):
    server.template = template_bare
    server.add(datetime.now() - timedelta(hours=1))
    info = provider().test_connection()
    assert not info.ok and "API Configuration" in info.message and "User ID" in info.message


def test_reads_the_window_a_day_at_a_time(server):
    start = datetime(2026, 9, 26, 0, 0)
    for day in range(3):
        server.add(start + timedelta(days=day, hours=9), exit_=0)
        server.add(start + timedelta(days=day, hours=18), exit_=1)
    punches = list(provider().fetch_punches(start, start + timedelta(days=3)))
    assert len(server.windows) == 3 and all(hi - lo == timedelta(days=1) for lo, hi in server.windows)
    assert len(punches) == 6
    first = punches[0]
    assert first.emp_code == "1001" and first.punch_time_local == datetime(2026, 9, 26, 9, 0)
    assert first.direction is True and punches[1].direction is False
    assert first.external_id == "cosec-srv:1" and first.terminal_sn == "Main Gate"
    assert (first.first_name, first.last_name) == ("Jane", "Doe")


def test_a_second_template_without_an_index(monkeypatch):
    fake = FakeCentra(template_b)
    monkeypatch.setattr(cc, "TRANSPORT", httpx.MockTransport(fake))
    fake.add(datetime(2026, 9, 29, 8, 30), user="E42", exit_=0, device="Panel 2")
    fake.add(datetime(2026, 9, 29, 17, 45), user="E42", exit_=1, device="Panel 2")
    a, b = provider().fetch_punches(datetime(2026, 9, 29), datetime(2026, 9, 30))
    assert a.emp_code == "E42" and a.punch_time_local == datetime(2026, 9, 29, 8, 30)
    assert a.direction is True and b.direction is False
    assert a.external_id == "cosec-srv:E42:20260929083000:Panel 2"


def test_a_long_backfill_is_capped(server):
    end = datetime(2026, 9, 30)
    list(provider().fetch_punches(end - timedelta(days=200), end))
    assert len(server.windows) == cc.MAX_DAYS


def test_sync_writes_attendance_and_learns_panels(api, server, monkeypatch):
    from sqlalchemy import select
    from zoneinfo import ZoneInfo

    from app.core.crypto import encrypt
    from app.models import OdooConnection, Tenant
    from app.services import sync_engine as engine_mod
    from tests.conftest import FakeOdoo

    token = signup(api, "Acme", "owner@acme.example.com")
    now = datetime.now(ZoneInfo(TZ)).replace(tzinfo=None, microsecond=0)
    server.add(now - timedelta(hours=3), exit_=0)
    server.add(now - timedelta(hours=1), exit_=1)
    source = api.post("/api/v1/sources", headers=head(token), json={
        "name": "COSEC", "provider": "cosec_centra", "connection_kind": "platform",
        "base_url": "http://cosec-srv/cosec", "username": "sa", "password": PASSWORD,
        "server_timezone": TZ}).json()
    assert source["status"] == "connected", source
    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://acme.odoo.com", db_name="acme",
                         username="bot@acme.com", api_key_enc=encrypt("key", tenant.crypto_key), is_active=True))
    s.commit(); s.close()
    odoo = FakeOdoo(employees={"1001": (11, "Jane Doe")})
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)

    run = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert run["status"] == "success", run
    assert run["punches_new"] == 2 and len(odoo.attendances) == 1
    devices = api.get("/api/v1/devices", headers=head(token)).json()
    assert [d["serial_number"] for d in devices] == ["Main Gate"]
    again = api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert again["punches_new"] == 0, "the overlap re-reads, the ledger dedupes"


# -- by index: late events whatever their time ---------------------------------
def test_time_reads_carry_the_index_as_record_id(server):
    server.add(datetime(2026, 10, 1, 9, 0))
    (event,) = provider().fetch_punches(datetime(2026, 10, 1), datetime(2026, 10, 2))
    assert event.record_id == 1 and event.external_id == "cosec-srv:1"


@pytest.mark.parametrize("mode", ["from", "after"])
def test_reads_events_stored_after_an_index_whatever_their_time(server, mode):
    server.index_mode = mode
    server.add(datetime(2026, 10, 1, 9, 0))                    # 1, already stored
    server.add(datetime(2026, 9, 20, 8, 0), user="1002")      # 2, a panel's late backlog
    server.add(datetime(2026, 10, 1, 9, 30))                   # 3
    p = provider()
    events = list(p.fetch_punches_after(1, datetime(2026, 9, 1), datetime(2026, 10, 2)))
    assert [(e.record_id, e.emp_code) for e in events] == [(2, "1002"), (3, "1001")]
    assert server.index_reads[0] == 1, "asked from the last stored index, so 'after' skips nothing"
    assert p.record_cursor_until == 3


def test_a_large_backlog_pages_and_stops_at_the_run_budget(server, monkeypatch):
    monkeypatch.setattr(cc, "INDEX_PAGE", 10)
    monkeypatch.setattr(cc, "MAX_INDEX_PAGES", 2)
    for i in range(35):
        server.add(datetime(2026, 10, 1, 8, 0) + timedelta(minutes=i))
    p = provider()
    events = list(p.fetch_punches_after(0, datetime(2026, 9, 1), datetime(2026, 10, 2)))
    assert [e.record_id for e in events] == list(range(1, 20))
    assert p.record_cursor_until == 19, "the next run carries on from here"
    rest = list(provider().fetch_punches_after(19, datetime(2026, 9, 1), datetime(2026, 10, 2)))
    assert [e.record_id for e in rest][:1] == [20]


def test_rows_without_a_user_still_move_the_cursor(server):
    server.add(datetime(2026, 10, 1, 9, 0), user="")
    p = provider()
    assert list(p.fetch_punches_after(0, datetime(2026, 9, 1), datetime(2026, 10, 2))) == []
    assert p.record_cursor_until == 1


@pytest.mark.parametrize("mode,template,why", [
    ("ignore", template_a, "ignored the index"),
    ("none", template_a, "no index read"),
    ("from", template_b, "no Index No column"),
])
def test_servers_that_cannot_read_by_index_are_reported(monkeypatch, mode, template, why):
    from app.integrations.base import IdFilterUnsupported

    fake = FakeCentra(template=template)
    fake.index_mode = mode
    monkeypatch.setattr(cc, "TRANSPORT", httpx.MockTransport(fake))
    for i in range(3):
        fake.add(datetime(2026, 10, 1, 9, i))
    with pytest.raises(IdFilterUnsupported, match=why):
        list(provider().fetch_punches_after(2, datetime(2026, 9, 1), datetime(2026, 10, 2)))


def test_sync_picks_up_a_panels_late_backlog_by_index(api, server, monkeypatch):
    from sqlalchemy import select
    from zoneinfo import ZoneInfo

    from app.core.crypto import encrypt
    from app.models import DeviceSource, OdooConnection, Tenant
    from app.services import sync_engine as engine_mod
    from tests.conftest import FakeOdoo

    token = signup(api, "Acme", "owner@acme.example.com")
    now = datetime.now(ZoneInfo(TZ)).replace(tzinfo=None, microsecond=0)
    server.add(now - timedelta(hours=3))
    source = api.post("/api/v1/sources", headers=head(token), json={
        "name": "COSEC", "provider": "cosec_centra", "connection_kind": "platform",
        "base_url": "http://cosec-srv/cosec", "username": "sa", "password": PASSWORD,
        "server_timezone": TZ}).json()
    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(OdooConnection(tenant_id=tenant.id, name="Odoo", url="https://acme.odoo.com", db_name="acme",
                         username="bot@acme.com", api_key_enc=encrypt("key", tenant.crypto_key), is_active=True))
    s.commit(); s.close()
    monkeypatch.setattr(engine_mod, "build_odoo_client",
                        lambda t, c: FakeOdoo(employees={"1001": (11, "Jane"), "1002": (12, "Sam")}))
    sync = lambda: api.post(f"/api/v1/sources/{source['id']}/sync", headers=head(token)).json()
    assert sync()["punches_new"] == 1

    server.add(now - timedelta(days=2), user="1002")   # an offline panel hands over yesterday's punch
    server.add(now - timedelta(minutes=20))
    run = sync()
    assert run["status"] == "success" and run["punches_new"] == 2, run
    s = api.session_factory()
    config = s.get(DeviceSource, source["id"]).config
    s.close()
    assert config.get("record_cursor") == 3 and not config.get("record_cursor_unsupported")
