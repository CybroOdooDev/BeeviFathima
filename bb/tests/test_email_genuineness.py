"""A new account's email has to be real, and proven to belong to whoever
signed up for it — for both ways a tenant gets created: self-signup
(POST /auth/signup) and staff onboarding (POST /admin/tenants).

Two layers, tested separately:
  1. app.services.email_check — syntax + deliverability (MX/A lookup),
     enforced before the row is ever written. The deliverability half is
     mocked here rather than hitting real DNS (see the autouse fixture in
     conftest.py, which turns it off everywhere else in the suite).
  2. app.services.email_verification — proof of inbox ownership via a
     one-time link, sent through app.services.mailer (captured here instead
     of actually sent, same as every other test).
"""

from __future__ import annotations

import pytest
from email_validator import EmailNotValidError
from sqlalchemy import select

from app.core.config import settings
from app.models import User
from app.services import email_verification as verification_mod
from app.services.email_check import UngenuineEmailError, assert_genuine_email

from tests.test_platform_admin import api, head, promote, signup, staff_login  # noqa: F401

PASSWORD = "a-long-enough-password"


@pytest.fixture
def captured_mail(monkeypatch):
    """Stand in for app.services.mailer.send_email and record every call,
    the same seam _probe_source's tests use for the Odoo/BioTime clients.
    """
    sent: list[dict] = []

    def fake_send(to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(verification_mod, "send_email", fake_send)
    return sent


def _link_token(sent: list[dict]) -> str:
    """Pull the token back out of the link in the one email that was sent."""
    assert len(sent) == 1, f"expected exactly one email, got {sent}"
    link = sent[0]["body"].splitlines()[2]
    assert "/verify-email?token=" in link
    return link.split("token=", 1)[1]


# ===========================================================================
# Layer 1: is the address even real (unit level, no HTTP)
# ===========================================================================
def test_malformed_address_is_rejected():
    with pytest.raises(UngenuineEmailError):
        assert_genuine_email("not-an-email")


def test_well_formed_address_passes_with_deliverability_off():
    # The suite-wide autouse fixture already sets this False; asserted here
    # so this test fails loudly if that ever stops being true.
    assert settings.verify_email_deliverability is False
    assert assert_genuine_email("Owner@Example.COM") == "Owner@example.com"


def test_a_domain_with_no_mail_exchanger_is_rejected(monkeypatch):
    """With deliverability checking on, a domain that cannot receive mail is
    refused. The DNS lookup itself is mocked -- this is the one place in the
    suite that exercises the check_deliverability=True path, and it must not
    depend on real network access to do it.
    """
    monkeypatch.setattr(settings, "verify_email_deliverability", True)

    def fake_validate_email(email, **kwargs):
        raise EmailNotValidError("The domain name someco-not-real.invalid does not "
                                  "accept email.")

    monkeypatch.setattr(
        "app.services.email_check.validate_email", fake_validate_email
    )

    with pytest.raises(UngenuineEmailError):
        assert_genuine_email("owner@someco-not-real.invalid")


# ===========================================================================
# Layer 1, wired into the two account-creation paths
# ===========================================================================
def test_signup_rejects_a_malformed_email(api):
    response = api.post(
        "/api/v1/auth/signup",
        json={
            "company_name": "Acme",
            "email": "not-an-email",
            "password": PASSWORD,
            "timezone": "Asia/Dubai",
        },
    )
    # Pydantic's EmailStr on SignupRequest catches this before the route body
    # even runs -- 422, not the 400 assert_genuine_email itself would raise.
    assert response.status_code == 422, response.text


def test_signup_rejects_an_undeliverable_domain(api, monkeypatch):
    # Not a reserved TLD (.invalid, .test, ...) -- those are refused by
    # EmailStr's own syntax check before the request body even runs, which
    # would exercise the wrong layer. This one is syntactically ordinary; the
    # patched assert_genuine_email is what rejects it.
    monkeypatch.setattr(settings, "verify_email_deliverability", True)
    monkeypatch.setattr(
        "app.api.v1.auth.assert_genuine_email",
        lambda email: (_ for _ in ()).throw(
            UngenuineEmailError(f"{email} does not accept mail")
        ),
    )
    response = api.post(
        "/api/v1/auth/signup",
        json={
            "company_name": "Acme",
            "email": "owner@someco-not-real.example",
            "password": PASSWORD,
            "timezone": "Asia/Dubai",
        },
    )
    assert response.status_code == 400, response.text
    assert "does not accept mail" in response.json()["detail"]


def test_admin_create_tenant_rejects_an_undeliverable_domain(api, monkeypatch):
    _, staff = _promote_a_staff_user(api)
    monkeypatch.setattr(
        "app.api.v1.admin.assert_genuine_email",
        lambda email: (_ for _ in ()).throw(UngenuineEmailError("bad domain")),
    )
    response = api.post(
        "/api/v1/admin/tenants",
        json={"company_name": "Globex", "owner_email": "owner@globex.example.com"},
        headers=head(staff),
    )
    assert response.status_code == 400, response.text


def _promote_a_staff_user(api):
    signup(api, "Ops Co", "ops-owner@platform.example.com")
    return None, promote(api, "ops-owner@platform.example.com")


# ===========================================================================
# Layer 2: proving the inbox, end to end
# ===========================================================================
def test_signup_sends_exactly_one_verification_email(api, captured_mail):
    signup(api, "Acme", "owner@acme.example.com")
    assert len(captured_mail) == 1
    assert captured_mail[0]["to"] == "owner@acme.example.com"


def test_new_account_starts_unverified(api, captured_mail):
    token = signup(api, "Acme", "owner@acme.example.com")
    me = api.get("/api/v1/auth/me", headers=head(token)).json()
    assert me["email_verified_at"] is None


def test_clicking_the_link_verifies_the_account(api, captured_mail):
    token = signup(api, "Acme", "owner@acme.example.com")
    verify_token = _link_token(captured_mail)

    response = api.post("/api/v1/auth/verify-email", json={"token": verify_token})
    assert response.status_code == 200, response.text

    me = api.get("/api/v1/auth/me", headers=head(token)).json()
    assert me["email_verified_at"] is not None


def test_a_used_token_cannot_be_replayed(api, captured_mail):
    signup(api, "Acme", "owner@acme.example.com")
    verify_token = _link_token(captured_mail)

    first = api.post("/api/v1/auth/verify-email", json={"token": verify_token})
    assert first.status_code == 200, first.text

    second = api.post("/api/v1/auth/verify-email", json={"token": verify_token})
    assert second.status_code == 400, second.text


def test_an_unknown_token_is_rejected(api):
    response = api.post("/api/v1/auth/verify-email", json={"token": "not-a-real-token"})
    assert response.status_code == 400, response.text


def test_an_expired_token_is_rejected(api, captured_mail):
    token = signup(api, "Acme", "owner@acme.example.com")
    verify_token = _link_token(captured_mail)

    db = api.session_factory()
    user = db.scalars(select(User).where(User.email == "owner@acme.example.com")).one()
    from datetime import datetime, timedelta, timezone

    user.email_verify_token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    db.close()

    response = api.post("/api/v1/auth/verify-email", json={"token": verify_token})
    assert response.status_code == 400, response.text


def test_resend_issues_a_working_new_link_and_retires_the_old_one(api, captured_mail):
    token = signup(api, "Acme", "owner@acme.example.com")
    first_link_token = _link_token(captured_mail)
    captured_mail.clear()

    response = api.post("/api/v1/auth/resend-verification", headers=head(token))
    assert response.status_code == 200, response.text
    second_link_token = _link_token(captured_mail)
    assert second_link_token != first_link_token

    # The token from the first email no longer works...
    stale = api.post("/api/v1/auth/verify-email", json={"token": first_link_token})
    assert stale.status_code == 400, stale.text

    # ...but the one from the resend does.
    fresh = api.post("/api/v1/auth/verify-email", json={"token": second_link_token})
    assert fresh.status_code == 200, fresh.text


def test_resend_after_verification_is_a_no_op(api, captured_mail):
    token = signup(api, "Acme", "owner@acme.example.com")
    verify_token = _link_token(captured_mail)
    api.post("/api/v1/auth/verify-email", json={"token": verify_token})
    captured_mail.clear()

    response = api.post("/api/v1/auth/resend-verification", headers=head(token))
    assert response.status_code == 200, response.text
    assert captured_mail == [], "already-verified accounts should not get a new email"


def test_staff_onboarded_owner_also_gets_a_verification_email(api, captured_mail):
    signup(api, "Ops Co", "ops-owner@platform.example.com")
    staff = promote(api, "ops-owner@platform.example.com")
    captured_mail.clear()

    response = api.post(
        "/api/v1/admin/tenants",
        json={"company_name": "Globex", "owner_email": "owner@globex.example.com"},
        headers=head(staff),
    )
    assert response.status_code == 201, response.text
    assert len(captured_mail) == 1
    assert captured_mail[0]["to"] == "owner@globex.example.com"
