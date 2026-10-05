"""Alert emails: grace period, one email per problem, daily reminder, opt-out."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import OdooConnection, DeviceSource, Tenant, User
from app.services import alert_emails
from app.services.alert_emails import due_alerts, sweep_alert_emails
from tests.test_billing import _signup, client  # noqa: F401

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
RED = {"key": "odoo-connection", "severity": "bad", "title": "BioBridge can't reach Odoo", "detail": "Invalid API key."}
AMBER = {"key": "unmapped", "severity": "warn", "title": "5 badges", "detail": "x"}


def test_a_problem_must_stand_for_the_grace_period_before_it_is_mailed():
    fresh, state, rem = due_alerts({}, [RED], T0)
    assert fresh == [] and state["odoo-connection"]["emailed_at"] is None
    fresh, state, _ = due_alerts(state, [RED], T0 + timedelta(minutes=settings.alert_email_grace_minutes - 1))
    assert fresh == []
    fresh, state, _ = due_alerts(state, [RED], T0 + timedelta(minutes=settings.alert_email_grace_minutes))
    assert fresh == [RED]


def test_then_it_is_quiet_until_the_reminder_and_amber_is_never_mailed():
    _, state, _ = due_alerts({}, [RED, AMBER], T0)
    t1 = T0 + timedelta(minutes=20)
    fresh, state, _ = due_alerts(state, [RED, AMBER], t1)
    assert fresh == [RED] and "unmapped" not in state
    fresh, state, rem = due_alerts(state, [RED], t1 + timedelta(hours=23))
    assert fresh == [] and rem == []
    fresh, state, rem = due_alerts(state, [RED], t1 + timedelta(hours=24))
    assert fresh == [] and rem == [RED]


def test_a_cleared_alert_is_forgotten_so_its_return_is_news_again():
    _, state, _ = due_alerts({}, [RED], T0)
    _, state, _ = due_alerts(state, [RED], T0 + timedelta(minutes=20))
    _, state, _ = due_alerts(state, [], T0 + timedelta(minutes=30))
    assert state == {}
    _, state, _ = due_alerts(state, [RED], T0 + timedelta(minutes=40))
    fresh, _, _ = due_alerts(state, [RED], T0 + timedelta(minutes=60))
    assert fresh == [RED]


def test_account_stopped_is_left_to_the_existing_notice():
    stopped = {"key": "account-stopped", "severity": "bad", "title": "t", "detail": "d"}
    _, state, _ = due_alerts({}, [stopped], T0)
    assert state == {}


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(alert_emails, "send_email", lambda to, subject, body, db=None: sent.append((to, subject, body)))
    return sent


def _break_odoo(client):  # noqa: F811
    s = client.Session()
    t = s.scalars(select(Tenant)).one()
    s.add(OdooConnection(tenant_id=t.id, url="https://x.odoo.com", db_name="d", username="u",
                         status="failed", status_message="Invalid API key"))
    s.add(DeviceSource(tenant_id=t.id, name="Main", provider="biotime", base_url="http://b", username="u", status="connected"))
    s.commit()
    return s


def test_sweep_emails_the_owner_once_after_the_grace_then_stays_quiet(client, mail):  # noqa: F811
    _signup(client)
    s = _break_odoo(client)
    assert sweep_alert_emails(s, now=T0)["emails_sent"] == 0
    r = sweep_alert_emails(s, now=T0 + timedelta(minutes=20))
    assert r["emails_sent"] == 1 and mail[0][0] == "owner@acme.com"
    assert "Invalid API key" in mail[0][2] and "can't reach Odoo" in mail[0][1]
    assert sweep_alert_emails(s, now=T0 + timedelta(minutes=30))["emails_sent"] == 0
    assert sweep_alert_emails(s, now=T0 + timedelta(hours=25))["emails_sent"] == 1
    assert "Still unresolved" in mail[-1][1]
    s.close()


def test_the_opt_out_stops_the_emails(client, mail):  # noqa: F811
    headers = _signup(client)
    s = _break_odoo(client)
    assert client.patch("/api/v1/tenant", headers=headers, json={"alert_emails_enabled": False}).status_code == 200
    s.expire_all()
    sweep_alert_emails(s, now=T0)
    assert sweep_alert_emails(s, now=T0 + timedelta(hours=2))["emails_sent"] == 0 and mail == []
    assert client.get("/api/v1/tenant", headers=headers).json()["alert_emails_enabled"] is False
    s.close()
