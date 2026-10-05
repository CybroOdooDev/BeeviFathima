"""In-app alerts: what is wrong right now, derived from live state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.session import get_db
from app.main import app
from app.models import (
    AdmsDevice, DeviceSource, OdooConnection, PunchRecord, SyncRun, Tenant,
)
from tests.test_punch_ledger import api  # noqa: F401 — seeded tenant: 2 error + 1 unmapped punch


def _db():
    return next(app.dependency_overrides[get_db]())


def _alerts(api):  # noqa: F811
    r = api.get("/api/v1/alerts")
    assert r.status_code == 200, r.text
    return {a["key"]: a for a in r.json()["alerts"]}, r.json()


def _connect(db, **odoo):
    t = db.scalars(select(Tenant)).one()
    db.add(OdooConnection(tenant_id=t.id, url="https://x.odoo.com", db_name="d", username="u", **odoo))
    db.add(DeviceSource(tenant_id=t.id, name="Main", provider="biotime", base_url="http://b", username="u"))
    db.commit()
    return t


def test_punch_errors_are_listed_and_stuck_ones_are_critical(api):  # noqa: F811
    keys, body = _alerts(api)
    assert keys["punch-errors"]["severity"] == "warn" and "2 punches" in keys["punch-errors"]["title"]
    db = _db()
    for p in db.scalars(select(PunchRecord).where(PunchRecord.process_state == "error")):
        p.attempts = 5
    db.commit(); db.close()
    keys, body = _alerts(api)
    assert keys["punch-errors"]["severity"] == "bad" and body["worst"] == "bad"
    assert "manual Retry" in keys["punch-errors"]["detail"]
    assert body["count"] == len(body["alerts"])


def test_failed_odoo_and_source_connections_alert_with_the_reason(api):  # noqa: F811
    db = _db()
    _connect(db)
    for c in db.scalars(select(OdooConnection)):
        c.status, c.status_message = "failed", "Invalid API key"
    for s in db.scalars(select(DeviceSource)):
        s.status, s.status_message = "failed", "Timed out"
    db.commit(); db.close()
    keys, _ = _alerts(api)
    assert "Invalid API key" in keys["odoo-connection"]["detail"] and keys["odoo-connection"]["severity"] == "bad"
    src = next(a for k, a in keys.items() if k.startswith("source-"))
    assert "Timed out" in src["detail"]


def test_an_overdue_sync_is_critical_but_a_recent_one_is_not(api):  # noqa: F811
    db = _db()
    t = _connect(db)
    db.add(SyncRun(tenant_id=t.id, status="success", started_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    db.commit()
    assert "sync-overdue" not in _alerts(api)[0]
    db.commit()
    for r in db.scalars(select(SyncRun)):
        r.started_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db.commit(); db.close()
    keys, _ = _alerts(api)
    assert keys["sync-overdue"]["severity"] == "bad"


def test_a_silent_push_terminal_is_flagged_and_a_live_one_is_not(api):  # noqa: F811
    db = _db()
    t = _connect(db)
    src = db.scalars(select(DeviceSource)).one()
    db.add_all([
        AdmsDevice(serial_number="SILENT1", tenant_id=t.id, source_id=src.id, users={},
                   last_seen_at=datetime.now(timezone.utc) - timedelta(hours=3)),
        AdmsDevice(serial_number="LIVE1", tenant_id=t.id, source_id=src.id, users={},
                   last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=1)),
    ])
    db.commit(); db.close()
    keys, _ = _alerts(api)
    assert "terminal-offline-SILENT1" in keys and "terminal-offline-LIVE1" not in keys


def test_a_healthy_account_has_no_alerts(api):  # noqa: F811
    db = _db()
    for p in db.scalars(select(PunchRecord)):
        p.process_state = "synced"
    db.commit(); db.close()
    # the seeded unmapped badge lives on EmployeeMapping, which this seed doesn't create
    _, body = _alerts(api)
    assert body == {"count": 0, "worst": None, "alerts": []}


def test_alerts_need_a_signed_in_tenant(api):  # noqa: F811
    api.headers.pop("Authorization")
    assert api.get("/api/v1/alerts").status_code == 401


def test_a_trial_about_to_end_raises_a_renewal_alert(api):  # noqa: F811
    db = _db()
    t = db.scalars(select(Tenant)).one()
    t.status = "trialing"
    t.subscription_renews_at = datetime.now(timezone.utc) + timedelta(days=2)
    db.commit(); db.close()
    keys, _ = _alerts(api)
    assert keys["renewal"]["title"].startswith("Your free trial ends in 2 days")
    assert keys["renewal"]["severity"] == "bad"
