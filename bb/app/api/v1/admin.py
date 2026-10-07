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
    AccountClosure,
    ContactEvent,
    ContactRequest,
    OPEN_STAGES,
    PIPELINE_STAGES,
    PendingSignup,
    Device,
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
    ContactEventOut,
    ContactNoteIn,
    ContactRequestOut,
    ContactRequestUpdate,
    ErrorGroup,
    MailSettingsIn,
    StripeSettingsIn,
    TenantDeleteIn,
    MailTestIn,
    MessageOut,
    SubscriptionPlanAdminOut,
    SubscriptionPlanIn,
    SyncRunOut,
    TenantAdminOut,
    TenantConfigUpdate,
    TenantCreateIn,
    TenantCreateOut,
    TenantDeactivateIn,
    TenantDiagnosticsOut,
    TenantScheduleUpdate,
    TenantUsageOut,
)
from app.services import billing, mail_settings, stripe_settings
from app.services.account_deletion import EXIT_REASONS, delete_tenant
from app.services.device_limits import over_limit_device_ids
from app.services.mailer import MailError, build_message, deliver
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
        limit_max_employees=tenant.limit_max_employees,
        limit_max_devices=tenant.limit_max_devices,
        limit_min_sync_interval_minutes=tenant.limit_min_sync_interval_minutes,
        max_employees=tenant.plan_max_employees,
        max_devices=tenant.plan_max_devices,
        min_sync_interval_minutes=tenant.plan_min_sync_interval_minutes,
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


def _plan_out(db: Session, plan: SubscriptionPlan) -> SubscriptionPlanAdminOut:
    out = SubscriptionPlanAdminOut.model_validate(plan)
    out.stripe_price_id = plan.stripe_price_id
    out.stripe_yearly_price_id = plan.stripe_yearly_price_id
    out.tenants = db.scalar(select(func.count(Tenant.id)).where(Tenant.plan_id == plan.id)) or 0
    return out


@router.get("/plans", response_model=list[SubscriptionPlanAdminOut])
def list_plans(
    _: User = Depends(get_platform_admin), db: Session = Depends(get_db)
) -> list[SubscriptionPlanAdminOut]:
    """Every plan, active or retired.

    Retired ones stay in this list on purpose: it is what feeds the plan
    picker on an account's own config form, and hiding a retired plan there
    would leave that tenant's current selection unable to render — a select
    whose chosen option is not among its options.
    """
    plans = db.scalars(select(SubscriptionPlan).order_by(
        SubscriptionPlan.monthly_price_cents.is_(None), SubscriptionPlan.monthly_price_cents,
        SubscriptionPlan.name)).all()
    return [_plan_out(db, p) for p in plans]


def _apply_plan(db: Session, plan: SubscriptionPlan, data: dict) -> None:
    """Write ``data`` onto ``plan``, keeping the one-default rule."""
    if "name" in data:
        clash = db.scalar(select(SubscriptionPlan.id).where(
            func.lower(SubscriptionPlan.name) == data["name"].strip().lower(),
            SubscriptionPlan.id != plan.id))
        if clash:
            raise HTTPException(status.HTTP_409_CONFLICT, "A plan with that name already exists")
        data["name"] = data["name"].strip()
    for key in ("stripe_price_id", "stripe_yearly_price_id"):
        if data.get(key) == "":
            data[key] = None
    for key, value in data.items():
        setattr(plan, key, value)
    if plan.is_active is False:
        # A retired plan cannot be what new accounts are given.
        plan.is_default = False
    if data.get("is_default"):
        for other in db.scalars(select(SubscriptionPlan).where(
                SubscriptionPlan.is_default.is_(True), SubscriptionPlan.id != plan.id)):
            other.is_default = False


@router.post("/plans", response_model=SubscriptionPlanAdminOut, status_code=status.HTTP_201_CREATED)
def create_plan(
    payload: SubscriptionPlanIn,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> SubscriptionPlanAdminOut:
    """A new tier. Every limit left out is unlimited."""
    data = payload.model_dump(exclude_unset=True)
    if not (data.get("name") or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A plan needs a name")
    plan = SubscriptionPlan(name=data["name"].strip(), is_active=True, is_default=False)
    db.add(plan)
    _apply_plan(db, plan, data)
    db.commit()
    db.refresh(plan)
    log.info("Platform user %s created plan %s", actor.email, plan.name)
    return _plan_out(db, plan)


@router.patch("/plans/{plan_id}", response_model=SubscriptionPlanAdminOut)
def update_plan(
    plan_id: str,
    payload: SubscriptionPlanIn,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> SubscriptionPlanAdminOut:
    """Edit a tier — its limits apply to every account on it from now.

    Plans are retired (``is_active: false``), never deleted: accounts
    already on one keep it and keep working. Changing ``stripe_price_id``
    affects new checkouts and plan switches only; subscriptions already on
    the old Price stay there until moved in Stripe.
    """
    plan = db.get(SubscriptionPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such plan")
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to change")
    if "name" in data and not (data["name"] or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A plan needs a name")
    for flag in ("is_active", "is_default"):
        if flag in data and data[flag] is None:
            del data[flag]
    before = {k: getattr(plan, k) for k in data}
    _apply_plan(db, plan, data)
    db.commit()
    db.refresh(plan)
    log.info("Platform user %s changed plan %s — %s", actor.email, plan.name,
             ", ".join(f"{k}: {before[k]} -> {getattr(plan, k)}" for k in data))
    return _plan_out(db, plan)


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


@router.get("/tenants/{tenant_id}/usage", response_model=TenantUsageOut)
def tenant_usage(
    tenant_id: str,
    _: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> TenantUsageOut:
    """What this account uses against the limits in force — counts only."""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such tenant")
    return TenantUsageOut(
        employees_mapped=db.scalar(select(func.count(EmployeeMapping.id)).where(
            EmployeeMapping.tenant_id == tenant.id,
            EmployeeMapping.status == MappingStatus.mapped.value)) or 0,
        max_employees=tenant.plan_max_employees,
        devices=db.scalar(select(func.count(Device.id)).where(Device.tenant_id == tenant.id)) or 0,
        max_devices=tenant.plan_max_devices,
        devices_over_limit=len(over_limit_device_ids(db, tenant)),
        punches_held=db.scalar(select(func.count(PunchRecord.id)).where(
            PunchRecord.tenant_id == tenant.id,
            PunchRecord.process_state == PunchState.held.value)) or 0,
        sync_interval_minutes=tenant.sync_interval_minutes,
        min_sync_interval_minutes=tenant.plan_min_sync_interval_minutes,
    )


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

    # Assigning a plan (or a new interval floor) brings the account's sync
    # interval up to what it now allows, the same as a signup or a customer's
    # own switch does — otherwise the account would hold a cadence its own
    # plan forbids, and the customer's next settings save would be refused
    # for a value they never chose. Staff can still set a faster one
    # afterwards from the account row, which is a deliberate exception.
    plan_changed = "plan_id" in data and before["plan_id"] != tenant.plan_id
    floor_changed = ("limit_min_sync_interval_minutes" in data
                     and before["limit_min_sync_interval_minutes"] != tenant.limit_min_sync_interval_minutes)
    if plan_changed or floor_changed:
        db.flush()
        db.refresh(tenant, ["plan"])
        floor = tenant.plan_min_sync_interval_minutes
        if floor and tenant.sync_interval_minutes < floor:
            before["sync_interval_minutes"] = tenant.sync_interval_minutes
            tenant.sync_interval_minutes = floor
            data["sync_interval_minutes"] = floor

    changes = ", ".join(f"{k}: {before[k]} -> {v}" for k, v in data.items() if before[k] != v) \
        or "no changes"
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

    # Start inside the plan's limits, as a self-signup does.
    chosen = db.get(SubscriptionPlan, plan_id) if plan_id else None
    interval = payload.sync_interval_minutes
    if chosen and chosen.min_sync_interval_minutes:
        interval = max(interval, chosen.min_sync_interval_minutes)

    tenant = Tenant(
        name=payload.company_name,
        slug=_unique_slug(db, _slugify(payload.company_name)),
        status=TenantStatus.trialing.value,
        timezone=payload.timezone,
        sync_interval_minutes=interval,
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


# --- Email server ------------------------------------------------------------
# Platform-wide, so it lives here with the other staff-only routes. The SMTP
# password is write-only: it is never sent back, only "has_password".

@router.get("/mail", tags=["platform"])
def get_mail_settings(_: User = Depends(get_platform_admin), db: Session = Depends(get_db)) -> dict:
    """The saved Email server settings, which source is in use, and presets."""
    return mail_settings.public_view(db)


@router.patch("/mail", tags=["platform"])
def update_mail_settings(
    payload: MailSettingsIn,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    data = payload.model_dump(exclude_unset=True)
    saved = mail_settings.stored_config(db)
    host = (data.get("host") if data.get("host") is not None else (saved.host if saved else "")) or ""
    enabled = data.get("enabled") if data.get("enabled") is not None else (saved.enabled if saved else True)
    if enabled and not host.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An SMTP host is needed, or switch the setting off.")
    from_email = data.get("from_email") or (saved.from_email if saved else "")
    if enabled and not from_email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A From address is needed.")
    if enabled and "gmail" in host.lower():
        has_password = bool(data.get("password")) or (bool(saved and saved.password) and not data.get("clear_password"))
        username = data.get("username") if data.get("username") is not None else (saved.username if saved else "")
        if not has_password or not (username or from_email):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Gmail needs the account's full address as Username and an App Password.")
    mail_settings.save(db, data, actor.email)
    log.info("Platform user %s changed the email server settings (%s)", actor.email,
             ", ".join(sorted(k for k in data if k != "password")) + (", password" if data.get("password") else ""))
    return mail_settings.public_view(db)


@router.post("/mail/test", response_model=MessageOut, tags=["platform"])
def send_test_mail(
    payload: MailTestIn,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """Send one message through the settings in use right now."""
    config = mail_settings.active_config(db)
    if not config.can_send:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "No email server is configured yet — save the settings first.")
    message = build_message(
        config, str(payload.to), "BioBridge test email",
        "This is a test email from BioBridge.\n\n"
        f"Sent through {config.host}:{config.port} ({config.security.upper()}) "
        f"from {config.from_email or config.username}, requested by {actor.email}.\n\n"
        "If you can read this, signup confirmations and login details will be delivered.\n",
    )
    try:
        deliver(config, message)
    except MailError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    log.info("Platform user %s sent a test email to %s via %s", actor.email, payload.to, config.host)
    return MessageOut(message=f"Test email sent to {payload.to} through {config.host}.")


# --- Payments (Stripe) -------------------------------------------------------
# The keys are write-only like the SMTP password: only a hint (sk_test_…4242)
# is ever returned. Price ids stay on each plan (Plans page).

@router.get("/stripe", tags=["platform"])
def get_stripe_settings(_: User = Depends(get_platform_admin), db: Session = Depends(get_db)) -> dict:
    return stripe_settings.public_view(db)


@router.patch("/stripe", tags=["platform"])
def update_stripe_settings(
    payload: StripeSettingsIn,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    data = payload.model_dump(exclude_unset=True)
    stripe_settings.save(db, data, actor.email)
    billing.load_config(db)
    log.info("Platform user %s changed the Stripe settings (%s)", actor.email,
             ", ".join(sorted(k for k, v in data.items() if v not in (None, "", False))))
    return stripe_settings.public_view(db)


@router.post("/stripe/test", tags=["platform"])
def test_stripe(_: User = Depends(get_platform_admin), db: Session = Depends(get_db)) -> dict:
    """Check the key in use against Stripe, and every active plan's price."""
    billing.load_config(db)
    if not billing.enabled():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No Stripe secret key is configured yet.")
    mode = stripe_settings.mode_of(billing.secret_key())
    plans = db.scalars(select(SubscriptionPlan).where(SubscriptionPlan.is_active.is_(True))
                       .order_by(SubscriptionPlan.monthly_price_cents)).all()
    try:
        billing._request("GET", "prices", {"limit": 1})
    except billing.BillingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    results = []

    def check(label: str, price_id: str | None, interval: str, expected: int | None) -> None:
        row = {"plan": label, "stripe_price_id": price_id, "ok": False, "message": ""}
        results.append(row)
        if not price_id:
            row["message"] = "No Stripe price — this plan can't be bought online."
            return
        try:
            price = billing._request("GET", f"prices/{price_id}")
        except billing.BillingError as exc:
            row["message"] = (f"{exc}. A {mode or ''} key can only see {mode or 'its own'}-mode prices."
                              if "No such price" in str(exc) else str(exc))
            return
        recurring = price.get("recurring") or {}
        problems = []
        if not price.get("active", True):
            problems.append("the price is archived in Stripe")
        if recurring.get("interval") != interval:
            problems.append(f"it isn't a {'yearly' if interval == 'year' else 'monthly'} recurring price")
        amount = price.get("unit_amount")
        if expected is not None and amount is not None and amount != expected:
            problems.append(f"Stripe charges {amount / 100:.2f} {str(price.get('currency', '')).upper()} "
                            f"but the plan says {expected / 100:.2f}")
        row["ok"] = not problems
        row["message"] = "; ".join(problems).capitalize() if problems else (
            f"{(amount or 0) / 100:.2f} {str(price.get('currency', '')).upper()} / "
            f"{'year' if interval == 'year' else 'month'}")

    for plan in plans:
        check(plan.name, plan.stripe_price_id, "month", plan.monthly_price_cents)
        # A yearly price is optional: only checked when the plan has one.
        if plan.stripe_yearly_price_id:
            check(f"{plan.name} (yearly)", plan.stripe_yearly_price_id, "year", plan.yearly_price_cents)
    webhook = bool(billing.webhook_secret())
    return {
        "ok": all(r["ok"] for r in results) and webhook,
        "mode": mode,
        "webhook_secret_set": webhook,
        "message": f"Connected to Stripe in {mode or 'unknown'} mode."
                   + ("" if webhook else " The webhook signing secret is missing, so payments will never activate accounts."),
        "plans": results,
    }


# --- Deleting plans and accounts ----------------------------------------------

@router.delete("/plans/{plan_id}", response_model=MessageOut)
def delete_plan(
    plan_id: str,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """Remove a plan for good. Only a plan nobody is on — or switching to —
    can go; anything else is retired instead, so no account loses its limits."""
    plan = db.get(SubscriptionPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such plan")
    on_it = db.scalar(select(func.count()).select_from(Tenant).where(
        (Tenant.plan_id == plan.id) | (Tenant.pending_plan_id == plan.id)))
    if on_it:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{on_it} account{'s are' if on_it != 1 else ' is'} on {plan.name} or switching to it. "
            "Move them to another plan first, or retire it instead — a retired plan is hidden from new customers.")
    waiting = db.scalar(select(func.count()).select_from(PendingSignup).where(
        PendingSignup.plan_id == plan.id, PendingSignup.tenant_id.is_(None)))
    if waiting:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{waiting} website checkout{'s are' if waiting != 1 else ' is'} still open for {plan.name}. "
            "Retire it now and delete it later.")
    name = plan.name
    db.delete(plan)
    db.commit()
    log.warning("Platform user %s deleted plan %s", actor.email, name)
    return MessageOut(message=f"{name} deleted.")


#: Accounts staff may delete: deactivated by hand or by the lapse rules
#: (suspended, cancelled) or not paying (past due). Never trialing or active.
DELETABLE_STATUSES = (
    TenantStatus.suspended.value, TenantStatus.cancelled.value, TenantStatus.past_due.value,
)


@router.post("/tenants/{tenant_id}/delete", response_model=MessageOut)
def delete_tenant_account(
    tenant_id: str,
    payload: TenantDeleteIn,
    actor: User = Depends(get_platform_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """Permanently delete a customer account that is no longer in good standing:
    deactivated (suspended or cancelled) or past due. The name has to be typed
    back. A live Stripe subscription is cancelled first."""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such tenant")
    if tenant.status not in DELETABLE_STATUSES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{tenant.name} is still {tenant.status}. Deactivate it first — only a suspended, cancelled "
            "or past-due account can be deleted.")
    if payload.confirm_name.strip().lower() != tenant.name.strip().lower():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The name typed doesn't match the account name.")
    if actor.tenant_id == tenant.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is your own workspace — it can't be deleted from the console.")
    billing.load_config(db)
    name = tenant.name
    try:
        delete_tenant(db, tenant, closed_by="staff", closed_by_email=actor.email,
                      reason_code="staff", reason_text=payload.reason)
    except billing.BillingError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Not deleted — the Stripe subscription couldn't be cancelled: {exc}") from exc
    return MessageOut(message=f"{name} and all its data were deleted.")


@router.get("/closures", tags=["platform"])
def list_closures(
    _: User = Depends(get_platform_admin), db: Session = Depends(get_db), limit: int = Query(default=200, le=500),
) -> list[dict]:
    """Deleted accounts, newest first, with the reason given."""
    rows = db.scalars(select(AccountClosure).order_by(AccountClosure.closed_at.desc()).limit(limit)).all()
    return [{
        "id": r.id, "tenant_name": r.tenant_name, "tenant_slug": r.tenant_slug, "owner_email": r.owner_email,
        "plan_name": r.plan_name, "status_before": r.status_before, "closed_by": r.closed_by,
        "closed_by_email": r.closed_by_email, "reason_code": r.reason_code,
        "reason_label": EXIT_REASONS.get(r.reason_code or "", "Deleted by staff" if r.closed_by == "staff" else r.reason_code),
        "reason_text": r.reason_text, "stripe_subscription_cancelled": r.stripe_subscription_cancelled,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
    } for r in rows]


# ===========================================================================
# Leads — the website's Contact / Book a demo requests
# ===========================================================================
@router.get("/contact-requests", response_model=list[ContactRequestOut], tags=["platform"])
def list_contact_requests(
    status_filter: str | None = Query(default=None, alias="status"),
    _: User = Depends(get_platform_admin), db: Session = Depends(get_db),
    limit: int = Query(default=300, le=500),
) -> list[ContactRequest]:
    """Newest first. ``status=open`` is every stage before won / lost."""
    stmt = select(ContactRequest).order_by(ContactRequest.created_at.desc()).limit(limit)
    if status_filter == "open":
        stmt = stmt.where(ContactRequest.status.in_(OPEN_STAGES))
    elif status_filter:
        stmt = stmt.where(ContactRequest.status == status_filter)
    return list(db.scalars(stmt).all())


@router.get("/contact-requests/pipeline", tags=["platform"])
def contact_pipeline(
    _: User = Depends(get_platform_admin), db: Session = Depends(get_db),
) -> dict:
    """The pipeline at a glance: a count per stage (in order), what is still
    open, and how well leads close. Conversion is won / (won + lost) — leads
    still in play are neither yet, so they do not drag it down."""
    counts = dict(db.execute(
        select(ContactRequest.status, func.count()).group_by(ContactRequest.status)
    ).all())
    stages = [{"stage": st, "count": int(counts.get(st, 0))} for st in PIPELINE_STAGES]
    won, lost = int(counts.get("won", 0)), int(counts.get("lost", 0))
    won_rows = db.execute(
        select(ContactRequest.created_at, ContactRequest.stage_changed_at)
        .where(ContactRequest.status == "won", ContactRequest.stage_changed_at.is_not(None))
    ).all()
    days = [(w - c).total_seconds() / 86400 for c, w in won_rows if w and c]
    return {
        "stages": stages,
        "open": sum(int(counts.get(st, 0)) for st in OPEN_STAGES),
        "won": won,
        "lost": lost,
        "conversion": round(won / (won + lost), 3) if (won + lost) else None,
        "avg_days_to_win": round(sum(days) / len(days), 1) if days else None,
    }


@router.patch("/contact-requests/{request_id}", response_model=ContactRequestOut, tags=["platform"])
def update_contact_request(
    request_id: str, payload: ContactRequestUpdate,
    actor: User = Depends(get_platform_admin), db: Session = Depends(get_db),
) -> ContactRequest:
    req = db.get(ContactRequest, request_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such request")
    data = payload.model_dump(exclude_unset=True)
    new_stage = data.get("status")
    if new_stage and new_stage != req.status:
        db.add(ContactEvent(
            contact_id=req.id, kind="stage", from_stage=req.status, to_stage=new_stage,
            actor=actor.email, created_at=datetime.now(timezone.utc),
        ))
        req.status = new_stage
        req.stage_changed_at = datetime.now(timezone.utc)
        if new_stage != "lost":
            req.lost_reason = None   # a reason belongs to a lost lead only
    # A lead that asked for a demo gets a pending one the moment it is worked.
    if (new_stage in ("contacted", "qualified") and req.demo_status is None and "demo_status" not in data
            and (req.topic == "Demo" or req.preferred_date)):
        data["demo_status"] = "pending"
    if "demo_status" in data and data["demo_status"] != req.demo_status:
        if data["demo_status"] is not None and req.status == "new":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Move the lead to Contacted before setting a demo status")
        db.add(ContactEvent(
            contact_id=req.id, kind="demo", from_stage=req.demo_status, to_stage=data["demo_status"],
            actor=actor.email, created_at=datetime.now(timezone.utc),
        ))
        req.demo_status = data["demo_status"]
    if "notes" in data:
        req.notes = data["notes"]
    if "lost_reason" in data and req.status == "lost":
        req.lost_reason = (data["lost_reason"] or "").strip() or None
    req.handled_by = actor.email
    db.commit()
    db.refresh(req)
    return req


@router.get(
    "/contact-requests/{request_id}/events", response_model=list[ContactEventOut], tags=["platform"]
)
def contact_request_events(
    request_id: str, _: User = Depends(get_platform_admin), db: Session = Depends(get_db),
) -> list[ContactEvent]:
    """A lead's history, newest first."""
    if db.get(ContactRequest, request_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such request")
    return list(db.scalars(
        select(ContactEvent).where(ContactEvent.contact_id == request_id)
        .order_by(ContactEvent.created_at.desc())
    ).all())


@router.post(
    "/contact-requests/{request_id}/events", response_model=ContactEventOut,
    status_code=status.HTTP_201_CREATED, tags=["platform"],
)
def add_contact_note(
    request_id: str, payload: ContactNoteIn,
    actor: User = Depends(get_platform_admin), db: Session = Depends(get_db),
) -> ContactEvent:
    """A dated note on the lead's timeline ("called, voicemail")."""
    req = db.get(ContactRequest, request_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such request")
    event = ContactEvent(
        contact_id=req.id, kind="note", note=payload.note.strip(), actor=actor.email,
        created_at=datetime.now(timezone.utc),
    )
    req.handled_by = actor.email
    db.add(event)
    db.commit()
    db.refresh(event)
    return event
