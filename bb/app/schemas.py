"""Request and response models.

Secrets are write-only by *absence*: an API key or password appears on the input
model and simply has no field on the output model. There is no masking helper to
forget to call, and no placeholder that could be mistaken for a real value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth -------------------------------------------------------------------
class SignupRequest(BaseModel):
    company_name: str = Field(min_length=2, max_length=120)
    full_name: str | None = None
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    timezone: str = "UTC"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserOut(ORMModel):
    id: str
    email: str
    full_name: str | None
    role: str
    is_active: bool
    #: Read-only everywhere. The dashboard uses it to decide whether to show the
    #: Platform section; the server never takes it from a request.
    is_platform_admin: bool = False


# --- platform staff ---------------------------------------------------------
class TenantScheduleOut(BaseModel):
    """One customer's scheduling, as the staff console lists it."""

    id: str
    name: str
    slug: str
    status: str
    timezone: str
    sync_enabled: bool
    #: What the customer configured.
    sync_interval_minutes: int
    #: What is actually being applied, after any slow-lane widening.
    effective_interval_minutes: int
    interval_widened: bool
    consecutive_failures: int
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    next_run_at: datetime | None = None


class TenantScheduleUpdate(BaseModel):
    """Only scheduling — kept separate from TenantConfigUpdate on purpose.

    Changing a cadence is routine; changing pairing rules silently changes a
    customer's attendance results. Two endpoints means the risky one cannot be
    reached by a request that meant to do the safe one.
    """

    sync_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    sync_enabled: bool | None = None


class TenantAdminOut(TenantScheduleOut):
    """Everything the console lets staff see and edit, scheduling included."""

    pairing_mode: str
    day_boundary_hour: int
    min_punch_interval_seconds: int
    max_shift_hours: int
    orphan_out_policy: str
    work_start_time: str
    late_grace_minutes: int
    users: int = 0
    odoo_connected: bool = False
    source_connected: bool = False


class TenantConfigUpdate(BaseModel):
    """Account lifecycle and the pairing rules.

    Everything optional and ``exclude_unset`` at the call site, so a console
    editing one field cannot blank the rest.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    status: Literal["trialing", "active", "past_due", "suspended", "cancelled"] | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    pairing_mode: Literal["state_based", "alternating", "first_last"] | None = None
    day_boundary_hour: int | None = Field(default=None, ge=0, le=23)
    min_punch_interval_seconds: int | None = Field(default=None, ge=0, le=3600)
    max_shift_hours: int | None = Field(default=None, ge=1, le=48)
    orphan_out_policy: Literal["flag", "create", "ignore"] | None = None
    work_start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    late_grace_minutes: int | None = Field(default=None, ge=0, le=240)


class TenantCreateIn(BaseModel):
    """Staff onboarding a customer, instead of the customer self-registering."""

    company_name: str = Field(min_length=1, max_length=120)
    owner_email: EmailStr
    owner_name: str | None = Field(default=None, max_length=120)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    sync_interval_minutes: int = Field(default=15, ge=1, le=1440)
    #: Left unset, one is generated and returned once.
    owner_password: str | None = Field(default=None, min_length=12, max_length=128)


class TenantCreateOut(BaseModel):
    tenant: TenantAdminOut
    owner_email: str
    #: Shown exactly once, in this response. Nothing stores it in the clear, so
    #: it cannot be retrieved later — only replaced.
    owner_password: str
    note: str


class ErrorGroup(BaseModel):
    """One distinct failure, with a count. No times, badges or names."""

    message: str
    count: int


class TenantDiagnosticsOut(BaseModel):
    """Enough to diagnose a stuck customer without reading their attendance.

    Counts and error text only. The punch ledger itself — who badged when — is
    not reachable from the staff console.
    """

    tenant_id: str
    name: str
    punches_pending: int
    punches_error: int
    punches_unmapped: int
    punches_at_attempt_cap: int
    unmapped_badges: int
    last_run_status: str | None = None
    last_run_error: str | None = None
    errors: list[ErrorGroup] = []
    redacted: bool = False


# --- tenant -----------------------------------------------------------------
class TenantOut(ORMModel):
    id: str
    name: str
    slug: str
    status: str
    timezone: str
    sync_interval_minutes: int
    sync_enabled: bool
    pairing_mode: str
    day_boundary_hour: int
    min_punch_interval_seconds: int
    max_shift_hours: int
    orphan_out_policy: str
    work_start_time: str
    late_grace_minutes: int
    consecutive_failures: int


class TenantUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    sync_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    sync_enabled: bool | None = None
    pairing_mode: Literal["state_based", "alternating", "first_last"] | None = None
    day_boundary_hour: int | None = Field(default=None, ge=0, le=23)
    min_punch_interval_seconds: int | None = Field(default=None, ge=0, le=3600)
    max_shift_hours: int | None = Field(default=None, ge=1, le=48)
    orphan_out_policy: Literal["flag", "create", "ignore"] | None = None
    auto_create_employees: bool | None = None
    work_start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    late_grace_minutes: int | None = Field(default=None, ge=0, le=240)


# --- connections ------------------------------------------------------------
def _validate_url(value: str) -> str:
    if not value.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    return value.rstrip("/")


class OdooConnectionIn(BaseModel):
    name: str = "Primary Odoo"
    url: str
    db_name: str
    username: str
    api_key: str

    @field_validator("url")
    @classmethod
    def _check(cls, value: str) -> str:
        return _validate_url(value)


class OdooConnectionUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    db_name: str | None = None
    username: str | None = None
    api_key: str | None = None
    is_active: bool | None = None

    @field_validator("url")
    @classmethod
    def _check(cls, value: str | None) -> str | None:
        return _validate_url(value) if value else value


class OdooConnectionOut(ORMModel):
    id: str
    name: str
    url: str
    db_name: str
    username: str
    odoo_version: str | None
    has_companion_addon: bool
    status: str
    status_message: str | None
    last_checked_at: datetime | None
    is_active: bool
    # No api_key field. That is the whole mechanism.


class SourceIn(BaseModel):
    name: str = "Primary BioTime"
    provider: str = "biotime"
    base_url: str
    username: str
    password: str
    auth_type: Literal["token", "jwt"] = "token"
    server_timezone: str = "UTC"
    verify_ssl: bool = True
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _check(cls, value: str) -> str:
        return _validate_url(value)


class SourceUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    username: str | None = None
    password: str | None = None
    auth_type: Literal["token", "jwt"] | None = None
    server_timezone: str | None = None
    verify_ssl: bool | None = None
    is_active: bool | None = None

    @field_validator("base_url")
    @classmethod
    def _check(cls, value: str | None) -> str | None:
        return _validate_url(value) if value else value


class SourceOut(ORMModel):
    id: str
    name: str
    provider: str
    base_url: str
    username: str
    auth_type: str
    server_timezone: str
    verify_ssl: bool
    cursor_punch_time: datetime | None
    status: str
    status_message: str | None
    last_checked_at: datetime | None
    is_active: bool


class DeviceOut(ORMModel):
    id: str
    serial_number: str
    alias: str | None
    area: str | None
    ip_address: str | None
    is_enabled: bool
    pairing_override: str | None
    last_seen_at: datetime | None
    punch_count: int


class DeviceUpdate(BaseModel):
    alias: str | None = None
    is_enabled: bool | None = None
    pairing_override: Literal["state_based", "alternating", "first_last"] | None = None


class TestResult(BaseModel):
    ok: bool
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


# --- ledger and runs --------------------------------------------------------
class MappingOut(ORMModel):
    id: str
    emp_code: str
    source_name: str | None
    odoo_employee_id: int | None
    odoo_employee_name: str | None
    status: str
    match_method: str | None
    match_note: str | None
    open_attendance_id: int | None
    last_punch_at: datetime | None


class MappingUpdate(BaseModel):
    odoo_employee_id: int | None = None
    status: Literal["mapped", "unmapped", "ignored", "ambiguous"] | None = None


class PunchOut(ORMModel):
    id: str
    emp_code: str
    punch_time_utc: datetime
    punch_time_local: datetime | None
    direction: str
    terminal_sn: str | None
    process_state: str
    odoo_attendance_id: int | None
    error_message: str | None
    attempts: int
    #: The run that first ingested this punch. Null for punches recorded before
    #: the column existed.
    first_seen_run_id: str | None = None


class SyncRunOut(ORMModel):
    id: str
    status: str
    triggered_by: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    punches_fetched: int
    punches_new: int
    attendances_created: int
    attendances_closed: int
    employees_matched: int
    error_count: int
    error_message: str | None
    log: list | None


class AttendanceOut(ORMModel):
    id: str
    emp_code: str
    employee_name: str | None
    odoo_attendance_id: int | None
    check_in: datetime
    check_out: datetime | None
    check_in_local: datetime | None
    check_out_local: datetime | None
    worked_hours: float | None
    shift_date: str | None
    device_serial: str | None
    is_auto_closed: bool
    is_orphan_out: bool
    is_late: bool
    late_minutes: int
    notes: str | None


class ScheduleOut(BaseModel):
    """The state of the clock, as the dashboard needs to show it.

    ``running`` comes from the scheduler's heartbeat, not from configuration —
    the interesting case is a deployment that is configured correctly and whose
    scheduler is nonetheless dead.
    """

    running: bool
    mode: str | None = None
    owner: str | None = None
    last_tick_at: datetime | None = None
    seconds_since_tick: int | None = None
    #: None when this tenant is not scheduled at all (sync off, or suspended).
    next_run_at: datetime | None = None
    #: The tenant's interval after any slow-lane widening, so the UI can explain
    #: why a 15-minute setting is currently behaving like an hour.
    effective_interval_minutes: int
    interval_widened: bool = False


class DashboardOut(BaseModel):
    tenant: TenantOut
    punches_today: int
    punches_pending: int
    punches_error: int
    unmapped_employees: int
    last_run: SyncRunOut | None
    connection_health: dict[str, str]
    schedule: ScheduleOut


class MessageOut(BaseModel):
    message: str
