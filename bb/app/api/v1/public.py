"""What the marketing website calls: plans, registration, resend.

No authentication — these are the only routes a visitor with no account can
reach besides login. Registration and resend are rate limited per IP and
carry a honeypot field, because both send email to an address the visitor
typed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models import PendingSignup, SubscriptionPlan
from app.schemas import MessageOut, PublicPlanOut, RegisterIn, RegisterOut, ResendIn
from app.services import billing, onboarding
from app.services.email_check import UngenuineEmailError, assert_genuine_email

log = logging.getLogger(__name__)
router = APIRouter(prefix="/public", tags=["public"], dependencies=[Depends(billing.use_config)])

CHECK_EMAIL = "Check your inbox — we've sent a link to confirm your email."
RESEND_OK = "If that address has an account waiting, we've sent the email again."


def _ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _can_buy(plan: SubscriptionPlan) -> bool:
    return bool(billing.enabled() and plan.stripe_price_id)


@router.get("/plans", response_model=list[PublicPlanOut])
def plans(db: Session = Depends(get_db)) -> list[PublicPlanOut]:
    """Active plans, cheapest first, for the website's pricing and signup pages."""
    rows = db.scalars(
        select(SubscriptionPlan)
        .where(SubscriptionPlan.is_active.is_(True))
        .order_by(SubscriptionPlan.monthly_price_cents.is_(None),
                  SubscriptionPlan.monthly_price_cents, SubscriptionPlan.name)
    ).all()
    out = []
    for plan in rows:
        item = PublicPlanOut.model_validate(plan)
        item.can_buy_online = _can_buy(plan)
        out.append(item)
    return out


def _find_plan(db: Session, ref: str | None) -> SubscriptionPlan | None:
    if not ref:
        return db.scalars(select(SubscriptionPlan).where(
            SubscriptionPlan.is_default.is_(True), SubscriptionPlan.is_active.is_(True))).first()
    plan = db.get(SubscriptionPlan, ref)
    if plan is None:
        plan = db.scalars(select(SubscriptionPlan).where(
            func.lower(SubscriptionPlan.name) == ref.strip().lower())).first()
    if plan is None or not plan.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That plan isn't available.")
    return plan


@router.post("/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)) -> RegisterOut:
    """The website's registration form.

    ``mode: trial`` makes the account now (trialing, no card) and sends the
    verification email. ``mode: buy`` makes no account — it returns a Stripe
    Checkout link, and the account is made when payment is confirmed.
    Either way the login details are emailed once the address is confirmed.
    """
    if payload.website:
        # A bot filled the hidden field. Look successful, do nothing.
        log.info("Registration honeypot tripped from %s", _ip(request))
        return RegisterOut(next="check_email", message=CHECK_EMAIL)
    if not onboarding.allow(_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many attempts from here. Please try again in an hour.")
    try:
        email = assert_genuine_email(payload.email).lower()
    except UngenuineEmailError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if onboarding.email_taken(db, email):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "An account with that email already exists — sign in instead.")
    plan = _find_plan(db, payload.plan)

    if payload.mode == "buy":
        if plan is None or not _can_buy(plan):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "That plan can't be bought online yet — start a free trial, or contact us.",
            )
        pending = PendingSignup(
            company_name=payload.company_name.strip(), email=email,
            full_name=payload.full_name, timezone=payload.timezone, plan_id=plan.id,
        )
        db.add(pending)
        db.flush()
        try:
            url = billing.create_signup_checkout_session(pending.id, email, plan)
        except billing.BillingError as exc:
            db.rollback()
            log.warning("Checkout for %s failed: %s", email, exc)
            raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                                "We couldn't open the payment page. Please try again.") from exc
        db.commit()
        return RegisterOut(next="checkout", checkout_url=url,
                           message="Taking you to payment…")

    _tenant, user = onboarding.create_account(
        db, company_name=payload.company_name, email=email, full_name=payload.full_name,
        timezone_name=payload.timezone, plan=plan, paid=False,
    )
    onboarding.start_verification(user)
    db.commit()
    return RegisterOut(next="check_email", message=CHECK_EMAIL)


@router.post("/resend", response_model=MessageOut)
def resend(payload: ResendIn, request: Request, db: Session = Depends(get_db)) -> MessageOut:
    """"Didn't get the email?" — sends the next email this address is waiting
    for (verification link, or login details), and says the same thing
    whether or not the address has an account."""
    if not onboarding.allow(_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many attempts from here. Please try again in an hour.")
    onboarding.resend(db, payload.email)
    return MessageOut(message=RESEND_OK)
