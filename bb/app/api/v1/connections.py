"""Connection CRUD and the Test Connection probes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import Principal, audit, get_principal, require_writer
from app.core.crypto import encrypt
from app.db.session import get_db
from app.integrations.base import (
    Capability,
    ProviderError,
    available_providers,
    get_provider_class,
)
from app.integrations.odoo import OdooError
from app.models import (
    ConnectionStatus,
    Device,
    DeviceSource,
    EmployeeMapping,
    MappingStatus,
    OdooConnection,
)
from app.schemas import (
    DeviceOut,
    DeviceUpdate,
    MessageOut,
    OdooConnectionIn,
    OdooConnectionOut,
    OdooConnectionUpdate,
    ProvisionOut,
    SourceIn,
    SourceOut,
    SourceUpdate,
    TestResult,
)
from app.services.connections import (
    UnsafeTargetError,
    build_odoo_client,
    build_source_provider,
)
from app.services.provisioning import provision_unmapped

log = logging.getLogger(__name__)
router = APIRouter(tags=["connections"])


def _dupe_name_guard(db: Session, name: str, commit: bool = False):
    """Turn a name collision into the same friendly 400 whether it was caught
    by the pre-check (the common case) or not.

    The pre-check queries before writing, but query-then-write is not atomic:
    two requests racing on the same name can both pass it and only collide at
    the database's own unique constraint. Catching that here is the backstop,
    not the primary mechanism — it just means a genuine race gets the same
    clear message instead of a raw 500.
    """
    try:
        db.commit() if commit else db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"A connection named '{name}' already exists."
        ) from None


# ===========================================================================
# Odoo
# ===========================================================================
def _get_odoo(db: Session, principal: Principal, conn_id: str) -> OdooConnection:
    conn = db.get(OdooConnection, conn_id)
    # 404 rather than 403: an id from another tenant must be indistinguishable
    # from one that does not exist.
    if conn is None or conn.tenant_id != principal.tenant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Odoo connection not found")
    return conn


def _probe_odoo(principal: Principal, conn: OdooConnection) -> TestResult:
    try:
        info = build_odoo_client(principal.tenant, conn).ping()
    except (OdooError, UnsafeTargetError) as exc:
        conn.status = ConnectionStatus.failed.value
        conn.status_message = str(exc)[:500]
        conn.last_checked_at = datetime.now(timezone.utc)
        return TestResult(ok=False, message=str(exc))

    conn.status = ConnectionStatus.connected.value
    conn.status_message = None
    conn.uid_cache = info["uid"]
    conn.odoo_version = str(info.get("server_version") or "")
    conn.has_companion_addon = bool(info.get("has_companion_addon"))
    conn.has_device_tracking = bool(info.get("has_device_tracking"))
    conn.device_tracking_mode = info.get("device_tracking_mode")
    conn.last_checked_at = datetime.now(timezone.utc)
    # company_name is a display cache, refreshed here and only here — never
    # trust a name the client sent, and never leave a stale one paired with
    # whatever company_id ends up being true after this probe.
    companies = info.get("companies") or []
    conn.company_name = next(
        (c["name"] for c in companies if c["id"] == conn.company_id), None
    ) if conn.company_id is not None else None

    if not info.get("can_create_attendance"):
        # Connected but useless: worth failing the test loudly, because the
        # alternative is a green tick followed by every push erroring.
        return TestResult(
            ok=False,
            message=(
                "Connected, but this Odoo user cannot create attendance records. "
                "Add them to 'Employees / Administrator' or HR Officer."
            ),
            detail=info,
        )
    return TestResult(
        ok=True,
        message=(
            f"Connected to Odoo {info.get('server_version')} — "
            f"{info['employee_count']} employee(s) visible"
        ),
        detail=info,
    )


@router.get("/odoo-connections", response_model=list[OdooConnectionOut])
def list_odoo(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[OdooConnection]:
    return list(
        db.scalars(
            select(OdooConnection).where(OdooConnection.tenant_id == principal.tenant.id)
        ).all()
    )


@router.post(
    "/odoo-connections", response_model=OdooConnectionOut, status_code=status.HTTP_201_CREATED
)
def create_odoo(
    payload: OdooConnectionIn,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> OdooConnection:
    if db.scalar(
        select(OdooConnection).where(
            OdooConnection.tenant_id == principal.tenant.id,
            OdooConnection.name == payload.name,
        )
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"A connection named '{payload.name}' already exists."
        )
    conn = OdooConnection(
        tenant_id=principal.tenant.id,
        name=payload.name,
        url=payload.url,
        db_name=payload.db_name,
        username=payload.username,
        api_key_enc=encrypt(payload.api_key, principal.tenant.crypto_key),
        company_id=payload.company_id,
    )
    db.add(conn)
    _dupe_name_guard(db, payload.name)
    _probe_odoo(principal, conn)
    audit(db, principal, "odoo.create", conn.id, payload.url, request)
    db.commit()
    db.refresh(conn)
    return conn


@router.patch("/odoo-connections/{conn_id}", response_model=OdooConnectionOut)
def update_odoo(
    conn_id: str,
    payload: OdooConnectionUpdate,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> OdooConnection:
    conn = _get_odoo(db, principal, conn_id)
    data = payload.model_dump(exclude_unset=True)
    api_key = data.pop("api_key", None)
    for key, value in data.items():
        setattr(conn, key, value)
    if api_key:
        conn.api_key_enc = encrypt(api_key, principal.tenant.crypto_key)
        conn.uid_cache = None  # a new key means a new session
    if "company_id" in data:
        conn.company_name = None  # stale until the next Test Connection refreshes it
    conn.status = ConnectionStatus.unverified.value
    audit(db, principal, "odoo.update", conn.id, ",".join(data), request)
    _dupe_name_guard(db, data.get("name", conn.name), commit=True)
    db.refresh(conn)
    return conn


@router.post("/odoo-connections/{conn_id}/test", response_model=TestResult)
def test_odoo(
    conn_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> TestResult:
    conn = _get_odoo(db, principal, conn_id)
    result = _probe_odoo(principal, conn)
    db.commit()
    return result


@router.post("/odoo-connections/{conn_id}/device-tracking/bootstrap", response_model=TestResult)
def bootstrap_device_tracking(
    conn_id: str,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> TestResult:
    """Turn on device tracking without installing an Odoo add-on.

    See ``OdooClient.ensure_device_tracking_bootstrap`` — it creates a
    custom model and fields purely through the external API (the same
    mechanism Odoo Studio's own UI uses), which is the only device-tracking
    path that reaches Odoo Online, since a real module never will.

    Kept as its own explicit action rather than something the ordinary
    Test Connection probe does on its own initiative: unlike a probe, this
    writes new fields into the customer's own Odoo schema, and a button
    named "test" should never have that side effect.
    """
    conn = _get_odoo(db, principal, conn_id)
    try:
        build_odoo_client(principal.tenant, conn).ensure_device_tracking_bootstrap()
    except (OdooError, UnsafeTargetError) as exc:
        return TestResult(ok=False, message=str(exc))

    result = _probe_odoo(principal, conn)
    audit(
        db, principal, "odoo.device_tracking_bootstrap", conn.id,
        conn.device_tracking_mode or "", request,
    )
    db.commit()
    return result


@router.delete(
    "/odoo-connections/{conn_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
def delete_odoo(
    conn_id: str,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> None:
    conn = _get_odoo(db, principal, conn_id)
    audit(db, principal, "odoo.delete", conn.id, conn.name, request)
    db.delete(conn)
    db.commit()


# ===========================================================================
# Device sources
# ===========================================================================
@router.get("/providers")
def list_providers(_: Principal = Depends(get_principal)) -> list[dict]:
    """The integrations this deployment can talk to.

    The setup form renders itself from this, so adding a provider needs no
    frontend change, and the UI can grey out what a platform cannot do rather
    than offering a button that will fail.
    """
    return available_providers()


_MODE_LABEL = {"platform": "platform server", "device": "individual device"}


def _enforce_biometric_mode(db: Session, principal: Principal, kind: str) -> None:
    """A tenant adds one kind of biometric connection at a time.

    ``Tenant.biometric_mode`` is the record of that choice. Unset means
    undecided — the frontend asks before offering either "+ Add" button, but
    a direct API call gets the same gate rather than a free pass: the first
    call adopts ``kind`` (or, if this tenant already has a connection from
    before this field existed, that connection's kind) as the mode, and every
    call after either matches it or is refused.
    """
    tenant = principal.tenant
    if tenant.biometric_mode is None:
        inferred = db.scalar(
            select(DeviceSource.connection_kind)
            .where(DeviceSource.tenant_id == tenant.id)
            .order_by(DeviceSource.created_at)
            .limit(1)
        )
        tenant.biometric_mode = inferred or kind
    if kind != tenant.biometric_mode:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"This account is set up for {_MODE_LABEL[tenant.biometric_mode]} connections. "
            f"Switch modes in Settings → Biometric before adding a {_MODE_LABEL[kind]} connection.",
        )


def _require_provider_fields(provider_cls, payload: SourceIn) -> None:
    """A provider's own ``config_fields`` says what it actually needs.

    This is what lets a provider with a different shape than BioTime's
    (a standalone device has no username, for instance) skip fields BioTime
    requires without a schema change here for every new integration —
    the provider declares its own requirements and this just enforces them.
    """
    missing = [
        f["label"]
        for f in provider_cls.config_fields
        if f.get("required") and not str(getattr(payload, f["name"], "") or "").strip()
    ]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{', '.join(missing)} required for {provider_cls.label}.",
        )


def _get_source(db: Session, principal: Principal, source_id: str) -> DeviceSource:
    source = db.get(DeviceSource, source_id)
    if source is None or source.tenant_id != principal.tenant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device source not found")
    return source


def _probe_source(principal: Principal, source: DeviceSource) -> TestResult:
    """Each provider reports success in its own terms, so the message is theirs."""
    provider = None
    try:
        provider = build_source_provider(principal.tenant, source)
        result = provider.test_connection()
        cached = getattr(provider, "cached_token", None)
        if cached:
            source.token_enc = encrypt(cached, principal.tenant.crypto_key)
    except (ProviderError, UnsafeTargetError) as exc:
        source.status = ConnectionStatus.failed.value
        source.status_message = str(exc)[:500]
        source.last_checked_at = datetime.now(timezone.utc)
        return TestResult(ok=False, message=str(exc))
    finally:
        if provider is not None:
            provider.close()

    source.last_checked_at = datetime.now(timezone.utc)
    source.status = (
        ConnectionStatus.connected.value if result.ok else ConnectionStatus.failed.value
    )
    source.status_message = None if result.ok else result.message[:500]
    return TestResult(ok=result.ok, message=result.message, detail=result.detail)


@router.get("/sources", response_model=list[SourceOut])
def list_sources(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[DeviceSource]:
    return list(
        db.scalars(
            select(DeviceSource).where(DeviceSource.tenant_id == principal.tenant.id)
        ).all()
    )


@router.post("/sources", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
def create_source(
    payload: SourceIn,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> DeviceSource:
    # Reject an unknown provider now rather than storing a row that can never be
    # built — a source that fails only at sync time is far harder to diagnose.
    try:
        provider_cls = get_provider_class(payload.provider)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if payload.connection_kind not in provider_cls.kinds:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{provider_cls.label} is not offered as a {_MODE_LABEL[payload.connection_kind]} "
            "connection.",
        )
    _require_provider_fields(provider_cls, payload)

    if payload.auto_provision_employees and not (
        Capability.READ_EMPLOYEES in provider_cls.capabilities
        and Capability.WRITE_EMPLOYEES in provider_cls.capabilities
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{provider_cls.label} cannot create employees, so it cannot be "
            "provisioned from Odoo's roster.",
        )

    _enforce_biometric_mode(db, principal, payload.connection_kind)

    if db.scalar(
        select(DeviceSource).where(
            DeviceSource.tenant_id == principal.tenant.id,
            DeviceSource.name == payload.name,
        )
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"A connection named '{payload.name}' already exists."
        )

    source = DeviceSource(
        tenant_id=principal.tenant.id,
        name=payload.name,
        provider=payload.provider,
        connection_kind=payload.connection_kind,
        config=payload.config or {},
        base_url=payload.base_url,
        username=payload.username,
        password_enc=encrypt(payload.password, principal.tenant.crypto_key),
        auth_type=payload.auth_type,
        server_timezone=payload.server_timezone,
        verify_ssl=payload.verify_ssl,
        auto_provision_employees=payload.auto_provision_employees,
    )
    db.add(source)
    _dupe_name_guard(db, payload.name)
    _probe_source(principal, source)
    audit(db, principal, "source.create", source.id, payload.base_url, request)
    db.commit()
    db.refresh(source)
    return source


@router.patch("/sources/{source_id}", response_model=SourceOut)
def update_source(
    source_id: str,
    payload: SourceUpdate,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> DeviceSource:
    source = _get_source(db, principal, source_id)
    data = payload.model_dump(exclude_unset=True)
    password = data.pop("password", None)
    if data.get("auto_provision_employees"):
        try:
            provider_cls = get_provider_class(source.provider)
        except ProviderError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if not (
            Capability.READ_EMPLOYEES in provider_cls.capabilities
            and Capability.WRITE_EMPLOYEES in provider_cls.capabilities
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{provider_cls.label} cannot create employees, so it cannot be "
                "provisioned from Odoo's roster.",
            )
    if "name" in data and data["name"] != source.name:
        clash = db.scalar(
            select(DeviceSource).where(
                DeviceSource.tenant_id == principal.tenant.id,
                DeviceSource.name == data["name"],
                DeviceSource.id != source.id,
            )
        )
        if clash:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"A connection named '{data['name']}' already exists.",
            )
    for key, value in data.items():
        setattr(source, key, value)
    if password:
        source.password_enc = encrypt(password, principal.tenant.crypto_key)
        source.token_enc = None  # the cached token was minted with the old one
    source.status = ConnectionStatus.unverified.value
    audit(db, principal, "source.update", source.id, ",".join(data), request)
    _dupe_name_guard(db, data.get("name", source.name), commit=True)
    db.refresh(source)
    return source


@router.post("/sources/{source_id}/test", response_model=TestResult)
def test_source(
    source_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> TestResult:
    source = _get_source(db, principal, source_id)
    result = _probe_source(principal, source)
    db.commit()
    return result


@router.delete(
    "/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
def delete_source(
    source_id: str,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> None:
    source = _get_source(db, principal, source_id)
    audit(db, principal, "source.delete", source.id, source.name, request)
    db.delete(source)
    db.commit()


def _push_devices_to_odoo(
    db: Session, principal: Principal, devices: list[Device]
) -> str | None:
    """Register every discovered terminal in Odoo's device model right away,
    rather than waiting for it to earn one the slow way.

    Without this, a terminal only gets an Odoo device record the first time
    one of *its* punches makes it into a closed, pushed attendance record
    (see ``SyncEngine._odoo_device_id``) — and a ``PunchRecord``'s device
    link is decided once, at ingest time, from whatever local ``Device``
    rows existed *then*. A terminal imported here after its punches had
    already been ingested (or already pushed to Odoo, in which case that
    attendance is never revisited) keeps a permanently null device link on
    those old rows, so it could sit with punches flowing into Odoo and no
    device record to show for it, indefinitely. Pushing straight from the
    terminal's serial number here has no such dependency on punch history.

    Never raises, and never stops the terminals from being imported into
    BioBridge itself — that part always succeeds regardless of Odoo
    reachability. But a caller that swallows failures entirely leaves the
    customer with no way to tell "nothing needed pushing" apart from
    "something is silently broken", so this returns a short human-readable
    problem description on any failure (Odoo unreachable, or one or more
    individual devices rejected) and ``None`` when there was nothing to
    report — including the ordinary case of device tracking being off.
    """
    if not devices:
        return None
    conn = db.scalars(
        select(OdooConnection)
        .where(
            OdooConnection.tenant_id == principal.tenant.id,
            OdooConnection.is_active.is_(True),
        )
        .limit(1)
    ).first()
    if conn is None:
        return None
    if not conn.has_device_tracking:
        return None

    try:
        odoo = build_odoo_client(principal.tenant, conn)
        odoo.authenticate()
    except (OdooError, UnsafeTargetError) as exc:
        message = f"Could not reach Odoo to register discovered devices: {exc}"
        log.warning(message)
        return message

    failures: list[str] = []
    for device in devices:
        try:
            odoo.upsert_device(
                device.serial_number,
                name=device.alias,
                location=device.area,
                terminal_model=device.model,
                ip_address=device.ip_address,
            )
        except OdooError as exc:
            log.warning(
                "Could not register device %s in Odoo: %s", device.serial_number, exc
            )
            failures.append(f"{device.serial_number}: {exc}")

    if not failures:
        return None
    if len(failures) == len(devices):
        return "Could not register any device in Odoo — " + "; ".join(failures)
    return "Could not register some devices in Odoo — " + "; ".join(failures)


@router.post("/sources/{source_id}/discover-devices", response_model=list[DeviceOut])
def discover_devices(
    source_id: str,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> list[Device]:
    source = _get_source(db, principal, source_id)
    # Scoped to this source, not the whole tenant — a serial number is only
    # unique within its own vendor/account namespace, and two sources (e.g.
    # two BioTime accounts, one per company) can each legitimately report a
    # terminal with the same serial. Matches the (tenant_id, source_id,
    # serial_number) constraint on Device and the lookup sync_engine.py
    # already uses for the same reason.
    existing = {
        d.serial_number: d
        for d in db.scalars(
            select(Device).where(Device.source_id == source.id)
        ).all()
    }

    provider = build_source_provider(principal.tenant, source)
    if not provider.supports(Capability.LIST_TERMINALS):
        provider.close()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{provider.label} does not publish its device inventory. Devices "
            "will appear on their own as punches arrive carrying a serial number.",
        )
    try:
        terminals = list(provider.fetch_terminals())
    except (ProviderError, UnsafeTargetError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    finally:
        provider.close()

    added = 0
    touched: list[Device] = []
    seen_serials: set[str] = set()
    for terminal in terminals:
        serial = (terminal.serial_number or "").strip()
        if not serial:
            continue
        seen_serials.add(serial)
        device = existing.get(serial)
        if device is None:
            device = Device(
                tenant_id=principal.tenant.id, source_id=source.id, serial_number=serial
            )
            db.add(device)
            existing[serial] = device
            added += 1
        device.alias = terminal.alias or device.alias
        device.area = terminal.area or device.area
        device.ip_address = terminal.ip_address or device.ip_address
        device.model = terminal.model or device.model
        # Reported again — whatever absence was tracked before no longer holds.
        device.missing_since = None
        touched.append(device)

    # A device this source used to report and now does not: flagged, not
    # deleted or disabled, so its alias/punch history/pairing override survive
    # a terminal that is only briefly offline or mid-relocation. The
    # timestamp is set once and left alone on repeat imports that still don't
    # see it, so it reflects when it first went missing, not the most recent
    # check.
    now = datetime.now(timezone.utc)
    missing_now = 0
    for serial, device in existing.items():
        if serial in seen_serials:
            continue
        if device.missing_since is None:
            device.missing_since = now
            missing_now += 1

    audit(
        db,
        principal,
        "device.discover",
        source.id,
        f"{added} new, {missing_now} newly missing",
        request,
    )
    # Every terminal this call found, not just newly-added ones — a terminal
    # imported in an earlier call may still have no Odoo device record (see
    # _push_devices_to_odoo), and re-running Import terminals is the natural
    # way a customer retries that.
    push_problem = _push_devices_to_odoo(db, principal, touched)
    # Surface a real problem the same way the sync engine already does —
    # the "Last error" banner on the source card (settings.js:sourceCard) —
    # so a failure here isn't invisible just because it happened outside a
    # sync run. A clean push, or nothing to push, clears any stale message
    # from an earlier attempt rather than leaving it stuck.
    source.status_message = push_problem[:500] if push_problem else None
    db.commit()
    return list(
        db.scalars(
            select(Device)
            .where(Device.source_id == source.id)
            .order_by(Device.serial_number)
        ).all()
    )


@router.post("/sources/{source_id}/provision-employees", response_model=ProvisionOut)
def provision_employees(
    source_id: str,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict:
    """Create Odoo employees who aren't mapped to a device user yet, and have
    a Badge ID or PIN, on this source's device — see
    app.services.provisioning for the exact rule.

    The settings page calls this right after "Import terminals" succeeds, for
    any provider that can create employees; it's its own endpoint so the
    import itself (terminals into BioBridge) never fails or slows down over
    a problem on the Odoo or employee side, and so the result can be reported
    back on its own.
    """
    source = _get_source(db, principal, source_id)
    try:
        provider_cls = get_provider_class(source.provider)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not (
        Capability.READ_EMPLOYEES in provider_cls.capabilities
        and Capability.WRITE_EMPLOYEES in provider_cls.capabilities
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{provider_cls.label} cannot create employees on the device.",
        )

    conn = db.scalars(
        select(OdooConnection)
        .where(
            OdooConnection.tenant_id == principal.tenant.id,
            OdooConnection.is_active.is_(True),
        )
        .limit(1)
    ).first()
    if conn is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Connect Odoo first — employees are created on the device from Odoo's list.",
        )
    try:
        odoo = build_odoo_client(principal.tenant, conn)
        odoo.authenticate()
        roster = odoo.list_employees()
    except (OdooError, UnsafeTargetError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not read employees from Odoo: {exc}"
        ) from exc

    mapped_ids = set(
        db.scalars(
            select(EmployeeMapping.odoo_employee_id).where(
                EmployeeMapping.tenant_id == principal.tenant.id,
                EmployeeMapping.status == MappingStatus.mapped.value,
                EmployeeMapping.odoo_employee_id.is_not(None),
            )
        ).all()
    )

    provider = None
    try:
        provider = build_source_provider(principal.tenant, source)
        result = provision_unmapped(provider, roster, mapped_ids)
    except (ProviderError, UnsafeTargetError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not read the device's user list: {exc}"
        ) from exc
    finally:
        if provider is not None:
            provider.close()

    audit(
        db, principal, "employee.provision", source.id,
        f"{len(result.created)} created, {len(result.failed)} failed", request,
    )
    db.commit()
    return {
        "created": result.created,
        "failed": result.failed,
        "already_on_device": result.already_on_device,
        "already_mapped": result.already_mapped,
        "no_badge_or_pin": result.no_badge_or_pin,
    }


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[Device]:
    return list(
        db.scalars(
            select(Device)
            .where(Device.tenant_id == principal.tenant.id)
            .order_by(Device.serial_number)
        ).all()
    )


@router.patch("/devices/{device_id}", response_model=DeviceOut)
def update_device(
    device_id: str,
    payload: DeviceUpdate,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> Device:
    device = db.get(Device, device_id)
    if device is None or device.tenant_id != principal.tenant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(device, key, value)
    db.commit()
    db.refresh(device)
    return device
