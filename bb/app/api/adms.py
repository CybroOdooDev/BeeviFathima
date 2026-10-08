"""The ZKTeco ADMS receiver: the paths a push terminal calls.

Mounted at the site root (terminals are configured with only a host and port
and always use ``/iclock/…``), plain-text in and out. Firmware variously
appends ``.aspx`` to the paths, so both spellings are served.

There is no authentication in this protocol beyond the serial number. What
keeps it safe enough:

* a terminal's punches are stored only once a tenant has claimed its serial
  in Settings (an unknown serial is recorded as "heard from" and nothing else);
* uploads from an unclaimed serial are refused with an error, so the terminal
  keeps them and re-sends after it is claimed — nothing is lost by the wait;
* one serial belongs to one account, platform-wide;
* request bodies are capped (ADMS_MAX_BODY_BYTES).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.services import adms

log = logging.getLogger(__name__)
router = APIRouter(prefix="/iclock", tags=["adms"], include_in_schema=False)


def _text(body: str, status_code: int = 200) -> PlainTextResponse:
    return PlainTextResponse(body, status_code=status_code)


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _body(request: Request) -> str | None:
    raw = await request.body()
    if len(raw) > settings.adms_max_body_bytes:
        return None
    return raw.decode("utf-8", errors="replace")


def _serial(request: Request) -> str:
    return adms.normalise_serial(request.query_params.get("SN"))


@router.get("/cdata")
@router.get("/cdata.aspx")
def handshake(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    serial = _serial(request)
    if not adms.valid_serial(serial):
        return _text("ERROR: bad SN", 400)
    device = adms.touch(db, serial, _ip(request))
    reply = adms.handshake_reply(device, dict(request.query_params))
    db.commit()
    return _text(reply)


@router.post("/cdata")
@router.post("/cdata.aspx")
async def upload(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    serial = _serial(request)
    if not adms.valid_serial(serial):
        return _text("ERROR: bad SN", 400)
    body = await _body(request)
    if body is None:
        return _text("ERROR: too large", 413)
    table = (request.query_params.get("table") or "").upper()
    stamp = (request.query_params.get("Stamp") or "")[:32] or None

    device = adms.touch(db, serial, _ip(request))
    owner = adms.claimed_source(db, device)
    if owner is None:
        # Not claimed (yet): refuse the data so the terminal keeps it.
        db.commit()
        log.info("ADMS upload from unclaimed terminal %s (%s) refused", serial, table)
        return _text("ERROR: device not registered", 403)

    lines = len([l for l in body.splitlines() if l.strip()])
    if table == "ATTLOG":
        created = adms.store_punches(db, device, adms.parse_attlog(body, serial))
        if stamp:
            device.attlog_stamp = stamp
        log.info("ADMS %s: %d punch line(s), %d new", serial, lines, created)
    elif table in ("OPERLOG", "USERINFO", "USER"):
        before = set(device.users or {})
        adms.remember_users(device, adms.parse_users(body))
        if set(device.users or {}) - before:
            # New users on the device: link any that match an Odoo employee
            # now, rather than waiting for their first punch or the next sync.
            from app.services.roster_import import auto_import

            db.flush()
            auto_import(db, owner[0])
        if stamp and table == "OPERLOG":
            device.operlog_stamp = stamp
    # Anything else (ATTPHOTO, BIODATA, options…) is acknowledged and ignored.
    db.commit()
    return _text(f"OK: {lines}")


@router.get("/getrequest")
@router.get("/getrequest.aspx")
def heartbeat(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    serial = _serial(request)
    if not adms.valid_serial(serial):
        return _text("ERROR: bad SN", 400)
    device = adms.touch(db, serial, _ip(request))
    adms.note_heartbeat(device, request.query_params.get("INFO"))
    reply = adms.command_reply(db, serial) if adms.claimed_source(db, device) else "OK"
    db.commit()
    return _text(reply)


@router.post("/devicecmd")
@router.post("/devicecmd.aspx")
async def command_result(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    serial = _serial(request)
    if not adms.valid_serial(serial):
        return _text("ERROR: bad SN", 400)
    body = await _body(request)
    if body is None:
        return _text("ERROR: too large", 413)
    device = adms.touch(db, serial, _ip(request))
    adms.apply_results(db, device, body)
    db.commit()
    return _text("OK")


@router.get("/ping")
@router.get("/ping.aspx")
def ping(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    serial = _serial(request)
    if adms.valid_serial(serial):
        adms.touch(db, serial, _ip(request))
        db.commit()
    return _text("OK")
