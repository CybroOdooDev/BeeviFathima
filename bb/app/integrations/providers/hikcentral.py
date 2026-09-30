"""HikCentral Professional (HCP) — Hikvision's on-premises security platform.

HCP runs on the customer's Windows server and manages their Hikvision
terminals, doors and cameras. Its **OpenAPI** add-on (installed alongside
HCP, matched to its version) exposes a gateway called *Artemis* at
``https://<server>/artemis/api/…``. BioBridge must be able to reach that
server, like BioTime or BioStar 2 (port forward, VPN, or a public address).

Signing
-------
Every call is a JSON ``POST`` signed with the partner's API key (AK) and
secret (SK) created when OpenAPI is set up — no session, no token::

    string_to_sign = "POST\\n*/*\\napplication/json\\n"
                     "x-ca-key:<AK>\\nx-ca-nonce:<uuid>\\nx-ca-timestamp:<ms>\\n"
                     "/artemis/api/…"
    X-Ca-Signature = base64(HMAC-SHA256(SK, string_to_sign))

sent with ``X-Ca-Key``, ``X-Ca-Nonce``, ``X-Ca-Timestamp`` and
``X-Ca-Signature-Headers: x-ca-key,x-ca-nonce,x-ca-timestamp``. Replies are
``{"code": "0", "msg": …, "data": {…}}``.

Calls used
----------
``/artemis/api/resource/v1/person/personList``  ``{pageNo, pageSize}``
    → ``data.list[{personId, personCode, personGivenName, personFamilyName…}]``.
    Door events name the person by HCP's internal ``personId``; the
    ``personCode`` (HCP's *Employee ID*) is what matches Odoo, so the list is
    read once per run, only if there are events to map.
``/artemis/api/acs/v1/door/events``  ``{startTime, endTime, pageNo, pageSize}``
    → ``data.list[{eventId, eventType, eventTime, personId, doorName,
    doorIndexCode, inAndOutType…}]``. Only events with a person are kept.
    If an HCP version insists on an ``eventType``, the common "access
    granted" types are asked for one at a time.

Built from Hikvision's published OpenAPI reference and public working
signing code; tested against a simulated server (tests/test_hikcentral.py)
that checks the signature, not a live HCP. Pilot first.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
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

log = logging.getLogger(__name__)

#: Tests point this at an httpx.MockTransport standing in for the server.
TRANSPORT: httpx.BaseTransport | None = None

API = "/artemis/api"
ACCEPT = "*/*"
CONTENT_TYPE = "application/json"
SIGNED_HEADERS = "x-ca-key,x-ca-nonce,x-ca-timestamp"
PAGE_SIZE = 100
MAX_PAGES = 500
MAX_DAYS = 31
#: "Access granted" event types (card, face) — asked for one by one only when
#: the server refuses a search without an eventType.
GRANTED_EVENT_TYPES = (198914, 196893)
DIRECTION = {"1": True, "0": False}     # inAndOutType: 1 = entry, 0 = exit


class HikCentralError(ProviderError):
    pass


class HikCentralRefused(HikCentralError):
    """The server understood the request and said no (not a transport or login failure)."""


def string_to_sign(path: str, key: str, nonce: str, timestamp: str) -> str:
    return (f"POST\n{ACCEPT}\n{CONTENT_TYPE}\n"
            f"x-ca-key:{key}\nx-ca-nonce:{nonce}\nx-ca-timestamp:{timestamp}\n{path}")


def sign(secret: str, text: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), text.encode(), hashlib.sha256).digest()).decode()


def parse_time(value: Any, zone: ZoneInfo) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


@register
class HikCentralProvider(AttendanceProvider):
    slug = "hikcentral"
    label = "HikCentral Professional"
    description = (
        "Hikvision's on-premises platform, which manages your Hikvision terminals "
        "and doors. BioBridge reads its access events through the HCP OpenAPI add-on."
    )
    capabilities = frozenset({Capability.READ_PUNCHES})
    kinds = frozenset({"platform"})
    config_fields = (
        {"name": "base_url", "label": "Server URL", "type": "text", "required": True,
         "help": "Where HCP's OpenAPI answers, e.g. https://hcp.example.com (add :port if not 443)."},
        {"name": "username", "label": "Partner key (AK)", "type": "text", "required": True,
         "help": "The API key of the OpenAPI partner created for BioBridge."},
        {"name": "password", "label": "Partner secret (SK)", "type": "password", "required": True},
        {"name": "server_timezone", "label": "Server timezone", "type": "timezone",
         "required": True, "default": "UTC", "help": "The zone the HCP server runs in."},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        base = (config.base_url or "").strip().rstrip("/")
        if base and "://" not in base:
            base = f"https://{base}"
        for suffix in (API, "/artemis"):
            if base.lower().endswith(suffix):
                base = base[: -len(suffix)]
        self.base = base
        self.host = urlparse(base).hostname or "hcp"
        self.tz = config.timezone or "UTC"
        self._client: httpx.Client | None = None
        self._people: dict[str, dict] | None = None

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

    def _post(self, endpoint: str, body: dict) -> dict:
        path = f"{API}/{endpoint}"
        key, secret = self.config.username or "", self.config.password or ""
        nonce, stamp = str(uuid.uuid4()), str(int(time.time() * 1000))
        headers = {
            "Accept": ACCEPT, "Content-Type": CONTENT_TYPE,
            "X-Ca-Key": key, "X-Ca-Nonce": nonce, "X-Ca-Timestamp": stamp,
            "X-Ca-Signature-Headers": SIGNED_HEADERS,
            "X-Ca-Signature": sign(secret, string_to_sign(path, key, nonce, stamp)),
        }
        try:
            response = self._http().post(self.base + path, content=json.dumps(body).encode(), headers=headers)
        except httpx.RequestError as exc:
            where = urlparse(self.base).netloc or self.base
            raise HikCentralError(f"Cannot reach HikCentral at {where}: {type(exc).__name__}. Check the "
                                  "address, and that this server can reach it (VPN or forwarded port).") from exc
        if response.status_code in (401, 403):
            raise HikCentralError("HikCentral refused the partner key or secret. Check the AK/SK of the "
                                  "OpenAPI partner, and that the server's clock is right (signatures expire).")
        if response.status_code == 404:
            raise HikCentralError(f"No HikCentral OpenAPI at {self.base}/artemis — is the OpenAPI add-on "
                                  "installed and running, and is this the right address and port?")
        try:
            data = response.json()
        except ValueError as exc:
            raise HikCentralError(f"HikCentral sent an unreadable reply (HTTP {response.status_code}).") from exc
        code = _text(data.get("code", response.status_code if response.status_code >= 400 else "0"))
        if code != "0":
            message = data.get("msg") or data.get("message") or f"code {code}"
            if response.status_code >= 500:
                raise HikCentralError(f"HikCentral failed: {message} (code {code})")
            raise HikCentralRefused(f"HikCentral refused {endpoint}: {message} (code {code})")
        return data.get("data") or {}

    # -- people ---------------------------------------------------------------
    def _load_people(self) -> dict[str, dict]:
        if self._people is None:
            people: dict[str, dict] = {}
            for page in range(1, MAX_PAGES + 1):
                data = self._post("resource/v1/person/personList", {"pageNo": page, "pageSize": PAGE_SIZE})
                rows = data.get("list") or []
                for row in rows:
                    pid = _text(row.get("personId"))
                    if pid:
                        people[pid] = row
                total = int(data.get("total") or 0)
                if not rows or len(rows) < PAGE_SIZE or (total and page * PAGE_SIZE >= total):
                    break
            self._people = people
        return self._people

    # -- events ---------------------------------------------------------------
    def _events(self, start: datetime, end: datetime, page: int, size: int, event_type: int | None) -> dict:
        body: dict[str, Any] = {"startTime": start.isoformat(timespec="seconds"),
                                "endTime": end.isoformat(timespec="seconds"), "pageNo": page, "pageSize": size}
        if event_type is not None:
            body["eventType"] = event_type
        return self._post("acs/v1/door/events", body)

    def _event_rows(self, start: datetime, end: datetime) -> Iterator[dict]:
        try:
            yield from self._paged(start, end, None)
            return
        except HikCentralRefused as exc:
            log.info("HikCentral %s: search without eventType refused (%s); asking per type", self.host, exc)
        for event_type in GRANTED_EVENT_TYPES:
            yield from self._paged(start, end, event_type)

    def _paged(self, start: datetime, end: datetime, event_type: int | None) -> Iterator[dict]:
        for page in range(1, MAX_PAGES + 1):
            data = self._events(start, end, page, PAGE_SIZE, event_type)
            rows = data.get("list") or []
            yield from (r for r in rows if isinstance(r, dict))
            total = int(data.get("total") or 0)
            if not rows or len(rows) < PAGE_SIZE or (total and page * PAGE_SIZE >= total):
                return
        log.warning("HikCentral %s: stopped paging after %d pages", self.host, MAX_PAGES)

    # -- interface -----------------------------------------------------------
    def test_connection(self) -> ConnectionInfo:
        zone = ZoneInfo(self.tz)
        people = self._post("resource/v1/person/personList", {"pageNo": 1, "pageSize": 1})
        now = datetime.now(zone).replace(microsecond=0)
        try:
            events = self._events(now - timedelta(days=1), now, 1, 1, None)
        except HikCentralRefused:
            events = self._events(now - timedelta(days=1), now, 1, 1, GRANTED_EVENT_TYPES[0])
        persons, count = int(people.get("total") or 0), int(events.get("total") or 0)
        return ConnectionInfo(True, f"Connected to HikCentral — {persons} person{'' if persons == 1 else 's'}, "
                                    f"{count} door event{'' if count == 1 else 's'} in the last 24 hours",
                              {"persons": persons, "events_24h": count})

    def fetch_punches(self, since=None, until=None) -> Iterator[PunchEvent]:
        zone = ZoneInfo(self.tz)
        now = datetime.now(zone).replace(microsecond=0)
        end = until.replace(tzinfo=zone) if until else now
        start = since.replace(tzinfo=zone) if since else end - timedelta(days=1)
        start = max(start, end - timedelta(days=MAX_DAYS))
        seen: set[str] = set()
        unknown: set[str] = set()
        for row in self._event_rows(start, end):
            pid = _text(row.get("personId"))
            when = parse_time(row.get("eventTime"), zone)
            if not pid or pid in ("0", "-1") or when is None:
                continue                       # a door or alarm event, not a person
            event_id = _text(row.get("eventId")) or f"{pid}:{when.astimezone(timezone.utc):%Y%m%d%H%M%S}"
            if event_id in seen:
                continue                       # the per-type fallback can overlap
            seen.add(event_id)
            person = self._load_people().get(pid)
            code = _text((person or {}).get("personCode")) or _text(row.get("personCode")) or _text(row.get("jobNo"))
            if not code:
                unknown.add(pid)
                continue
            door = _text(row.get("doorIndexCode")) or _text(row.get("readerDevIndexCode"))
            door_name = _text(row.get("doorName")) or _text(row.get("readerDevName"))
            terminal = _text(row.get("devSerialNo")) or (f"{self.host}:door{door}" if door else None)
            yield PunchEvent(
                external_id=f"{self.host}:{event_id}",
                emp_code=code,
                punch_time_local=when.astimezone(zone).replace(tzinfo=None),
                direction=DIRECTION.get(_text(row.get("inAndOutType"))),
                terminal_sn=terminal,
                terminal_alias=door_name or None,
                first_name=_text((person or {}).get("personGivenName")) or None,
                last_name=_text((person or {}).get("personFamilyName")) or None,
                raw={"punch_state": _text(row.get("inAndOutType")), "event_type": row.get("eventType")},
            )
        if unknown:
            log.warning("HikCentral %s: %d person(s) with events have no Employee ID (personCode) in HCP",
                        self.host, len(unknown))
