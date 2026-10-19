"""Stripe billing: Checkout to start paying, the Customer Portal to manage it,
and webhooks to keep each account's plan and status in step with Stripe.

Deliberately small, and built on httpx rather than the stripe SDK: BioBridge
needs six calls, and a thin client keeps them readable and easy to fake in
tests. Everything is off unless ``STRIPE_SECRET_KEY`` is set.

How an account's state follows Stripe
-------------------------------------
* No card is asked for at signup. The trial is BioBridge's own; the customer
  goes through Checkout when they choose a plan.
* Checkout completing links the account to its Stripe customer and
  subscription, puts it on the plan it paid for, and marks it ``active``
  with ``subscription_renews_at`` = the end of the paid period.
* From then on Stripe is the authority: every webhook re-reads the
  subscription from Stripe and applies it (``apply_subscription``). That makes
  handling idempotent and independent of the order events arrive in — the
  newest state always wins, whichever event carried the news.
* A plan switch on a paid subscription keeps BioBridge's existing rule: it
  lands at the next renewal (``pending_plan_id``). Stripe is told the new
  price straight away with no proration, so the *next* invoice is at the new
  price and nothing is charged or refunded mid-period.
* ``suspended`` belongs to platform staff and is never changed by a webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models import StripeEvent, SubscriptionPlan, Tenant, TenantStatus

log = logging.getLogger(__name__)

#: How far a webhook's signed timestamp may be from now — Stripe's own default.
SIGNATURE_TOLERANCE_SECONDS = 300

#: Stripe subscription status -> BioBridge tenant status. Anything missing
#: (``incomplete``, ``paused``) leaves the account's status as it is.
STATUS_MAP = {
    "active": TenantStatus.active.value,
    "trialing": TenantStatus.active.value,
    "past_due": TenantStatus.past_due.value,
    "unpaid": TenantStatus.past_due.value,
    "canceled": TenantStatus.cancelled.value,
    "incomplete_expired": TenantStatus.cancelled.value,
}


# =============================================================================
# Which keys are in use: the console's (Payments page) or .env's
# =============================================================================
#: (secret key, webhook secret) from the console, loaded per request by
#: ``use_config``; None means fall back to STRIPE_* from the environment.
_CONSOLE: tuple[str, str] | None = None


def load_config(db: Session) -> None:
    """Pick up the console's Stripe keys (or their absence) from ``db``."""
    global _CONSOLE
    from app.services.stripe_settings import console_keys

    try:
        _CONSOLE = console_keys(db)
    except Exception as exc:  # noqa: BLE001 — a missing table must not stop billing
        log.debug("Could not read console Stripe settings, using .env: %s", exc)
        _CONSOLE = None


def use_config(db: Session = Depends(get_db)) -> None:
    """FastAPI dependency for every route that touches Stripe."""
    load_config(db)


def secret_key() -> str:
    return _CONSOLE[0] if _CONSOLE else settings.stripe_secret_key


def webhook_secret() -> str:
    return _CONSOLE[1] if _CONSOLE else settings.stripe_webhook_secret


def enabled() -> bool:
    return bool(secret_key())


class BillingError(Exception):
    """Stripe refused or could not be reached. The message is safe to show."""


class SignatureError(BillingError):
    """A webhook whose signature does not check out — never processed."""


# =============================================================================
# The Stripe API, the six calls BioBridge makes
# =============================================================================
def _flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe takes form-encoded bodies with bracketed keys:
    ``{"line_items": [{"price": "p"}]}`` -> ``line_items[0][price]=p``."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else key
        if value is None:
            continue
        if isinstance(value, dict):
            out.extend(_flatten(value, name))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out.extend(_flatten(item, f"{name}[{i}]"))
                else:
                    out.append((f"{name}[{i}]", str(item)))
        elif isinstance(value, bool):
            out.append((name, "true" if value else "false"))
        else:
            out.append((name, str(value)))
    return out


def _request(method: str, path: str, data: dict[str, Any] | None = None,
             idempotency_key: str | None = None) -> dict[str, Any]:
    if not enabled():
        raise BillingError("Online billing is not set up on this BioBridge.")
    headers = {"Authorization": f"Bearer {secret_key()}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    # Stripe wants application/x-www-form-urlencoded with repeated, bracketed
    # keys. httpx's ``data=`` only takes a dict (a list of pairs is read as raw
    # body chunks and fails mid-send), so the body is encoded here.
    body = None
    if data and method != "GET":
        body = urlencode(_flatten(data)).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        response = httpx.request(
            method,
            f"{settings.stripe_api_base.rstrip('/')}/v1/{path.lstrip('/')}",
            content=body,
            params=_flatten(data) if data and method == "GET" else None,
            headers=headers,
            timeout=settings.http_timeout_seconds,
        )
    except httpx.RequestError as exc:
        raise BillingError(f"Could not reach Stripe: {exc}") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400:
        message = (body.get("error") or {}).get("message") or f"HTTP {response.status_code}"
        raise BillingError(f"Stripe: {message}")
    return body


def create_customer(tenant: Tenant, email: str | None) -> str:
    customer = _request("POST", "customers", {
        "name": tenant.name,
        "email": email,
        "metadata": {"tenant_id": tenant.id},
    }, idempotency_key=f"customer-{tenant.id}")
    return customer["id"]


def create_checkout_session(tenant: Tenant, plan: SubscriptionPlan, interval: str = "month") -> str:
    base = settings.public_base_url.rstrip("/")
    session = _request("POST", "checkout/sessions", {
        "mode": "subscription",
        "customer": tenant.stripe_customer_id,
        "client_reference_id": tenant.id,
        "line_items": [{"price": plan.price_id_for(interval), "quantity": 1}],
        "subscription_data": {"metadata": {"tenant_id": tenant.id, "plan_id": plan.id}},
        "metadata": {"tenant_id": tenant.id, "plan_id": plan.id},
        "allow_promotion_codes": True,
        "success_url": f"{base}/app/#/settings/billing?checkout=success",
        "cancel_url": f"{base}/app/#/settings/billing/choose?checkout=cancelled",
    })
    return session["url"]


def create_signup_checkout_session(pending_id: str, email: str, plan: SubscriptionPlan,
                                   interval: str = "month") -> str:
    """Checkout for a website registration that has no account yet.

    Stripe makes the customer from ``customer_email``; the webhook finds the
    registration again by ``pending_signup_id`` and creates the account then
    (app.services.onboarding.complete_paid_signup).
    """
    from urllib.parse import quote

    site = (settings.site_url or f"{settings.public_base_url.rstrip('/')}/app/#").rstrip("/")
    if settings.site_url:
        success = f"{site}/check-email.html?paid=1&email={quote(email)}"
        cancel = f"{site}/signup.html?plan={quote(plan.name)}&mode=buy&billing={interval}&cancelled=1"
    else:
        success = f"{site}/login?registered=1"
        cancel = f"{site}/login"
    session = _request("POST", "checkout/sessions", {
        "mode": "subscription",
        "customer_email": email,
        "client_reference_id": f"signup:{pending_id}",
        "line_items": [{"price": plan.price_id_for(interval), "quantity": 1}],
        "subscription_data": {"metadata": {"pending_signup_id": pending_id, "plan_id": plan.id}},
        "metadata": {"pending_signup_id": pending_id, "plan_id": plan.id},
        "allow_promotion_codes": True,
        "success_url": success,
        "cancel_url": cancel,
    })
    return session["url"]


def create_portal_session(tenant: Tenant) -> str:
    base = settings.public_base_url.rstrip("/")
    session = _request("POST", "billing_portal/sessions", {
        "customer": tenant.stripe_customer_id,
        "return_url": f"{base}/app/#/settings/billing",
    })
    return session["url"]


def retrieve_subscription(subscription_id: str) -> dict[str, Any]:
    return _request("GET", f"subscriptions/{subscription_id}")


def set_subscription_price(subscription: dict[str, Any], price_id: str) -> None:
    """Point the subscription at another price from the next invoice on —
    no proration, so nothing is charged or refunded for the current period
    (BioBridge's own plan switch waits for renewal to match)."""
    item = _items(subscription)[0]
    if (item.get("price") or {}).get("id") == price_id:
        return
    _request("POST", f"subscriptions/{subscription['id']}", {
        "items": [{"id": item["id"], "price": price_id}],
        "proration_behavior": "none",
    })


def create_payment_method_portal_session(tenant: Tenant) -> str:
    """The Customer Portal opened straight on "update payment method"."""
    base = settings.public_base_url.rstrip("/")
    session = _request("POST", "billing_portal/sessions", {
        "customer": tenant.stripe_customer_id,
        "return_url": f"{base}/app/#/settings/billing?card=updated",
        "flow_data": {
            "type": "payment_method_update",
            "after_completion": {"type": "redirect",
                                 "redirect": {"return_url": f"{base}/app/#/settings/billing?card=updated"}},
        },
    })
    return session["url"]


def set_cancel_at_period_end(subscription_id: str, cancel: bool) -> dict[str, Any]:
    """Cancel at the end of the paid period (access continues until then), or
    undo that. Never an immediate cancellation from the app."""
    return _request("POST", f"subscriptions/{subscription_id}", {"cancel_at_period_end": cancel})


def cancel_subscription_now(subscription_id: str) -> None:
    """End a subscription immediately, without proration or refund — used
    only when the account itself is being deleted."""
    try:
        _request("DELETE", f"subscriptions/{subscription_id}")
    except BillingError as exc:
        if "No such subscription" in str(exc) or "canceled" in str(exc).lower():
            return  # already gone in Stripe: nothing left to stop
        raise


def _card(pm: Any) -> dict[str, Any] | None:
    if not isinstance(pm, dict):
        return None
    card = pm.get("card") or {}
    if card:
        return {"brand": card.get("brand"), "last4": card.get("last4"),
                "exp_month": card.get("exp_month"), "exp_year": card.get("exp_year")}
    return {"brand": pm.get("type"), "last4": None, "exp_month": None, "exp_year": None}


def _ts(value: Any) -> str | None:
    return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat() if value else None


def _invoice_out(inv: dict[str, Any]) -> dict[str, Any]:
    status = inv.get("status")
    return {
        "id": inv.get("id"),
        "number": inv.get("number"),
        "created": _ts(inv.get("created")),
        "period_end": _ts(inv.get("period_end")),
        "amount_due": inv.get("amount_due"),
        "amount_paid": inv.get("amount_paid"),
        "amount_remaining": inv.get("amount_remaining"),
        "currency": inv.get("currency"),
        "status": status,
        "attempt_count": inv.get("attempt_count"),
        "next_payment_attempt": _ts(inv.get("next_payment_attempt")),
        "hosted_invoice_url": inv.get("hosted_invoice_url"),
        "invoice_pdf": inv.get("invoice_pdf"),
        "payable": status == "open" and bool(inv.get("hosted_invoice_url")),
    }


def billing_overview(tenant: Tenant) -> dict[str, Any]:
    """Everything the tenant's Billing page shows, read live from Stripe."""
    out: dict[str, Any] = {"subscription": None, "payment_method": None, "invoices": [],
                           "open_invoice": None}
    if not tenant.stripe_customer_id:
        return out
    customer = _request("GET", f"customers/{tenant.stripe_customer_id}",
                        {"expand": ["invoice_settings.default_payment_method"]})
    pm = (customer.get("invoice_settings") or {}).get("default_payment_method")

    if tenant.stripe_subscription_id:
        sub = _request("GET", f"subscriptions/{tenant.stripe_subscription_id}",
                       {"expand": ["default_payment_method"]})
        item = (_items(sub)[:1] or [{}])[0]
        price = item.get("price") or {}
        period_end = _period_end(sub)
        out["subscription"] = {
            "status": sub.get("status"),
            "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
            "cancel_at": _ts(sub.get("cancel_at")),
            "current_period_end": period_end.isoformat() if period_end else None,
            "unit_amount": price.get("unit_amount"),
            "quantity": item.get("quantity") or 1,
            "currency": price.get("currency"),
            "interval": (price.get("recurring") or {}).get("interval") or "month",
        }
        if isinstance(sub.get("default_payment_method"), dict):
            pm = sub["default_payment_method"]
    out["payment_method"] = _card(pm)

    invoices = _request("GET", "invoices", {"customer": tenant.stripe_customer_id, "limit": 12})
    rows = [_invoice_out(inv) for inv in invoices.get("data") or [] if inv.get("status") != "draft"]
    out["invoices"] = rows
    out["open_invoice"] = next((r for r in rows if r["payable"]), None)
    return out


def payable_invoice_url(tenant: Tenant, invoice_id: str) -> str:
    """Stripe's hosted page for one open invoice of *this* tenant."""
    inv = _request("GET", f"invoices/{invoice_id}")
    if inv.get("customer") != tenant.stripe_customer_id:
        raise BillingError("That invoice is not on this account.")
    if inv.get("status") != "open" or not inv.get("hosted_invoice_url"):
        raise BillingError("That invoice has nothing left to pay.")
    return inv["hosted_invoice_url"]


# =============================================================================
# Webhooks
# =============================================================================
def verify_signature(payload: bytes, header: str | None, *, now: float | None = None) -> None:
    """Stripe's scheme: ``Stripe-Signature: t=<ts>,v1=<hex>[,v1=…]`` where each
    v1 is HMAC-SHA256 of ``"<ts>.<raw body>"`` under the endpoint secret."""
    secret = webhook_secret()
    if not secret:
        raise SignatureError("Webhook signing secret is not configured.")
    if not header:
        raise SignatureError("Missing Stripe-Signature header.")
    parts: dict[str, list[str]] = {}
    for piece in header.split(","):
        key, _, value = piece.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError, IndexError) as exc:
        raise SignatureError("Malformed Stripe-Signature header.") from exc
    if abs((now or time.time()) - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
        raise SignatureError("Webhook timestamp is outside the tolerance window.")
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise SignatureError("Webhook signature does not match.")


def handle_event(db: Session, event: dict[str, Any]) -> str:
    """Apply one verified event. Returns a short note for the response/log."""
    event_id = event.get("id") or ""
    kind = event.get("type") or ""
    if db.get(StripeEvent, event_id):
        return "duplicate"
    db.add(StripeEvent(id=event_id, type=kind))
    db.flush()

    obj = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed":
        if obj.get("mode") != "subscription" or not obj.get("subscription"):
            return "ignored"
        pending_id = (obj.get("metadata") or {}).get("pending_signup_id")
        if pending_id:
            # A website registration that has just paid: the account is made now.
            from app.services.onboarding import complete_paid_signup

            tenant = complete_paid_signup(db, pending_id, obj)
            if tenant is None:
                return "unknown signup"
            tenant.stripe_customer_id = obj.get("customer") or tenant.stripe_customer_id
            tenant.stripe_subscription_id = obj["subscription"]
            apply_subscription(db, tenant, retrieve_subscription(obj["subscription"]), first=True)
            return "account created"
        tenant = _tenant_for(db, obj.get("client_reference_id") or (obj.get("metadata") or {}).get("tenant_id"),
                             obj.get("customer"))
        if tenant is None:
            return "unknown tenant"
        tenant.stripe_customer_id = obj.get("customer") or tenant.stripe_customer_id
        tenant.stripe_subscription_id = obj["subscription"]
        apply_subscription(db, tenant, retrieve_subscription(obj["subscription"]), first=True)
        return "linked"

    if kind.startswith("customer.subscription.") or kind.startswith("invoice."):
        subscription_id = obj.get("id") if kind.startswith("customer.subscription.") else obj.get("subscription")
        if not subscription_id:
            # Newer API versions nest it under parent.subscription_details.
            subscription_id = ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
        if not subscription_id:
            return "ignored"
        metadata = obj.get("metadata") or {}
        tenant = db.scalar(select(Tenant).where(Tenant.stripe_subscription_id == subscription_id)) \
            or _tenant_for(db, metadata.get("tenant_id"), obj.get("customer"))
        if tenant is None:
            return "unknown tenant"
        # Re-read rather than trust this event's copy: an older event arriving
        # late must not undo a newer state.
        subscription = retrieve_subscription(subscription_id)
        if tenant.stripe_subscription_id not in (None, subscription_id) \
                and subscription.get("status") in ("canceled", "incomplete_expired"):
            return "stale subscription"  # an old, replaced subscription ending
        tenant.stripe_subscription_id = subscription_id
        apply_subscription(db, tenant, subscription)
        return "applied"

    return "ignored"


def _tenant_for(db: Session, tenant_id: str | None, customer_id: str | None) -> Tenant | None:
    tenant = db.get(Tenant, tenant_id) if tenant_id else None
    if tenant is None and customer_id:
        tenant = db.scalar(select(Tenant).where(Tenant.stripe_customer_id == customer_id))
    return tenant


def _items(subscription: dict[str, Any]) -> list[dict[str, Any]]:
    return ((subscription.get("items") or {}).get("data")) or []


def _period_end(subscription: dict[str, Any]) -> datetime | None:
    # Moved from the subscription onto its items in Stripe API 2025-03-31;
    # read either so the account's pinned API version doesn't matter.
    raw = subscription.get("current_period_end")
    if raw is None:
        ends = [i.get("current_period_end") for i in _items(subscription) if i.get("current_period_end")]
        raw = max(ends) if ends else None
    return datetime.fromtimestamp(int(raw), tz=timezone.utc) if raw else None


def apply_subscription(db: Session, tenant: Tenant, subscription: dict[str, Any],
                       *, first: bool = False) -> None:
    """Bring one account in line with its Stripe subscription."""
    status = STATUS_MAP.get(subscription.get("status") or "")
    ended = status == TenantStatus.cancelled.value
    was_syncing = tenant.syncable

    if tenant.status != TenantStatus.suspended.value and status:
        tenant.status = status
        if status == TenantStatus.active.value:
            tenant.suspended_at = None

    period_end = _period_end(subscription)
    previous_end = tenant.subscription_renews_at
    if previous_end is not None and previous_end.tzinfo is None:
        previous_end = previous_end.replace(tzinfo=timezone.utc)
    # A new billing period has started since we last looked: whatever price
    # it is on is now the plan in force (a queued switch lands here).
    renewed = bool(period_end and previous_end and period_end > previous_end)
    if period_end and not ended:
        tenant.subscription_renews_at = period_end

    price_id = ((_items(subscription)[:1] or [{}])[0].get("price") or {}).get("id")
    plan = db.scalar(select(SubscriptionPlan).where(or_(
        SubscriptionPlan.stripe_price_id == price_id,
        SubscriptionPlan.stripe_yearly_price_id == price_id))) if price_id else None
    if plan is not None and not ended:
        # The price in use says how they pay: monthly or yearly.
        tenant.billing_interval = "year" if plan.stripe_yearly_price_id == price_id else "month"
        if first or renewed or tenant.plan_id is None:
            # The plan they just paid for is in force from now.
            tenant.plan_id = plan.id
            tenant.pending_plan_id = None
            floor = tenant.limit_for("min_sync_interval_minutes", plan)
            if floor:
                tenant.sync_interval_minutes = max(tenant.sync_interval_minutes, floor)
        elif plan.id != tenant.plan_id:
            # Changed mid-period (here, the portal, or the Stripe dashboard):
            # lands at renewal, same as any paid switch.
            tenant.pending_plan_id = plan.id
        else:
            tenant.pending_plan_id = None

    if ended:
        tenant.stripe_subscription_id = None
        tenant.pending_plan_id = None
    db.flush()

    if was_syncing and not tenant.syncable and not first:
        # Stripe just stopped this account: a failed renewal or a
        # cancellation reaching its end. Tell them, so a quiet Odoo isn't
        # how they find out.
        from app.services.notices import notify_sync_stopped

        notify_sync_stopped(db, tenant, "cancelled" if ended else "payment_failed")
