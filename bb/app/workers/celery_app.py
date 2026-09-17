"""Celery application and beat schedule."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "biobridge",
    broker=settings.redis_url or None,
    backend=settings.redis_url or None,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # A sync that dies mid-push must be redelivered, not lost.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # One tenant at a time per worker process: a sync is I/O-bound on two remote
    # systems, and prefetching just delays other tenants behind a slow one.
    worker_prefetch_multiplier=1,
    task_time_limit=15 * 60,
    task_soft_time_limit=13 * 60,
    result_expires=3600,
    task_default_queue="sync",
    task_routes={
        "app.workers.tasks.sync_tenant": {"queue": "sync"},
        "app.workers.tasks.dispatch_due_tenants": {"queue": "beat"},
        "app.workers.tasks.close_stale_attendances": {"queue": "maintenance"},
        "app.workers.tasks.prune_old_punches": {"queue": "maintenance"},
    },
    beat_schedule={
        # Every minute; the task itself decides who is actually due, which keeps
        # per-tenant intervals out of the scheduler's configuration.
        "dispatch-due-tenants": {
            "task": "app.workers.tasks.dispatch_due_tenants",
            "schedule": 60.0,
        },
        # Hourly. Without this, one employee who forgets to badge out holds an
        # open record that blocks every later check-in for that person.
        "close-stale-attendances": {
            "task": "app.workers.tasks.close_stale_attendances",
            "schedule": crontab(minute=15),
        },
        "prune-old-punches": {
            "task": "app.workers.tasks.prune_old_punches",
            "schedule": crontab(hour=3, minute=30),
        },
    },
)
