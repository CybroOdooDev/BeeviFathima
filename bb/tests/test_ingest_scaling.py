"""The ingest path's cost must track the batch, never the tenant's history.

``_known_external_ids`` used to load every external id the tenant had ever
stored into a Python set, once per run. Correct, and invisible until the fleet
grew: measured on a 5,000-employee tenant it cost 0.06 s / 2 MB at 10k ledger
rows and 5.3 s / 232 MB at 1M — for a run with 67 new punches to ingest. Across
500 tenants on a one-minute tick that single query was ~44 CPU cores against
roughly 1.4 for the actual work, and it grew every day the system ran.

Nothing failed, so no test caught it. These do, by asserting the shape rather
than a timing: a run's reads must be bounded by the punches in front of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select

from app.models import PunchRecord
from app.services import sync_engine as engine_mod
from app.services.sync_engine import INGEST_BATCH, SyncEngine
from tests.conftest import FakeOdoo, FakeProvider
from tests.test_sync_engine import punch

HISTORY = 4_000


def primed(db, tenant) -> SyncEngine:
    """An engine whose run row exists, as run_cycle() would have made it.

    The counters take their defaults on INSERT, so a test that calls
    _fetch_and_ingest directly has to flush the row first or every += hits None.
    """
    engine = SyncEngine(db, tenant, "test")
    db.add(engine.run)
    db.flush()
    return engine


@pytest.fixture
def stuffed_ledger(db, tenant, local_day):
    """A tenant with a lot of past punches and nothing to do about them."""
    source = db.scalars(select(engine_mod.DeviceSource)).first()
    base = local_day - timedelta(days=200)
    db.bulk_insert_mappings(PunchRecord, [
        {
            "tenant_id": tenant.id, "source_id": source.id,
            "external_id": f"old-{i}", "emp_code": "1001",
            "punch_time_utc": base + timedelta(seconds=i),
            "punch_time_local": base + timedelta(seconds=i),
            "direction": "in", "process_state": "synced", "attempts": 0,
        }
        for i in range(HISTORY)
    ])
    db.commit()
    return source


def test_lookup_is_scoped_to_the_ids_asked_about(db, tenant, stuffed_ledger):
    engine = SyncEngine(db, tenant, "test")
    found = engine._known_external_ids(  # noqa: SLF001
        stuffed_ledger, ["old-7", "old-9", "never-seen"]
    )
    assert found == {"old-7", "old-9"}, (
        "it must answer about the ids asked, not hand back the ledger"
    )


def test_lookup_of_nothing_touches_the_database_not_at_all(db, tenant, stuffed_ledger):
    engine = SyncEngine(db, tenant, "test")
    statements: list[str] = []
    listener = lambda c, cur, stmt, p, ctx, many: statements.append(stmt)  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", listener)
    try:
        assert engine._known_external_ids(stuffed_ledger, []) == set()  # noqa: SLF001
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", listener)
    assert statements == [], "an empty batch is answerable without a query"


def test_lookup_chunks_stay_within_sqlite_parameter_limits(db, tenant, stuffed_ledger):
    """SQLite's older builds cap bound parameters at 999.

    A single IN list of every id in a large batch raises OperationalError there
    — on the smallest deployments, which are the least likely to be load-tested.
    """
    engine = SyncEngine(db, tenant, "test")
    asked = [f"old-{i}" for i in range(2_500)]
    found = engine._known_external_ids(stuffed_ledger, asked)  # noqa: SLF001
    assert found == set(asked)


def test_a_run_reads_no_more_punch_rows_than_it_ingests(
    db, tenant, local_day, stuffed_ledger, monkeypatch
):
    """The regression itself, stated as a bound rather than a duration.

    A full-history scan reads HISTORY rows here; a batch-scoped one reads at
    most what it was asked about. The gap is wide enough that the assertion
    needs no timing and will not flake on a slow machine.
    """
    rows = [punch(i, "1001", local_day.replace(hour=8) + timedelta(minutes=i * 30))
            for i in range(1, 7)]

    selected: list[int] = []

    def counting_scalars(statement, *args, **kwargs):
        result = original(statement, *args, **kwargs)
        text = str(statement)
        if "punch_record.external_id" in text and "FROM punch_record" in text:
            values = list(result)
            selected.append(len(values))
            return _Rewound(values)
        return result

    class _Rewound:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

        def first(self):
            return self._values[0] if self._values else None

        def __iter__(self):
            return iter(self._values)

    original = db.scalars
    monkeypatch.setattr(db, "scalars", counting_scalars)
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: FakeProvider(rows))

    SyncEngine(db, tenant, "test").run_cycle()

    assert selected, "the ingest path must still check for duplicates"
    worst = max(selected)
    assert worst <= len(rows), (
        f"one external_id lookup returned {worst} rows for a {len(rows)}-punch "
        f"batch against {HISTORY:,} rows of history — the full-history scan is back"
    )


def test_batches_are_bounded_so_memory_does_not_track_the_window(
    db, tenant, local_day, monkeypatch
):
    """A large catch-up must not be buffered whole.

    Buffering the fetch to scope the lookup is only safe while the buffer has a
    ceiling: without one, a backfill after an outage would hold the entire
    window in memory and trade the old problem for a worse one.
    """
    source = db.scalars(select(engine_mod.DeviceSource)).first()
    count = INGEST_BATCH * 2 + 25
    rows = [punch(i, f"{1000 + i % 40}", local_day.replace(hour=1) + timedelta(seconds=i * 7))
            for i in range(count)]

    sizes: list[int] = []
    engine = primed(db, tenant)
    real_batch = engine._ingest_batch  # noqa: SLF001

    def watched(batch, src, devices):
        sizes.append(len(batch))
        return real_batch(batch, src, devices)

    monkeypatch.setattr(engine, "_ingest_batch", watched)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: FakeProvider(rows))
    engine._fetch_and_ingest(source)  # noqa: SLF001

    assert sizes, "nothing was ingested"
    assert max(sizes) <= INGEST_BATCH, f"a batch of {max(sizes)} exceeds the ceiling"
    assert sum(sizes) == count
    assert len(sizes) >= 3, "a window larger than the ceiling must span several batches"


def test_duplicates_inside_one_batch_do_not_violate_the_unique_index(
    db, tenant, local_day, monkeypatch
):
    """The overlap window means a repeat can arrive within a single batch.

    The old code added each id to its in-memory set as it went, so this was
    handled incidentally. Batching reintroduces the hazard: the database is
    consulted once per batch, before any of it is written.
    """
    moment = local_day.replace(hour=9)
    rows = [punch(1, "1001", moment), punch(1, "1001", moment), punch(2, "1001", moment)]

    source = db.scalars(select(engine_mod.DeviceSource)).first()
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: FakeProvider(rows))
    primed(db, tenant)._fetch_and_ingest(source)  # noqa: SLF001
    db.commit()

    stored = db.scalars(select(PunchRecord.external_id)).all()
    assert sorted(stored) == ["1", "2"], f"expected one row per id, got {stored}"
