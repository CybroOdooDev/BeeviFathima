"""Anviz CrossChex Cloud — Anviz's hosted attendance service.

Anviz terminals (FaceDeep, W-series, VF30, …) report to CrossChex Cloud over
the internet, so there is nothing on the customer's network to reach: one
connection is the customer's CrossChex Cloud account, identified by the API
key and secret created under its API settings. Every call is a ``POST`` to
the region's API root with one JSON body carrying ``header`` (``nameSpace``,
``nameAction``, ``version``, ``requestId``, ``timestamp``), ``authorize``
(the token, for data calls) and ``payload``:

``authorize.token / token``
    ``{"api_key", "api_secret"}`` → ``{"token", "expires"}``.
``attendance.record / getrecord``
    ``{"begin_time", "end_time", "order": "asc", "page", "per_page"}``
    (ISO-8601 with offset, up to 1000 per page) → ``{"count", "pageCount",
    "page", "list": [{"checktime", "checktype", "device": {"serial_number",
    "name"}, "employee": {"workno", "first_name", "last_name"}}]}``.

Terminals are learnt from the punches themselves (each carries its device),
so there is no separate import. ``checktype`` 0 = in, 1 = out; anything else
(the common 128, no state key pressed) leaves direction to pairing.

Built from Anviz's API guide as posted on the Anviz community; tested against
a simulated service (tests/test_crosschex.py), not a live account. Pilot first.
"""

from __future__ import annotations

import logging
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

#: Tests point this at an httpx.MockTransport standing in for the service.
TRANSPORT: httpx.BaseTransport | None = None

REGIONS = {
    "us": "https://api.us.crosschexcloud.com",
    "eu": "https://api.eu.crosschexcloud.com",
    "ap": "https://api.ap.crosschexcloud.com",
}
PAGE_SIZE = 1000
MAX_PAGES = 500
DIRECTION = {0: True, 1: False}


class CrossChexError(ProviderError):
    pass


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@register
class CrossChexProvider(AttendanceProvider):
    slug = "crosschex"
    label = "Anviz CrossChex Cloud"
    description = (
        "Anviz's cloud service, which Anviz terminals (FaceDeep, W-series, VF30…) "
        "report to over the internet. Connect with an API key — nothing on your "
        "network to open."
    )
    capabilities = frozenset({Capability.READ_PUNCHES})
    kinds = frozenset({"platform"})
    config_fields = (
        {"name": "base_url", "label": "Region", "type": "text", "required": True,
         "help": "Your CrossChex Cloud region's API address (US, EU or Asia-Pacific)."},
        {"name": "username", "label": "API Key", "type": "text", "required": True,
         "help": "CrossChex Cloud → Settings → API: the API key."},
        {"name": "password", "label": "API Secret", "type": "password", "required": True},
        {"name": "server_timezone", "label": "Site Timezone", "type": "timezone",
         "required": True, "default": "UTC",
         "help": "CrossChex Cloud sends times with their offset; this is the zone they're shown in."},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        base = (config.base_url or "").strip().rstrip("/")
        base = REGIONS.get(base.lower(), base)
        if base and "://" not in base:
            base = f"https://{base}"
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

    def _post(self, namespace: str, action: str, payload: dict, token: str | None = None) -> dict:
        body: dict[str, Any] = {
            "header": {"nameSpace": namespace, "nameAction": action, "version": "1.0",
                       "requestId": str(uuid.uuid4()),
                       "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds")},
            "payload": payload,
        }
        if token:
            body["authorize"] = {"type": "token", "token": token}
        try:
            response = self._http().post(self.base + "/", json=body)
        except httpx.RequestError as exc:
            where = urlparse(self.base).netloc or self.base
            raise CrossChexError(f"Cannot reach CrossChex Cloud at {where}: {type(exc).__name__}. "
                                 "Check the region and this server's internet access.") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise CrossChexError(f"CrossChex Cloud sent an unreadable reply (HTTP {response.status_code}).") from exc
        code = data.get("code", response.status_code)
        if response.status_code >= 400 or str(code) not in ("200", "0"):
            message = data.get("message") or data.get("msg") or ((data.get("data") or {}).get("message")) or f"code {code}"
            raise CrossChexError(str(message))
        return ((data.get("data") or {}).get("payload")) or data.get("payload") or {}

    def _authenticate(self) -> str:
        try:
            payload = self._post("authorize.token", "token", {
                "api_key": self.config.username or "", "api_secret": self.config.password or ""})
        except CrossChexError as exc:
            raise CrossChexError(f"CrossChex Cloud refused the API key or secret ({exc}). "
                                 "Check them, and that the region matches your account.") from exc
        token = payload.get("token")
        if not token:
            raise CrossChexError("CrossChex Cloud accepted the key but sent no token.")
        self._token = token
        return token

    def _data(self, namespace: str, action: str, payload: dict) -> dict:
        if not self._token:
            self._authenticate()
        try:
            return self._post(namespace, action, payload, self._token)
        except CrossChexError as exc:
            text = str(exc).lower()
            if "token" in text or "expire" in text or "auth" in text or "401" in text:
                self._authenticate()  # expired: once more with a fresh token
                return self._post(namespace, action, payload, self._token)
            raise

    # -- interface -----------------------------------------------------------
    def test_connection(self) -> ConnectionInfo:
        self._token = None
        self._authenticate()
        now = datetime.now(timezone.utc)
        payload = self._data("attendance.record", "getrecord", {
            "begin_time": (now - timedelta(days=1)).isoformat(timespec="seconds"),
            "end_time": now.isoformat(timespec="seconds"), "order": "asc", "page": 1, "per_page": 1})
        count = int(payload.get("count") or 0)
        return ConnectionInfo(True, f"Connected to CrossChex Cloud — {count} punch{'' if count == 1 else 'es'} "
                                    "in the last 24 hours", {"records_24h": count})

    def fetch_punches(self, since=None, until=None) -> Iterator[PunchEvent]:
        zone = ZoneInfo(self.tz)
        now = datetime.now(timezone.utc)
        start = since.replace(tzinfo=zone) if since else now - timedelta(days=1)
        end = until.replace(tzinfo=zone) if until else now
        page = 1
        for _ in range(MAX_PAGES):
            payload = self._data("attendance.record", "getrecord", {
                "begin_time": start.isoformat(timespec="seconds"), "end_time": end.isoformat(timespec="seconds"),
                "order": "asc", "page": page, "per_page": PAGE_SIZE})
            rows = payload.get("list") or []
            for row in rows:
                employee = row.get("employee") or {}
                device = row.get("device") or {}
                workno = str(employee.get("workno") or "").strip()
                when = parse_time(row.get("checktime"))
                if not workno or when is None:
                    continue
                serial = str(device.get("serial_number") or "").strip() or None
                try:
                    checktype = int(row.get("checktype"))
                except (TypeError, ValueError):
                    checktype = None
                yield PunchEvent(
                    external_id=str(row.get("uuid") or f"{serial or 'cc'}:{workno}:{when:%Y%m%d%H%M%S}"),
                    emp_code=workno,
                    punch_time_local=when.astimezone(zone).replace(tzinfo=None),
                    direction=DIRECTION.get(checktype),
                    terminal_sn=serial,
                    terminal_alias=str(device.get("name") or "").strip() or None,
                    first_name=str(employee.get("first_name") or "").strip() or None,
                    last_name=str(employee.get("last_name") or "").strip() or None,
                    raw={"punch_state": "" if checktype is None else str(checktype)},
                )
            pages = int(payload.get("pageCount") or 0)
            if not rows or page >= pages or len(rows) < PAGE_SIZE:
                return
            page += 1
        log.warning("CrossChex %s: stopped paging after %d pages", self.base, MAX_PAGES)
