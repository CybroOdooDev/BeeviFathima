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


# --- subscription plans ------------------------------------------------------
class SubscriptionPlanOut(ORMModel):
    """A tier as the staff console lists it — active or retired.

    Retired plans are included deliberately: a tenant already on one still
    needs its name to show up wherever a plan is picked, even though new
    assignment should prefer the active ones. ``is_active`` is what lets the
    console tell the two apart rather than guessing from absence.
    """

    id: str
    name: str
    description: str | None = None
    is_active: bool
    is_default: bool
    monthly_price_cents: int | None = None
    max_employees: int | None = None
    min_sync_interval_minutes: int | None = None


class RenewalWarningOut(BaseModel):
    """Present only when a subscription is close enough to lapse to mention.

    See ``app.services.scheduling.renewal_warning`` for the window and why a
    lapsed account does not get one of these as well.
    """

    renews_at: datetime
    days_left: int
    #: Inside subscription_urgent_days of lapsing — same warning, louder
    #: styling, not a second message. See app.services.scheduling.renewal_warning.
    urgent: bool = False


# --- auth -------------------------------------------------------------------
class SignupRequest(BaseModel):
    company_name: str = Field(min_length=2, max_length=120)
    full_name: str | None = None
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    timezone: str = "UTC"
    #: Left unset, the plan marked default is used — same fallback staff
    #: onboarding follows (TenantCreateIn.plan_id). Sent explicitly, it is the
    #: signup screen's plan picker: validated against the active plans in
    #: GET /auth/plans, the same list the picker itself was built from.
    plan_id: str | None = Field(default=None)
    #: The other half of signup's "start a free trial" vs "choose a plan"
    #: choice. False (the default) is the trial: status starts ``trialing``
    #: for settings.trial_days, whatever plan_id says. True skips the trial
    #: entirely — status starts ``active`` for settings.billing_period_days
    #: — and requires plan_id, since "no trial, no chosen plan" is not a
    #: real choice. See app.api.v1.auth.signup.
    skip_trial: bool = False


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    #: Which surface this session is for — "tenant" or "staff". Returned so the
    #: UI knows which shell to render without having to decode the token, and so
    #: a client cannot end up showing the console to a customer session.
    scope: Literal["tenant", "staff"] = "tenant"


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

    #: Whether the platform permits this account to sync at all — ``status`` in
    #: SYNCABLE. Computed server-side rather than left to each client to work
    #: out from the status string, so there is one definition of "stopped" and
    #: the console and the customer's dashboard cannot disagree about it.
    syncable: bool = True
    suspended_at: datetime | None = None

    #: The tier this account is on, when to renew, and whether that is close
    #: enough to warn about — all null/null/null together means no plan is
    #: assigned, which enforces nothing. See SubscriptionPlan.
    plan_id: str | None = None
    plan_name: str | None = None
    subscription_renews_at: datetime | None = None
    renewal_warning: RenewalWarningOut | None = None
    #: A self-service switch queued while this account was on a paid plan —
    #: null the rest of the time. See Tenant.pending_plan_id.
    pending_plan_id: str | None = None
    pending_plan_name: str | None = None


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
    #: Staff-only. Deliberately absent from TenantOut: the customer is shown a
    #: fixed line, not whatever note support left for the next engineer.
    suspension_reason: str | None = None


class TenantDeactivateIn(BaseModel):
    """Why an account is being stopped.

    Optional so the console can offer a one-click action, but worth filling in:
    it is what the next person reads when the customer calls to ask why their
    attendance stopped updating.
    """

    reason: str | None = Field(default=None, max_length=200)


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

    #: Which tier this account is sold under. Explicitly nullable: sending
    #: ``null`` clears the plan (nothing enforced from then on), distinct from
    #: leaving the key out (which — like every field here — leaves it alone).
    plan_id: str | None = None
    #: When this account's access lapses without a renewal. Also explicitly
    #: nullable: ``null`` exempts the account from the automatic sweep in
    #: app.services.scheduling rather than lapsing it, since a missing date
    #: has no "past due" moment to reach.
    subscription_renews_at: datetime | None = None


class TenantCreateIn(BaseModel):
    """Staff onboarding a customer, instead of the customer self-registering."""

    company_name: str = Field(min_length=1, max_length=120)
    owner_email: EmailStr
    owner_name: str | None = Field(default=None, max_length=120)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    sync_interval_minutes: int = Field(default=15, ge=1, le=1440)
    #: Left unset, one is generated and returned once.
    owner_password: str | None = Field(default=None, min_length=12, max_length=128)
    #: Left unset (or sent as null), whichever plan is marked default is used
    #: — the same rule self-signup follows in app.api.v1.auth.signup. An
    #: account can still end up with no plan at all (no default exists, or
    #: staff clears it afterwards from the account's own config form) — this
    #: field just is not how that is requested at creation time.
    plan_id: str | None = Field(default=None)


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

    #: Why the dashboard is not updating, when it is not the customer's own
    #: doing. ``syncable`` is false while the platform has the account stopped;
    #: ``suspended_at`` says since when. The staff note behind it is not here on
    #: purpose — see TenantAdminOut.
    syncable: bool = True
    suspended_at: datetime | None = None

    #: What this account's plan is and what it enforces for *them* — the
    #: employee cap and sync-interval floor, so Settings can explain a limit
    #: before they hit it rather than only after. All null together means no
    #: plan is assigned, which enforces nothing. The renewal date itself is
    #: always shown; whether it is close enough to warn about is a separate,
    #: server-decided question — see DashboardOut.renewal_warning.
    plan_name: str | None = None
    plan_max_employees: int | None = None
    plan_min_sync_interval_minutes: int | None = None
    subscription_renews_at: datetime | None = None
    #: Which plan, by id — alongside plan_name so a self-service plan picker
    #: can preselect the current choice without a second lookup.
    plan_id: str | None = None
    #: A switch already chosen but not yet in effect — see
    #: Tenant.pending_plan_id. Settings shows this as "switching to X on
    #: renewal"; null means no switch is queued.
    pending_plan_id: str | None = None
    pending_plan_name: str | None = None


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
    #: Self-service plan switch — repeatable, any time. Deliberately not
    #: nullable the way the staff console's TenantConfigUpdate.plan_id is:
    #: a customer can move to another active plan but cannot clear their own
    #: plan and go unenforced that way. Takes effect immediately unless the
    #: account is already on a paid (``active``) plan, in which case it is
    #: queued in Tenant.pending_plan_id instead and lands at the next
    #: renewal — see app.api.v1.sync.update_tenant for the full rule and the
    #: auto-raise-the-floor behaviour signup also uses.
    plan_id: str | None = None


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
    #: None right up until the subscription is genuinely close to lapsing —
    #: see app.services.scheduling.renewal_warning. The overview page is the
    #: one place this is rendered as a banner.
    renewal_warning: RenewalWarningOut | None = None


class MessageOut(BaseModel):
    message: str
