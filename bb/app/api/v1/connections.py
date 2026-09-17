"""Connection CRUD and the Test Connection probes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
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
from app.models import ConnectionStatus, Device, DeviceSource, OdooConnection
from app.schemas import (
    DeviceOut,
    DeviceUpdate,
    MessageOut,
    OdooConnectionIn,
    OdooConnectionOut,
    OdooConnectionUpdate,
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

log = logging.getLogger(__name__)
router = APIRouter(tags=["connections"])


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
    conn.last_checked_at = datetime.now(timezone.utc)

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
    conn = OdooConnection(
        tenant_id=principal.tenant.id,
        name=payload.name,
        url=payload.url,
        db_name=payload.db_name,
        username=payload.username,
        api_key_enc=encrypt(payload.api_key, principal.tenant.crypto_key),
    )
    db.add(conn)
    db.flush()
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
    conn.status = ConnectionStatus.unverified.value
    audit(db, principal, "odoo.update", conn.id, ",".join(data), request)
    db.commit()
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
        get_provider_class(payload.provider)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    source = DeviceSource(
        tenant_id=principal.tenant.id,
        name=payload.name,
        provider=payload.provider,
        config=payload.config or {},
        base_url=payload.base_url,
        username=payload.username,
        password_enc=encrypt(payload.password, principal.tenant.crypto_key),
        auth_type=payload.auth_type,
        server_timezone=payload.server_timezone,
        verify_ssl=payload.verify_ssl,
    )
    db.add(source)
    db.flush()
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
    for key, value in data.items():
        setattr(source, key, value)
    if password:
        source.password_enc = encrypt(password, principal.tenant.crypto_key)
        source.token_enc = None  # the cached token was minted with the old one
    source.status = ConnectionStatus.unverified.value
    audit(db, principal, "source.update", source.id, ",".join(data), request)
    db.commit()
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


@router.post("/sources/{source_id}/discover-devices", response_model=list[DeviceOut])
def discover_devices(
    source_id: str,
    request: Request,
    principal: Principal = Depends(require_writer),
    db: Session = Depends(get_db),
) -> list[Device]:
    source = _get_source(db, principal, source_id)
    existing = {
        d.serial_number: d
        for d in db.scalars(
            select(Device).where(Device.tenant_id == principal.tenant.id)
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
    for terminal in terminals:
        serial = (terminal.serial_number or "").strip()
        if not serial:
            continue
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

    audit(db, principal, "device.discover", source.id, f"{added} new", request)
    db.commit()
    return list(
        db.scalars(
            select(Device)
            .where(Device.source_id == source.id)
            .order_by(Device.serial_number)
        ).all()
    )


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
