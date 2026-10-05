# syntax=docker/dockerfile:1
#
# One image, three possible roles — api, worker, beat — selected by the command.
# The API image already contains the scheduler, so `docker compose up` alone
# gives you a working schedule; worker and beat are for the multi-node case.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build deps for psycopg, then removed — the wheel is linked against libpq,
# which stays.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && apt-get purge -y --auto-remove gcc libpq-dev

COPY app ./app
COPY tools ./tools
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

# Non-root. The container writes nothing but logs when DATABASE_URL points at
# Postgres; on SQLite, mount a volume owned by this uid.
RUN useradd --create-home --uid 10001 biobridge \
    && chown -R biobridge:biobridge /app
USER biobridge

EXPOSE 8000

# Liveness, not schedule health. Compose restarts a container that stops
# answering; a container that answers while its scheduler is dead needs
# /health/scheduler, which belongs in your monitoring rather than here — a
# healthcheck on it would restart the API for a fault a restart may not fix.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# The schema is brought up to date before the API starts, so a deploy can never
# serve code that expects columns the database does not have yet. db_upgrade
# takes a database lock, so replicas starting together take turns, and it is a
# no-op when nothing is pending. The worker and beat services override this
# command and do not migrate — they start after the API in compose.
CMD ["sh", "-c", "python tools/db_upgrade.py && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
