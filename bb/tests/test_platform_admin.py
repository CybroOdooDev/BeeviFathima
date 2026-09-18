"""The staff console, and the boundary it is allowed to cross.

The console exists to cross tenant isolation, which makes it the most dangerous
router in the system. Most of these tests are therefore about what it *cannot*
do: an ordinary customer must not reach it, the flag must not be obtainable
through the product, and the console itself must not become a way to read one
customer's attendance data.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app
from app.models import AuditLog, Base, Tenant, User


@pytest.fixture
def api():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        client.session_factory = Session
        yield client
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


def promote(client, email):
    """What tools/grant_admin.py does — the only way the flag is ever set."""
    db = client.session_factory()
    user = db.scalars(select(User).where(User.email == email)).first()
    user.is_platform_admin = True
    db.commit()
    db.close()


def head(token):
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# The boundary
# ===========================================================================
ADMIN_ROUTES = [
    ("get", "/api/v1/admin/tenants"),
    ("get", "/api/v1/admin/scheduler"),
]


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_an_ordinary_customer_cannot_reach_the_console(api, method, path):
    token = signup(api, "Acme", "owner@acme.example.com")
    response = getattr(api, method)(path, headers=head(token))
    assert response.status_code == 403, response.text


def test_an_owner_is_still_not_platform_staff(api):
    """Owner is the top role *inside* an account. It must not imply anything
    outside it — that conflation is how a customer ends up able to list every
    other customer."""
    token = signup(api, "Acme", "owner@acme.example.com")
    assert api.get("/api/v1/auth/me", headers=head(token)).json()["role"] == "owner"
    assert api.get("/api/v1/admin/tenants", headers=head(token)).status_code == 403


def test_the_console_needs_a_token_at_all(api):
    assert api.get("/api/v1/admin/tenants").status_code == 401


def test_patching_another_tenant_is_refused_for_a_customer(api):
    """The route someone would actually try: change a competitor's schedule."""
    signup(api, "Victim", "owner@victim.example.com")
    attacker = signup(api, "Attacker", "owner@attacker.example.com")

    db = api.session_factory()
    victim = db.scalars(select(Tenant).where(Tenant.slug == "victim")).first()
    victim_id = victim.id
    db.close()

    response = api.patch(
        f"/api/v1/admin/tenants/{victim_id}/schedule",
        json={"sync_interval_minutes": 1},
        headers=head(attacker),
    )
    assert response.status_code == 403


def test_the_flag_cannot_be_set_through_the_product(api):
    """No request body anywhere writes it. Signup does not accept it, and the
    tenant update schema has no route to it."""
    token = signup(api, "Acme", "owner@acme.example.com")
    api.post(
        "/api/v1/auth/signup",
        json={
            "company_name": "Sneaky", "email": "s@sneaky.example.com",
            "password": "a-long-enough-password", "timezone": "UTC",
            "is_platform_admin": True,
        },
    )
    api.patch("/api/v1/tenant", json={"is_platform_admin": True}, headers=head(token))

    db = api.session_factory()
    assert not db.scalars(select(User).where(User.is_platform_admin.is_(True))).all()
    db.close()


def test_me_reports_the_flag_so_the_dashboard_can_hide_the_section(api):
    token = signup(api, "Ops", "ops@platform.example.com")
    assert api.get("/api/v1/auth/me", headers=head(token)).json()["is_platform_admin"] is False
    promote(api, "ops@platform.example.com")
    assert api.get("/api/v1/auth/me", headers=head(token)).json()["is_platform_admin"] is True


# ===========================================================================
# What staff can do
# ===========================================================================
def test_staff_see_every_tenant(api):
    signup(api, "Acme", "a@acme.example.com")
    signup(api, "Globex", "b@globex.example.com")
    staff = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")

    rows = api.get("/api/v1/admin/tenants", headers=head(staff)).json()
    assert {r["slug"] for r in rows} == {"acme", "globex", "ops"}


def test_staff_can_search_by_name_or_slug(api):
    signup(api, "Acme Industrial", "a@acme.example.com")
    signup(api, "Globex", "b@globex.example.com")
    staff = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")

    rows = api.get("/api/v1/admin/tenants?q=globe", headers=head(staff)).json()
    assert [r["slug"] for r in rows] == ["globex"]


def test_staff_change_one_tenants_interval(api):
    signup(api, "Acme", "a@acme.example.com")
    staff = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")

    rows = api.get("/api/v1/admin/tenants?q=acme", headers=head(staff)).json()
    acme = rows[0]
    assert acme["sync_interval_minutes"] == 15

    response = api.patch(
        f"/api/v1/admin/tenants/{acme['id']}/schedule",
        json={"sync_interval_minutes": 5},
        headers=head(staff),
    )
    assert response.status_code == 200, response.text
    assert response.json()["sync_interval_minutes"] == 5


def test_changing_one_tenant_leaves_the_others_alone(api):
    """An obvious property, and exactly the one a bad WHERE clause breaks."""
    signup(api, "Acme", "a@acme.example.com")
    signup(api, "Globex", "b@globex.example.com")
    staff = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")

    rows = {r["slug"]: r for r in api.get("/api/v1/admin/tenants", headers=head(staff)).json()}
    api.patch(
        f"/api/v1/admin/tenants/{rows['acme']['id']}/schedule",
        json={"sync_interval_minutes": 2},
        headers=head(staff),
    )

    after = {r["slug"]: r for r in api.get("/api/v1/admin/tenants", headers=head(staff)).json()}
    assert after["acme"]["sync_interval_minutes"] == 2
    assert after["globex"]["sync_interval_minutes"] == 15


def test_the_customer_sees_the_change_in_their_own_audit_trail(api):
    """Support work that is invisible to the customer is how "we never touched
    it" arguments start."""
    signup(api, "Acme", "a@acme.example.com")
    staff = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")

    acme = api.get("/api/v1/admin/tenants?q=acme", headers=head(staff)).json()[0]
    api.patch(
        f"/api/v1/admin/tenants/{acme['id']}/schedule",
        json={"sync_interval_minutes": 30},
        headers=head(staff),
    )

    db = api.session_factory()
    entries = db.scalars(
        select(AuditLog).where(AuditLog.tenant_id == acme["id"])
    ).all()
    db.close()
    assert [e.action for e in entries] == ["platform.schedule.update"]
    assert "15 -> 30" in entries[0].detail
    assert "ops@platform.example.com" in entries[0].detail


def test_the_interval_is_bounded(api):
    staff = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")
    own = api.get("/api/v1/admin/tenants", headers=head(staff)).json()[0]

    for bad in (0, -5, 1441):
        response = api.patch(
            f"/api/v1/admin/tenants/{own['id']}/schedule",
            json={"sync_interval_minutes": bad},
            headers=head(staff),
        )
        assert response.status_code == 422, f"{bad} should be rejected"


def test_an_empty_patch_is_refused_rather_than_silently_doing_nothing(api):
    staff = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")
    own = api.get("/api/v1/admin/tenants", headers=head(staff)).json()[0]

    response = api.patch(
        f"/api/v1/admin/tenants/{own['id']}/schedule", json={}, headers=head(staff)
    )
    assert response.status_code == 400


def test_an_unknown_tenant_is_a_404(api):
    staff = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")
    response = api.patch(
        "/api/v1/admin/tenants/does-not-exist/schedule",
        json={"sync_interval_minutes": 5},
        headers=head(staff),
    )
    assert response.status_code == 404


def test_turning_sync_off_clears_the_next_run(api):
    staff = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")
    own = api.get("/api/v1/admin/tenants", headers=head(staff)).json()[0]
    assert own["next_run_at"] is not None

    after = api.patch(
        f"/api/v1/admin/tenants/{own['id']}/schedule",
        json={"sync_enabled": False},
        headers=head(staff),
    ).json()
    assert after["next_run_at"] is None


def test_clearing_the_failure_count_lifts_the_slow_lane(api):
    signup(api, "Acme", "a@acme.example.com")
    staff = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")

    db = api.session_factory()
    acme = db.scalars(select(Tenant).where(Tenant.slug == "acme")).first()
    acme.consecutive_failures = 9
    acme_id = acme.id
    db.commit()
    db.close()

    before = api.get(f"/api/v1/admin/tenants/{acme_id}", headers=head(staff)).json()
    assert before["interval_widened"] is True
    assert before["effective_interval_minutes"] == 60  # 15 x 4

    api.post(f"/api/v1/admin/tenants/{acme_id}/schedule/reset", headers=head(staff))

    after = api.get(f"/api/v1/admin/tenants/{acme_id}", headers=head(staff)).json()
    assert after["interval_widened"] is False
    assert after["effective_interval_minutes"] == 15


def test_the_console_exposes_no_customer_attendance_data(api):
    """Scope check. Scheduling only — a support console that hands over one
    customer's punches is a data-protection problem waiting to happen."""
    staff = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")

    row = api.get("/api/v1/admin/tenants", headers=head(staff)).json()[0]
    leaky = {"punches", "attendance", "employees", "api_key", "password", "credentials"}
    assert not leaky & set(row), f"unexpected fields: {leaky & set(row)}"


# ===========================================================================
# Configuring a tenant
# ===========================================================================
def staff_client(api):
    token = signup(api, "Ops", "ops@platform.example.com")
    promote(api, "ops@platform.example.com")
    return token


def test_staff_change_account_lifecycle(api):
    signup(api, "Acme", "a@acme.example.com")
    staff = staff_client(api)
    acme = api.get("/api/v1/admin/tenants?q=acme", headers=head(staff)).json()[0]

    response = api.patch(
        f"/api/v1/admin/tenants/{acme['id']}/config",
        json={"status": "active", "timezone": "Asia/Muscat", "name": "Acme Industrial"},
        headers=head(staff),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["timezone"], body["name"]) == (
        "active", "Asia/Muscat", "Acme Industrial",
    )


def test_suspending_stops_the_account_syncing(api):
    """Only trialing and active are syncable, so this is the off switch that
    does not depend on the customer's own setting."""
    signup(api, "Acme", "a@acme.example.com")
    staff = staff_client(api)
    acme = api.get("/api/v1/admin/tenants?q=acme", headers=head(staff)).json()[0]
    assert acme["next_run_at"] is not None

    after = api.patch(
        f"/api/v1/admin/tenants/{acme['id']}/config",
        json={"status": "suspended"},
        headers=head(staff),
    ).json()
    assert after["next_run_at"] is None


def test_staff_change_pairing_rules(api):
    signup(api, "Acme", "a@acme.example.com")
    staff = staff_client(api)
    acme = api.get("/api/v1/admin/tenants?q=acme", headers=head(staff)).json()[0]

    body = api.patch(
        f"/api/v1/admin/tenants/{acme['id']}/config",
        json={"pairing_mode": "first_last", "max_shift_hours": 12,
              "work_start_time": "07:30", "late_grace_minutes": 5},
        headers=head(staff),
    ).json()
    assert body["pairing_mode"] == "first_last"
    assert body["max_shift_hours"] == 12
    assert body["work_start_time"] == "07:30"


def test_config_rejects_values_the_engine_cannot_use(api):
    signup(api, "Acme", "a@acme.example.com")
    staff = staff_client(api)
    acme = api.get("/api/v1/admin/tenants?q=acme", headers=head(staff)).json()[0]

    for bad in (
        {"pairing_mode": "telepathy"},
        {"max_shift_hours": 0},
        {"day_boundary_hour": 24},
        {"work_start_time": "9am"},
        {"status": "deleted"},
    ):
        response = api.patch(
            f"/api/v1/admin/tenants/{acme['id']}/config", json=bad, headers=head(staff)
        )
        assert response.status_code == 422, f"{bad} should be rejected"


def test_config_changes_are_audited_to_the_customer(api):
    signup(api, "Acme", "a@acme.example.com")
    staff = staff_client(api)
    acme = api.get("/api/v1/admin/tenants?q=acme", headers=head(staff)).json()[0]
    api.patch(
        f"/api/v1/admin/tenants/{acme['id']}/config",
        json={"status": "suspended"},
        headers=head(staff),
    )

    db = api.session_factory()
    entry = db.scalars(
        select(AuditLog).where(AuditLog.tenant_id == acme["id"])
    ).first()
    db.close()
    assert entry.action == "platform.config.update"
    assert "trialing -> suspended" in entry.detail


def test_an_ordinary_customer_cannot_configure_anyone(api):
    token = signup(api, "Acme", "a@acme.example.com")
    db = api.session_factory()
    acme_id = db.scalars(select(Tenant).where(Tenant.slug == "acme")).first().id
    db.close()

    for path, payload in [
        (f"/api/v1/admin/tenants/{acme_id}/config", {"status": "active"}),
        (f"/api/v1/admin/tenants/{acme_id}/sync", None),
    ]:
        response = api.patch(path, json=payload, headers=head(token)) if payload \
            else api.post(path, headers=head(token))
        assert response.status_code == 403


# ===========================================================================
# Creating a tenant
# ===========================================================================
def test_staff_create_a_tenant_and_its_owner(api):
    staff = staff_client(api)
    response = api.post(
        "/api/v1/admin/tenants",
        json={"company_name": "Muscat Traders", "owner_email": "boss@muscat.example.com",
              "timezone": "Asia/Muscat", "sync_interval_minutes": 30},
        headers=head(staff),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["tenant"]["slug"] == "muscat-traders"
    assert body["tenant"]["sync_interval_minutes"] == 30
    assert len(body["owner_password"]) >= 16, "a generated password must not be guessable"

    # The new owner can actually sign in with it — the whole point.
    login = api.post(
        "/api/v1/auth/login",
        json={"email": "boss@muscat.example.com", "password": body["owner_password"]},
    )
    assert login.status_code == 200, login.text


def test_a_created_owner_is_not_platform_staff(api):
    """Creating accounts must not mint more people who can create accounts."""
    staff = staff_client(api)
    body = api.post(
        "/api/v1/admin/tenants",
        json={"company_name": "Muscat", "owner_email": "boss@muscat.example.com"},
        headers=head(staff),
    ).json()
    token = api.post(
        "/api/v1/auth/login",
        json={"email": "boss@muscat.example.com", "password": body["owner_password"]},
    ).json()["access_token"]
    assert api.get("/api/v1/admin/tenants", headers=head(token)).status_code == 403


def test_creating_a_tenant_refuses_a_duplicate_email(api):
    signup(api, "Acme", "taken@acme.example.com")
    staff = staff_client(api)
    response = api.post(
        "/api/v1/admin/tenants",
        json={"company_name": "Other", "owner_email": "taken@acme.example.com"},
        headers=head(staff),
    )
    assert response.status_code == 409


def test_a_customer_cannot_create_tenants(api):
    token = signup(api, "Acme", "a@acme.example.com")
    response = api.post(
        "/api/v1/admin/tenants",
        json={"company_name": "Sneaky", "owner_email": "s@sneaky.example.com"},
        headers=head(token),
    )
    assert response.status_code == 403


# ===========================================================================
# Diagnostics — counts and error text, and nothing that identifies anyone
# ===========================================================================
def seed_failure(api, tenant_id: str) -> None:
    """A stuck punch whose Odoo message names the employee, as Odoo's does."""
    from datetime import datetime

    from app.models import Direction, EmployeeMapping, MappingStatus, PunchRecord, PunchState

    db = api.session_factory()
    db.add(
        EmployeeMapping(
            tenant_id=tenant_id, emp_code="1002", odoo_employee_name="Sara Tanaka",
            status=MappingStatus.mapped.value,
        )
    )
    db.add(
        PunchRecord(
            tenant_id=tenant_id, source_id="src", external_id="1", emp_code="1002",
            punch_time_utc=datetime(2026, 9, 14, 9, 22),
            direction=Direction.inward.value, terminal_sn="GATE-01",
            process_state=PunchState.error.value, attempts=5,
            error_message=(
                "Odoo hr.attendance.create failed: Cannot create new attendance "
                "record for Sara Tanaka, the employee was already checked in on "
                "09/14/2026 01:22:00 PM"
            ),
        )
    )
    db.commit()
    db.close()


def test_diagnostics_report_what_is_stuck(api):
    signup(api, "Acme", "a@acme.example.com")
    staff = staff_client(api)
    acme = api.get("/api/v1/admin/tenants?q=acme", headers=head(staff)).json()[0]
    seed_failure(api, acme["id"])

    body = api.get(
        f"/api/v1/admin/tenants/{acme['id']}/diagnostics", headers=head(staff)
    ).json()
    assert body["punches_error"] == 1
    assert body["punches_at_attempt_cap"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["count"] == 1


def test_diagnostics_scrub_the_employee_name_out_of_the_odoo_message(api):
    """Odoo writes the person's name into its own error text, so withholding
    other columns is not enough — the message itself has to be scrubbed."""
    signup(api, "Acme", "a@acme.example.com")
    staff = staff_client(api)
    acme = api.get("/api/v1/admin/tenants?q=acme", headers=head(staff)).json()[0]
    seed_failure(api, acme["id"])

    body = api.get(
        f"/api/v1/admin/tenants/{acme['id']}/diagnostics", headers=head(staff)
    ).json()
    message = body["errors"][0]["message"]
    assert "Sara" not in message and "Tanaka" not in message
    assert "<employee>" in message
    assert "already checked in" in message, "the diagnosis must survive redaction"
    assert body["redacted"] is True


def test_diagnostics_expose_no_punch_times_or_badges(api):
    signup(api, "Acme", "a@acme.example.com")
    staff = staff_client(api)
    acme = api.get("/api/v1/admin/tenants?q=acme", headers=head(staff)).json()[0]
    seed_failure(api, acme["id"])

    body = api.get(
        f"/api/v1/admin/tenants/{acme['id']}/diagnostics", headers=head(staff)
    ).json()
    blob = str(body)
    assert "GATE-01" not in blob, "terminal serials are not staff business"
    assert "1002" not in blob, "badge numbers identify a person"
    leaky = {"punches", "attendance", "ledger"}
    assert not leaky & set(body)


def test_a_customer_cannot_read_another_tenants_diagnostics(api):
    signup(api, "Victim", "v@victim.example.com")
    attacker = signup(api, "Attacker", "x@attacker.example.com")
    db = api.session_factory()
    victim_id = db.scalars(select(Tenant).where(Tenant.slug == "victim")).first().id
    db.close()

    response = api.get(
        f"/api/v1/admin/tenants/{victim_id}/diagnostics", headers=head(attacker)
    )
    assert response.status_code == 403


# ===========================================================================
# Staff are not a tenant
# ===========================================================================
def make_staff(api, email="ops@platform.example.com"):
    """A platform user with no tenant — what tools/grant_admin.py --create does."""
    from app.core.security import hash_password
    from app.models import UserRole

    db = api.session_factory()
    db.add(
        User(
            tenant_id=None,
            email=email,
            hashed_password=hash_password("a-long-enough-password"),
            role=UserRole.owner.value,
            is_platform_admin=True,
        )
    )
    db.commit()
    db.close()
    response = api.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "a-long-enough-password"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def test_a_staff_user_needs_no_tenant_to_exist(api):
    token = make_staff(api)
    me = api.get("/api/v1/auth/me", headers=head(token)).json()
    assert me["is_platform_admin"] is True


def test_a_tenantless_staff_user_reaches_the_console(api):
    signup(api, "Acme", "a@acme.example.com")
    token = make_staff(api)
    rows = api.get("/api/v1/admin/tenants", headers=head(token)).json()
    assert [r["slug"] for r in rows] == ["acme"]


def test_staff_do_not_appear_as_a_customer(api):
    """The flaw this fixes: a support engineer's own account showed up in the
    customer list, was counted as scheduled, and got polled."""
    signup(api, "Acme", "a@acme.example.com")
    token = make_staff(api)

    rows = api.get("/api/v1/admin/tenants", headers=head(token)).json()
    assert len(rows) == 1, "only the real customer"
    health = api.get("/api/v1/admin/scheduler", headers=head(token)).json()
    assert health["tenants_total"] == 1
    assert health["tenants_scheduled"] == 1


def test_a_tenantless_user_is_refused_every_tenant_route(api):
    """Not 401 — their session is fine. They simply have no workspace."""
    token = make_staff(api)
    for path in ("/api/v1/tenant", "/api/v1/dashboard", "/api/v1/punches",
                 "/api/v1/attendance", "/api/v1/sync/runs"):
        response = api.get(path, headers=head(token))
        assert response.status_code == 403, f"{path} gave {response.status_code}"
        assert "platform staff" in response.json()["detail"]


def test_a_tenantless_user_cannot_trigger_their_own_sync(api):
    token = make_staff(api)
    assert api.post("/api/v1/sync/run-inline", headers=head(token)).status_code == 403


def test_the_scheduler_never_picks_up_a_staff_user(api):
    """There is no tenant row to pick up, which is the whole point."""
    from app.services.scheduling import due_tenants

    make_staff(api)
    db = api.session_factory()
    assert due_tenants(db) == []
    db.close()


def test_a_dual_role_user_keeps_their_customer_account(api):
    """Someone who really is both stays both — the tenant is optional, not
    forbidden."""
    token = signup(api, "Acme", "owner@acme.example.com")
    promote(api, "owner@acme.example.com")
    assert api.get("/api/v1/tenant", headers=head(token)).status_code == 200
    assert api.get("/api/v1/admin/tenants", headers=head(token)).status_code == 200
