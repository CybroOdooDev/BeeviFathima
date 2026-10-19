"""auto_provision_employees is gated on the provider actually being able to
create employees — this exercises that gate through the real HTTP stack,
on both POST /sources (create) and PATCH /sources/{id} (update).

biotime and zk_device both gained WRITE_EMPLOYEES this session, so neither
is a stand-in for a provider that lacks it; a minimal read-only provider is
registered just for this file and removed afterwards, so this test file is
the only thing that ever sees it.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.integrations.base import (
    _REGISTRY,
    AttendanceProvider,
    Capability,
    ConnectionInfo,
    PunchEvent,
    SourceConfig,
    register,
)
from app.main import app
from app.models import Base

READONLY_SLUG = "test_readonly_device"


@register
class _ReadOnlyProvider(AttendanceProvider):
    """Registered only for this file: read-punches only, no employee write —
    the shape needed to exercise the auto_provision_employees rejection."""

    slug = READONLY_SLUG
    label = "Test Read-Only Device"
    capabilities = frozenset({Capability.READ_PUNCHES})

    def test_connection(self) -> ConnectionInfo:
        return ConnectionInfo(ok=True, message="ok")

    def fetch_punches(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> Iterator[PunchEvent]:
        return iter(())


@pytest.fixture(autouse=True, scope="module")
def _unregister_after_module():
    yield
    _REGISTRY.pop(READONLY_SLUG, None)


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def signup(client, company, email):
    response = client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": company,
            "email": email,
            "password": "a-long-enough-password",
            "timezone": "Asia/Dubai",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_create_is_rejected_for_a_provider_with_no_write_employees(client):
    token = signup(client, "Acme", "owner@acme.com")
    response = client.post(
        "/api/v1/sources",
        headers=auth(token),
        json={
            "provider": READONLY_SLUG,
            "base_url": "https://device.test",
            "auto_provision_employees": True,
        },
    )
    assert response.status_code == 400
    assert "cannot create employees" in response.json()["detail"]


def test_create_succeeds_without_the_flag(client):
    token = signup(client, "Acme", "owner@acme.com")
    response = client.post(
        "/api/v1/sources",
        headers=auth(token),
        json={"provider": READONLY_SLUG, "base_url": "https://device.test"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["auto_provision_employees"] is False


def test_create_succeeds_for_a_provider_that_can_write_employees(client):
    token = signup(client, "Acme", "owner@acme.com")
    response = client.post(
        "/api/v1/sources",
        headers=auth(token),
        json={
            "provider": "biotime",
            "base_url": "https://bio.test",
            "username": "u",
            "password": "p",
            "auto_provision_employees": True,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["auto_provision_employees"] is True


def test_update_is_rejected_for_a_provider_with_no_write_employees(client):
    token = signup(client, "Acme", "owner@acme.com")
    created = client.post(
        "/api/v1/sources",
        headers=auth(token),
        json={"provider": READONLY_SLUG, "base_url": "https://device.test"},
    ).json()

    response = client.patch(
        f"/api/v1/sources/{created['id']}",
        headers=auth(token),
        json={"auto_provision_employees": True},
    )
    assert response.status_code == 400
    assert "cannot create employees" in response.json()["detail"]
