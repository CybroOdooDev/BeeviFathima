"""Password hashing and JWT issuance."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

#: The two doors a session can be minted at.
#:
#: A token's scope is decided by where the credentials were presented, not by
#: who the account is. That distinction is the whole point: the User model
#: deliberately allows someone to be both a customer and platform staff, and
#: before scopes existed such a person carried one token that reached both
#: surfaces at once. Now they choose a hat at sign-in — a customer session at
#: the product door, a console session at the staff door — and neither token
#: reaches the other side. A leaked customer session is therefore useless for
#: cross-tenant work even when its owner happens to be staff.
SCOPE_TENANT = "tenant"
SCOPE_STAFF = "staff"
SCOPES = (SCOPE_TENANT, SCOPE_STAFF)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except ValueError:
        return False


def hash_token(raw: str) -> str:
    """Tokens are persisted as a sha256 digest, never in the clear.

    A stolen database therefore yields no usable session or invitation link.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def scope_of(payload: dict[str, Any]) -> str:
    """Which surface a decoded token was minted for.

    A token issued before scopes existed carries no ``scp`` claim, and an
    unrecognised value is treated the same way. Both fall back to tenant scope
    rather than staff: the default has to fail closed, or introducing this
    would have left every already-issued session holding console access until
    it happened to expire.
    """
    scope = payload.get("scp")
    return scope if scope in SCOPES else SCOPE_TENANT


def access_ttl(scope: str) -> timedelta:
    """Exported because the login response tells the client when to refresh."""
    return timedelta(minutes=(
        settings.staff_access_token_ttl_minutes if scope == SCOPE_STAFF
        else settings.access_token_ttl_minutes
    ))


def refresh_ttl(scope: str) -> timedelta:
    """Exported because the session row records the same expiry the token has."""
    return timedelta(days=(
        settings.staff_refresh_token_ttl_days if scope == SCOPE_STAFF
        else settings.refresh_token_ttl_days
    ))


def _create_token(
    subject: str, tenant_id: str | None, role: str, ttl: timedelta, kind: str, scope: str
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "tid": tenant_id,
        "role": role,
        "scp": scope,
        "typ": kind,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(
    user_id: str, tenant_id: str | None, role: str, scope: str = SCOPE_TENANT
) -> str:
    """``tenant_id`` is None for platform staff, who belong to no customer.

    ``scope`` defaults to the customer surface deliberately: a caller that
    forgets to say which door it is issuing for gets the unprivileged one.
    """
    return _create_token(user_id, tenant_id, role, access_ttl(scope), "access", scope)


def create_refresh_token(
    user_id: str, tenant_id: str | None, role: str, scope: str = SCOPE_TENANT
) -> str:
    return _create_token(user_id, tenant_id, role, refresh_ttl(scope), "refresh", scope)


def decode_token(token: str, expected_kind: str = "access") -> dict[str, Any]:
    """Decode and validate a JWT. Raises ValueError when it is not usable."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("Invalid or expired token") from exc
    if payload.get("typ") != expected_kind:
        raise ValueError("Wrong token type")
    return payload
