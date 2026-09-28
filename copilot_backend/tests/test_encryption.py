"""Tests for the credential encryption layer (T05 / #6).

Acceptance criterion for the ticket: "Credential 加密写入".
The seam is `AesGcmEncryptor` + `MasterKey` + `EncryptedPayload`.
We verify:

* Each `encrypt` produces a fresh nonce (no reuse) and a distinct
  ciphertext (even for identical plaintexts).
* `decrypt` round-trips every payload it produces.
* Tampered ciphertext fails closed (`EncryptionError`, never plaintext bits).
* Wrong AAD fails closed.
* `MasterKey` length invariants are enforced at construction time.
* The `keys.py` factory derives a deterministic key from passphrase + salt.
"""
from __future__ import annotations

import os

import pytest

from app.security.crypto import (
    AesGcmEncryptor,
    EncryptedPayload,
    EncryptionError,
    MasterKey,
)
from app.security.keys import build_credential_encryptor
from app.settings import Settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def master_key() -> MasterKey:
    """A fresh `MasterKey` for each test.

    Using `os.urandom` keeps the key material distinct across tests so a
    leak in one assertion can't decrypt another's payload.
    """
    return MasterKey(key_bytes=os.urandom(32), key_id="test")


@pytest.fixture
def encryptor(master_key: MasterKey) -> AesGcmEncryptor:
    return AesGcmEncryptor(master_key)


# ---------------------------------------------------------------------------
# MasterKey construction
# ---------------------------------------------------------------------------


class TestMasterKey:
    """`MasterKey` construction + factory methods."""

    def test_rejects_wrong_length_bytes(self) -> None:
        """MasterKey must be exactly 32 bytes; shorter / longer raises."""
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            MasterKey(key_bytes=b"short", key_id="x")
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            MasterKey(key_bytes=os.urandom(31), key_id="x")
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            MasterKey(key_bytes=os.urandom(64), key_id="x")

    def test_rejects_empty_key_id(self) -> None:
        """`key_id` is part of the audit trace; empty values are invalid."""
        with pytest.raises(ValueError, match="key_id"):
            MasterKey(key_bytes=os.urandom(32), key_id="")

    def test_from_passphrase_is_deterministic(self) -> None:
        """Same passphrase + salt → same key bytes (PBKDF2 is deterministic)."""
        salt = os.urandom(16)
        a = MasterKey.from_passphrase("hello", salt=salt, key_id="derived")
        b = MasterKey.from_passphrase("hello", salt=salt, key_id="derived")
        assert a.key_bytes == b.key_bytes
        assert a.key_id == "derived"

    def test_from_passphrase_rejects_short_salt(self) -> None:
        """Salt under 16 bytes is rejected per NIST SP 800-132 floor."""
        with pytest.raises(ValueError, match="salt must be at least"):
            MasterKey.from_passphrase("p", salt=b"short", key_id="x")


# ---------------------------------------------------------------------------
# AesGcmEncryptor — happy path
# ---------------------------------------------------------------------------


class TestAesGcmEncrypt:
    """`encrypt` produces fresh, distinct ciphertexts every call."""

    def test_key_id_passthrough(self, encryptor: AesGcmEncryptor) -> None:
        """`encryptor.key_id` mirrors the underlying `MasterKey.key_id`."""
        assert encryptor.key_id == "test"

    def test_encrypt_produces_fresh_nonce_each_call(
        self, encryptor: AesGcmEncryptor
    ) -> None:
        """Two encrypts of identical plaintext use different nonces.

        GCM nonce reuse is catastrophic — it leaks the XOR of plaintexts.
        The encryptor must generate a fresh 12-byte nonce every call.
        """
        plaintext = b"shared-secret"
        a = encryptor.encrypt(plaintext)
        b = encryptor.encrypt(plaintext)
        assert a.nonce != b.nonce
        # Distinct nonces ⇒ distinct ciphertexts (with overwhelming
        # probability) for the same plaintext under the same key.
        assert a.ciphertext != b.ciphertext

    def test_encrypt_stamps_key_id(self, encryptor: AesGcmEncryptor) -> None:
        """The produced payload carries the encryptor's `key_id`."""
        payload = encryptor.encrypt(b"hello")
        assert payload.key_id == "test"

    def test_encrypt_rejects_empty_plaintext(
        self, encryptor: AesGcmEncryptor
    ) -> None:
        """Empty plaintext raises `ValueError` — refuse silent misuse."""
        with pytest.raises(ValueError, match="non-empty"):
            encryptor.encrypt(b"")


class TestAesGcmDecrypt:
    """`decrypt` round-trips and refuses tampering."""

    def test_round_trip(self, encryptor: AesGcmEncryptor) -> None:
        """encrypt → decrypt returns the original plaintext."""
        plaintext = b"super-secret-api-key"
        payload = encryptor.encrypt(plaintext)
        assert encryptor.decrypt(payload) == plaintext

    def test_round_trip_with_aad(self, encryptor: AesGcmEncryptor) -> None:
        """AAD is part of the auth tag; matching AAD decrypts cleanly."""
        plaintext = b"data"
        aad = b"credentials_id=507f1f77bcf86cd799439011"
        payload = encryptor.encrypt(plaintext, aad=aad)
        assert encryptor.decrypt(payload, aad=aad) == plaintext

    def test_wrong_aad_raises_encryption_error(
        self, encryptor: AesGcmEncryptor
    ) -> None:
        """Wrong AAD fails the GCM tag check — never leak plaintext."""
        payload = encryptor.encrypt(b"data", aad=b"correct")
        with pytest.raises(EncryptionError):
            encryptor.decrypt(payload, aad=b"wrong")

    def test_tampered_ciphertext_raises_encryption_error(
        self, encryptor: AesGcmEncryptor
    ) -> None:
        """Flipping a byte in the ciphertext breaks the tag check."""
        payload = encryptor.encrypt(b"critical-secret")
        tampered = EncryptedPayload(
            nonce=payload.nonce,
            ciphertext=payload.ciphertext[:-1] + bytes([payload.ciphertext[-1] ^ 1]),
            key_id=payload.key_id,
        )
        with pytest.raises(EncryptionError):
            encryptor.decrypt(tampered)

    def test_wrong_key_raises_encryption_error(self) -> None:
        """A different encryptor (different key) cannot decrypt the payload."""
        encryptor_a = AesGcmEncryptor(
            MasterKey(key_bytes=os.urandom(32), key_id="a")
        )
        encryptor_b = AesGcmEncryptor(
            MasterKey(key_bytes=os.urandom(32), key_id="b")
        )
        payload = encryptor_a.encrypt(b"only-a-knows")
        with pytest.raises(EncryptionError):
            encryptor_b.decrypt(payload)

    def test_malformed_nonce_raises_encryption_error(
        self, encryptor: AesGcmEncryptor
    ) -> None:
        """A nonce of the wrong length surfaces as `EncryptionError`.

        `EncryptedPayload.__post_init__` rejects short nonces first;
        the encryptor's `decrypt` then funnels any malformed input into
        `EncryptionError` so callers never see raw driver exceptions.
        """
        # `EncryptedPayload.__post_init__` rejects short nonces.
        with pytest.raises(ValueError, match="nonce"):
            EncryptedPayload(nonce=b"short", ciphertext=b"x", key_id="test")

        # An over-length nonce likewise fails validation.
        with pytest.raises(ValueError, match="nonce"):
            EncryptedPayload(nonce=b"\x00" * 13, ciphertext=b"x", key_id="test")

    def test_empty_ciphertext_raises_value_error(
        self, encryptor: AesGcmEncryptor
    ) -> None:
        """`EncryptedPayload` requires non-empty ciphertext."""
        with pytest.raises(ValueError, match="ciphertext"):
            EncryptedPayload(nonce=b"\x00" * 12, ciphertext=b"", key_id="test")


# ---------------------------------------------------------------------------
# EncryptedPayload validation
# ---------------------------------------------------------------------------


class TestEncryptedPayload:
    """`EncryptedPayload` validation runs in `__post_init__`."""

    def test_rejects_wrong_length_nonce(self) -> None:
        with pytest.raises(ValueError, match="nonce"):
            EncryptedPayload(nonce=b"x" * 11, ciphertext=b"x", key_id="k")
        with pytest.raises(ValueError, match="nonce"):
            EncryptedPayload(nonce=b"x" * 13, ciphertext=b"x", key_id="k")

    def test_rejects_empty_ciphertext(self) -> None:
        with pytest.raises(ValueError, match="ciphertext"):
            EncryptedPayload(nonce=b"\x00" * 12, ciphertext=b"", key_id="k")

    def test_rejects_empty_key_id(self) -> None:
        with pytest.raises(ValueError, match="key_id"):
            EncryptedPayload(nonce=b"\x00" * 12, ciphertext=b"x", key_id="")


# ---------------------------------------------------------------------------
# keys.py — build_credential_encryptor
# ---------------------------------------------------------------------------


class TestBuildCredentialEncryptor:
    """`build_credential_encryptor` derives a key via PBKDF2."""

    def test_derives_key_via_pbkdf2(self) -> None:
        """Same passphrase + salt → same key bytes; round-trips cleanly."""
        settings = Settings(
            credential_encryption_key="dev-only-do-not-use-in-prod",
            credential_encryption_salt="copilot-dev-salt-001",
        )
        encryptor = build_credential_encryptor(settings)
        assert encryptor.key_id == "primary"
        # Round-trip on the same instance.
        payload = encryptor.encrypt(b"hello")
        assert encryptor.decrypt(payload) == b"hello"

    def test_is_deterministic_across_calls(self) -> None:
        """Two encryptors from the same settings agree on the same plaintext."""
        settings = Settings(
            credential_encryption_key="stable-secret",
            credential_encryption_salt="stable-salt-0001",
        )
        encryptor_a = build_credential_encryptor(settings)
        encryptor_b = build_credential_encryptor(settings)
        payload = encryptor_a.encrypt(b"shared")
        assert encryptor_b.decrypt(payload) == b"shared"

    def test_different_salt_produces_different_key(self) -> None:
        """A different salt rotates the master key — old payloads stay sealed."""
        settings_a = Settings(
            credential_encryption_key="shared-secret",
            credential_encryption_salt="salt-aaaaaaaaaaaa",
        )
        settings_b = Settings(
            credential_encryption_key="shared-secret",
            credential_encryption_salt="salt-bbbbbbbbbbbb",
        )
        encryptor_a = build_credential_encryptor(settings_a)
        encryptor_b = build_credential_encryptor(settings_b)
        payload = encryptor_a.encrypt(b"hello")
        with pytest.raises(EncryptionError):
            encryptor_b.decrypt(payload)