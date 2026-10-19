"""A fixed platform-staff login, from the server's settings.

``tools/grant_admin.py`` is the way to make staff, but it needs a shell (or a
database login) on the server. A host that offers neither leaves nobody able to
open the staff console. ``STAFF_EMAIL`` and ``STAFF_PASSWORD`` close that gap
without opening a hole: they are read from the process environment, so the
privilege costs exactly what control of the server's settings costs — the same
bar as a database login — and no HTTP route can set it.

At start-up, on every replica, and idempotently:

* neither set                         -> nothing happens;
* no such user                        -> a staff user with no workspace is created;
* a staff user with no workspace      -> its password is made to match the setting
  (the setting is the source of truth, so changing it and restarting rotates the
  password, and every session that account held is ended);
* an existing *customer* account      -> left alone, with a warning. A setting
  must not be able to take over a customer's login; promote that one with
  ``grant_admin.py`` instead.

Removing the settings does not delete the account or its access — revoke it with
``grant_admin.py --revoke`` or disable the user.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.models import User, UserRole, UserSession

log = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 12


def ensure_static_staff(db: Session) -> str:
    """Apply STAFF_EMAIL / STAFF_PASSWORD. Returns what was done:
    ``off``, ``created``, ``updated``, ``unchanged``, or ``skipped``."""
    email = (settings.staff_email or "").strip().lower()
    password = settings.staff_password or ""
    if not email and not password:
        return "off"
    if not email or not password:
        log.warning("STAFF_EMAIL and STAFF_PASSWORD must both be set; no staff login was configured")
        return "skipped"
    if "@" not in email:
        log.error("STAFF_EMAIL %r is not an email address; no staff login was configured", email)
        return "skipped"
    if len(password) < MIN_PASSWORD_LENGTH:
        log.error("STAFF_PASSWORD is shorter than %d characters; no staff login was configured",
                  MIN_PASSWORD_LENGTH)
        return "skipped"

    user = db.scalars(select(User).where(User.email == email)).first()
    if user is None:
        db.add(User(
            tenant_id=None,                       # staff are not a customer
            email=email,
            full_name="Platform staff",
            hashed_password=hash_password(password),
            role=UserRole.owner.value,            # unused without a tenant
            is_platform_admin=True,
        ))
        try:
            db.commit()
        except IntegrityError:                    # another replica created it first
            db.rollback()
            return "unchanged"
        log.info("Created platform staff login %s from STAFF_EMAIL", email)
        return "created"

    if user.tenant_id is not None:
        log.warning(
            "STAFF_EMAIL %s belongs to a customer workspace, so the setting was ignored. "
            "Promote that account with tools/grant_admin.py, or use another address.", email)
        return "skipped"

    changed = False
    if not user.is_platform_admin:
        user.is_platform_admin = True
        changed = True
    if not user.is_active:
        user.is_active = True
        changed = True
    if not verify_password(password, user.hashed_password):
        user.hashed_password = hash_password(password)
        # A rotated password ends every session the old one opened.
        db.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        changed = True
    if user.failed_login_count or user.locked_until:
        user.failed_login_count = 0           # a restart clears a lock-out of this login
        user.locked_until = None
        changed = True
    if not changed:
        return "unchanged"
    db.commit()
    log.info("Updated platform staff login %s from STAFF_PASSWORD", email)
    return "updated"
