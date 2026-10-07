"""Online billing through Stripe — see app.services.billing for how an
account's plan and status follow its subscription."""

from __future__ import annotations

import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import Principal, audit, get_principal, require_writer
from app.core.config import settings
from app.db.session import get_db
from app.models import SubscriptionPlan
from app.models.tenant import TenantStatus
from app.services import billing

log = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"], dependencies=[Depends(billing.use_config)])


class BillingStatus(BaseModel):
    enabled: bool
    billed_by_stripe: bool
    has_customer: bool


class CheckoutIn(BaseModel):
    plan_id: str
    #: "month" or "year"; omitted = how this account already pays.
    interval: str | None = None


class CancelIn(BaseModel):
    #: "period_end": stop renewing, keep working until the paid period is over.
    #: "now": end the plan immediately — the account stays, but stops syncing.
    when: Literal["period_end", "now"] = "period_end"


class RedirectOut(BaseModel):
    url: str


def _require_enabled() -> None:
    if not billing.enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Online billing is not set up.")


@router.get("", response_model=BillingStatus)
def billing_status(principal: Principal = Depends(get_principal)) -> BillingStatus:
    tenant = principal.tenant
    return BillingStatus(
        enabled=billing.enabled(),
        billed_by_stripe=tenant.billed_by_stripe,
        has_customer=bool(tenant.stripe_customer_id),
    )


@router.post("/checkout", response_model=RedirectOut)
def start_checkout(
    payload: CheckoutIn,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> RedirectOut:
    """Stripe's hosted payment page for one plan. The plan is not applied
    here — only once Stripe confirms payment (checkout.session.completed)."""
    _require_enabled()
    tenant = principal.tenant
    if tenant.billed_by_stripe:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This account already has a subscription — switch plans instead.",
        )
    plan = db.get(SubscriptionPlan, payload.plan_id)
    if plan is None or not plan.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such plan")
    interval = payload.interval if payload.interval in ("month", "year") else (tenant.billing_interval or "month")
    if not plan.price_id_for(interval):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{plan.name} can't be {'paid for yearly' if interval == 'year' else 'bought'} online yet. "
            "Contact support to subscribe.",
        )
    try:
        if not tenant.stripe_customer_id:
            tenant.stripe_customer_id = billing.create_customer(tenant, principal.user.email)
            db.commit()  # keep the customer even if the session call fails
        url = billing.create_checkout_session(tenant, plan, interval)
    except billing.BillingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    audit(db, principal, "billing.checkout", plan.id, plan.name, request)
    db.commit()
    return RedirectOut(url=url)


@router.post("/portal", response_model=RedirectOut)
def open_portal(
    principal: Principal = Depends(require_writer),
) -> RedirectOut:
    """Stripe's Customer Portal: card, invoices, cancellation."""
    _require_enabled()
    if not principal.tenant.stripe_customer_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No billing account yet — choose a plan first.")
    try:
        return RedirectOut(url=billing.create_portal_session(principal.tenant))
    except billing.BillingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


# --- the tenant's Billing page ------------------------------------------------
@router.get("/overview")
def billing_overview(principal: Principal = Depends(get_principal)) -> dict:
    """Subscription, renewal, card and invoices — live from Stripe."""
    tenant = principal.tenant
    base = {"enabled": billing.enabled(), "billed_by_stripe": tenant.billed_by_stripe,
            "has_customer": bool(tenant.stripe_customer_id), "status": tenant.status,
            "plan_name": tenant.plan.name if tenant.plan else None,
            "subscription": None, "payment_method": None, "invoices": [], "open_invoice": None,
            "error": None}
    if not billing.enabled() or not tenant.stripe_customer_id:
        return base
    try:
        base.update(billing.billing_overview(tenant))
    except billing.BillingError as exc:
        log.warning("Billing overview for %s failed: %s", tenant.id, exc)
        base["error"] = str(exc)
    return base


def _subscribed(principal: Principal):
    _require_enabled()
    tenant = principal.tenant
    if not tenant.stripe_subscription_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This account has no active subscription.")
    return tenant


@router.post("/invoices/{invoice_id}/pay", response_model=RedirectOut)
def pay_invoice(
    invoice_id: str,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> RedirectOut:
    """Stripe's hosted invoice page — pay an overdue renewal with any card."""
    _require_enabled()
    if not principal.tenant.stripe_customer_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No billing account yet.")
    try:
        url = billing.payable_invoice_url(principal.tenant, invoice_id)
    except billing.BillingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit(db, principal, "billing.pay_invoice", invoice_id, None, request)
    db.commit()
    return RedirectOut(url=url)


@router.post("/payment-method", response_model=RedirectOut)
def update_payment_method(principal: Principal = Depends(require_writer)) -> RedirectOut:
    """Change the card future renewals are charged to (Stripe-hosted)."""
    _require_enabled()
    if not principal.tenant.stripe_customer_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No billing account yet — choose a plan first.")
    try:
        return RedirectOut(url=billing.create_payment_method_portal_session(principal.tenant))
    except billing.BillingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/cancel")
def cancel_subscription(
    request: Request,
    payload: CancelIn | None = None,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict:
    """Stop the plan. ``period_end`` (default): no more renewals, works until
    the paid period ends. ``now``: ends immediately. Either way the account
    itself stays — logins, connections and history are kept; it just stops
    syncing — until the owner or staff deletes it."""
    when = (payload.when if payload else "period_end")
    tenant = principal.tenant
    if when == "now":
        if tenant.status == TenantStatus.cancelled.value:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This plan has already ended.")
        if tenant.stripe_subscription_id:
            try:
                billing.cancel_subscription_now(tenant.stripe_subscription_id)
            except billing.BillingError as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        was = tenant.stripe_subscription_id or "no subscription"
        tenant.stripe_subscription_id = None
        tenant.pending_plan_id = None
        # Staff's suspension is a stronger state; leave it alone.
        if tenant.status != TenantStatus.suspended.value:
            tenant.status = TenantStatus.cancelled.value
        audit(db, principal, "billing.cancel", was, "now", request)
        db.commit()
        return {"cancelled": True, "status": tenant.status, "cancel_at_period_end": False}

    tenant = _subscribed(principal)
    try:
        sub = billing.set_cancel_at_period_end(tenant.stripe_subscription_id, True)
    except billing.BillingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    audit(db, principal, "billing.cancel", tenant.stripe_subscription_id, "at period end", request)
    db.commit()
    return {"cancel_at_period_end": bool(sub.get("cancel_at_period_end", True))}


@router.post("/resume")
def resume_subscription(
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict:
    """Undo a cancellation that hasn't taken effect yet."""
    tenant = _subscribed(principal)
    try:
        sub = billing.set_cancel_at_period_end(tenant.stripe_subscription_id, False)
    except billing.BillingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    audit(db, principal, "billing.resume", tenant.stripe_subscription_id, None, request)
    db.commit()
    return {"cancel_at_period_end": bool(sub.get("cancel_at_period_end", False))}


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """Stripe → BioBridge. Unauthenticated by design; trusted only through
    the signature. A 5xx makes Stripe retry, so anything that failed to
    apply is rolled back — the event id included — and left for the retry."""
    payload = await request.body()
    try:
        billing.verify_signature(payload, request.headers.get("stripe-signature"))
    except billing.SignatureError as exc:
        log.warning("Stripe webhook refused: %s", exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not JSON") from exc
    try:
        result = billing.handle_event(db, event)
        db.commit()
    except billing.BillingError as exc:
        db.rollback()
        db.info.pop("after_commit_mail", None)
        log.error("Stripe webhook %s could not be applied: %s", event.get("id"), exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    from app.services.onboarding import after_commit_mail

    after_commit_mail(db)
    log.info("Stripe webhook %s (%s): %s", event.get("id"), event.get("type"), result)
    return {"received": True, "result": result}
