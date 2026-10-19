"""Which Odoo companies a connection works across, and what that rules out.

On an Odoo with several companies switched on, BioBridge cannot tell which
company a biometric terminal belongs to without being told or working it out
(see ``infer_device_companies``), so anything that would push the whole Odoo
roster onto a device — creating employees on it — is not allowed: a terminal
in Company A's office would be handed Company B's people.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.odoo import OdooError
from app.models import (
    Device,
    DeviceSource,
    EmployeeMapping,
    MappingStatus,
    OdooConnection,
    PunchRecord,
    Tenant,
)

log = logging.getLogger("biobridge.company_scope")

MULTI_COMPANY_PROVISION_MESSAGE = (
    "This Odoo has more than one company switched on, so employees are not created "
    "on biometric devices automatically: BioBridge cannot tell which company's "
    "people belong on which device. Add them on the device yourself; they are "
    "matched by badge as soon as they punch."
)


def enabled_company_ids(odoo) -> list[int] | None:
    """Every company this connection may touch, or None when that can't be read."""
    try:
        scope = odoo.company_scope()
        if scope is not None:
            return list(scope)
        return [c["id"] for c in odoo.list_companies()]
    except OdooError:
        return None


def is_multi_company(odoo) -> bool:
    """True when more than one company is on. Unknown (Odoo unreachable) counts
    as False, so a hiccup never blocks something that is otherwise allowed."""
    ids = enabled_company_ids(odoo)
    return ids is not None and len(ids) > 1


def tenant_is_multi_company(db: Session, tenant: Tenant, build_client) -> bool:
    """The same question, asked outside a sync: builds a client for the
    tenant's active Odoo connection. Any failure answers False."""
    conn = db.scalars(
        select(OdooConnection)
        .where(OdooConnection.tenant_id == tenant.id, OdooConnection.is_active.is_(True))
        .limit(1)
    ).first()
    if conn is None:
        return False
    try:
        client = build_client(tenant, conn)
        client.authenticate()
        return is_multi_company(client)
    except Exception:  # noqa: BLE001 — a probe must never fail the caller
        log.debug("could not tell whether Odoo has several companies", exc_info=True)
        return False


def infer_device_companies(db: Session, tenant: Tenant) -> list[str]:
    """Give each terminal without a company the company of the people who use it.

    A terminal is assigned when every employee who has punched on it *and* is
    matched to an Odoo employee belongs to the same company. A terminal used
    by employees of several companies is left unassigned (its punches are held
    for review) and reported, because guessing there is exactly the mistake
    scoping exists to prevent. A company set by hand, on the terminal or on its
    connection, is never overwritten. Returns a log line per change.
    """
    notes: list[str] = []
    sources = {
        s.id: s for s in db.scalars(select(DeviceSource).where(DeviceSource.tenant_id == tenant.id))
    }
    open_devices = [
        d for d in db.scalars(select(Device).where(Device.tenant_id == tenant.id))
        if d.company_id is None
        and (sources.get(d.source_id) is None or sources[d.source_id].default_company_id is None)
    ]
    if not open_devices:
        return notes
    by_key = {(d.source_id, d.serial_number): d for d in open_devices}

    rows = db.execute(
        select(
            PunchRecord.source_id, PunchRecord.terminal_sn,
            EmployeeMapping.odoo_company_id, EmployeeMapping.odoo_company_name,
        )
        .join(
            EmployeeMapping,
            (EmployeeMapping.tenant_id == PunchRecord.tenant_id)
            & (EmployeeMapping.emp_code == PunchRecord.emp_code),
        )
        .where(
            PunchRecord.tenant_id == tenant.id,
            PunchRecord.terminal_sn.is_not(None),
            EmployeeMapping.status == MappingStatus.mapped.value,
            EmployeeMapping.odoo_company_id.is_not(None),
        )
        .distinct()
    ).all()

    seen: dict[tuple[str, str], dict[int, str | None]] = {}
    for source_id, serial, company_id, company_name in rows:
        if (source_id, serial) in by_key:
            seen.setdefault((source_id, serial), {})[company_id] = company_name

    for key, companies in seen.items():
        device = by_key[key]
        label = device.alias or device.serial_number
        if len(companies) == 1:
            (company_id, company_name), = companies.items()
            device.company_id = company_id
            device.company_name = company_name
            notes.append(f"Terminal {label} assigned to {company_name or f'company {company_id}'} "
                         "(its matched employees all belong to it)")
        else:
            names = ", ".join(sorted(n or f"company {i}" for i, n in companies.items()))
            notes.append(f"Terminal {label} is used by employees of several companies ({names}); "
                         "choose its company on the Terminals page")
    return notes


def terminal_company_id(db: Session, device) -> int | None:
    """The Odoo company a terminal belongs to: its own, else its connection's
    default. The one place this is decided, so every path that registers a
    terminal in Odoo stamps the same company."""
    if device.company_id is not None:
        return device.company_id
    source = db.get(DeviceSource, device.source_id) if device.source_id else None
    return source.default_company_id if source is not None else None
