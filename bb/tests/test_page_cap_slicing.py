"""A read cut short by a page cap never lets the cursor step over punches.

BioTime pages its transactions in id order, not time order, so a read that
stops at the page cap can leave unread punches anywhere in its window —
including before the newest punch it did read. The engine reads such a window
again in smaller slices, and never moves the cursor past what it read
completely.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import DeviceSource, PunchRecord
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo, FakeProvider
from tests.test_sync_engine import punch

NOW_LOCAL = datetime.utcnow() + timedelta(hours=4)   # tenant zone is Asia/Dubai
DUBAI = timedelta(hours=4)


class CappedProvider(FakeProvider):
    """Returns at most ``cap`` rows per read, in id order, and says so."""

    def __init__(self, rows, cap):
        super().__init__(rows)
        self.cap = cap
        self.truncated = False
        self.reads = 0

    def fetch_punches(self, since=None, until=None):
        self.reads += 1
        hits = list(super().fetch_punches(since, until))
        hits.sort(key=lambda e: int(e.external_id))      # id order, like BioTime
        if len(hits) > self.cap:
            self.truncated = True
        yield from hits[: self.cap]


def _run(db, tenant, provider, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: provider)
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


def _rows(n, seed=7):
    """Ids and times deliberately unrelated: a late id can carry an early time."""
    rnd = random.Random(seed)
    times = [NOW_LOCAL - timedelta(hours=2, minutes=rnd.randint(0, 60 * 40)) for _ in range(n)]
    ids = list(range(1, n + 1))
    rnd.shuffle(ids)
    return [punch(i, "1001", t) for i, t in zip(ids, times)]


def _check_nothing_skipped(db, rows):
    """Every punch before the cursor's re-read point is in the ledger."""
    source = db.scalars(select(DeviceSource)).one()
    have = {p.external_id for p in db.scalars(select(PunchRecord))}
    if source.cursor_punch_time is None:
        return have
    safe_until = source.cursor_punch_time - timedelta(minutes=15) + DUBAI   # local
    for row in rows:
        when = datetime.strptime(row["punch_time"], "%Y-%m-%d %H:%M:%S")
        if when < safe_until:
            assert str(row["id"]) in have, f"punch {row['id']} at {when} was stepped over"
    return have


def test_a_capped_read_is_resliced_and_everything_arrives(db, tenant, monkeypatch):
    rows = _rows(60)
    have: set[str] = set()
    for _ in range(10):
        _run(db, tenant, CappedProvider(rows, cap=10), monkeypatch)
        have = _check_nothing_skipped(db, rows)
        if len(have) == len(rows):
            break
    assert have == {str(r["id"]) for r in rows}


def test_each_run_is_bounded_and_says_it_will_continue(db, tenant, monkeypatch):
    rows = _rows(200)
    provider = CappedProvider(rows, cap=5)
    result = _run(db, tenant, provider, monkeypatch)
    assert provider.reads <= engine_mod.MAX_SLICE_READS
    assert any("continuing next run" in str(line) for line in result.log)
    _check_nothing_skipped(db, rows)


def test_an_uncapped_read_takes_one_request(db, tenant, monkeypatch):
    rows = _rows(20)
    provider = CappedProvider(rows, cap=1000)
    _run(db, tenant, provider, monkeypatch)
    assert provider.reads == 1
    assert len({p.external_id for p in db.scalars(select(PunchRecord))}) == 20


def test_biotime_says_when_it_stopped_at_the_page_cap(monkeypatch):
    from app.core.config import settings
    from app.integrations.base import SourceConfig
    from app.integrations.providers.biotime import BioTimeProvider

    p = BioTimeProvider(SourceConfig(base_url="https://bio.test", username="u", password="p"))
    row = {"id": 1, "emp_code": "1001", "punch_time": "2026-10-06 08:00:00", "terminal_sn": "T1"}
    monkeypatch.setattr(settings, "max_pages_per_run", 2)
    monkeypatch.setattr(p, "_get", lambda path, params: {"data": [row], "next": "more"})
    list(p.fetch_punches())
    assert p.truncated is True

    p.truncated = False
    monkeypatch.setattr(p, "_get", lambda path, params: {"data": [row], "next": None})
    list(p.fetch_punches())
    assert p.truncated is False
