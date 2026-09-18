"""The scheduler's shared row.

One row, id ``global``, doing two jobs at once.

**A lease.** Whoever holds it is the process allowed to dispatch syncs this
tick. Two API replicas behind a load balancer, or one uvicorn started with
``--workers 4``, all run the same scheduler loop; without a lease each of them
would fire the same tenant's cycle at the same minute. The lease is claimed by a
conditional UPDATE, which is atomic on both SQLite and Postgres and needs no
Redis — the point being that a customer who deploys only the API still gets a
working schedule.

**A heartbeat.** The holder stamps ``last_tick_at`` every tick, so the dashboard
can say whether anything is actually running rather than showing a configured
interval and hoping. Celery beat writes the same row, so the health signal reads
identically whichever scheduler is in use.

The lease is deliberately short-lived: a process that is killed does not release
anything, and the next tick after expiry takes over. That is the whole failover
story — no election, no coordinator.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: The only row this table ever holds. A fixed id turns "claim the lease" into a
#: single-row UPDATE with no lookup and no race over which row to contend for.
LEASE_ID = "global"


class SchedulerLease(Base):
    __tablename__ = "scheduler_lease"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=LEASE_ID)

    #: host:pid of the holder. Purely diagnostic — it answers "which box is
    #: actually running the schedule" when someone deploys three and expects one.
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: "inprocess" or "celery". Shown in the UI, because the two fail in
    #: different ways and the fix differs.
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True)

    #: Naive UTC, like every other time column in this system.
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
