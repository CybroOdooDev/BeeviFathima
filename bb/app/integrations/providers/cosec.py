"""Matrix COSEC terminals over the COSEC Devices API.

COSEC door controllers and terminals (ARGO, VEGA, PATH, PVR, NGT, …) serve a
small HTTP API on the device itself, ``/device.cgi/…``, with HTTP Basic auth
(factory default ``admin`` / ``1234``). BioBridge reaches the terminal
directly — same network, VPN, or forwarded port — like a standalone ZKTeco or
Hikvision terminal. From Matrix's "COSEC Devices API Guide":

``GET /device.cgi/device-basic-config?action=get&format=xml``
    The device's configured name — the connection test.
``GET /device.cgi/events?action=getevent&roll-over-count=R&seq-number=S&no-of-events=N&format=xml``
    The event log is a ring buffer addressed by (roll-over count, sequence
    number), not by time. Each ``<Events>`` block carries ``roll-over-count``,
    ``seq-No``, ``date`` (D/M/YYYY), ``time``, ``event-id`` and
    ``detail-1``…``detail-5``. For user events detail-1 is the user's numeric
    reference id, detail-2 the special function, detail-3 entry (0) / exit (1).
``GET /device.cgi/users?action=get|set&user-id=…&ref-user-id=…&name=…``
    Read one user / create or update one.

Reading by sequence
-------------------
The log can't be searched by time, so this provider keeps a cursor — the
last (roll-over, sequence) read — in the connection's config
(``config_updates``, saved by the sync engine only after the punches are
stored). The first sync reads from the start of the log (bounded per run,
continuing next run); later syncs read only what's new. When the sequence
runs out at the top of the ring the next roll-over is tried.

Which events are punches
-------------------------
"User allowed" events: event-id 101–110 with a user in detail-1. Denied,
door, alarm and system events are skipped.

Built from Matrix's published device API guide; tested against a simulated
device (tests/test_cosec.py), not a physical terminal here. Pilot first.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.integrations.base import (
    AttendanceProvider,
    Capability,
    ConnectionInfo,
    EmployeeRecord,
    ProviderError,
    PunchEvent,
    SourceConfig,
    TerminalRecord,
    register,
)

log = logging.getLogger(__name__)

#: Tests point this at an httpx.MockTransport standing in for a device.
TRANSPORT: httpx.BaseTransport | None = None

PAGE_SIZE = 100            # the API's maximum (PATH/V2 devices return fewer)
MAX_REQUESTS = 400         # per sync run; a long first backlog continues next run
ALLOWED_EVENTS = set(range(101, 111))
CURSOR_KEY = "cosec_cursor"


class CosecError(ProviderError):
    pass


def _parse(text: str) -> ET.Element | None:
    try:
        return ET.fromstring(text.strip())
    except ET.ParseError:
        return None


def parse_events(text: str) -> list[dict[str, str]]:
    root = _parse(text)
    if root is None:
        return []
    out = []
    for block in root.iter("Events"):
        out.append({child.tag: (child.text or "").strip() for child in block})
    return out


def parse_datetime(date_text: str, time_text: str) -> datetime | None:
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(f"{date_text} {time_text}", fmt)
        except ValueError:
            continue
    return None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@register
class CosecProvider(AttendanceProvider):
    slug = "cosec"
    label = "Matrix COSEC terminal"
    description = (
        "A Matrix COSEC door controller or attendance terminal (ARGO, VEGA, PATH, "
        "PVR, NGT…), reached directly over the network with its web login."
    )
    capabilities = frozenset({
        Capability.READ_PUNCHES,
        Capability.LIST_TERMINALS,
        Capability.READ_EMPLOYEES,
        Capability.WRITE_EMPLOYEES,
    })
    kinds = frozenset({"device"})
    config_fields = (
        {"name": "base_url", "label": "Device address", "type": "text", "required": True,
         "help": "http://<device IP> — add :port if it isn't 80."},
        {"name": "username", "label": "Username", "type": "text", "required": True,
         "default": "admin", "help": "The device's web login (factory default admin)."},
        {"name": "password", "label": "Password", "type": "password", "required": True},
        {"name": "server_timezone", "label": "Device timezone", "type": "timezone",
         "required": True, "default": "UTC"},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        base = (config.base_url or "").strip().rstrip("/")
        if base and "://" not in base:
            base = f"http://{base}"
        self.base = base
        self.host = urlparse(base).hostname or base
        self._client: httpx.Client | None = None
        cursor = (config.options or {}).get(CURSOR_KEY) or {}
        self._cursor = (_int(cursor.get("rollover"), 0), _int(cursor.get("seq"), 0))
        #: Read by the sync engine and merged into DeviceSource.config once the
        #: punches from this fetch are stored.
        self.config_updates: dict[str, Any] = {}

    # -- transport ------------------------------------------------------------
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base, auth=(self.config.username or "", self.config.password or ""),
                verify=self.config.verify_ssl, timeout=settings.http_timeout_seconds, transport=TRANSPORT)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _get(self, path: str, params: dict[str, Any]) -> str:
        try:
            response = self._http().get(path, params=params)
        except httpx.RequestError as exc:
            where = urlparse(self.base).netloc or self.base
            if isinstance(exc, httpx.ConnectTimeout):
                hint = "nothing answered. Check the IP, and that this server can reach the device's network."
            elif isinstance(exc, httpx.ConnectError):
                hint = "the connection was refused or the address didn't resolve. Check the IP and port."
            else:
                hint = f"the connection failed ({type(exc).__name__})."
            raise CosecError(f"Cannot reach the COSEC device at {where}: {hint}") from exc
        if response.status_code == 401:
            raise CosecError("The COSEC device refused the username or password.")
        if response.status_code == 404:
            raise CosecError("That address has no COSEC device API (/device.cgi). Is it a COSEC "
                             "terminal or door controller, and is its API enabled?")
        if response.status_code >= 400:
            raise CosecError(f"The COSEC device refused the request (HTTP {response.status_code}).")
        return response.text

    # -- interface -----------------------------------------------------------
    def _basic_config(self) -> dict[str, str]:
        root = _parse(self._get("/device.cgi/device-basic-config", {"action": "get", "format": "xml"}))
        if root is None:
            raise CosecError("That address answered, but not like a COSEC device.")
        return {child.tag: (child.text or "").strip() for child in root.iter() if child is not root}

    def test_connection(self) -> ConnectionInfo:
        info = self._basic_config()
        name = info.get("name") or "COSEC device"
        return ConnectionInfo(True, f"Connected to {name} at {self.host}", {"device_name": name})

    def fetch_terminals(self) -> Iterator[TerminalRecord]:
        info = self._basic_config()
        # The device API reports no serial number; the address identifies the
        # one terminal behind this connection.
        return iter([TerminalRecord(serial_number=self.host, alias=info.get("name") or None,
                                    ip_address=self.host, model="Matrix COSEC")])

    def _page(self, rollover: int, seq: int) -> list[dict[str, str]]:
        return parse_events(self._get("/device.cgi/events", {
            "action": "getevent", "roll-over-count": rollover, "seq-number": seq,
            "no-of-events": PAGE_SIZE, "format": "xml"}))

    def fetch_punches(self, since=None, until=None) -> Iterator[PunchEvent]:
        rollover, seq = self._cursor
        next_seq = seq + 1 if seq else 1
        for _ in range(MAX_REQUESTS):
            rows = self._page(rollover, next_seq)
            if not rows:
                # The top of this roll-over: the log may continue in the next.
                ahead = self._page(rollover + 1, 1)
                if not ahead:
                    break
                rows = ahead
            for row in rows:
                r = _int(row.get("roll-over-count"), rollover)
                s = _int(row.get("seq-No") or row.get("seq-no"), next_seq)
                rollover, next_seq = r, s + 1
                self.config_updates[CURSOR_KEY] = {"rollover": r, "seq": s}
                if _int(row.get("event-id"), -1) not in ALLOWED_EVENTS:
                    continue
                user = str(row.get("detail-1") or "").strip()
                if not user or user == "0":
                    continue
                when = parse_datetime(row.get("date", ""), row.get("time", ""))
                if when is None:
                    continue
                exit_flag = str(row.get("detail-3") or "").strip()
                yield PunchEvent(
                    external_id=f"{self.host}:{r}:{s}",
                    emp_code=user,
                    punch_time_local=when,
                    direction=True if exit_flag == "0" else False if exit_flag == "1" else None,
                    terminal_sn=self.host,
                    verify_type=None,
                    raw={"punch_state": exit_flag, "event_id": row.get("event-id"),
                         "special_function": row.get("detail-2")},
                )

    # -- people ---------------------------------------------------------------
    def fetch_employees(self) -> Iterator[EmployeeRecord]:
        # The device API reads users one id at a time and has no list call, so
        # there is nothing to enumerate; create_employee checks each id first.
        return iter(())

    def _user_exists(self, code: str) -> bool:
        root = _parse(self._get("/device.cgi/users", {"action": "get", "user-id": code, "format": "xml"}))
        if root is None:
            return False
        fields = {child.tag: (child.text or "").strip() for child in root.iter() if child is not root}
        if fields.get("Response-Code", "0") not in ("0", ""):
            return False
        return bool(fields.get("user-id") or fields.get("ref-user-id"))

    def create_employee(self, record: EmployeeRecord) -> EmployeeRecord:
        code = (record.emp_code or "").strip()
        if not code.isdigit() or len(code) > 8:
            raise CosecError(f"'{code}' can't be a COSEC user — its reference id must be numeric, up to 8 digits.")
        if self._user_exists(code):
            return record
        name = (record.full_name or code)[:15]
        root = _parse(self._get("/device.cgi/users", {
            "action": "set", "user-id": code, "ref-user-id": code, "name": name,
            "user-active": 1, "format": "xml"}))
        if root is not None:
            code_text = next((c.text for c in root.iter("Response-Code")), None)
            if code_text not in (None, "", "0"):
                raise CosecError(f"The COSEC device didn't accept user {code} (response code {code_text}).")
        return EmployeeRecord(external_id=code, emp_code=code,
                              first_name=record.first_name, last_name=record.last_name)
