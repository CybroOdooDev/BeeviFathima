"""The platform console: scheduling, across every customer.

This is the only router in the API that is not tenant-scoped, and the scope is
kept deliberately narrow. Staff can see each customer's sync cadence and change
it. They cannot read a customer's punches, attendance, employees or credentials
through here — support almost never needs that, and a console that offers it is
a console that leaks a customer's attendance data the first time someone is
careless with an account.

So the hole in tenant isolation is exactly one table's scheduling columns, every
change is written into the customer's own audit trail, and reaching any of it
requires a flag no HTTP route can set.
"""

from __future__ import annotations

import logging
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import audit_platform, get_platform_admin
from app.core.security import hash_password
from app.db.session import get_db
from app.models import (
    DeviceSource,
    EmployeeMapping,
    MappingStatus,
    OdooConnection,
    PunchRecord,
    PunchState,
    SyncRun,
    Tenant,
    TenantStatus,
    User,
    UserRole,
)
from app.schemas import (
    ErrorGroup,
    MessageOut,
    SyncRunOut,
    TenantAdminOut,
    TenantConfigUpdate,
    TenantCreateIn,
    TenantCreateOut,
    TenantDiagnosticsOut,
    TenantScheduleUpdate,
)
from app.services.scheduling import (
    SYNCABLE,
    effective_interval,
    next_run_at,
    scheduler_health,
)
from app.services.sync_engine import SyncEngine

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["platform"])


# Same rules as self-signup, so a staff-created account is indistinguishable
# from one the customer made themselves.
def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "tenant"


def _unique_slug(db: Session, base: str) -> str:
    slug, n = base, 1
    while db.scalar(select(func.count(Tenant.id)).where(Tenant.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def _to_out(db: Session, tenant: Tenant) -> TenantAdminOut:
    last = db.scalars(
        select(SyncRun)
        .where(SyncRun.tenant_id == tenant.id)
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    ).first()
    interval = effective_interval(tenant)
    return TenantAdminOut(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        timezone=tenant.timezone,
        sync_enabled=tenant.sync_enabled,
        sync_interval_minutes=tenant.sync_interval_minutes,
        effective_interval_minutes=interval,
        interval_widened=interval != max(1, tenant.sync_interval_minutes),
        consecutive_failures=tenant.consecutive_failures,
        last_run_at=last.started_at if last else None,
        last_run_status=last.status if last else None,
        next_run_at=next_run_at(db, tenant),
        pairing_mode=tenant.pairing_mode,
        day_boundary_hour=tenant.day_boundary_hour,
        min_punch_interval_seconds=tenant.min_punch_interval_seconds,
        max_shift_hours=tenant.max_shift_hours,
        orphan_out_policy=tenant.orphan_out_policy,
        work_start_time=tenant.work_start_time,
        late_grace_minutes=tenant.late_grace_minutes,
        users=db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant.id)
        ) or 0,
        odoo_connected=bool(
            db.scalar(
                select(func.count(OdooConnection.id)).where(
                    OdooConnection.tenant_id == tenant.id,
                    OdooConnection.is_active.is_(True),
                )
            )
        ),
        source_connected=bool(
            db.scalar(
                select(func.count(DeviceSource.id)).where(
                    DeviceSource.tenant_id == tenant.id,
                    DeviceSource.is_active.is_(True),
                )
            )
        ),
    )


def _redact(message: str, names: set[str]) -> str:
    """Strip employee identities out of an Odoo error before staff read it.

    Odoo writes the person's name into the message itself — "Cannot create new
    attendance record for Sara Tanaka, the employee was already checked in on
    …". So "error text without employee names" is not achieved by withholding
    other columns; the text has to be scrubbed, or the console leaks exactly
    what it claims not to show.

    Longest names first, so "Sara Tanaka" is replaced whole rather than leaving
    "Tanaka" behind after matching "Sara".
    """
    for name in sorted(names, key=len, reverse=True):
        if name and len(name) > 2:
            message = message.replace(name, "<employee>")
    return message


@router.get("/scheduler", tags=["platform"])
def platform_scheduler(
    _: User = Depends(get_platform_admin), db: Session = Depends(get_db)
) -> dict:
    """Is the clock running at all?

    Worth reading before concluding that one customer's interval is wrong: if
    the scheduler is down, *every* customer has stopped, and changing one
    tenant's number will not help.
    """
    health = scheduler_health(db)
    return {
        **health,
        "last_tick_at": health["last_tick_at"].isoformat() + "Z"
        if health["last_tick_at"]
        else None,
        "tenants_total": db.scalar(select(func.count(Tenant.id))) or 0,
        "tenants_scheduled": db.scalar(
            select(func.count(Tenant.id)).where(
                Tenant.sync_enabled.is_(True), Tenant.status.in_(SYNCABLE)
            )
        ) or 0,
    }


@router.get("/tenants", response_model=list[TenantAdminOut])
def list_tenants(
    q: str | None = Query(default=None, description="Match on name or slug."),
    limit: int = Query(default=200, le=500),
    _: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> list[TenantAdminOut]:
    stmt = select(Tenant).order_by(Tenant.name)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Tenant.name).like(like) | func.lower(Tenant.slug).like(like)
        )
    return [_to_out(db, t) for t in db.scalars(stmt.limit(limit)).all()]


@router.get("/tenants/{tenant_id}", response_model=TenantAdminOut)
def get_tenant(
    tenant_id: str,
    _: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> TenantAdminOut:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such tenant")
    return _to_out(db, tenant)


@router.patch("/tenants/{tenant_id}/schedule", response_model=TenantAdminOut)
def update_schedule(
    tenant_id: str,
    payload: TenantScheduleUpdate,
    request: Request,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> TenantAdminOut:
    """Change one customer's cadence.

    The new interval applies from the **last run**, not from now: due-ness is
    measured that way everywhere, so shortening the interval on a customer who
    synced ten minutes ago can make them due immediately. That is usually what
    the person changing it wants, and the response carries the recomputed
    ``next_run_at`` so it is visible rather than surprising.
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such tenant")

    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to change")

    before = {key: getattr(tenant, key) for key in data}
    for key, value in data.items():
        setattr(tenant, key, value)

    changes = ", ".join(f"{k}: {before[k]} -> {v}" for k, v in data.items())
    audit_platform(
        db,
        actor,
        tenant.id,
        "platform.schedule.update",
        target=tenant.slug,
        detail=f"{changes} (by {actor.email})",
        request=request,
    )
    db.commit()
    db.refresh(tenant)

    log.info(
        "Platform user %s changed %s scheduling — %s", actor.email, tenant.slug, changes
    )
    return _to_out(db, tenant)


@router.patch("/tenants/{tenant_id}/config", response_model=TenantAdminOut)
def update_config(
    tenant_id: str,
    payload: TenantConfigUpdate,
    request: Request,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> TenantAdminOut:
    """Account lifecycle and pairing rules.

    Separate from the schedule endpoint because the risk is different. An
    interval is a cadence; ``pairing_mode`` and ``max_shift_hours`` decide how
    punches become shifts, so changing them silently changes what the customer
    sees as their attendance. Nothing here rewrites history — existing records
    stand, and the new rules apply from the next run.

    Suspending or cancelling stops that account syncing immediately, because
    only trialing and active are in SYNCABLE.
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such tenant")

    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to change")

    before = {key: getattr(tenant, key) for key in data}
    for key, value in data.items():
        setattr(tenant, key, value)

    changes = ", ".join(f"{k}: {before[k]} -> {v}" for k, v in data.items())
    audit_platform(
        db,
        actor,
        tenant.id,
        "platform.config.update",
        target=tenant.slug,
        detail=f"{changes} (by {actor.email})",
        request=request,
    )
    db.commit()
    db.refresh(tenant)
    log.info("Platform user %s changed %s config — %s", actor.email, tenant.slug, changes)
    return _to_out(db, tenant)


@router.post(
    "/tenants", response_model=TenantCreateOut, status_code=status.HTTP_201_CREATED
)
def create_tenant(
    payload: TenantCreateIn,
    request: Request,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> TenantCreateOut:
    """Onboard a customer, rather than waiting for them to sign up.

    The owner needs a way in and this system sends no email, so a password is
    set here and returned **once**. It is stored only as a hash, so it cannot be
    read back later — a lost one is replaced, never recovered. Generated by
    default: a staff member inventing passwords under time pressure invents weak
    ones, and reuses them.
    """
    email = payload.owner_email.lower()
    if db.scalar(select(func.count(User.id)).where(User.email == email)):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A user with that email already exists"
        )

    password = payload.owner_password or secrets.token_urlsafe(16)

    tenant = Tenant(
        name=payload.company_name,
        slug=_unique_slug(db, _slugify(payload.company_name)),
        status=TenantStatus.trialing.value,
        timezone=payload.timezone,
        sync_interval_minutes=payload.sync_interval_minutes,
    )
    db.add(tenant)
    db.flush()

    db.add(
        User(
            tenant_id=tenant.id,
            email=email,
            full_name=payload.owner_name,
            hashed_password=hash_password(password),
            role=UserRole.owner.value,
        )
    )
    audit_platform(
        db,
        actor,
        tenant.id,
        "platform.tenant.create",
        target=tenant.slug,
        detail=f"created by {actor.email} with owner {email}",
        request=request,
    )
    db.commit()
    db.refresh(tenant)

    log.info("Platform user %s created tenant %s", actor.email, tenant.slug)
    return TenantCreateOut(
        tenant=_to_out(db, tenant),
        owner_email=email,
        owner_password=password,
        note=(
            "This password is shown once and stored only as a hash. Give it to "
            "the owner over a channel you trust and have them change it. If it "
            "is lost, replace it with tools/show_users.py --set-password."
        ),
    )


@router.post("/tenants/{tenant_id}/sync", response_model=SyncRunOut)
def run_sync_now(
    tenant_id: str,
    request: Request,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> SyncRun:
    """Run one cycle for a customer, now.

    Inline rather than queued, so the result comes back in the response instead
    of "queued" and a second question. This reaches into the customer's BioTime
    and their Odoo under a staff member's hand, so it is audited as
    ``platform.sync.manual`` in *their* trail, and it is tagged ``manual`` in
    the run history rather than pretending to be the schedule.
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such tenant")
    if tenant.status not in SYNCABLE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{tenant.name} is {tenant.status}, so it does not sync. Set it to "
            f"active first.",
        )

    audit_platform(
        db,
        actor,
        tenant.id,
        "platform.sync.manual",
        target=tenant.slug,
        detail=f"triggered by {actor.email}",
        request=request,
    )
    db.commit()

    log.info("Platform user %s triggered a sync for %s", actor.email, tenant.slug)
    return SyncEngine(db, tenant, "manual").run_cycle()


@router.get("/tenants/{tenant_id}/diagnostics", response_model=TenantDiagnosticsOut)
def tenant_diagnostics(
    tenant_id: str,
    _: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> TenantDiagnosticsOut:
    """Why a customer is stuck — counts and error text, nothing more.

    No punch times, badges or employee names. The last two need actual work:
    Odoo writes the person's name into its own error message, so the text is
    scrubbed against this tenant's known employee names before it is returned.
    Withholding the other columns while handing over "Cannot create new
    attendance record for Sara Tanaka…" would be a console that leaks precisely
    what it claims not to show.
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such tenant")

    def count(*conditions) -> int:
        return db.scalar(
            select(func.count(PunchRecord.id)).where(
                PunchRecord.tenant_id == tenant.id, *conditions
            )
        ) or 0

    names: set[str] = set()
    for mapping in db.scalars(
        select(EmployeeMapping).where(EmployeeMapping.tenant_id == tenant.id)
    ).all():
        names.update(
            n for n in (mapping.odoo_employee_name, mapping.source_name, mapping.emp_code) if n
        )

    grouped: dict[str, int] = {}
    for message, total in db.execute(
        select(PunchRecord.error_message, func.count(PunchRecord.id))
        .where(
            PunchRecord.tenant_id == tenant.id,
            PunchRecord.process_state == PunchState.error.value,
            PunchRecord.error_message.is_not(None),
        )
        .group_by(PunchRecord.error_message)
    ).all():
        clean = _redact(message, names)
        grouped[clean] = grouped.get(clean, 0) + total

    last = db.scalars(
        select(SyncRun)
        .where(SyncRun.tenant_id == tenant.id)
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    ).first()

    return TenantDiagnosticsOut(
        tenant_id=tenant.id,
        name=tenant.name,
        punches_pending=count(PunchRecord.process_state == PunchState.pending.value),
        punches_error=count(PunchRecord.process_state == PunchState.error.value),
        punches_unmapped=count(PunchRecord.process_state == PunchState.unmapped.value),
        punches_at_attempt_cap=count(
            PunchRecord.process_state == PunchState.error.value,
            PunchRecord.attempts >= 5,
        ),
        unmapped_badges=db.scalar(
            select(func.count(EmployeeMapping.id)).where(
                EmployeeMapping.tenant_id == tenant.id,
                EmployeeMapping.status.in_(
                    [MappingStatus.unmapped.value, MappingStatus.ambiguous.value]
                ),
            )
        ) or 0,
        last_run_status=last.status if last else None,
        last_run_error=_redact(last.error_message, names)
        if last and last.error_message
        else None,
        errors=[
            ErrorGroup(message=m, count=c)
            for m, c in sorted(grouped.items(), key=lambda kv: -kv[1])
        ],
        redacted=bool(names),
    )


@router.post("/tenants/{tenant_id}/schedule/reset", response_model=MessageOut)
def reset_schedule(
    tenant_id: str,
    request: Request,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """Clear the failure counter, lifting a customer out of the slow lane.

    After repeated failures a tenant is polled four times less often. Once the
    cause is fixed, waiting out the widened interval to prove it is pointless —
    this puts them back on their configured cadence straight away.
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such tenant")

    was = tenant.consecutive_failures
    tenant.consecutive_failures = 0
    audit_platform(
        db,
        actor,
        tenant.id,
        "platform.schedule.reset_failures",
        target=tenant.slug,
        detail=f"consecutive_failures: {was} -> 0 (by {actor.email})",
        request=request,
    )
    db.commit()
    return MessageOut(
        message=f"Failure count cleared ({was} -> 0). {tenant.name} is back on its "
                f"configured {tenant.sync_interval_minutes}-minute interval."
    )
