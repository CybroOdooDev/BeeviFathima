"""The console's landing page: platform-wide counts, and nothing more."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import EmployeeMapping, PunchRecord, SyncRun, Tenant
from tests.test_platform_admin import api, head, promote, signup  # noqa: F401

STAFF = "ops@platform.example.com"


def _setup(api):  # noqa: F811
    signup(api, "Acme", "owner@acme.example.com")
    signup(api, "Globex", "owner@globex.example.com")
    signup(api, "Ops", STAFF)
    return promote(api, STAFF)


def test_the_overview_counts_every_account(api):  # noqa: F811
    token = _setup(api)
    body = api.get("/api/v1/admin/overview", headers=head(token)).json()
    assert body["accounts"]["total"] == 3
    assert sum(body["accounts"]["by_status"].values()) == 3
    assert body["setup"] == {"ready": 0, "partial": 0, "none": 3}
    assert len(body["punches_by_day"]) == 14
    # Nothing is connected anywhere, so every account needs a person.
    assert body["attention_total"] == 3
    assert all(any(r["text"] == "nothing connected" for r in a["reasons"]) for a in body["attention"])


def test_punch_volume_is_a_count_per_day_and_failed_runs_are_scrubbed(api):  # noqa: F811
    token = _setup(api)
    db = api.session_factory()
    acme = db.scalars(select(Tenant).where(Tenant.name == "Acme")).one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(3):
        db.add(PunchRecord(tenant_id=acme.id, source_id="s", external_id=str(i), emp_code="1",
                           punch_time_utc=now - timedelta(minutes=i), direction="in", process_state="synced"))
    db.add(EmployeeMapping(tenant_id=acme.id, emp_code="1", odoo_employee_name="Sara Tanaka", status="mapped"))
    db.add(SyncRun(tenant_id=acme.id, status="failed", triggered_by="schedule",
                   started_at=datetime.now(timezone.utc),
                   error_message="Cannot create attendance for Sara Tanaka: already checked in"))
    db.commit()
    db.close()

    body = api.get("/api/v1/admin/overview", headers=head(token)).json()
    assert body["activity"]["punches_today"] == 3
    assert body["activity"]["runs_24h"]["failed"] == 1
    (failed,) = body["failed_runs"]
    assert failed["tenant_name"] == "Acme"
    assert "Sara Tanaka" not in failed["message"] and "<employee>" in failed["message"]
    acme_row = next(a for a in body["attention"] if a["name"] == "Acme")
    assert any(r["text"] == "last sync failed" for r in acme_row["reasons"])


def test_a_customer_session_cannot_read_it(api):  # noqa: F811
    customer = signup(api, "Acme", "owner@acme.example.com")
    assert api.get("/api/v1/admin/overview", headers=head(customer)).status_code == 403
