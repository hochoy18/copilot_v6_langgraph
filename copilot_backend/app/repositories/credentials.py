"""`CredentialRepository` — CRUD for the `credentials` collection.

Per ADR-0002 the credential bytes must stay encrypted at rest. The
repository is the seam where that promise is enforced: every write
goes through a `CredentialEncryptor`, every read either returns the
canonical `Credential` (bytes stripped) or the persisted `CredentialInDB`
(bytes intact) for use by the Worker at call time.

Design notes:

* `create` seals the caller-supplied plaintext before insert. The
  encryptor's `key_id` is stamped on the row so a future multi-key
  registry can route decryption to the right master key.
* `rotate_payload` re-seals with the same encryptor and stamps
  `last_rotated_at` — the operation that triggers the 401/403 → admin
  notification flow in ADR-0024.
* `decrypt_payload` is the dedicated opener used by the Tool Worker.
  It returns the original plaintext bytes and raises `EncryptionError`
  on any cryptographic failure. Callers must NEVER log the result.
* `delete` is a hard delete; the FK lookup in `ToolRepository.list_by_credential`
  lets the admin UI refuse to delete a credential that's still in use.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError

from app.db.errors import NotFoundError
from app.db.indexes import CREDENTIALS
from app.db.schemas import Credential, CredentialCreate, CredentialInDB, CredentialUpdate
from app.repositories.base import BaseRepository
from app.security.crypto import CredentialEncryptor, EncryptedPayload, EncryptionError


class CredentialRepository(BaseRepository[Credential, CredentialCreate, CredentialUpdate]):
    """CRUD for the `credentials` collection.

    Constructed against a `motor` database handle AND a
    `CredentialEncryptor` — the encryption layer is a hard dependency,
    not optional, because there is no use case for storing unencrypted
    credential bytes at rest (ADR-0002).
    """

    collection_name: ClassVar[str] = CREDENTIALS

    def __init__(
        self,
        database: AsyncIOMotorDatabase[Any],
        encryptor: CredentialEncryptor,
    ) -> None:
        super().__init__(database)
        self._encryptor = encryptor

    # ------------------------------------------------------------------
    # Internal — Mongo doc → read shape.
    # ------------------------------------------------------------------

    @staticmethod
    def _doc_to_read(doc: dict[str, Any]) -> Credential:
        return Credential.from_db(CredentialRepository._doc_to_in_db(doc))

    @staticmethod
    def _doc_to_in_db(doc: dict[str, Any]) -> CredentialInDB:
        return CredentialInDB.model_validate(BaseRepository._coerce_id(doc))

    @staticmethod
    def _serialise_plaintext(payload: dict[str, Any] | bytes) -> bytes:
        """Coerce the `plaintext_payload` input into bytes for encryption.

        Dicts are JSON-encoded with stable key order so the same input
        always seals to the same bytes (helpful for tests that compare
        ciphertexts). Bytes pass through.
        """
        if isinstance(payload, bytes):
            if not payload:
                raise ValueError("plaintext_payload must be non-empty")
            return payload
        if isinstance(payload, dict):
            if not payload:
                raise ValueError("plaintext_payload dict must be non-empty")
            return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        raise TypeError(
            f"plaintext_payload must be dict or bytes, got {type(payload).__name__}"
        )

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, data: CredentialCreate) -> Credential:
        """Encrypt `plaintext_payload`, insert, return the canonical shape.

        `last_rotated_at` is intentionally left `None` on initial seal —
        it tracks the most recent rotation (ADR-0024), not creation.
        Conflating the two would let a fresh row trip rotation-staleness
        alerts the moment it lands. `rotate_payload` is the writer.

        Raises:
            DuplicateKeyError: a Credential with the same `name` exists.
            ValueError: `plaintext_payload` is empty.
            TypeError: `plaintext_payload` is neither dict nor bytes.
        """
        plaintext = self._serialise_plaintext(data.plaintext_payload)
        sealed = self._encryptor.encrypt(plaintext)
        now = self._now()
        doc: dict[str, Any] = {
            "name": data.name,
            "auth_type": data.auth_type,
            "payload": sealed.ciphertext,
            "nonce": sealed.nonce,
            "key_id": sealed.key_id,
            "created_at": now,
            "updated_at": now,
            "last_rotated_at": None,
        }
        try:
            await self._collection.insert_one(doc)
        except PyMongoDuplicateKeyError as exc:
            raise self._translate_duplicate(exc) from exc
        # Re-fetch so the canonical read shape reflects the persisted
        # row exactly (mirrors `UserRepository.create`).
        stored = await self._collection.find_one({"_id": doc["_id"]})
        if stored is None:
            raise NotFoundError(message_en="Credential disappeared after insert")
        return self._doc_to_read(stored)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, credential_id: str) -> Credential:
        """Canonical read. Bytes stripped — API surfaces only metadata."""
        oid = self.to_object_id(credential_id)
        doc = await self._collection.find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                message_en=f"Credential {credential_id} not found",
                details={"credential_id": credential_id},
            )
        return self._doc_to_read(doc)

    async def get_in_db(self, credential_id: str) -> CredentialInDB:
        """Persisted-shape read. Carries `payload` / `nonce`.

        Worker-only. Returning the row through this method documents
        that the bytes are leaving the repository layer.
        """
        oid = self.to_object_id(credential_id)
        doc = await self._collection.find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                message_en=f"Credential {credential_id} not found",
                details={"credential_id": credential_id},
            )
        return self._doc_to_in_db(doc)

    async def get_by_name(self, name: str) -> Credential:
        """Admin lookup by human label."""
        doc = await self._collection.find_one({"name": name})
        if doc is None:
            raise NotFoundError(
                message_en=f"Credential {name!r} not found",
                details={"name": name},
            )
        return self._doc_to_read(doc)

    async def list_all(self) -> list[Credential]:
        """Every Credential, sorted by name. Admin Registry view."""
        cursor = self._collection.find({}).sort("name", 1)
        return [self._doc_to_read(doc) async for doc in cursor]

    async def list_by_key_id(self, key_id: str) -> list[Credential]:
        """Every Credential sealed under a given `key_id`.

        Supports the multi-key rotation flow: enumerate rows that need
        re-encryption when a master key is being deprecated.
        """
        cursor = self._collection.find({"key_id": key_id}).sort("name", 1)
        return [self._doc_to_read(doc) async for doc in cursor]

    # ------------------------------------------------------------------
    # Decrypt (Worker-only seam)
    # ------------------------------------------------------------------

    async def decrypt_payload(self, credential_id: str) -> bytes:
        """Open the encrypted bytes for a Credential.

        Worker-only seam per ADR-0002: returns the plaintext payload
        bytes. Callers must inject the result into the outgoing request
        and then drop it — never log, never persist, never return to
        the front end.

        Raises:
            NotFoundError: no row with this id.
            EncryptionError: the row exists but cannot be decrypted
                (wrong key, tampered ciphertext, malformed nonce).
        """
        row = await self.get_in_db(credential_id)
        payload = EncryptedPayload(nonce=row.nonce, ciphertext=row.payload, key_id=row.key_id)
        return self._encryptor.decrypt(payload)

    # ------------------------------------------------------------------
    # Update — metadata only. Rotation is `rotate_payload` below.
    # ------------------------------------------------------------------

    async def update(self, credential_id: str, patch: CredentialUpdate) -> Credential:
        """Update metadata (`name` / `auth_type`). Bytes are untouched."""
        oid = self.to_object_id(credential_id)
        update_doc = patch.model_dump(exclude_none=True)
        update_doc["updated_at"] = self._now()
        try:
            result = await self._collection.find_one_and_update(
                {"_id": oid},
                {"$set": update_doc},
                return_document=True,
            )
        except PyMongoDuplicateKeyError as exc:
            raise self._translate_duplicate(exc) from exc
        if result is None:
            raise NotFoundError(
                message_en=f"Credential {credential_id} not found",
                details={"credential_id": credential_id},
            )
        return self._doc_to_read(result)

    async def rotate_payload(
        self,
        credential_id: str,
        plaintext_payload: dict[str, Any] | bytes,
    ) -> Credential:
        """Re-seal the credential bytes, stamping `last_rotated_at`.

        Triggered by:
          * admin rotation via the UI (ADR-0024 → 401/403 case);
          * a scheduled job once the upstream API confirms the new
            secret is in service (T35 / T38).

        The encryptor instance is unchanged — rotation here means
        replacing the *sealed bytes*, not the master key. Master-key
        rotation lives one layer up.
        """
        plaintext = self._serialise_plaintext(plaintext_payload)
        sealed = self._encryptor.encrypt(plaintext)
        now = self._now()
        oid = self.to_object_id(credential_id)
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {
                "$set": {
                    "payload": sealed.ciphertext,
                    "nonce": sealed.nonce,
                    "key_id": sealed.key_id,
                    "updated_at": now,
                    "last_rotated_at": now,
                }
            },
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"Credential {credential_id} not found",
                details={"credential_id": credential_id},
            )
        return self._doc_to_read(result)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, credential_id: str) -> None:
        """Hard-delete the Credential.

        Callers should pre-check `ToolRepository.list_by_credential(credential_id)`
        to avoid orphaning Tools that reference this row.
        """
        oid = self.to_object_id(credential_id)
        result = await self._collection.delete_one({"_id": oid})
        if result.deleted_count == 0:
            raise NotFoundError(
                message_en=f"Credential {credential_id} not found",
                details={"credential_id": credential_id},
            )

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    async def last_rotated_at(self, credential_id: str) -> datetime | None:
        """Most recent rotation timestamp, or `None` if missing."""
        oid = self.to_object_id(credential_id)
        doc: dict[str, Any] | None = await self._collection.find_one(
            {"_id": oid}, projection={"last_rotated_at": 1, "_id": 0}
        )
        if doc is None:
            return None
        rotated: Any = doc.get("last_rotated_at")
        return rotated if isinstance(rotated, datetime) else None


__all__ = ["CredentialRepository", "EncryptionError"]