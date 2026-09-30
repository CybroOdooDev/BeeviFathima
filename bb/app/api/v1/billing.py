"""Online billing through Stripe — see app.services.billing for how an
account's plan and status follow its subscription."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import Principal, audit, get_principal, require_writer
from app.core.config import settings
from app.db.session import get_db
from app.models import SubscriptionPlan
from app.services import billing

log = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])


class BillingStatus(BaseModel):
    enabled: bool
    billed_by_stripe: bool
    has_customer: bool


class CheckoutIn(BaseModel):
    plan_id: str


class RedirectOut(BaseModel):
    url: str


def _require_enabled() -> None:
    if not settings.billing_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Online billing is not set up.")


@router.get("", response_model=BillingStatus)
def billing_status(principal: Principal = Depends(get_principal)) -> BillingStatus:
    tenant = principal.tenant
    return BillingStatus(
        enabled=settings.billing_enabled,
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
    if not plan.stripe_price_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{plan.name} can't be bought online yet. Contact support to subscribe.",
        )
    try:
        if not tenant.stripe_customer_id:
            tenant.stripe_customer_id = billing.create_customer(tenant, principal.user.email)
            db.commit()  # keep the customer even if the session call fails
        url = billing.create_checkout_session(tenant, plan)
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
        log.error("Stripe webhook %s could not be applied: %s", event.get("id"), exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    log.info("Stripe webhook %s (%s): %s", event.get("id"), event.get("type"), result)
    return {"received": True, "result": result}
