"""Tests for `CredentialRepository` (T05 / #6).

Acceptance criterion: "Credential 加密写入".

The seam is the public API of `CredentialRepository`. We exercise every
method against an in-memory `mongomock_motor` database and verify:

* Inserted rows carry ciphertext (never plaintext), a fresh nonce, and
  the encryptor's `key_id`.
* The canonical read shape (`Credential`) does NOT carry the
  encrypted bytes — only metadata.
* `decrypt_payload` round-trips a payload sealed by the same repository.
* `rotate_payload` re-seals and stamps `last_rotated_at`.
* Plaintext bytes never appear in the persisted Mongo doc.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.db.errors import DuplicateKeyError, NotFoundError
from app.db.init_db import init_database
from app.db.schemas import CredentialCreate, CredentialUpdate
from app.repositories.credentials import CredentialRepository
from app.security.crypto import AesGcmEncryptor, EncryptionError, MasterKey


@pytest.fixture
def encryptor() -> AesGcmEncryptor:
    """A fresh `AesGcmEncryptor` per test.

    Tests get their own encryptor so a key leakage in one test cannot
    decrypt another's ciphertext.
    """
    return AesGcmEncryptor(MasterKey(key_bytes=os.urandom(32), key_id="test"))


@pytest.fixture
async def repo(encryptor: AesGcmEncryptor) -> CredentialRepository:
    """A fresh `CredentialRepository` against an isolated in-memory DB."""
    db = AsyncMongoMockClient()["copilot_credential_test"]
    await init_database(db)
    return CredentialRepository(db, encryptor)


def _cred_input(**overrides: object) -> CredentialCreate:
    """A valid Credential input, with per-test overrides applied."""
    base: dict[str, object] = {
        "name": "salesforce-prod",
        "auth_type": "api_key",
        "plaintext_payload": {
            "api_key": "sk-test-1234567890",
            "api_secret": "shhh-this-is-secret",
        },
    }
    base.update(overrides)
    return CredentialCreate(**base)  # type: ignore[arg-type]


class TestCredentialCreate:
    """`create` — encryption on insert + canonical read shape."""

    @pytest.mark.asyncio
    async def test_create_encrypts_payload_before_persisting(
        self, repo: CredentialRepository
    ) -> None:
        """The persisted Mongo doc carries ciphertext, not plaintext.

        This is the literal acceptance criterion: "Credential 加密写入".
        The plaintext API key and secret must NOT appear anywhere on the
        row — neither as a top-level field nor nested.
        """
        await repo.create(_cred_input())

        doc = await repo._collection.find_one({})
        assert doc is not None
        # Top-level fields land as configured.
        assert doc["name"] == "salesforce-prod"
        assert doc["auth_type"] == "api_key"
        assert doc["key_id"] == "test"
        # The ciphertext field carries bytes, not the plaintext bytes
        # (encrypted output differs from plaintext output every time
        # because of the random nonce).
        assert isinstance(doc["payload"], bytes)
        assert isinstance(doc["nonce"], bytes)
        assert len(doc["nonce"]) == 12
        # Plaintext never appears anywhere in the persisted doc.
        assert b"sk-test-1234567890" not in doc["payload"]
        assert b"shhh-this-is-secret" not in doc["payload"]
        # And nowhere else in the doc as a string either.
        for value in doc.values():
                if isinstance(value, str):
                    assert "sk-test-1234567890" not in value
                    assert "shhh-this-is-secret" not in value

    @pytest.mark.asyncio
    async def test_create_returns_canonical_shape_without_payload(
        self, repo: CredentialRepository
    ) -> None:
        """Canonical `Credential` strips payload bytes + nonce.

        The canonical read shape is what API responses return; if a
        future endpoint accidentally surfaces `payload`, a downstream
        client could cache ciphertext bytes. Verify it's not there.
        """
        created = await repo.create(_cred_input())
        dumped = created.model_dump()
        assert "payload" not in dumped
        assert "nonce" not in dumped
        # But the metadata IS there.
        assert created.id
        assert created.name == "salesforce-prod"
        assert created.auth_type == "api_key"
        assert created.last_rotated_at is None  # initial seal is not a rotation

    @pytest.mark.asyncio
    async def test_create_two_rows_get_distinct_nonces(
        self, repo: CredentialRepository
    ) -> None:
        """Two inserts of the same plaintext produce different ciphertexts.

        GCE nonce reuse is catastrophic — the encryptor generates a
        fresh 12-byte nonce per call.
        """
        await repo.create(_cred_input(name="a"))
        await repo.create(_cred_input(name="b"))
        docs = [doc async for doc in repo._collection.find({})]
        assert len(docs) == 2
        nonces = {doc["nonce"] for doc in docs}
        ciphertexts = {doc["payload"] for doc in docs}
        assert len(nonces) == 2
        assert len(ciphertexts) == 2

    @pytest.mark.asyncio
    async def test_create_accepts_bytes_payload(
        self, repo: CredentialRepository
    ) -> None:
        """Bytes payload (e.g. mTLS PEM) passes through verbatim."""
        pem_bytes = b"-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----"
        created = await repo.create(
            _cred_input(name="mtls-prod", auth_type="mtls", plaintext_payload=pem_bytes)
        )
        decrypted = await repo.decrypt_payload(created.id)
        assert decrypted == pem_bytes

    @pytest.mark.asyncio
    async def test_create_rejects_empty_payload(
        self, repo: CredentialRepository
    ) -> None:
        """Empty bytes / empty dict raise before hitting the database."""
        with pytest.raises(ValueError):
            await repo.create(_cred_input(plaintext_payload=b""))
        with pytest.raises(ValueError):
            await repo.create(_cred_input(plaintext_payload={}))

    @pytest.mark.asyncio
    async def test_create_rejects_wrong_payload_type(
        self, repo: CredentialRepository
    ) -> None:
        """`int` payloads surface as `pydantic.ValidationError`.

        Pydantic accepts `dict | bytes` and coerces strings to bytes
        (which is fine — bytes are a valid plaintext form). Anything
        that doesn't coerce to either form (e.g. an `int`) raises
        `ValidationError` before the repository code runs.
        `_serialise_plaintext` adds a second guard for callers that
        bypass Pydantic by supplying a non-bytes non-dict at runtime.
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            await repo.create(_cred_input(plaintext_payload=42))

    @pytest.mark.asyncio
    async def test_duplicate_name_raises_duplicate_key_error(
        self, repo: CredentialRepository
    ) -> None:
        """A second credential with the same `name` collides on `uniq_name`."""
        await repo.create(_cred_input())
        with pytest.raises(DuplicateKeyError) as exc:
            await repo.create(_cred_input(name="salesforce-prod"))
        assert exc.value.code == "duplicate_key"


class TestCredentialRead:
    """`get`, `get_in_db`, `get_by_name`, list helpers."""

    @pytest.mark.asyncio
    async def test_get_returns_canonical_shape(
        self, repo: CredentialRepository
    ) -> None:
        created = await repo.create(_cred_input())
        fetched = await repo.get(created.id)
        assert fetched.id == created.id
        assert fetched.name == created.name
        # No bytes on the canonical shape.
        assert "payload" not in fetched.model_dump()

    @pytest.mark.asyncio
    async def test_get_in_db_carries_payload_bytes(
        self, repo: CredentialRepository
    ) -> None:
        """`get_in_db` returns the persisted row with bytes intact."""
        created = await repo.create(_cred_input())
        in_db = await repo.get_in_db(created.id)
        assert in_db.payload
        assert in_db.nonce
        assert in_db.key_id == "test"
        assert isinstance(in_db.payload, bytes)
        assert isinstance(in_db.nonce, bytes)

    @pytest.mark.asyncio
    async def test_get_missing_raises_not_found(
        self, repo: CredentialRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.get(str(ObjectId()))

    @pytest.mark.asyncio
    async def test_get_by_name(self, repo: CredentialRepository) -> None:
        await repo.create(_cred_input(name="find_me"))
        fetched = await repo.get_by_name("find_me")
        assert fetched.name == "find_me"

    @pytest.mark.asyncio
    async def test_list_all(self, repo: CredentialRepository) -> None:
        await repo.create(_cred_input(name="a"))
        await repo.create(_cred_input(name="b"))
        names = {c.name for c in await repo.list_all()}
        assert names == {"a", "b"}

    @pytest.mark.asyncio
    async def test_list_by_key_id(self, repo: CredentialRepository) -> None:
        """Multi-key rotation drill-down."""
        await repo.create(_cred_input(name="a"))
        await repo.create(_cred_input(name="b"))
        matches = await repo.list_by_key_id("test")
        assert {c.name for c in matches} == {"a", "b"}
        matches_other = await repo.list_by_key_id("nonexistent")
        assert matches_other == []


class TestCredentialDecrypt:
    """`decrypt_payload` — Worker-only seam, round-trips + failure modes."""

    @pytest.mark.asyncio
    async def test_round_trip_via_repository(self, repo: CredentialRepository) -> None:
        """`decrypt_payload` returns the original plaintext for a sealed row."""
        plaintext = {"api_key": "hello", "api_secret": "world"}
        created = await repo.create(_cred_input(plaintext_payload=plaintext))
        decrypted = await repo.decrypt_payload(created.id)
        # The repository coerces dicts to JSON; decode back to compare.
        assert json.loads(decrypted) == plaintext

    @pytest.mark.asyncio
    async def test_decrypt_missing_raises_not_found(
        self, repo: CredentialRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.decrypt_payload(str(ObjectId()))


class TestCredentialUpdateAndRotate:
    """`update` (metadata only) and `rotate_payload` (re-seal)."""

    @pytest.mark.asyncio
    async def test_update_renames_and_bumps_updated_at(
        self, repo: CredentialRepository
    ) -> None:
        created = await repo.create(_cred_input())
        before = created.updated_at
        await asyncio.sleep(0.005)
        updated = await repo.update(
            created.id, CredentialUpdate(name="salesforce-prod-v2")
        )
        assert updated.name == "salesforce-prod-v2"
        assert updated.updated_at > before

    @pytest.mark.asyncio
    async def test_rotate_payload_re_seals_and_stamps_last_rotated_at(
        self, repo: CredentialRepository
    ) -> None:
        """`rotate_payload` writes new ciphertext + stamps rotation time.

        Initial seal leaves `last_rotated_at` as `None`; the first
        rotation populates it. Subsequent rotations bump it forward.
        """
        old_payload = {"api_key": "old-key"}
        new_payload = {"api_key": "new-key"}
        created = await repo.create(_cred_input(plaintext_payload=old_payload))
        assert created.last_rotated_at is None  # confirm pre-rotation state
        # Capture old ciphertext bytes.
        old_row = await repo.get_in_db(created.id)
        old_ciphertext = old_row.payload

        await asyncio.sleep(0.005)
        rotated = await repo.rotate_payload(created.id, new_payload)
        assert rotated.last_rotated_at is not None
        # First rotation: previously None, now populated.
        assert rotated.last_rotated_at > created.updated_at - timedelta(seconds=1)

        # Verify the row was actually re-sealed: ciphertext bytes differ
        # (new nonce was used).
        new_row = await repo.get_in_db(created.id)
        assert new_row.payload != old_ciphertext
        # And decryption returns the new plaintext, not the old.
        decrypted = await repo.decrypt_payload(created.id)
        assert json.loads(decrypted) == new_payload

    @pytest.mark.asyncio
    async def test_rotate_missing_raises_not_found(
        self, repo: CredentialRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.rotate_payload(str(ObjectId()), {"k": "v"})


class TestCredentialDelete:
    """`delete` — with pre-check that no Tools reference this credential."""

    @pytest.mark.asyncio
    async def test_delete_removes_credential(self, repo: CredentialRepository) -> None:
        created = await repo.create(_cred_input())
        await repo.delete(created.id)
        with pytest.raises(NotFoundError):
            await repo.get(created.id)

    @pytest.mark.asyncio
    async def test_delete_missing_raises_not_found(
        self, repo: CredentialRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.delete(str(ObjectId()))


class TestCredentialLastRotatedAt:
    """`last_rotated_at` helper for the admin UI."""

    @pytest.mark.asyncio
    async def test_returns_none_before_first_rotation(
        self, repo: CredentialRepository
    ) -> None:
        """Initial seal leaves `last_rotated_at` unset until the first rotation."""
        created = await repo.create(_cred_input())
        assert created.last_rotated_at is None
        rotated = await repo.last_rotated_at(created.id)
        assert rotated is None

    @pytest.mark.asyncio
    async def test_returns_stamp_after_rotation(self, repo: CredentialRepository) -> None:
        """After `rotate_payload`, the helper returns the rotation timestamp."""
        created = await repo.create(_cred_input())
        # Pre-rotation: the helper agrees with the model — both None.
        assert await repo.last_rotated_at(created.id) is None
        rotated = await repo.rotate_payload(created.id, {"api_key": "new"})
        assert isinstance(rotated.last_rotated_at, datetime)
        # Post-rotation: the helper matches the freshly stamped timestamp.
        helper_value = await repo.last_rotated_at(created.id)
        assert helper_value == rotated.last_rotated_at

    @pytest.mark.asyncio
    async def test_returns_none_for_missing(self, repo: CredentialRepository) -> None:
        assert await repo.last_rotated_at(str(ObjectId())) is None


class TestCredentialTamperResistance:
    """Direct ciphertext tampering must not leak plaintext.

    This is the AEAD guarantee (AES-256-GCM authenticates). We flip a
    byte on the persisted row and confirm `decrypt_payload` raises
    `EncryptionError` rather than returning bytes.
    """

    @pytest.mark.asyncio
    async def test_tampered_ciphertext_raises_encryption_error(
        self, repo: CredentialRepository
    ) -> None:
        created = await repo.create(_cred_input())
        # Flip one byte in the persisted ciphertext.
        doc = await repo._collection.find_one({"_id": ObjectId(created.id)})
        assert doc is not None
        tampered = bytearray(doc["payload"])
        tampered[-1] ^= 1
        await repo._collection.update_one(
            {"_id": ObjectId(created.id)},
            {"$set": {"payload": bytes(tampered)}},
        )
        with pytest.raises(EncryptionError):
            await repo.decrypt_payload(created.id)

    @pytest.mark.asyncio
    async def test_wrong_encryptor_key_raises_encryption_error(
        self, repo: CredentialRepository
    ) -> None:
        """An encryptor built from a different key cannot decrypt this row."""
        created = await repo.create(_cred_input())
        other = AesGcmEncryptor(MasterKey(key_bytes=os.urandom(32), key_id="other"))
        # Build a throwaway repo against the same DB but the new key.
        other_repo = CredentialRepository(repo._db, other)
        with pytest.raises(EncryptionError):
            await other_repo.decrypt_payload(created.id)