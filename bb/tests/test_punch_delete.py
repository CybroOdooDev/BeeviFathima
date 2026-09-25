"""Delete on the Activity screen: out of the queue for good, and restorable."""

from __future__ import annotations

from sqlalchemy import select

from app.models import PunchRecord, PunchState
from tests.conftest import FakeOdoo
from tests.test_punch_ledger import api, punches  # noqa: F401 — the api fixture
from tests.test_sync_engine import punch, run


def _id(api, external_state):  # noqa: F811
    return next(r["id"] for r in punches(api, state=external_state))


def test_delete_hides_the_punch_and_restore_brings_it_back(api):  # noqa: F811
    pid = _id(api, "unmapped")
    response = api.delete(f"/api/v1/punches/{pid}")
    assert response.status_code == 200, response.text

    assert pid not in {r["id"] for r in punches(api)}, "gone from the unfiltered list"
    (deleted,) = punches(api, state="deleted")
    assert deleted["id"] == pid and deleted["error_message"] == "Deleted (was unmapped)"

    assert api.post(f"/api/v1/punches/{pid}/retry").status_code == 200
    assert next(r for r in punches(api) if r["id"] == pid)["process_state"] == "pending"


def test_an_error_punch_can_be_deleted_too(api):  # noqa: F811
    assert api.delete(f"/api/v1/punches/{_id(api, 'error')}").status_code == 200


def test_a_synced_or_pending_punch_cannot_be_deleted(api):  # noqa: F811
    for state in ("synced", "pending"):
        response = api.delete(f"/api/v1/punches/{_id(api, state)}")
        assert response.status_code == 409, state
        assert "can't be deleted" in response.json()["detail"]


def test_an_unknown_punch_is_404(api):  # noqa: F811
    assert api.delete("/api/v1/punches/nope").status_code == 404


def test_the_next_sync_neither_re_ingests_nor_pushes_a_deleted_punch(
    db, tenant, local_day, monkeypatch
):
    rows = [punch(1, "1001", local_day.replace(hour=8))]
    run(db, tenant, FakeOdoo(employees={}), rows, monkeypatch)  # badge unknown → unmapped
    (stored,) = db.scalars(select(PunchRecord)).all()
    stored.process_state = PunchState.deleted.value
    db.commit()

    odoo = FakeOdoo()  # Odoo now knows the badge
    run(db, tenant, odoo, rows, monkeypatch)  # the same punch is on the wire again

    assert odoo.attendances == {}, "a deleted punch is never pushed"
    (again,) = db.scalars(select(PunchRecord)).all()
    assert again.process_state == PunchState.deleted.value, "and not re-ingested as new"
