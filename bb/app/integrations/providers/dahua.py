"""Dahua access-control and attendance terminals over the Dahua HTTP API.

Dahua face and card terminals (ASI/ASA series, access controllers…) serve a
CGI interface on the device itself, ``/cgi-bin/…``, with HTTP Digest auth
(some older firmware only offers Basic; both are handled). BioBridge reaches
the terminal directly — same network, VPN or forwarded port — like a
standalone Hikvision or COSEC terminal. Replies are plain ``key=value``
lines, not JSON.

From Dahua's "Access Control Products Integration Instruction" and its HTTP
API guide:

``GET /cgi-bin/magicBox.cgi?action=getSystemInfo``
    ``serialNumber=…`` / ``deviceType=…`` — the connection test, and the
    terminal's serial number and model.
``GET /cgi-bin/recordFinder.cgi?action=find&name=AccessControlCardRec&StartTime=&EndTime=&count=``
    The swipe/face/fingerprint log: ``totalCount=``, ``found=``, then
    ``records[n].RecNo / CreateTime / UserID / CardNo / Method / Status /
    Door / ReaderID / Type``. ``CreateTime`` is UTC (epoch seconds); ``Method``
    0 password, 1 card, 2 password+card, 6 fingerprint, 15 face; ``Status``
    1 success. Up to ``count`` records (default 1024) come back; if
    ``totalCount`` exceeds ``found`` the next query starts at the last
    record's ``CreateTime`` and the overlap is dropped by ``RecNo``.

Which records are punches
--------------------------
Successful (``Status=1``) records that carry a ``UserID`` — the person's id on
the device, which is what matches the Odoo employee. Failed attempts and cards
that belong to no user are skipped. Direction comes from ``AttendanceState``
(CheckIn / CheckOut / BreakIn / BreakOut / OvertimeIn / OvertimeOut) on
attendance terminals, else ``Type`` (Entry / Exit); when neither is present it
is left to the pairing logic.

Built from Dahua's published documents; tested against a simulated terminal
(tests/test_dahua.py), not a physical one. Firmware varies — pilot first. Time
handling is the thing to check: the query is sent as epoch seconds and, if a
firmware refuses that, as the device's local ``YYYY-MM-DD HH:MM:SS``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.integrations.base import (
    AttendanceProvider,
    Capability,
    ConnectionInfo,
    ProviderError,
    PunchEvent,
    SourceConfig,
    TerminalRecord,
    register,
)

log = logging.getLogger(__name__)

#: Tests point this at an httpx.MockTransport standing in for a terminal.
TRANSPORT: httpx.BaseTransport | None = None

PAGE_SIZE = 500
MAX_PAGES = 200
MAX_DAYS = 45
_IN = {"checkin", "entry", "in", "breakin", "overtimein", "mealin"}
_OUT = {"checkout", "exit", "out", "breakout", "overtimeout", "mealout"}
METHODS = {"0": "password", "1": "card", "2": "password+card", "6": "fingerprint", "15": "face"}


class DahuaError(ProviderError):
    pass


def parse_kv(text: str) -> dict[str, str]:
    """``a=1`` lines → dict. Repeated keys keep the last; ``records[n].x`` stay as they are."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key:
            out[key.strip()] = value.strip()
    return out


def parse_records(text: str) -> tuple[int, list[dict[str, str]]]:
    """(totalCount, records) from a recordFinder reply."""
    kv = parse_kv(text)
    rows: dict[int, dict[str, str]] = {}
    for key, value in kv.items():
        m = re.fullmatch(r"records\[(\d+)\]\.(.+)", key)
        if m:
            rows.setdefault(int(m.group(1)), {})[m.group(2)] = value
    try:
        total = int(kv.get("totalCount", len(rows)))
    except ValueError:
        total = len(rows)
    return total, [rows[i] for i in sorted(rows)]


def parse_created(value: str, zone: ZoneInfo) -> datetime | None:
    """CreateTime: epoch seconds (UTC), or a ``YYYY-MM-DD HH:MM:SS`` / ISO string in the device's zone."""
    text = (value or "").strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, tz=timezone.utc)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def direction_of(row: dict[str, str]) -> bool | None:
    for key in ("AttendanceState", "Type"):
        state = re.sub(r"[^a-z]", "", (row.get(key) or "").lower())
        if state in _IN:
            return True
        if state in _OUT:
            return False
    return None


@register
class DahuaProvider(AttendanceProvider):
    slug = "dahua"
    label = "Dahua terminal"
    description = (
        "A Dahua face, fingerprint or card terminal (ASI/ASA series, access "
        "controllers…), reached directly over the network with its admin account."
    )
    capabilities = frozenset({Capability.READ_PUNCHES, Capability.LIST_TERMINALS})
    kinds = frozenset({"device"})
    config_fields = (
        {"name": "base_url", "label": "Device Address", "type": "text", "required": True,
         "help": "http://<device IP> — add :port if it isn't 80."},
        {"name": "username", "label": "Username", "type": "text", "required": True, "default": "admin",
         "help": "The device's admin account."},
        {"name": "password", "label": "Password", "type": "password", "required": True},
        {"name": "server_timezone", "label": "Device Timezone", "type": "timezone",
         "required": True, "default": "UTC", "help": "The zone the device's clock is set to."},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        base = (config.base_url or "").strip().rstrip("/")
        if base and "://" not in base:
            base = f"http://{base}"
        self.base = base
        self.host = urlparse(base).hostname or base
        self.tz = config.timezone or "UTC"
        self._client: httpx.Client | None = None
        self._basic = False           # this device only answers a Basic challenge
        self._epoch = True            # StartTime/EndTime as epoch seconds; False = local text
        self._serial: str | None = None

    # -- transport ------------------------------------------------------------
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base, verify=self.config.verify_ssl,
                                        timeout=settings.http_timeout_seconds, transport=TRANSPORT)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _auth(self) -> httpx.Auth:
        user, password = self.config.username or "", self.config.password or ""
        return httpx.BasicAuth(user, password) if self._basic else httpx.DigestAuth(user, password)

    def _get(self, path: str, params: dict) -> str:
        try:
            response = self._http().get(path, params=params, auth=self._auth())
            if response.status_code == 401 and not self._basic and \
                    response.headers.get("www-authenticate", "").lower().startswith("basic"):
                self._basic = True   # older firmware: answers only a Basic challenge
                response = self._http().get(path, params=params, auth=self._auth())
        except httpx.RequestError as exc:
            where = urlparse(self.base).netloc or self.base
            if isinstance(exc, httpx.ConnectTimeout):
                hint = "nothing answered. Check the IP, and that this server can reach the device's network."
            elif isinstance(exc, httpx.ConnectError):
                hint = "the connection was refused or the address didn't resolve. Check the IP and port."
            else:
                hint = f"the connection failed ({type(exc).__name__})."
            raise DahuaError(f"Cannot reach the Dahua device at {where}: {hint}") from exc
        if response.status_code == 401:
            raise DahuaError("The Dahua device refused the username or password. (Repeated wrong "
                             "tries lock the account on the device for a while.)")
        if response.status_code == 404:
            raise DahuaError("That address has no Dahua HTTP API (/cgi-bin). Is it a Dahua terminal or "
                             "access controller, and is its HTTP API enabled?")
        if response.status_code >= 400:
            raise DahuaError(f"The Dahua device refused the request (HTTP {response.status_code}).")
        return response.text

    # -- device ---------------------------------------------------------------
    def _system_info(self) -> dict[str, str]:
        kv = parse_kv(self._get("/cgi-bin/magicBox.cgi", {"action": "getSystemInfo"}))
        if not kv.get("serialNumber") and not kv.get("deviceType"):
            # Older firmware: only getSerialNo answers.
            sn = parse_kv(self._get("/cgi-bin/magicBox.cgi", {"action": "getSerialNo"})).get("sn")
            if not sn:
                raise DahuaError("That address answered, but not like a Dahua device.")
            kv["serialNumber"] = sn
        self._serial = kv.get("serialNumber") or self._serial
        return kv

    # -- records --------------------------------------------------------------
    def _stamp(self, moment: datetime) -> str:
        return str(int(moment.timestamp())) if self._epoch else f"{moment.astimezone(ZoneInfo(self.tz)):%Y-%m-%d %H:%M:%S}"

    def _find(self, start: datetime, end: datetime, count: int) -> tuple[int, list[dict[str, str]]]:
        def ask() -> str:
            return self._get("/cgi-bin/recordFinder.cgi", {
                "action": "find", "name": "AccessControlCardRec",
                "StartTime": self._stamp(start), "EndTime": self._stamp(end), "count": count})

        text = ask()
        if self._epoch and text.lstrip().lower().startswith("error"):
            self._epoch = False            # this firmware wants local text, not epoch seconds
            text = ask()
        if text.lstrip().lower().startswith("error"):
            raise DahuaError(f"The Dahua device rejected the record query: {text.strip()[:120]}")
        return parse_records(text)

    def _window(self, start: datetime, end: datetime) -> Iterator[dict[str, str]]:
        seen: set[str] = set()
        cursor = start
        for _ in range(MAX_PAGES):
            total, rows = self._find(cursor, end, PAGE_SIZE)
            fresh = [r for r in rows if r.get("RecNo") not in seen or not r.get("RecNo")]
            for row in fresh:
                if row.get("RecNo"):
                    seen.add(row["RecNo"])
                yield row
            if total <= len(rows) or not rows:
                return
            last = parse_created(rows[-1].get("CreateTime", ""), ZoneInfo(self.tz))
            if last is None:
                return
            # More than one page: continue from the last record's time (inclusive).
            cursor = last if (last > cursor or fresh) else last + timedelta(seconds=1)
        log.warning("Dahua %s: stopped paging after %d pages", self.host, MAX_PAGES)

    # -- interface -----------------------------------------------------------
    def test_connection(self) -> ConnectionInfo:
        info = self._system_info()
        zone = ZoneInfo(self.tz)
        now = datetime.now(zone).replace(microsecond=0)
        total, _ = self._find(now - timedelta(days=1), now, 1)
        model = info.get("deviceType") or "Dahua device"
        return ConnectionInfo(True, f"Connected to {model} at {self.host} — {total} access record"
                                    f"{'' if total == 1 else 's'} in the last 24 hours",
                              {"device_type": model, "serial": info.get("serialNumber"), "records_24h": total})

    def fetch_terminals(self) -> Iterator[TerminalRecord]:
        info = self._system_info()
        return iter([TerminalRecord(serial_number=info.get("serialNumber") or self.host,
                                    alias=info.get("deviceType") or None, ip_address=self.host,
                                    model=info.get("deviceType") or "Dahua")])

    def fetch_punches(self, since=None, until=None) -> Iterator[PunchEvent]:
        zone = ZoneInfo(self.tz)
        now = datetime.now(zone).replace(microsecond=0)
        end = until.replace(tzinfo=zone) if until else now
        start = since.replace(tzinfo=zone) if since else end - timedelta(days=1)
        start = max(start, end - timedelta(days=MAX_DAYS))
        if not self._serial:
            try:
                self._system_info()
            except DahuaError:
                raise
        serial = self._serial or self.host
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=1), end)
            for row in self._window(cursor, chunk_end):
                if row.get("Status", "1") != "1":
                    continue                       # a failed attempt, not a punch
                user = (row.get("UserID") or "").strip()
                when = parse_created(row.get("CreateTime", ""), zone)
                if not user or when is None:
                    continue
                utc = when.astimezone(timezone.utc)
                rec = (row.get("RecNo") or "").strip()
                door = (row.get("Door") or row.get("ReaderID") or "").strip()
                yield PunchEvent(
                    external_id=f"{serial}:{rec}" if rec else f"{serial}:{user}:{utc:%Y%m%d%H%M%S}",
                    emp_code=user,
                    punch_time_local=when.astimezone(zone).replace(tzinfo=None),
                    direction=direction_of(row),
                    terminal_sn=serial,
                    verify_type=METHODS.get((row.get("Method") or "").strip()),
                    raw={"punch_state": row.get("AttendanceState") or row.get("Type") or "",
                         "method": row.get("Method"), "door": door, "card": row.get("CardNo")},
                )
            cursor = chunk_end
