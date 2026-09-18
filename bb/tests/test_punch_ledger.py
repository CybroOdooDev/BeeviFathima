"""The punch ledger's filters, through the real HTTP stack.

The ledger is the screen someone opens when Odoo has rejected something, so
"what did gate 2 send on the 14th" has to be answerable without paging through
every punch since install. These tests pin the filters and, more importantly,
that a filter which matches nothing returns nothing — a filter silently ignored
by the query looks like *more* data, which is the failure that misleads.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app
from app.models import Base, Direction, PunchRecord, PunchState, Tenant, User


@pytest.fixture
def api():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        token = client.post(
            "/api/v1/auth/signup",
            json={
                "company_name": "Ledger Co",
                "email": "ledger@example.com",
                "password": "a-long-enough-password",
                "timezone": "Asia/Dubai",
            },
        ).json()["access_token"]

        db = Session()
        tenant_id = db.scalar(
            Tenant.__table__.select().with_only_columns(Tenant.id)
        )
        seed(db, tenant_id)
        db.close()

        client.headers.update({"Authorization": f"Bearer {token}"})
        yield client
    app.dependency_overrides.clear()


def seed(db, tenant_id: str) -> None:
    """Two terminals, three badges, three days, a mix of states, two runs."""
    rows = [
        # (external_id, badge, utc, direction, terminal, state, run)
        ("1", "1001", "2026-09-14 04:02:00", Direction.inward, "GATE-01", PunchState.synced, "runA"),
        ("2", "1001", "2026-09-14 13:31:00", Direction.outward, "GATE-01", PunchState.synced, "runA"),
        ("3", "1001", "2026-09-15 03:58:00", Direction.inward, "GATE-01", PunchState.error, "runB"),
        ("4", "1001", "2026-09-15 14:14:00", Direction.outward, "GATE-02", PunchState.error, "runB"),
        ("5", "0042", "2026-09-15 05:11:00", Direction.inward, "GATE-02", PunchState.unmapped, "runB"),
        # No run: a punch recorded before the column existed.
        ("6", "A7", "2026-09-16 02:30:00", Direction.inward, "GATE-01", PunchState.pending, None),
    ]
    for external_id, badge, stamp, direction, terminal, state, run in rows:
        db.add(
            PunchRecord(
                tenant_id=tenant_id,
                source_id="src",
                external_id=external_id,
                emp_code=badge,
                punch_time_utc=datetime.fromisoformat(stamp),
                direction=direction.value,
                terminal_sn=terminal,
                process_state=state.value,
                first_seen_run_id=run,
            )
        )
    db.commit()


def punches(api, **params) -> list[dict]:
    response = api.get("/api/v1/punches", params={"limit": 200, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_whole_ledger_comes_back_newest_first(api):
    rows = punches(api)
    assert len(rows) == 6
    stamps = [r["punch_time_utc"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_filtering_by_terminal(api):
    rows = punches(api, terminal_sn="GATE-02")
    assert {r["terminal_sn"] for r in rows} == {"GATE-02"}
    assert len(rows) == 2


def test_an_unknown_terminal_returns_nothing(api):
    """Not everything. A filter dropped by the query reads as more data, and
    whoever is debugging believes the punches came from a gate they did not."""
    assert punches(api, terminal_sn="NO-SUCH-GATE") == []


def test_the_date_range_is_inclusive_at_both_ends(api):
    """Asking for the 15th means the whole of the 15th, including 23:59."""
    rows = punches(api, date_from="2026-09-15", date_to="2026-09-15")
    assert {r["punch_time_utc"][:10] for r in rows} == {"2026-09-15"}
    assert len(rows) == 3


def test_a_single_day_needs_both_bounds_the_same(api):
    assert len(punches(api, date_from="2026-09-14", date_to="2026-09-14")) == 2


def test_an_open_ended_range_works_from_one_side(api):
    assert len(punches(api, date_from="2026-09-15")) == 4
    assert len(punches(api, date_to="2026-09-14")) == 2


def test_filters_combine(api):
    """The actual question: what did this gate send for this badge that day."""
    rows = punches(
        api, terminal_sn="GATE-01", emp_code="1001",
        date_from="2026-09-14", date_to="2026-09-14",
    )
    assert len(rows) == 2
    assert {r["emp_code"] for r in rows} == {"1001"}


def test_state_still_filters_alongside_the_new_parameters(api):
    rows = punches(api, state="error", date_from="2026-09-15", date_to="2026-09-15")
    assert len(rows) == 2
    assert {r["process_state"] for r in rows} == {"error"}


def test_a_range_matching_nothing_is_empty(api):
    assert punches(api, date_from="2001-01-01", date_to="2001-01-02") == []


def test_filtering_by_run_shows_what_that_sync_brought_in(api):
    """The question the run counters cannot answer: *which* punches were those."""
    rows = punches(api, run_id="runB")
    assert len(rows) == 3
    assert {r["first_seen_run_id"] for r in rows} == {"runB"}


def test_a_run_that_ingested_nothing_returns_nothing(api):
    """Common and not an error: every punch on the wire was already in the
    ledger, so the run brought in none of them."""
    assert punches(api, run_id="runC-never-ingested-anything") == []


def test_the_run_id_is_exposed_on_every_punch(api):
    """The UI links a punch back to its run, so the field has to be readable —
    and null for punches that predate the column rather than absent."""
    rows = punches(api)
    assert all("first_seen_run_id" in r for r in rows)
    assert any(r["first_seen_run_id"] is None for r in rows)


def test_the_run_filter_combines_with_the_others(api):
    rows = punches(api, run_id="runB", state="error")
    assert len(rows) == 2
    assert {r["process_state"] for r in rows} == {"error"}


def test_the_ledger_never_crosses_tenants(api):
    """The filters add WHERE clauses; none of them may replace the tenant scope."""
    other = api.post(
        "/api/v1/auth/signup",
        json={
            "company_name": "Someone Else",
            "email": "other@example.com",
            "password": "a-long-enough-password",
            "timezone": "UTC",
        },
    ).json()["access_token"]

    response = api.get(
        "/api/v1/punches",
        params={"terminal_sn": "GATE-01", "limit": 200},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert response.status_code == 200
    assert response.json() == []
