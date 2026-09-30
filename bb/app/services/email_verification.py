"""Proving the person who signed up controls the inbox they typed.

A random opaque token, stored only as a hash — the same shape
``UserSession.token_hash`` already uses for refresh tokens — so a stolen
database yields no usable verification link, same as it yields no usable
session. One live token per user: issuing a fresh one (via resend, or a
second signup attempt after a mistyped-then-fixed address) silently
invalidates whichever link was sent before it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_token, new_token
from app.models import User
from app.services.mailer import send_email
from app.services.timeutils import ensure_aware


def issue_verification_token(user: User) -> str:
    """Mint a fresh token for ``user`` and store its hash on the row.

    Returns the raw token — the only moment it exists outside the link it
    goes into. Caller is responsible for committing.
    """
    raw = new_token()
    user.email_verify_token_hash = hash_token(raw)
    user.email_verify_token_expires_at = datetime.now(timezone.utc) + timedelta(
        hours=settings.email_verification_ttl_hours
    )
    return raw


def verification_link(raw_token: str) -> str:
    """Where the emailed link lands: the website's "confirmed" page when
    SITE_URL is set, otherwise the app's own verify screen. Both post the
    token to POST /auth/verify-email."""
    if settings.site_url:
        return f"{settings.site_url.rstrip('/')}/verified.html?token={raw_token}"
    return f"{settings.public_base_url.rstrip('/')}/app/#/verify-email?token={raw_token}"


def send_verification_email(user: User, raw_token: str) -> None:
    link = verification_link(raw_token)
    follow_up = (
        "Once it's confirmed we'll email your login details.\n\n"
        if user.credentials_pending else ""
    )
    send_email(
        to=user.email,
        subject="Confirm your email for BioBridge",
        body=(
            "Confirm this address to finish setting up your BioBridge "
            f"account:\n\n{link}\n\n{follow_up}"
            f"This link expires in {settings.email_verification_ttl_hours} hours. "
            "If you didn't request this, you can ignore it."
        ),
    )


def verify_token(db: Session, raw_token: str) -> User | None:
    """Consume ``raw_token``.

    Returns the now-verified user, or ``None`` if the token is unknown,
    already used, or expired. Caller is responsible for committing.
    """
    token_hash = hash_token(raw_token)
    user = db.scalars(
        select(User).where(User.email_verify_token_hash == token_hash)
    ).first()
    if user is None:
        return None

    expires_at = ensure_aware(user.email_verify_token_expires_at)
    if expires_at is None or expires_at < datetime.now(timezone.utc):
        return None

    user.email_verified_at = datetime.now(timezone.utc)
    user.email_verify_token_hash = None
    user.email_verify_token_expires_at = None
    return user
