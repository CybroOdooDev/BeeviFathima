"""Cams Biometrics — the Cams Biometric Gateway (Web API 3.0).

Cams Biometrics makes its own terminals (SlimBeast, Macronium, Hawking,
Ultron, Galaxy…) and, through its gateway, also fronts 100+ other brands
(ZKTeco, eSSL, Suprema, Hikvision, Anviz, Matrix…). The device talks to the
Cams cloud; applications talk to the cloud in one JSON format. Each
registered device has its own **Service Tag ID** (``stgid``) and **AuthToken**
(32 characters), shown in the customer's *API Monitor* account — so one
BioBridge connection is one device.

BioBridge uses the gateway's RESTful side (application → Cams), not the
real-time callback, which fits the sync engine's pull-and-dedupe model and
needs no inbound port on BioBridge:

``POST <endpoint>?stgid=<service tag id>``, JSON body, always with
``AuthToken``, ``OperationID`` and ``Time`` (``YYYY-MM-DD HH:mm:ss GMT +0530``).

``Load.PunchLog.Filter {StartTime, EndTime}`` (operation 12, LoadLog)
    → ``{"Status": "done", "PunchLog": {"ReturnRowCount", "Log": [{"Type",
    "InputType", "UserID", "LogTime", "Temperature", "FaceMask"}]}}``.
    Cams recommends at most 30 days per request; BioBridge asks a week at a
    time. ``Type`` is CheckIn/CheckOut, BreakIn/BreakOut, OverTimeIn/Out,
    MealIn/Out; the *In* kinds are entries, the *Out* kinds exits.
``Load.DeviceInformation "All"`` (operation 13)
    → model, MAC, licence dates, user counts. Names the terminal.

Things a customer has to know
-----------------------------
* Cams only accepts REST calls from origins registered in the API Monitor
  ("Invalid Origin IP", status 3): BioBridge's server address must be added
  there.
* A REST call takes about 15 seconds to reach the device's record, so a sync
  of one device is slow-ish; syncs are best spaced at 5+ minutes.
* Punches held while a device is offline arrive when it reconnects; the
  overlap-and-dedupe window catches them.

Built from Cams' published Web API 3.0 documentation and tested against a
simulated gateway (tests/test_cams.py), not a live account. Pilot first. The
real-time callback (device → BioBridge) is not implemented.
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
    TerminalRecord,
    register,
)

log = logging.getLogger(__name__)

#: Tests point this at an httpx.MockTransport standing in for the gateway.
TRANSPORT: httpx.BaseTransport | None = None

CHUNK_DAYS = 7
MAX_DAYS = 31
_IN = {"checkin", "breakin", "overtimein", "mealin"}
_OUT = {"checkout", "breakout", "overtimeout", "mealout"}

STATUS_HELP = {
    1: "Cams could not read the request — please report this.",
    2: "Cams doesn't know that Service Tag ID. Copy it again from your API Monitor account.",
    3: "Cams refused the request's origin. Add this BioBridge server's address as an allowed origin in your "
       "API Monitor account.",
    5: "The device has no valid Cams subscription (or is offline). Check its subscription in API Monitor.",
    7: "Cams refused the AuthToken. Copy it again from your API Monitor account.",
    29: "The API version configured for this device in API Monitor isn't 3.0.",
    35: "That operation isn't allowed for this device in API Monitor.",
    37: "Cams is already handling a call for this device group — try again in a minute.",
    38: "Reading logs over the REST API isn't allowed for this device in API Monitor.",
}


class CamsError(ProviderError):
    pass


def fmt_time(moment: datetime) -> str:
    """``2020-09-17 12:01:33 GMT +0530`` — the gateway's time format."""
    offset = moment.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{moment:%Y-%m-%d %H:%M:%S} GMT {sign}{abs(minutes) // 60:02d}{abs(minutes) % 60:02d}"


def parse_time(text: Any) -> datetime | None:
    """Inverse of ``fmt_time`` → an aware datetime, or None."""
    raw = str(text or "").strip()
    if not raw:
        return None
    body, _, offset = raw.partition(" GMT")
    try:
        moment = datetime.strptime(body.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    offset = offset.strip()
    if len(offset) >= 5 and offset[0] in "+-" and offset[1:5].isdigit():
        delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        return moment.replace(tzinfo=timezone(delta if offset[0] == "+" else -delta))
    return moment.replace(tzinfo=timezone.utc)


def direction_of(kind: Any) -> bool | None:
    key = str(kind or "").replace("_", "").replace(" ", "").lower()
    return True if key in _IN else False if key in _OUT else None


@register
class CamsProvider(AttendanceProvider):
    slug = "cams"
    label = "Cams Biometrics (cloud gateway)"
    description = (
        "A Cams Biometrics terminal — or another brand connected through the "
        "Cams Biometric Gateway — read through its cloud Web API."
    )
    capabilities = frozenset({Capability.READ_PUNCHES, Capability.LIST_TERMINALS})
    kinds = frozenset({"device"})
    config_fields = (
        {"name": "base_url", "label": "Endpoint URL", "type": "text", "required": True,
         "help": "The RESTful endpoint URL shown in your Cams API Monitor account."},
        {"name": "username", "label": "Service Tag ID", "type": "text", "required": True,
         "help": "The device's stgid in API Monitor."},
        {"name": "password", "label": "AuthToken", "type": "password", "required": True,
         "help": "The 32-character token set for this device in API Monitor."},
        {"name": "server_timezone", "label": "Device timezone", "type": "timezone",
         "required": True, "default": "UTC", "help": "The zone the device's clock is set to."},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        base = (config.base_url or "").strip()
        if base and "://" not in base:
            base = f"https://{base}"
        parsed = urlparse(base)
        # An endpoint pasted with its ?stgid=… is fine: the id is added here.
        self.endpoint = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/") if parsed.netloc else base
        self.stgid = (config.username or "").strip()
        self.tz = config.timezone or "UTC"
        self._client: httpx.Client | None = None

    # -- transport ------------------------------------------------------------
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=max(settings.http_timeout_seconds, 45),
                                        verify=self.config.verify_ssl, transport=TRANSPORT)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _call(self, body: dict) -> dict:
        now = datetime.now(ZoneInfo(self.tz))
        payload = {**body, "OperationID": uuid.uuid4().hex[:12], "AuthToken": self.config.password or "",
                   "Time": fmt_time(now)}
        try:
            response = self._http().post(self.endpoint, params={"stgid": self.stgid}, json=payload)
        except httpx.RequestError as exc:
            where = urlparse(self.endpoint).netloc or self.endpoint
            raise CamsError(f"Cannot reach the Cams gateway at {where}: {type(exc).__name__}. "
                            "Check the endpoint URL and this server's internet access.") from exc
        if response.status_code == 404:
            raise CamsError(f"No Cams API at {self.endpoint}. Check the endpoint URL from API Monitor.")
        if response.status_code >= 400:
            raise CamsError(f"The Cams gateway refused the request (HTTP {response.status_code}).")
        try:
            data = response.json()
        except ValueError as exc:
            raise CamsError("The Cams gateway sent an unreadable reply — is the endpoint URL right?") from exc
        code = data.get("StatusCode", 0 if str(data.get("Status", "")).lower() == "done" else 999)
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = 999
        if code != 0 or str(data.get("Status", "done")).lower() == "error":
            raise CamsError(STATUS_HELP.get(code) or f"Cams reported an error (status {code})"
                            f"{' — operation ' + str(data.get('OperationID')) if data.get('OperationID') else ''}.")
        return data

    def _log(self, start: datetime, end: datetime) -> list[dict]:
        data = self._call({"Load": {"PunchLog": {"Filter": {"StartTime": fmt_time(start), "EndTime": fmt_time(end)}}}})
        return (data.get("PunchLog") or {}).get("Log") or []

    # -- interface -----------------------------------------------------------
    def _device_information(self) -> dict:
        try:
            return self._call({"Load": {"DeviceInformation": "All"}}).get("DeviceInformation") or {}
        except CamsError:
            raise

    def test_connection(self) -> ConnectionInfo:
        info = self._device_information()
        now = datetime.now(ZoneInfo(self.tz)).replace(microsecond=0)
        rows = self._log(now - timedelta(days=1), now)
        model = info.get("DeviceModel") or "Cams device"
        return ConnectionInfo(True, f"Connected to {model} — {len(rows)} punch{'' if len(rows) == 1 else 'es'} "
                                    "in the last 24 hours", {"device_model": model, "punches_24h": len(rows)})

    def fetch_terminals(self) -> Iterator[TerminalRecord]:
        try:
            info = self._device_information()
        except CamsError:
            info = {}   # the punches are what matter; the terminal still gets recorded
        return iter([TerminalRecord(serial_number=self.stgid, alias=info.get("DeviceModel") or None,
                                    ip_address=None, model=info.get("DeviceModel") or "Cams Biometrics")])

    def fetch_punches(self, since=None, until=None) -> Iterator[PunchEvent]:
        zone = ZoneInfo(self.tz)
        now = datetime.now(zone).replace(microsecond=0)
        end = until.replace(tzinfo=zone) if until else now
        start = since.replace(tzinfo=zone) if since else end - timedelta(days=1)
        start = max(start, end - timedelta(days=MAX_DAYS))
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
            for row in self._log(cursor, chunk_end):
                user = str(row.get("UserID") or row.get("UserId") or "").strip()
                when = parse_time(row.get("LogTime"))
                if not user or when is None:
                    continue
                kind = row.get("Type")
                utc = when.astimezone(timezone.utc)
                yield PunchEvent(
                    external_id=f"{self.stgid}:{user}:{utc:%Y%m%d%H%M%S}",
                    emp_code=user,
                    punch_time_local=when.astimezone(zone).replace(tzinfo=None),
                    direction=direction_of(kind),
                    terminal_sn=self.stgid,
                    verify_type=str(row.get("InputType") or "") or None,
                    raw={"punch_state": str(kind or ""), "input_type": row.get("InputType"),
                         "temperature": row.get("Temperature")},
                )
            cursor = chunk_end
