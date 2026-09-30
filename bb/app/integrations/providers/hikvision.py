"""Hikvision access-control / attendance terminals over ISAPI.

Face, fingerprint and card terminals (DS-K1T… "MinMoe", DS-K1A…) expose
ISAPI — Hikvision's HTTP API — on the device itself, with HTTP Digest auth
using the device's own admin (or an operator) account. BioBridge reaches the
terminal directly, like a standalone ZKTeco terminal: same network, VPN, or
a forwarded port.

Calls used
----------
``GET  /ISAPI/System/deviceInfo``
    XML: deviceName, model, serialNumber, firmwareVersion, macAddress.
``POST /ISAPI/AccessControl/AcsEvent?format=json``
    The event log, searched by time window, paged:
    ``{"AcsEventCond": {"searchID", "searchResultPosition", "maxResults",
    "major", "minor", "startTime", "endTime"}}`` → ``{"AcsEvent":
    {"responseStatusStrg": "OK"|"MORE"|"NO MATCH", "numOfMatches",
    "totalMatches", "InfoList": [{"time", "employeeNoString", "name",
    "serialNo", "minor", "attendanceStatus", "currentVerifyMode", …}]}}``.
    Page with the same searchID, advancing the position by numOfMatches.
``POST /ISAPI/AccessControl/UserInfo/Search?format=json``
    The people enrolled on the terminal, paged the same way.
``POST /ISAPI/AccessControl/UserInfo/Record?format=json``
    Create a person (identity only — faces and fingerprints are enrolled at
    the terminal, or uploaded separately; BioBridge never handles biometrics).

Which events are punches
-------------------------
Access events are major 5. Rather than trust one firmware's minor code for
"face passed" (published lists disagree between models), the search asks for
every major-5 event and keeps those that name an employee: successful
authentications carry ``employeeNoString``; door, alarm, and "unknown face"
events do not. A terminal that refuses ``minor: 0`` is asked again per
success code (``FALLBACK_MINORS``). ``attendanceStatus`` gives in/out when the
terminal's attendance mode is on; otherwise direction is left to pairing.

Built from Hikvision's ISAPI documentation and public integrations; tested
against a simulated terminal (tests/test_hikvision.py), not physical
hardware in this environment. Pilot a real terminal before relying on it.
"""

from __future__ import annotations

import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any, Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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

#: Tests point this at an httpx.MockTransport standing in for a terminal.
TRANSPORT: httpx.BaseTransport | None = None

PAGE_SIZE = 30            # most terminals cap maxResults at 30
MAX_PAGES = 2000          # a runaway guard: 60k events in one window
MAJOR_ACCESS = 5
#: Success minors to ask for one by one when a terminal rejects minor 0.
FALLBACK_MINORS = (75, 38, 1, 113, 104)
VERIFY_BY_MINOR = {75: "face", 38: "finger", 113: "finger", 1: "card", 104: "face"}

_IN = {"checkin", "breakin", "overtimein"}
_OUT = {"checkout", "breakout", "overtimeout"}


class HikvisionError(ProviderError):
    pass


class HikvisionRefused(HikvisionError):
    """The device answered but rejected the request (bad/unsupported params) —
    as opposed to being unreachable or refusing the login."""


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _xml_fields(text: str) -> dict[str, str]:
    try:
        root = _strip_ns(ET.fromstring(text))
    except ET.ParseError:
        return {}
    return {child.tag: (child.text or "").strip() for child in root}


def _error_text(response: httpx.Response) -> str:
    """Hikvision errors come as JSON {statusString, subStatusCode, errorMsg}
    or XML <ResponseStatus>; pull out whatever says what went wrong."""
    try:
        data = response.json()
        parts = [data.get("statusString"), data.get("subStatusCode"), data.get("errorMsg")]
        text = " — ".join(str(p) for p in parts if p)
        if text:
            return text
    except ValueError:
        pass
    fields = _xml_fields(response.text)
    text = " — ".join(v for v in (fields.get("statusString"), fields.get("subStatusCode")) if v)
    return text or f"HTTP {response.status_code}"


def parse_event_time(value: str, tz: str) -> datetime | None:
    """ISAPI times carry an offset ("2026-09-30T08:59:12+05:30"). Returned as
    naive wall-clock in the connection's zone, which is what every provider
    hands the engine."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(ZoneInfo(tz or "UTC"))
    return moment.replace(tzinfo=None)


def format_time(local: datetime, tz: str) -> str:
    """Naive local → ISO with the zone's offset, as terminals expect."""
    aware = local.replace(tzinfo=ZoneInfo(tz or "UTC"))
    return aware.isoformat(timespec="seconds")


def direction_of(status: str | None) -> bool | None:
    key = (status or "").replace("_", "").lower()
    return True if key in _IN else False if key in _OUT else None


@register
class HikvisionProvider(AttendanceProvider):
    slug = "hik_isapi"
    label = "Hikvision terminal (ISAPI)"
    description = (
        "A Hikvision face, fingerprint or card terminal (DS-K1T… and similar), "
        "reached directly over the network with its admin account."
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
         "help": "http://<device IP> — add :port if it isn't 80. Use https:// only "
                 "if the device has HTTPS on."},
        {"name": "username", "label": "Username", "type": "text", "required": True,
         "default": "admin", "help": "The device's admin account, or an operator with access-control rights."},
        {"name": "password", "label": "Password", "type": "password", "required": True,
         "help": "Five wrong tries lock the account for 30 minutes."},
        {"name": "server_timezone", "label": "Device timezone", "type": "timezone",
         "required": True, "default": "UTC",
         "help": "The zone the device's clock is set to."},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        base = (config.base_url or "").strip().rstrip("/")
        if base and "://" not in base:
            base = f"http://{base}"
        self.base = base
        self.tz = config.timezone or "UTC"
        self._client: httpx.Client | None = None
        self._info: dict[str, str] | None = None

    # -- transport ------------------------------------------------------------
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base,
                auth=httpx.DigestAuth(self.config.username or "", self.config.password or ""),
                verify=self.config.verify_ssl,
                timeout=settings.http_timeout_seconds,
                transport=TRANSPORT,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _call(self, method: str, path: str, json_body: dict | None = None) -> httpx.Response:
        try:
            response = self._http().request(method, path, json=json_body)
        except httpx.RequestError as exc:
            raise HikvisionError(self._unreachable(exc)) from exc
        if response.status_code == 401:
            locked = "locked" in response.text.lower()
            raise HikvisionError(
                "The device locked this account after too many wrong passwords — "
                "wait 30 minutes, or unlock it on the device." if locked else
                "The device refused the username or password. (Five wrong tries "
                "lock the account for 30 minutes.)"
            )
        if response.status_code == 403:
            raise HikvisionError("This account isn't allowed to read access-control data. "
                                 "Use the admin account, or give it access-control rights.")
        if response.status_code == 404:
            raise HikvisionError(f"The device doesn't offer {path.split('?')[0]} — is this an "
                                 "access-control / attendance terminal (not a camera or NVR)?")
        return response

    def _unreachable(self, exc: httpx.RequestError) -> str:
        where = urlparse(self.base).netloc or self.base
        if isinstance(exc, httpx.ConnectTimeout):
            hint = "nothing answered. Check the IP, and that this server can reach the device's network."
        elif isinstance(exc, httpx.ConnectError):
            hint = ("the connection was refused or the address didn't resolve. Check the IP and "
                    "port, and whether the device has HTTP on (Network → Advanced → Integration Protocol).")
        elif isinstance(exc, httpx.TimeoutException):
            hint = "the device accepted the connection but didn't answer in time."
        else:
            hint = f"the connection failed ({type(exc).__name__})."
        return f"Cannot reach the Hikvision device at {where}: {hint}"

    def _json(self, response: httpx.Response, what: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise HikvisionRefused(f"The device refused {what}: {_error_text(response)}")
        try:
            return response.json()
        except ValueError as exc:
            raise HikvisionError(f"The device sent an unreadable reply to {what}.") from exc

    # -- device ---------------------------------------------------------------
    def device_info(self) -> dict[str, str]:
        if self._info is None:
            response = self._call("GET", "/ISAPI/System/deviceInfo")
            if response.status_code >= 400:
                raise HikvisionError(f"The device refused the info request: {_error_text(response)}")
            info = _xml_fields(response.text)
            if not info:
                raise HikvisionError("That address answered, but not like a Hikvision device.")
            self._info = info
        return self._info

    def test_connection(self) -> ConnectionInfo:
        info = self.device_info()
        name = info.get("model") or info.get("deviceName") or "Hikvision device"
        serial = info.get("serialNumber") or "?"
        return ConnectionInfo(
            True,
            f"Connected to {name} (serial {serial})",
            {k: v for k, v in {
                "model": info.get("model"), "serial_number": info.get("serialNumber"),
                "firmware": info.get("firmwareVersion"), "device_name": info.get("deviceName"),
            }.items() if v},
        )

    def fetch_terminals(self) -> Iterator[TerminalRecord]:
        info = self.device_info()
        serial = info.get("serialNumber") or info.get("deviceID") or ""
        if not serial:
            return iter(())
        return iter([TerminalRecord(
            serial_number=serial,
            alias=info.get("deviceName") or None,
            ip_address=urlparse(self.base).hostname,
            model=info.get("model") or None,
        )])

    # -- punches --------------------------------------------------------------
    def _search_events(self, minor: int, start: str, end: str) -> Iterator[dict]:
        search_id = uuid.uuid4().hex
        position = 0
        for _ in range(MAX_PAGES):
            body = {"AcsEventCond": {
                "searchID": search_id, "searchResultPosition": position,
                "maxResults": PAGE_SIZE, "major": MAJOR_ACCESS, "minor": minor,
                "startTime": start, "endTime": end,
            }}
            data = self._json(self._call("POST", "/ISAPI/AccessControl/AcsEvent?format=json", body),
                              "the event search")
            block = data.get("AcsEvent") or {}
            rows = block.get("InfoList") or []
            yield from rows
            status = str(block.get("responseStatusStrg") or "").upper()
            got = int(block.get("numOfMatches") or len(rows))
            if status != "MORE" or got == 0:
                return
            position += got
        log.warning("Hikvision %s: stopped paging after %d pages", self.base, MAX_PAGES)

    def fetch_punches(self, since=None, until=None) -> Iterator[PunchEvent]:
        info = self.device_info()
        serial = info.get("serialNumber") or info.get("deviceID") or urlparse(self.base).hostname or "hik"
        now = datetime.now(ZoneInfo(self.tz)).replace(tzinfo=None)
        start = format_time(since or now - timedelta(days=1), self.tz)
        end = format_time(until or now, self.tz)

        try:
            rows = list(self._search_events(0, start, end))
        except HikvisionRefused as exc:
            # Some firmware insists on a specific minor code. Only a refusal
            # falls back: an unreachable device or a wrong password must fail
            # the sync, not look like "no punches".
            log.info("Hikvision %s rejected minor 0 (%s); asking per success code", self.base, exc)
            rows, accepted = [], 0
            for minor in FALLBACK_MINORS:
                try:
                    rows.extend(self._search_events(minor, start, end))
                    accepted += 1
                except HikvisionRefused:
                    continue
            if not accepted:
                raise

        seen: set[str] = set()
        for row in sorted(rows, key=lambda r: (str(r.get("time") or ""), r.get("serialNo") or 0)):
            employee = str(row.get("employeeNoString") or row.get("employeeNo") or "").strip()
            if not employee:
                continue  # door / alarm / stranger events carry no employee
            when = parse_event_time(str(row.get("time") or ""), self.tz)
            if when is None:
                continue
            serial_no = row.get("serialNo")
            external = f"{serial}:{serial_no}" if serial_no not in (None, "") \
                else f"{serial}:{employee}:{when:%Y%m%d%H%M%S}"
            if external in seen:
                continue
            seen.add(external)
            minor = row.get("minor")
            name = str(row.get("name") or "").strip()
            first, _, last = name.partition(" ")
            yield PunchEvent(
                external_id=external,
                emp_code=employee,
                punch_time_local=when,
                direction=direction_of(row.get("attendanceStatus")),
                terminal_sn=serial,
                terminal_alias=info.get("deviceName") or None,
                first_name=first or None,
                last_name=last or None,
                verify_type=VERIFY_BY_MINOR.get(minor, (row.get("currentVerifyMode") or "")[:8] or None),
                raw={"punch_state": row.get("attendanceStatus") or "", "minor": minor,
                     "serialNo": serial_no, "time": row.get("time")},
            )

    # -- people ---------------------------------------------------------------
    def fetch_employees(self) -> Iterator[EmployeeRecord]:
        search_id = uuid.uuid4().hex
        position = 0
        for _ in range(MAX_PAGES):
            body = {"UserInfoSearchCond": {"searchID": search_id,
                                           "searchResultPosition": position, "maxResults": PAGE_SIZE}}
            data = self._json(self._call("POST", "/ISAPI/AccessControl/UserInfo/Search?format=json", body),
                              "the user search")
            block = data.get("UserInfoSearch") or {}
            users = block.get("UserInfo") or []
            for user in users:
                code = str(user.get("employeeNo") or "").strip()
                if not code:
                    continue
                first, _, last = str(user.get("name") or "").strip().partition(" ")
                yield EmployeeRecord(external_id=code, emp_code=code, first_name=first, last_name=last,
                                     is_active=bool((user.get("Valid") or {}).get("enable", True)))
            status = str(block.get("responseStatusStrg") or "").upper()
            got = int(block.get("numOfMatches") or len(users))
            if status != "MORE" or got == 0:
                return
            position += got

    def create_employee(self, record: EmployeeRecord) -> EmployeeRecord:
        code = (record.emp_code or "").strip()
        if not code or len(code) > 32:
            raise HikvisionError(f"'{code}' can't be used as a Hikvision employee number (1–32 characters).")
        name = (record.full_name or code)[:32]
        this_year = datetime.now().year
        body = {"UserInfo": {
            "employeeNo": code, "name": name, "userType": "normal",
            "Valid": {"enable": True, "beginTime": f"{this_year - 1}-01-01T00:00:00",
                      "endTime": "2037-12-31T23:59:59", "timeType": "local"},
            "doorRight": "1", "RightPlan": [{"doorNo": 1, "planTemplateNo": "1"}],
        }}
        response = self._call("POST", "/ISAPI/AccessControl/UserInfo/Record?format=json", body)
        if response.status_code >= 400:
            text = _error_text(response)
            if "exist" in text.lower():
                return record  # already on the device: nothing to do
            raise HikvisionError(f"The device didn't accept {code}: {text}")
        return EmployeeRecord(external_id=code, emp_code=code,
                              first_name=record.first_name, last_name=record.last_name)

