"""The plan's device allowance: which of an account's terminals are covered.

Counted over every terminal the account has ever added — imported from a
platform, registered by a standalone connection test, or first seen on an
incoming punch. The oldest fill the allowance, so adding a terminal never
pushes out one that was already working (terminals recorded in the same
instant — one import of several — are ordered by serial number, so the
choice is stable and explainable rather than arbitrary). Terminals beyond it are
*over the limit*: recorded, visible, their punches kept in the ledger but held
back from Odoo (PunchState.held) until the plan covers them.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Device, Tenant


def over_limit_device_ids(db: Session, tenant: Tenant) -> set[str]:
    """Ids of this account's terminals beyond its plan's allowance (empty
    when the plan sets no device limit, or none is assigned)."""
    cap = tenant.plan_max_devices
    if cap is None:
        return set()
    ordered = db.scalars(
        select(Device.id)
        .where(Device.tenant_id == tenant.id)
        .order_by(Device.created_at, Device.serial_number, Device.id)
    ).all()
    return set(ordered[cap:])
