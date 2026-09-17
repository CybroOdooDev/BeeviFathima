"""Shared dependencies: authentication, tenant scoping, role checks.

``get_principal`` is the single choke point for tenant isolation. There is no
row-level security in the database, so every query downstream filters on
``principal.tenant.id`` — an unscoped query is a cross-tenant leak, which is why
the test suite asserts it directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.db.session import get_db
from app.models import AuditLog, Tenant, User, UserRole

bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    user: User
    tenant: Tenant

    @property
    def role(self) -> str:
        return self.user.role

    @property
    def is_writer(self) -> bool:
        return self.role in (UserRole.owner.value, UserRole.admin.value)


def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Principal:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    try:
        payload = decode_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or disabled")

    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Tenant not found")

    # Defence in depth: the token's tenant claim must agree with the user row,
    # so a token minted against a since-moved user cannot reach the new tenant.
    if payload.get("tid") != tenant.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token/tenant mismatch")

    return Principal(user=user, tenant=tenant)


def require_writer(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_writer:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This action requires admin access")
    return principal


def require_owner(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.role != UserRole.owner.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the account owner can do this")
    return principal


def audit(
    db: Session,
    principal: Principal,
    action: str,
    target: str | None = None,
    detail: str | None = None,
    request: Request | None = None,
) -> None:
    db.add(
        AuditLog(
            tenant_id=principal.tenant.id,
            actor_user_id=principal.user.id,
            action=action,
            target=target,
            detail=detail,
            ip_address=request.client.host if request and request.client else None,
        )
    )
