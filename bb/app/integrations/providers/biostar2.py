"""Suprema BioStar 2 — a server that collects punches from many terminals.

Like BioTime, one connection is the customer's BioStar 2 server (usually
``https://<server>`` on port 443, self-signed certificate), not a terminal;
its devices come in through **Import terminals**. Calls, from Suprema's
"BioStar 2 New Local API" guides:

``POST /api/login`` ``{"User": {"login_id", "password"}}``
    The session id comes back in the ``bs-session-id`` response header and is
    sent on every later request in the same header. Sessions expire (an hour
    by default); a 401 means log in again.
``POST /api/events/search``
    ``{"Query": {"limit", "conditions": [{"column": "datetime", "operator": 3
    (between), "values": [from, to]}], "orders": [{"column": "datetime"}]}}``
    → ``{"EventCollection": {"rows": [{"id", "datetime", "user_id": {"user_id",
    "name"}, "device_id": {"id", "name"}, "event_type_id": {"code"},
    "tna_key"}]}}``. Times are UTC ISO-8601.
``GET /api/devices`` → ``{"DeviceCollection": {"rows": [...]}}``
``GET /api/users?limit=&offset=`` / ``POST /api/users``

Which events are punches
-------------------------
Successful authentications: VERIFY_SUCCESS (0x1000–0x10FF, 1:1),
IDENTIFY_SUCCESS (0x1300–0x13FF, 1:N) and DUAL_AUTH_SUCCESS (0x1500–0x15FF).
Everything else (door, alarm, failed and unknown-credential events) is
skipped. ``tna_key`` — the T&A key pressed — gives direction: 1 check in,
2 check out, 3 break start, 4 break end, 5 meal start, 6 meal end.

Paging: the search is ordered by time and re-issued from the last row's time,
de-duplicated on the event id, so no offset support is assumed.

Built from Suprema's published API guides; tested against a simulated server
(tests/test_biostar2.py), not a live BioStar 2 install. Pilot before relying
on it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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

#: Tests point this at an httpx.MockTransport standing in for the server.
TRANSPORT: httpx.BaseTransport | None = None

PAGE_SIZE = 500
MAX_PAGES = 1000
SUCCESS_RANGES = ((0x1000, 0x10FF), (0x1300, 0x13FF), (0x1500, 0x15FF))
TNA_DIRECTION = {1: True, 2: False, 3: False, 4: True, 5: False, 6: True}
# Low byte of the event code: how the person was recognised. 1:1 (verify)
# sub-codes 0x01 ID+PIN, 0x02-0x05 ID+finger/face, 0x06-0x0A card combinations;
# 1:N (identify) 0x01-0x02 finger, 0x03-0x05 face.
VERIFY_BY_SUB = {0x01: "pw", 0x02: "finger", 0x03: "finger", 0x04: "face", 0x05: "face",
                 0x06: "card", 0x07: "card", 0x08: "card", 0x09: "card", 0x0A: "card"}


class BioStarError(ProviderError):
    pass


def is_punch(code: Any) -> bool:
    try:
        value = int(str(code))
    except (TypeError, ValueError):
        return False
    return any(lo <= value <= hi for lo, hi in SUCCESS_RANGES)


def verify_of(code: Any) -> str | None:
    try:
        value = int(str(code))
    except (TypeError, ValueError):
        return None
    if 0x1300 <= value <= 0x13FF:  # identify: 1 finger, 3 face, 5 face+finger
        return {0x01: "finger", 0x02: "finger", 0x03: "face", 0x04: "face", 0x05: "face"}.get(value & 0xFF)
    return VERIFY_BY_SUB.get(value & 0xFF)


def direction_of(tna_key: Any) -> bool | None:
    try:
        return TNA_DIRECTION.get(int(str(tna_key)))
    except (TypeError, ValueError):
        return None


def parse_time(value: str | None) -> datetime | None:
    """"2026-09-30T03:29:12.00Z" → aware UTC."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            moment = datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _nested(value: Any, key: str) -> str:
    if isinstance(value, dict):
        return str(value.get(key) or "").strip()
    return str(value or "").strip()


@register
class BioStar2Provider(AttendanceProvider):
    slug = "biostar2"
    label = "Suprema BioStar 2"
    description = (
        "A Suprema BioStar 2 server, which collects punches from the BioStation, "
        "BioEntry, FaceStation and other Suprema terminals connected to it."
    )
    capabilities = frozenset({
        Capability.READ_PUNCHES,
        Capability.LIST_TERMINALS,
        Capability.READ_EMPLOYEES,
        Capability.WRITE_EMPLOYEES,
    })
    kinds = frozenset({"platform"})
    config_fields = (
        {"name": "base_url", "label": "Server URL", "type": "text", "required": True,
         "help": "Where BioStar 2 runs, e.g. https://biostar.example.com (add :port if it isn't 443)."},
        {"name": "username", "label": "Username", "type": "text", "required": True,
         "help": "A BioStar 2 operator login, e.g. admin."},
        {"name": "password", "label": "Password", "type": "password", "required": True},
        {"name": "server_timezone", "label": "Site Timezone", "type": "timezone",
         "required": True, "default": "UTC",
         "help": "BioStar 2 reports times in UTC; this is the zone attendance is shown in."},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        base = (config.base_url or "").strip().rstrip("/")
        if base and "://" not in base:
            base = f"https://{base}"
        if base.endswith("/api"):
            base = base[:-4]
        self.base = base
        self.tz = config.timezone or "UTC"
        self._session: str | None = config.token or None
        self._client: httpx.Client | None = None

    @property
    def cached_token(self) -> str | None:
        """Persisted by the caller so the next run can skip the login."""
        return self._session

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

    def _unreachable(self, exc: httpx.RequestError) -> str:
        where = urlparse(self.base).netloc or self.base
        text = str(exc).lower()
        if "certificate" in text or "ssl" in text:
            hint = ("its HTTPS certificate isn't trusted. BioStar 2 normally uses a self-signed "
                    "one — choose “Accept the server’s own certificate”.")
        elif isinstance(exc, httpx.ConnectTimeout):
            hint = "nothing answered. Check the address, and that this server can reach it."
        elif isinstance(exc, httpx.ConnectError):
            hint = "the connection was refused or the address didn't resolve. Check the URL and port."
        elif isinstance(exc, httpx.TimeoutException):
            hint = "the server accepted the connection but didn't answer in time."
        else:
            hint = f"the connection failed ({type(exc).__name__})."
        return f"Cannot reach BioStar 2 at {where}: {hint}"

    @staticmethod
    def _message(response: httpx.Response) -> str:
        try:
            data = response.json()
            inner = data.get("Response") or data
            text = inner.get("message") or inner.get("code")
            if text:
                return str(text)
        except ValueError:
            pass
        return f"HTTP {response.status_code}"

    def _login(self) -> str:
        try:
            response = self._http().post("/api/login", json={
                "User": {"login_id": self.config.username or "", "password": self.config.password or ""}})
        except httpx.RequestError as exc:
            raise BioStarError(self._unreachable(exc)) from exc
        if response.status_code in (401, 403) or (response.status_code >= 400 and "password" in response.text.lower()):
            raise BioStarError(f"BioStar 2 refused the username or password ({self._message(response)}).")
        if response.status_code == 404:
            raise BioStarError("That address answered, but has no BioStar 2 API at /api/login. "
                               "Check the URL (and the port, if BioStar 2 isn't on 443).")
        if response.status_code >= 400:
            raise BioStarError(f"BioStar 2 login failed: {self._message(response)}")
        session = response.headers.get("bs-session-id")
        if not session:
            raise BioStarError("BioStar 2 accepted the login but sent no session id.")
        self._session = session
        return session

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in (1, 2):
            if not self._session:
                self._login()
            try:
                response = self._http().request(method, path, headers={"bs-session-id": self._session}, **kwargs)
            except httpx.RequestError as exc:
                raise BioStarError(self._unreachable(exc)) from exc
            if response.status_code == 401 and attempt == 1:
                self._session = None  # expired: log in again, once
                continue
            if response.status_code == 403:
                raise BioStarError("This BioStar 2 operator isn't allowed to do that — use an "
                                   "administrator, or give it User and Monitoring permissions.")
            if response.status_code >= 400:
                raise BioStarError(f"BioStar 2 refused {path}: {self._message(response)}")
            try:
                data = response.json()
            except ValueError as exc:
                raise BioStarError(f"BioStar 2 sent an unreadable reply to {path}.") from exc
            code = str((data.get("Response") or {}).get("code") or "0")
            if code not in ("0", "1"):
                raise BioStarError(f"BioStar 2 refused {path}: {self._message(response)}")
            return data
        raise BioStarError("BioStar 2 keeps refusing the session — check the operator account.")

    # -- interface -----------------------------------------------------------
    def test_connection(self) -> ConnectionInfo:
        self._session = None
        self._login()
        devices = self._devices()
        count = len(devices)
        return ConnectionInfo(True, f"Connected to BioStar 2 — {count} device{'' if count == 1 else 's'} registered",
                              {"devices": count})

    def _devices(self) -> list[dict]:
        data = self._call("GET", "/api/devices")
        return (data.get("DeviceCollection") or {}).get("rows") or []

    def fetch_terminals(self) -> Iterator[TerminalRecord]:
        for row in self._devices():
            serial = str(row.get("id") or "").strip()
            if not serial:
                continue
            lan = row.get("lan") or {}
            yield TerminalRecord(
                serial_number=serial,
                alias=str(row.get("name") or "").strip() or None,
                area=_nested(row.get("device_group_id"), "name") or None,
                ip_address=str(lan.get("ip") or "").strip() or None,
                model=_nested(row.get("device_type_id"), "name") or None,
            )

    def fetch_punches(self, since=None, until=None) -> Iterator[PunchEvent]:
        zone = ZoneInfo(self.tz)
        now = datetime.now(timezone.utc)
        start = since.replace(tzinfo=zone) if since else now - timedelta(days=1)
        end = until.replace(tzinfo=zone) if until else now
        seen: set[str] = set()

        for _ in range(MAX_PAGES):
            body = {"Query": {
                "limit": PAGE_SIZE,
                "conditions": [{"column": "datetime", "operator": 3,
                                "values": [utc_text(start), utc_text(end)]}],
                "orders": [{"column": "datetime", "descending": False}],
            }}
            rows = (self._call("POST", "/api/events/search", json=body).get("EventCollection") or {}).get("rows") or []
            fresh = 0
            last: datetime | None = None
            for row in rows:
                event_id = str(row.get("id") or "").strip()
                when = parse_time(row.get("datetime"))
                if when is not None:
                    last = when if last is None or when > last else last
                if not event_id or event_id in seen:
                    continue
                seen.add(event_id)
                fresh += 1
                code = _nested(row.get("event_type_id"), "code")
                employee = _nested(row.get("user_id"), "user_id")
                if not is_punch(code) or not employee or when is None:
                    continue
                device = row.get("device_id") or {}
                name = _nested(row.get("user_id"), "name")
                first, _, rest = name.partition(" ")
                yield PunchEvent(
                    external_id=event_id,
                    emp_code=employee,
                    punch_time_local=when.astimezone(zone).replace(tzinfo=None),
                    direction=direction_of(row.get("tna_key")),
                    terminal_sn=_nested(device, "id") or None,
                    terminal_alias=_nested(device, "name") or None,
                    first_name=first or None,
                    last_name=rest or None,
                    verify_type=verify_of(code),
                    raw={"punch_state": str(row.get("tna_key") or ""), "event_code": code},
                )
            if len(rows) < PAGE_SIZE or last is None:
                return
            # Next page from the last row's time; ids already seen are skipped.
            # A full page of one second's events would never advance, so step on.
            start = last if fresh else last + timedelta(seconds=1)
        log.warning("BioStar 2 %s: stopped paging after %d pages", self.base, MAX_PAGES)

    def fetch_employees(self) -> Iterator[EmployeeRecord]:
        offset = 0
        for _ in range(MAX_PAGES):
            data = self._call("GET", "/api/users", params={"limit": PAGE_SIZE, "offset": offset})
            block = data.get("UserCollection") or {}
            rows = block.get("rows") or []
            for row in rows:
                code = str(row.get("user_id") or "").strip()
                if not code:
                    continue
                first, _, last = str(row.get("name") or "").strip().partition(" ")
                yield EmployeeRecord(external_id=code, emp_code=code, first_name=first, last_name=last,
                                     is_active=str(row.get("disabled", "false")).lower() != "true")
            offset += len(rows)
            total = int(block.get("total") or 0)
            if not rows or len(rows) < PAGE_SIZE or (total and offset >= total):
                return

    def create_employee(self, record: EmployeeRecord) -> EmployeeRecord:
        code = (record.emp_code or "").strip()
        if not code.isdigit() or len(code) > 10:
            # BioStar 2's default user id is numeric (alphanumeric ids are an
            # opt-in server setting); refuse early rather than half-create.
            raise BioStarError(f"'{code}' can't be a BioStar 2 user ID — it must be numeric (up to 10 digits).")
        body = {"User": {
            "user_id": code,
            "name": (record.full_name or code)[:48],
            "user_group_id": {"id": 1},
            "disabled": "false",
            "start_datetime": "2001-01-01T00:00:00.00Z",
            "expiry_datetime": "2037-12-31T23:59:00.00Z",
        }}
        try:
            self._call("POST", "/api/users", json=body)
        except BioStarError as exc:
            if "exist" in str(exc).lower() or "duplicate" in str(exc).lower():
                return record
            raise
        return EmployeeRecord(external_id=code, emp_code=code,
                              first_name=record.first_name, last_name=record.last_name)
