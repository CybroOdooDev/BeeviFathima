"""Signup, login, refresh, logout."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_principal
from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.db.session import get_db
from app.models import Tenant, TenantStatus, User, UserRole, UserSession
from app.schemas import LoginRequest, MessageOut, SignupRequest, TokenPair, UserOut
from app.services.timeutils import ensure_aware, is_past

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


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


def _issue(db: Session, user: User, request: Request) -> TokenPair:
    """Mint a token pair and record the session so it can be revoked."""
    access = create_access_token(user.id, user.tenant_id, user.role)
    refresh = create_refresh_token(user.id, user.tenant_id, user.role)
    payload = decode_token(refresh, expected_kind="refresh")
    now = datetime.now(timezone.utc)

    db.add(
        UserSession(
            user_id=user.id,
            tenant_id=user.tenant_id,
            token_hash=hash_token(payload["jti"]),
            ip_address=_client_ip(request),
            device_label=(request.headers.get("user-agent") or "")[:120] or None,
            last_seen_at=now,
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.post("/signup", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    email = payload.email.lower()
    if db.scalar(select(func.count(User.id)).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists")

    tenant = Tenant(
        name=payload.company_name,
        slug=_unique_slug(db, _slugify(payload.company_name)),
        status=TenantStatus.trialing.value,
        timezone=payload.timezone,
        sync_interval_minutes=settings.default_sync_interval_minutes,
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

    tokens = _issue(db, user, request)
    db.commit()
    return tokens


@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
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
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled")

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    tokens = _issue(db, user, request)
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

    session.last_seen_at = datetime.now(timezone.utc)
    db.commit()
    return TokenPair(
        access_token=create_access_token(user.id, user.tenant_id, user.role),
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_minutes * 60,
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
def me(principal: Principal = Depends(get_principal)) -> User:
    return principal.user
