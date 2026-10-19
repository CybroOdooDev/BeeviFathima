"""ZKTeco BioTime (8.0 / 8.5 / 9.x) as an AttendanceProvider.

Endpoints used
--------------
POST /api-token-auth/            {username, password} -> {"token": "..."}
POST /jwt-api-token-auth/        the JWT flavour on 8.5+
GET  /iclock/api/transactions/   ?page&page_size&emp_code&start_time&end_time
GET  /personnel/api/employees/   ?page&page_size
GET  /iclock/api/terminals/      ?page&page_size

All list endpoints paginate DRF-style:
    {"count": N, "next": url|null, "previous": ..., "data": [...]}

``punch_time`` comes back as a naive local string in the *server's* zone. It is
left naive here on purpose — see PunchEvent.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

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

TIME_FMT = "%Y-%m-%d %H:%M:%S"

#: DeviceSource.config key recording how this server treats ``upload_time``:
#: absent until the first conclusive answer, then "from" (filters from that
#: time on, used) or "off" (ignored or exact-match, never asked again).
UPLOAD_FILTER_KEY = "biotime_upload_filter"
UPLOAD_FILTER_ON = "from"
UPLOAD_FILTER_OFF = "off"

#: Single-transaction reads per run for the id-gap check (fetch_punches_after).
MAX_ID_LOOKUPS = 200

# BioTime punch_state -> semantic direction.
STATE_IN = {"0", "3", "4"}   # Check In, Break In, Overtime In
STATE_OUT = {"1", "2", "5"}  # Check Out, Break Out, Overtime Out


class BioTimeError(ProviderError):
    """Any failure talking to BioTime."""


class BioTimeNotFound(BioTimeError):
    """HTTP 404: no such endpoint, or (for one transaction) no such record."""


class BioTimeAuthError(BioTimeError):
    """Credentials rejected, or the token expired and could not be renewed."""


@dataclass
class _Transaction:
    external_id: str
    emp_code: str
    punch_time_local: datetime
    punch_state: str
    verify_type: str | None
    terminal_sn: str | None
    terminal_alias: str | None
    first_name: str | None
    last_name: str | None
    department: str | None
    raw: dict[str, Any]

    @property
    def direction(self) -> bool | None:
        if self.punch_state in STATE_IN:
            return True
        if self.punch_state in STATE_OUT:
            return False
        return None


def _describe_transport_error(base_url: str, exc: httpx.RequestError) -> str:
    """Turn an httpx transport failure into something a customer can act on.

    These are the most common failures in production and they are almost never
    bugs: the BioTime box is off, the port is wrong, or it is on a LAN segment
    BioBridge cannot see. Left unwrapped they surface as "[Errno 111] Connection
    refused" with a stack trace, which reads like BioBridge broke.

    The cause is inferred from the httpx subclass rather than the errno string,
    because the string is platform-specific — Linux says "Connection refused",
    Windows says "No connection could be made because the target machine
    actively refused it", and the same sync must explain both.
    """
    detail = str(exc) or type(exc).__name__

    if isinstance(exc, httpx.ConnectTimeout):
        hint = (
            f"nothing answered within {settings.http_timeout_seconds}s. The host "
            "is usually up but unreachable — a firewall dropping the packets, or "
            "a VPN tunnel that is down."
        )
    elif isinstance(exc, httpx.TimeoutException):
        hint = (
            f"the server accepted the connection but did not reply within "
            f"{settings.http_timeout_seconds}s. Either it is overloaded, or the "
            "requested window is large enough that the query is slow — a shorter "
            "sync interval keeps each window small."
        )
    elif isinstance(exc, httpx.ConnectError):
        # gaierror is wrapped in ConnectError by httpx, so name resolution and a
        # refused port arrive as the same class and need separating by text.
        lowered = detail.lower()
        if "name or service not known" in lowered or "nodename nor servname" in lowered \
                or "getaddrinfo" in lowered or "temporary failure in name resolution" in lowered:
            hint = (
                "the hostname does not resolve. Check the spelling, or use the "
                "server's IP address if it has no DNS entry."
            )
        else:
            hint = (
                "the connection was refused — nothing is listening on that port. "
                "Check BioTime is running, that the port in the URL is the one it "
                "serves on, and that this host can reach it."
            )
    else:
        hint = f"the connection failed ({type(exc).__name__})."

    return f"Cannot reach BioTime at {base_url}: {hint}"


def _as_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return str(value.get("dept_name") or value.get("name") or value)
    return str(value)


@register
class BioTimeProvider(AttendanceProvider):
    slug = "biotime"
    label = "ZKTeco BioTime"
    description = (
        "BioTime 8.x / 9.x web server. Pulls punches, employees and terminals."
    )
    #: A server that collects many terminals' punches — never one device.
    #: (It once defaulted to both kinds, from before BioBridge had real
    #: standalone-device protocols; tools/fix_connection_kinds.py re-files
    #: any connection saved that way.)
    kinds = frozenset({"platform"})
    #: Transaction ids only grow, and one transaction can be read by id
    #: (/iclock/api/transactions/<id>/), though the list ignores id filters —
    #: see fetch_punches_after.
    record_id_cursor = True
    capabilities = frozenset(
        {
            Capability.READ_PUNCHES,
            Capability.READ_EMPLOYEES,
            Capability.WRITE_EMPLOYEES,
            Capability.LIST_TERMINALS,
        }
    )
    config_fields = (
        {"name": "base_url", "label": "Server URL", "type": "url", "required": True,
         "help": "Where BioTime is reachable, e.g. https://biotime.example.com:8081"},
        {"name": "username", "label": "Username", "type": "text", "required": True},
        {"name": "password", "label": "Password", "type": "password", "required": True},
        {"name": "auth_type", "label": "Auth Style", "type": "select", "required": False,
         "default": "token", "choices": ["token", "jwt"],
         "help": "BioTime 8.5+ usually needs jwt; older builds use token."},
        {"name": "server_timezone", "label": "Server Timezone", "type": "timezone",
         "required": True, "default": "UTC",
         "help": "BioTime stores punch times as local wall-clock with no offset, "
                 "so this must match the server or every punch shifts."},
        {"name": "verify_ssl", "label": "Verify TLS Certificate", "type": "bool",
         "required": False, "default": True},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self.base_url = config.base_url.rstrip("/")
        self.auth_type = str(config.options.get("auth_type") or "token")
        self._token = config.token
        #: Transaction ids the punch-time read returned this run, so the id
        #: read only looks up the ones it did not (fetch_punches_after).
        self._seen_ids: set[int] = set()
        #: How far the id read got; the engine never moves its id cursor past it.
        self.record_cursor_until: int | None = None
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=settings.http_timeout_seconds,
            verify=config.verify_ssl,
            follow_redirects=True,
            headers={"Accept": "application/json"},
            # Ignore ambient HTTP(S)_PROXY: customer BioTime servers usually sit
            # on a LAN or private tunnel, and an inherited proxy silently breaks
            # every sync with an error that points nowhere useful.
            trust_env=False,
        )

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    @property
    def cached_token(self) -> str | None:
        """Lets the caller persist a renewed token and skip the next handshake."""
        return self._token

    # -- transport ---------------------------------------------------------
    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        wait=wait_exponential_jitter(initial=1, max=20),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Transport only, deliberately leaking httpx so tenacity can retry it.

        Nothing outside this class may call it: the raw exception is retryable
        but not reportable.
        """
        return self._client.request(method, path, **kwargs)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """The boundary. Past here, no httpx exception escapes.

        Retries first, then translates. Wrapping inside ``_send`` would make the
        BioTimeError un-retryable and a single dropped packet would fail the run;
        wrapping here means the retries happen and *then* the exhausted failure
        is named.

        This mattered more than it looks: an unwrapped ``httpx.ConnectError`` is
        not a ``ProviderError``, so it sailed past the per-source handler in the
        sync engine that exists to keep one dead site from killing the others,
        and landed in the catch-all as "Unexpected error". A customer with two
        sites lost the working site's punches because the other one was off.
        """
        try:
            return self._send(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise BioTimeError(_describe_transport_error(self.base_url, exc)) from exc

    # -- auth --------------------------------------------------------------
    def _authenticate(self) -> str:
        path = "/jwt-api-token-auth/" if self.auth_type == "jwt" else "/api-token-auth/"
        response = self._request(
            "POST",
            path,
            json={"username": self.config.username, "password": self.config.password},
        )

        if response.status_code in (400, 401, 403):
            raise BioTimeAuthError("BioTime rejected the username or password.")
        if response.status_code >= 400:
            raise BioTimeError(
                f"BioTime auth failed: HTTP {response.status_code} {response.text[:200]}"
            )

        data = response.json()
        token = data.get("token") or data.get("access") or data.get("access_token")
        if not token:
            raise BioTimeError(f"BioTime auth response carried no token: {data}")
        self._token = token
        return token

    def _auth_header(self) -> dict[str, str]:
        if not self._token:
            self._authenticate()
        scheme = "JWT" if self.auth_type == "jwt" else "Token"
        return {"Authorization": f"{scheme} {self._token}"}

    # -- reads -------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request("GET", path, params=params, headers=self._auth_header())

        # A token can expire mid-run. Re-authenticate once and replay before
        # giving up, so a long backfill does not fail at the 40th page.
        if response.status_code in (401, 403):
            log.info("BioTime token rejected; re-authenticating")
            self._token = None
            response = self._request("GET", path, params=params, headers=self._auth_header())

        if response.status_code == 404:
            raise BioTimeNotFound(
                f"BioTime has no endpoint at {path} — check the URL and the BioTime version."
            )
        if response.status_code >= 400:
            raise BioTimeError(
                f"BioTime GET {path} -> HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise BioTimeError(f"BioTime returned non-JSON for {path}") from exc

    def _post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        response = self._request("POST", path, json=json_body, headers=self._auth_header())

        # Same expired-token retry-once shape as _get — a long reconciliation
        # run can easily outlive the token too.
        if response.status_code in (401, 403):
            log.info("BioTime token rejected; re-authenticating")
            self._token = None
            response = self._request("POST", path, json=json_body, headers=self._auth_header())

        if response.status_code == 404:
            raise BioTimeError(
                f"BioTime has no endpoint at {path} — check the URL and the BioTime version."
            )
        if response.status_code >= 400:
            raise BioTimeError(
                f"BioTime POST {path} -> HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise BioTimeError(f"BioTime returned non-JSON for {path}") from exc

    def _paginate(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield rows across pages.

        ``next`` is the only end-of-data signal: an empty page in the middle of a
        result set is not the end, and treating it as one silently drops
        everything after it.
        """
        params = dict(params or {})
        params.setdefault("page_size", settings.default_page_size)
        page = 1

        while page <= settings.max_pages_per_run:
            params["page"] = page
            payload = self._get(path, params)
            yield from payload.get("data") or []
            if not payload.get("next"):
                return
            page += 1

        self.truncated = True   # the window was not read to its end
        log.warning(
            "BioTime %s: hit max_pages=%s; the rest follows on the next run.",
            path, settings.max_pages_per_run,
        )

    # -- required ----------------------------------------------------------
    def test_connection(self) -> ConnectionInfo:
        try:
            self._authenticate()
            payload = self._get("/personnel/api/employees/", {"page": 1, "page_size": 1})
        except BioTimeError as exc:
            return ConnectionInfo(ok=False, message=str(exc))
        return ConnectionInfo(
            ok=True,
            message=f"Connected — {payload.get('count', 0)} employee record(s) visible",
            detail={"employee_count": payload.get("count", 0), "base_url": self.base_url},
        )

    def fetch_punches(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> Iterator[PunchEvent]:
        params: dict[str, Any] = {}
        if since:
            params["start_time"] = since.strftime(TIME_FMT)
        if until:
            params["end_time"] = until.strftime(TIME_FMT)

        seen: set[str] = set()
        for row in self._paginate("/iclock/api/transactions/", params):
            event = self._event(row)
            if event is not None:
                seen.add(event.external_id)
                yield event
        self._seen_ids.update(int(i) for i in seen if i.isdigit())

        # Late uploads: a terminal that was offline sends its backlog with the
        # punch times it recorded, which fall before this window. Asking by
        # upload time (when BioTime received them) finds them. Only for the
        # window that reaches "now" — an old backlog slice has nothing late
        # to add, and re-reading every upload since then would be wasteful.
        if since is not None and self._upload_filter() != UPLOAD_FILTER_OFF and self._is_live(until):
            yield from self._fetch_uploaded_since(since, seen)

    def _update(self, values: dict[str, Any]) -> None:
        self.config_updates = {**(getattr(self, "config_updates", None) or {}), **values}

    # -- late uploads: the upload_time filter --------------------------------
    def _upload_filter(self) -> str | None:
        return (self.config.options or {}).get(UPLOAD_FILTER_KEY)

    def _is_live(self, until: datetime | None) -> bool:
        if until is None:
            return True
        now = datetime.now(ZoneInfo(self.config.timezone or "UTC")).replace(tzinfo=None)
        return until >= now - timedelta(hours=1)

    def _fetch_uploaded_since(self, since: datetime, seen: set[str]) -> Iterator[PunchEvent]:
        """Transactions BioTime received at or after ``since``, whatever
        their punch time. The filter's behaviour is checked against the rows
        that come back, and remembered (``config_updates``): one that
        ignores the value, or matches it exactly instead of "from", is
        switched off for this connection rather than trusted."""
        value = since.strftime(TIME_FMT)
        cut_short = self.truncated
        self.truncated = False
        rows = list(self._paginate("/iclock/api/transactions/", {"upload_time": value}))
        if self.truncated:
            log.warning("BioTime %s: more late uploads than one read holds; the rest follow next run",
                        self.base_url)
        self.truncated = cut_short   # the punch-time window's completeness is what the engine tracks

        uploaded = [str(r.get("upload_time") or "")[:19] for r in rows]
        if any(u and u < value for u in uploaded):
            self._remember(UPLOAD_FILTER_OFF, "ignores the upload_time filter")
            return
        if uploaded and all(u == value for u in uploaded) and len(rows) > 1:
            # Exact-match semantics: useless as a "from" filter. (One row equal
            # to the value proves nothing either way, so that waits.)
            self._remember(UPLOAD_FILTER_OFF, "matches upload_time exactly, not from that time on")
            return
        if any(u > value for u in uploaded):
            self._remember(UPLOAD_FILTER_ON, None)
        late = 0
        for row in rows:
            event = self._event(row)
            if event is None or event.external_id in seen:
                continue
            seen.add(event.external_id)
            if event.external_id.isdigit():
                self._seen_ids.add(int(event.external_id))
            late += 1
            yield event
        if late:
            log.info("BioTime %s: %d late upload(s) found by upload_time", self.base_url, late)

    def _remember(self, mode: str, why: str | None) -> None:
        if self._upload_filter() == mode:
            return
        if why:
            log.warning("BioTime %s %s; reading by punch time only", self.base_url, why)
        self._update({UPLOAD_FILTER_KEY: mode})

    def _event(self, row: dict[str, Any]) -> PunchEvent | None:
        txn = self._parse(row)
        if txn is None:
            return None
        return PunchEvent(
            external_id=txn.external_id,
            emp_code=txn.emp_code,
            punch_time_local=txn.punch_time_local,
            direction=txn.direction,
            terminal_sn=txn.terminal_sn,
            terminal_alias=txn.terminal_alias,
            first_name=txn.first_name,
            last_name=txn.last_name,
            department=txn.department,
            verify_type=txn.verify_type,
            record_id=int(txn.external_id) if txn.external_id.isdigit() else None,
            raw=txn.raw,
        )

    # -- late uploads: the gaps in the id sequence ----------------------------
    def fetch_punches_after(self, after_id: int, since=None, until=None) -> Iterator[PunchEvent]:
        """Transactions numbered after ``after_id`` that the punch-time read
        did not return. BioTime numbers transactions as it receives them, so
        a gap between ids the time read saw is a transaction stored there
        with a punch time outside the window — a late upload — or one since
        deleted. Each gap is read on its own (/iclock/api/transactions/<id>/),
        then a few ids past the newest, until one does not exist. At most
        MAX_ID_LOOKUPS per run; ``record_cursor_until`` says how far it got."""
        top = max(self._seen_ids, default=after_id)
        gaps = [i for i in range(after_id + 1, top + 1) if i not in self._seen_ids]
        lookups = 0
        checked = after_id
        for txn_id in gaps:
            if lookups >= MAX_ID_LOOKUPS:
                self.record_cursor_until = checked
                log.warning("BioTime %s: %d id gap(s) left to check; continuing next run",
                            self.base_url, len([g for g in gaps if g > checked]))
                return
            lookups += 1
            event = self._one(txn_id)
            checked = txn_id
            if event is not None:
                yield event
        # Past the newest id the time read saw: anything there was received
        # after it with an older punch time.
        checked = max(checked, top)
        for txn_id in range(top + 1, top + 1 + MAX_ID_LOOKUPS - lookups):
            event = self._one(txn_id)
            if event is None:
                break
            checked = txn_id
            yield event
        self.record_cursor_until = checked

    def _one(self, txn_id: int) -> PunchEvent | None:
        try:
            data = self._get(f"/iclock/api/transactions/{txn_id}/")
        except BioTimeNotFound:
            return None
        row = data.get("data") if isinstance(data.get("data"), dict) else data
        if not isinstance(row, dict) or row.get("id") is None:
            return None
        return self._event(row)

    @staticmethod
    def _parse(row: dict[str, Any]) -> _Transaction | None:
        """One malformed row must not abort an otherwise good import."""
        punch_time = row.get("punch_time") or row.get("punch_time_str") or ""
        try:
            parsed = datetime.strptime(str(punch_time)[:19], TIME_FMT)
        except ValueError:
            log.warning("Skipping BioTime row %s: unparseable punch_time %r",
                        row.get("id"), punch_time)
            return None
        if row.get("id") is None:
            log.warning("Skipping BioTime row with no id: %s", row)
            return None

        return _Transaction(
            external_id=str(row["id"]),
            emp_code=str(row.get("emp_code") or "").strip(),
            punch_time_local=parsed,
            punch_state=str(row.get("punch_state", "")).strip(),
            verify_type=_as_str(row.get("verify_type")),
            terminal_sn=_as_str(row.get("terminal_sn")),
            terminal_alias=_as_str(row.get("terminal_alias")),
            first_name=_as_str(row.get("first_name")),
            last_name=_as_str(row.get("last_name")),
            department=_as_str(row.get("department")),
            raw=row,
        )

    # -- optional ----------------------------------------------------------
    def fetch_employees(self) -> Iterator[EmployeeRecord]:
        for row in self._paginate("/personnel/api/employees/"):
            department = row.get("department")
            if isinstance(department, dict):
                department = department.get("dept_name")
            yield EmployeeRecord(
                external_id=str(row["id"]) if row.get("id") is not None else None,
                emp_code=str(row.get("emp_code") or "").strip(),
                first_name=str(row.get("first_name") or ""),
                last_name=str(row.get("last_name") or ""),
                department=department,
                # BioTime models "can this person clock in" as enable_attendance,
                # which is what disabling a leaver actually toggles.
                is_active=bool(row.get("enable_attendance", True)),
                raw=row,
            )

    def create_employee(self, record: EmployeeRecord) -> EmployeeRecord:
        """Create a BioTime personnel record for an Odoo employee that has
        none here yet — identity only, same as every other vendor: BioTime
        has no remote-enrollment concept for a fingerprint or face either,
        just a personnel row a person can then clock against once they're
        added at a terminal or via BioTime's own enrollment tooling.
        """
        emp_code = (record.emp_code or "").strip()
        if not emp_code:
            raise BioTimeError("Cannot create a BioTime employee with no employee code.")

        body = {
            "emp_code": emp_code,
            "first_name": record.first_name or emp_code,
            "last_name": record.last_name or "",
            "enable_attendance": record.is_active,
        }
        # BioTime refuses a personnel row without a department and at least
        # one area (both are ids). Fill them from what already exists there.
        body.update(self._placement(record.department))
        try:
            row = self._post("/personnel/api/employees/", body)
        except BioTimeError as exc:
            lowered = str(exc).lower()
            if "emp_code" in lowered and ("exist" in lowered or "unique" in lowered):
                # Someone already created this emp_code between our read and
                # this write — or a previous, partially-failed reconciliation
                # run already got this far. Look it up rather than treating a
                # duplicate as a hard failure, so a retried run is idempotent.
                for existing in self.fetch_employees():
                    if existing.emp_code == emp_code:
                        return existing
            raise

        department = row.get("department")
        if isinstance(department, dict):
            department = department.get("dept_name")
        return EmployeeRecord(
            external_id=str(row["id"]) if row.get("id") is not None else None,
            emp_code=str(row.get("emp_code") or emp_code).strip(),
            first_name=str(row.get("first_name") or record.first_name or ""),
            last_name=str(row.get("last_name") or record.last_name or ""),
            department=department,
            is_active=bool(row.get("enable_attendance", True)),
            raw=row,
        )

    def _placement(self, department_name: str | None) -> dict[str, Any]:
        """``department`` / ``area`` ids a new personnel row must carry.

        The department is the one matching the Odoo employee's department name
        when there is one, else the first BioTime has (its built-in default).
        ``area`` is a list in BioTime and every area is allowed, so a new person
        can clock in at any terminal. A lookup that fails just leaves the field
        out, so an older BioTime that does not need them is not made worse.
        """
        placement: dict[str, Any] = {}
        try:
            departments = list(self._paginate("/personnel/api/departments/"))
            if departments:
                wanted = (department_name or "").strip().lower()
                chosen = next(
                    (d for d in departments if str(d.get("dept_name") or "").strip().lower() == wanted),
                    None,
                ) if wanted else None
                chosen = chosen or next((d for d in departments if str(d.get("dept_code")) == "1"), departments[0])
                if chosen.get("id") is not None:
                    placement["department"] = chosen["id"]
        except BioTimeError as exc:
            log.info("BioTime departments not readable (%s); creating without one", exc)
        try:
            areas = [a["id"] for a in self._paginate("/personnel/api/areas/") if a.get("id") is not None]
            if areas:
                placement["area"] = areas
        except BioTimeError as exc:
            log.info("BioTime areas not readable (%s); creating without one", exc)
        return placement

    def fetch_terminals(self) -> Iterator[TerminalRecord]:
        for row in self._paginate("/iclock/api/terminals/"):
            area = row.get("area")
            yield TerminalRecord(
                serial_number=str(row.get("sn") or row.get("terminal_sn") or "").strip(),
                alias=row.get("alias") or row.get("terminal_name"),
                area=area.get("area_name") if isinstance(area, dict) else area,
                ip_address=row.get("ip_address"),
                model=row.get("model"),
                raw=row,
            )
