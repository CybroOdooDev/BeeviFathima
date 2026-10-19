"""BioTime late uploads, found by the upload_time filter.

A terminal that was offline uploads its backlog later with the punch times it
recorded, so those punches fall before the next sync's punch-time window.
BioTime records when it received each one (upload_time); asking from the start
of the window by upload time finds them. How a server treats the filter is
checked against what it returns and remembered per connection.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.integrations.base import SourceConfig
from app.integrations.providers import biotime as bt
from app.integrations.providers.biotime import BioTimeProvider
from app.models import DeviceSource, PunchRecord
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo

TZ = "Asia/Dubai"
FMT = "%Y-%m-%d %H:%M:%S"
NOW = datetime.utcnow() + timedelta(hours=4)


class Server:
    """BioTime's transactions endpoint; ``upload_mode`` is how it treats
    upload_time: "from" (>=), "exact" (==) or "ignores"."""

    def __init__(self, upload_mode="from"):
        self.rows: list[dict] = []
        self.upload_mode = upload_mode
        self.calls: list[dict] = []

    def add(self, pid, punched, uploaded=None):
        self.rows.append({"id": pid, "emp_code": "1001", "punch_time": punched.strftime(FMT),
                          "upload_time": (uploaded or punched).strftime(FMT),
                          "punch_state": "0", "terminal_sn": "T1"})

    def get(self, path, params=None):
        params = params or {}
        detail = path.rstrip("/").rsplit("/", 1)[-1]
        if detail.isdigit():                       # /iclock/api/transactions/<id>/
            self.calls.append({"id": int(detail)})
            row = next((r for r in self.rows if r["id"] == int(detail)), None)
            if row is None:
                raise bt.BioTimeNotFound("404")
            return row
        self.calls.append(dict(params))
        rows = list(self.rows)
        if params.get("start_time"):
            rows = [r for r in rows if r["punch_time"] >= params["start_time"]]
        if params.get("end_time"):
            rows = [r for r in rows if r["punch_time"] <= params["end_time"]]
        if params.get("upload_time"):
            value = params["upload_time"]
            if self.upload_mode == "from":
                rows = [r for r in rows if r["upload_time"] >= value]
            elif self.upload_mode == "exact":
                rows = [r for r in rows if r["upload_time"] == value]
        return {"data": rows, "next": None}


def _provider(server, options=None):
    p = BioTimeProvider(SourceConfig(base_url="https://bio.test", username="u", password="p",
                                     timezone=TZ, options=dict(options or {})))
    p._get = server.get
    return p


def _window():
    return NOW - timedelta(minutes=30), NOW + timedelta(minutes=5)


def test_a_late_upload_with_an_old_punch_time_is_found():
    server = Server()
    server.add(1, NOW - timedelta(minutes=10))
    server.add(2, NOW - timedelta(days=1), uploaded=NOW - timedelta(minutes=5))   # offline terminal
    p = _provider(server)
    ids = [e.external_id for e in p.fetch_punches(*_window())]
    assert ids == ["1", "2"]
    assert p.config_updates[bt.UPLOAD_FILTER_KEY] == "from"


def test_punches_in_both_reads_are_returned_once():
    server = Server()
    server.add(1, NOW - timedelta(minutes=10))
    ids = [e.external_id for e in _provider(server).fetch_punches(*_window())]
    assert ids == ["1"]


@pytest.mark.parametrize("mode", ["ignores", "exact"])
def test_a_server_that_does_not_filter_from_upload_time_is_switched_off(mode):
    server = Server(upload_mode=mode)
    since = _window()[0]
    server.add(1, NOW - timedelta(days=2))                           # ignored filter returns this
    server.add(2, NOW - timedelta(days=1), uploaded=since)           # exact match returns these two
    server.add(3, NOW - timedelta(days=1, hours=1), uploaded=since)
    p = _provider(server)
    late = [e.external_id for e in p.fetch_punches(*_window())]
    assert late == [], "nothing is trusted from a filter that does not filter"
    assert p.config_updates[bt.UPLOAD_FILTER_KEY] == "off"


def test_switched_off_means_not_asked_again():
    server = Server()
    p = _provider(server, {bt.UPLOAD_FILTER_KEY: "off"})
    list(p.fetch_punches(*_window()))
    assert not any("upload_time" in c for c in server.calls)


def test_an_old_backlog_slice_does_not_ask_by_upload_time():
    server = Server()
    p = _provider(server)
    list(p.fetch_punches(NOW - timedelta(days=10), NOW - timedelta(days=9)))
    assert not any("upload_time" in c for c in server.calls)


def test_end_to_end_the_late_upload_reaches_the_ledger(db, tenant, monkeypatch):
    server = Server()
    server.add(1, NOW - timedelta(hours=2))

    def build(t, s):
        return _provider(server, s.config)
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", build)
    engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    # The terminal comes back online and uploads yesterday's punch now.
    server.add(2, NOW - timedelta(days=1), uploaded=NOW - timedelta(minutes=1))
    engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert {p.external_id for p in db.scalars(select(PunchRecord))} == {"1", "2"}
    assert db.scalars(select(DeviceSource)).one().config[bt.UPLOAD_FILTER_KEY] == "from"


# --- the id-gap check: one transaction read by id -------------------------------
import re  # noqa: E402

from app.integrations.providers.biotime import BioTimeNotFound  # noqa: E402


class IdServer(Server):
    """Also serves /iclock/api/transactions/<id>/; can be told to ignore upload_time."""

    def __init__(self, upload_mode="ignores"):
        super().__init__(upload_mode)
        self.one_calls: list[int] = []

    def get(self, path, params=None):
        match = re.fullmatch(r"/iclock/api/transactions/(\d+)/", path)
        if match:
            txn_id = int(match.group(1))
            self.one_calls.append(txn_id)
            row = next((r for r in self.rows if r["id"] == txn_id), None)
            if row is None:
                raise BioTimeNotFound("not found")
            return {"code": 0, "msg": "", "data": row}
        return super().get(path, params or {})


def _engine(db, tenant, server, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: _provider(server, s.config))
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


def _ledger(db):
    return {p.external_id for p in db.scalars(select(PunchRecord))}


def _cursor(db):
    return db.scalars(select(DeviceSource)).one().config.get("record_cursor")


def test_a_gap_between_ids_is_read_on_its_own_and_is_the_late_upload(db, tenant, monkeypatch):
    server = IdServer()
    server.add(1, NOW - timedelta(hours=2))
    server.add(2, NOW - timedelta(hours=1, minutes=50))
    _engine(db, tenant, server, monkeypatch)
    assert _cursor(db) == 2

    server.add(3, NOW - timedelta(days=1))          # late upload, old punch time
    server.add(4, NOW - timedelta(minutes=5))       # an ordinary new punch
    _engine(db, tenant, server, monkeypatch)
    assert _ledger(db) == {"1", "2", "3", "4"}
    assert 3 in server.one_calls and 4 not in server.one_calls[:1], "only the gap is looked up first"
    assert _cursor(db) == 4


def test_a_late_upload_after_the_newest_id_is_found_by_reading_past_it(db, tenant, monkeypatch):
    server = IdServer()
    server.add(1, NOW - timedelta(hours=2))
    _engine(db, tenant, server, monkeypatch)
    server.add(2, NOW - timedelta(days=1))          # nothing newer arrived since
    _engine(db, tenant, server, monkeypatch)
    assert "2" in _ledger(db)
    assert server.one_calls[-2:] == [2, 3], "reads on until an id that does not exist"
    assert _cursor(db) == 2


def test_a_deleted_transaction_is_passed_over_once(db, tenant, monkeypatch):
    server = IdServer()
    server.add(1, NOW - timedelta(hours=2))
    _engine(db, tenant, server, monkeypatch)
    server.add(3, NOW - timedelta(minutes=5))       # id 2 was deleted on the server
    _engine(db, tenant, server, monkeypatch)
    assert _cursor(db) == 3
    server.one_calls.clear()
    _engine(db, tenant, server, monkeypatch)
    assert 2 not in server.one_calls


def test_the_lookups_are_capped_and_the_cursor_stops_where_they_stopped(db, tenant, monkeypatch):
    monkeypatch.setattr(bt, "MAX_ID_LOOKUPS", 2)
    server = IdServer()
    server.add(1, NOW - timedelta(hours=2))
    _engine(db, tenant, server, monkeypatch)
    for pid in (2, 3, 4, 5):
        server.add(pid, NOW - timedelta(days=2))   # four late uploads
    server.add(6, NOW - timedelta(minutes=5))
    _engine(db, tenant, server, monkeypatch)
    assert _cursor(db) == 3 and {"2", "3"} <= _ledger(db)
    _engine(db, tenant, server, monkeypatch)
    assert {"4", "5"} <= _ledger(db)


# --- single transactions by id: /iclock/api/transactions/<id>/ -------------------
def _id_calls(server):
    return [c["id"] for c in server.calls if "id" in c]


def _read(server, after_id, options=None):
    """One sync's reads for this provider: by time, then by id after ``after_id``."""
    p = _provider(server, {bt.UPLOAD_FILTER_KEY: "off", **(options or {})})
    by_time = [e.external_id for e in p.fetch_punches(*_window())]
    by_id = [e.external_id for e in p.fetch_punches_after(after_id)]
    return p, by_time, by_id


def test_a_skipped_id_is_read_on_its_own():
    """Upload-time filter off: the late punch is found through the gap in ids."""
    server = Server()
    server.add(3, NOW - timedelta(days=1))                 # late upload, old time
    server.add(4, NOW - timedelta(minutes=5))
    p, by_time, by_id = _read(server, after_id=2)
    assert by_time == ["4"] and by_id == ["3"]
    assert _id_calls(server)[0] == 3
    assert p.record_cursor_until == 4


def test_ids_after_the_newest_seen_are_probed_until_one_is_missing():
    server = Server()
    server.add(3, NOW - timedelta(days=1))                 # nothing recent, only a late upload
    p, by_time, by_id = _read(server, after_id=2)
    assert by_time == [] and by_id == ["3"] and _id_calls(server) == [3, 4]
    assert p.record_cursor_until == 3


def test_an_id_deleted_on_the_server_is_passed_over():
    server = Server()
    server.add(4, NOW - timedelta(minutes=5))              # id 3 was deleted in BioTime
    p, _, by_id = _read(server, after_id=2)
    assert by_id == [] and 3 in _id_calls(server)
    assert p.record_cursor_until == 4, "checked, and not looked up again"


def test_lookups_per_sync_are_bounded(monkeypatch):
    monkeypatch.setattr(bt, "MAX_ID_LOOKUPS", 3)
    server = Server()
    server.add(10, NOW - timedelta(minutes=5))             # ids 3..9 are gaps
    p, _, _ = _read(server, after_id=2)
    assert _id_calls(server) == [3, 4, 5]
    assert p.record_cursor_until == 5, "the rest is checked next sync"


def test_end_to_end_a_late_upload_is_found_by_id_when_upload_time_is_off(db, tenant, monkeypatch):
    server = Server(upload_mode="ignores")
    server.add(1, NOW - timedelta(hours=2))
    server.add(2, NOW - timedelta(hours=1))

    def build(t, s):
        return _provider(server, s.config)
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", build)
    engine_mod.SyncEngine(db, tenant, "test").run_cycle()          # notes the newest id

    server.add(3, NOW - timedelta(days=1))                          # offline terminal uploads late
    server.add(4, NOW - timedelta(minutes=2))
    engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert {p.external_id for p in db.scalars(select(PunchRecord))} == {"1", "2", "3", "4"}
    config = db.scalars(select(DeviceSource)).one().config
    assert config[bt.UPLOAD_FILTER_KEY] == "off" and config["record_cursor"] == 4
