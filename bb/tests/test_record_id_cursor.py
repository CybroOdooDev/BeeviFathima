"""Reading by record id: a punch the platform stores late, with an old
timestamp, is still picked up — the time window alone would never see it.

The engine always reads by time first, so nothing depends on the id cursor
alone; the id read only adds what the time window missed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.integrations.base import IdFilterUnsupported, ProviderError, PunchEvent
from app.models import DeviceSource, PunchRecord
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo, FakeProvider
from tests.test_sync_engine import punch

NOW_LOCAL = datetime.utcnow() + timedelta(hours=4)   # tenant zone is Asia/Dubai


class IdProvider(FakeProvider):
    """A platform that numbers its records and can be asked for those after one."""

    record_id_cursor = True

    def __init__(self, rows, mode="ok"):
        super().__init__(rows)
        self.mode = mode            # "ok" | "ignores" | "breaks"
        self.after_calls: list[int] = []

    def fetch_punches(self, since=None, until=None):
        for event in super().fetch_punches(since, until):
            event.record_id = int(event.external_id)
            yield event

    def fetch_punches_after(self, after_id, since=None, until=None):
        self.after_calls.append(after_id)
        if self.mode == "ignores":
            raise IdFilterUnsupported("ignored the id condition")
        if self.mode == "breaks":
            raise ProviderError("connection reset")
        for row in sorted(self.rows, key=lambda r: r["id"]):
            if row["id"] <= after_id:
                continue
            moment = datetime.strptime(row["punch_time"], "%Y-%m-%d %H:%M:%S")
            if since and moment < since:
                continue
            yield PunchEvent(external_id=str(row["id"]), emp_code=row["emp_code"],
                             punch_time_local=moment, terminal_sn="GATE-01",
                             record_id=row["id"], raw=row)


def _run(db, tenant, provider, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: provider)
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


def _source(db):
    return db.scalars(select(DeviceSource)).one()


def _ledger(db):
    return {p.external_id for p in db.scalars(select(PunchRecord))}


def _late(rows, pid):
    rows.append(punch(pid, "1001", NOW_LOCAL - timedelta(days=2)))   # old time, new id


def test_first_run_reads_by_time_and_remembers_the_highest_id(db, tenant, monkeypatch):
    rows = [punch(1, "1001", NOW_LOCAL - timedelta(hours=3)), punch(2, "1002", NOW_LOCAL - timedelta(hours=2))]
    provider = IdProvider(rows)
    _run(db, tenant, provider, monkeypatch)
    assert provider.after_calls == [], "nothing to read by id before a cursor exists"
    assert _source(db).config["record_cursor"] == 2


def test_late_upload_with_an_old_timestamp_is_found_by_id(db, tenant, monkeypatch):
    rows = [punch(1, "1001", NOW_LOCAL - timedelta(hours=3)), punch(2, "1002", NOW_LOCAL - timedelta(hours=2))]
    _run(db, tenant, IdProvider(rows), monkeypatch)
    _late(rows, 3)

    provider = IdProvider(rows)
    result = _run(db, tenant, provider, monkeypatch)
    assert provider.after_calls == [2]
    assert "3" in _ledger(db)
    assert _source(db).config["record_cursor"] == 3
    assert any("found by record id" in str(line) for line in result.log)


def test_without_an_id_cursor_the_late_upload_is_missed(db, tenant, monkeypatch):
    """The gap this feature closes: the time window starts after the old timestamp."""
    rows = [punch(1, "1001", NOW_LOCAL - timedelta(hours=2))]
    _run(db, tenant, FakeProvider(rows), monkeypatch)
    _late(rows, 2)
    _run(db, tenant, FakeProvider(rows), monkeypatch)
    assert "2" not in _ledger(db)


def test_a_platform_that_ignores_the_id_filter_is_read_by_time_from_then_on(db, tenant, monkeypatch):
    rows = [punch(1, "1001", NOW_LOCAL - timedelta(hours=2))]
    _run(db, tenant, IdProvider(rows), monkeypatch)

    ignoring = IdProvider(rows, mode="ignores")
    result = _run(db, tenant, ignoring, monkeypatch)
    assert result.status == "success"
    assert _source(db).config.get("record_cursor_unsupported") is True

    again = IdProvider(rows)
    _run(db, tenant, again, monkeypatch)
    assert again.after_calls == [], "not asked by id again"


def test_restarted_ids_move_the_cursor_down(db, tenant, monkeypatch):
    source = _source(db)
    source.config = {**(source.config or {}), "record_cursor": 900}
    db.commit()
    rows = [punch(1, "1001", NOW_LOCAL - timedelta(hours=1)), punch(2, "1002", NOW_LOCAL - timedelta(minutes=30))]
    _run(db, tenant, IdProvider(rows), monkeypatch)
    assert _ledger(db) >= {"1", "2"}
    assert _source(db).config["record_cursor"] == 2, "a log cleared on the device starts again from 1"


def test_a_failed_id_read_leaves_both_cursors_where_they_were(db, tenant, monkeypatch):
    rows = [punch(1, "1001", NOW_LOCAL - timedelta(hours=2))]
    _run(db, tenant, IdProvider(rows), monkeypatch)
    before_time = _source(db).cursor_punch_time
    rows.append(punch(2, "1002", NOW_LOCAL - timedelta(minutes=10)))

    _run(db, tenant, IdProvider(rows, mode="breaks"), monkeypatch)
    source = _source(db)
    assert source.config["record_cursor"] == 1
    assert source.cursor_punch_time == before_time
