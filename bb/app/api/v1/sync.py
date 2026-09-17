"""Sync control, the ledger, and the dashboard summary."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Principal, audit, get_principal, require_writer
from app.core.config import settings
from app.db.session import get_db
from app.models import (
    AttendanceRecord,
    DeviceSource,
    EmployeeMapping,
    MappingStatus,
    OdooConnection,
    PunchRecord,
    PunchState,
    SyncRun,
    Tenant,
)
from app.schemas import (
    AttendanceOut,
    DashboardOut,
    MappingOut,
    MappingUpdate,
    MessageOut,
    PunchOut,
    SyncRunOut,
    TenantOut,
    TenantUpdate,
)
from app.services.sync_engine import SyncEngine
from app.services.timeutils import utcnow_naive

log = logging.getLogger(__name__)
router = APIRouter(tags=["sync"])


# ===========================================================================
# Tenant settings
# ===========================================================================
@router.get("/tenant", response_model=TenantOut)
def get_tenant(principal: Principal = Depends(get_principal)) -> Tenant:
    return principal.tenant


@router.patch("/tenant", response_model=TenantOut)
def update_tenant(
    payload: TenantUpdate,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> Tenant:
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(principal.tenant, key, value)
    audit(db, principal, "tenant.update", None, ",".join(data), request)
    db.commit()
    db.refresh(principal.tenant)
    return principal.tenant


# ===========================================================================
# Sync control
# ===========================================================================
def _has_live_worker() -> bool:
    """Is a worker actually consuming the queue?

    A task queued with no worker sits forever with no error and no visible sign
    anything is wrong — from the UI it is indistinguishable from the button
    doing nothing. ``delay()`` only fails if the *broker* is unreachable, so a
    crashed worker on a healthy Redis passes silently. Ping for a live consumer.

    Skipped entirely when no broker is configured, so the single-process
    quick-start does not stall for seconds before falling back.
    """
    if not settings.redis_url:
        return False
    try:
        from app.workers.celery_app import celery_app

        return bool(celery_app.control.inspect(timeout=0.75).ping())
    except Exception as exc:  # noqa: BLE001 — any transport problem means "no"
        log.warning("Could not confirm a live worker (%s)", exc)
        return False


def _queue_or_run(principal: Principal, db: Session) -> str:
    """Queue a sync, falling back to running it inline.

    Without the fallback, a deployment with no worker leaves the button silently
    doing nothing.
    """
    if _has_live_worker():
        try:
            from app.workers.tasks import sync_tenant

            sync_tenant.delay(principal.tenant.id, "manual")
            return "Sync queued"
        except Exception as exc:  # noqa: BLE001 — the broker rejected the send
            log.warning("Could not queue the sync (%s); running inline", exc)

    run = SyncEngine(db, principal.tenant, "manual").run_cycle()
    return (
        f"Ran inline: {run.status} — {run.punches_new} new punch(es), "
        f"{run.attendances_created} attendance record(s) created, "
        f"{run.attendances_closed} closed."
    )


@router.post("/sync/run", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED)
def trigger_sync(
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> MessageOut:
    audit(db, principal, "sync.manual", None, None, request)
    db.commit()
    return MessageOut(message=_queue_or_run(principal, db))


@router.post("/sync/run-inline", response_model=SyncRunOut)
def trigger_sync_inline(
    principal: Principal = Depends(require_writer), db: Session = Depends(get_db)
) -> SyncRun:
    """Run a cycle synchronously — used by onboarding's 'first sync' step."""
    return SyncEngine(db, principal.tenant, "manual").run_cycle()


@router.post("/sync/reset-cursor", response_model=MessageOut)
def reset_cursor(
    request: Request,
    days_back: int = Query(default=7, ge=1, le=90),
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> MessageOut:
    """Move the cursor back, then actually sync.

    A cursor reset with no follow-up is invisible: nothing changes on screen
    until something runs a cycle, and with no beat running that may be never.
    """
    sources = db.scalars(
        select(DeviceSource).where(DeviceSource.tenant_id == principal.tenant.id)
    ).all()
    if not sources:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No device source is connected yet."
        )

    new_cursor = utcnow_naive() - timedelta(days=days_back)
    for source in sources:
        source.cursor_punch_time = new_cursor
    audit(db, principal, "sync.reset_cursor", None, f"days_back={days_back}", request)
    db.commit()

    return MessageOut(
        message=(
            f"Cursor moved back {days_back} day(s) on {len(sources)} source(s). "
            f"{_queue_or_run(principal, db)}"
        )
    )


@router.get("/sync/runs", response_model=list[SyncRunOut])
def list_runs(
    limit: int = Query(default=25, le=100),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[SyncRun]:
    return list(
        db.scalars(
            select(SyncRun)
            .where(SyncRun.tenant_id == principal.tenant.id)
            .order_by(SyncRun.started_at.desc())
            .limit(limit)
        ).all()
    )


# ===========================================================================
# Ledger and mappings
# ===========================================================================
@router.get("/punches", response_model=list[PunchOut])
def list_punches(
    state: str | None = None,
    emp_code: str | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[PunchRecord]:
    stmt = select(PunchRecord).where(PunchRecord.tenant_id == principal.tenant.id)
    if state:
        stmt = stmt.where(PunchRecord.process_state == state)
    if emp_code:
        stmt = stmt.where(PunchRecord.emp_code == emp_code)
    stmt = stmt.order_by(PunchRecord.punch_time_utc.desc()).offset(offset).limit(limit)
    return list(db.scalars(stmt).all())


@router.post("/punches/{punch_id}/retry", response_model=MessageOut)
def retry_punch(
    punch_id: str,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> MessageOut:
    punch = db.get(PunchRecord, punch_id)
    if punch is None or punch.tenant_id != principal.tenant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Punch not found")
    punch.process_state = PunchState.pending.value
    punch.attempts = 0
    punch.error_message = None
    db.commit()
    return MessageOut(message="Punch queued for the next sync")


@router.get("/mappings", response_model=list[MappingOut])
def list_mappings(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, le=1000),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[EmployeeMapping]:
    stmt = select(EmployeeMapping).where(EmployeeMapping.tenant_id == principal.tenant.id)
    if status_filter:
        stmt = stmt.where(EmployeeMapping.status == status_filter)
    return list(
        db.scalars(
            stmt.order_by(EmployeeMapping.status, EmployeeMapping.emp_code).limit(limit)
        ).all()
    )


@router.patch("/mappings/{mapping_id}", response_model=MappingOut)
def update_mapping(
    mapping_id: str,
    payload: MappingUpdate,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> EmployeeMapping:
    mapping = db.get(EmployeeMapping, mapping_id)
    if mapping is None or mapping.tenant_id != principal.tenant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mapping not found")

    data = payload.model_dump(exclude_unset=True)

    if data.get("odoo_employee_id"):
        # One Odoo employee cannot hold two badges: their attendance would split
        # across two mapping rows and double-count on every report.
        clash = db.scalars(
            select(EmployeeMapping).where(
                EmployeeMapping.tenant_id == principal.tenant.id,
                EmployeeMapping.odoo_employee_id == data["odoo_employee_id"],
                EmployeeMapping.status == MappingStatus.mapped.value,
                EmployeeMapping.id != mapping.id,
            )
        ).first()
        if clash is not None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"That Odoo employee is already matched to badge {clash.emp_code}. "
                "Unmap it first, or set a separate badge on the Odoo record.",
            )
        mapping.odoo_employee_id = data["odoo_employee_id"]
        mapping.status = MappingStatus.mapped.value
        mapping.match_method = "manual"
        mapping.match_note = None

        # Give previously-unmapped punches another chance on the next run.
        db.query(PunchRecord).filter(
            PunchRecord.tenant_id == principal.tenant.id,
            PunchRecord.emp_code == mapping.emp_code,
            PunchRecord.process_state == PunchState.unmapped.value,
        ).update({"process_state": PunchState.pending.value})

    if "status" in data and not data.get("odoo_employee_id"):
        mapping.status = data["status"]

    audit(db, principal, "mapping.update", mapping.emp_code, str(data), request)
    db.commit()
    db.refresh(mapping)
    return mapping


@router.get("/attendance", response_model=list[AttendanceOut])
def list_attendance(
    date_from: str | None = None,
    date_to: str | None = None,
    emp_code: str | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AttendanceRecord]:
    stmt = select(AttendanceRecord).where(AttendanceRecord.tenant_id == principal.tenant.id)
    if date_from:
        stmt = stmt.where(AttendanceRecord.shift_date >= date_from)
    if date_to:
        stmt = stmt.where(AttendanceRecord.shift_date <= date_to)
    if emp_code:
        stmt = stmt.where(AttendanceRecord.emp_code == emp_code)
    stmt = stmt.order_by(AttendanceRecord.check_in.desc()).offset(offset).limit(limit)
    return list(db.scalars(stmt).all())


# ===========================================================================
# Dashboard
# ===========================================================================
@router.get("/dashboard", response_model=DashboardOut)
def dashboard(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> DashboardOut:
    tenant = principal.tenant
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )

    def count(**filters) -> int:
        stmt = select(func.count(PunchRecord.id)).where(PunchRecord.tenant_id == tenant.id)
        if "state" in filters:
            stmt = stmt.where(PunchRecord.process_state == filters["state"])
        if filters.get("today"):
            stmt = stmt.where(PunchRecord.punch_time_utc >= midnight)
        return db.scalar(stmt) or 0

    last_run = db.scalars(
        select(SyncRun)
        .where(SyncRun.tenant_id == tenant.id)
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    ).first()

    odoo_conn = db.scalars(
        select(OdooConnection).where(OdooConnection.tenant_id == tenant.id).limit(1)
    ).first()
    source = db.scalars(
        select(DeviceSource).where(DeviceSource.tenant_id == tenant.id).limit(1)
    ).first()

    return DashboardOut(
        tenant=TenantOut.model_validate(tenant),
        punches_today=count(today=True),
        punches_pending=count(state=PunchState.pending.value),
        punches_error=count(state=PunchState.error.value),
        unmapped_employees=db.scalar(
            select(func.count(EmployeeMapping.id)).where(
                EmployeeMapping.tenant_id == tenant.id,
                EmployeeMapping.status.in_(
                    [MappingStatus.unmapped.value, MappingStatus.ambiguous.value]
                ),
            )
        ) or 0,
        last_run=SyncRunOut.model_validate(last_run) if last_run else None,
        connection_health={
            "odoo": odoo_conn.status if odoo_conn else "missing",
            "source": source.status if source else "missing",
        },
    )
