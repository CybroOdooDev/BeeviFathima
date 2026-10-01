"""Accounts made from the marketing website.

Two ways in, one ending:

* **Free trial** — the registration form creates the account straight away,
  ``trialing``, no card.
* **Buy a plan** — the form creates only a PendingSignup and sends the
  visitor to Stripe Checkout. The account is made when Stripe confirms the
  payment (``complete_paid_signup``, called from the webhook), ``active`` on
  the plan they paid for.

Either way the owner has **no password yet**: the account cannot sign in
until the address is confirmed. Confirming it (``POST /auth/verify-email``)
generates a password and emails it with the login address
(``send_credentials``); the first sign-in with it must set a new one.
"""

from __future__ import annotations

import logging
import re
import secrets
import string
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password, new_token
from app.models import (
    PendingSignup,
    SubscriptionPlan,
    Tenant,
    TenantStatus,
    User,
    UserRole,
)
from app.services.email_verification import issue_verification_token, send_verification_email
from app.services.mailer import send_email

log = logging.getLogger(__name__)


# --- helpers ----------------------------------------------------------------
def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "tenant"


def _unique_slug(db: Session, base: str) -> str:
    slug, n = base, 1
    while db.scalar(select(func.count(Tenant.id)).where(Tenant.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def generate_password(length: int = 14) -> str:
    """Readable and strong: letters and digits without look-alikes (0/O, 1/l/I)."""
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw


def login_url() -> str:
    return f"{settings.public_base_url.rstrip('/')}/app/#/login"


def email_taken(db: Session, email: str) -> bool:
    return bool(db.scalar(select(func.count(User.id)).where(User.email == email.lower())))


# --- rate limit -------------------------------------------------------------
# In-process and per IP: enough to stop one browser (or script) from using the
# registration form to mail-bomb an address. Behind several app processes
# each keeps its own count, which only makes it looser, never wrong.
_hits: dict[str, deque[float]] = defaultdict(deque)
_hits_lock = threading.Lock()


def allow(ip: str | None) -> bool:
    limit = settings.registration_rate_per_hour
    if limit <= 0:
        return True
    now = time.monotonic()
    with _hits_lock:
        q = _hits[ip or "?"]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


# --- accounts ---------------------------------------------------------------
def create_account(
    db: Session,
    *,
    company_name: str,
    email: str,
    full_name: str | None,
    timezone_name: str,
    plan: SubscriptionPlan | None,
    paid: bool,
) -> tuple[Tenant, User]:
    """A tenant and its owner, the owner with no usable password yet."""
    interval = settings.default_sync_interval_minutes
    if plan and plan.min_sync_interval_minutes:
        interval = max(interval, plan.min_sync_interval_minutes)
    days = settings.billing_period_days if paid else settings.trial_days
    tenant = Tenant(
        name=company_name.strip(),
        slug=_unique_slug(db, _slugify(company_name)),
        status=(TenantStatus.active if paid else TenantStatus.trialing).value,
        timezone=timezone_name,
        sync_interval_minutes=interval,
        plan_id=plan.id if plan else None,
        subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=days),
    )
    db.add(tenant)
    db.flush()
    user = User(
        tenant_id=tenant.id,
        email=email.lower(),
        full_name=full_name,
        # A random secret nobody is ever told: the account cannot be signed
        # into until send_credentials replaces it.
        hashed_password=hash_password(new_token()),
        role=UserRole.owner.value,
        credentials_pending=True,
    )
    db.add(user)
    db.flush()
    return tenant, user


def start_verification(user: User) -> None:
    """Issue a verification token and mail it. Best effort — see mailer."""
    raw = issue_verification_token(user)
    try:
        send_verification_email(user, raw)
    except Exception as exc:  # noqa: BLE001 — a mail outage must not undo the account
        log.warning("Could not send verification email to %s: %s", user.email, exc)


def send_credentials(db: Session, user: User) -> bool:
    """Generate a password, store it, and email the login details.

    Commits before mailing so the password that is sent is the one stored.
    Returns whether the email went out; if not, the visitor can ask again
    (POST /public/resend) and a fresh password is generated.
    """
    password = generate_password()
    user.hashed_password = hash_password(password)
    user.credentials_pending = False
    user.must_change_password = True
    db.commit()
    tenant = db.get(Tenant, user.tenant_id) if user.tenant_id else None
    try:
        send_email(
            db=db,
            to=user.email,
            subject="Your BioBridge account is ready",
            body=(
                f"Welcome to BioBridge{f', {tenant.name}' if tenant else ''}!\n\n"
                "Your email is confirmed and your account is ready. Sign in with:\n\n"
                f"  Sign in at: {login_url()}\n"
                f"  Email:      {user.email}\n"
                f"  Password:   {password}\n\n"
                "You'll be asked to choose your own password the first time you sign "
                "in — this one works only until then.\n\n"
                "Next: connect your Odoo database and your biometric device from "
                "Settings. The setup guide walks through both.\n"
            ),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not send login details to %s: %s", user.email, exc)
        return False


def resend(db: Session, email: str) -> None:
    """The public "didn't get it?" button. Silent about what exists.

    Unconfirmed address → a fresh verification link. Confirmed but the
    emailed password never used → a fresh password. Anything else: nothing.
    """
    user = db.scalars(select(User).where(User.email == email.lower())).first()
    if user is None or not user.is_active:
        return
    if user.email_verified_at is None:
        start_verification(user)
        db.commit()
    elif user.must_change_password and user.last_login_at is None:
        send_credentials(db, user)


# --- paid signups -----------------------------------------------------------
def complete_paid_signup(db: Session, pending_id: str, session: dict[str, Any]) -> Tenant | None:
    """Stripe says the Checkout for ``pending_id`` is paid: make the account.

    Idempotent — a replayed event finds ``completed_at`` set and returns the
    tenant it already made. The caller links the Stripe subscription.
    """
    pending = db.get(PendingSignup, pending_id)
    if pending is None:
        return None
    if pending.completed_at is not None:
        return db.get(Tenant, pending.tenant_id) if pending.tenant_id else None

    existing = db.scalars(select(User).where(User.email == pending.email)).first()
    if existing is not None and existing.tenant_id:
        # Registered twice (a trial first, say) and paid on the second: the
        # payment belongs to the account that address already owns.
        log.warning("Paid signup for %s attached to its existing account", pending.email)
        tenant = db.get(Tenant, existing.tenant_id)
    else:
        plan = db.get(SubscriptionPlan, pending.plan_id)
        tenant, user = create_account(
            db, company_name=pending.company_name, email=pending.email,
            full_name=pending.full_name, timezone_name=pending.timezone,
            plan=plan, paid=True,
        )
        # Issued now, mailed only once the webhook's transaction has
        # committed (see after_commit_mail): if anything after this point
        # fails, Stripe retries the event and the customer must not already
        # hold a link to a token that was rolled back.
        raw = issue_verification_token(user)
        db.info.setdefault("after_commit_mail", []).append(
            lambda db=db, user_id=user.id, raw=raw: _send_verification_later(db, user_id, raw))

    pending.completed_at = datetime.now(timezone.utc)
    pending.tenant_id = tenant.id if tenant else None
    return tenant


def _send_verification_later(db: Session, user_id: str, raw: str) -> None:
    """Runs after the webhook committed, on the same (still open) session."""
    user = db.get(User, user_id)
    if user is None:
        return
    try:
        send_verification_email(user, raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not send verification email to %s: %s", user.email, exc)


def after_commit_mail(db: Session) -> None:
    """Send what was queued on ``db`` for after its commit (paid signups)."""
    for send in db.info.pop("after_commit_mail", []):
        send()
