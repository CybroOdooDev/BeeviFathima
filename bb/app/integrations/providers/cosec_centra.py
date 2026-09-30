"""Matrix COSEC CENTRA — the COSEC server, which collects every panel's punches.

CENTRA (and the older COSEC server) exposes an HTTP API on the server as a
WCF service, ``<server>/cosec/api.svc/<module>?action=get;…``. Parameters are
separated by **semicolons**, not ampersands. Only the System Administrator
account may call it, so the login is always ``sa``. From Matrix's technical
mailers (MTSM-11, MTSM-18):

``event-ta-date?action=get;daterange=DDMMYYYYHHMMSS-DDMMYYYYHHMMSS;format=xml``
    Time-attendance events in a window — what this provider reads, one day at
    a time so a first 30-day backfill never asks for a month in one reply.
``eventta?action=get;index=1;count=100;format=xml``
    The same events by database index (not used: the window form fits the
    sync engine's overlap-and-dedupe model directly).

The response columns are not fixed: they come from an **API template** the
customer defines in COSEC (Admin → Utility → API Configuration). So instead
of expecting exact tag names, each record's fields are recognised by name:
a user id (``UserID``, ``user-id``, ``EmpCode``…), a date and time (one
``…DateTime`` field, or separate ``…Date`` and ``…Time``), optionally an
index number, entry/exit and the device. The setup form tells the customer
which fields the template needs.

Built from Matrix's published mailers; tested against a simulated server with
two different templates (tests/test_cosec_centra.py), not a live CENTRA.
Pilot first.
"""

from __future__ import annotations

import logging
import re
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
    ProviderError,
    PunchEvent,
    SourceConfig,
    register,
)

log = logging.getLogger(__name__)

#: Tests point this at an httpx.MockTransport standing in for the server.
TRANSPORT: httpx.BaseTransport | None = None

MAX_DAYS = 45   # a guard on one run's window


class CentraError(ProviderError):
    pass


def _norm(tag: str) -> str:
    return re.sub(r"[^a-z]", "", tag.lower())


# Recognised field names, normalised (lower-case letters only).
USER_KEYS = ("userid", "user", "empid", "empcode", "employeeid", "employeecode", "referencecode", "refid", "id")
DATETIME_KEYS = ("eventdatetime", "edatetime", "datetime", "punchdatetime", "eventtime")
DATE_KEYS = ("eventdate", "edate", "date", "punchdate")
TIME_KEYS = ("eventtime", "etime", "time", "punchtime")
INDEX_KEYS = ("indexno", "index", "eventindex", "idx", "serialno", "srno")
DIRECTION_KEYS = ("entryexittype", "entryexit", "iotype", "inout", "direction", "entryexitmode")
DEVICE_KEYS = ("devicename", "device", "panelname", "panel", "doorname", "door", "deviceid", "mid")
NAME_KEYS = ("username", "name", "empname", "employeename")

_IN = {"0", "in", "entry", "i", "checkin"}
_OUT = {"1", "out", "exit", "o", "checkout"}


def _pick(fields: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = fields.get(key)
        if value:
            return value
    return ""


def records(text: str) -> list[dict[str, str]]:
    """Every element whose children are all leaves is one record, fields
    keyed by normalised tag. Tolerates whatever wrapper tags a template or
    version uses."""
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError:
        raise CentraError(f"COSEC sent something that isn't XML: {text.strip()[:120]!r}")
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    # An error reply is a flat <COSEC_API><Response-Code>n</Response-Code>…
    top = {_norm(c.tag): (c.text or "").strip() for c in root if len(c) == 0}
    code = top.get("responsecode")
    if code not in (None, "", "0"):
        detail = top.get("message") or top.get("responsemessage") or top.get("description") or ""
        raise CentraError(f"COSEC answered with error code {code}{': ' + detail if detail else ''}. "
                          "Check that the API template exists and the sa account may use the API.")
    out = []
    for el in root.iter():
        if el is root and "responsecode" in top:
            continue
        children = list(el)
        if len(children) >= 2 and all(len(c) == 0 for c in children):
            out.append({_norm(c.tag): (c.text or "").strip() for c in children})
    return out


def parse_when(fields: dict[str, str]) -> datetime | None:
    combined = _pick(fields, DATETIME_KEYS)
    candidates = [combined] if combined and re.search(r"\d{1,2}:\d{2}", combined) else []
    date, time_ = _pick(fields, DATE_KEYS), _pick(fields, TIME_KEYS)
    if date and time_ and not re.search(r"\d{1,2}:\d{2}", date):
        candidates.append(f"{date} {time_}")
    elif date and re.search(r"\d{1,2}:\d{2}", date):
        candidates.append(date)
    for text in candidates:
        text = text.replace("T", " ").strip()
        for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d%m%Y %H%M%S", "%d/%m/%Y %I:%M:%S %p",
                    "%d/%m/%Y %I:%M %p"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def direction_of(value: str) -> bool | None:
    key = (value or "").strip().lower()
    return True if key in _IN else False if key in _OUT else None


def fmt_range(start: datetime, end: datetime) -> str:
    return f"{start:%d%m%Y%H%M%S}-{end:%d%m%Y%H%M%S}"


@register
class CosecCentraProvider(AttendanceProvider):
    slug = "cosec_centra"
    label = "Matrix COSEC CENTRA"
    description = (
        "A Matrix COSEC CENTRA (or COSEC server) installation, which collects "
        "punches from all its panels and terminals."
    )
    capabilities = frozenset({Capability.READ_PUNCHES})
    kinds = frozenset({"platform"})
    config_fields = (
        {"name": "base_url", "label": "Server URL", "type": "text", "required": True,
         "help": "Where COSEC runs, e.g. http://cosec-server/cosec (the part before /api.svc)."},
        {"name": "username", "label": "Username", "type": "text", "required": True, "default": "sa",
         "help": "The COSEC API accepts only the System Administrator account (sa)."},
        {"name": "password", "label": "Password", "type": "password", "required": True},
        {"name": "server_timezone", "label": "Server timezone", "type": "timezone",
         "required": True, "default": "UTC", "help": "The zone the COSEC server's clock runs in."},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        base = (config.base_url or "").strip().rstrip("/")
        if base and "://" not in base:
            base = f"http://{base}"
        parsed = urlparse(base)
        if "api.svc" in parsed.path.lower():
            self.api = base
        else:
            path = parsed.path or "/cosec"
            self.api = f"{parsed.scheme}://{parsed.netloc}{path.rstrip('/')}/api.svc"
        self.tz = config.timezone or "UTC"
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(auth=(self.config.username or "sa", self.config.password or ""),
                                        verify=self.config.verify_ssl,
                                        timeout=settings.http_timeout_seconds, transport=TRANSPORT)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _get(self, module: str, params: dict[str, Any]) -> str:
        # Semicolon-separated, as the COSEC API expects — built by hand, since
        # an HTTP client would encode them as &-separated query parameters.
        query = ";".join(f"{k}={v}" for k, v in {"action": "get", **params}.items())
        url = f"{self.api}/{module}?{query}"
        try:
            response = self._http().get(url)
        except httpx.RequestError as exc:
            where = urlparse(self.api).netloc
            raise CentraError(f"Cannot reach the COSEC server at {where}: {type(exc).__name__}. "
                              "Check the address and that this server can reach it.") from exc
        if response.status_code in (401, 403):
            raise CentraError("COSEC refused the login. The API accepts only the System Administrator "
                              "account (sa) — check its password, and that API access is enabled.")
        if response.status_code == 404:
            raise CentraError(f"No COSEC API at {self.api}. Check the Server URL (usually "
                              "http://<server>/cosec) and that the COSEC API service is installed.")
        if response.status_code >= 400:
            raise CentraError(f"COSEC refused the request (HTTP {response.status_code}): {response.text.strip()[:120]}")
        return response.text

    def _window(self, start: datetime, end: datetime) -> list[dict[str, str]]:
        return records(self._get("event-ta-date", {"daterange": fmt_range(start, end), "format": "xml"}))

    def test_connection(self) -> ConnectionInfo:
        now = datetime.now(ZoneInfo(self.tz)).replace(tzinfo=None, microsecond=0)
        rows = self._window(now - timedelta(days=1), now)
        if rows and not all(_pick(r, USER_KEYS) and parse_when(r) for r in rows[:5]):
            return ConnectionInfo(False, "Connected, but the API template is missing fields BioBridge needs. "
                                         "In COSEC: Admin → Utility → API Configuration → T&A events template — "
                                         "include User ID and Event Date/Time (and Entry/Exit, Device).",
                                  {"fields": sorted(rows[0])})
        return ConnectionInfo(True, f"Connected to COSEC — {len(rows)} event{'' if len(rows) == 1 else 's'} "
                                    "in the last 24 hours", {"events_24h": len(rows)})

    def fetch_punches(self, since=None, until=None) -> Iterator[PunchEvent]:
        now = datetime.now(ZoneInfo(self.tz)).replace(tzinfo=None, microsecond=0)
        end = until or now
        start = since or end - timedelta(days=1)
        start = max(start, end - timedelta(days=MAX_DAYS))
        host = urlparse(self.api).hostname or "cosec"
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=1), end)
            for row in self._window(cursor, chunk_end):
                user = _pick(row, USER_KEYS)
                when = parse_when(row)
                if not user or when is None:
                    continue
                index = _pick(row, INDEX_KEYS)
                device = _pick(row, DEVICE_KEYS)
                name = _pick(row, NAME_KEYS)
                first, _, last = name.partition(" ")
                yield PunchEvent(
                    external_id=f"{host}:{index}" if index else f"{host}:{user}:{when:%Y%m%d%H%M%S}:{device}",
                    emp_code=user,
                    punch_time_local=when,
                    direction=direction_of(_pick(row, DIRECTION_KEYS)),
                    terminal_sn=device or None,
                    terminal_alias=device or None,
                    first_name=first or None,
                    last_name=last or None,
                    raw={"punch_state": _pick(row, DIRECTION_KEYS), "fields": row},
                )
            cursor = chunk_end
