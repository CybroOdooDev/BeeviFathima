"""Forgot password: emailed single-use link, same answer for any address."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import User, UserSession
from app.services import password_reset
from tests.test_billing import _signup, client  # noqa: F401

NEW = "a-brand-new-password"
OLD = "a-long-enough-password"


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(password_reset, "send_email",
                        lambda to, subject, body, db=None: sent.append({"to": to, "subject": subject, "body": body}))
    return sent


def _token(mail):
    return re.search(r"token=([\w\-]+)", mail[-1]["body"]).group(1)


def _user(client):
    s = client.Session()
    try:
        return s.scalars(select(User).where(User.email == "owner@acme.com")).one()
    finally:
        s.close()


def _forgot(client, email="owner@acme.com"):
    return client.post("/api/v1/auth/forgot-password", json={"email": email})


def test_the_answer_is_the_same_for_unknown_addresses(client, mail):
    _signup(client)
    a, b = _forgot(client), _forgot(client, "nobody@nowhere.com")
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert len(mail) == 1 and mail[0]["to"] == "owner@acme.com"


def test_reset_changes_the_password_and_the_link_is_single_use(client, mail):
    _signup(client)
    _forgot(client)
    token = _token(mail)
    r = client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": NEW})
    assert r.status_code == 200, r.text
    assert client.post("/api/v1/auth/login", json={"email": "owner@acme.com", "password": NEW}).status_code == 200
    assert client.post("/api/v1/auth/login", json={"email": "owner@acme.com", "password": OLD}).status_code == 401
    assert client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "another-long-one"}).status_code == 400
    assert "was changed" in mail[-1]["subject"]


def test_reset_signs_out_other_sessions_and_clears_a_lockout(client, mail):
    _signup(client)
    refresh = client.post("/api/v1/auth/login", json={"email": "owner@acme.com", "password": OLD}).json()["refresh_token"]
    s = client.Session()
    u = s.scalars(select(User)).one()
    u.locked_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    u.failed_login_count = 3
    s.commit(); s.close()
    _forgot(client)
    client.post("/api/v1/auth/reset-password", json={"token": _token(mail), "new_password": NEW})
    # Access tokens are short-lived; what ends is the ability to renew them.
    assert client.post(f"/api/v1/auth/refresh?refresh_token={refresh}").status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "owner@acme.com", "password": NEW}).status_code == 200
    s = client.Session()
    assert all(x.revoked_at for x in s.scalars(select(UserSession).where(UserSession.revoked_reason == "password_reset")))
    s.close()


def test_expired_and_bogus_tokens_are_refused(client, mail):
    _signup(client)
    _forgot(client)
    token = _token(mail)
    s = client.Session()
    u = s.scalars(select(User)).one()
    u.password_reset_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    s.commit(); s.close()
    assert client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": NEW}).status_code == 400
    assert client.post("/api/v1/auth/reset-password", json={"token": "x" * 40, "new_password": NEW}).status_code == 400


def test_requests_are_rate_limited_per_account_and_a_new_link_replaces_the_old(client, mail):
    _signup(client)
    _forgot(client); _forgot(client)
    assert len(mail) == 1, "second request inside the cooldown sends nothing"
    first = _token(mail)
    s = client.Session()
    u = s.scalars(select(User)).one()
    u.password_reset_sent_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    s.commit(); s.close()
    _forgot(client)
    assert len(mail) == 2
    assert client.post("/api/v1/auth/reset-password", json={"token": first, "new_password": NEW}).status_code == 400
    assert client.post("/api/v1/auth/reset-password", json={"token": _token(mail), "new_password": NEW}).status_code == 200


def test_a_short_password_is_refused_and_the_token_survives(client, mail):
    _signup(client)
    _forgot(client)
    token = _token(mail)
    assert client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "short"}).status_code == 422
    assert client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": NEW}).status_code == 200


def test_a_disabled_account_gets_no_link(client, mail):
    _signup(client)
    s = client.Session()
    s.scalars(select(User)).one().is_active = False
    s.commit(); s.close()
    assert _forgot(client).status_code == 200 and mail == []
