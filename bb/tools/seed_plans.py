#!/usr/bin/env python3
"""Create the starter set of subscription plans, if none exist yet.

Plans are not created through the API on purpose — deciding what tiers to
sell is a business decision, not a customer- or staff-reachable action, and
the console only ever *assigns* one to a tenant (PATCH .../admin/tenants/{id}
/config) or lists what exists (GET /admin/tenants and GET /admin/plans). This
script is the one place new plans get made, or an existing one's numbers
change.

Safe to re-run: each plan below is upserted by name, so editing PLANS and
running this again updates prices and limits in place rather than creating
duplicates. It never deletes a plan — retiring one is done by hand (set
is_active to false on the row, or add that toggle here later), because a
plan a tenant is already on must keep existing regardless of what this
script's defaults currently say.

    python3 tools/seed_plans.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db.session import session_scope  # noqa: E402
from app.models import SubscriptionPlan  # noqa: E402

#: Starting tiers, not a contract — edit freely and re-run. ``None`` for
#: either limit means unlimited / no floor, the same as leaving a tenant's
#: plan unassigned entirely (see SubscriptionPlan for what each limit does).
PLANS = [
    dict(
        name="Starter",
        description="Small teams getting started: one device, hourly-or-slower sync.",
        monthly_price_cents=4900,
        max_employees=25,
        min_sync_interval_minutes=60,
        is_default=True,
    ),
    dict(
        name="Growth",
        description="Growing teams on multiple devices, sync as often as every 15 minutes.",
        monthly_price_cents=14900,
        max_employees=150,
        min_sync_interval_minutes=15,
        is_default=False,
    ),
    dict(
        name="Scale",
        description="Large or multi-site operations: no employee cap, sync as often as every 5 minutes.",
        monthly_price_cents=39900,
        max_employees=None,
        min_sync_interval_minutes=5,
        is_default=False,
    ),
]


def main() -> int:
    with session_scope() as db:
        existing = {p.name: p for p in db.scalars(select(SubscriptionPlan)).all()}
        created, updated = 0, 0

        for spec in PLANS:
            plan = existing.get(spec["name"])
            if plan is None:
                plan = SubscriptionPlan(name=spec["name"])
                db.add(plan)
                created += 1
            else:
                updated += 1
            for key, value in spec.items():
                setattr(plan, key, value)
            plan.is_active = True
        db.flush()

        # The source of truth for "which one is default" is PLANS itself, not
        # whatever was already in the database — so a plan removed from the
        # list above loses the flag here rather than leaving two defaults
        # behind (or the wrong one) after an edit.
        default_names = {spec["name"] for spec in PLANS if spec.get("is_default")}
        for plan in db.scalars(select(SubscriptionPlan)).all():
            plan.is_default = plan.name in default_names

        db.commit()

        print(f"{created} plan(s) created, {updated} updated.\n")
        for plan in db.scalars(select(SubscriptionPlan).order_by(SubscriptionPlan.name)).all():
            tag = " (default)" if plan.is_default else ""
            cap = f"up to {plan.max_employees} employees" if plan.max_employees is not None else "no employee cap"
            floor = f"sync no faster than every {plan.min_sync_interval_minutes} min" \
                if plan.min_sync_interval_minutes else "no sync-speed floor"
            print(f"  {plan.name}{tag}: {cap}, {floor}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
