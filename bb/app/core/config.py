"""Application configuration — 12-factor, environment driven."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- app -----------------------------------------------------------------
    app_name: str = "BioBridge"
    environment: str = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    public_base_url: str = "http://localhost:8000"

    # --- stores --------------------------------------------------------------
    database_url: str = "sqlite:///./biobridge.db"
    #: Empty means "no broker": the API runs syncs inline instead of queuing.
    redis_url: str = ""

    # --- auth ----------------------------------------------------------------
    jwt_secret: str = Field(default="dev-only-change-me-at-least-32-characters")
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60 * 12
    refresh_token_ttl_days: int = 30

    #: Staff sessions expire far sooner than customer ones, because they are
    #: worth far more: a console token reaches every tenant, while a customer's
    #: reaches one. A support engineer signs in to answer a ticket and should
    #: not still be holding a cross-tenant credential the next morning — twelve
    #: hours of ambient console access is the wrong default for a laptop in a
    #: coffee shop. An hour is long enough for the work and short enough that a
    #: stolen token is usually already dead.
    staff_access_token_ttl_minutes: int = 60
    staff_refresh_token_ttl_days: int = 1

    max_failed_logins: int = 8
    lockout_minutes: int = 15

    # --- crypto --------------------------------------------------------------
    #: Master key for envelope encryption of tenant credentials. Generate with
    #:   python -c "import secrets; print(secrets.token_urlsafe(48))"
    master_encryption_key: str = Field(default="dev-only-master-key-not-for-production")

    # --- scheduling ----------------------------------------------------------
    #: Who runs the clock.
    #:   auto      — Celery beat when REDIS_URL is set, otherwise in-process.
    #:   inprocess — the API process runs it. No broker, no worker, one service.
    #:   celery    — beat only. The API never dispatches on a timer.
    #:   off       — nothing is scheduled; syncs happen only when triggered.
    #: "auto" is the default so a plain `uvicorn app.main:app` actually syncs on
    #: a schedule. Deployments that add Redis get beat and the API stands down,
    #: which is what stops both from firing the same tenant in the same minute.
    scheduler_mode: str = "auto"
    #: How often the loop wakes. The tick does not decide the sync frequency —
    #: each tenant's own interval does — so this only bounds how late a due
    #: tenant can be.
    scheduler_tick_seconds: int = 60
    #: Tenants synced at once. A cycle is I/O-bound on two remote systems, so
    #: this is about not opening fifty sockets to fifty customer LANs at once.
    scheduler_concurrency: int = 4
    #: Lease lifetime. Longer than a tick so a slow tick does not lose it,
    #: short enough that a killed process is replaced within a minute or two.
    scheduler_lease_ttl_seconds: int = 180

    # --- sync engine ---------------------------------------------------------
    default_sync_interval_minutes: int = 15
    #: Devices upload late and their clocks drift, so a strict cursor loses
    #: punches. Re-reading is free: the ledger is keyed on the vendor's own id.
    fetch_overlap_minutes: int = 15
    backfill_limit_days: int = 30
    default_page_size: int = 200
    max_pages_per_run: int = 100
    max_consecutive_failures: int = 5
    http_timeout_seconds: int = 30

    # --- subscriptions ---------------------------------------------------------
    #: Length of a self-signup trial, in days. Applied once, at signup — see
    #: app.api.v1.auth.signup. Staff can set any date by hand afterwards, and
    #: a staff-created account (app.api.v1.admin.create_tenant) gets the same
    #: default, theirs to change immediately if it should be something else.
    trial_days: int = 10
    #: Assumed length of one billing cycle, in days, for an account that
    #: skips the trial and starts directly on a chosen plan at signup
    #: (SignupRequest.skip_trial). There is no billing integration to say
    #: otherwise — see app.api.v1.auth.signup — so this is a placeholder
    #: "paid through" period, the same role trial_days plays for a trial.
    billing_period_days: int = 30
    #: How close to its renewal date an account has to be before the warning
    #: banner appears — on both the tenant's own dashboard and the staff
    #: console, so the two surfaces always agree on what counts as "soon".
    #: See app.services.scheduling.renewal_warning.
    subscription_warning_days: int = 7
    #: Inside this many days of lapsing, the same warning is shown with an
    #: urgent tone instead of a routine one — still one message, not a second
    #: banner. See app.services.scheduling.renewal_warning.
    subscription_urgent_days: int = 3

    # --- security ------------------------------------------------------------
    #: Customers legitimately run BioTime on a LAN, so this is a policy switch
    #: rather than a hard block. Public deployments set it false.
    allow_private_network_targets: bool = True
    cors_origins: str = ""

        # --- email genuineness -----------------------------------------------------
    #: Whether a new account's email address gets a real MX/A lookup, not just
    #: syntax checking, before the account is created. Off would let a signup
    #: through on "asdf@asdf" — this is what stops it costing a database row.
    #: The test suite turns this off globally (see tests/conftest.py) so it
    #: never depends on outbound DNS; a dedicated test re-enables it against a
    #: mocked resolver.
    verify_email_deliverability: bool = True
    #: How long a signup or staff-onboarded account has to click the
    #: confirmation link before it goes stale and a fresh one has to be
    #: requested (POST /auth/resend-verification).
    email_verification_ttl_hours: int = 48

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def scheduler_runs_in_process(self) -> bool:
        """Should this API process run the scheduling loop?

        Resolving "auto" in one place keeps the decision out of the loop, the
        health endpoint and the UI, which would otherwise each have their own
        opinion about whether a broker means Celery is really running.
        """
        mode = self.scheduler_mode.strip().lower()
        if mode == "off":
            return False
        if mode == "inprocess":
            return True
        if mode == "celery":
            return False
        return not self.redis_url  # auto


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
