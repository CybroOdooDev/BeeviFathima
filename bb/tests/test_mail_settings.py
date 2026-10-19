"""Console → Email server: saved SMTP settings, precedence over .env, the
write-only password, and the test-send endpoint — against a fake SMTP client."""

from __future__ import annotations

import smtplib

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import PlatformSetting
from app.services import mailer
from tests.test_platform_admin import api, head, promote, signup  # noqa: F401

GMAIL = {"host": "smtp.gmail.com", "port": 587, "security": "starttls",
         "username": "noreply@acme-hr.example.com", "password": "abcd efgh ijkl mnop",
         "from_email": "noreply@acme-hr.example.com", "from_name": "BioBridge"}


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    refuse_login = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.started_tls, self.login_as, self.sent = host, port, False, None, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        if FakeSMTP.refuse_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        self.login_as = (user, password)

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.refuse_login = False
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "")
    return FakeSMTP


def staff(api):
    signup(api, "Ops", "ops@platform.example.com")
    return promote(api, "ops@platform.example.com")


def test_customers_cannot_reach_it(api, smtp):
    token = signup(api, "Acme", "owner@acme.example.com")
    assert api.get("/api/v1/admin/mail", headers=head(token)).status_code == 403
    assert api.patch("/api/v1/admin/mail", headers=head(token), json=GMAIL).status_code == 403


def test_save_keeps_the_password_secret(api, smtp):
    token = staff(api)
    view = api.get("/api/v1/admin/mail", headers=head(token)).json()
    assert view["active_source"] == "none" and view["presets"]["gmail"]["host"] == "smtp.gmail.com"

    view = api.patch("/api/v1/admin/mail", headers=head(token), json=GMAIL).json()
    assert view["active_source"] == "database" and view["has_password"] is True
    assert "password" not in view and "password_enc" not in str(view)
    s = api.session_factory()
    raw = s.get(PlatformSetting, "mail").value
    s.close()
    assert "abcd" not in raw and "password_enc" in raw

    # A later save without a password keeps it; clear_password removes it.
    view = api.patch("/api/v1/admin/mail", headers=head(token), json={"from_name": "Acme HR"}).json()
    assert view["has_password"] is True and view["from_name"] == "Acme HR"
    # Gmail can't work without one, so removing it needs the setting switched off.
    r = api.patch("/api/v1/admin/mail", headers=head(token), json={"clear_password": True})
    assert r.status_code == 400
    view = api.patch("/api/v1/admin/mail", headers=head(token), json={"clear_password": True, "enabled": False}).json()
    assert view["has_password"] is False


def test_validation(api, smtp):
    token = staff(api)
    r = api.patch("/api/v1/admin/mail", headers=head(token), json={"port": 587})
    assert r.status_code == 400 and "host" in r.json()["detail"]
    r = api.patch("/api/v1/admin/mail", headers=head(token), json={"host": "smtp.gmail.com"})
    assert r.status_code == 400 and "From" in r.json()["detail"]


def test_mail_goes_through_the_saved_server(api, smtp):
    token = staff(api)
    api.patch("/api/v1/admin/mail", headers=head(token), json=GMAIL)
    s = api.session_factory()
    mailer.send_email("someone@example.com", "Hello", "Body", db=s)
    s.close()
    client = smtp.instances[-1]
    assert (client.host, client.port, client.started_tls) == ("smtp.gmail.com", 587, True)
    # The App Password's display spaces are dropped.
    assert client.login_as == ("noreply@acme-hr.example.com", "abcdefghijklmnop")
    message = client.sent[0]
    assert message["From"] == "BioBridge <noreply@acme-hr.example.com>" and message["Message-ID"]


def test_switched_off_falls_back_to_env(api, smtp, monkeypatch):
    token = staff(api)
    api.patch("/api/v1/admin/mail", headers=head(token), json={**GMAIL, "enabled": False})
    monkeypatch.setattr(settings, "smtp_host", "mail.internal.example.com")
    monkeypatch.setattr(settings, "smtp_use_tls", True)
    assert api.get("/api/v1/admin/mail", headers=head(token)).json()["active_source"] == "environment"
    s = api.session_factory()
    mailer.send_email("someone@example.com", "Hello", "Body", db=s)
    s.close()
    assert smtp.instances[-1].host == "mail.internal.example.com"


def test_nothing_configured_only_logs(api, smtp):
    mailer.send_email("someone@example.com", "Hello", "Body", db=None)
    assert smtp.instances == []


def test_test_email(api, smtp):
    token = staff(api)
    r = api.post("/api/v1/admin/mail/test", headers=head(token), json={"to": "ops@platform.example.com"})
    assert r.status_code == 400
    api.patch("/api/v1/admin/mail", headers=head(token), json=GMAIL)
    r = api.post("/api/v1/admin/mail/test", headers=head(token), json={"to": "ops@platform.example.com"})
    assert r.status_code == 200, r.text
    assert "smtp.gmail.com" in r.json()["message"]
    assert smtp.instances[-1].sent[0]["To"] == "ops@platform.example.com"

    smtp.refuse_login = True
    r = api.post("/api/v1/admin/mail/test", headers=head(token), json={"to": "ops@platform.example.com"})
    assert r.status_code == 502 and "App Password" in r.json()["detail"]


def test_gmail_without_a_sign_in_is_explained(api, smtp):
    token = staff(api)
    r = api.patch("/api/v1/admin/mail", headers=head(token),
                  json={k: v for k, v in GMAIL.items() if k not in ("password",)})
    assert r.status_code == 400 and "App Password" in r.json()["detail"]


def test_blank_username_signs_in_as_the_from_address(api, smtp):
    token = staff(api)
    api.patch("/api/v1/admin/mail", headers=head(token), json={**GMAIL, "username": ""})
    r = api.post("/api/v1/admin/mail/test", headers=head(token), json={"to": "ops@platform.example.com"})
    assert r.status_code == 200, r.text
    assert smtp.instances[-1].login_as == ("noreply@acme-hr.example.com", "abcdefghijklmnop")


def test_530_reads_as_a_missing_sign_in():
    from app.services.mail_settings import MailConfig
    exc = smtplib.SMTPSenderRefused(530, b"5.7.0 Authentication Required.", "x@gmail.com")
    text = mailer._explain(exc, MailConfig(source="database", host="smtp.gmail.com"))
    assert "sign-in" in text and "App Password" in text
