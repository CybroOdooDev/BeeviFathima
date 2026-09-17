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


def _create_token(subject: str, tenant_id: str, role: str, ttl: timedelta, kind: str) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "tid": tenant_id,
        "role": role,
        "typ": kind,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: str, tenant_id: str, role: str) -> str:
    return _create_token(
        user_id, tenant_id, role,
        timedelta(minutes=settings.access_token_ttl_minutes), "access",
    )


def create_refresh_token(user_id: str, tenant_id: str, role: str) -> str:
    return _create_token(
        user_id, tenant_id, role,
        timedelta(days=settings.refresh_token_ttl_days), "refresh",
    )


def decode_token(token: str, expected_kind: str = "access") -> dict[str, Any]:
    """Decode and validate a JWT. Raises ValueError when it is not usable."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("Invalid or expired token") from exc
    if payload.get("typ") != expected_kind:
        raise ValueError("Wrong token type")
    return payload
