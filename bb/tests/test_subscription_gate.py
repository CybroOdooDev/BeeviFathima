"""Stopping and restarting one customer's syncing, from the platform console.

This is the lever behind a subscription: when someone stops paying, their
syncing stops, and when they pay it starts again. The account keeps its data and
its screens throughout — nothing here destroys anything, which is what makes it
safe to use for a billing decision that may turn out to be wrong.

The status field and SYNCABLE already existed, so the interesting part is not
that suspending works. It is the two things that made it advisory rather than a
gate:

* the customer's own "Sync now" never checked the status, so a suspended
  account could simply press the button and the run went through; and
* nothing told the customer, while the sidebar kept showing a healthy green
  pill and the overview blamed a switch in their own Settings that was plainly
  still on.

Both are asserted below, along with the boundary that matters most: the
customer cannot restore themselves.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import AuditLog, Tenant, TenantStatus
from app.services.scheduling import due_tenants
from app.services.sync_engine import SyncEngine

# The console fixture and account helpers, so "a customer" and "a staff session"
# mean the same thing here as they do next door.
from tests.test_platform_admin import (  # noqa: F401
    api,
    head,
    make_staff,
    promote,
    signup,
    staff_login,
)

CUSTOMER = "owner@acme.example.com"


def a_customer_and_staff(api):
    """An Acme owner's session, and a console session that can act on it."""
    customer = signup(api, "Acme", CUSTOMER)
    staff = make_staff(api)
    return customer, staff


def tenant_id(api, slug="acme") -> str:
    db = api.session_factory()
    found = db.scalars(select(Tenant).where(Tenant.slug == slug)).first().id
    db.close()
    return found


def deactivate(api, staff, tid, reason=None):
    return api.post(f"/api/v1/admin/tenants/{tid}/deactivate",
                    json={"reason": reason}, headers=head(staff))


def activate(api, staff, tid):
    return api.post(f"/api/v1/admin/tenants/{tid}/activate", headers=head(staff))


def give_it_a_device(api, slug="acme") -> None:
    """A tenant with nothing to poll is never due, whatever its status.

    So the schedule assertions below need a source, or they would pass for the
    wrong reason — an empty due list proves nothing about the gate if the
    account was never going to be picked up anyway.
    """
    from app.core.crypto import encrypt
    from app.models import DeviceSource

    db = api.session_factory()
    tenant = db.scalars(select(Tenant).where(Tenant.slug == slug)).first()
    db.add(DeviceSource(
        tenant_id=tenant.id, name="BioTime", base_url="https://bio.test",
        username="svc", password_enc=encrypt("pw", tenant.crypto_key),
        server_timezone="Asia/Dubai", is_active=True,
    ))
    db.commit()
    db.close()


def row(api, slug="acme") -> Tenant:
    db = api.session_factory()
    found = db.scalars(select(Tenant).where(Tenant.slug == slug)).first()
    db.expunge(found)
    db.close()
    return found


# ===========================================================================
# The gate closes every door, not just the clock
# ===========================================================================
def test_deactivating_takes_the_account_off_the_schedule(api):
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    give_it_a_device(api)

    db = api.session_factory()
    assert [t.slug for t in due_tenants(db)] == ["acme"]
    db.close()

    assert deactivate(api, staff, tid).status_code == 200

    db = api.session_factory()
    assert due_tenants(db) == [], "a stopped account must not be picked up"
    db.close()


def test_deactivating_stops_the_customers_own_sync_button(api):
    """The hole this closes.

    Suspension used to stop only the scheduler. The customer's button called
    straight into the engine, so an account stopped for non-payment could keep
    syncing for as long as somebody kept pressing it.
    """
    customer, staff = a_customer_and_staff(api)
    deactivate(api, staff, tenant_id(api))

    response = api.post("/api/v1/sync/run", headers=head(customer))
    assert response.status_code == 403, response.text
    assert "suspended" in response.json()["detail"]
    assert "still visible" in response.json()["detail"], (
        "the refusal should say their data is intact — this message is read by "
        "someone who has just watched their attendance stop"
    )


def test_deactivating_stops_the_inline_sync_too(api):
    """Onboarding's 'first sync' is a second door into the same engine."""
    customer, staff = a_customer_and_staff(api)
    deactivate(api, staff, tenant_id(api))
    assert api.post("/api/v1/sync/run-inline", headers=head(customer)).status_code == 403


def test_the_staff_button_refuses_a_stopped_account_as_well(api):
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    deactivate(api, staff, tid)

    response = api.post(f"/api/v1/admin/tenants/{tid}/sync", headers=head(staff))
    assert response.status_code == 400, response.text


def test_the_engine_itself_refuses_a_stopped_account(api):
    """The backstop, tested at the choke point rather than through a route.

    Every caller is checked, but this is the one place a cycle can start, so a
    route added later that forgets still cannot sync a stopped account.
    """
    customer, staff = a_customer_and_staff(api)
    deactivate(api, staff, tenant_id(api))

    db = api.session_factory()
    tenant = db.scalars(select(Tenant).where(Tenant.slug == "acme")).first()
    before = tenant.consecutive_failures

    run = SyncEngine(db, tenant, "manual").run_cycle()

    assert run.status == "failed"
    assert "does not sync" in (run.error_message or "")
    assert tenant.consecutive_failures == before, (
        "a stopped account is not a failing one: counting this would slow-lane "
        "and badge as degraded an account that is working perfectly well"
    )
    db.close()


# ===========================================================================
# The customer cannot let themselves back in
# ===========================================================================
def test_the_customer_cannot_change_their_own_status(api):
    """Status is absent from TenantUpdate, so the field is not reachable."""
    customer, staff = a_customer_and_staff(api)
    deactivate(api, staff, tenant_id(api))

    api.patch("/api/v1/tenant", json={"status": "active"}, headers=head(customer))

    assert row(api).status == TenantStatus.suspended.value
    assert api.post("/api/v1/sync/run", headers=head(customer)).status_code == 403


def test_flipping_their_own_sync_switch_does_not_restore_syncing(api):
    """The two switches are different questions, and this is why it matters.

    If suspension had been implemented as sync_enabled=False, this test would
    be the whole bug: the customer turns it back on and carries on.
    """
    customer, staff = a_customer_and_staff(api)
    deactivate(api, staff, tenant_id(api))

    give_it_a_device(api)
    api.patch("/api/v1/tenant", json={"sync_enabled": True}, headers=head(customer))

    assert api.post("/api/v1/sync/run", headers=head(customer)).status_code == 403
    db = api.session_factory()
    assert due_tenants(db) == []
    db.close()


# ===========================================================================
# What deactivating leaves alone
# ===========================================================================
def test_deactivating_does_not_touch_the_customers_own_switch(api):
    customer, staff = a_customer_and_staff(api)
    assert row(api).sync_enabled is True
    deactivate(api, staff, tenant_id(api))
    assert row(api).sync_enabled is True, (
        "moving their switch would look to them like they had done it"
    )


def test_activating_gives_back_the_setting_they_chose(api):
    """An account that had its own sync off must not come back with it on."""
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    api.patch("/api/v1/tenant", json={"sync_enabled": False}, headers=head(customer))

    give_it_a_device(api)
    deactivate(api, staff, tid)
    activate(api, staff, tid)

    assert row(api).sync_enabled is False, "reactivating decided for them"
    assert row(api).status == TenantStatus.active.value

    db = api.session_factory()
    assert due_tenants(db) == [], "their own switch is still off, so still not due"
    db.close()


def test_activating_restores_syncing_for_an_ordinary_account(api):
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    give_it_a_device(api)
    deactivate(api, staff, tid)
    assert activate(api, staff, tid).status_code == 200

    db = api.session_factory()
    assert [t.slug for t in due_tenants(db)] == ["acme"]
    db.close()
    assert api.post("/api/v1/sync/run", headers=head(customer)).status_code != 403


def test_the_customer_keeps_their_screens_and_their_records(api):
    """Deliberate: nothing is taken away, only the collection of new punches.

    A gate that hid a customer's existing attendance the moment an invoice was
    late would make a billing mistake into a payroll incident.
    """
    customer, staff = a_customer_and_staff(api)
    deactivate(api, staff, tenant_id(api))

    for path in ("/api/v1/tenant", "/api/v1/dashboard", "/api/v1/attendance",
                 "/api/v1/punches", "/api/v1/sync/runs"):
        assert api.get(path, headers=head(customer)).status_code == 200, path


# ===========================================================================
# What each side is told
# ===========================================================================
def test_the_customer_is_told_their_account_is_stopped(api):
    """Before this, the dashboard said nothing and the pill stayed green."""
    customer, staff = a_customer_and_staff(api)
    deactivate(api, staff, tenant_id(api), reason="unpaid invoice 4021")

    body = api.get("/api/v1/tenant", headers=head(customer)).json()
    assert body["syncable"] is False
    assert body["suspended_at"] is not None, "since when, so the UI can say it"


def test_the_staff_note_is_never_shown_to_the_customer(api):
    """"Chasing payment, third email" is for the next engineer, not the customer."""
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    deactivate(api, staff, tid, reason="chasing payment, third email")

    customer_view = api.get("/api/v1/tenant", headers=head(customer)).json()
    assert "suspension_reason" not in customer_view
    assert "chasing" not in str(customer_view)

    console_view = api.get(f"/api/v1/admin/tenants/{tid}", headers=head(staff)).json()
    assert console_view["suspension_reason"] == "chasing payment, third email"


def test_the_audit_entry_records_the_act_but_not_the_note(api):
    """The trail belongs to the customer, so it must stay safe to show them.

    It is not exposed by any route today. That is exactly why the note has to be
    kept out of it now, rather than the first time somebody adds that screen.
    """
    customer, staff = a_customer_and_staff(api)
    deactivate(api, staff, tenant_id(api), reason="unpaid invoice 4021")

    db = api.session_factory()
    entries = db.scalars(
        select(AuditLog).where(AuditLog.action == "platform.tenant.deactivate")
    ).all()
    assert len(entries) == 1
    assert "4021" not in (entries[0].detail or "")
    assert "platform staff" in (entries[0].detail or "")
    db.close()


def test_the_console_shows_which_accounts_are_stopped(api):
    customer, staff = a_customer_and_staff(api)
    deactivate(api, staff, tenant_id(api))

    rows = api.get("/api/v1/admin/tenants", headers=head(staff)).json()
    acme = next(r for r in rows if r["slug"] == "acme")
    assert acme["syncable"] is False
    assert acme["status"] == "suspended"


# ===========================================================================
# Edges
# ===========================================================================
def test_correcting_the_reason_does_not_move_the_date(api):
    """Re-running it to fix a typo must not lose when the account really stopped."""
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    first = deactivate(api, staff, tid, reason="unpaid").json()
    again = deactivate(api, staff, tid, reason="unpaid invoice 4021").json()

    assert again["suspended_at"] == first["suspended_at"]
    assert again["suspension_reason"] == "unpaid invoice 4021"


def test_a_reason_is_optional_so_the_action_stays_one_click(api):
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    response = api.post(f"/api/v1/admin/tenants/{tid}/deactivate",
                        json={}, headers=head(staff))
    assert response.status_code == 200, response.text
    assert response.json()["suspension_reason"] is None


def test_a_cancelled_account_is_not_quietly_turned_into_suspended(api):
    """Cancelled is the stronger statement and already does not sync."""
    customer, staff = a_customer_and_staff(api)
    tid = tenant_id(api)
    api.patch(f"/api/v1/admin/tenants/{tid}/config",
              json={"status": "cancelled"}, headers=head(staff))

    response = deactivate(api, staff, tid)
    assert response.status_code == 400
    assert "cancelled" in response.json()["detail"]
    assert row(api).status == TenantStatus.cancelled.value


def test_an_unknown_tenant_is_a_404_at_both_doors(api):
    _, staff = a_customer_and_staff(api)
    assert deactivate(api, staff, "nope").status_code == 404
    assert activate(api, staff, "nope").status_code == 404


def test_only_the_console_can_work_the_gate(api):
    """An ordinary customer must not be able to stop a competitor syncing."""
    customer = signup(api, "Acme", CUSTOMER)
    victim = signup(api, "Globex", "owner@globex.example.com")
    tid = tenant_id(api, "globex")

    assert api.post(f"/api/v1/admin/tenants/{tid}/deactivate",
                    json={"reason": "because"}, headers=head(customer)).status_code == 403
    assert api.post(f"/api/v1/admin/tenants/{tid}/activate",
                    headers=head(customer)).status_code == 403
    assert row(api, "globex").syncable is True


def test_a_customer_session_cannot_work_the_gate_even_for_staff(api):
    """The two changes compose: the gate is behind the console door.

    A support engineer signed in to their own workspace holds a customer
    session, and this is a console action.
    """
    both = signup(api, "Acme", CUSTOMER)
    promote(api, CUSTOMER)
    tid = tenant_id(api)

    assert api.post(f"/api/v1/admin/tenants/{tid}/deactivate",
                    json={}, headers=head(both)).status_code == 403
