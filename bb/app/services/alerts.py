"""What is wrong with this account right now, in words a customer can act on.

Computed on demand from state BioBridge already keeps — connection health, the
sync history, the punch ledger, terminal heartbeats — so an alert disappears the
moment its cause is fixed and there is nothing to acknowledge or clean up.
Nothing here is stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AdmsDevice, ConnectionStatus, Device, DeviceSource, EmployeeMapping, MappingStatus,
    OdooConnection, PunchRecord, PunchState, SyncRun, Tenant,
)
from app.services.scheduling import effective_interval, next_run_at, renewal_warning
from app.services.timeutils import ensure_aware

#: Punches stop being retried by the sync engine after this many failed tries
#: (see SyncEngine's ``attempts < 5``), so they sit until someone presses Retry.
MAX_ATTEMPTS = 5
#: A push terminal calls in every minute or so; this long silent means trouble.
TERMINAL_SILENT_MINUTES = 30
#: A sync is "overdue" once it has missed this many intervals (and 30 minutes).
OVERDUE_INTERVALS = 3

BAD, WARN = "bad", "warn"


def _alert(key: str, severity: str, title: str, detail: str, href: str | None = None,
           action: str | None = None) -> dict:
    return {"key": key, "severity": severity, "title": title, "detail": detail,
            "href": href, "action": action}


def _ago(moment: datetime, now: datetime) -> str:
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    if minutes < 60 * 48:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} days"


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def tenant_alerts(db: Session, tenant: Tenant, *, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out: list[dict] = []

    if not tenant.syncable:
        out.append(_alert(
            "account-stopped", BAD, "Syncing is stopped for this account",
            "New punches are not being collected. Your records are unchanged.",
            "#/settings/billing", "See plan"))

    warning = renewal_warning(tenant, now=now) if tenant.syncable else None
    trial = tenant.status == "trialing"
    if warning and (trial or tenant.plan_id):
        days = warning["days_left"]
        subject = "free trial" if trial else "subscription"
        out.append(_alert(
            "renewal", BAD if warning["urgent"] else WARN,
            f"Your {subject} ends today" if days <= 0 else f"Your {subject} ends in {_plural(days, 'day', 'days')}",
            "Syncing stops automatically when it does. Nothing already recorded is affected — only the "
            "collection of new punches would stop. " + ("Choose a plan to continue." if trial else "Contact support to renew."),
            "#/settings/billing" if trial else None, "Choose a plan" if trial else None))

    odoo = db.scalars(select(OdooConnection).where(
        OdooConnection.tenant_id == tenant.id, OdooConnection.is_active.is_(True)).limit(1)).first()
    sources = list(db.scalars(select(DeviceSource).where(
        DeviceSource.tenant_id == tenant.id, DeviceSource.is_active.is_(True))).all())

    connection_alert = False
    if odoo is not None and odoo.status in (ConnectionStatus.failed.value, ConnectionStatus.degraded.value):
        connection_alert = True
        failed = odoo.status == ConnectionStatus.failed.value
        out.append(_alert(
            "odoo-connection", BAD,
            "BioBridge can't reach Odoo" if failed else "The Odoo connection is unstable",
            _sentence(odoo.status_message or "Odoo did not accept the last request.")
            + " Punches are kept and will be sent once it works. Check the URL, database "
              "and API key — an expired or revoked key is the usual cause.",
            "#/settings/odoo", "Fix Odoo connection"))
    for source in sources:
        if source.status in (ConnectionStatus.failed.value, ConnectionStatus.degraded.value):
            connection_alert = True
            out.append(_alert(
                f"source-{source.id}", BAD,
                f"“{source.name}” isn't answering"
                if source.status == ConnectionStatus.failed.value else f"“{source.name}” is unstable",
                _sentence(source.status_message or "The last attempt to read punches failed.")
                + " New punches from it are not arriving.",
                "#/settings/biometric", "Check connection"))

    # Not syncing when it should be.
    last = db.scalars(select(SyncRun).where(SyncRun.tenant_id == tenant.id)
                      .order_by(SyncRun.started_at.desc()).limit(1)).first()
    if odoo is not None and sources and next_run_at(db, tenant, now=now) is not None and last is not None:
        started = ensure_aware(last.started_at)
        limit = max(OVERDUE_INTERVALS * effective_interval(tenant), 30)
        if started and now - started > timedelta(minutes=limit):
            out.append(_alert(
                "sync-overdue", BAD, "Automatic sync has stopped running",
                f"The last sync started {_ago(started, now)} ago, but one is due every "
                f"{effective_interval(tenant)} min. Attendance in Odoo is going stale.",
                "#/", "Sync now"))
        elif last.status == "failed" and not connection_alert:
            out.append(_alert(
                "sync-failed", WARN, "The last sync failed",
                (last.error_message or "See Activity for the log.")[:300], "#/activity", "Open Activity"))

    # Punches.
    counts = dict(db.execute(
        select(PunchRecord.process_state, func.count()).where(PunchRecord.tenant_id == tenant.id,
        PunchRecord.process_state.in_([PunchState.error.value, PunchState.held.value])).group_by(PunchRecord.process_state)
    ).all())
    errors = counts.get(PunchState.error.value, 0)
    if errors:
        stuck = db.scalar(select(func.count()).where(
            PunchRecord.tenant_id == tenant.id, PunchRecord.process_state == PunchState.error.value,
            PunchRecord.attempts >= MAX_ATTEMPTS)) or 0
        out.append(_alert(
            "punch-errors", BAD if stuck else WARN,
            f"{_plural(errors, 'punch', 'punches')} failed to reach Odoo",
            (f"{stuck} stopped retrying and need a manual Retry. " if stuck else "BioBridge is still retrying. ")
            + "Each one shows the reason Odoo gave.",
            "#/activity?state=error", "Review errors"))
    held = counts.get(PunchState.held.value, 0)
    if held:
        out.append(_alert(
            "punch-held", WARN, f"{_plural(held, 'punch is', 'punches are')} waiting on your plan",
            "They come from a terminal beyond your plan's device limit and go to Odoo once your plan covers it.",
            "#/settings/billing/choose", "Upgrade plan"))
    unmapped = db.scalar(select(func.count()).select_from(EmployeeMapping).where(
        EmployeeMapping.tenant_id == tenant.id,
        EmployeeMapping.status.in_([MappingStatus.unmapped.value, MappingStatus.ambiguous.value]))) or 0
    if unmapped:
        out.append(_alert(
            "unmapped", WARN, f"{_plural(unmapped, 'badge', 'badges')} waiting to be matched",
            "Their attendance is held until each badge is matched to an Odoo employee.",
            "#/settings/odoo?show=unmapped", "Match badges"))

    # Terminals.
    source_ids = [s.id for s in sources]
    if source_ids:
        cutoff = now - timedelta(minutes=TERMINAL_SILENT_MINUTES)
        for dev in db.scalars(select(AdmsDevice).where(AdmsDevice.source_id.in_(source_ids))).all():
            seen = ensure_aware(dev.last_seen_at)
            if seen is not None and seen < cutoff:
                label = _terminal_label(db, tenant.id, dev.serial_number) or dev.serial_number
                out.append(_alert(
                    f"terminal-offline-{dev.serial_number}", WARN, f"Terminal {label} is offline",
                    f"It last contacted BioBridge {_ago(seen, now)} ago. Check its power and network — "
                    "punches made meanwhile stay on the terminal and arrive when it reconnects.",
                    "#/terminals", "View terminals"))
        missing = db.scalars(select(Device).where(
            Device.tenant_id == tenant.id, Device.missing_since.is_not(None), Device.is_enabled.is_(True))).all()
        if missing:
            names = ", ".join(d.alias or d.serial_number for d in missing[:3]) + ("…" if len(missing) > 3 else "")
            out.append(_alert(
                "terminals-missing", WARN, f"{_plural(len(missing), 'terminal is', 'terminals are')} no longer reported",
                f"{names} — its platform stopped listing it. It may have been removed or renamed there.",
                "#/terminals", "View terminals"))

    out.sort(key=lambda a: 0 if a["severity"] == BAD else 1)
    return out


def _terminal_label(db: Session, tenant_id: str, serial: str) -> str | None:
    dev = db.scalars(select(Device).where(Device.tenant_id == tenant_id, Device.serial_number == serial)).first()
    return (dev.alias if dev and dev.alias else None)
