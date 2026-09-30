"""Stripe billing: Checkout, webhooks, plan switches — against a fake Stripe.

The fake stands in for app.services.billing._request, so every rule above
the HTTP layer (signature checks, idempotency, status mapping, when a plan
lands) runs for real.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models import Base, SubscriptionPlan, Tenant
from app.services import billing
from app.services.scheduling import sweep_subscriptions

SECRET = "whsec_test_secret"
NOW = int(time.time())
PERIOD_END = NOW + 30 * 86400


class FakeStripe:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.subscriptions: dict[str, dict] = {}

    def sub(self, sub_id="sub_1", price="price_growth", status="active", period_end=PERIOD_END, customer="cus_1"):
        self.subscriptions[sub_id] = {
            "id": sub_id, "status": status, "customer": customer,
            "items": {"data": [{"id": "si_1", "price": {"id": price}, "current_period_end": period_end}]},
        }
        return self.subscriptions[sub_id]

    def __call__(self, method, path, data=None, idempotency_key=None):
        self.calls.append((method, path, data or {}))
        if path == "customers":
            return {"id": "cus_1"}
        if path == "checkout/sessions":
            return {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"}
        if path == "billing_portal/sessions":
            return {"url": "https://billing.stripe.test/p_1"}
        if path.startswith("subscriptions/"):
            sub_id = path.split("/", 1)[1]
            if method == "GET":
                return self.subscriptions[sub_id]
            price = data["items"][0]["price"]
            self.subscriptions[sub_id]["items"]["data"][0]["price"] = {"id": price}
            return self.subscriptions[sub_id]
        raise AssertionError(f"unexpected Stripe call {method} {path}")


@pytest.fixture
def stripe(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    monkeypatch.setattr(billing, "_request", fake)
    return fake


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add_all([
        SubscriptionPlan(name="Starter", monthly_price_cents=4900, max_employees=25,
                         min_sync_interval_minutes=60, is_default=True, stripe_price_id="price_starter"),
        SubscriptionPlan(name="Growth", monthly_price_cents=14900, max_employees=150,
                         min_sync_interval_minutes=15, stripe_price_id="price_growth"),
        SubscriptionPlan(name="Scale", monthly_price_cents=39900, min_sync_interval_minutes=5),
    ])
    db.commit(); db.close()

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        c.Session = Session
        yield c
    app.dependency_overrides.clear()


def _signup(client):
    r = client.post("/api/v1/auth/signup", json={
        "company_name": "Acme", "email": "owner@acme.com",
        "password": "a-long-enough-password", "timezone": "UTC"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _plan(client, name):
    s = client.Session()
    try:
        return s.scalar(select(SubscriptionPlan).where(SubscriptionPlan.name == name)).id
    finally:
        s.close()


def _tenant(client):
    s = client.Session()
    try:
        t = s.scalars(select(Tenant)).first()
        s.expunge(t)
        return t
    finally:
        s.close()


def _post_event(client, event, secret=SECRET, ts=None):
    body = json.dumps(event).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return client.post("/api/v1/billing/webhook", content=body,
                       headers={"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"})


def _checkout_completed(client, tenant_id, event_id="evt_1"):
    return _post_event(client, {"id": event_id, "type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "subscription": "sub_1", "customer": "cus_1",
        "client_reference_id": tenant_id}}})


# --- disabled by default -------------------------------------------------------
def test_billing_is_off_without_a_key(client):
    headers = _signup(client)
    assert client.get("/api/v1/billing", headers=headers).json()["enabled"] is False
    r = client.post("/api/v1/billing/checkout", headers=headers, json={"plan_id": _plan(client, "Growth")})
    assert r.status_code == 404


# --- checkout ---------------------------------------------------------------------
def test_checkout_creates_a_customer_and_returns_stripes_page(client, stripe):
    headers = _signup(client)
    r = client.post("/api/v1/billing/checkout", headers=headers, json={"plan_id": _plan(client, "Growth")})
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://checkout.stripe.test/cs_1"
    session = next(d for m, p, d in stripe.calls if p == "checkout/sessions")
    assert session["mode"] == "subscription"
    assert session["line_items"][0]["price"] == "price_growth"
    assert _tenant(client).stripe_customer_id == "cus_1"
    # Nothing is applied until Stripe confirms payment.
    assert _tenant(client).plan_id == _plan(client, "Starter")


def test_a_plan_without_a_stripe_price_cannot_be_bought_online(client, stripe):
    headers = _signup(client)
    r = client.post("/api/v1/billing/checkout", headers=headers, json={"plan_id": _plan(client, "Scale")})
    assert r.status_code == 400 and "can't be bought online" in r.json()["detail"]


# --- webhooks ------------------------------------------------------------------------
def test_bad_signatures_are_refused(client, stripe):
    _signup(client)
    event = {"id": "evt_x", "type": "checkout.session.completed", "data": {"object": {}}}
    assert _post_event(client, event, secret="whsec_wrong").status_code == 400
    assert _post_event(client, event, ts=int(time.time()) - 3600).status_code == 400
    r = client.post("/api/v1/billing/webhook", content=b"{}")
    assert r.status_code == 400


def test_checkout_completed_puts_the_account_on_its_paid_plan(client, stripe):
    _signup(client)
    tenant = _tenant(client)
    stripe.sub(price="price_growth")
    assert _checkout_completed(client, tenant.id).json()["result"] == "linked"
    t = _tenant(client)
    assert t.status == "active"
    assert t.plan_id == _plan(client, "Growth")
    assert t.stripe_subscription_id == "sub_1"
    renews = t.subscription_renews_at.replace(tzinfo=timezone.utc)
    assert abs(renews.timestamp() - PERIOD_END) < 2
    assert t.sync_interval_minutes >= 15  # raised to the plan's floor if needed


def test_the_same_event_twice_is_applied_once(client, stripe):
    _signup(client)
    tenant = _tenant(client)
    stripe.sub()
    _checkout_completed(client, tenant.id)
    assert _checkout_completed(client, tenant.id).json()["result"] == "duplicate"


def test_failed_payment_moves_the_account_to_past_due(client, stripe):
    _signup(client)
    tenant = _tenant(client)
    stripe.sub()
    _checkout_completed(client, tenant.id)
    stripe.subscriptions["sub_1"]["status"] = "past_due"
    r = _post_event(client, {"id": "evt_2", "type": "invoice.payment_failed",
                             "data": {"object": {"subscription": "sub_1", "customer": "cus_1"}}})
    assert r.json()["result"] == "applied"
    assert _tenant(client).status == "past_due"


def test_cancelled_subscription_cancels_the_account(client, stripe):
    _signup(client)
    tenant = _tenant(client)
    stripe.sub()
    _checkout_completed(client, tenant.id)
    stripe.subscriptions["sub_1"]["status"] = "canceled"
    _post_event(client, {"id": "evt_3", "type": "customer.subscription.deleted",
                         "data": {"object": {"id": "sub_1", "customer": "cus_1"}}})
    t = _tenant(client)
    assert t.status == "cancelled" and t.stripe_subscription_id is None


def test_a_webhook_never_lifts_a_staff_suspension(client, stripe):
    _signup(client)
    tenant = _tenant(client)
    stripe.sub()
    _checkout_completed(client, tenant.id)
    s = client.Session(); t = s.get(Tenant, tenant.id); t.status = "suspended"; s.commit(); s.close()
    _post_event(client, {"id": "evt_4", "type": "invoice.paid",
                         "data": {"object": {"subscription": "sub_1", "customer": "cus_1"}}})
    assert _tenant(client).status == "suspended"


# --- plan switches on a paid subscription ---------------------------------------------
def test_switch_is_queued_and_stripe_is_told_the_next_price(client, stripe):
    headers = _signup(client)
    tenant = _tenant(client)
    stripe.sub(price="price_growth")
    _checkout_completed(client, tenant.id)
    r = client.patch("/api/v1/tenant", headers=headers, json={"plan_id": _plan(client, "Starter")})
    assert r.status_code == 200, r.text
    assert r.json()["pending_plan_id"] == _plan(client, "Starter")
    assert r.json()["plan_id"] == _plan(client, "Growth")
    update = [d for m, p, d in stripe.calls if m == "POST" and p == "subscriptions/sub_1"][-1]
    assert update["items"][0]["price"] == "price_starter"
    assert update["proration_behavior"] == "none"

    # The webhook for that change must not apply it early...
    _post_event(client, {"id": "evt_5", "type": "customer.subscription.updated",
                         "data": {"object": {"id": "sub_1", "customer": "cus_1"}}})
    assert _tenant(client).plan_id == _plan(client, "Growth")
    # ...but the renewal into the next period does.
    stripe.subscriptions["sub_1"]["items"]["data"][0]["current_period_end"] = PERIOD_END + 30 * 86400
    _post_event(client, {"id": "evt_6", "type": "invoice.paid",
                         "data": {"object": {"subscription": "sub_1", "customer": "cus_1"}}})
    t = _tenant(client)
    assert t.plan_id == _plan(client, "Starter") and t.pending_plan_id is None


def test_switch_to_an_unpriced_plan_is_refused_and_nothing_changes(client, stripe):
    headers = _signup(client)
    tenant = _tenant(client)
    stripe.sub(price="price_growth")
    _checkout_completed(client, tenant.id)
    r = client.patch("/api/v1/tenant", headers=headers, json={"plan_id": _plan(client, "Scale")})
    assert r.status_code == 400
    assert _tenant(client).pending_plan_id is None


def test_checkout_is_refused_once_subscribed(client, stripe):
    headers = _signup(client)
    stripe.sub()
    _checkout_completed(client, _tenant(client).id)
    r = client.post("/api/v1/billing/checkout", headers=headers, json={"plan_id": _plan(client, "Growth")})
    assert r.status_code == 409


# --- the renewal-date sweep leaves Stripe accounts to Stripe ----------------------------------
def test_sweep_does_not_lapse_a_stripe_account_on_the_date_alone(client, stripe):
    _signup(client)
    stripe.sub()
    _checkout_completed(client, _tenant(client).id)
    s = client.Session()
    t = s.scalars(select(Tenant)).first()
    t.subscription_renews_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    s.commit()
    sweep_subscriptions(s)
    s.commit()
    assert s.get(Tenant, t.id).status == "active"
    s.close()


def test_portal_needs_a_customer(client, stripe):
    headers = _signup(client)
    assert client.post("/api/v1/billing/portal", headers=headers).status_code == 400
    client.post("/api/v1/billing/checkout", headers=headers, json={"plan_id": _plan(client, "Growth")})
    r = client.post("/api/v1/billing/portal", headers=headers)
    assert r.status_code == 200 and r.json()["url"].startswith("https://billing.stripe.test")
