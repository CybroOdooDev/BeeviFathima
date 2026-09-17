"""BioBridge API entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

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

    if settings.is_production:
        # Refuse to boot with development secrets rather than run a production
        # system whose credential encryption everyone can reproduce.
        if "dev-only" in settings.master_encryption_key:
            raise RuntimeError("MASTER_ENCRYPTION_KEY must be set in production")
        if "dev-only" in settings.jwt_secret or "change-me" in settings.jwt_secret:
            raise RuntimeError("JWT_SECRET must be set in production")
        if settings.cors_origins.strip() == "*":
            raise RuntimeError("CORS_ORIGINS must name real origins in production")
    yield
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


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name, "version": app.version}


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
