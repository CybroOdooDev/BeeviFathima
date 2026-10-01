"""ZKTeco terminals in ADMS / "Cloud Server" mode — they push to BioBridge.

Unlike every other provider this one never reaches out: the terminal calls
``/iclock/…`` on BioBridge (app/api/adms.py) and its punches are written to
the ledger as they arrive. So this class is the connection's *view* of that
traffic, backed by the ``adms_device`` / ``adms_command`` tables:

* ``test_connection`` — has the terminal with this serial called in lately;
* ``fetch_punches`` — nothing (they are already in the ledger; the sync
  engine claims them, see ``SyncEngine._fetch_and_ingest``);
* ``fetch_terminals`` — the terminal's own serial, model, firmware and IP;
* ``fetch_employees`` / ``create_employee`` — the users it reported, and a
  queued ``DATA UPDATE USERINFO`` command it picks up on its next heartbeat,
  so "provision Odoo employees onto the device" works exactly as for a
  directly-connected terminal.

The connection's address is stored as ``adms://<SERIAL>``. The provider
needs a database session; ``build_source_provider`` hands it the source's
own via ``options["_db"]``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from app.integrations.base import (
    AttendanceProvider,
    Capability,
    ConnectionInfo,
    EmployeeRecord,
    ProviderError,
    PunchEvent,
    SourceConfig,
    TerminalRecord,
    register,
)

SCHEME = "adms://"


def serial_from_address(base_url: str) -> str:
    from app.services.adms import normalise_serial

    value = (base_url or "").strip()
    if value.lower().startswith(SCHEME):
        value = value[len(SCHEME):]
    return normalise_serial(value.strip("/"))


def _ago(when: datetime | None) -> str:
    if when is None:
        return "never"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - when).total_seconds())
    if seconds < 5:
        return "just now"
    if seconds < 90:
        return f"{seconds} seconds ago"
    if seconds < 5400:
        return f"{seconds // 60} minutes ago"
    if seconds < 172800:
        return f"{seconds // 3600} hours ago"
    return f"{seconds // 86400} days ago"


@register
class ZKAdmsProvider(AttendanceProvider):
    slug = "zk_adms"
    label = "Cloud push device (ZKTeco ADMS)"
    description = (
        "A ZKTeco terminal (or eSSL, Realtime, Biomax and other ZKTeco-built "
        "models) that sends its punches to BioBridge itself, through its "
        "Cloud Server setting. Nothing on your network needs opening."
    )
    capabilities = frozenset({
        Capability.READ_PUNCHES,
        Capability.LIST_TERMINALS,
        Capability.READ_EMPLOYEES,
        Capability.WRITE_EMPLOYEES,
    })
    kinds = frozenset({"device"})
    #: Punches arrive by themselves; the engine claims them instead of fetching.
    pushes = True
    config_fields = (
        {"name": "base_url", "label": "Device Serial Number", "type": "text", "required": True,
         "help": "On the device: Menu → System Info → Device Info → Serial Number "
                 "(also on the label on its back)."},
        {"name": "server_timezone", "label": "Device Timezone", "type": "timezone",
         "required": True, "default": "UTC",
         "help": "The zone the device's clock is set to. Punch times arrive with "
                 "no offset, so a wrong value shifts every attendance record."},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self.serial = serial_from_address(config.base_url)
        self.db = config.options.get("_db")
        self.source_id = config.options.get("_source_id")
        self.tenant_id = config.options.get("_tenant_id")

    # -- helpers -------------------------------------------------------------
    def _device(self):
        from app.services import adms

        if self.db is None:
            raise ProviderError("Internal: the push-device connection has no database session.")
        return adms.get_device(self.db, self.serial)

    @staticmethod
    def setup_info() -> dict:
        from app.services.adms import server_address

        host, port = server_address()
        return {"server_address": host, "server_port": port}

    # -- interface -----------------------------------------------------------
    def test_connection(self) -> ConnectionInfo:
        from app.services import adms

        if not adms.valid_serial(self.serial):
            return ConnectionInfo(False, "That doesn't look like a device serial number.")
        host, port = adms.server_address()
        how = (f"On the device open Comm. → Cloud Server Setting, set Server Address to "
               f"{host} and Server Port to {port}, turn off HTTPS/Domain Name if it can't "
               f"be used, save, and test again in a minute.")
        device = self._device()
        if device is None or device.last_seen_at is None:
            return ConnectionInfo(False, f"BioBridge hasn't heard from device {self.serial} yet. {how}")
        if device.tenant_id and self.tenant_id and device.tenant_id != self.tenant_id:
            return ConnectionInfo(False, "This device is already connected to another BioBridge account.")
        if not adms.is_online(device):
            return ConnectionInfo(
                False,
                f"Device {self.serial} last called in {_ago(device.last_seen_at)}. Check it is "
                f"powered on and online. {how}",
                adms.describe(device),
            )
        what = device.model or "ZKTeco terminal"
        return ConnectionInfo(
            True,
            f"Connected: {what} {self.serial} last called in {_ago(device.last_seen_at)}",
            adms.describe(device),
        )

    def fetch_punches(self, since=None, until=None) -> Iterator[PunchEvent]:
        return iter(())

    def fetch_terminals(self) -> Iterator[TerminalRecord]:
        device = self._device()
        if device is None:
            return iter(())
        return iter([TerminalRecord(
            serial_number=device.serial_number,
            alias=None,
            ip_address=device.last_ip,
            model=device.model or (f"ZKTeco ({device.firmware})" if device.firmware else None),
        )])

    def fetch_employees(self) -> Iterator[EmployeeRecord]:
        device = self._device()
        users = (device.users or {}) if device else {}
        for pin, name in users.items():
            first, _, last = (name or "").partition(" ")
            yield EmployeeRecord(external_id=pin, emp_code=pin, first_name=first, last_name=last)

    def create_employee(self, record: EmployeeRecord) -> EmployeeRecord:
        from app.services import adms

        device = self._device()
        if device is None:
            raise ProviderError("The device hasn't called in yet, so users can't be sent to it.")
        pin = (record.emp_code or "").strip()
        if not pin or len(pin) > 24 or "\t" in pin:
            raise ProviderError(f"'{pin}' can't be used as a device user ID.")
        adms.queue_user(self.db, device, pin, record.full_name or pin)
        return EmployeeRecord(external_id=pin, emp_code=pin,
                              first_name=record.first_name, last_name=record.last_name)
