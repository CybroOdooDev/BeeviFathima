"""Deleting plans and accounts, and stopping syncing when a subscription
lapses — with an email to the customer every time BioBridge stops them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import AccountClosure, AuditLog, SubscriptionPlan, Tenant, User
from app.services import billing, notices
from app.services.scheduling import sweep_subscriptions
from tests.test_billing import (  # noqa: F401
    FakeStripe, _checkout_completed, _plan, _post_event, _signup, _tenant, client, stripe,
)


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(notices, "send_email", lambda to, subject, body, db=None: sent.append((to, subject, body)))
    return sent


def _staff(client):
    from tests.test_platform_admin import staff_login

    client.post("/api/v1/auth/signup", json={"company_name": "Ops", "email": "ops@platform.example.com",
                                             "password": "a-long-enough-password", "timezone": "UTC"})
    s = client.Session()
    u = s.scalars(select(User).where(User.email == "ops@platform.example.com")).first()
    u.is_platform_admin = True
    s.commit(); s.close()
    return {"Authorization": f"Bearer {staff_login(client, 'ops@platform.example.com')}"}


def _acme(client):
    s = client.Session()
    t = s.scalars(select(Tenant).where(Tenant.name == "Acme")).first()
    s.close()
    return t


class DeletingStripe(FakeStripe):
    fail = False

    def __call__(self, method, path, data=None, idempotency_key=None):
        if method == "DELETE":
            self.calls.append((method, path, {}))
            if self.fail:
                raise billing.BillingError("Stripe: connection reset")
            return {"id": path.split("/")[1], "status": "canceled"}
        return super().__call__(method, path, data, idempotency_key)


@pytest.fixture
def dstripe(stripe, monkeypatch):
    fake = DeletingStripe()
    fake.subscriptions = stripe.subscriptions
    monkeypatch.setattr(billing, "_request", fake)
    return fake


# --- plans ------------------------------------------------------------------
def test_a_plan_in_use_cannot_be_deleted(client):
    _signup(client)  # lands on the default plan, Starter
    staff = _staff(client)
    r = client.delete(f"/api/v1/admin/plans/{_plan(client, 'Starter')}", headers=staff)
    assert r.status_code == 409 and "retire" in r.json()["detail"]
    r = client.delete(f"/api/v1/admin/plans/{_plan(client, 'Scale')}", headers=staff)
    assert r.status_code == 200
    s = client.Session()
    assert s.scalar(select(func.count()).select_from(SubscriptionPlan).where(SubscriptionPlan.name == "Scale")) == 0
    s.close()


# --- staff deleting an account ------------------------------------------------
def test_staff_delete_needs_a_deactivated_account_and_its_name(client, dstripe):
    _signup(client)
    dstripe.sub()
    _checkout_completed(client, _tenant(client).id)
    staff = _staff(client)
    acme = _acme(client)
    r = client.post(f"/api/v1/admin/tenants/{acme.id}/delete", headers=staff, json={"confirm_name": "Acme"})
    assert r.status_code == 400 and "Deactivate it first" in r.json()["detail"]

    client.post(f"/api/v1/admin/tenants/{acme.id}/deactivate", headers=staff, json={"reason": "unpaid"})
    r = client.post(f"/api/v1/admin/tenants/{acme.id}/delete", headers=staff, json={"confirm_name": "Acmee"})
    assert r.status_code == 400

    r = client.post(f"/api/v1/admin/tenants/{acme.id}/delete", headers=staff,
                    json={"confirm_name": "acme", "reason": "unpaid since March"})
    assert r.status_code == 200, r.text
    assert ("DELETE", "subscriptions/sub_1", {}) in dstripe.calls
    s = client.Session()
    assert s.get(Tenant, acme.id) is None
    assert s.scalar(select(func.count()).select_from(User).where(User.tenant_id == acme.id)) == 0
    assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.tenant_id == acme.id)) == 0
    closure = s.scalars(select(AccountClosure)).one()
    assert (closure.tenant_name, closure.closed_by, closure.reason_text) == ("Acme", "staff", "unpaid since March")
    assert closure.owner_email == "owner@acme.com" and closure.stripe_subscription_cancelled
    s.close()
    rows = client.get("/api/v1/admin/closures", headers=staff).json()
    assert rows[0]["tenant_name"] == "Acme" and rows[0]["reason_label"] == "Deleted by staff"


# --- the owner deleting their own account --------------------------------------
def test_owner_deletes_own_account_with_a_reason(client, dstripe):
    headers = _signup(client)
    dstripe.sub()
    _checkout_completed(client, _tenant(client).id)
    good = {"reason_code": "too_expensive", "reason_text": "Budget cut", "password": "a-long-enough-password",
            "confirm_name": "Acme"}
    assert client.post("/api/v1/tenant/delete", headers=headers, json={**good, "reason_code": "nope"}).status_code == 400
    r = client.post("/api/v1/tenant/delete", headers=headers, json={**good, "reason_code": "other", "reason_text": ""})
    assert r.status_code == 400 and "why" in r.json()["detail"]
    assert client.post("/api/v1/tenant/delete", headers=headers, json={**good, "password": "wrong"}).status_code == 400
    assert client.post("/api/v1/tenant/delete", headers=headers, json={**good, "confirm_name": "X"}).status_code == 400

    r = client.post("/api/v1/tenant/delete", headers=headers, json=good)
    assert r.status_code == 200 and r.json()["deleted"] is True
    s = client.Session()
    closure = s.scalars(select(AccountClosure)).one()
    assert (closure.closed_by, closure.reason_code, closure.reason_text) == ("customer", "too_expensive", "Budget cut")
    assert s.scalar(select(func.count()).select_from(Tenant)) == 0
    s.close()
    # The session went with the account.
    assert client.get("/api/v1/tenant", headers=headers).status_code == 401
    login = client.post("/api/v1/auth/login", json={"email": "owner@acme.com", "password": "a-long-enough-password"})
    assert login.status_code in (400, 401)


def test_nothing_is_deleted_if_stripe_cannot_cancel(client, dstripe):
    headers = _signup(client)
    dstripe.sub()
    _checkout_completed(client, _tenant(client).id)
    dstripe.fail = True
    r = client.post("/api/v1/tenant/delete", headers=headers, json={
        "reason_code": "switching", "password": "a-long-enough-password", "confirm_name": "Acme"})
    assert r.status_code == 502 and "not deleted" in r.json()["detail"]
    assert _acme(client) is not None


def test_only_the_owner_can_delete(client):
    headers = _signup(client)
    s = client.Session()
    u = s.scalars(select(User)).first()
    u.role = "admin"
    s.commit(); s.close()
    r = client.post("/api/v1/tenant/delete", headers=headers, json={
        "reason_code": "switching", "password": "a-long-enough-password", "confirm_name": "Acme"})
    assert r.status_code == 403
    assert len(client.get("/api/v1/tenant/exit-reasons", headers=headers).json()) >= 5


# --- automatic deactivation ------------------------------------------------------
def _set(client, **values):
    s = client.Session()
    t = s.scalars(select(Tenant)).first()
    for k, v in values.items():
        setattr(t, k, v)
    s.commit()
    return s, t.id


def test_trial_ending_without_a_plan_stops_syncing_and_tells_them(client, mail):
    _signup(client)
    s, tid = _set(client, status="trialing", subscription_renews_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert sweep_subscriptions(s)["lapsed"] == 1
    assert s.get(Tenant, tid).status == "past_due" and not s.get(Tenant, tid).syncable
    s.close()
    assert mail and mail[0][0] == "owner@acme.com" and "trial has ended" in mail[0][1]


def test_stripe_account_with_no_renewal_is_stopped_after_the_grace(client, stripe, mail):
    _signup(client)
    stripe.sub()
    _checkout_completed(client, _tenant(client).id)
    s, tid = _set(client, subscription_renews_at=datetime.now(timezone.utc) - timedelta(days=1))
    sweep_subscriptions(s)
    assert s.get(Tenant, tid).status == "active"      # inside the grace: Stripe may still be retrying
    s.close()
    s, tid = _set(client, subscription_renews_at=datetime.now(timezone.utc) - timedelta(days=4))
    sweep_subscriptions(s)
    assert s.get(Tenant, tid).status == "past_due"
    s.close()
    assert "Payment failed" in mail[-1][1]


def test_failed_renewal_and_cancellation_send_a_notice(client, stripe, mail):
    _signup(client)
    stripe.sub()
    _checkout_completed(client, _tenant(client).id)
    assert mail == []  # paying doesn't send a "stopped" notice
    stripe.subscriptions["sub_1"]["status"] = "past_due"
    _post_event(client, {"id": "evt_f", "type": "invoice.payment_failed",
                         "data": {"object": {"subscription": "sub_1", "customer": "cus_1"}}})
    assert _tenant(client).status == "past_due" and "Payment failed" in mail[-1][1]

    stripe.subscriptions["sub_1"]["status"] = "active"
    _post_event(client, {"id": "evt_p", "type": "invoice.paid",
                         "data": {"object": {"subscription": "sub_1", "customer": "cus_1"}}})
    assert _tenant(client).status == "active"
    stripe.subscriptions["sub_1"]["status"] = "canceled"
    _post_event(client, {"id": "evt_c", "type": "customer.subscription.deleted",
                         "data": {"object": {"id": "sub_1", "customer": "cus_1"}}})
    assert _tenant(client).status == "cancelled" and "has ended" in mail[-1][1]
    assert len(mail) == 2
