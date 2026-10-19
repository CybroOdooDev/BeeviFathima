#!/usr/bin/env python3
"""Copy a BioBridge SQLite database into PostgreSQL, once, when moving to Docker.

    python3 tools/sqlite_to_postgres.py --sqlite biobridge.db \\
        --postgres postgresql+psycopg://biobridge:PASS@localhost:5432/biobridge

    # in the compose stack (see deploy/README-docker.md):
    docker compose -f deploy/docker-compose.yml --profile import run --rm import-sqlite

What it does, in order:

1. Brings the SQLite file to the latest schema first (on a temporary copy — the
   file you point at is never written), so an older install copies cleanly.
2. Brings the Postgres database to the latest schema (Alembic, as every deploy
   does) and refuses to go on if any BioBridge table there already has rows —
   unless ``--replace`` is given, which empties them first.
3. Copies every table, parents before children, in batches, in one
   transaction: either everything arrives or nothing does.
4. Moves integer id sequences past the copied rows, so new rows don't collide.

Stored passwords and API keys are copied as the ciphertext they are. They stay
readable only if the Docker stack runs with the **same MASTER_ENCRYPTION_KEY**
as the install the file came from — the tool checks one and says so.

Moving to a server with a new key (production refuses the dev-only one): run
with the new key in MASTER_ENCRYPTION_KEY and pass the old one as
``--old-master-key``; every stored secret is decrypted with the old key and
encrypted again with the new one on the way across.

``--rewrite-localhost`` changes connection addresses that point at this
machine (``127.0.0.1``, ``localhost``) to ``host.docker.internal``: inside a
container, 127.0.0.1 is the container itself, so a mock BioTime or a local
Odoo on the host would otherwise be unreachable. Run those on 0.0.0.0.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import Integer, String, create_engine, func, inspect, select, text  # noqa: E402

BATCH = 1000
LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}
DOCKER_HOST = "host.docker.internal"
#: (table, column) holding an address a sync connects to.
ADDRESS_COLUMNS = (("device_source", "base_url"), ("odoo_connection", "url"))
#: Not carried over: the old process's scheduler lease would only make the new
#: one wait out its TTL before dispatching.
SKIP = {"scheduler_lease"}


def rewrite_local(url: str | None) -> str | None:
    """``http://127.0.0.1:8100`` → ``http://host.docker.internal:8100``."""
    if not url or "://" not in url:
        return url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in LOCAL_HOSTS:
        return url
    netloc = DOCKER_HOST + (f":{parts.port}" if parts.port else "")
    if parts.username:
        netloc = f"{parts.username}{':' + parts.password if parts.password else ''}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class Rekey:
    """Decrypt with the old master key, encrypt with the current one."""

    #: (table, column) -> how to find the per-row key salt.
    TENANT_COLUMNS = {("odoo_connection", "api_key_enc"), ("device_source", "password_enc"),
                      ("device_source", "token_enc")}
    PLATFORM_KEYS = {"mail": "platform:mail", "stripe": "platform:stripe"}

    def __init__(self, old_master: str):
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        from app.core import crypto

        self.crypto = crypto
        self._old: dict[str, Fernet] = {}

        def old_fernet(salt_key: str) -> Fernet:
            if salt_key not in self._old:
                hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=crypto._SALT,
                            info=f"tenant:{salt_key}".encode())
                self._old[salt_key] = Fernet(base64.urlsafe_b64encode(hkdf.derive(old_master.encode())))
            return self._old[salt_key]

        self._old_fernet = old_fernet
        self.count = 0

    def _swap(self, ciphertext: str | None, salt_key: str) -> str | None:
        if not ciphertext:
            return ciphertext
        version, _, payload = ciphertext.partition(":")
        if version != self.crypto.CURRENT_VERSION:
            return ciphertext
        try:
            plain = self._old_fernet(salt_key).decrypt(payload.encode()).decode()
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"--old-master-key cannot read a stored secret ({salt_key}): {exc}")
        self.count += 1
        return self.crypto.encrypt(plain, salt_key)

    def row(self, table: str, row: dict) -> None:
        for (tname, cname) in self.TENANT_COLUMNS:
            if table == tname and row.get(cname):
                row[cname] = self._swap(row[cname], row["tenant_id"])
        if table == "platform_setting" and row.get("key") in self.PLATFORM_KEYS:
            try:
                data = json.loads(row.get("value") or "{}")
            except ValueError:
                return
            salt = self.PLATFORM_KEYS[row["key"]]
            changed = False
            for k, v in list(data.items()):
                if k.endswith("_enc") and isinstance(v, str) and v:
                    data[k] = self._swap(v, salt)
                    changed = True
            if changed:
                row["value"] = json.dumps(data)


def _upgrade(url: str) -> None:
    from app.db import migrations

    engine = create_engine(url)
    try:
        migrations.upgrade(engine)
    finally:
        engine.dispose()


def _row_count(conn, table) -> int:
    return conn.execute(select(func.count()).select_from(table)).scalar_one()


def copy(sqlite_path: str, pg_url: str, *, replace: bool = False, rewrite_localhost: bool = False,
         old_master_key: str | None = None, log=print) -> dict[str, int]:
    import app.models  # noqa: F401 — registers every table
    from app.db.base import Base

    if not os.path.exists(sqlite_path):
        raise SystemExit(f"No SQLite file at {sqlite_path}")
    if not pg_url.startswith("postgresql"):
        raise SystemExit("--postgres must be a postgresql+psycopg://… URL")

    # 1. A private copy of the source, brought to the latest schema.
    work = tempfile.mkdtemp(prefix="bb-import-")
    source_file = os.path.join(work, "source.db")
    shutil.copy2(sqlite_path, source_file)
    source_url = f"sqlite:///{source_file}"
    log(f"source: {sqlite_path} (working on a copy)")
    _upgrade(source_url)

    # 2. The target, at the latest schema and empty.
    log(f"target: {pg_url.split('@')[-1]}")
    _upgrade(pg_url)

    rekey = Rekey(old_master_key) if old_master_key else None
    src = create_engine(source_url)
    dst = create_engine(pg_url)
    tables = [t for t in Base.metadata.sorted_tables]
    src_tables = set(inspect(src).get_table_names())
    copied: dict[str, int] = {}
    try:
        with dst.begin() as out:
            out.execute(text("SET TIME ZONE 'UTC'"))   # naive SQLite times are UTC
            busy = {t.name: _row_count(out, t) for t in tables}
            busy = {k: v for k, v in busy.items() if v}
            if busy and not replace:
                raise SystemExit(
                    "The Postgres database already has data "
                    f"({', '.join(f'{k}: {v}' for k, v in sorted(busy.items()))}). "
                    "Point at an empty database, or pass --replace to empty these tables first.")
            if busy:
                names = ", ".join(f'"{t.name}"' for t in tables)
                out.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
                log(f"emptied {len(busy)} table(s) first (--replace)")

            # Foreign keys are not enforced by SQLite, so an old file can hold a
            # child whose parent was deleted. Copy it as it is rather than
            # stopping halfway: triggers (and so FK checks) are off for this
            # session when the role may do that, and the order is parents-first
            # either way.
            try:
                with out.begin_nested():
                    out.execute(text("SET LOCAL session_replication_role = replica"))
            except Exception:  # noqa: BLE001 — not a superuser: ordered copy only
                log("note: foreign-key checks stay on (the role is not a superuser)")

            with src.connect() as inp:
                for table in tables:
                    if table.name not in src_tables or table.name in SKIP:
                        copied[table.name] = 0
                        continue
                    have = {c["name"] for c in inspect(src).get_columns(table.name)}
                    cols = [c for c in table.columns if c.name in have]
                    limits = {c.name: c.type.length for c in cols
                              if isinstance(c.type, String) and getattr(c.type, "length", None)}
                    cut = 0
                    n = 0
                    result = inp.execution_options(stream_results=True).execute(select(*cols))
                    while True:
                        chunk = result.fetchmany(BATCH)
                        if not chunk:
                            break
                        rows = []
                        for raw in chunk:
                            row = dict(raw._mapping)
                            for name, limit in limits.items():
                                value = row.get(name)
                                if isinstance(value, str) and len(value) > limit:
                                    row[name] = value[:limit]   # SQLite never enforced the length
                                    cut += 1
                            if rewrite_localhost:
                                for tname, cname in ADDRESS_COLUMNS:
                                    if table.name == tname and cname in row:
                                        row[cname] = rewrite_local(row[cname])
                            if rekey is not None:
                                rekey.row(table.name, row)
                            rows.append(row)
                        out.execute(table.insert(), rows)
                        n += len(rows)
                    copied[table.name] = n
                    note = f" ({cut} over-long value(s) shortened)" if cut else ""
                    if n or cut:
                        log(f"  {table.name}: {n}{note}")

            # 4. Sequences past the copied ids.
            for table in tables:
                for col in table.primary_key.columns:
                    if isinstance(col.type, Integer) and col.autoincrement in (True, "auto"):
                        seq = out.execute(text("SELECT pg_get_serial_sequence(:t, :c)"),
                                          {"t": table.name, "c": col.name}).scalar()
                        if seq:
                            out.execute(text(
                                f'SELECT setval(:s, COALESCE((SELECT MAX("{col.name}") FROM "{table.name}"), 0) + 1, false)'),
                                {"s": seq})
    finally:
        src.dispose()
        dst.dispose()
        shutil.rmtree(work, ignore_errors=True)
    if rekey is not None:
        log(f"re-encrypted {rekey.count} stored secret(s) with the new master key")
    return copied


def check_key(pg_url: str, log=print) -> bool | None:
    """Can this process's MASTER_ENCRYPTION_KEY read the copied secrets?"""
    from app.core.crypto import CryptoError, decrypt

    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT tenant_id, api_key_enc FROM odoo_connection WHERE api_key_enc IS NOT NULL LIMIT 1"
            )).first()
    finally:
        engine.dispose()
    if row is None:
        return None
    try:
        decrypt(row[1], row[0])
        log("encryption key: matches — stored Odoo / device passwords are readable")
        return True
    except (CryptoError, Exception):  # noqa: BLE001
        log("WARNING: this MASTER_ENCRYPTION_KEY cannot read the copied passwords. Run the stack "
            "with the key the old install used, or re-enter every connection's password.")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", required=True, help="The SQLite file (e.g. biobridge.db)")
    parser.add_argument("--postgres", default=os.environ.get("DATABASE_URL", ""),
                        help="Target URL (default: $DATABASE_URL)")
    parser.add_argument("--replace", action="store_true",
                        help="Empty BioBridge's tables in the target first")
    parser.add_argument("--rewrite-localhost", action="store_true",
                        help=f"Point connection addresses at {DOCKER_HOST} instead of 127.0.0.1/localhost")
    parser.add_argument("--old-master-key", default=os.environ.get("OLD_MASTER_ENCRYPTION_KEY") or None,
                        help="The MASTER_ENCRYPTION_KEY the SQLite install used, when the new stack "
                             "has a different one (default: $OLD_MASTER_ENCRYPTION_KEY)")
    args = parser.parse_args()
    copied = copy(args.sqlite, args.postgres, replace=args.replace,
                  rewrite_localhost=args.rewrite_localhost, old_master_key=args.old_master_key)
    print(f"copied {sum(copied.values())} row(s) across {sum(1 for v in copied.values() if v)} table(s)")
    check_key(args.postgres)
    return 0


if __name__ == "__main__":
    sys.exit(main())
