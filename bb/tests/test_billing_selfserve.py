"""After the first purchase: the tenant's Billing page (renewal, card,
invoices, pay-now, cancel/resume), and the console's Stripe keys — which
take over from .env, stay write-only, and are what webhooks are checked with."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import Tenant, User
from app.services import billing
from tests.test_billing import (  # noqa: F401
    PERIOD_END, SECRET, FakeStripe, _checkout_completed, _post_event, _signup, _tenant, client, stripe,
)


class RichStripe(FakeStripe):
    """FakeStripe plus what the Billing page reads."""

    def __init__(self):
        super().__init__()
        self.invoices = [
            {"id": "in_2", "customer": "cus_1", "number": "BB-0002", "created": PERIOD_END - 86400, "status": "open",
             "amount_due": 14900, "amount_paid": 0, "amount_remaining": 14900, "currency": "usd",
             "hosted_invoice_url": "https://invoice.stripe.test/in_2", "invoice_pdf": "https://pdf.test/in_2",
             "next_payment_attempt": PERIOD_END + 3 * 86400},
            {"id": "in_1", "customer": "cus_1", "number": "BB-0001", "created": PERIOD_END - 31 * 86400, "status": "paid",
             "amount_due": 14900, "amount_paid": 14900, "amount_remaining": 0, "currency": "usd",
             "hosted_invoice_url": "https://invoice.stripe.test/in_1", "invoice_pdf": "https://pdf.test/in_1"},
            {"id": "in_x", "customer": "cus_other", "status": "open", "hosted_invoice_url": "https://x"},
        ]

    def __call__(self, method, path, data=None, idempotency_key=None):
        if path == "customers/cus_1":
            self.calls.append((method, path, data or {}))
            return {"id": "cus_1", "invoice_settings": {"default_payment_method": {
                "type": "card", "card": {"brand": "visa", "last4": "4242", "exp_month": 4, "exp_year": 2030}}}}
        if path == "invoices":
            self.calls.append((method, path, data or {}))
            return {"data": [i for i in self.invoices if i["customer"] == data["customer"]]}
        if path.startswith("invoices/"):
            self.calls.append((method, path, data or {}))
            return next(i for i in self.invoices if i["id"] == path.split("/", 1)[1])
        if path.startswith("subscriptions/") and method == "POST" and "cancel_at_period_end" in (data or {}):
            self.calls.append((method, path, data))
            sub = self.subscriptions[path.split("/", 1)[1]]
            sub["cancel_at_period_end"] = data["cancel_at_period_end"]
            return sub
        if method == "DELETE" and path.startswith("subscriptions/"):
            self.calls.append((method, path, data or {}))
            return {"id": path.split("/", 1)[1], "status": "canceled"}
        if path == "prices" or path.startswith("prices/"):
            self.calls.append((method, path, data or {}))
            if path == "prices":
                return {"data": []}
            pid = path.split("/", 1)[1]
            if pid == "price_growth":
                return {"id": pid, "active": True, "unit_amount": 14900, "currency": "usd",
                        "recurring": {"interval": "month"}}
            if pid == "price_starter":
                return {"id": pid, "active": True, "unit_amount": 5900, "currency": "usd",
                        "recurring": {"interval": "month"}}
            raise billing.BillingError("Stripe: No such price: '%s'" % pid)
        return super().__call__(method, path, data, idempotency_key)


@pytest.fixture
def rich(monkeypatch):
    fake = RichStripe()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    monkeypatch.setattr(billing, "_request", fake)
    return fake


def _subscribed(client, stripe):
    headers = _signup(client)
    stripe.sub(price="price_growth")
    s = client.Session()
    t = s.scalars(select(Tenant)).first()
    t.stripe_customer_id = "cus_1"
    s.commit(); s.close()
    _checkout_completed(client, _tenant(client).id)
    return headers


def test_overview_without_a_subscription(client, rich):
    headers = _signup(client)
    data = client.get("/api/v1/billing/overview", headers=headers).json()
    assert data["enabled"] is True and data["has_customer"] is False and data["invoices"] == []


def test_overview_shows_renewal_card_and_invoices(client, rich):
    headers = _subscribed(client, rich)
    data = client.get("/api/v1/billing/overview", headers=headers).json()
    sub = data["subscription"]
    assert sub["status"] == "active" and sub["cancel_at_period_end"] is False
    assert sub["current_period_end"].startswith(str(__import__("datetime").datetime.utcfromtimestamp(PERIOD_END).date()))
    assert data["payment_method"] == {"brand": "visa", "last4": "4242", "exp_month": 4, "exp_year": 2030}
    assert [i["number"] for i in data["invoices"]] == ["BB-0002", "BB-0001"]
    assert data["open_invoice"]["id"] == "in_2" and data["plan_name"] == "Growth"


def test_pay_now_only_for_this_accounts_open_invoices(client, rich):
    headers = _subscribed(client, rich)
    r = client.post("/api/v1/billing/invoices/in_2/pay", headers=headers)
    assert r.status_code == 200 and r.json()["url"] == "https://invoice.stripe.test/in_2"
    assert client.post("/api/v1/billing/invoices/in_1/pay", headers=headers).status_code == 400   # already paid
    assert client.post("/api/v1/billing/invoices/in_x/pay", headers=headers).status_code == 400   # someone else's


def test_cancel_and_resume_at_period_end(client, rich):
    headers = _subscribed(client, rich)
    assert client.post("/api/v1/billing/cancel", headers=headers).json() == {"cancel_at_period_end": True}
    assert ("POST", "subscriptions/sub_1", {"cancel_at_period_end": True}) in rich.calls
    assert client.get("/api/v1/billing/overview", headers=headers).json()["subscription"]["cancel_at_period_end"]
    assert _tenant(client).status == "active"   # keeps working until the period ends
    assert client.post("/api/v1/billing/resume", headers=headers).json() == {"cancel_at_period_end": False}


def test_update_card_opens_the_portal_on_payment_method_update(client, rich):
    headers = _subscribed(client, rich)
    r = client.post("/api/v1/billing/payment-method", headers=headers)
    assert r.status_code == 200
    session = [d for m, p, d in rich.calls if p == "billing_portal/sessions"][-1]
    assert session["flow_data"]["type"] == "payment_method_update"


def test_viewers_cannot_act(client, rich):
    headers = _subscribed(client, rich)
    s = client.Session()
    u = s.scalars(select(User)).first()
    u.role = "viewer"
    s.commit(); s.close()
    assert client.get("/api/v1/billing/overview", headers=headers).status_code == 200
    assert client.post("/api/v1/billing/cancel", headers=headers).status_code == 403
    assert client.post("/api/v1/billing/invoices/in_2/pay", headers=headers).status_code == 403


# --- console Stripe keys ------------------------------------------------------
def _staff(client):
    from tests.test_platform_admin import staff_login

    client.post("/api/v1/auth/signup", json={"company_name": "Ops", "email": "ops@platform.example.com",
                                             "password": "a-long-enough-password", "timezone": "UTC"})
    s = client.Session()
    u = s.scalars(select(User).where(User.email == "ops@platform.example.com")).first()
    u.is_platform_admin = True
    s.commit(); s.close()
    return {"Authorization": f"Bearer {staff_login(client, 'ops@platform.example.com')}"}


def test_console_keys_turn_billing_on_and_stay_secret(client, monkeypatch):
    fake = RichStripe()
    monkeypatch.setattr(billing, "_request", fake)
    staff = _staff(client)
    assert client.get("/api/v1/public/plans").json()[0]["can_buy_online"] is False

    view = client.patch("/api/v1/admin/stripe", headers=staff, json={
        "secret_key": "sk_test_51Abcdefghijkl4242", "webhook_secret": "whsec_console"}).json()
    assert view["active_source"] == "database" and view["active_mode"] == "test"
    assert view["secret_key_hint"] == "sk_test_…4242" and "51Abcd" not in str(view)
    assert view["has_webhook_secret"] is True
    # A fresh request (website pricing) sees billing on.
    assert any(p["can_buy_online"] for p in client.get("/api/v1/public/plans").json())

    # Webhooks are checked against the console secret, not .env's.
    billing._CONSOLE = None
    event = {"id": "evt_c", "type": "invoice.paid", "data": {"object": {}}}
    assert _post_event(client, event, secret="whsec_console").status_code == 200
    assert _post_event(client, event | {"id": "evt_d"}, secret=SECRET).status_code == 400

    # Switched off: back to .env (empty here), so billing is off again.
    client.patch("/api/v1/admin/stripe", headers=staff, json={"enabled": False})
    assert client.get("/api/v1/admin/stripe", headers=staff).json()["active_source"] == "none"


def test_bad_key_formats_are_refused(client):
    staff = _staff(client)
    assert client.patch("/api/v1/admin/stripe", headers=staff, json={"secret_key": "pk_test_123"}).status_code == 422
    assert client.patch("/api/v1/admin/stripe", headers=staff, json={"webhook_secret": "abc"}).status_code == 422


def test_connection_check_reports_each_plan(client, rich):
    staff = _staff(client)
    result = client.post("/api/v1/admin/stripe/test", headers=staff).json()
    by_plan = {r["plan"]: r for r in result["plans"]}
    assert result["mode"] == "test" and result["webhook_secret_set"] is True
    assert by_plan["Growth"]["ok"] is True
    assert by_plan["Starter"]["ok"] is False and "59.00" in by_plan["Starter"]["message"]   # price mismatch
    assert by_plan["Scale"]["ok"] is False and "can't be bought online" in by_plan["Scale"]["message"]
    assert result["ok"] is False


def test_customers_cannot_reach_the_stripe_page(client, rich):
    headers = _signup(client)
    assert client.get("/api/v1/admin/stripe", headers=headers).status_code == 403


def test_requests_reach_stripe_form_encoded(monkeypatch):
    """The real HTTP layer (no fake _request): bracketed form keys, the key as
    a bearer token, GET params for reads."""
    import httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "x", "url": "https://checkout.stripe.test/x"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(billing.httpx, "request", lambda *a, **kw: httpx.Client(transport=transport).request(*a, **{k: v for k, v in kw.items() if k != "timeout"}))
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_real")
    billing._request("POST", "checkout/sessions", {"line_items": [{"price": "price_1", "quantity": 1}],
                                                   "allow_promotion_codes": True})
    post = seen[-1]
    assert post.headers["authorization"] == "Bearer sk_test_real"
    assert post.headers["content-type"] == "application/x-www-form-urlencoded"
    assert post.content == b"line_items%5B0%5D%5Bprice%5D=price_1&line_items%5B0%5D%5Bquantity%5D=1&allow_promotion_codes=true"
    billing._request("GET", "invoices", {"customer": "cus_1", "expand": ["data.x"]})
    assert seen[-1].url.params["customer"] == "cus_1" and seen[-1].url.params["expand[0]"] == "data.x"


def test_cancel_now_ends_the_plan_but_keeps_the_account(client, rich):
    headers = _subscribed(client, rich)
    r = client.post("/api/v1/billing/cancel", json={"when": "now"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"
    assert ("DELETE", "subscriptions/sub_1", {}) in rich.calls
    tenant = _tenant(client)
    assert tenant.status == "cancelled" and tenant.stripe_subscription_id is None
    # The account and its login are still there.
    assert client.get("/api/v1/tenant", headers=headers).status_code == 200
    # Ending it twice is refused rather than silently repeated.
    assert client.post("/api/v1/billing/cancel", json={"when": "now"}, headers=headers).status_code == 400


def test_a_trial_without_stripe_can_be_discontinued(client, rich):
    headers = _signup(client)
    assert _tenant(client).stripe_subscription_id is None
    r = client.post("/api/v1/billing/cancel", json={"when": "now"}, headers=headers)
    assert r.status_code == 200, r.text
    assert _tenant(client).status == "cancelled"
    assert not [c for c in rich.calls if c[0] == "DELETE"]   # nothing to cancel at Stripe
    # Cancelling at the period end still needs a subscription.
    assert client.post("/api/v1/billing/cancel", headers=headers).status_code == 400
