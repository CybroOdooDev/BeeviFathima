#!/usr/bin/env python3
"""Create the database schema.

    python3 tools/init_db.py            # create missing tables
    python3 tools/init_db.py --drop     # drop everything first (destructive)

``create_all`` only adds tables that are absent. It does not alter or drop an
existing one, so it cannot migrate a schema that has already diverged — once
this system holds data you care about, changes belong in Alembic, not here.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
import app.models  # noqa: E402,F401 — registers every table on Base.metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drop", action="store_true", help="drop every table first — destroys all data"
    )
    args = parser.parse_args()

    print(f"database: {settings.database_url}")

    if args.drop:
        confirm = input("This deletes every row. Type 'drop' to continue: ")
        if confirm.strip() != "drop":
            print("aborted")
            return 1
        Base.metadata.drop_all(engine)
        print("dropped all tables")

    Base.metadata.create_all(engine)
    print(f"created/verified {len(Base.metadata.tables)} table(s):")
    for name in sorted(Base.metadata.tables):
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
