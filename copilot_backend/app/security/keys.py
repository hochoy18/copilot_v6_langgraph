"""Build the credential encryptor from `Settings`.

This is the bridge between `app.settings.Settings` (env-driven config)
and `app.security.crypto.CredentialEncryptor` (the encryption seam).
Centralising it here keeps both modules independent: settings doesn't
import cryptography, and crypto doesn't import pydantic-settings.

Dispatch rule
-------------

T05 (#6) uses **PBKDF2-HMAC-SHA256 derivation unconditionally** — even
when the operator supplies a CSPRNG secret. The earlier dual-mode
factory (raw 32-byte base64 literal vs. passphrase) was ambiguous: a
44-character passphrase that happened to base64-decode to 32 bytes was
silently treated as a raw key. Forcing every operator through PBKDF2
makes the dispatch deterministic: same `credential_encryption_key` +
same `credential_encryption_salt` ⇒ same derived key, restart-stable.

Production deployments set `COPILOT_CREDENTIAL_ENCRYPTION_KEY` to a
CSPRNG-generated string (e.g. `openssl rand -hex 32`). Dev sets the
shipped dev passphrase from `.env.example`.
"""
from __future__ import annotations

from app.security.crypto import AesGcmEncryptor, CredentialEncryptor, MasterKey
from app.settings import Settings


def build_credential_encryptor(settings: Settings) -> CredentialEncryptor:
    """Return a process-wide `CredentialEncryptor` derived from `settings`.

    Args:
        settings: env-driven configuration. Reads
            `credential_encryption_key` and `credential_encryption_salt`.

    Returns:
        An `AesGcmEncryptor` wrapping a `MasterKey`. The encryptor is
        cheap to construct and stateless across calls; callers should
        cache one instance per process (the FastAPI lifespan does this).
    """
    salt = settings.credential_encryption_salt.encode("utf-8")
    master = MasterKey.from_passphrase(
        settings.credential_encryption_key, salt=salt, key_id="primary"
    )
    return AesGcmEncryptor(master)


__all__ = ["build_credential_encryptor"]
