"""Every timezone conversion in the system, and nowhere else.

The contract at the two edges:

* **Device platforms** report punch times as a naive local wall-clock string in
  the *server's* zone, with no offset. "2026-03-05 08:59:12" and nothing more.
* **Odoo** stores ``Datetime`` fields as naive **UTC**.

Getting this wrong does not raise; it produces attendance that is plausible and
several hours out. Keeping the conversion in one module means there is one place
to read, one place to test, and no second implementation to drift.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_zone(name: str | None) -> ZoneInfo:
    """Resolve a zone name, falling back to UTC rather than raising.

    A bad zone on one source must not take down a run for every other source.
    """
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_to_utc(naive_local: datetime, tz_name: str) -> datetime:
    """Naive local wall-clock -> naive UTC, which is what Odoo stores."""
    zone = get_zone(tz_name)
    return naive_local.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_local(naive_utc: datetime, tz_name: str) -> datetime:
    """Naive UTC -> naive local wall-clock, which is what device APIs filter on."""
    return (
        naive_utc.replace(tzinfo=timezone.utc)
        .astimezone(get_zone(tz_name))
        .replace(tzinfo=None)
    )


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_aware(value: datetime | None) -> datetime | None:
    """Guarantee a UTC-aware datetime.

    ``DateTime(timezone=True)`` round-trips as aware on PostgreSQL but naive on
    SQLite, so a bare ``value < now()`` comparison raises on one backend and
    quietly works on the other. Every comparison against a stored timestamp goes
    through here.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def is_past(value: datetime | None) -> bool:
    """True when a stored timestamp is in the past. Missing never expires."""
    aware = ensure_aware(value)
    return aware is not None and aware < datetime.now(timezone.utc)


def shift_day(moment: datetime, boundary_hour: int) -> date:
    """The *shift* day a punch belongs to.

    With ``boundary_hour=4``, a punch at 02:00 on the 6th belongs to the 5th's
    shift. A night-shift site sets the boundary after the shift ends — noon, not
    the small hours.
    """
    return (moment - timedelta(hours=boundary_hour)).date()
