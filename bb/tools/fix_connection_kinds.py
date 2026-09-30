#!/usr/bin/env python3
"""Re-file connections saved under a kind their provider no longer offers.

Early on, "Standalone device" was only a label: a BioTime server could be
added under it. Every provider now declares the kinds it makes sense under
(``AttendanceProvider.kinds``) and BioTime is platform-only, so a BioTime
connection saved as a device is moved to "platform" — the kind its provider
does offer. It keeps working either way; this only makes it show, and
behave, as the server it is (no device location, no terminal registered
from the connection test).

    python3 tools/fix_connection_kinds.py            # report
    python3 tools/fix_connection_kinds.py --apply    # fix

Only a provider with exactly one kind is fixed automatically; anything else
is reported for a person to decide. Safe to run more than once.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.integrations.base import ProviderError, get_provider_class  # noqa: E402
import app.integrations.providers  # noqa: E402,F401 — registers every provider
import app.models  # noqa: E402,F401
from app.models import DeviceSource  # noqa: E402


def misfiled(db) -> list[tuple[DeviceSource, str | None]]:
    """(connection, the kind to move it to — or None if a person must decide)."""
    out = []
    for source in db.scalars(select(DeviceSource)):
        try:
            kinds = get_provider_class(source.provider).kinds
        except ProviderError:
            continue  # an unknown provider is another problem, not this one
        if source.connection_kind not in kinds:
            out.append((source, next(iter(kinds)) if len(kinds) == 1 else None))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually make the changes.")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        found = misfiled(db)
        if not found:
            print("Every connection is saved under a kind its provider offers — nothing to fix.")
            return 0
        for source, target in found:
            action = f"-> {target}" if target else "NEEDS ATTENTION (provider offers several kinds)"
            print(f"  {source.tenant_id}  {source.name!r:30} {source.provider:12} "
                  f"{source.connection_kind} {action}")
        if not args.apply:
            print("\nReport only. Re-run with --apply to make the changes.")
            return 0
        fixed = 0
        for source, target in found:
            if target:
                source.connection_kind = target
                fixed += 1
        db.commit()
        print(f"\nre-filed {fixed} connection(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
