"""Tenant isolation and auth, exercised through the real HTTP stack.

There is no row-level security in the database, so isolation is entirely a
property of the dependency and the queries. That makes it exactly the thing to
test through the API rather than at the unit level.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app
from app.models import Base


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def signup(client, company, email):
    response = client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": company,
            "email": email,
            "password": "a-long-enough-password",
            "timezone": "Asia/Dubai",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def make_odoo(client, token, name="Primary Odoo"):
    response = client.post(
        "/api/v1/odoo-connections",
        headers=auth(token),
        json={
            "name": name,
            "url": "https://acme.odoo.com",
            "db_name": "acme",
            "username": "bot@acme.com",
            "api_key": "super-secret-key",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
def test_health_needs_no_auth(client):
    assert client.get("/health").json()["status"] == "ok"


def test_protected_routes_reject_anonymous_callers(client):
    for path in ("/api/v1/tenant", "/api/v1/dashboard", "/api/v1/odoo-connections"):
        assert client.get(path).status_code == 401, path


def test_signup_creates_an_owner_and_a_tenant(client):
    token = signup(client, "Acme", "owner@acme.com")
    me = client.get("/api/v1/auth/me", headers=auth(token)).json()
    assert me["role"] == "owner"

    tenant = client.get("/api/v1/tenant", headers=auth(token)).json()
    assert tenant["slug"] == "acme"
    assert tenant["timezone"] == "Asia/Dubai"


def test_duplicate_email_is_refused(client):
    signup(client, "Acme", "owner@acme.com")
    response = client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": "Other",
            "email": "owner@acme.com",
            "password": "a-long-enough-password",
        },
    )
    assert response.status_code == 409


def test_slug_collision_is_resolved(client):
    signup(client, "Acme", "a@acme.com")
    signup(client, "Acme", "b@acme.com")
    token = client.post(
        "/api/v1/auth/login",
        json={"email": "b@acme.com", "password": "a-long-enough-password"},
    ).json()["access_token"]
    assert client.get("/api/v1/tenant", headers=auth(token)).json()["slug"] == "acme-2"


def test_login_is_rate_limited_after_repeated_failures(client):
    signup(client, "Acme", "owner@acme.com")
    for _ in range(8):
        client.post(
            "/api/v1/auth/login", json={"email": "owner@acme.com", "password": "wrong"}
        )
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@acme.com", "password": "a-long-enough-password"},
    )
    assert response.status_code == 429
    assert "Try again in" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #
def test_one_tenant_cannot_see_anothers_connections(client):
    token_a = signup(client, "Alpha", "a@alpha.com")
    token_b = signup(client, "Beta", "b@beta.com")
    make_odoo(client, token_a)

    assert len(client.get("/api/v1/odoo-connections", headers=auth(token_a)).json()) == 1
    assert client.get("/api/v1/odoo-connections", headers=auth(token_b)).json() == []


def test_addressing_another_tenants_object_by_id_returns_404_not_403(client):
    """404 so the id's existence is never confirmed to an outsider."""
    token_a = signup(client, "Alpha", "a@alpha.com")
    token_b = signup(client, "Beta", "b@beta.com")
    conn = make_odoo(client, token_a)

    for method, path in [
        ("patch", f"/api/v1/odoo-connections/{conn['id']}"),
        ("post", f"/api/v1/odoo-connections/{conn['id']}/test"),
    ]:
        response = getattr(client, method)(path, headers=auth(token_b), json={})
        assert response.status_code == 404, path


def test_dashboards_do_not_bleed_between_tenants(client):
    token_a = signup(client, "Alpha", "a@alpha.com")
    token_b = signup(client, "Beta", "b@beta.com")
    make_odoo(client, token_a)

    assert client.get("/api/v1/dashboard", headers=auth(token_a)).json()[
        "connection_health"
    ]["odoo"] != "missing"
    assert client.get("/api/v1/dashboard", headers=auth(token_b)).json()[
        "connection_health"
    ]["odoo"] == "missing"


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
def test_the_api_key_never_comes_back_out(client):
    token = signup(client, "Acme", "owner@acme.com")
    created = make_odoo(client, token)

    assert "api_key" not in created
    assert "super-secret-key" not in str(created)

    listed = client.get("/api/v1/odoo-connections", headers=auth(token)).json()
    assert "super-secret-key" not in str(listed)


def test_the_stored_api_key_is_encrypted_at_rest(client):
    """Read the column directly: the ciphertext must not contain the secret."""
    from app.core.crypto import decrypt
    from app.models import OdooConnection, Tenant

    token = signup(client, "Acme", "owner@acme.com")
    make_odoo(client, token)

    db = next(app.dependency_overrides[get_db]())
    conn = db.query(OdooConnection).first()
    tenant = db.get(Tenant, conn.tenant_id)

    assert conn.api_key_enc.startswith("v1:")
    assert "super-secret-key" not in conn.api_key_enc
    assert decrypt(conn.api_key_enc, tenant.crypto_key) == "super-secret-key"


def test_a_tenants_key_cannot_decrypt_another_tenants_secret(client):
    """The per-tenant derivation is what limits the blast radius of a leak."""
    from app.core.crypto import CryptoError, decrypt
    from app.models import OdooConnection, Tenant

    token_a = signup(client, "Alpha", "a@alpha.com")
    signup(client, "Beta", "b@beta.com")
    make_odoo(client, token_a)

    db = next(app.dependency_overrides[get_db]())
    conn = db.query(OdooConnection).first()
    other = db.query(Tenant).filter(Tenant.slug == "beta").first()

    with pytest.raises(CryptoError):
        decrypt(conn.api_key_enc, other.crypto_key)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_an_odoo_url_with_a_path_is_rejected_at_the_edge(client):
    """The /odoo suffix the browser shows on Odoo 17+ is the classic mistake."""
    token = signup(client, "Acme", "owner@acme.com")
    response = client.post(
        "/api/v1/odoo-connections",
        headers=auth(token),
        json={
            "url": "https://acme.odoo.com/odoo",
            "db_name": "acme",
            "username": "bot@acme.com",
            "api_key": "k",
        },
    )
    # Accepted by the schema, rejected by the client when it is built.
    assert response.status_code in (201, 400, 502)
    if response.status_code == 201:
        probe = client.post(
            f"/api/v1/odoo-connections/{response.json()['id']}/test", headers=auth(token)
        ).json()
        assert probe["ok"] is False
        assert "no path" in probe["message"]


def test_a_url_without_a_scheme_is_refused(client):
    token = signup(client, "Acme", "owner@acme.com")
    response = client.post(
        "/api/v1/odoo-connections",
        headers=auth(token),
        json={
            "url": "acme.odoo.com",
            "db_name": "acme",
            "username": "bot@acme.com",
            "api_key": "k",
        },
    )
    assert response.status_code == 422


def test_an_unknown_provider_is_refused_at_creation(client):
    """Better than storing a row that can only fail later, at sync time."""
    token = signup(client, "Acme", "owner@acme.com")
    response = client.post(
        "/api/v1/sources",
        headers=auth(token),
        json={
            "provider": "not-a-real-vendor",
            "base_url": "https://bio.test",
            "username": "u",
            "password": "p",
        },
    )
    assert response.status_code == 400
    assert "Unknown device platform" in response.json()["detail"]


def test_the_provider_catalogue_drives_the_setup_form(client):
    token = signup(client, "Acme", "owner@acme.com")
    providers = client.get("/api/v1/providers", headers=auth(token)).json()

    biotime = next(p for p in providers if p["slug"] == "biotime")
    assert "read_punches" in biotime["capabilities"]
    # Must match a real SourceIn/DeviceSource field name — this drives both
    # the settings form and the server-side required-field check.
    assert any(f["name"] == "server_timezone" for f in biotime["config_fields"])
