"""Two doors, and what each session is allowed to do once it is through one.

Platform staff and customers used to share a sign-in. The flag on the user row
decided what the console would answer, so one token was both a customer session
and a cross-tenant credential at the same time — and for anyone who was both a
customer and staff, signing in to look at their own attendance handed them a
session that could also list every other account.

Now the scope is decided by the door: ``/auth/login`` mints a customer session
whoever presents the password, ``/auth/staff/login`` mints a console one, and
neither reaches the other's surface. Server-side authorisation was already the
real control — these tests are about it staying the real control once there are
two places to sign in, rather than the split being a matter of which page
someone happened to load.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from jose import jwt
from sqlalchemy import select

from app.core.config import settings
from app.core.security import decode_token
from app.models import User, UserSession
from app.services.timeutils import ensure_aware

# The fixture and the account helpers already exist next door; importing them
# keeps one definition of "a signed-up customer" and "a promoted staff user".
from tests.test_platform_admin import (  # noqa: F401
    api,
    head,
    make_staff,
    promote,
    signup,
    staff_login,
)

PASSWORD = "a-long-enough-password"
STAFF_EMAIL = "ops@platform.example.com"


def login(client, email, password=PASSWORD, door="login"):
    """A raw sign-in at either door, returning the response for inspection."""
    path = "/api/v1/auth/staff/login" if door == "staff" else "/api/v1/auth/login"
    return client.post(path, json={"email": email, "password": password})


# ===========================================================================
# Each door mints its own kind of session
# ===========================================================================
def test_the_customer_door_issues_a_customer_session(api):
    signup(api, "Acme", "owner@acme.example.com")
    response = login(api, "owner@acme.example.com")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "tenant"
    assert decode_token(body["access_token"])["scp"] == "tenant"


def test_the_console_door_issues_a_console_session(api):
    signup(api, "Ops", STAFF_EMAIL)
    token = promote(api, STAFF_EMAIL)
    assert decode_token(token)["scp"] == "staff"


def test_the_scope_is_a_claim_in_the_token_not_a_field_beside_it(api):
    """A client cannot upgrade itself by editing the response body."""
    signup(api, "Ops", STAFF_EMAIL)
    token = promote(api, STAFF_EMAIL)
    payload = decode_token(token)
    assert payload["scp"] == "staff"
    assert payload["typ"] == "access"


# ===========================================================================
# The separation itself
# ===========================================================================
def test_a_customer_session_cannot_reach_the_console_even_for_staff(api):
    """The defect this closes.

    Everything about this user says "staff" — the flag is set, the password is
    right — and the session is still refused, because it was opened at the
    customer door. Before the split this token listed every tenant.
    """
    customer = signup(api, "Acme", "owner@acme.example.com")
    promote(api, "owner@acme.example.com")  # the flag is genuinely set

    response = api.get("/api/v1/admin/tenants", headers=head(customer))
    assert response.status_code == 403
    assert "staff session" in response.json()["detail"], (
        "the refusal must say the session is wrong, not that the person lacks "
        "a permission they actually hold"
    )


def test_a_console_session_cannot_act_inside_an_account(api):
    """The other direction, which matters for the audit trail as much as access.

    A console session acting as a customer would file the change against a hat
    the person was not wearing.
    """
    signup(api, "Acme", "owner@acme.example.com")
    console = promote(api, "owner@acme.example.com")

    for path in ("/api/v1/tenant", "/api/v1/dashboard", "/api/v1/attendance"):
        response = api.get(path, headers=head(console))
        assert response.status_code == 403, f"{path} gave {response.status_code}"


def test_both_sessions_can_still_ask_who_they_are(api):
    """/auth/me is the one scope-agnostic route: the UI calls it before it knows
    which shell to build, on either surface."""
    customer = signup(api, "Acme", "owner@acme.example.com")
    console = promote(api, "owner@acme.example.com")

    for token in (customer, console):
        response = api.get("/api/v1/auth/me", headers=head(token))
        assert response.status_code == 200, response.text
        assert response.json()["is_platform_admin"] is True


# ===========================================================================
# The console door must not become an oracle
# ===========================================================================
def test_the_console_door_does_not_reveal_who_is_staff(api):
    """A correct password at the wrong door reads exactly like a wrong one.

    Anything more specific turns this endpoint into a lookup for which accounts
    hold the flag — the shortlist of credentials most worth phishing.
    """
    signup(api, "Acme", "owner@acme.example.com")

    right_password = login(api, "owner@acme.example.com", door="staff")
    wrong_password = login(api, "owner@acme.example.com", "not-the-password", door="staff")
    no_such_account = login(api, "nobody@acme.example.com", door="staff")

    assert right_password.status_code == 401
    assert (
        right_password.json()["detail"]
        == wrong_password.json()["detail"]
        == no_such_account.json()["detail"]
    ), "the three failures must be indistinguishable"


def test_the_console_door_hands_back_no_token_to_a_customer(api):
    signup(api, "Acme", "owner@acme.example.com")
    body = login(api, "owner@acme.example.com", door="staff").json()
    assert "access_token" not in body


def test_the_console_door_locks_out_a_guesser_like_the_other_one(api):
    """The rate limiting is shared, so the console is not the softer target."""
    signup(api, "Ops", STAFF_EMAIL)
    promote(api, STAFF_EMAIL)

    for _ in range(settings.max_failed_logins):
        login(api, STAFF_EMAIL, "wrong-password", door="staff")

    locked = login(api, STAFF_EMAIL, PASSWORD, door="staff")
    assert locked.status_code == 429, locked.text


# ===========================================================================
# Being sent to the right door
# ===========================================================================
def test_a_tenantless_staff_account_is_turned_away_from_the_customer_door(api):
    """A token minted here would authenticate and then fail on every screen.

    Refusing at the door, with the address of the one that works, beats handing
    back a session that reads as a broken account.
    """
    make_staff(api)  # creates the account and signs in at the console door
    response = login(api, STAFF_EMAIL)
    assert response.status_code == 403
    assert "staff console" in response.json()["detail"]


def test_a_staff_user_with_a_workspace_is_still_welcome_at_the_customer_door(api):
    """Being staff is not a reason to refuse someone their own account."""
    signup(api, "Acme", "owner@acme.example.com")
    promote(api, "owner@acme.example.com")

    response = login(api, "owner@acme.example.com")
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "tenant"


# ===========================================================================
# Refreshing stays on the surface it started on
# ===========================================================================
def _refresh(client, refresh_token):
    return client.post(f"/api/v1/auth/refresh?refresh_token={refresh_token}")


def test_refreshing_a_console_session_stays_a_console_session(api):
    signup(api, "Ops", STAFF_EMAIL)
    promote(api, STAFF_EMAIL)
    opened = login(api, STAFF_EMAIL, door="staff").json()

    renewed = _refresh(api, opened["refresh_token"]).json()
    assert renewed["scope"] == "staff"
    assert api.get("/api/v1/admin/tenants", headers=head(renewed["access_token"])).status_code == 200


def test_refreshing_a_customer_session_cannot_become_a_console_one(api):
    """The scope comes from the signed token, so there is nothing to ask for."""
    signup(api, "Acme", "owner@acme.example.com")
    promote(api, "owner@acme.example.com")
    opened = login(api, "owner@acme.example.com").json()

    renewed = _refresh(api, opened["refresh_token"]).json()
    assert renewed["scope"] == "tenant"
    assert api.get(
        "/api/v1/admin/tenants", headers=head(renewed["access_token"])
    ).status_code == 403


def test_losing_the_staff_flag_ends_the_console_session(api):
    """Revoking access has to actually revoke it.

    Every console route checks the flag per request, so the live token stops
    working at once. This is about the refresh: without the check the session
    would keep renewing itself until its refresh token ran out.
    """
    signup(api, "Ops", STAFF_EMAIL)
    promote(api, STAFF_EMAIL)
    opened = login(api, STAFF_EMAIL, door="staff").json()

    db = api.session_factory()
    user = db.scalars(select(User).where(User.email == STAFF_EMAIL)).first()
    user.is_platform_admin = False
    db.commit()
    db.close()

    assert _refresh(api, opened["refresh_token"]).status_code == 401


# ===========================================================================
# A console credential is worth more, so it lives for less time
# ===========================================================================
def test_a_console_session_expires_sooner_than_a_customer_one(api):
    signup(api, "Acme", "owner@acme.example.com")
    signup(api, "Ops", STAFF_EMAIL)
    promote(api, STAFF_EMAIL)

    customer = login(api, "owner@acme.example.com").json()
    console = login(api, STAFF_EMAIL, door="staff").json()

    assert console["expires_in"] < customer["expires_in"], (
        "a token that reaches every tenant must not outlive one that reaches a "
        "single account"
    )


def test_the_console_refresh_token_also_lives_shorter(api):
    """A short access token is not much use if the refresh beside it lasts a
    month — the session would simply renew itself all month."""
    signup(api, "Ops", STAFF_EMAIL)
    promote(api, STAFF_EMAIL)

    db = api.session_factory()
    sessions = db.scalars(select(UserSession)).all()
    staff = [s for s in sessions if s.scope == "staff"]
    assert staff, "the console sign-in recorded no session"

    # Generous slack: this is about being a day rather than a month, not about
    # the clock.
    ceiling = datetime.now(timezone.utc) + timedelta(
        days=settings.staff_refresh_token_ttl_days, hours=1
    )
    for session in staff:
        assert ensure_aware(session.expires_at) < ceiling, session.expires_at
    assert settings.staff_refresh_token_ttl_days < settings.refresh_token_ttl_days
    db.close()


# ===========================================================================
# The session row says which door was used
# ===========================================================================
def test_the_session_row_records_which_door_was_used(api):
    """Not derivable from tenant_id: a dual-role person has a tenant either way.

    During an incident the question is which live sessions could reach other
    customers, and that has to be answerable from the table.
    """
    signup(api, "Acme", "owner@acme.example.com")
    promote(api, "owner@acme.example.com")
    login(api, "owner@acme.example.com")
    login(api, "owner@acme.example.com", door="staff")

    db = api.session_factory()
    user = db.scalars(select(User).where(User.email == "owner@acme.example.com")).first()
    scopes = sorted(
        s.scope for s in db.scalars(
            select(UserSession).where(UserSession.user_id == user.id)
        ).all()
    )
    db.close()

    assert scopes.count("staff") >= 1 and scopes.count("tenant") >= 1, scopes


# ===========================================================================
# Tokens that predate the split
# ===========================================================================
def test_a_token_minted_before_scopes_existed_cannot_reach_the_console(api):
    """The fallback has to fail closed.

    A token issued by the old code carries no scope claim. Reading that as
    staff would have left every session already in the wild holding console
    access until it happened to expire.
    """
    signup(api, "Ops", STAFF_EMAIL)
    promote(api, STAFF_EMAIL)

    db = api.session_factory()
    user = db.scalars(select(User).where(User.email == STAFF_EMAIL)).first()
    legacy = jwt.encode(
        {
            "sub": user.id,
            "tid": user.tenant_id,
            "role": user.role,
            "typ": "access",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            "jti": uuid.uuid4().hex,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    db.close()

    assert api.get("/api/v1/auth/me", headers=head(legacy)).status_code == 200, (
        "the token is still a valid session — this is about scope, not validity"
    )
    assert api.get("/api/v1/admin/tenants", headers=head(legacy)).status_code == 403
