"""The contract every attendance-device integration implements.

This module is the seam. Everything above it — sync engine, provisioning,
reports — speaks in the normalised types defined here; everything below it is
vendor-specific and swappable. A tenant can hold several sources at once, of
different vendors, and the pipeline neither knows nor cares which is which.

Two rules keep that honest:

1. **Normalise at the edge.** A provider converts the vendor's payload into a
   ``PunchEvent`` before returning it. No vendor dict escapes upward, so no
   vendor quirk can quietly become load-bearing in the engine.
2. **Declare, don't assume.** Vendors differ in what they can do — a file drop
   cannot create an employee, a cheap terminal cannot list its siblings. A
   provider declares ``capabilities``; callers check before asking. Anything
   undeclared is unavailable, never a runtime surprise.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class ProviderError(RuntimeError):
    """A provider could not talk to its platform.

    Every integration raises this instead of leaking httpx, XML-RPC or csv
    errors upward, so the engine has exactly one thing to catch.
    """


class UnsupportedCapability(ProviderError):
    """Asked a provider to do something it never claimed it could do."""


class Capability(str, enum.Enum):
    READ_PUNCHES = "read_punches"          # the only universal one
    READ_EMPLOYEES = "read_employees"
    WRITE_EMPLOYEES = "write_employees"
    DISABLE_EMPLOYEES = "disable_employees"
    LIST_TERMINALS = "list_terminals"


# --------------------------------------------------------------------------- #
# Normalised vocabulary
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class PunchEvent:
    """One clock event, vendor-neutral.

    ``punch_time_local`` is deliberately naive: devices report wall-clock in
    their own zone and almost never say which. The source's configured timezone
    converts it one layer up, where that setting lives.
    """

    external_id: str
    emp_code: str
    punch_time_local: datetime
    direction: bool | None = None      # True in, False out, None unknown
    terminal_sn: str | None = None
    terminal_alias: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    department: str | None = None
    verify_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()


@dataclass(slots=True)
class EmployeeRecord:
    external_id: str | None
    emp_code: str
    first_name: str = ""
    last_name: str = ""
    department: str | None = None
    is_active: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()


@dataclass(slots=True)
class TerminalRecord:
    serial_number: str
    alias: str | None = None
    area: str | None = None
    ip_address: str | None = None
    model: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConnectionInfo:
    """Result of a connectivity check, for the UI's Test Connection button."""

    ok: bool
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SourceConfig:
    """Everything a provider needs to reach one customer's platform.

    ``options`` carries the vendor-specific remainder — an API path prefix, a
    field mapping, an SFTP directory. Keeping it free-form JSON is what lets a
    new integration ship without a database migration.
    """

    base_url: str = ""
    username: str = ""
    password: str = ""
    token: str | None = None
    verify_ssl: bool = True
    timezone: str = "UTC"
    options: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# The interface
# --------------------------------------------------------------------------- #
class AttendanceProvider(ABC):
    """Base class for every device-platform integration."""

    #: Stable identifier stored on the source row — never rename one in place.
    slug: str = ""
    label: str = ""
    description: str = ""
    capabilities: frozenset[Capability] = frozenset({Capability.READ_PUNCHES})

    #: Which connection_kind(s) this integration makes sense under (see
    #: app.models.connection.DeviceSource.connection_kind). A shared server
    #: like BioTime works framed either way, so it defaults to both; a
    #: standalone-terminal protocol (one connection == one physical device)
    #: only ever makes sense under "device", and declares that explicitly so
    #: the connection picker does not offer it while in "platform" mode.
    kinds: frozenset[str] = frozenset({"platform", "device"})

    #: Fields the setup form renders, so the UI hardcodes no vendor.
    config_fields: tuple[dict[str, Any], ...] = ()

    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Release sockets and handles. Safe to call more than once."""

    def __enter__(self) -> "AttendanceProvider":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- required ----------------------------------------------------------
    @abstractmethod
    def test_connection(self) -> ConnectionInfo:
        """Prove the credentials work, without changing anything."""

    @abstractmethod
    def fetch_punches(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> Iterator[PunchEvent]:
        """Yield punches in the window, oldest first where the platform allows.

        Bounds are naive local time in the source's zone, matching how devices
        report. Yielding rather than returning a list keeps a year of history
        from being held in memory at once.
        """

    # -- optional, gated on capabilities -----------------------------------
    def fetch_employees(self) -> Iterator[EmployeeRecord]:
        raise UnsupportedCapability(f"{self.label} cannot list employees")

    def fetch_terminals(self) -> Iterator[TerminalRecord]:
        raise UnsupportedCapability(f"{self.label} cannot list terminals")

    def create_employee(self, record: EmployeeRecord) -> EmployeeRecord:
        raise UnsupportedCapability(f"{self.label} cannot create employees")

    def set_employee_active(self, external_id: str, active: bool) -> None:
        raise UnsupportedCapability(f"{self.label} cannot enable/disable employees")

    # -- helpers -----------------------------------------------------------
    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise UnsupportedCapability(f"{self.label} does not support {capability.value}")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, type[AttendanceProvider]] = {}


def register(provider_cls: type[AttendanceProvider]) -> type[AttendanceProvider]:
    """Add a provider to the catalogue. Usable as a decorator."""
    slug = provider_cls.slug
    if not slug:
        raise ValueError(f"{provider_cls.__name__} must define a slug")
    existing = _REGISTRY.get(slug)
    if existing is not None and existing is not provider_cls:
        raise ValueError(f"Provider slug {slug!r} is already taken by {existing.__name__}")
    _REGISTRY[slug] = provider_cls
    return provider_cls


def get_provider_class(slug: str) -> type[AttendanceProvider]:
    try:
        return _REGISTRY[slug]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise ProviderError(f"Unknown device platform {slug!r}. Available: {known}") from None


def build_provider(slug: str, config: SourceConfig) -> AttendanceProvider:
    return get_provider_class(slug)(config)


def available_providers() -> list[dict[str, Any]]:
    """Catalogue for the connection picker, so the UI names no vendor itself."""
    return [
        {
            "slug": cls.slug,
            "label": cls.label,
            "description": cls.description,
            "capabilities": sorted(c.value for c in cls.capabilities),
            "kinds": sorted(cls.kinds),
            "config_fields": list(cls.config_fields),
        }
        for cls in sorted(_REGISTRY.values(), key=lambda c: c.label)
    ]
