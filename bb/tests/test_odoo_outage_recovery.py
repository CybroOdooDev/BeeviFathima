"""An Odoo outage costs a delay, never a punch.

The device side is read and written to the ledger first, independently of Odoo.
Whatever Odoo could not take stays pending/error in the ledger, and the next
sync that finds Odoo healthy pushes it, exactly once, even if the device has
since stopped returning those punches.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.integrations.odoo import OdooError
from app.models import PunchRecord
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo
from tests.test_sync_engine import punch, run

DUBAI_OFFSET = timedelta(hours=4)


class FlakyOdoo(FakeOdoo):
    """A FakeOdoo that can be taken down and brought back."""

    def __init__(self):
        super().__init__()
        self.down = False          # unreachable: even authenticate fails
        self.reject_writes = False  # reachable, but writing attendance fails

    def authenticate(self) -> int:
        if self.down:
            raise OdooError("Cannot reach Odoo: connection refused")
        return super().authenticate()

    def create_attendance(self, *args, **kwargs):
        if self.reject_writes:
            raise OdooError("Odoo returned 502 Bad Gateway")
        return super().create_attendance(*args, **kwargs)


def _states(db):
    return {p.external_id: p.process_state for p in db.scalars(select(PunchRecord))}


def test_punches_captured_while_odoo_is_down_are_pushed_by_the_next_sync(
    db, tenant, local_day, monkeypatch
):
    odoo = FlakyOdoo()
    rows = [punch(1, "1001", local_day.replace(hour=8)), punch(2, "1002", local_day.replace(hour=9))]

    odoo.down = True
    outage = run(db, tenant, odoo, rows, monkeypatch)
    assert outage.status == "failed"
    assert "Cannot reach Odoo" in outage.error_message
    assert odoo.attendances == {}, "nothing can reach Odoo while it is down"
    assert set(_states(db).values()) == {"pending"}, "captured and waiting, not lost"

    odoo.down = False
    recovered = run(db, tenant, odoo, rows, monkeypatch)
    assert recovered.status == "success", recovered.error_message
    assert set(_states(db).values()) == {"synced"}
    assert len(odoo.attendances) == 2


def test_recovery_works_even_if_the_device_no_longer_returns_the_punches(
    db, tenant, local_day, monkeypatch
):
    """Platforms prune old transactions: the retry must come from the ledger."""
    odoo = FlakyOdoo()
    odoo.down = True
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)

    odoo.down = False
    recovered = run(db, tenant, odoo, [], monkeypatch)   # the device now returns nothing
    assert recovered.status == "success", recovered.error_message
    assert len(odoo.attendances) == 1
    assert next(iter(odoo.attendances.values()))["check_in"] == local_day.replace(hour=8) - DUBAI_OFFSET


def test_check_in_and_check_out_both_made_during_the_outage_become_one_record(
    db, tenant, local_day, monkeypatch
):
    odoo = FlakyOdoo()
    rows = [punch(1, "1001", local_day.replace(hour=8)), punch(2, "1001", local_day.replace(hour=17))]

    odoo.down = True
    run(db, tenant, odoo, rows, monkeypatch)
    odoo.down = False
    recovered = run(db, tenant, odoo, rows, monkeypatch)

    assert recovered.status == "success", recovered.error_message
    assert len(odoo.attendances) == 1, "one shift, one record"
    record = next(iter(odoo.attendances.values()))
    assert record["check_in"] == local_day.replace(hour=8) - DUBAI_OFFSET
    assert record["check_out"] == local_day.replace(hour=17) - DUBAI_OFFSET


def test_a_failed_push_is_marked_error_then_recovered_without_duplicates(
    db, tenant, local_day, monkeypatch
):
    """Odoo answers the login but rejects the write (a 5xx, a lock timeout)."""
    odoo = FlakyOdoo()
    rows = [punch(1, "1001", local_day.replace(hour=8))]

    odoo.reject_writes = True
    failed = run(db, tenant, odoo, rows, monkeypatch)
    assert failed.error_count >= 1
    (stuck,) = db.scalars(select(PunchRecord)).all()
    assert stuck.process_state == "error"
    assert stuck.attempts == 1
    assert "502" in stuck.error_message
    assert odoo.attendances == {}

    odoo.reject_writes = False
    ok = run(db, tenant, odoo, rows, monkeypatch)   # the overlap window re-reads the punch
    assert ok.status == "success", ok.error_message
    db.refresh(stuck)
    assert stuck.process_state == "synced"
    assert len(odoo.attendances) == 1, "the re-read punch must not create a second record"
    assert db.scalars(select(PunchRecord)).all() == [stuck], "and must not be ingested twice"


def test_a_punch_that_failed_before_is_pushed_together_with_new_ones(
    db, tenant, local_day, monkeypatch
):
    odoo = FlakyOdoo()
    first = punch(1, "1001", local_day.replace(hour=8))
    second = punch(2, "1001", local_day.replace(hour=17))

    odoo.reject_writes = True
    run(db, tenant, odoo, [first], monkeypatch)
    odoo.reject_writes = False
    ok = run(db, tenant, odoo, [first, second], monkeypatch)   # check-out arrives after recovery

    assert ok.status == "success", ok.error_message
    assert set(_states(db).values()) == {"synced"}
    assert len(odoo.attendances) == 1
    record = next(iter(odoo.attendances.values()))
    assert record["check_out"] == local_day.replace(hour=17) - DUBAI_OFFSET


def test_after_five_failed_pushes_the_punch_waits_for_a_manual_retry(
    db, tenant, local_day, monkeypatch
):
    """Documents today's limit: attempts are capped at 5, then a person retries.

    A long outage burns through them even though nothing is wrong with the
    punch itself; see the discussion about not counting transient failures.
    """
    odoo = FlakyOdoo()
    rows = [punch(1, "1001", local_day.replace(hour=8))]

    odoo.reject_writes = True
    for _ in range(5):
        run(db, tenant, odoo, rows, monkeypatch)
    (stuck,) = db.scalars(select(PunchRecord)).all()
    assert (stuck.process_state, stuck.attempts) == ("error", 5)

    odoo.reject_writes = False
    run(db, tenant, odoo, rows, monkeypatch)
    db.refresh(stuck)
    assert stuck.process_state == "error", "exhausted punches are skipped until Retry"
    assert odoo.attendances == {}

    stuck.attempts = 0            # what POST /punches/{id}/retry does
    db.commit()
    ok = run(db, tenant, odoo, rows, monkeypatch)
    assert ok.status == "success", ok.error_message
    db.refresh(stuck)
    assert stuck.process_state == "synced"
    assert len(odoo.attendances) == 1
