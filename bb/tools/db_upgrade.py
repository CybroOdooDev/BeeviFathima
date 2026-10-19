#!/usr/bin/env python3
"""Bring the database to the latest schema. Run it at every deploy.

    python3 tools/db_upgrade.py            # apply anything pending
    python3 tools/db_upgrade.py --check    # report only; exit 1 if behind

Safe to run any number of times, and safe if two deploys start together (it
takes a database lock on PostgreSQL). An existing database that Alembic has
never seen is brought level and adopted the first time.

Making a schema change:
    1. edit the model
    2. alembic revision --autogenerate -m "what changed"
    3. read the file it wrote — autogenerate is a draft, not an authority
    4. python3 tools/db_upgrade.py
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.db import migrations  # noqa: E402
from app.db.session import engine  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Only report; exit 1 if the database is behind.")
    args = parser.parse_args()

    print(f"database: {settings.database_url.split('@')[-1]}")
    head = migrations.head_revision()
    if args.check:
        now = migrations.current_revision(engine)
        if migrations.is_legacy(engine):
            print("Not under Alembic yet. Run without --check to adopt it.")
            return 1
        print(f"at {now or 'nothing'}, latest is {head}")
        return 0 if now == head else 1
    revision = migrations.upgrade(engine)
    print(f"database is at {revision} (latest: {head})")
    return 0 if revision == head else 1


if __name__ == "__main__":
    sys.exit(main())
