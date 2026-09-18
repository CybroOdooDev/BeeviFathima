"""BioBridge API entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from app.api.v1 import api_router
from app.core.config import settings
from app.integrations.base import ProviderError
from app.integrations.odoo import OdooError
from app.services.connections import UnsafeTargetError

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


def _warn_about_schema_drift() -> None:
    """Say at boot when the database is behind the models.

    ``create_all`` adds missing tables and stops there, so a new column on an
    existing table is simply absent — and the first query that mentions it fails
    with ``no such column``, mid-request or mid-sync, pointing at SQLAlchemy
    rather than at the upgrade nobody applied. One line here costs nothing and
    turns that into an instruction.

    A warning, never a refusal: a schema one column behind still serves most of
    the product, and refusing to boot over it would take a working system down.
    """
    try:
        from app.db.base import Base
        from app.db.schema_check import detect_drift
        from app.db.session import engine
        import app.models  # noqa: F401

        drift = detect_drift(engine, Base.metadata)
        if drift.is_empty:
            return
        log.warning(
            "The database is behind the models — missing %s. "
            "Run: python3 tools/migrate.py --apply",
            drift.summary(),
        )
    except Exception as exc:  # noqa: BLE001 — a diagnostic must never block boot
        log.debug("Could not check the schema: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("%s starting (env=%s)", settings.app_name, settings.environment)

    if not settings.is_production:
        # Development convenience: a fresh SQLite file becomes a working system
        # with no extra step. create_all only adds missing tables — it never
        # alters or drops an existing one, so it cannot migrate a schema and is
        # deliberately not run in production. Use tools/init_db.py explicitly,
        # or Alembic once the schema starts changing under live data.
        from app.db.base import Base
        from app.db.session import engine
        import app.models  # noqa: F401 — registers every table on Base.metadata

        Base.metadata.create_all(engine)
        log.info("Schema ensured (development mode)")

    _warn_about_schema_drift()

    if settings.is_production:
        # Refuse to boot with development secrets rather than run a production
        # system whose credential encryption everyone can reproduce.
        if "dev-only" in settings.master_encryption_key:
            raise RuntimeError("MASTER_ENCRYPTION_KEY must be set in production")
        if "dev-only" in settings.jwt_secret or "change-me" in settings.jwt_secret:
            raise RuntimeError("JWT_SECRET must be set in production")
        if settings.cors_origins.strip() == "*":
            raise RuntimeError("CORS_ORIGINS must name real origins in production")

    # The clock. Safe in every replica: a database lease decides which one
    # actually dispatches, so scaling the API does not multiply the syncs.
    # Under SCHEDULER_MODE=celery (or auto with a broker configured) this stands
    # down and beat owns the schedule instead — see config.scheduler_mode.
    if settings.scheduler_runs_in_process:
        from app.services.scheduler import scheduler

        scheduler.start()
    else:
        log.info(
            "In-process scheduler off (SCHEDULER_MODE=%s, broker=%s) — Celery beat "
            "is expected to run the schedule",
            settings.scheduler_mode,
            "set" if settings.redis_url else "unset",
        )

    yield

    if settings.scheduler_runs_in_process:
        from app.services.scheduler import scheduler

        await scheduler.stop()
    log.info("%s shutting down", settings.app_name)


app = FastAPI(
    title="BioBridge",
    description="Multi-tenant middleware syncing biometric punches into Odoo hr.attendance.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

# Empty by default: same-origin needs no CORS, and a wildcard paired with
# credentials is the classic accidental hole.
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(api_router, prefix=settings.api_v1_prefix)


# --- error translation, so the client never sees a raw traceback -------------
@app.exception_handler(OdooError)
async def _odoo_error(_: Request, exc: OdooError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": f"Odoo: {exc}"})


@app.exception_handler(ProviderError)
async def _provider_error(_: Request, exc: ProviderError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": f"Device platform: {exc}"})


@app.exception_handler(UnsafeTargetError)
async def _unsafe_target(_: Request, exc: UnsafeTargetError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(OperationalError)
async def _database_error(_: Request, exc: OperationalError) -> JSONResponse:
    """Turn a schema-drift failure into an instruction.

    A model that gained a column queries for it everywhere, so on a database
    that was not migrated the failure lands on ordinary endpoints — login first,
    since ``select(User)`` names every column. What the user sees is a bare 500
    from a login form, which reads as "wrong password" and sends them hunting in
    exactly the wrong place. The boot log says what is wrong, but by then it has
    scrolled away.
    """
    message = str(exc.orig) if exc.orig else str(exc)
    missing = "no such column" in message.lower() or "undefinedcolumn" in message.lower()
    if missing:
        log.error("Schema drift reached a request: %s", message)
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "The database is missing a column this version needs, so this "
                    "request cannot be served. On the server, run: "
                    "python3 tools/migrate.py --apply"
                ),
                "database_error": message[:200],
            },
        )
    log.exception("Database error")
    return JSONResponse(status_code=503, content={"detail": "The database is unavailable."})


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness. Deliberately cheap — no database — so a load balancer can poll it."""
    return {"status": "ok", "service": settings.app_name, "version": app.version}


@app.get("/health/scheduler", tags=["meta"])
def scheduler_health_endpoint() -> JSONResponse:
    """Is the clock running? **503 when it is not**, so a monitor can alert.

    Point an uptime check here rather than at ``/health``: a BioBridge whose API
    answers but whose scheduler died is the failure that actually hurts, and it
    is invisible from the outside — attendance simply stops appearing in Odoo and
    nobody notices until payroll.

    Unauthenticated on purpose, and it reveals nothing tenant-specific: a
    monitoring endpoint behind a login is a monitoring endpoint nobody wires up.
    """
    from app.db.session import session_scope
    from app.services.scheduling import scheduler_health

    try:
        with session_scope() as db:
            state = scheduler_health(db)
    except Exception as exc:  # noqa: BLE001 — a dead database is also unhealthy
        return JSONResponse(
            status_code=503, content={"status": "error", "detail": str(exc)}
        )

    if settings.scheduler_mode.strip().lower() == "off":
        # Explicitly disabled is a configuration choice, not a fault. Reporting
        # it as unhealthy would train whoever set it to ignore this endpoint.
        return JSONResponse(
            status_code=200, content={"status": "disabled", "mode": "off"}
        )

    payload = {
        "status": "ok" if state["running"] else "stalled",
        "mode": state["mode"],
        "owner": state["owner"],
        "last_tick_at": state["last_tick_at"].isoformat() + "Z"
        if state["last_tick_at"]
        else None,
        "seconds_since_tick": state["seconds_since_tick"],
        "expected_tick_seconds": settings.scheduler_tick_seconds,
    }
    return JSONResponse(status_code=200 if state["running"] else 503, content=payload)


class RevalidatingStatics(StaticFiles):
    """Stop the browser serving a stale copy of the dashboard.

    The SPA is unbundled ES modules with stable filenames, so a new version
    looks like the same URL. Left to heuristic caching the browser keeps running
    the old app.js: the code on disk is right, the screen is not, and refreshing
    appears to do nothing because the browser never asks the server.

    ``no-cache`` still caches — the file is kept and revalidated by ETag, so an
    unchanged asset comes back as an empty 304 while a changed one is picked up.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = (
            "no-store" if settings.environment == "development" else "no-cache"
        )
        return response


STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/app", RevalidatingStatics(directory=STATIC_DIR, html=True), name="dashboard")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Send a browser to the dashboard, or to the docs when it is not shipped.

    Machines checking liveness should use /health.
    """
    return RedirectResponse("/app/" if STATIC_DIR.exists() else "/docs")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Browsers request this unprompted; 204 keeps it out of the log."""
    return Response(status_code=204)
