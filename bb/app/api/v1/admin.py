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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import audit_platform, get_platform_admin
from app.core.config import settings
from app.core.security import hash_password
from app.services.email_check import UngenuineEmailError, assert_genuine_email
from app.services.email_verification import issue_verification_token, send_verification_email
from app.db.session import get_db
from app.models import (
    DeviceSource,
    EmployeeMapping,
    MappingStatus,
    OdooConnection,
    PunchRecord,
    PunchState,
    SubscriptionPlan,
    SyncRun,
    Tenant,
    TenantStatus,
    User,
    UserRole,
)
from app.schemas import (
    ErrorGroup,
    MessageOut,
    SubscriptionPlanOut,
    SyncRunOut,
    TenantAdminOut,
    TenantConfigUpdate,
    TenantCreateIn,
    TenantCreateOut,
    TenantDeactivateIn,
    TenantDiagnosticsOut,
    TenantScheduleUpdate,
)
from app.services.scheduling import (
    SYNCABLE,
    effective_interval,
    next_run_at,
    renewal_warning,
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
        syncable=tenant.syncable,
        suspended_at=tenant.suspended_at,
        suspension_reason=tenant.suspension_reason,
        plan_id=tenant.plan_id,
        plan_name=tenant.plan_name,
        subscription_renews_at=tenant.subscription_renews_at,
        renewal_warning=renewal_warning(tenant),
        pending_plan_id=tenant.pending_plan_id,
        pending_plan_name=tenant.pending_plan_name,
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


@router.get("/overview", tags=["platform"])
def platform_overview(
    days: int = Query(default=14, ge=7, le=60),
    _: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """The console's landing page: the whole platform at a glance.

    Counts and states only — the same line the rest of the console holds.
    Nothing here reads a punch's content, an attendance record or an employee:
    punch volume is a count per day across every account, and the error text
    from failed runs is scrubbed of employee names (``_redact``) exactly as the
    per-account diagnostics are.
    """
    now = datetime.now(timezone.utc)
    tenants = db.scalars(select(Tenant).order_by(Tenant.name)).all()
    rows = [(t, _to_out(db, t)) for t in tenants]

    by_status = {s.value: 0 for s in TenantStatus}
    for t, _o in rows:
        by_status[t.status] = by_status.get(t.status, 0) + 1

    def aware(value):
        return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)

    new_30d = sum(1 for t, _o in rows if t.created_at and aware(t.created_at) >= now - timedelta(days=30))

    ready = sum(1 for _t, o in rows if o.odoo_connected and o.source_connected)
    none_connected = sum(1 for _t, o in rows if not o.odoo_connected and not o.source_connected)

    plans = {p.id: p for p in db.scalars(select(SubscriptionPlan)).all()}
    by_plan: dict[str, dict] = {}
    mrr = 0
    for t, _o in rows:
        plan = plans.get(t.plan_id)
        key = plan.name if plan else "No plan"
        entry = by_plan.setdefault(key, {"name": key, "accounts": 0, "paying": 0, "mrr_cents": 0,
                                         "price_cents": plan.monthly_price_cents if plan else None})
        entry["accounts"] += 1
        if t.status == TenantStatus.active.value and plan and plan.monthly_price_cents:
            entry["paying"] += 1
            entry["mrr_cents"] += plan.monthly_price_cents
            mrr += plan.monthly_price_cents

    # Punch volume per UTC day, every account together.
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    counts = dict(
        db.execute(
            select(func.date(PunchRecord.punch_time_utc), func.count(PunchRecord.id))
            .where(PunchRecord.punch_time_utc >= start.replace(tzinfo=None))
            .group_by(func.date(PunchRecord.punch_time_utc))
        ).all()
    )
    series = []
    for i in range(days):
        day = (start + timedelta(days=i)).date()
        series.append({"day": day.isoformat(), "punches": int(counts.get(day.isoformat(), counts.get(day, 0)) or 0)})

    since = now - timedelta(hours=24)
    runs_24h = {"success": 0, "partial": 0, "failed": 0}
    for status_, n in db.execute(
        select(SyncRun.status, func.count(SyncRun.id))
        .where(SyncRun.started_at >= since, SyncRun.status != "running")
        .group_by(SyncRun.status)
    ).all():
        runs_24h[status_] = n

    # Who needs a person, most urgent first.
    names = {t.id: t.name for t, _o in rows}
    attention = []
    for t, o in rows:
        reasons = []
        severity = 0
        if t.status in ("past_due", "suspended"):
            reasons.append({"tone": "bad", "text": t.status.replace("_", " ")})
            severity = max(severity, 3)
        if o.syncable and o.last_run_status == "failed":
            reasons.append({"tone": "bad", "text": "last sync failed"})
            severity = max(severity, 3)
        if o.interval_widened:
            reasons.append({"tone": "warn", "text": f"backed off to {o.effective_interval_minutes} min"})
            severity = max(severity, 2)
        if o.renewal_warning:
            days_left = o.renewal_warning.days_left if hasattr(o.renewal_warning, "days_left") else o.renewal_warning["days_left"]
            urgent = o.renewal_warning.urgent if hasattr(o.renewal_warning, "urgent") else o.renewal_warning["urgent"]
            reasons.append({"tone": "bad" if urgent else "warn",
                            "text": "renews today" if days_left <= 0 else f"renews in {days_left} d"})
            severity = max(severity, 3 if urgent else 1)
        if t.status not in ("cancelled",) and not (o.odoo_connected and o.source_connected):
            reasons.append({"tone": "mute", "text": "nothing connected" if not o.odoo_connected and not o.source_connected
                            else "no Odoo" if not o.odoo_connected else "no device"})
            severity = max(severity, 1)
        if reasons:
            attention.append({"id": t.id, "name": t.name, "status": t.status,
                              "reasons": reasons, "severity": severity})
    attention.sort(key=lambda a: (-a["severity"], a["name"].lower()))

    renewals = sorted(
        (
            {"id": t.id, "name": t.name, "status": t.status, "plan_name": o.plan_name,
             "renews_at": aware(t.subscription_renews_at).isoformat()}
            for t, o in rows
            if t.subscription_renews_at
            and now <= aware(t.subscription_renews_at) <= now + timedelta(days=14)
            # Only accounts that are still current: a lapsed or stopped one
            # already shows under Needs attention, for the reason that matters.
            and t.status in (TenantStatus.trialing.value, TenantStatus.active.value)
        ),
        key=lambda r: r["renews_at"],
    )

    failed = db.scalars(
        select(SyncRun).where(SyncRun.status == "failed").order_by(SyncRun.started_at.desc()).limit(8)
    ).all()
    failed_runs = []
    for r in failed:
        people = {
            n for (n,) in db.execute(
                select(EmployeeMapping.odoo_employee_name).where(EmployeeMapping.tenant_id == r.tenant_id)
            ).all() if n
        } | {
            n for (n,) in db.execute(
                select(EmployeeMapping.source_name).where(EmployeeMapping.tenant_id == r.tenant_id)
            ).all() if n
        }
        failed_runs.append({
            "tenant_id": r.tenant_id,
            "tenant_name": names.get(r.tenant_id, "—"),
            "started_at": aware(r.started_at).isoformat(),
            "triggered_by": r.triggered_by,
            "message": _redact(r.error_message or "", people)[:300],
        })

    health = scheduler_health(db)
    return {
        "generated_at": now.isoformat(),
        "accounts": {"total": len(rows), "by_status": by_status, "new_30d": new_30d},
        "setup": {"ready": ready, "partial": len(rows) - ready - none_connected, "none": none_connected},
        "health": {
            "failing": sum(1 for _t, o in rows if o.syncable and o.last_run_status == "failed"),
            "backed_off": sum(1 for _t, o in rows if o.interval_widened),
            "sync_off": sum(1 for _t, o in rows if not o.sync_enabled),
        },
        "activity": {
            "punches_today": series[-1]["punches"] if series else 0,
            "punches_period": sum(d["punches"] for d in series),
            "runs_24h": runs_24h,
            "error_punches": db.scalar(
                select(func.count(PunchRecord.id)).where(PunchRecord.process_state == PunchState.error.value)
            ) or 0,
            "unmatched_badges": db.scalar(
                select(func.count(EmployeeMapping.id)).where(EmployeeMapping.status == MappingStatus.unmapped.value)
            ) or 0,
        },
        "revenue": {"mrr_cents": mrr, "paying": by_status.get("active", 0), "by_plan": sorted(
            by_plan.values(), key=lambda p: (-p["accounts"], p["name"]))},
        "punches_by_day": series,
        "attention": attention[:12],
        "attention_total": len(attention),
        "renewals": renewals[:10],
        "failed_runs": failed_runs,
        "scheduler": {
            "running": health["running"],
            "mode": health["mode"],
            "last_tick_at": health["last_tick_at"].isoformat() + "Z" if health["last_tick_at"] else None,
        },
    }


@router.get("/plans", response_model=list[SubscriptionPlanOut])
def list_plans(
    _: User = Depends(get_platform_admin), db: Session = Depends(get_db)
) -> list[SubscriptionPlan]:
    """Every plan, active or retired.

    Retired ones stay in this list on purpose: it is what feeds the plan
    picker on an account's own config form, and hiding a retired plan there
    would leave that tenant's current selection unable to render — a select
    whose chosen option is not among its options. Plans are not created or
    edited through this API at all; see tools/seed_plans.py.
    """
    return db.scalars(select(SubscriptionPlan).order_by(SubscriptionPlan.name)).all()


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

    # A bad id here would otherwise surface as a raw foreign-key error from
    # the database at commit time, which is a 500 for what is really a 400 —
    # the console sent a plan that does not exist, most likely a stale list.
    if "plan_id" in data and data["plan_id"] is not None:
        if db.get(SubscriptionPlan, data["plan_id"]) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such plan")

    if "plan_id" in data and tenant.pending_plan_id is not None:
        # Staff setting plan_id directly is a deliberate override — the same
        # power they already have over status and pairing rules. It
        # supersedes whatever the customer had queued through self-service,
        # rather than leaving that switch to land later and quietly undo
        # this one at the next renewal.
        tenant.pending_plan_id = None

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


@router.post("/tenants/{tenant_id}/deactivate", response_model=TenantAdminOut)
def deactivate_tenant(
    tenant_id: str,
    payload: TenantDeactivateIn,
    request: Request,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> TenantAdminOut:
    """Stop this account syncing — the subscription gate.

    Its own endpoint rather than a ``status`` field on the config form, because
    the intent is different from editing a setting: this is what happens when a
    subscription lapses or ends, it needs a reason attached, and it should read
    as one deliberate act in the audit trail instead of
    ``status: active -> suspended`` among a form's other changes.

    **It does not touch ``sync_enabled``.** That switch belongs to the
    customer, and a suspension that flipped it would (a) look to them like they
    turned their own sync off, and (b) silently switch syncing on for an
    account that had chosen to have it off, the moment anyone reactivated.
    Status and preference are separate questions and stay separate.

    The customer keeps their data and their screens. Only the syncing stops —
    which is also what makes this safe to use for non-payment: nothing is
    destroyed and reactivating is one click.
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such tenant")

    if tenant.status == TenantStatus.cancelled.value:
        # Cancelled is the stronger, more deliberate state. Quietly turning it
        # into "suspended" would lose that, and it already does not sync, so
        # there is nothing for this action to achieve.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{tenant.name} is already cancelled, which does not sync. Use the "
            f"account form to change a cancelled account's status.",
        )

    already = not tenant.syncable
    tenant.status = TenantStatus.suspended.value
    tenant.suspension_reason = (payload.reason or "").strip() or None
    # Only on the way in: re-running this to correct the reason should not move
    # the date and lose when the account actually stopped.
    if not already:
        tenant.suspended_at = datetime.now(timezone.utc)

    # The reason is deliberately NOT in the audit detail. This trail belongs to
    # the customer — it exists so they can see that support changed something —
    # and a staff note like "chasing payment, third email" is for the next
    # engineer, not for them. It lives on the tenant row, which only the console
    # can read, and in the server log below.
    audit_platform(
        db,
        actor,
        tenant.id,
        "platform.tenant.deactivate",
        target=tenant.slug,
        detail=f"syncing stopped by platform staff (by {actor.email})",
        request=request,
    )
    db.commit()
    db.refresh(tenant)

    log.warning(
        "Platform user %s deactivated %s — reason: %s",
        actor.email, tenant.slug, tenant.suspension_reason or "(none given)",
    )
    return _to_out(db, tenant)


@router.post("/tenants/{tenant_id}/activate", response_model=TenantAdminOut)
def activate_tenant(
    tenant_id: str,
    request: Request,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> TenantAdminOut:
    """Let this account sync again.

    Sets *active* rather than restoring whatever it was before. The previous
    status is not recorded, and guessing would be worse than being plain: staff
    reactivating an account mean it may run, and an account that should go back
    to trialing can be set there on the account form.

    ``sync_enabled`` is untouched, so a customer who had their own sync switched
    off stays switched off — reactivating returns the account to their control,
    it does not make a decision on their behalf.
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such tenant")

    was = tenant.status
    tenant.status = TenantStatus.active.value
    tenant.suspended_at = None
    tenant.suspension_reason = None

    audit_platform(
        db,
        actor,
        tenant.id,
        "platform.tenant.activate",
        target=tenant.slug,
        detail=f"syncing restored by platform staff, was {was} (by {actor.email})",
        request=request,
    )
    db.commit()
    db.refresh(tenant)

    log.info("Platform user %s activated %s (was %s)", actor.email, tenant.slug, was)
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
    try:
        email = assert_genuine_email(payload.owner_email).lower()
    except UngenuineEmailError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if db.scalar(select(func.count(User.id)).where(User.email == email)):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A user with that email already exists"
        )

    password = payload.owner_password or secrets.token_urlsafe(16)

    if payload.plan_id is not None:
        if db.get(SubscriptionPlan, payload.plan_id) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such plan")
        plan_id = payload.plan_id
    else:
        # Same rule self-signup follows: whichever plan is marked default,
        # or none at all if no plan has ever been set up for this deployment.
        default_plan = db.scalars(
            select(SubscriptionPlan).where(SubscriptionPlan.is_default.is_(True))
        ).first()
        plan_id = default_plan.id if default_plan else None

    tenant = Tenant(
        name=payload.company_name,
        slug=_unique_slug(db, _slugify(payload.company_name)),
        status=TenantStatus.trialing.value,
        timezone=payload.timezone,
        sync_interval_minutes=payload.sync_interval_minutes,
        plan_id=plan_id,
        # Same trial length as self-signup (settings.trial_days). Staff can
        # move it immediately from the account's own row if this onboarding
        # should start on a different clock.
        subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=settings.trial_days),
    )
    db.add(tenant)
    db.flush()

    owner = User(
        tenant_id=tenant.id,
        email=email,
        full_name=payload.owner_name,
        hashed_password=hash_password(password),
        role=UserRole.owner.value,
    )
    db.add(owner)
    db.flush()
    raw_verify_token = issue_verification_token(owner)

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

    # Best effort, same as self-signup — staff already hand the password to
    # the owner directly, so a failed send here delays confirmation, not
    # access. See app.services.email_verification.
    try:
        send_verification_email(owner, raw_verify_token)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not send verification email to %s: %s", owner.email, exc)

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
