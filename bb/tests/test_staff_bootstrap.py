"""STAFF_EMAIL / STAFF_PASSWORD: a fixed staff login for a host with no shell."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.models import Tenant, User, UserSession
from app.services.staff_bootstrap import ensure_static_staff
from tests.test_api_isolation import client  # noqa: F401

EMAIL = "ops@cybrosys.com"
PASSWORD = "a-long-static-passphrase"


@pytest.fixture
def staff_settings(monkeypatch):
    monkeypatch.setattr(settings, "staff_email", EMAIL)
    monkeypatch.setattr(settings, "staff_password", PASSWORD)


def _user(db, email=EMAIL):
    return db.scalars(select(User).where(User.email == email)).first()


def test_nothing_configured_does_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "staff_email", "")
    monkeypatch.setattr(settings, "staff_password", "")
    assert ensure_static_staff(db) == "off"
    assert _user(db) is None


def test_creates_a_staff_user_with_no_workspace(db, staff_settings):
    assert ensure_static_staff(db) == "created"
    user = _user(db)
    assert user.is_platform_admin and user.tenant_id is None and user.is_active
    assert verify_password(PASSWORD, user.hashed_password)
    assert user.hashed_password != PASSWORD          # only the hash is stored


def test_running_again_changes_nothing(db, staff_settings):
    ensure_static_staff(db)
    assert ensure_static_staff(db) == "unchanged"
    assert len(db.scalars(select(User).where(User.email == EMAIL)).all()) == 1


def test_changing_the_setting_rotates_the_password_and_ends_old_sessions(db, staff_settings, monkeypatch):
    ensure_static_staff(db)
    user = _user(db)
    db.add(UserSession(user_id=user.id, token_hash="h" * 64, scope="staff",
                       expires_at=__import__("datetime").datetime(2099, 1, 1)))
    db.commit()

    monkeypatch.setattr(settings, "staff_password", "a-brand-new-passphrase")
    assert ensure_static_staff(db) == "updated"
    db.refresh(user)
    assert verify_password("a-brand-new-passphrase", user.hashed_password)
    assert not verify_password(PASSWORD, user.hashed_password)
    assert db.scalars(select(UserSession).where(UserSession.user_id == user.id)).one().revoked_at is not None


def test_restores_access_that_was_switched_off_and_clears_a_lockout(db, staff_settings):
    ensure_static_staff(db)
    user = _user(db)
    user.is_platform_admin = False
    user.is_active = False
    user.failed_login_count = 4
    db.commit()
    assert ensure_static_staff(db) == "updated"
    db.refresh(user)
    assert user.is_platform_admin and user.is_active and user.failed_login_count == 0


def test_a_customer_account_is_never_touched(db, staff_settings):
    tenant = Tenant(name="Acme", slug="acme", timezone="UTC")
    db.add(tenant); db.flush()
    db.add(User(tenant_id=tenant.id, email=EMAIL, hashed_password=hash_password("customers-own-password"),
                role="owner"))
    db.commit()

    assert ensure_static_staff(db) == "skipped"
    user = _user(db)
    assert not user.is_platform_admin
    assert verify_password("customers-own-password", user.hashed_password)


@pytest.mark.parametrize("email,password", [
    (EMAIL, ""), ("", PASSWORD), (EMAIL, "short"), ("not-an-email", PASSWORD),
])
def test_incomplete_or_weak_settings_are_refused(db, monkeypatch, email, password):
    monkeypatch.setattr(settings, "staff_email", email)
    monkeypatch.setattr(settings, "staff_password", password)
    assert ensure_static_staff(db) == "skipped"
    assert db.scalars(select(User)).first() is None


def test_the_login_works_at_the_staff_door_and_not_the_customer_one(client, monkeypatch):
    from app.db.session import get_db
    from app.main import app

    monkeypatch.setattr(settings, "staff_email", EMAIL)
    monkeypatch.setattr(settings, "staff_password", PASSWORD)
    db = next(app.dependency_overrides[get_db]())
    ensure_static_staff(db)

    ok = client.post("/api/v1/auth/staff/login", json={"email": EMAIL, "password": PASSWORD})
    assert ok.status_code == 200, ok.text
    assert client.post("/api/v1/auth/staff/login", json={"email": EMAIL, "password": "wrong-password-1"}).status_code == 401
    # The customer door refuses a staff account, as it does for one made with grant_admin.py.
    assert client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}).status_code in (401, 403)
