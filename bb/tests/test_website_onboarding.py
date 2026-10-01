"""Accounts made from the marketing website: trial and paid registration,
email verification releasing emailed login details, and the forced first
password change."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import PendingSignup, Tenant, User
from app.services import email_verification, onboarding
from tests.test_billing import FakeStripe, _post_event, client, stripe  # noqa: F401

REG = "/api/v1/public/register"


@pytest.fixture
def mail(monkeypatch):
    sent: list[dict] = []

    def capture(to, subject, body, db=None):
        sent.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(email_verification, "send_email", capture)
    monkeypatch.setattr(onboarding, "send_email", capture)
    onboarding._hits.clear()
    yield sent
    onboarding._hits.clear()


def form(**over):
    return {"company_name": "Kerala Textiles", "email": "hr@kerala.example.com",
            "timezone": "Asia/Kolkata", "plan": "Growth", "mode": "trial", **over}


def token_from(message) -> str:
    return re.search(r"token=([\w\-]+)", message["body"]).group(1)


def password_from(message) -> str:
    return re.search(r"Password:\s+(\S+)", message["body"]).group(1)


def user(client, email="hr@kerala.example.com") -> User:
    s = client.Session()
    try:
        u = s.scalars(select(User).where(User.email == email)).first()
        if u:
            s.expunge(u)
        return u
    finally:
        s.close()


def login(client, email, password):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


# --- trial ------------------------------------------------------------------
def test_trial_registration_emails_login_details_after_verification(client, mail):
    r = client.post(REG, json=form())
    assert r.status_code == 201, r.text
    assert r.json()["next"] == "check_email"

    u = user(client)
    assert u.credentials_pending and u.email_verified_at is None
    assert len(mail) == 1 and "login details" in mail[0]["body"]

    # No password exists yet: signing in says what to do instead.
    refused = login(client, u.email, "anything-at-all")
    assert refused.status_code == 403 and "Confirm your email" in refused.json()["detail"]

    verified = client.post("/api/v1/auth/verify-email", json={"token": token_from(mail[0])})
    assert verified.status_code == 200 and "login details" in verified.json()["message"]
    assert mail[-1]["subject"] == "Your BioBridge account is ready"
    password = password_from(mail[-1])
    assert u.email in mail[-1]["body"]

    s = client.Session()
    tenant = s.scalars(select(Tenant)).first()
    assert tenant.status == "trialing" and tenant.plan.name == "Growth"
    s.close()


def test_emailed_password_must_be_changed_before_anything_else(client, mail):
    client.post(REG, json=form())
    client.post("/api/v1/auth/verify-email", json={"token": token_from(mail[0])})
    password = password_from(mail[-1])

    tokens = login(client, "hr@kerala.example.com", password)
    assert tokens.status_code == 200, tokens.text
    head = {"Authorization": f"Bearer {tokens.json()['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=head).json()["must_change_password"] is True
    blocked = client.get("/api/v1/tenant", headers=head)
    assert blocked.status_code == 403 and "Set a new password" in blocked.json()["detail"]

    wrong = client.post("/api/v1/auth/change-password", headers=head,
                        json={"current_password": "nope", "new_password": "my-own-long-password"})
    assert wrong.status_code == 400
    ok = client.post("/api/v1/auth/change-password", headers=head,
                     json={"current_password": password, "new_password": "my-own-long-password"})
    assert ok.status_code == 200, ok.text
    assert client.get("/api/v1/tenant", headers=head).status_code == 200
    assert login(client, "hr@kerala.example.com", password).status_code == 401
    assert login(client, "hr@kerala.example.com", "my-own-long-password").status_code == 200


def test_resend_sends_whatever_the_address_is_waiting_for(client, mail):
    client.post(REG, json=form())
    client.post("/api/v1/public/resend", json={"email": "hr@kerala.example.com"})
    assert len(mail) == 2 and "token=" in mail[1]["body"], "unconfirmed → a fresh link"

    client.post("/api/v1/auth/verify-email", json={"token": token_from(mail[1])})
    first = password_from(mail[-1])
    client.post("/api/v1/public/resend", json={"email": "hr@kerala.example.com"})
    second = password_from(mail[-1])
    assert first != second, "unused login details → a fresh password"
    assert login(client, "hr@kerala.example.com", first).status_code == 401
    assert login(client, "hr@kerala.example.com", second).status_code == 200

    before = len(mail)
    r = client.post("/api/v1/public/resend", json={"email": "nobody@nowhere.example.com"})
    assert r.status_code == 200 and len(mail) == before, "same answer, nothing sent"


def test_duplicate_email_honeypot_and_rate_limit(client, mail, monkeypatch):
    assert client.post(REG, json=form()).status_code == 201
    assert client.post(REG, json=form()).status_code == 409

    bot = client.post(REG, json=form(email="bot@spam.example.com", website="http://x"))
    assert bot.status_code == 201 and user(client, "bot@spam.example.com") is None

    monkeypatch.setattr(settings, "registration_rate_per_hour", 3)
    onboarding._hits.clear()
    codes = [client.post(REG, json=form(email=f"p{i}@acme.example.com")).status_code for i in range(4)]
    assert codes[-1] == 429


# --- buy --------------------------------------------------------------------
def test_buy_without_online_billing_is_refused(client, mail):
    r = client.post(REG, json=form(mode="buy"))
    assert r.status_code == 400 and "free trial" in r.json()["detail"]


def test_buy_creates_the_account_only_when_payment_is_confirmed(client, mail, stripe, monkeypatch):
    monkeypatch.setattr(settings, "site_url", "https://biobridge.example")
    r = client.post(REG, json=form(mode="buy"))
    assert r.status_code == 201, r.text
    assert r.json()["next"] == "checkout" and r.json()["checkout_url"].startswith("https://checkout.stripe.test")
    call = [c for c in stripe.calls if c[1] == "checkout/sessions"][-1][2]
    assert call["customer_email"] == "hr@kerala.example.com"
    assert call["success_url"].startswith("https://biobridge.example/check-email.html")
    assert user(client) is None and not mail, "nothing exists until Stripe says it's paid"

    s = client.Session()
    pending_id = s.scalars(select(PendingSignup)).first().id
    s.close()
    stripe.sub("sub_9", price="price_growth", customer="cus_9")
    event = {"id": "evt_signup", "type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "subscription": "sub_9", "customer": "cus_9",
        "metadata": {"pending_signup_id": pending_id}}}}
    assert _post_event(client, event).status_code == 200

    s = client.Session()
    tenant = s.scalars(select(Tenant)).one()
    assert tenant.status == "active" and tenant.plan.name == "Growth"
    assert tenant.stripe_subscription_id == "sub_9" and tenant.stripe_customer_id == "cus_9"
    s.close()
    assert len(mail) == 1 and "https://biobridge.example/verified.html?token=" in mail[0]["body"]

    # Stripe retrying the same event, or a second event, makes nothing twice.
    _post_event(client, {**event, "id": "evt_signup_2"})
    s = client.Session()
    assert len(s.scalars(select(Tenant)).all()) == 1
    s.close()


def test_public_plans_say_which_can_be_bought(client, stripe):
    plans = {p["name"]: p for p in client.get("/api/v1/public/plans").json()}
    assert plans["Growth"]["can_buy_online"] is True
    assert plans["Scale"]["can_buy_online"] is False, "no Stripe Price"
    assert "stripe_price_id" not in plans["Growth"]


def test_paid_signup_mails_nothing_if_the_webhook_fails(client, mail, stripe, monkeypatch):
    """The confirmation link goes out only after the account is saved: a
    webhook that fails (and will be retried by Stripe) must not have mailed
    a link to a token that was rolled back — and the retry then sends it."""
    from app.services import billing as billing_mod

    assert client.post(REG, json=form(mode="buy")).status_code == 201
    s = client.Session()
    pending_id = s.scalars(select(PendingSignup)).first().id
    s.close()
    event = {"id": "evt_fail", "type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "subscription": "sub_9", "customer": "cus_9",
        "metadata": {"pending_signup_id": pending_id}}}}

    real = billing_mod.retrieve_subscription

    def boom(*a, **k):
        raise billing_mod.BillingError("Stripe: temporarily unavailable")
    monkeypatch.setattr(billing_mod, "retrieve_subscription", boom)
    assert _post_event(client, event).status_code == 503
    assert mail == [] and user(client) is None

    monkeypatch.setattr(billing_mod, "retrieve_subscription", real)
    stripe.sub("sub_9", price="price_growth", customer="cus_9")
    assert _post_event(client, event).status_code == 200
    assert len(mail) == 1 and "token=" in mail[0]["body"]


def test_first_password_needs_no_emailed_password_but_later_changes_do(client, mail):
    client.post(REG, json=form())
    client.post("/api/v1/auth/verify-email", json={"token": token_from(mail[0])})
    password = password_from(mail[-1])
    head = {"Authorization": f"Bearer {login(client, 'hr@kerala.example.com', password).json()['access_token']}"}

    # Reusing the emailed one isn't a new password.
    same = client.post("/api/v1/auth/change-password", headers=head, json={"new_password": password})
    assert same.status_code == 400
    ok = client.post("/api/v1/auth/change-password", headers=head, json={"new_password": "my-own-long-password"})
    assert ok.status_code == 200, ok.text
    assert client.get("/api/v1/tenant", headers=head).status_code == 200

    # From now on the current password is required again.
    again = client.post("/api/v1/auth/change-password", headers=head, json={"new_password": "another-long-password"})
    assert again.status_code == 400 and "current password" in again.json()["detail"]
