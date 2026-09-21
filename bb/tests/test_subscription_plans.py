"""Subscription plans: enforced tiers, and the automatic renewal-date sweep.

Kept apart from test_subscription_gate.py even though both end at the same
gate: that file is about the gate itself (SYNCABLE, the manual deactivate /
activate lever). This one is about what decides to pull that lever
automatically, and about the tier a tenant is sold under in the first place.

* **Enforcement** — a plan's limits actually constrain something. The sync
  interval floor is tested below; the employee cap lives in
  test_sync_engine.py, next to the matching logic it constrains.
* **The sweep** — app.services.scheduling.sweep_subscriptions moves accounts
  across the renewal date the same way staff already do by hand, and — the
  test that matters most — never invents a lapse for an account nobody told
  it to watch.
* **The warning** — advance notice before an account lapses, not a grace
  period after. That is the actual, literal shape of what was asked for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.models import EmployeeMapping, MappingStatus, SubscriptionPlan, Tenant, TenantStatus
from app.services.scheduling import due_tenants, renewal_warning, sweep_subscriptions

# The console fixture and account helpers — "a customer" and "a staff
# session" mean the same thing here as everywhere else in this suite.
from tests.test_platform_admin import (  # noqa: F401
    api,
    head,
    make_staff,
    promote,
    signup,
    staff_login,
)
from tests.test_subscription_gate import give_it_a_device, row, tenant_id

CUSTOMER = "owner@acme.example.com"


def a_customer_and_staff(api):
    customer = signup(api, "Acme", CUSTOMER)
    staff = make_staff(api)
    return customer, staff


def make_plan(api, name="Test Plan", **fields) -> str:
    db = api.session_factory()
    plan = SubscriptionPlan(name=name, **fields)
    db.add(plan)
    db.commit()
    plan_id = plan.id
    db.close()
    return plan_id


def set_tenant(api, slug="acme", **fields):
    """Reach past every endpoint to set up a precondition directly.

    Some of these — a renewal date already in the past, a status the
    automatic sweep is meant to *reach* rather than start from — are not
    supposed to be one API call away, which is exactly why the sweep itself
    has to be tested by arranging them here first.
    """
    db = api.session_factory()
    tenant = db.scalars(select(Tenant).where(Tenant.slug == slug)).first()
    for key, value in fields.items():
        setattr(tenant, key, value)
    db.commit()
    db.close()


# ===========================================================================
# Enforcement: the sync-interval floor
# ===========================================================================
def test_a_plan_floor_refuses_a_faster_interval_from_the_customer(api):
    customer, staff = a_customer_and_staff(api)
    plan_id = make_plan(api, name="Starter", min_sync_interval_minutes=30)
    set_tenant(api, plan_id=plan_id)

    response = api.patch("/api/v1/tenant", json={"sync_interval_minutes": 10},
                          headers=head(customer))
    assert response.status_code == 400, response.text
    assert "Starter" in response.json()["detail"]
    assert row(api).sync_interval_minutes != 10


def test_the_floor_itself_is_accepted(api):
    customer, staff = a_customer_and_staff(api)
    plan_id = make_plan(api, name="Starter", min_sync_interval_minutes=30)
    set_tenant(api, plan_id=plan_id)

    response = api.patch("/api/v1/tenant", json={"sync_interval_minutes": 30},
                          headers=head(customer))
    assert response.status_code == 200, response.text
    assert row(api).sync_interval_minutes == 30


def test_staff_can_still_set_a_tighter_interval_than_the_plan_allows(api):
    """The floor limits self-service, not the platform's own exceptions.

    Staff already override status, pairing rules and everything else on the
    account form directly — blocking them here would restrict the people
    running the platform, not protect anything.
    """
    customer, staff = a_customer_and_staff(api)
    plan_id = make_plan(api, name="Starter", min_sync_interval_minutes=30)
    tid = tenant_id(api)
    set_tenant(api, plan_id=plan_id)

    response = api.patch(f"/api/v1/admin/tenants/{tid}/schedule",
                          json={"sync_interval_minutes": 5}, headers=head(staff))
    assert response.status_code == 200, response.text
    assert row(api).sync_interval_minutes == 5


def test_no_plan_means_no_floor(api):
    customer, staff = a_customer_and_staff(api)
    response = api.patch("/api/v1/tenant", json={"sync_interval_minutes": 1},
                          headers=head(customer))
    assert response.status_code == 200, response.text


def test_a_customer_can_assign_their_own_plan(api):
    """Superseded by the self-service switch below: plan_id is now reachable
    on TenantUpdate on purpose, so a tenant can choose their own plan rather
    than only ever being assigned one by staff. See the "Self-service"
    section further down for the full set — this one just confirms the
    field, once forbidden, now actually works end to end."""
    customer, staff = a_customer_and_staff(api)
    plan_id = make_plan(api, name="Scale", max_employees=99999)

    response = api.patch("/api/v1/tenant", json={"plan_id": plan_id}, headers=head(customer))
    assert response.status_code == 200, response.text
    assert row(api).plan_id == plan_id


# ===========================================================================
# The sweep: who moves, and — the test that matters most — who never does
# ===========================================================================
def test_a_lapsed_trial_moves_to_past_due(api):
    customer, staff = a_customer_and_staff(api)
    set_tenant(api, status=TenantStatus.trialing.value,
               subscription_renews_at=datetime.now(timezone.utc) - timedelta(days=1))

    db = api.session_factory()
    result = sweep_subscriptions(db)
    db.close()

    assert result == {"lapsed": 1, "renewed": 0, "switched": 0}
    assert row(api).status == TenantStatus.past_due.value


def test_a_lapsed_active_account_moves_to_past_due_and_stops_syncing(api):
    customer, staff = a_customer_and_staff(api)
    give_it_a_device(api)
    set_tenant(api, status=TenantStatus.active.value,
               subscription_renews_at=datetime.now(timezone.utc) - timedelta(minutes=1))

    db = api.session_factory()
    sweep_subscriptions(db)
    db.close()

    assert row(api).status == TenantStatus.past_due.value
    db = api.session_factory()
    assert due_tenants(db) == [], "past_due is already excluded from SYNCABLE"
    db.close()
    # Proven at the door a customer can actually reach, not only at the
    # scheduler's own list — see test_subscription_gate.py for why the two
    # can disagree if only one of them is ever checked.
    assert api.post("/api/v1/sync/run", headers=head(customer)).status_code == 403


def test_a_renewed_account_moves_back_to_active(api):
    customer, staff = a_customer_and_staff(api)
    set_tenant(api, status=TenantStatus.past_due.value,
               subscription_renews_at=datetime.now(timezone.utc) - timedelta(days=40))
    # Someone — staff, or a future billing integration — pushes the date
    # forward. That is the only thing that changes.
    set_tenant(api, subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=30))

    db = api.session_factory()
    result = sweep_subscriptions(db)
    db.close()

    assert result == {"lapsed": 0, "renewed": 1, "switched": 0}
    assert row(api).status == TenantStatus.active.value


def test_a_suspended_account_is_never_swept_either_direction(api):
    """Suspension is a deliberate act and must outlast the sweep."""
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    api.post(f"/api/v1/admin/tenants/{tid}/deactivate", json={"reason": "unpaid"},
              headers=head(staff))
    set_tenant(api, subscription_renews_at=datetime.now(timezone.utc) - timedelta(days=1))

    db = api.session_factory()
    result = sweep_subscriptions(db)
    db.close()

    assert result == {"lapsed": 0, "renewed": 0, "switched": 0}
    assert row(api).status == TenantStatus.suspended.value


def test_a_cancelled_account_is_never_swept_either_direction(api):
    customer, staff = a_customer_and_staff(api)
    set_tenant(api, status=TenantStatus.cancelled.value,
               subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=5))

    db = api.session_factory()
    result = sweep_subscriptions(db)
    db.close()

    assert result == {"lapsed": 0, "renewed": 0, "switched": 0}
    assert row(api).status == TenantStatus.cancelled.value


def test_an_account_with_no_renewal_date_is_never_touched(api):
    """The one test this feature cannot ship without.

    Every tenant that existed before this column did has
    subscription_renews_at = NULL. If the sweep treated that as "already
    lapsed" instead of "not on an automatically-managed subscription",
    turning this feature on would suspend every existing customer the first
    time it ran.
    """
    customer, staff = a_customer_and_staff(api)
    give_it_a_device(api)
    for status in (TenantStatus.trialing.value, TenantStatus.active.value,
                   TenantStatus.past_due.value):
        set_tenant(api, status=status, subscription_renews_at=None)

        db = api.session_factory()
        result = sweep_subscriptions(db)
        db.close()

        assert result == {"lapsed": 0, "renewed": 0, "switched": 0}, status
        assert row(api).status == status, status


def test_the_sweep_is_a_safe_no_op_mid_subscription(api):
    customer, staff = a_customer_and_staff(api)
    set_tenant(api, status=TenantStatus.active.value,
               subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=20))

    db = api.session_factory()
    result = sweep_subscriptions(db)
    db.close()

    assert result == {"lapsed": 0, "renewed": 0, "switched": 0}
    assert row(api).status == TenantStatus.active.value


def test_the_full_loop_lapse_then_renew_flips_syncing_off_then_on_automatically(api):
    """The feature's actual claim, end to end: no staff click either way."""
    customer, staff = a_customer_and_staff(api)
    give_it_a_device(api)
    set_tenant(api, status=TenantStatus.active.value,
               subscription_renews_at=datetime.now(timezone.utc) + timedelta(minutes=1))

    db = api.session_factory()
    assert [t.slug for t in due_tenants(db)] == ["acme"]
    db.close()

    # The date passes with nobody touching the account.
    set_tenant(api, subscription_renews_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    db = api.session_factory()
    sweep_subscriptions(db)
    db.close()

    assert row(api).status == TenantStatus.past_due.value
    db = api.session_factory()
    assert due_tenants(db) == []
    db.close()
    assert api.post("/api/v1/sync/run", headers=head(customer)).status_code == 403

    # It is renewed — nothing but the date moves.
    set_tenant(api, subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=30))
    db = api.session_factory()
    sweep_subscriptions(db)
    db.close()

    assert row(api).status == TenantStatus.active.value
    db = api.session_factory()
    assert [t.slug for t in due_tenants(db)] == ["acme"]
    db.close()
    assert api.post("/api/v1/sync/run", headers=head(customer)).status_code != 403


# ===========================================================================
# The warning: advance notice, not a grace period
# ===========================================================================
def test_a_fresh_signup_does_not_immediately_warn(api):
    """The default trial is longer than the warning window, so a brand-new
    account does not open straight onto a warning about itself."""
    customer, staff = a_customer_and_staff(api)
    body = api.get("/api/v1/dashboard", headers=head(customer)).json()
    assert body["renewal_warning"] is None
    assert body["tenant"]["subscription_renews_at"] is not None


def test_a_dashboard_shows_no_warning_when_renewal_is_far_off(api):
    customer, staff = a_customer_and_staff(api)
    set_tenant(api, subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=20))

    body = api.get("/api/v1/dashboard", headers=head(customer)).json()
    assert body["renewal_warning"] is None


def test_a_dashboard_warns_once_the_renewal_date_is_close(api):
    customer, staff = a_customer_and_staff(api)
    set_tenant(api, subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=3))

    body = api.get("/api/v1/dashboard", headers=head(customer)).json()
    assert body["renewal_warning"] is not None
    assert body["renewal_warning"]["days_left"] in (2, 3)


def test_a_lapsed_account_gets_no_warning_the_stopped_banner_already_covers_it(api):
    customer, staff = a_customer_and_staff(api)
    set_tenant(api, status=TenantStatus.past_due.value,
               subscription_renews_at=datetime.now(timezone.utc) - timedelta(days=2))

    body = api.get("/api/v1/dashboard", headers=head(customer)).json()
    assert body["renewal_warning"] is None


def test_no_renewal_date_means_no_warning(api):
    customer, staff = a_customer_and_staff(api)
    set_tenant(api, subscription_renews_at=None)

    body = api.get("/api/v1/dashboard", headers=head(customer)).json()
    assert body["renewal_warning"] is None


def test_the_console_shows_the_same_warning_staff_side(api):
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    set_tenant(api, subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=1))

    body = api.get(f"/api/v1/admin/tenants/{tid}", headers=head(staff)).json()
    assert body["renewal_warning"] is not None
    assert body["renewal_warning"]["days_left"] in (0, 1)


def test_renewal_warning_boundary_is_inclusive(db, tenant):
    tenant.status = TenantStatus.active.value
    tenant.subscription_renews_at = datetime.now(timezone.utc) + timedelta(
        days=settings.subscription_warning_days
    )
    db.commit()
    assert renewal_warning(tenant) is not None

    tenant.subscription_renews_at = datetime.now(timezone.utc) + timedelta(
        days=settings.subscription_warning_days + 1
    )
    db.commit()
    assert renewal_warning(tenant) is None


def test_the_warning_turns_urgent_inside_its_own_window(db, tenant):
    """One warning, not two — urgent is a flag on it, not a second message.

    A trial and a paid plan are judged identically: both are just this
    tenant's own subscription_renews_at, whatever settings.trial_days or
    settings.billing_period_days it started from."""
    tenant.status = TenantStatus.trialing.value
    tenant.subscription_renews_at = datetime.now(timezone.utc) + timedelta(
        days=settings.subscription_urgent_days
    )
    db.commit()
    warning = renewal_warning(tenant)
    assert warning is not None
    assert warning["urgent"] is True

    tenant.status = TenantStatus.active.value
    tenant.subscription_renews_at = datetime.now(timezone.utc) + timedelta(
        days=settings.subscription_urgent_days + 1
    )
    db.commit()
    warning = renewal_warning(tenant)
    assert warning is not None
    assert warning["urgent"] is False


def test_urgency_is_visible_on_both_dashboard_and_console(api):
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    set_tenant(api, subscription_renews_at=datetime.now(timezone.utc)
               + timedelta(days=settings.subscription_urgent_days) - timedelta(hours=1))

    dash = api.get("/api/v1/dashboard", headers=head(customer)).json()
    assert dash["renewal_warning"]["urgent"] is True

    console = api.get(f"/api/v1/admin/tenants/{tid}", headers=head(staff)).json()
    assert console["renewal_warning"]["urgent"] is True


# ===========================================================================
# The Tenant-side convenience properties
# ===========================================================================
def test_plan_properties_are_null_safe_with_no_plan(db, tenant):
    assert tenant.plan_name is None
    assert tenant.plan_max_employees is None
    assert tenant.plan_min_sync_interval_minutes is None


def test_plan_properties_read_through_to_the_assigned_plan(db, tenant):
    plan = SubscriptionPlan(name="Growth", max_employees=50, min_sync_interval_minutes=15)
    db.add(plan)
    db.flush()
    tenant.plan_id = plan.id
    db.commit()

    assert tenant.plan_name == "Growth"
    assert tenant.plan_max_employees == 50
    assert tenant.plan_min_sync_interval_minutes == 15


# ===========================================================================
# Assigning a plan
# ===========================================================================
def test_signup_gets_the_default_plan_and_a_trial_renewal_date(api):
    make_plan(api, name="Starter", is_default=True)
    customer = signup(api, "Acme", CUSTOMER)

    body = api.get("/api/v1/tenant", headers=head(customer)).json()
    assert body["plan_name"] == "Starter"
    assert body["subscription_renews_at"] is not None


def test_signup_starts_within_its_own_plans_floor(api):
    """A fresh signup must not open on a setting its own plan would reject.

    Signup has no interval field of its own — it always uses the platform
    default, which can be faster than a plan's floor (Starter's 60-minute
    floor against a much shorter platform default, say). Caught by an actual
    screenshot of a fresh signup showing "15" next to "syncs no faster than
    every 60 min" before this was fixed.
    """
    make_plan(api, name="Starter", is_default=True, min_sync_interval_minutes=60)
    customer = signup(api, "Acme", CUSTOMER)

    body = api.get("/api/v1/tenant", headers=head(customer)).json()
    assert body["sync_interval_minutes"] >= 60


def test_signup_with_no_default_plan_gets_no_plan_but_still_a_trial_clock(api):
    customer = signup(api, "Acme", CUSTOMER)
    body = api.get("/api/v1/tenant", headers=head(customer)).json()
    assert body["plan_name"] is None
    assert body["subscription_renews_at"] is not None


def test_staff_can_list_plans(api):
    _, staff = a_customer_and_staff(api)
    make_plan(api, name="Starter")
    make_plan(api, name="Growth")

    names = {p["name"] for p in api.get("/api/v1/admin/plans", headers=head(staff)).json()}
    assert names == {"Starter", "Growth"}


def test_an_ordinary_customer_cannot_list_plans(api):
    customer, _ = a_customer_and_staff(api)
    assert api.get("/api/v1/admin/plans", headers=head(customer)).status_code == 403


def test_staff_can_assign_a_plan_to_an_existing_tenant(api):
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    plan_id = make_plan(api, name="Growth", max_employees=50)

    response = api.patch(f"/api/v1/admin/tenants/{tid}/config",
                          json={"plan_id": plan_id}, headers=head(staff))
    assert response.status_code == 200, response.text
    assert response.json()["plan_name"] == "Growth"


def test_assigning_an_unknown_plan_is_rejected(api):
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    response = api.patch(f"/api/v1/admin/tenants/{tid}/config",
                          json={"plan_id": "not-a-real-plan"}, headers=head(staff))
    assert response.status_code == 400


def test_staff_can_clear_a_tenants_plan(api):
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    plan_id = make_plan(api, name="Growth")
    api.patch(f"/api/v1/admin/tenants/{tid}/config", json={"plan_id": plan_id}, headers=head(staff))

    response = api.patch(f"/api/v1/admin/tenants/{tid}/config",
                          json={"plan_id": None}, headers=head(staff))
    assert response.status_code == 200, response.text
    assert response.json()["plan_name"] is None
    assert row(api).plan_id is None


def test_staff_can_set_a_tenants_renewal_date(api):
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    new_date = (datetime.now(timezone.utc) + timedelta(days=45)).replace(microsecond=0)

    response = api.patch(f"/api/v1/admin/tenants/{tid}/config",
                          json={"subscription_renews_at": new_date.isoformat()},
                          headers=head(staff))
    assert response.status_code == 200, response.text
    assert row(api).subscription_renews_at is not None


def test_creating_a_tenant_with_an_explicit_plan(api):
    _, staff = a_customer_and_staff(api)
    plan_id = make_plan(api, name="Scale")

    response = api.post("/api/v1/admin/tenants", json={
        "company_name": "Globex", "owner_email": "owner@globex.example.com",
        "plan_id": plan_id,
    }, headers=head(staff))
    assert response.status_code == 201, response.text
    assert response.json()["tenant"]["plan_name"] == "Scale"


def test_creating_a_tenant_falls_back_to_the_default_plan(api):
    _, staff = a_customer_and_staff(api)
    make_plan(api, name="Starter", is_default=True)

    response = api.post("/api/v1/admin/tenants", json={
        "company_name": "Globex", "owner_email": "owner@globex.example.com",
    }, headers=head(staff))
    assert response.status_code == 201, response.text
    assert response.json()["tenant"]["plan_name"] == "Starter"


def test_creating_a_tenant_with_an_unknown_plan_is_rejected(api):
    _, staff = a_customer_and_staff(api)
    response = api.post("/api/v1/admin/tenants", json={
        "company_name": "Globex", "owner_email": "owner@globex.example.com",
        "plan_id": "not-a-real-plan",
    }, headers=head(staff))
    assert response.status_code == 400


# ===========================================================================
# Self-service: a tenant choosing their own plan
# ===========================================================================
# Signup's own picker (GET /auth/plans, SignupRequest.plan_id) — a customer
# choice, distinct from the staff-onboarding path above and from the
# always-the-default fallback that existed before it.
def test_the_public_plan_list_needs_no_token_and_excludes_retired_plans(api):
    make_plan(api, name="Starter", is_active=True, monthly_price_cents=4900)
    make_plan(api, name="Old Tier", is_active=False)

    response = api.get("/api/v1/auth/plans")
    assert response.status_code == 200, response.text
    names = [p["name"] for p in response.json()]
    assert names == ["Starter"]


def test_signup_can_choose_a_plan_explicitly(api):
    make_plan(api, name="Starter", is_default=True, min_sync_interval_minutes=15)
    growth_id = make_plan(api, name="Growth", min_sync_interval_minutes=5)

    response = api.post("/api/v1/auth/signup", json={
        "company_name": "Acme", "email": CUSTOMER,
        "password": "a-long-enough-password", "timezone": "Asia/Dubai",
        "plan_id": growth_id,
    })
    assert response.status_code == 201, response.text
    assert row(api).plan_id == growth_id


def test_signup_with_no_plan_choice_still_falls_back_to_the_default(api):
    make_plan(api, name="Starter", is_default=True)

    response = api.post("/api/v1/auth/signup", json={
        "company_name": "Acme", "email": CUSTOMER,
        "password": "a-long-enough-password", "timezone": "Asia/Dubai",
    })
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]
    assert api.get("/api/v1/tenant", headers=head(token)).json()["plan_name"] == "Starter"


def test_signup_rejects_an_unknown_plan(api):
    response = api.post("/api/v1/auth/signup", json={
        "company_name": "Acme", "email": CUSTOMER,
        "password": "a-long-enough-password", "timezone": "Asia/Dubai",
        "plan_id": "not-a-real-plan",
    })
    assert response.status_code == 400


def test_signup_rejects_a_retired_plan(api):
    retired_id = make_plan(api, name="Old Tier", is_active=False)

    response = api.post("/api/v1/auth/signup", json={
        "company_name": "Acme", "email": CUSTOMER,
        "password": "a-long-enough-password", "timezone": "Asia/Dubai",
        "plan_id": retired_id,
    })
    assert response.status_code == 400


# PATCH /tenant's own plan_id — switching afterward, repeatably, from
# Settings. See app.api.v1.sync.update_tenant.
def test_a_customer_can_switch_their_own_plan(api):
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter", min_sync_interval_minutes=15)
    growth_id = make_plan(api, name="Growth", min_sync_interval_minutes=5)
    set_tenant(api, plan_id=starter_id)

    response = api.patch("/api/v1/tenant", json={"plan_id": growth_id}, headers=head(customer))
    assert response.status_code == 200, response.text
    assert response.json()["plan_name"] == "Growth"
    assert row(api).plan_id == growth_id


def test_switching_plans_is_repeatable(api):
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")

    for plan_id, name in [(starter_id, "Starter"), (growth_id, "Growth"), (starter_id, "Starter")]:
        response = api.patch("/api/v1/tenant", json={"plan_id": plan_id}, headers=head(customer))
        assert response.status_code == 200, response.text
        assert row(api).plan_id == plan_id


def test_a_customer_cannot_switch_to_an_unknown_plan(api):
    customer, staff = a_customer_and_staff(api)
    response = api.patch("/api/v1/tenant", json={"plan_id": "not-a-real-plan"},
                          headers=head(customer))
    assert response.status_code == 400


def test_a_customer_cannot_switch_to_a_retired_plan(api):
    customer, staff = a_customer_and_staff(api)
    retired_id = make_plan(api, name="Old Tier", is_active=False)
    response = api.patch("/api/v1/tenant", json={"plan_id": retired_id}, headers=head(customer))
    assert response.status_code == 400


def test_a_customer_cannot_clear_their_own_plan(api):
    """Unlike the staff console's TenantConfigUpdate.plan_id, which can send
    ``null`` to unassign a plan entirely, self-service can only move to
    another active plan — going planless (and so unenforced) is not a
    customer's own call to make."""
    customer, staff = a_customer_and_staff(api)
    plan_id = make_plan(api, name="Starter")
    set_tenant(api, plan_id=plan_id)

    response = api.patch("/api/v1/tenant", json={"plan_id": None}, headers=head(customer))
    assert response.status_code == 400
    assert row(api).plan_id == plan_id


def test_switching_to_a_stricter_plan_auto_raises_the_interval(api):
    """The same fix already applied to a fresh signup: an account must not
    end up holding a sync interval its own (new) plan would reject."""
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter", min_sync_interval_minutes=15)
    growth_id = make_plan(api, name="Growth", min_sync_interval_minutes=60)
    set_tenant(api, plan_id=starter_id, sync_interval_minutes=15)

    response = api.patch("/api/v1/tenant", json={"plan_id": growth_id}, headers=head(customer))
    assert response.status_code == 200, response.text
    assert row(api).sync_interval_minutes == 60


def test_switching_plans_with_an_explicit_interval_is_judged_against_the_new_plan(api):
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter", min_sync_interval_minutes=15)
    growth_id = make_plan(api, name="Growth", min_sync_interval_minutes=60)
    set_tenant(api, plan_id=starter_id, sync_interval_minutes=15)

    # 20 minutes is fine against the old plan's floor but not the new one's —
    # a combined request must be judged against where the save is headed.
    response = api.patch("/api/v1/tenant",
                          json={"plan_id": growth_id, "sync_interval_minutes": 20},
                          headers=head(customer))
    assert response.status_code == 400, response.text
    assert "Growth" in response.json()["detail"]
    assert row(api).plan_id == starter_id

    response = api.patch("/api/v1/tenant",
                          json={"plan_id": growth_id, "sync_interval_minutes": 60},
                          headers=head(customer))
    assert response.status_code == 200, response.text
    assert row(api).plan_id == growth_id
    assert row(api).sync_interval_minutes == 60


def test_downgrading_ones_own_plan_never_unmaps_an_existing_match(api):
    """The grandfathering rule already built for staff-driven plan changes
    (SyncEngine._resolve_mappings counting current DB state) applies exactly
    as-is here — self-service switching is just another way plan_id changes,
    and nothing in update_tenant touches EmployeeMapping at all."""
    customer, staff = a_customer_and_staff(api)
    give_it_a_device(api)
    scale_id = make_plan(api, name="Scale", max_employees=None)
    starter_id = make_plan(api, name="Starter", max_employees=1)
    set_tenant(api, plan_id=scale_id)

    db = api.session_factory()
    tid = row(api).id
    db.add(EmployeeMapping(
        tenant_id=tid, emp_code="0001", odoo_employee_id=7,
        status=MappingStatus.mapped.value, match_method="manual",
    ))
    db.commit()
    db.close()

    response = api.patch("/api/v1/tenant", json={"plan_id": starter_id}, headers=head(customer))
    assert response.status_code == 200, response.text

    db = api.session_factory()
    mapping = db.scalars(
        select(EmployeeMapping).where(EmployeeMapping.tenant_id == tid)
    ).first()
    assert mapping.status == MappingStatus.mapped.value
    assert mapping.odoo_employee_id == 7
    db.close()


# ===========================================================================
# Skipping the trial at signup
# ===========================================================================
def test_signup_can_skip_the_trial_with_a_chosen_plan(api):
    plan_id = make_plan(api, name="Growth", is_default=True)

    response = api.post("/api/v1/auth/signup", json={
        "company_name": "Acme", "email": CUSTOMER,
        "password": "a-long-enough-password", "timezone": "Asia/Dubai",
        "plan_id": plan_id, "skip_trial": True,
    })
    assert response.status_code == 201, response.text
    tenant = row(api)
    assert tenant.status == TenantStatus.active.value
    assert tenant.plan_id == plan_id
    # Roughly settings.billing_period_days out, not settings.trial_days.
    delta = tenant.subscription_renews_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert abs(delta.days - settings.billing_period_days) <= 1


def test_signup_without_skip_trial_still_starts_trialing(api):
    make_plan(api, name="Growth", is_default=True)
    response = api.post("/api/v1/auth/signup", json={
        "company_name": "Acme", "email": CUSTOMER,
        "password": "a-long-enough-password", "timezone": "Asia/Dubai",
    })
    assert response.status_code == 201, response.text
    assert row(api).status == TenantStatus.trialing.value


def test_skipping_the_trial_requires_a_plan(api):
    response = api.post("/api/v1/auth/signup", json={
        "company_name": "Acme", "email": CUSTOMER,
        "password": "a-long-enough-password", "timezone": "Asia/Dubai",
        "skip_trial": True,
    })
    assert response.status_code == 400
    assert "plan" in response.json()["detail"].lower()


def test_skipping_the_trial_rejects_an_unknown_plan(api):
    response = api.post("/api/v1/auth/signup", json={
        "company_name": "Acme", "email": CUSTOMER,
        "password": "a-long-enough-password", "timezone": "Asia/Dubai",
        "skip_trial": True, "plan_id": "not-a-real-plan",
    })
    assert response.status_code == 400


# ===========================================================================
# Deferred switching: a plan already paid for waits for its renewal
# ===========================================================================
def test_switching_while_trialing_applies_immediately(api):
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")
    set_tenant(api, status=TenantStatus.trialing.value, plan_id=starter_id)

    response = api.patch("/api/v1/tenant", json={"plan_id": growth_id}, headers=head(customer))
    assert response.status_code == 200, response.text
    assert response.json()["plan_id"] == growth_id
    assert response.json()["pending_plan_id"] is None
    assert row(api).plan_id == growth_id
    assert row(api).pending_plan_id is None


def test_switching_while_past_due_applies_immediately(api):
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")
    set_tenant(api, status=TenantStatus.past_due.value, plan_id=starter_id)

    response = api.patch("/api/v1/tenant", json={"plan_id": growth_id}, headers=head(customer))
    assert response.status_code == 200, response.text
    assert row(api).plan_id == growth_id
    assert row(api).pending_plan_id is None


def test_switching_while_active_with_no_plan_yet_applies_immediately(api):
    """status=active but plan_id is still null isn't "already paid for a
    plan" in any meaningful sense — nothing to protect by deferring."""
    customer, staff = a_customer_and_staff(api)
    growth_id = make_plan(api, name="Growth")
    set_tenant(api, status=TenantStatus.active.value, plan_id=None)

    response = api.patch("/api/v1/tenant", json={"plan_id": growth_id}, headers=head(customer))
    assert response.status_code == 200, response.text
    assert row(api).plan_id == growth_id
    assert row(api).pending_plan_id is None


def test_switching_while_active_and_already_on_a_plan_defers_to_renewal(api):
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")
    renews_at = datetime.now(timezone.utc) + timedelta(days=20)
    set_tenant(api, status=TenantStatus.active.value, plan_id=starter_id,
               subscription_renews_at=renews_at)

    response = api.patch("/api/v1/tenant", json={"plan_id": growth_id}, headers=head(customer))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_id"] == starter_id, "unchanged until the renewal"
    assert body["pending_plan_id"] == growth_id
    assert body["pending_plan_name"] == "Growth"

    tenant = row(api)
    assert tenant.plan_id == starter_id
    assert tenant.pending_plan_id == growth_id


def test_a_deferred_switch_does_not_check_the_interval_against_the_new_plan(api):
    """Not in effect yet, so the floor judged right now is still the plan
    actually in force — a combined request only reaches the new plan's floor
    once the switch itself has landed."""
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter", min_sync_interval_minutes=15)
    growth_id = make_plan(api, name="Growth", min_sync_interval_minutes=60)
    set_tenant(api, status=TenantStatus.active.value, plan_id=starter_id,
               sync_interval_minutes=15,
               subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=20))

    response = api.patch("/api/v1/tenant",
                          json={"plan_id": growth_id, "sync_interval_minutes": 15},
                          headers=head(customer))
    assert response.status_code == 200, response.text
    assert row(api).sync_interval_minutes == 15
    assert row(api).pending_plan_id == growth_id


def test_choosing_the_current_plan_cancels_a_pending_switch(api):
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")
    set_tenant(api, status=TenantStatus.active.value, plan_id=starter_id,
               pending_plan_id=growth_id,
               subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=20))

    response = api.patch("/api/v1/tenant", json={"plan_id": starter_id}, headers=head(customer))
    assert response.status_code == 200, response.text
    assert response.json()["pending_plan_id"] is None
    tenant = row(api)
    assert tenant.plan_id == starter_id
    assert tenant.pending_plan_id is None


def test_switching_again_replaces_the_previously_queued_plan(api):
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")
    scale_id = make_plan(api, name="Scale")
    set_tenant(api, status=TenantStatus.active.value, plan_id=starter_id,
               subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=20))

    api.patch("/api/v1/tenant", json={"plan_id": growth_id}, headers=head(customer))
    assert row(api).pending_plan_id == growth_id

    api.patch("/api/v1/tenant", json={"plan_id": scale_id}, headers=head(customer))
    assert row(api).pending_plan_id == scale_id
    assert row(api).plan_id == starter_id, "still not landed"


def test_staff_setting_plan_id_directly_clears_a_pending_switch(api):
    """Staff's own override — TenantConfigUpdate.plan_id, applied
    immediately as always — outranks whatever a customer had queued."""
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")
    scale_id = make_plan(api, name="Scale")
    tid = tenant_id(api)
    set_tenant(api, status=TenantStatus.active.value, plan_id=starter_id,
               pending_plan_id=growth_id,
               subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=20))

    response = api.patch(f"/api/v1/admin/tenants/{tid}/config",
                          json={"plan_id": scale_id}, headers=head(staff))
    assert response.status_code == 200, response.text
    tenant = row(api)
    assert tenant.plan_id == scale_id
    assert tenant.pending_plan_id is None


# ===========================================================================
# Promotion: a deferred switch landing at the renewal date
# ===========================================================================
def test_a_pending_switch_is_promoted_once_the_renewal_date_is_reached(api):
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")
    set_tenant(api, status=TenantStatus.active.value, plan_id=starter_id,
               pending_plan_id=growth_id,
               subscription_renews_at=datetime.now(timezone.utc) - timedelta(minutes=1))

    db = api.session_factory()
    result = sweep_subscriptions(db)
    db.close()

    assert result["switched"] == 1
    tenant = row(api)
    assert tenant.plan_id == growth_id
    assert tenant.pending_plan_id is None


def test_promotion_raises_the_interval_to_the_new_plans_floor(api):
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter", min_sync_interval_minutes=15)
    growth_id = make_plan(api, name="Growth", min_sync_interval_minutes=60)
    set_tenant(api, status=TenantStatus.active.value, plan_id=starter_id,
               pending_plan_id=growth_id, sync_interval_minutes=15,
               subscription_renews_at=datetime.now(timezone.utc) - timedelta(minutes=1))

    db = api.session_factory()
    sweep_subscriptions(db)
    db.close()

    assert row(api).sync_interval_minutes == 60


def test_a_pending_switch_survives_a_lapse_into_past_due(api):
    """Promoted the same tick the account lapses — the switch is not lost
    just because nobody renewed in time."""
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")
    set_tenant(api, status=TenantStatus.active.value, plan_id=starter_id,
               pending_plan_id=growth_id,
               subscription_renews_at=datetime.now(timezone.utc) - timedelta(minutes=1))

    db = api.session_factory()
    result = sweep_subscriptions(db)
    db.close()

    assert result == {"lapsed": 1, "renewed": 0, "switched": 1}
    tenant = row(api)
    assert tenant.status == TenantStatus.past_due.value
    assert tenant.plan_id == growth_id
    assert tenant.pending_plan_id is None


def test_a_pending_switch_stays_queued_while_still_past_due(api):
    """An already-lapsed account whose date has not moved: still not
    renewed, but the switch — already promoted on the lapsing tick — must
    not be lost, and a *second* sweep with nothing new to do must not error
    or re-promote something that is no longer pending."""
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")
    set_tenant(api, status=TenantStatus.past_due.value, plan_id=starter_id,
               pending_plan_id=growth_id,
               subscription_renews_at=datetime.now(timezone.utc) - timedelta(days=5))

    db = api.session_factory()
    result = sweep_subscriptions(db)
    db.close()

    assert result["switched"] == 1
    tenant = row(api)
    assert tenant.status == TenantStatus.past_due.value, "still not renewed"
    assert tenant.plan_id == growth_id
    assert tenant.pending_plan_id is None

    db = api.session_factory()
    result_again = sweep_subscriptions(db)
    db.close()
    assert result_again["switched"] == 0


def test_a_pending_switch_with_no_renewal_date_is_never_promoted(api):
    """The load-bearing null-date rule applies here too: nothing here may
    invent a promotion for an account nobody told the sweep to watch."""
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")
    set_tenant(api, status=TenantStatus.active.value, plan_id=starter_id,
               pending_plan_id=growth_id, subscription_renews_at=None)

    db = api.session_factory()
    result = sweep_subscriptions(db)
    db.close()

    assert result == {"lapsed": 0, "renewed": 0, "switched": 0}
    tenant = row(api)
    assert tenant.plan_id == starter_id
    assert tenant.pending_plan_id == growth_id


def test_a_pending_switch_not_yet_due_is_left_alone(api):
    customer, staff = a_customer_and_staff(api)
    starter_id = make_plan(api, name="Starter")
    growth_id = make_plan(api, name="Growth")
    set_tenant(api, status=TenantStatus.active.value, plan_id=starter_id,
               pending_plan_id=growth_id,
               subscription_renews_at=datetime.now(timezone.utc) + timedelta(days=5))

    db = api.session_factory()
    result = sweep_subscriptions(db)
    db.close()

    assert result["switched"] == 0
    tenant = row(api)
    assert tenant.plan_id == starter_id
    assert tenant.pending_plan_id == growth_id
