"""Forgot password: an emailed, single-use, short-lived link.

Same shape as email verification — an opaque token stored only as a hash, one
live token per user. Requesting a link never reveals whether the address has an
account (the endpoint answers identically), and is rate-limited per account so
it can't be used to flood someone's inbox.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password, hash_token, new_token
from app.models import User, UserSession
from app.services.mailer import send_email
from app.services.timeutils import ensure_aware

log = logging.getLogger(__name__)


def reset_link(raw_token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/app/#/reset-password?token={raw_token}"


def request_reset(db: Session, email: str) -> tuple[User, str] | None:
    """Mint a link for ``email`` if it belongs to an active, confirmed account
    that hasn't asked in the last minute. Returns (user, raw_token) or None.
    Caller commits, then sends."""
    user = db.scalars(select(User).where(User.email == email.strip().lower())).first()
    # The caller always gets the same reply, so the reason a link was NOT sent
    # is only ever visible here, in the server log.
    if user is None:
        log.info("Password reset requested for %r: no such account, nothing sent", email)
        return None
    if not user.is_active:
        log.info("Password reset for %s: account is disabled, nothing sent", user.email)
        return None
    if user.credentials_pending:
        log.info("Password reset for %s: email not confirmed yet (login details still pending), nothing sent", user.email)
        return None
    now = datetime.now(timezone.utc)
    sent = ensure_aware(user.password_reset_sent_at)
    if sent and (now - sent).total_seconds() < settings.password_reset_cooldown_seconds:
        log.info("Password reset for %s: asked again inside the %ss cooldown, nothing sent",
                 user.email, settings.password_reset_cooldown_seconds)
        return None
    raw = new_token()
    user.password_reset_token_hash = hash_token(raw)
    user.password_reset_expires_at = now + timedelta(minutes=settings.password_reset_ttl_minutes)
    user.password_reset_sent_at = now
    return user, raw


def send_reset_email(db: Session, user: User, raw_token: str) -> None:
    log.info("Password reset link issued for %s, sending", user.email)
    send_email(
        db=db, to=user.email,
        subject="Reset your BioBridge password",
        body=(
            "Someone asked to reset the password for this BioBridge account. "
            f"To choose a new one:\n\n{reset_link(raw_token)}\n\n"
            f"The link works once and expires in {settings.password_reset_ttl_minutes} minutes. "
            "If it wasn't you, ignore this email — your password is unchanged."
        ),
    )


def send_changed_email(db: Session, user: User) -> None:
    send_email(
        db=db, to=user.email,
        subject="Your BioBridge password was changed",
        body=("The password for this BioBridge account was just reset, and every signed-in "
              "session was ended. If this wasn't you, contact support right away."),
    )


def consume(db: Session, raw_token: str, new_password: str) -> User | None:
    """Set the new password if the token is live. Also clears any lockout,
    ends every session, and counts as proof of the inbox. Caller commits."""
    user = db.scalars(select(User).where(User.password_reset_token_hash == hash_token(raw_token))).first()
    if user is None:
        return None
    expires = ensure_aware(user.password_reset_expires_at)
    now = datetime.now(timezone.utc)
    if expires is None or expires < now or not user.is_active:
        return None
    user.hashed_password = hash_password(new_password)
    user.must_change_password = False
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    user.failed_login_count = 0
    user.locked_until = None
    if user.email_verified_at is None:
        user.email_verified_at = now
    db.execute(update(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
               .values(revoked_at=now, revoked_reason="password_reset"))
    return user
