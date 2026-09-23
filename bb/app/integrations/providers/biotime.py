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
from datetime import datetime
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

# BioTime punch_state -> semantic direction.
STATE_IN = {"0", "3", "4"}   # Check In, Break In, Overtime In
STATE_OUT = {"1", "2", "5"}  # Check Out, Break Out, Overtime Out


class BioTimeError(ProviderError):
    """Any failure talking to BioTime."""


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
        {"name": "auth_type", "label": "Auth style", "type": "select", "required": False,
         "default": "token", "choices": ["token", "jwt"],
         "help": "BioTime 8.5+ usually needs jwt; older builds use token."},
        {"name": "server_timezone", "label": "Server timezone", "type": "timezone",
         "required": True, "default": "UTC",
         "help": "BioTime stores punch times as local wall-clock with no offset, "
                 "so this must match the server or every punch shifts."},
        {"name": "verify_ssl", "label": "Verify TLS certificate", "type": "bool",
         "required": False, "default": True},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self.base_url = config.base_url.rstrip("/")
        self.auth_type = str(config.options.get("auth_type") or "token")
        self._token = config.token
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
            raise BioTimeError(
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

        for row in self._paginate("/iclock/api/transactions/", params):
            txn = self._parse(row)
            if txn is None:
                continue
            yield PunchEvent(
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
                raw=txn.raw,
            )

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
