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


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    """Authenticate, and nothing else.

    Separated from ``get_principal`` because some things are about the *person*
    rather than their workspace — ``/auth/me`` most of all. Requiring a tenant
    there locked platform staff out of reading their own profile, which is the
    first call the dashboard makes, so they could not sign in at all.
    """
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    try:
        payload = decode_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or disabled")
    return user


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

    if user.tenant_id is None:
        # Platform staff have no customer workspace, which is the point — they
        # are not a tenant. Say so rather than 401, which would read as a broken
        # session and send someone re-entering a correct password.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This is a platform staff account. It has no customer workspace of "
            "its own — use the platform console.",
        )

    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Tenant not found")

    # Defence in depth: the token's tenant claim must agree with the user row,
    # so a token minted against a since-moved user cannot reach the new tenant.
    if payload.get("tid") != tenant.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token/tenant mismatch")

    return Principal(user=user, tenant=tenant)


def get_platform_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    """The one door that is not tenant-scoped.

    Everything else in this API answers "within your account". Support work does
    not fit that shape — the question is "which customer is not syncing" — so
    this returns a bare user and the routes behind it are responsible for
    naming the tenant they touch.

    It is a separate dependency rather than a flag checked inside
    ``get_principal`` precisely so the two cannot be confused: any route that
    crosses tenants has to say so in its signature, and the test suite can
    enumerate them.
    """
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        payload = decode_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or disabled")
    if not user.is_platform_admin:
        # 403, not 404: unlike a tenant-scoped object, the existence of the
        # platform console is not a secret worth keeping, and a support user
        # hitting this needs to know their account lacks the flag rather than
        # that the URL is wrong.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform staff access required")
    return user


def audit_platform(
    db: Session,
    actor: User,
    tenant_id: str,
    action: str,
    target: str | None = None,
    detail: str | None = None,
    request: Request | None = None,
) -> None:
    """Record a staff action in the *customer's* audit trail.

    Filed against the tenant that was changed, not the staff user's own account,
    so the customer can see that support altered their settings. Support work
    that is invisible to the customer is how "we never touched it" arguments
    start.
    """
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_user_id=actor.id,
            action=action,
            target=target,
            detail=detail,
            ip_address=request.client.host if request and request.client else None,
        )
    )


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
