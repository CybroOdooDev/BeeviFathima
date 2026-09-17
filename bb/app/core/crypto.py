"""Envelope encryption for tenant credentials.

Every secret stored in the database — Odoo API keys, BioTime passwords, cached
tokens — is encrypted with a Fernet key derived per tenant from the master key
using HKDF.

Two properties follow, and both matter commercially:

* A leaked database dump decrypts to nothing without the master key.
* One tenant's derived key cannot read another tenant's rows, so a bug that
  crosses the tenant boundary still cannot read the credentials it reached.

Ciphertext is version-tagged so keys can be rotated without a big-bang
migration: a reader accepts any version it knows, a writer emits the current one.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings

CURRENT_VERSION = "v1"
_SALT = b"biobridge.credential.v1"
_CACHE: dict[str, Fernet] = {}


class CryptoError(ValueError):
    """The ciphertext is unreadable: wrong key, wrong version, or tampered."""


def _fernet_for(tenant_key: str) -> Fernet:
    """Derive and memoise the Fernet instance for one tenant."""
    cached = _CACHE.get(tenant_key)
    if cached is not None:
        return cached

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        info=f"tenant:{tenant_key}".encode(),
    )
    derived = hkdf.derive(settings.master_encryption_key.encode())
    instance = Fernet(base64.urlsafe_b64encode(derived))
    _CACHE[tenant_key] = instance
    return instance


def encrypt(plaintext: str | None, tenant_key: str) -> str | None:
    """Encrypt a secret for one tenant. Empty input stays empty."""
    if not plaintext:
        return None
    token = _fernet_for(tenant_key).encrypt(plaintext.encode())
    return f"{CURRENT_VERSION}:{token.decode()}"


def decrypt(ciphertext: str | None, tenant_key: str) -> str | None:
    """Decrypt a secret. Raises CryptoError on a wrong key or tampering."""
    if not ciphertext:
        return None
    version, _, payload = ciphertext.partition(":")
    if version != CURRENT_VERSION:
        raise CryptoError(f"Unsupported ciphertext version {version!r}")
    try:
        return _fernet_for(tenant_key).decrypt(payload.encode()).decode()
    except InvalidToken as exc:
        raise CryptoError("Cannot decrypt credential (wrong key or tampered)") from exc


def mask(secret: str | None, keep: int = 4) -> str:
    """Render a secret safe to display: ``sk_l••••••••3f2a``."""
    if not secret:
        return ""
    if len(secret) <= keep * 2:
        return "•" * len(secret)
    return f"{secret[:keep]}{'•' * 8}{secret[-keep:]}"
