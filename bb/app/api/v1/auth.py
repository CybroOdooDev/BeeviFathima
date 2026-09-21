"""Signup, login, refresh, logout."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import (
    SCOPE_STAFF,
    SCOPE_TENANT,
    access_ttl,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    refresh_ttl,
    scope_of,
    verify_password,
)
from app.db.session import get_db
from app.models import SubscriptionPlan, Tenant, TenantStatus, User, UserRole, UserSession
from app.schemas import (
    LoginRequest,
    MessageOut,
    SignupRequest,
    SubscriptionPlanOut,
    TokenPair,
    UserOut,
)
from app.services.timeutils import ensure_aware, is_past

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

#: One message for every way a sign-in can fail on credentials, at either door.
#: Anything more specific is an oracle — for which emails exist, or for which of
#: them are staff.
BAD_CREDENTIALS = "Incorrect email or password"


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "tenant"


def _unique_slug(db: Session, base: str) -> str:
    slug, n = base, 1
    while db.scalar(select(func.count(Tenant.id)).where(Tenant.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _issue(db: Session, user: User, request: Request, scope: str) -> TokenPair:
    """Mint a token pair for one surface and record the revocable session.

    ``scope`` is the caller's decision, not the user's attribute: it says which
    door these credentials were presented at. Every field below follows from it,
    including how long the session lives.
    """
    access = create_access_token(user.id, user.tenant_id, user.role, scope)
    refresh = create_refresh_token(user.id, user.tenant_id, user.role, scope)
    payload = decode_token(refresh, expected_kind="refresh")
    now = datetime.now(timezone.utc)

    db.add(
        UserSession(
            user_id=user.id,
            tenant_id=user.tenant_id,
            scope=scope,
            token_hash=hash_token(payload["jti"]),
            ip_address=_client_ip(request),
            device_label=(request.headers.get("user-agent") or "")[:120] or None,
            last_seen_at=now,
            expires_at=now + refresh_ttl(scope),
        )
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=int(access_ttl(scope).total_seconds()),
        scope=scope,
    )


@router.get("/plans", response_model=list[SubscriptionPlanOut])
def list_active_plans(db: Session = Depends(get_db)) -> list[SubscriptionPlan]:
    """The tiers open to a self-service choice — signup's picker and the
    customer's own Settings page both read this list.

    Deliberately public: the signup screen calls it before anyone has a
    token. Deliberately excludes retired plans (``is_active`` false) — unlike
    the staff console's ``/admin/plans``, which still needs to show a retired
    plan for the account already sitting on one. Nothing here is a reason to
    hide pricing from a signed-out visitor.
    """
    return db.scalars(
        select(SubscriptionPlan)
        .where(SubscriptionPlan.is_active.is_(True))
        .order_by(SubscriptionPlan.monthly_price_cents.is_(None), SubscriptionPlan.monthly_price_cents)
    ).all()


@router.post("/signup", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    email = payload.email.lower()
    if db.scalar(select(func.count(User.id)).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists")

    # The signup screen offers two things, not one: "start a free trial" —
    # the plan picker (GET /auth/plans) is optional there, and an unset
    # plan_id falls back to whichever plan is marked default, or to no plan
    # at all if this deployment has never set one up (tools/seed_plans.py) —
    # or "choose a plan now", which skips the trial outright. The second one
    # requires an actual plan: "no trial, no chosen plan" is not a request
    # this endpoint can act on.
    if payload.skip_trial and not payload.plan_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Choose a plan to start without a trial."
        )

    chosen_plan = None
    if payload.plan_id:
        chosen_plan = db.scalars(
            select(SubscriptionPlan).where(
                SubscriptionPlan.id == payload.plan_id,
                SubscriptionPlan.is_active.is_(True),
            )
        ).first()
        if chosen_plan is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such plan")
    if chosen_plan is None:
        chosen_plan = db.scalars(
            select(SubscriptionPlan).where(SubscriptionPlan.is_default.is_(True))
        ).first()

    # The platform-wide default interval can be faster than a plan's own
    # floor allows — Starter's 60-minute floor against a 15-minute platform
    # default, say. A signup has no interval field of its own to violate that
    # on purpose, so it starts at whichever is slower rather than opening on
    # a setting its own plan would reject if it were saved again unchanged.
    sync_interval = settings.default_sync_interval_minutes
    if chosen_plan and chosen_plan.min_sync_interval_minutes:
        sync_interval = max(sync_interval, chosen_plan.min_sync_interval_minutes)

    # "Choose a plan now" reads as having already paid for it — status opens
    # active, not trialing, and the clock counts a billing cycle rather than
    # a trial. There is no payment behind either number (see the module docs
    # in app.core.config) — both are just how long this account's access is
    # good for before something has to renew it.
    if payload.skip_trial:
        initial_status = TenantStatus.active.value
        period_days = settings.billing_period_days
    else:
        initial_status = TenantStatus.trialing.value
        period_days = settings.trial_days

    tenant = Tenant(
        name=payload.company_name,
        slug=_unique_slug(db, _slugify(payload.company_name)),
        status=initial_status,
        timezone=payload.timezone,
        sync_interval_minutes=sync_interval,
        plan_id=chosen_plan.id if chosen_plan else None,
        subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=period_days),
    )
    db.add(tenant)
    db.flush()

    user = User(
        tenant_id=tenant.id,
        email=email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=UserRole.owner.value,
    )
    db.add(user)
    db.flush()

    # Signup creates a customer workspace, so it can only ever be the customer
    # surface. Nothing here can make a staff session.
    tokens = _issue(db, user, request, SCOPE_TENANT)
    db.commit()
    return tokens


def _verify_credentials(db: Session, payload: LoginRequest) -> User:
    """Check an email and password, with the lockout accounting.

    Shared by both doors deliberately. What the two do once the password is
    known to be right differs; how they treat a wrong one must not, or the
    console door becomes the weaker of the two to guess against.
    """
    user = db.scalars(select(User).where(User.email == payload.email.lower())).first()
    now = datetime.now(timezone.utc)

    locked_until = ensure_aware(user.locked_until) if user is not None else None
    if locked_until and locked_until > now:
        wait = int((locked_until - now).total_seconds() / 60) + 1
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many failed attempts. Try again in {wait} minute(s).",
        )

    if user is None or not verify_password(payload.password, user.hashed_password):
        # The same message either way: a different response for a missing
        # account lets anyone enumerate which emails are registered.
        if user is not None:
            user.failed_login_count += 1
            if user.failed_login_count >= settings.max_failed_logins:
                user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
                user.failed_login_count = 0
                log.warning("Locked account %s after repeated failures", user.email)
            db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, BAD_CREDENTIALS)

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled")

    return user


def _record_login(user: User) -> None:
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)


@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    """The customer door. Issues a tenant-scoped session, whoever signs in.

    Being platform staff is not a reason to refuse someone their own workspace,
    so a dual-role account is admitted here — it simply arrives without the
    console, because this token is tenant-scoped no matter who presents it.
    """
    user = _verify_credentials(db, payload)

    if user.tenant_id is None:
        # Staff with no workspace of their own. A token minted here would
        # authenticate and then fail on every screen — the tenant routes have
        # nothing of theirs to show, and the console refuses a customer
        # session — which reads as a broken account rather than a wrong door.
        # Cheaper to say so now, with the address of the door that works.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This is a platform staff account with no customer workspace of its "
            "own. Sign in at the staff console instead.",
        )

    _record_login(user)
    tokens = _issue(db, user, request, SCOPE_TENANT)
    db.commit()
    return tokens


@router.post("/staff/login", response_model=TokenPair)
def staff_login(
    payload: LoginRequest, request: Request, db: Session = Depends(get_db)
) -> TokenPair:
    """The console door. Issues a short-lived staff-scoped session.

    Separate from ``/login`` so that a console credential is never entered into
    the customer-facing form and vice versa: the two surfaces no longer share a
    sign-in page to phish, and a staff session gets its own much shorter life.
    """
    user = _verify_credentials(db, payload)

    if not user.is_platform_admin:
        # Right password, wrong door — answered exactly as a wrong password is.
        # "You are not staff" would turn this endpoint into a lookup for which
        # accounts hold the flag, and those are precisely the credentials worth
        # phishing. The refusal is logged instead, where we can see it and an
        # attacker cannot.
        log.warning("Console sign-in refused: %s is not platform staff", user.email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, BAD_CREDENTIALS)

    _record_login(user)
    tokens = _issue(db, user, request, SCOPE_STAFF)
    db.commit()
    return tokens


@router.post("/refresh", response_model=TokenPair)
def refresh(refresh_token: str, db: Session = Depends(get_db)) -> TokenPair:
    try:
        payload = decode_token(refresh_token, expected_kind="refresh")
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    session = db.scalars(
        select(UserSession).where(UserSession.token_hash == hash_token(payload["jti"]))
    ).first()
    if session is None or not session.is_active or is_past(session.expires_at):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired or revoked")

    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or disabled")

    # The scope comes from the signed token, not from the user: a refresh
    # returns to the same surface the session was opened on, so a customer
    # session can never be renewed into a console one.
    scope = scope_of(payload)

    if scope == SCOPE_STAFF and not user.is_platform_admin:
        # The flag was taken away while the session was open. Every console
        # route would refuse this token anyway, but ending the session here
        # means revoking staff access actually ends it rather than leaving a
        # console session alive until its refresh token runs out.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "This account no longer has console access"
        )

    session.last_seen_at = datetime.now(timezone.utc)
    db.commit()
    return TokenPair(
        access_token=create_access_token(user.id, user.tenant_id, user.role, scope),
        refresh_token=refresh_token,
        expires_in=int(access_ttl(scope).total_seconds()),
        scope=scope,
    )


@router.post("/logout", response_model=MessageOut)
def logout(refresh_token: str = "", db: Session = Depends(get_db)) -> MessageOut:
    if refresh_token:
        try:
            payload = decode_token(refresh_token, expected_kind="refresh")
        except ValueError:
            return MessageOut(message="Signed out")
        db.execute(
            update(UserSession)
            .where(
                UserSession.token_hash == hash_token(payload["jti"]),
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(timezone.utc), revoked_reason="logout")
        )
        db.commit()
    return MessageOut(message="Signed out")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    """Who am I — regardless of whether I have a customer workspace.

    Deliberately not tenant-scoped: platform staff have no tenant, and this is
    the first call the dashboard makes after sign-in.
    """
    return user
