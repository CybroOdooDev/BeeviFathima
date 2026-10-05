"""The website's Contact / Book a demo form, and the staff list behind it."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import ContactRequest, User
from app.services import contact as contact_service
from tests.test_billing import _signup, client  # noqa: F401
from tests.test_account_lifecycle import _staff  # noqa: F401

FORM = {
    "topic": "Demo", "name": "Ravi Menon", "email": "ravi@globex.com", "company": "Globex",
    "employees": "26–150", "odoo_version": "17", "odoo_hosting": "Odoo.sh",
    "biometric_system": "ZKTeco", "devices": "Both", "message": "Two sites",
    "preferred_date": "2026-10-20", "preferred_window": "Morning", "timezone": "Asia/Kolkata",
}


@pytest.fixture(autouse=True)
def _mail(monkeypatch):
    sent = []
    monkeypatch.setattr(contact_service, "send_email", lambda to, subject, body, db=None: sent.append((to, subject, body)))
    monkeypatch.setattr(settings, "sales_email", "sales@bb.test")
    monkeypatch.setattr(settings, "demo_booking_url", "")
    contact_service._hits.clear()
    return sent


def test_a_request_is_saved_then_sales_is_told_and_the_visitor_gets_a_receipt(client, _mail):  # noqa: F811
    r = client.post("/api/v1/public/contact", json=FORM)
    assert r.status_code == 201 and "one working day" in r.json()["message"]
    s = client.Session()
    row = s.scalars(select(ContactRequest)).one()
    assert (row.email, row.odoo_version, row.biometric_system, row.preferred_window, row.status) == (
        "ravi@globex.com", "17", "ZKTeco", "Morning", "new")
    s.close()
    to = [m[0] for m in _mail]
    assert to == ["sales@bb.test", "ravi@globex.com"]
    assert "Preferred time: 2026-10-20 · Morning (Asia/Kolkata)" in _mail[0][2] and "Odoo version: 17" in _mail[0][2]
    assert "2026-10-20" in _mail[1][2]


def test_the_lead_survives_a_mail_failure(client, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(contact_service, "send_email", boom)
    assert client.post("/api/v1/public/contact", json=FORM).status_code == 201
    s = client.Session()
    assert s.scalars(select(ContactRequest)).one().name == "Ravi Menon"
    s.close()


def test_bots_and_floods_are_stopped(client, _mail, monkeypatch):  # noqa: F811
    r = client.post("/api/v1/public/contact", json={**FORM, "website": "http://spam"})
    assert r.status_code == 201 and _mail == []
    s = client.Session(); assert s.scalars(select(ContactRequest)).first() is None; s.close()
    monkeypatch.setattr(settings, "contact_rate_per_hour", 2)
    codes = [client.post("/api/v1/public/contact", json=FORM).status_code for _ in range(3)]
    assert codes == [201, 201, 429]


def test_bad_input_is_refused(client):  # noqa: F811
    assert client.post("/api/v1/public/contact", json={**FORM, "email": "nope"}).status_code == 422
    assert client.post("/api/v1/public/contact", json={**FORM, "preferred_date": "next week"}).status_code == 422
    assert client.post("/api/v1/public/contact", json={**FORM, "preferred_window": "Midnight"}).status_code == 422


def test_a_demo_request_gets_the_booking_link_when_one_is_configured(client, monkeypatch, _mail):  # noqa: F811
    monkeypatch.setattr(settings, "demo_booking_url", "https://cal.example.com/biobridge")
    r = client.post("/api/v1/public/contact", json=FORM).json()
    assert r["booking_url"] == "https://cal.example.com/biobridge" and "cal.example.com" in _mail[1][2]
    r = client.post("/api/v1/public/contact", json={**FORM, "topic": "Sales question"}).json()
    assert r["booking_url"] is None


def test_without_a_sales_address_every_staff_account_is_told(client, monkeypatch, _mail):  # noqa: F811
    _signup(client)
    _staff(client)
    monkeypatch.setattr(settings, "sales_email", "")
    client.post("/api/v1/public/contact", json=FORM)
    assert _mail[0][0] == "ops@platform.example.com"


def test_staff_can_list_and_work_the_leads_and_customers_cannot(client):  # noqa: F811
    customer = _signup(client)
    staff = _staff(client)
    client.post("/api/v1/public/contact", json=FORM)
    assert client.get("/api/v1/admin/contact-requests", headers=customer).status_code in (401, 403)
    rows = client.get("/api/v1/admin/contact-requests", headers=staff).json()
    assert len(rows) == 1 and rows[0]["company"] == "Globex"
    r = client.patch(f"/api/v1/admin/contact-requests/{rows[0]['id']}", headers=staff,
                     json={"status": "contacted", "notes": "Called, sending a quote"})
    assert r.status_code == 200 and r.json()["status"] == "contacted" and r.json()["handled_by"] == "ops@platform.example.com"
    assert client.get("/api/v1/admin/contact-requests?status=open", headers=staff).json()[0]["notes"].startswith("Called")
    client.patch(f"/api/v1/admin/contact-requests/{rows[0]['id']}", headers=staff, json={"status": "closed"})
    assert client.get("/api/v1/admin/contact-requests?status=open", headers=staff).json() == []
    assert client.patch("/api/v1/admin/contact-requests/nope", headers=staff, json={"status": "won"}).status_code == 404
