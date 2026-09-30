"""Staff configuring accounts by plan: managing plans from the console,
per-account overrides of a plan's limits, usage against them, and the plan's
sync floor applied when a plan is assigned."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Device, DeviceSource
from tests.test_platform_admin import api, head  # noqa: F401
from tests.test_subscription_gate import give_it_a_device, row, tenant_id
from tests.test_subscription_plans import a_customer_and_staff, make_plan, set_tenant

PLANS = "/api/v1/admin/plans"


def cfg(api, staff, tid, **body):
    return api.patch(f"/api/v1/admin/tenants/{tid}/config", json=body, headers=head(staff))


# --- managing plans ---------------------------------------------------------
def test_staff_create_edit_and_retire_a_plan(api):
    customer, staff = a_customer_and_staff(api)
    made = api.post(PLANS, json={"name": "Pro", "monthly_price_cents": 9900, "max_employees": 50,
                                 "max_devices": 3, "min_sync_interval_minutes": 30,
                                 "stripe_price_id": "price_123"}, headers=head(staff))
    assert made.status_code == 201, made.text
    plan = made.json()
    assert plan["max_devices"] == 3 and plan["stripe_price_id"] == "price_123" and plan["tenants"] == 0
    public = [p for p in api.get("/api/v1/auth/plans").json() if p["id"] == plan["id"]]
    assert public and "stripe_price_id" not in public[0], "customers never see the Stripe link"

    edited = api.patch(f"{PLANS}/{plan['id']}", json={"max_devices": None, "is_active": False},
                       headers=head(staff)).json()
    assert edited["max_devices"] is None and edited["is_active"] is False

    assert api.post(PLANS, json={"name": "X"}, headers=head(customer)).status_code == 403


def test_plan_names_are_unique_and_only_one_plan_is_default(api):
    _, staff = a_customer_and_staff(api)
    a = api.post(PLANS, json={"name": "A", "is_default": True}, headers=head(staff)).json()
    assert api.post(PLANS, json={"name": "a"}, headers=head(staff)).status_code == 409
    b = api.post(PLANS, json={"name": "B", "is_default": True}, headers=head(staff)).json()
    plans = {p["id"]: p for p in api.get(PLANS, headers=head(staff)).json()}
    assert plans[b["id"]]["is_default"] and not plans[a["id"]]["is_default"]

    retired = api.patch(f"{PLANS}/{b['id']}", json={"is_active": False}, headers=head(staff)).json()
    assert retired["is_default"] is False, "a retired plan is never handed to new accounts"


def test_a_bad_stripe_price_is_refused(api):
    _, staff = a_customer_and_staff(api)
    assert api.post(PLANS, json={"name": "Q", "stripe_price_id": "prod_1"},
                    headers=head(staff)).status_code == 422


# --- overrides --------------------------------------------------------------
def test_an_override_beats_the_plan_and_zero_means_unlimited(api):
    _, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    set_tenant(api, plan_id=make_plan(api, name="Growth", max_devices=5, max_employees=150))

    out = cfg(api, staff, tid, limit_max_devices=8, limit_max_employees=0).json()
    assert out["max_devices"] == 8 and out["max_employees"] is None
    assert out["limit_max_devices"] == 8

    out = cfg(api, staff, tid, limit_max_devices=None).json()
    assert out["max_devices"] == 5, "clearing an override goes back to the plan"


def test_an_override_raises_the_device_allowance_for_held_terminals(api):
    _, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    set_tenant(api, plan_id=make_plan(api, name="One", max_devices=1))
    give_it_a_device(api)
    db = api.session_factory()
    source_id = db.scalar(select(DeviceSource.id).where(DeviceSource.tenant_id == tid))
    db.add_all([Device(tenant_id=tid, source_id=source_id, serial_number=f"SN{i}") for i in range(3)])
    db.commit()
    db.close()

    usage = api.get(f"/api/v1/admin/tenants/{tid}/usage", headers=head(staff)).json()
    assert usage["devices"] == 3 and usage["max_devices"] == 1 and usage["devices_over_limit"] == 2

    cfg(api, staff, tid, limit_max_devices=3)
    usage = api.get(f"/api/v1/admin/tenants/{tid}/usage", headers=head(staff)).json()
    assert usage["devices_over_limit"] == 0


# --- plan defaults on assign ------------------------------------------------
def test_assigning_a_plan_raises_the_interval_to_its_floor(api):
    _, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    set_tenant(api, sync_interval_minutes=5)
    starter = make_plan(api, name="Starter", min_sync_interval_minutes=60)

    out = cfg(api, staff, tid, plan_id=starter).json()
    assert out["sync_interval_minutes"] == 60
    assert row(api).sync_interval_minutes == 60


def test_an_interval_override_lets_one_account_sync_faster(api):
    _, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    set_tenant(api, sync_interval_minutes=5)
    starter = make_plan(api, name="Starter", min_sync_interval_minutes=60)

    out = cfg(api, staff, tid, plan_id=starter, limit_min_sync_interval_minutes=15).json()
    assert out["min_sync_interval_minutes"] == 15 and out["sync_interval_minutes"] == 15


def test_a_staff_created_account_starts_inside_its_plan(api):
    _, staff = a_customer_and_staff(api)
    starter = make_plan(api, name="Starter", min_sync_interval_minutes=60)
    made = api.post("/api/v1/admin/tenants", json={
        "company_name": "Muscat Traders", "owner_email": "boss@muscat.example.com",
        "timezone": "Asia/Dubai", "sync_interval_minutes": 15, "plan_id": starter,
    }, headers=head(staff))
    assert made.status_code in (200, 201), made.text
    assert made.json()["tenant"]["sync_interval_minutes"] == 60
