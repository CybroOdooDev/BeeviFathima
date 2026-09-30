"""Hik-Connect for Teams — Hikvision's cloud platform (formerly HikCentral Connect).

Hikvision terminals in a Hik-Connect *Team* reach the cloud over P2P, so
there is nothing on the customer's network to open: one connection is the
customer's team, identified by an API key and secret the team's admin
creates under **Team Management → API Integration**. The consumer Hik-Connect
app has no such API.

The OpenAPI lives under ``<region>/api/hccgw/`` (e.g.
``https://ieu.hikcentralconnect.com``). Every call is a JSON ``POST``; the
reply is ``{"errorCode": "0", "message": …, "data": {…}}`` with errorCode
"0" for success.

``platform/v1/token/get``  ``{"appKey", "secretKey"}``
    → ``data.accessToken`` (+ ``expireTime``, and on some accounts
    ``areaDomain`` — the regional host to use from then on). Data calls carry
    it in a ``Token:`` header (not ``Authorization: Bearer``). Cached between
    runs; renewed once when refused.
``attendance/v1/records/get``  ``{"startTime", "endTime", "pageNo", "pageSize"}``
    Check-in/out records in a window (ISO-8601 with offset) → ``data.total``
    and ``data.list``.

What is not pinned down
-----------------------
Hikvision's developer guide is only available to registered partners. The
token call and the ``Token`` header are confirmed by public working
examples; the attendance endpoint and its request come from a third-party
reference, and the *record* field names are not published there at all. So,
as with COSEC CENTRA, fields are recognised by name — the person's code
(``personCode``, ``employeeNo``…, also inside a nested ``personInfo``),
the time (``clockTime``, ``attendanceTime``, ``eventTime``…), the device
(``deviceSerialNo``/``deviceName``) and the in/out status — and the
connection test says plainly when a record carries none of them. Verify
against a real team (or the partner guide) before selling; see
tests/test_hikconnect.py for the shape assumed.
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
    ProviderError,
    PunchEvent,
    SourceConfig,
    register,
)
from app.integrations.providers.hikvision import direction_of as _hik_direction

log = logging.getLogger(__name__)

#: Tests point this at an httpx.MockTransport standing in for the cloud.
TRANSPORT: httpx.BaseTransport | None = None

REGIONS = {
    "eu": "https://ieu.hikcentralconnect.com",
    "us": "https://ius.hikcentralconnect.com",
}
PREFIX = "/api/hccgw"
PAGE_SIZE = 100
MAX_PAGES = 500
MAX_DAYS = 31          # one run's window; a longer first backfill continues next run

PERSON_KEYS = ("personCode", "employeeNo", "employeeNoString", "personNo", "workNo", "personId")
TIME_KEYS = ("clockTime", "attendanceTime", "checkTime", "punchTime", "eventTime", "occurTime",
             "recordTime", "deviceTime", "time")
SERIAL_KEYS = ("deviceSerialNo", "deviceSerial", "serialNo", "devSerial", "deviceId")
DEVICE_NAME_KEYS = ("deviceName", "devName", "resourceName", "doorName")
STATUS_KEYS = ("attendanceStatus", "clockType", "checkType", "attendanceType", "punchType", "direction")
ID_KEYS = ("recordId", "recordGuid", "eventId", "id", "guid")

_NUMERIC_STATUS = {"0": True, "1": False}   # 0 = check-in, 1 = check-out, as on Hikvision terminals
_TOKEN_ERRORS = ("token", "expire", "auth", "0x2001", "0x2004", "401")


class HikConnectError(ProviderError):
    pass


def _first(row: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _flatten(row: dict) -> dict:
    """A record's own fields, with a nested person/device object's fields
    filled in underneath — some APIs put ``personCode`` in ``personInfo``."""
    flat: dict[str, Any] = {}
    for key in ("personInfo", "person", "baseInfo", "deviceInfo", "device"):
        nested = row.get(key)
        if isinstance(nested, dict):
            for k, v in _flatten(nested).items():
                flat.setdefault(k, v)
    for k, v in row.items():
        if not isinstance(v, dict):
            flat[k] = v
    return flat


def parse_time(value: Any, zone: ZoneInfo) -> datetime | None:
    """ISO-8601 (with or without offset) or epoch seconds/milliseconds → aware."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        number = float(value)
        return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text.replace(" ", "T", 1) if "T" not in text else text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def direction_of(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text in _NUMERIC_STATUS:
        return _NUMERIC_STATUS[text]
    return _hik_direction(text)


@register
class HikConnectProvider(AttendanceProvider):
    slug = "hikconnect"
    label = "Hik-Connect for Teams"
    description = (
        "Hikvision's cloud platform (formerly HikCentral Connect). Hikvision "
        "terminals in your team report to it over the internet — connect with "
        "an API key, nothing on your network to open."
    )
    capabilities = frozenset({Capability.READ_PUNCHES})
    kinds = frozenset({"platform"})
    config_fields = (
        {"name": "base_url", "label": "Region", "type": "text", "required": True,
         "help": "Your team's Hik-Connect region (Europe or North America)."},
        {"name": "username", "label": "App key", "type": "text", "required": True,
         "help": "Hik-Connect for Teams → Team Management → API Integration."},
        {"name": "password", "label": "Secret key", "type": "password", "required": True},
        {"name": "server_timezone", "label": "Site timezone", "type": "timezone",
         "required": True, "default": "UTC",
         "help": "Records arrive with their offset; this is the zone they're read in."},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        base = (config.base_url or "").strip().rstrip("/")
        base = REGIONS.get(base.lower(), base)
        if base and "://" not in base:
            base = f"https://{base}"
        if base.lower().endswith(PREFIX):
            base = base[: -len(PREFIX)]
        self.base = base
        self.tz = config.timezone or "UTC"
        self._token: str | None = config.token or None
        self._client: httpx.Client | None = None

    @property
    def cached_token(self) -> str | None:
        return self._token

    # -- transport ------------------------------------------------------------
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=settings.http_timeout_seconds, verify=self.config.verify_ssl,
                                        transport=TRANSPORT)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _post(self, path: str, body: dict, token: str | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Token"] = token
        try:
            response = self._http().post(f"{self.base}{PREFIX}/{path}", json=body, headers=headers)
        except httpx.RequestError as exc:
            where = urlparse(self.base).netloc or self.base
            raise HikConnectError(f"Cannot reach Hik-Connect at {where}: {type(exc).__name__}. "
                                  "Check the region and this server's internet access.") from exc
        if response.status_code == 404:
            raise HikConnectError(f"Hik-Connect has no such API at {urlparse(self.base).netloc} "
                                  f"({path}). Check the region.")
        try:
            data = response.json()
        except ValueError as exc:
            raise HikConnectError(f"Hik-Connect sent an unreadable reply (HTTP {response.status_code}).") from exc
        code = str(data.get("errorCode", data.get("code", "0" if response.status_code < 400 else response.status_code)))
        if response.status_code >= 400 or code not in ("0", "200"):
            message = data.get("message") or data.get("msg") or f"error {code}"
            raise HikConnectError(f"{message} (code {code})")
        return data.get("data") or {}

    def _authenticate(self) -> str:
        try:
            data = self._post("platform/v1/token/get",
                              {"appKey": self.config.username or "", "secretKey": self.config.password or ""})
        except HikConnectError as exc:
            raise HikConnectError(f"Hik-Connect refused the app key or secret key ({exc}). Check them "
                                  "under Team Management → API Integration, and that the region "
                                  "matches your team.") from exc
        token = data.get("accessToken")
        if not token:
            raise HikConnectError("Hik-Connect accepted the key but sent no access token.")
        # Some accounts are told which regional host to use from here on.
        # Only a Hik-Connect host is followed, so the token never goes elsewhere.
        area = str(data.get("areaDomain") or "").strip().rstrip("/")
        host = (urlparse(area).hostname or "").lower()
        if area.startswith("https://") and host.endswith(".hikcentralconnect.com"):
            self.base = area
        self._token = token
        return token

    def _data(self, path: str, body: dict) -> dict:
        if not self._token:
            self._authenticate()
        try:
            return self._post(path, body, self._token)
        except HikConnectError as exc:
            if any(marker in str(exc).lower() for marker in _TOKEN_ERRORS):
                self._authenticate()  # expired: once more with a fresh token
                return self._post(path, body, self._token)
            raise

    def _page(self, start: datetime, end: datetime, page: int, size: int) -> dict:
        return self._data("attendance/v1/records/get", {
            "startTime": start.isoformat(timespec="seconds"), "endTime": end.isoformat(timespec="seconds"),
            "pageNo": page, "pageSize": size})

    # -- interface -----------------------------------------------------------
    def test_connection(self) -> ConnectionInfo:
        self._token = None
        self._authenticate()
        zone = ZoneInfo(self.tz)
        now = datetime.now(zone).replace(microsecond=0)
        data = self._page(now - timedelta(days=1), now, 1, 5)
        rows = [_flatten(r) for r in (data.get("list") or []) if isinstance(r, dict)]
        total = int(data.get("total") or len(rows))
        if rows and not all(_first(r, PERSON_KEYS) and parse_time(_first(r, TIME_KEYS), zone) for r in rows):
            return ConnectionInfo(False, "Connected to Hik-Connect, but its attendance records carry no person "
                                         "code or time BioBridge recognises. Send us the fields listed below.",
                                  {"fields": sorted(rows[0])})
        return ConnectionInfo(True, f"Connected to Hik-Connect — {total} attendance record"
                                    f"{'' if total == 1 else 's'} in the last 24 hours", {"records_24h": total})

    def fetch_punches(self, since=None, until=None) -> Iterator[PunchEvent]:
        zone = ZoneInfo(self.tz)
        now = datetime.now(zone).replace(microsecond=0)
        end = until.replace(tzinfo=zone) if until else now
        start = since.replace(tzinfo=zone) if since else end - timedelta(days=1)
        start = max(start, end - timedelta(days=MAX_DAYS))
        page = 1
        for _ in range(MAX_PAGES):
            data = self._page(start, end, page, PAGE_SIZE)
            rows = data.get("list") or []
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                row = _flatten(raw)
                person = str(_first(row, PERSON_KEYS) or "").strip()
                when = parse_time(_first(row, TIME_KEYS), zone)
                if not person or when is None:
                    continue
                serial = str(_first(row, SERIAL_KEYS) or "").strip() or None
                name = str(_first(row, DEVICE_NAME_KEYS) or "").strip() or None
                status = _first(row, STATUS_KEYS)
                record_id = _first(raw, ID_KEYS)
                utc = when.astimezone(timezone.utc)
                yield PunchEvent(
                    external_id=(f"hc:{record_id}" if record_id not in (None, "")
                                 else f"{serial or name or 'hc'}:{person}:{utc:%Y%m%d%H%M%S}"),
                    emp_code=person,
                    punch_time_local=when.astimezone(zone).replace(tzinfo=None),
                    direction=direction_of(status),
                    terminal_sn=serial or name,
                    terminal_alias=name,
                    first_name=str(row.get("firstName") or "").strip() or None,
                    last_name=str(row.get("lastName") or "").strip() or None,
                    raw={"punch_state": "" if status is None else str(status)},
                )
            total = int(data.get("total") or 0)
            if not rows or len(rows) < PAGE_SIZE or (total and page * PAGE_SIZE >= total):
                return
            page += 1
        log.warning("Hik-Connect %s: stopped paging after %d pages", self.base, MAX_PAGES)
