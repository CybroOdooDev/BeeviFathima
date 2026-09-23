"""Every schema that carries a timezone must reject a name that isn't real.

Before this, a typo (or a display name copied from somewhere that isn't a
zone name at all) was accepted silently by every one of these schemas and
only misbehaved later: app.services.timeutils.get_zone() falls back to UTC
with no error at sync time. These tests are the schema-level half of closing
that gap — catching a bad value at the point it's saved, in every place a
timezone can be typed, not just app.services.timeutils's own fallback.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    SignupRequest,
    SourceIn,
    SourceUpdate,
    TenantConfigUpdate,
    TenantCreateIn,
    TenantUpdate,
)

GOOD = ["UTC", "Asia/Dubai", "Asia/Kolkata", "America/New_York", "Etc/UTC"]
BAD = ["Mars/Phobos", "not-a-zone", "Asia/Dubi", "PST", "  "]


@pytest.mark.parametrize("tz", GOOD)
def test_source_in_accepts_real_zones(tz: str) -> None:
    src = SourceIn(base_url="https://x.example.com", server_timezone=tz)
    assert src.server_timezone == tz


@pytest.mark.parametrize("tz", BAD)
def test_source_in_rejects_fake_zones(tz: str) -> None:
    with pytest.raises(ValidationError):
        SourceIn(base_url="https://x.example.com", server_timezone=tz)


@pytest.mark.parametrize("tz", BAD)
def test_source_update_rejects_fake_zones(tz: str) -> None:
    with pytest.raises(ValidationError):
        SourceUpdate(server_timezone=tz)


def test_source_update_leaves_an_unset_timezone_alone() -> None:
    """None must stay None — every field here is optional-and-independent,
    not "clear to the default", and this must not be mistaken for a value
    that needs validating."""
    assert SourceUpdate(name="x").server_timezone is None


@pytest.mark.parametrize("tz", BAD)
def test_signup_request_rejects_fake_zones(tz: str) -> None:
    with pytest.raises(ValidationError):
        SignupRequest(company_name="Acme", email="a@example.com", password="x" * 12, timezone=tz)


@pytest.mark.parametrize("tz", BAD)
def test_tenant_create_in_rejects_fake_zones(tz: str) -> None:
    with pytest.raises(ValidationError):
        TenantCreateIn(company_name="Acme", owner_email="a@example.com", timezone=tz)


@pytest.mark.parametrize("tz", BAD)
def test_tenant_update_rejects_fake_zones(tz: str) -> None:
    with pytest.raises(ValidationError):
        TenantUpdate(timezone=tz)


@pytest.mark.parametrize("tz", BAD)
def test_tenant_config_update_rejects_fake_zones(tz: str) -> None:
    with pytest.raises(ValidationError):
        TenantConfigUpdate(timezone=tz)


def test_tenant_update_leaves_an_unset_timezone_alone() -> None:
    assert TenantUpdate(name="Acme").timezone is None
