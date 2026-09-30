#!/usr/bin/env python3
"""Create the starter set of subscription plans, if they don't exist yet.

Day to day, plans are managed by staff in the console (Platform → Plans,
POST/PATCH /admin/plans). This script only gives a fresh deployment its
first tiers, so by default it creates the plans below that are missing and
leaves every existing plan exactly as staff last set it — re-running it
never undoes a price or limit changed in the console.

    python3 tools/seed_plans.py            # create missing plans
    python3 tools/seed_plans.py --reset    # also overwrite existing ones with PLANS

A ``STRIPE_PRICE_<PLAN NAME>`` env var is applied either way: it is an
explicit instruction, not a default. It never deletes or retires a plan.
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
        max_devices=1,
        min_sync_interval_minutes=60,
        is_default=True,
    ),
    dict(
        name="Growth",
        description="Growing teams on up to 5 devices, sync as often as every 15 minutes.",
        monthly_price_cents=14900,
        max_employees=150,
        max_devices=5,
        min_sync_interval_minutes=15,
        is_default=False,
    ),
    dict(
        name="Scale",
        description="Large or multi-site operations: no employee or device cap, sync as often as every 5 minutes.",
        monthly_price_cents=39900,
        max_employees=None,
        max_devices=None,
        min_sync_interval_minutes=5,
        is_default=False,
    ),
]


def main(argv: list[str] | None = None) -> int:
    reset = "--reset" in (argv if argv is not None else sys.argv[1:])
    with session_scope() as db:
        existing = {p.name: p for p in db.scalars(select(SubscriptionPlan)).all()}
        had_default = any(p.is_default for p in existing.values())
        created, updated = 0, 0

        for spec in PLANS:
            plan = existing.get(spec["name"])
            fresh = plan is None
            if fresh:
                plan = SubscriptionPlan(name=spec["name"])
                db.add(plan)
                created += 1
            elif reset:
                updated += 1
            if fresh or reset:
                for key, value in spec.items():
                    setattr(plan, key, value)
                plan.is_active = True
                if fresh and not reset and had_default:
                    # Staff already chose a default; a new starter plan
                    # doesn't take it over.
                    plan.is_default = False
            # Stripe Price for online billing, when this deployment has one:
            # STRIPE_PRICE_STARTER=price_… (plan name upper-cased, spaces as
            # underscores). Left alone when unset, so re-running this never
            # unlinks a plan that was set up by hand.
            price = os.environ.get(f"STRIPE_PRICE_{spec['name'].upper().replace(' ', '_')}")
            if price:
                plan.stripe_price_id = price.strip()
        db.flush()

        # On --reset, PLANS is the source of truth for "which one is default";
        # otherwise whatever staff chose stands.
        if reset:
            default_names = {spec["name"] for spec in PLANS if spec.get("is_default")}
            for plan in db.scalars(select(SubscriptionPlan)).all():
                plan.is_default = plan.name in default_names

        db.commit()

        print(f"{created} plan(s) created, {updated} reset.\n")
        for plan in db.scalars(select(SubscriptionPlan).order_by(SubscriptionPlan.name)).all():
            tag = " (default)" if plan.is_default else ""
            cap = f"up to {plan.max_employees} employees" if plan.max_employees is not None else "no employee cap"
            floor = f"sync no faster than every {plan.min_sync_interval_minutes} min" \
                if plan.min_sync_interval_minutes else "no sync-speed floor"
            print(f"  {plan.name}{tag}: {cap}, {floor}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
