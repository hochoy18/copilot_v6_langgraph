"""Authenticated encryption for Tool Credentials.

Per ADR-0002 the credential bytes must never reach the LLM prompt or
the front end — they live only inside the Worker at call time. This
module is the thin encryption layer that makes that promise hold:

* `MasterKey` — owns a 32-byte symmetric key. Constructed from either a
  base64-encoded literal (production) or a passphrase + salt pair via
  PBKDF2-HMAC-SHA256 (dev / .env default). The two factories mean the
  `.env.example` can ship a readable passphrase rather than a raw key.
* `CredentialEncryptor` — wraps a `MasterKey` and offers `encrypt` /
  `decrypt` over byte strings. AES-256-GCM is the chosen primitive:
  AEAD, so a tampered ciphertext fails to decrypt (no silent corruption
  of credential bytes — see ADR-0024 / ADR-0027).
* `EncryptedPayload` — wire shape carrying `nonce` + `ciphertext` +
  `key_id`. Persisted by `CredentialRepository`; rotated independently
  of the key bytes by stamping a new `key_id`.

Why a dedicated module: this is the single seam that touches raw key
material. Everything outside it (settings, repositories, tests) sees
`CredentialEncryptor` as an opaque object. A future KMS integration
replaces only this file.

References: ADR-0002 (凭证隔离), ADR-0024 (凭证失效感知),
ADR-0027 (Plan-Tool 快照), ADR-0015 (backend tech stack — Python).
"""
from __future__ import annotations

import base64
import binascii
import os
import secrets
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# AES-256 key length in bytes. AES-GCM accepts 128/192/256-bit keys;
# we pick 256 because the cost difference is negligible and the security
# margin against quantum-assisted search is the widest available.
_KEY_BYTES = 32

# GCM standard nonce size. NIST SP 800-38D recommends 12 bytes; AESGCM
# in `cryptography` accepts it natively without further conversion.
_NONCE_BYTES = 12

# PBKDF2 iteration count. OWASP 2023 baseline for HMAC-SHA256 is
# 600_000; we round up to 1_000_000 to leave headroom against faster
# hardware without breaking dev-loop responsiveness.
_PBKDF2_ITERATIONS = 1_000_000

# PBKDF2 salt length. 16 bytes is the documented minimum; longer is
# harmless and we keep the floor at 16 so generated salts are uniform.
_SALT_BYTES = 16


@dataclass(frozen=True)
class MasterKey:
    """A 32-byte symmetric key for AES-256-GCM, plus a stable `key_id`.

    `key_id` lets us rotate the underlying key material while keeping
    older ciphertexts decryptable: every `EncryptedPayload` records
    which `key_id` sealed it. A future multi-key registry can then
    resolve the right `MasterKey` per payload. For now we have one key
    per process and the `key_id` is just a human label.
    """

    key_bytes: bytes
    key_id: str

    def __post_init__(self) -> None:
        if len(self.key_bytes) != _KEY_BYTES:
            raise ValueError(
                f"MasterKey must be exactly {_KEY_BYTES} bytes, "
                f"got {len(self.key_bytes)}"
            )
        if not self.key_id:
            raise ValueError("MasterKey.key_id must be a non-empty string")

    # -- Factories -----------------------------------------------------

    @classmethod
    def from_base64(cls, key_b64: str, *, key_id: str = "primary") -> MasterKey:
        """Build a `MasterKey` from a base64-encoded 32-byte literal.

        Production deployments set `COPILOT_CREDENTIAL_ENCRYPTION_KEY`
        to a base64 string of 32 random bytes (44 chars including
        padding). This is the preferred form — no KDF, no chance of a
        weak passphrase.
        """
        try:
            raw = base64.b64decode(key_b64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(
                "MasterKey.from_base64 expects a base64-encoded 32-byte key"
            ) from exc
        return cls(key_bytes=raw, key_id=key_id)

    @classmethod
    def from_passphrase(
        cls,
        passphrase: str,
        *,
        salt: bytes,
        key_id: str = "derived",
    ) -> MasterKey:
        """Derive a 32-byte key from a passphrase via PBKDF2-HMAC-SHA256.

        Used for dev / `.env.example` so the file stays human-readable.
        The salt is a server-side constant (or, in future, persisted
        per-environment); deriving a stable salt from a fixed string keeps
        `from_passphrase` deterministic without forcing callers to
        manage a separate config.
        """
        if len(salt) < _SALT_BYTES:
            raise ValueError(
                f"salt must be at least {_SALT_BYTES} bytes, got {len(salt)}"
            )
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=_KEY_BYTES,
            salt=salt,
            iterations=_PBKDF2_ITERATIONS,
        )
        return cls(key_bytes=kdf.derive(passphrase.encode("utf-8")), key_id=key_id)


@dataclass(frozen=True)
class EncryptedPayload:
    """Wire shape of a sealed credential payload.

    `nonce` and `ciphertext` are kept separate so `CredentialRepository`
    can persist them as distinct BSON `Binary` fields (handy for
    partial decrypt without parsing). `key_id` lets a future multi-key
    registry route decryption to the right `MasterKey`.
    """

    nonce: bytes
    ciphertext: bytes
    key_id: str

    def __post_init__(self) -> None:
        if len(self.nonce) != _NONCE_BYTES:
            raise ValueError(
                f"nonce must be exactly {_NONCE_BYTES} bytes, got {len(self.nonce)}"
            )
        if not self.ciphertext:
            raise ValueError("ciphertext must be non-empty")
        if not self.key_id:
            raise ValueError("key_id must be a non-empty string")


class CredentialEncryptor(Protocol):
    """Protocol seam for swappable encryption implementations.

    Repositories depend on this protocol rather than the concrete
    `AesGcmEncryptor` so tests can pass a stub that returns canned
    bytes, and a future KMS-backed implementation can drop in without
    touching repository code.
    """

    @property
    def key_id(self) -> str: ...

    def encrypt(self, plaintext: bytes, *, aad: bytes = b"") -> EncryptedPayload: ...

    def decrypt(self, payload: EncryptedPayload, *, aad: bytes = b"") -> bytes: ...


class AesGcmEncryptor:
    """AES-256-GCM encryptor. AEAD: tampered ciphertext fails closed.

    `encrypt` generates a fresh 12-byte nonce per call (AESGCM.nonce-
    length-aware, never reused). `decrypt` raises `EncryptionError` on
    any cryptographic failure — invalid tag, wrong key, malformed
    payload — so a corrupted row cannot leak plaintext bits into an
    exception message.
    """

    def __init__(self, master_key: MasterKey) -> None:
        self._key = master_key
        self._aesgcm = AESGCM(master_key.key_bytes)

    @property
    def key_id(self) -> str:
        """The `key_id` stamped onto every payload produced by this encryptor."""
        return self._key.key_id

    def encrypt(self, plaintext: bytes, *, aad: bytes = b"") -> EncryptedPayload:
        """Seal `plaintext` (with optional `aad` as authenticated data).

        Raises:
            ValueError: `plaintext` is empty — refusing to encrypt zero
                bytes guards against silent misuse where the caller
                forgot to populate the payload.
        """
        if not plaintext:
            raise ValueError("plaintext must be non-empty")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, aad)
        return EncryptedPayload(nonce=nonce, ciphertext=ciphertext, key_id=self.key_id)

    def decrypt(self, payload: EncryptedPayload, *, aad: bytes = b"") -> bytes:
        """Open a payload, raising `EncryptionError` on any failure.

        `InvalidTag` (wrong key / tampered ciphertext / wrong AAD) and
        malformed payloads all funnel into the same exception so a
        caller cannot infer anything from the failure mode.
        """
        try:
            return self._aesgcm.decrypt(payload.nonce, payload.ciphertext, aad)
        except InvalidTag as exc:
            raise EncryptionError("decrypt failed: invalid tag") from exc
        except (ValueError, TypeError) as exc:
            # AESGCM raises generic ValueError/TypeError on malformed
            # input lengths; collapse them to a single envelope.
            raise EncryptionError("decrypt failed: malformed payload") from exc


class EncryptionError(Exception):
    """Raised when encryption or decryption fails for any reason.

    Distinct from `ValueError` (programmer errors) and from any
    pymongo error (storage). Callers should treat any `EncryptionError`
    as "this credential row cannot be opened right now" — never as a
    signal that plaintext was recoverable.
    """


def generate_salt() -> bytes:
    """Return a fresh 16-byte salt for PBKDF2 derivation.

    Useful for tests that exercise `MasterKey.from_passphrase` with a
    deterministic salt but want a cryptographically-uniform starting
    point. Production deployments should configure a fixed salt in env.
    """
    return os.urandom(_SALT_BYTES)


__all__ = [
    "AesGcmEncryptor",
    "CredentialEncryptor",
    "EncryptedPayload",
    "EncryptionError",
    "MasterKey",
    "generate_salt",
]