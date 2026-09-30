"""ZKTeco ADMS ("cloud server" / PUSH) protocol — the terminal calls us.

A terminal with *Cloud Server Setting* pointed at BioBridge makes plain HTTP
requests to fixed paths under ``/iclock/`` (see app/api/adms.py), carrying
its serial number as ``SN``:

``GET  /iclock/cdata?SN=…&options=all``
    Handshake at power-on / reconnect. We answer with its settings, including
    where to resume its logs from (``ATTLOGStamp``).
``POST /iclock/cdata?SN=…&table=ATTLOG&Stamp=…``
    Punches, one per line: ``PIN<TAB>YYYY-MM-DD HH:MM:SS<TAB>status<TAB>verify…``.
``POST /iclock/cdata?SN=…&table=OPERLOG`` / ``table=USERINFO``
    Operation log / user list; ``USER PIN=…<TAB>Name=…`` lines tell us who is
    enrolled on it.
``GET  /iclock/getrequest?SN=…[&INFO=fw,users,fps,logs,ip,…]``
    Heartbeat every ``Delay`` seconds. We answer ``OK`` or queued commands,
    one per line, ``C:<id>:<command>``.
``POST /iclock/devicecmd?SN=…``
    The results of those commands: ``ID=<id>&Return=<code>&CMD=<kind>``.

Everything here takes a session and leaves committing to the caller. This is
reverse-engineered, vendor-published-but-informal protocol ("Attendance PUSH
Communication Protocol"); firmware differs in details, so parsing is lenient
and anything unrecognised is ignored rather than rejected.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.integrations.base import PunchEvent
from app.models import (
    AdmsCommand,
    AdmsDevice,
    Device,
    DeviceSource,
    PunchRecord,
    Tenant,
    TenantStatus,
)
from app.services.timeutils import local_to_utc, utcnow_naive

log = logging.getLogger(__name__)

PROVIDER_SLUG = "zk_adms"
#: A terminal heard from within this long counts as connected.
ONLINE_WINDOW = timedelta(minutes=5)
#: Seconds between heartbeats we ask terminals for. Lower = commands land
#: faster, higher = fewer requests per terminal.
HEARTBEAT_SECONDS = 30

# Punch status (the "state" key a person pressed, or the device's auto state):
# 0 check-in, 1 check-out, 2 break-out, 3 break-in, 4 overtime-in, 5 overtime-out.
STATE_IN = {0, 3, 4}
STATE_OUT = {1, 2, 5}
# Verify mode codes (varies by firmware; the common ones).
VERIFY = {0: "pw", 1: "finger", 2: "card", 3: "pw", 4: "card", 9: "card", 15: "face", 25: "palm"}

_SERIAL_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def normalise_serial(value: str | None) -> str:
    return (value or "").strip().upper()


def valid_serial(value: str) -> bool:
    return bool(_SERIAL_RE.match(value or ""))


def server_address() -> tuple[str, int]:
    """What a customer types into the terminal's Cloud Server Setting."""
    from urllib.parse import urlparse

    host = settings.adms_server_host or urlparse(settings.public_base_url).hostname or "localhost"
    port = settings.adms_server_port or urlparse(settings.public_base_url).port or 80
    return host, int(port)


# --- devices -----------------------------------------------------------------
def get_device(db: Session, serial: str) -> AdmsDevice | None:
    return db.scalars(select(AdmsDevice).where(AdmsDevice.serial_number == serial)).first()


def touch(db: Session, serial: str, ip: str | None) -> AdmsDevice:
    """Record that ``serial`` called in, creating its row on first contact."""
    device = get_device(db, serial)
    if device is None:
        device = AdmsDevice(serial_number=serial, users={})
        db.add(device)
    device.last_seen_at = datetime.now(timezone.utc)
    if ip:
        device.last_ip = ip[:64]
    return device


def is_online(device: AdmsDevice | None) -> bool:
    if device is None or device.last_seen_at is None:
        return False
    seen = device.last_seen_at
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - seen <= ONLINE_WINDOW


def claimed_source(db: Session, device: AdmsDevice) -> tuple[Tenant, DeviceSource] | None:
    """The tenant and connection a terminal belongs to, if it is claimed and
    that connection is live. Cancelled accounts are treated as unclaimed."""
    if not device.source_id:
        return None
    source = db.get(DeviceSource, device.source_id)
    if source is None or not source.is_active or source.provider != PROVIDER_SLUG:
        return None
    tenant = db.get(Tenant, source.tenant_id)
    if tenant is None or tenant.status == TenantStatus.cancelled.value:
        return None
    return tenant, source


def claim(db: Session, source: DeviceSource, serial: str) -> AdmsDevice:
    """Attach ``serial`` to ``source``. Raises ValueError if another account
    already holds it."""
    device = get_device(db, serial)
    if device is not None and device.tenant_id and device.tenant_id != source.tenant_id:
        raise ValueError(
            "This device is already connected to another BioBridge account. "
            "If it's yours, contact support to release it."
        )
    if device is None:
        device = AdmsDevice(serial_number=serial, users={})
        db.add(device)
    if device.source_id and device.source_id != source.id:
        other = db.get(DeviceSource, device.source_id)
        if other is not None and other.is_active and other.tenant_id == source.tenant_id:
            raise ValueError(f"This device is already connected here as '{other.name}'.")
    device.tenant_id = source.tenant_id
    device.source_id = source.id
    return device


def release(db: Session, source: DeviceSource) -> None:
    """Free every serial ``source`` holds (the connection is being removed)."""
    for device in db.scalars(select(AdmsDevice).where(AdmsDevice.source_id == source.id)).all():
        device.source_id = None
        device.tenant_id = None
        device.attlog_stamp = None  # a future owner gets the terminal's full log


# --- handshake ------------------------------------------------------------------
def handshake_reply(device: AdmsDevice, params: dict[str, str]) -> str:
    device.push_version = (params.get("pushver") or device.push_version or "")[:32] or None
    lines = [
        f"GET OPTION FROM: {device.serial_number}",
        f"ATTLOGStamp={device.attlog_stamp or 'None'}",
        f"OPERLOGStamp={device.operlog_stamp or 'None'}",
        "ATTPHOTOStamp=None",
        "ErrorDelay=60",
        f"Delay={HEARTBEAT_SECONDS}",
        "TransTimes=00:00;12:00",
        "TransInterval=1",
        "TransFlag=TransData AttLog OpLog EnrollUser ChgUser",
        "Realtime=1",
        "Encrypt=None",
        "ServerVer=2.4.1",
        "PushProtVer=2.4.1",
    ]
    return "\n".join(lines) + "\n"


def note_heartbeat(device: AdmsDevice, info: str | None) -> None:
    """``INFO=Ver 8.0.4-20190708,12,10,3345,192.168.1.201,10,7,15,11,111``:
    firmware, users, fingerprints, attendance records, device IP, …"""
    if not info:
        return
    parts = [p.strip() for p in info.split(",")]
    if parts and parts[0]:
        device.firmware = parts[0][:80]
    for index, attr in ((1, "user_count"), (3, "attlog_count")):
        if len(parts) > index and parts[index].isdigit():
            setattr(device, attr, int(parts[index]))


# --- uploads --------------------------------------------------------------------
def parse_attlog(text: str, serial: str) -> list[PunchEvent]:
    events: list[PunchEvent] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        pin = cols[0].strip()
        try:
            when = datetime.strptime(cols[1].strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if not pin:
            continue
        state = int(cols[2]) if len(cols) > 2 and cols[2].strip().lstrip("-").isdigit() else None
        verify = int(cols[3]) if len(cols) > 3 and cols[3].strip().isdigit() else None
        direction = True if state in STATE_IN else False if state in STATE_OUT else None
        events.append(PunchEvent(
            # No record id in this protocol: a person can't punch twice in the
            # same second on one terminal, so PIN + time identifies the punch
            # and makes the terminal's re-sends harmless.
            external_id=f"{serial}:{pin}:{when:%Y%m%d%H%M%S}",
            emp_code=pin,
            punch_time_local=when,
            direction=direction,
            terminal_sn=serial,
            verify_type=VERIFY.get(verify, str(verify) if verify is not None else None),
            raw={"punch_state": state if state is not None else "", "verify": verify,
                 "workcode": cols[4].strip() if len(cols) > 4 else "", "line": line[:200]},
        ))
    return events


def _kv(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in line.split("\t"):
        key, sep, value = part.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def parse_users(text: str) -> dict[str, str]:
    """PIN -> name from ``USER PIN=…<TAB>Name=…`` lines (OPERLOG / USERINFO)."""
    users: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("USER "):
            line = line[5:]
        elif not line.startswith("PIN="):
            continue
        fields = _kv(line)
        pin = fields.get("PIN")
        if pin:
            users[pin] = fields.get("Name", "")
    return users


def store_punches(db: Session, device: AdmsDevice, events: list[PunchEvent]) -> int:
    """Put a terminal's pushed punches into its tenant's ledger. Returns how
    many were new. Assumes the device is claimed (see claimed_source)."""
    from app.services.sync_engine import store_punch

    owner = claimed_source(db, device)
    if owner is None or not events:
        return 0
    tenant, source = owner
    tz = source.server_timezone or tenant.timezone
    floor = utcnow_naive() - timedelta(days=settings.backfill_limit_days)

    devices = {d.serial_number: d for d in db.scalars(
        select(Device).where(Device.source_id == source.id)).all()}
    local = devices.get(device.serial_number)
    if local is not None and not local.is_enabled:
        return 0  # the customer switched this terminal off in BioBridge

    ids = list(dict.fromkeys(e.external_id for e in events))
    seen: set[str] = set()
    for start in range(0, len(ids), 500):
        seen.update(db.scalars(select(PunchRecord.external_id).where(
            PunchRecord.tenant_id == tenant.id, PunchRecord.source_id == source.id,
            PunchRecord.external_id.in_(ids[start:start + 500]))).all())

    created = 0
    for event in events:
        if event.external_id in seen:
            continue
        seen.add(event.external_id)
        punch_utc = local_to_utc(event.punch_time_local, tz)
        if punch_utc < floor:
            continue
        event.terminal_alias = source.name
        store_punch(db, tenant, source, devices, event, punch_utc)
        created += 1
    if created:
        db.flush()
    return created


def remember_users(device: AdmsDevice, users: dict[str, str]) -> None:
    if not users:
        return
    merged = dict(device.users or {})
    merged.update(users)
    device.users = merged  # reassign so the JSON change is detected


# --- commands -------------------------------------------------------------------
def queue(db: Session, serial: str, command: str) -> AdmsCommand:
    row = AdmsCommand(serial_number=serial, command=command)
    db.add(row)
    return row


def queue_user(db: Session, device: AdmsDevice, pin: str, name: str) -> AdmsCommand:
    """Create (or update) a user on the terminal. Identity only — fingerprints
    and faces are enrolled at the terminal itself."""
    clean = re.sub(r"[\t\r\n]", " ", name or "").strip()[:24]
    row = queue(db, device.serial_number,
                f"DATA UPDATE USERINFO PIN={pin}\tName={clean}\tPri=0\tPasswd=\tCard=\tGrp=1\tTZ=0000000000000000\tVerify=0")
    remember_users(device, {pin: clean})
    return row


def pending_commands(db: Session, serial: str, limit: int = 20) -> list[AdmsCommand]:
    """Queued commands to hand out, plus ones sent over 10 minutes ago with no
    answer (the terminal may have rebooted before running them)."""
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    rows = db.scalars(select(AdmsCommand).where(
        AdmsCommand.serial_number == serial,
        AdmsCommand.status.in_(("queued", "sent")),
    ).order_by(AdmsCommand.id).limit(limit * 3)).all()
    out = []
    for row in rows:
        sent = row.sent_at
        if sent is not None and sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        if row.status == "queued" or (sent is not None and sent < stale):
            out.append(row)
        if len(out) >= limit:
            break
    return out


def command_reply(db: Session, serial: str) -> str:
    rows = pending_commands(db, serial)
    if not rows:
        return "OK"
    now = datetime.now(timezone.utc)
    for row in rows:
        row.status = "sent"
        row.sent_at = now
    return "\n".join(f"C:{row.id}:{row.command}" for row in rows) + "\n"


def apply_results(db: Session, device: AdmsDevice, body: str) -> int:
    """``ID=12&Return=0&CMD=DATA`` lines. Return 0 (or positive) is success.
    An INFO result also carries ``~DeviceName=…`` style lines."""
    done = 0
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("~DeviceName=") or line.startswith("DeviceName="):
            device.model = line.split("=", 1)[1].strip()[:80] or device.model
            continue
        if line.startswith("FWVersion="):
            device.firmware = line.split("=", 1)[1].strip()[:80] or device.firmware
            continue
        if not line.startswith("ID="):
            continue
        fields = dict(part.partition("=")[::2] for part in line.split("&"))
        try:
            row = db.get(AdmsCommand, int(fields.get("ID", "")))
        except ValueError:
            continue
        if row is None or row.serial_number != device.serial_number:
            continue
        code = (fields.get("Return") or "").strip()
        row.return_code = code[:16]
        row.status = "done" if code.lstrip("-").isdigit() and int(code) >= 0 else "failed"
        row.done_at = datetime.now(timezone.utc)
        done += 1
    return done


def ask_for_details(db: Session, device: AdmsDevice) -> None:
    """On first claim: ask the terminal for its user list and identity."""
    queue(db, device.serial_number, "DATA QUERY USERINFO")
    queue(db, device.serial_number, "INFO")


def describe(device: AdmsDevice | None) -> dict[str, Any]:
    if device is None:
        return {}
    return {k: v for k, v in {
        "serial_number": device.serial_number, "model": device.model,
        "firmware": device.firmware, "ip_address": device.last_ip,
        "users_on_device": device.user_count, "last_seen_at":
            device.last_seen_at.isoformat() if device.last_seen_at else None,
    }.items() if v is not None}
