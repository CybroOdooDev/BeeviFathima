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
    max_failed_logins: int = 8
    lockout_minutes: int = 15

    # --- crypto --------------------------------------------------------------
    #: Master key for envelope encryption of tenant credentials. Generate with
    #:   python -c "import secrets; print(secrets.token_urlsafe(48))"
    master_encryption_key: str = Field(default="dev-only-master-key-not-for-production")

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

    # --- security ------------------------------------------------------------
    #: Customers legitimately run BioTime on a LAN, so this is a policy switch
    #: rather than a hard block. Public deployments set it false.
    allow_private_network_targets: bool = True
    cors_origins: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
