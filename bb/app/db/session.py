"""Engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

_connect_args = {}
_engine_kwargs: dict = {"pool_pre_ping": True, "future": True}

if settings.database_url.startswith("sqlite"):
    # SQLite is the development and test backend; the pool options below are
    # meaningless there and raise if passed.
    #
    # ``timeout`` is sqlite3's own lock wait, not a pool setting — it defaults
    # to 5s, which a request that holds the row open across a live outbound
    # probe (testing an Odoo or biometric connection) can exceed under any
    # concurrent write, turning an ordinary second request into "database is
    # locked" instead of a short wait. Raised rather than left at the
    # default now that a tenant can hold several biometric connections and
    # add or test them close together.
    _connect_args = {"check_same_thread": False, "timeout": 20}
    _engine_kwargs = {"future": True}
else:
    _engine_kwargs.update(pool_size=10, max_overflow=20)

engine = create_engine(settings.database_url, connect_args=_connect_args, **_engine_kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for workers and scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
