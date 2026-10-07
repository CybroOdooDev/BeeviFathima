"""Paying yearly: the website's billing choice, the yearly Stripe Price used
for checkout, and the account remembering how it pays."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import PendingSignup, SubscriptionPlan, Tenant
from tests.test_billing import _post_event, _plan, _signup, client, stripe  # noqa: F401
from tests.test_website_onboarding import REG, form, mail  # noqa: F401


@pytest.fixture
def yearly(client):
    """Growth sells yearly too; Starter is monthly-only."""
    s = client.Session()
    g = s.scalar(select(SubscriptionPlan).where(SubscriptionPlan.name == "Growth"))
    g.yearly_price_cents = 149000
    g.stripe_yearly_price_id = "price_growth_year"
    s.commit()
    s.close()
    return client


def _checkout_call(stripe):
    return [c for c in stripe.calls if c[1] == "checkout/sessions"][-1][2]


def test_public_plans_say_which_can_be_paid_yearly(yearly, stripe):
    plans = {p["name"]: p for p in yearly.get("/api/v1/public/plans").json()}
    assert plans["Growth"]["yearly_price_cents"] == 149000
    assert plans["Growth"]["can_buy_yearly"] is True
    assert plans["Starter"]["can_buy_yearly"] is False
    assert "stripe_yearly_price_id" not in plans["Growth"]


def test_yearly_buy_is_refused_for_a_plan_with_no_yearly_price(yearly, stripe, mail):
    r = yearly.post(REG, json=form(mode="buy", plan="Starter", billing="year"))
    assert r.status_code == 400 and "yearly" in r.json()["detail"]
    # The same plan is still fine monthly.
    assert yearly.post(REG, json=form(mode="buy", plan="Starter", billing="month")).status_code == 201


def test_yearly_buy_charges_the_yearly_price_and_the_account_remembers_it(yearly, stripe, mail, monkeypatch):
    monkeypatch.setattr(settings, "site_url", "https://biobridge.example")
    r = yearly.post(REG, json=form(mode="buy", billing="year"))
    assert r.status_code == 201, r.text
    call = _checkout_call(stripe)
    assert call["line_items"][0]["price"] == "price_growth_year"
    assert "billing=year" in call["cancel_url"]

    s = yearly.Session()
    pending = s.scalars(select(PendingSignup)).one()
    assert pending.billing_interval == "year"
    pending_id = pending.id
    s.close()

    stripe.sub("sub_y", price="price_growth_year", customer="cus_y")
    _post_event(yearly, {"id": "evt_y", "type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "subscription": "sub_y", "customer": "cus_y",
        "metadata": {"pending_signup_id": pending_id}}}})
    s = yearly.Session()
    tenant = s.scalars(select(Tenant)).one()
    assert tenant.billing_interval == "year" and tenant.plan.name == "Growth"
    assert tenant.subscription_renews_at is not None
    s.close()


def test_monthly_buy_is_unchanged(yearly, stripe, mail):
    yearly.post(REG, json=form(mode="buy"))
    assert _checkout_call(stripe)["line_items"][0]["price"] == "price_growth"
    s = yearly.Session()
    assert s.scalars(select(PendingSignup)).one().billing_interval == "month"
    s.close()


def test_a_trial_remembers_yearly_and_later_checkout_uses_it(yearly, stripe, mail):
    assert yearly.post(REG, json=form(billing="year")).status_code == 201
    s = yearly.Session()
    tenant = s.scalars(select(Tenant)).one()
    assert tenant.billing_interval == "year"
    s.close()

    # Checkout from inside the app follows the interval the account chose.
    from tests.test_website_onboarding import login, password_from, token_from
    yearly.post("/api/v1/auth/verify-email", json={"token": token_from(mail[0])})
    tokens = login(yearly, "hr@kerala.example.com", password_from(mail[-1])).json()
    head = {"Authorization": f"Bearer {tokens['access_token']}"}
    yearly.post("/api/v1/auth/change-password", headers=head, json={
        "current_password": password_from(mail[-1]), "new_password": "my-own-long-password"})
    r = yearly.post("/api/v1/billing/checkout", headers=head, json={"plan_id": _plan(yearly, "Growth")})
    assert r.status_code == 200, r.text
    assert _checkout_call(stripe)["line_items"][0]["price"] == "price_growth_year"
    # …and an explicit choice wins.
    yearly.post("/api/v1/billing/checkout", headers=head,
                json={"plan_id": _plan(yearly, "Growth"), "interval": "month"})
    assert _checkout_call(stripe)["line_items"][0]["price"] == "price_growth"
