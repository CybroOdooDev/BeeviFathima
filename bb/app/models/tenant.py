"""Tenant, users and sessions."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamped, UUIDPk
from app.models.subscription import SubscriptionPlan


class TenantStatus(str, enum.Enum):
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    suspended = "suspended"
    cancelled = "cancelled"


#: The statuses allowed to sync. A suspended, past-due or cancelled tenant
#: keeps its data and its screens, and stops costing anybody polling traffic.
#:
#: Defined here rather than in the scheduler because it is a fact about the
#: status itself, and both the scheduler and the API need it — including
#: ``Tenant.syncable`` below, which the models cannot get from a service
#: without importing in a circle. ``app.services.scheduling`` re-exports it as
#: SYNCABLE, which is the name the rest of the code already uses.
SYNCABLE_STATUSES: frozenset[str] = frozenset(
    {TenantStatus.trialing.value, TenantStatus.active.value}
)


class UserRole(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    viewer = "viewer"


class Tenant(Base, UUIDPk, Timestamped):
    __tablename__ = "tenant"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default=TenantStatus.trialing.value)

    #: When the platform stopped this account syncing, and why.
    #:
    #: ``status`` alone cannot answer what support is actually asked — "since
    #: when, and on what grounds" — and the audit trail records the transition
    #: rather than the reason behind it.
    #:
    #: The reason is a **staff note and stays one**: it is never returned to the
    #: customer, who is shown a fixed line instead. "Chasing payment, third
    #: email" is a useful thing for the next engineer to read and not something
    #: to render in the customer's dashboard.
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspension_reason: Mapped[str | None] = mapped_column(String(200))

    #: The tenant's own timezone, used to render reports. Not the BioTime
    #: server's timezone -- that lives on the source, because a tenant can have
    #: sites in different zones.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- pairing policy, tenant-wide defaults --------------------------------
    pairing_mode: Mapped[str] = mapped_column(String(20), default="alternating")
    day_boundary_hour: Mapped[int] = mapped_column(Integer, default=4)
    min_punch_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    max_shift_hours: Mapped[int] = mapped_column(Integer, default=16)
    orphan_out_policy: Mapped[str] = mapped_column(String(20), default="flag")
    auto_create_employees: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- working hours, for late-arrival scoring -----------------------------
    work_start_time: Mapped[str] = mapped_column(String(5), default="09:00")
    late_grace_minutes: Mapped[int] = mapped_column(Integer, default=10)

    #: The kind of biometric connection ("platform" or "device") this tenant
    #: added most recently. Informational only now: the kind is picked per
    #: connection in the "+ Add connection" flow and both kinds can coexist.
    #: It used to gate which kind could be added at all.
    biometric_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)

    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    #: The tier this account is sold under, and what it is allowed to do.
    #: Null is a real, supported state — an account with no plan assigned
    #: enforces none of the limits below, which is what every tenant that
    #: existed before subscription plans were added looks like, and what
    #: staff can still choose for an account they would rather manage by
    #: hand. ``SET NULL`` rather than a hard delete-block: retiring a plan a
    #: tenant is still on is a decision for a person, not a database error.
    plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("subscription_plan.id", ondelete="SET NULL"), index=True, nullable=True
    )

    #: A plan switch queued while this account is on a paid (``active``)
    #: subscription — see app.api.v1.sync.update_tenant. Waits for
    #: ``subscription_renews_at`` rather than landing immediately: a switch
    #: mid-period would otherwise let a customer step into (or out of) limits
    #: they have not actually finished paying the current period for.
    #: Promoted to ``plan_id`` by the same sweep that moves the renewal date
    #: (``app.services.scheduling.sweep_subscriptions``), then cleared. Null
    #: the rest of the time — a switch made from ``trialing`` or ``past_due``
    #: still applies immediately and never touches this column at all.
    pending_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("subscription_plan.id", ondelete="SET NULL"), index=True, nullable=True
    )

    #: The date this account's access to syncing lapses without a renewal —
    #: one field doing the job of both "trial ends" and "subscription paid
    #: through", because to the automatic sweep in
    #: ``app.services.scheduling.sweep_subscriptions`` they are the same
    #: question: is this account still current. Null exempts the account
    #: from the sweep entirely rather than lapsing it immediately — the
    #: correct meaning for "not on a metered subscription", and the state
    #: every existing tenant is in the moment this column is added, so
    #: rolling this feature out cannot suspend an account nobody ever set a
    #: date for.
    subscription_renews_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    users: Mapped[list["User"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    #: ``foreign_keys`` is required on both now that two columns reference
    #: subscription_plan — without it SQLAlchemy has no way to tell which FK
    #: each relationship is for and refuses to guess.
    plan: Mapped["SubscriptionPlan | None"] = relationship(foreign_keys=[plan_id])
    pending_plan: Mapped["SubscriptionPlan | None"] = relationship(
        foreign_keys=[pending_plan_id]
    )

    @property
    def syncable(self) -> bool:
        """Whether the platform permits this account to sync at all.

        The subscription gate, in one place. ``sync_enabled`` is the customer's
        own switch and is a separate question: an account can be allowed to
        sync and have chosen not to.
        """
        return self.status in SYNCABLE_STATUSES

    @property
    def crypto_key(self) -> str:
        """Salt for deriving this tenant's credential-encryption key."""
        return self.id

    #: Guarded on ``plan_id`` rather than truthiness-checking ``self.plan``,
    #: so an unassigned tenant — the common case — never pays for a lazy-load
    #: query it already knows will come back empty.
    @property
    def plan_name(self) -> str | None:
        return self.plan.name if self.plan_id else None

    @property
    def plan_max_employees(self) -> int | None:
        return self.plan.max_employees if self.plan_id else None

    @property
    def plan_min_sync_interval_minutes(self) -> int | None:
        return self.plan.min_sync_interval_minutes if self.plan_id else None

    @property
    def pending_plan_name(self) -> str | None:
        return self.pending_plan.name if self.pending_plan_id else None


class User(Base, UUIDPk, Timestamped):
    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)

    #: Null for platform staff, who are not a customer.
    #:
    #: A support engineer is not a tenant. Forcing one on them put a phantom
    #: company in the customer list, counted it in "N accounts scheduled", and
    #: polled a BioTime server that does not exist — while giving the engineer a
    #: meaningless attendance dashboard of their own. Null says the true thing:
    #: this person has no customer workspace, and every tenant-scoped query
    #: correctly finds nothing for them.
    #:
    #: Still allowed to be set: someone who really is both a customer and staff
    #: keeps their account and gains the console on top.
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default=UserRole.owner.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    #: Platform staff: may read and change *other* tenants' scheduling.
    #:
    #: Deliberately not a ``UserRole``. The roles are positions inside one
    #: customer's account and every query filters on that account; this crosses
    #: that line, so making it a fourth role would invite someone to hand it out
    #: from the customer-facing user screen. It is a separate flag with a
    #: separate dependency, and no HTTP route writes it — ``tools/grant_admin.py``
    #: on the server is the only way in, which keeps the blast radius where a
    #: database login already reaches.
    #:
    #: ``server_default`` as well as ``default``: the Python-side default only
    #: applies to rows this ORM inserts, and is never rendered into DDL. Without
    #: it, adding this column to a table that already has users emits
    #: ``BOOLEAN NOT NULL`` with no DEFAULT, and every existing row has no value
    #: to take — the migration fails outright.
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Set once the person has clicked the link sent to this address. Null
    #: means unverified — including for every account created before this
    #: column existed, which is the correct backfilled state: they were never
    #: asked to prove it either. Nothing currently blocks on this being null;
    #: it is tracked so the dashboard can say so and so a later gate has
    #: something to check.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Hash of the one live email-verification token, if any — same shape as
    #: UserSession.token_hash, so a stolen database yields no usable
    #: verification link, same as it yields no usable session. Cleared the
    #: moment the token is used or a fresh one is requested.
    email_verify_token_hash: Mapped[str | None] = mapped_column(String(64))
    email_verify_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant] = relationship(back_populates="users")


class UserSession(Base, UUIDPk, Timestamped):
    """One row per issued refresh token, so a login is visible and revocable.

    The token itself is never stored -- only the sha256 of its ``jti``. A
    database dump therefore yields no usable session.
    """

    __tablename__ = "user_session"
    __table_args__ = (Index("ix_session_user_revoked", "user_id", "revoked_at"),)

    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Mirrors the user's tenant, and is null for a platform staff session.
    tenant_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)

    #: Which door this session was opened at — "tenant" or "staff".
    #:
    #: Not derivable from ``tenant_id``: someone who is both a customer and
    #: staff has a tenant either way, so without this column a console session
    #: and a product session by the same person are indistinguishable in the
    #: table. That matters exactly once — during an incident, when the question
    #: is which live sessions could reach other customers — which is the wrong
    #: moment to discover it was not recorded.
    scope: Mapped[str] = mapped_column(
        String(16), default="tenant", server_default=text("'tenant'"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device_label: Mapped[str | None] = mapped_column(String(120))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(80))

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class AuditLog(Base, UUIDPk, Timestamped):
    __tablename__ = "audit_log"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    actor_user_id: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(120))
    detail: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(64))
