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
def test_confirming_the_email_sets_the_password_and_signs_in(client, mail):
    r = client.post(REG, json=form())
    assert r.status_code == 201, r.text
    assert r.json()["next"] == "check_email"

    u = user(client)
    assert u.credentials_pending and u.email_verified_at is None
    assert len(mail) == 1 and "choose your password" in mail[0]["body"]
    token = token_from(mail[0])

    # No password exists yet: signing in says what to do instead.
    refused = login(client, u.email, "anything-at-all")
    assert refused.status_code == 403 and "Confirm your email" in refused.json()["detail"]

    # The page asks first: the link is good and a password is needed.
    check = client.post("/api/v1/auth/verify-email/check", json={"token": token})
    assert check.status_code == 200 and check.json() == {"email": u.email, "needs_password": True}

    # No password, or a short one, is refused without spending the link.
    assert client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 400
    assert client.post("/api/v1/auth/verify-email", json={"token": token, "password": "short"}).status_code == 422
    assert client.post("/api/v1/auth/verify-email/check", json={"token": token}).status_code == 200

    done = client.post("/api/v1/auth/verify-email", json={"token": token, "password": "my-own-long-password"})
    assert done.status_code == 200, done.text
    assert done.json()["signin_code"] and "password set" in done.json()["message"]

    # The email states the login: the address and the password they chose — never the password itself.
    note = mail[-1]
    assert note["subject"] == "Your BioBridge account is ready"
    assert u.email in note["body"] and "my-own-long-password" not in note["body"]
    assert "the password you chose" in note["body"]

    # The chosen password works at once, with nothing forced afterwards.
    tokens = login(client, u.email, "my-own-long-password")
    assert tokens.status_code == 200, tokens.text
    head = {"Authorization": f"Bearer {tokens.json()['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=head).json()["must_change_password"] is False
    assert client.get("/api/v1/tenant", headers=head).status_code == 200

    s = client.Session()
    tenant = s.scalars(select(Tenant)).first()
    assert tenant.status == "trialing" and tenant.plan.name == "Growth"
    s.close()


def test_the_sign_in_button_code_opens_the_dashboard_once(client, mail):
    client.post(REG, json=form())
    done = client.post("/api/v1/auth/verify-email",
                       json={"token": token_from(mail[0]), "password": "my-own-long-password"})
    code = done.json()["signin_code"]

    session = client.post("/api/v1/auth/signin-code", json={"code": code})
    assert session.status_code == 200, session.text
    head = {"Authorization": f"Bearer {session.json()['access_token']}"}
    assert client.get("/api/v1/tenant", headers=head).status_code == 200

    again = client.post("/api/v1/auth/signin-code", json={"code": code})
    assert again.status_code == 400, "a code works once"
    assert client.post("/api/v1/auth/signin-code", json={"code": "not-a-real-code-at-all"}).status_code == 400


def test_a_password_reset_token_is_not_a_sign_in_code(client, mail):
    client.post(REG, json=form())
    client.post("/api/v1/auth/verify-email",
                json={"token": token_from(mail[0]), "password": "my-own-long-password"})
    from app.services import password_reset

    s = client.Session()
    reset_user = s.scalars(select(User).where(User.email == "hr@kerala.example.com")).one()
    _, reset = password_reset.request_reset(s, reset_user.email)
    s.commit()
    s.close()
    assert client.post("/api/v1/auth/signin-code", json={"code": reset}).status_code == 400


def test_resend_sends_a_fresh_link_only_while_unconfirmed(client, mail):
    client.post(REG, json=form())
    client.post("/api/v1/public/resend", json={"email": "hr@kerala.example.com"})
    assert len(mail) == 2 and "token=" in mail[1]["body"], "unconfirmed → a fresh link"
    assert client.post("/api/v1/auth/verify-email/check", json={"token": token_from(mail[0])}).status_code == 400, \
        "the older link no longer works"

    client.post("/api/v1/auth/verify-email", json={"token": token_from(mail[1]), "password": "my-own-long-password"})
    before = len(mail)
    client.post("/api/v1/public/resend", json={"email": "hr@kerala.example.com"})
    assert len(mail) == before, "confirmed with a chosen password → nothing more to send"

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


def test_changing_the_password_later_needs_the_current_one(client, mail):
    client.post(REG, json=form())
    client.post("/api/v1/auth/verify-email",
                json={"token": token_from(mail[0]), "password": "my-own-long-password"})
    head = {"Authorization": f"Bearer {login(client, 'hr@kerala.example.com', 'my-own-long-password').json()['access_token']}"}

    again = client.post("/api/v1/auth/change-password", headers=head, json={"new_password": "another-long-password"})
    assert again.status_code == 400 and "current password" in again.json()["detail"]
    ok = client.post("/api/v1/auth/change-password", headers=head,
                     json={"current_password": "my-own-long-password", "new_password": "another-long-password"})
    assert ok.status_code == 200, ok.text
