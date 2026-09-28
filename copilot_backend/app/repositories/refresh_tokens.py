"""`RefreshTokenRepository` — CRUD for the `refresh_tokens` collection.

The schema is fixed by ADR-0009 (short JWT + refresh-token rotation):

* `token_hash` — SHA-256 of the opaque token. Indexed unique. The raw
  token only exists in the user's cookie / local storage; the DB
  never sees it.
* `user_id` — pointer to `users._id`. Indexed.
* `family_id` — rotation-chain identifier (T07 / #8). Indexed.
* `expires_at` — absolute expiry (7 days after issuance per ADR-0009).
  Carries a TTL index so expired rows are purged by Mongo itself.
* `revoked_at` — `None` while active; stamped on rotation / logout /
  admin force-logout. A non-null `revoked_at` makes the token
  unusable even before `expires_at`.

T04 (#5) introduces the repository. T07 (#8) layers the rotation
flow on top: `revoke_family` powers reuse detection (one revoked
token presented again → revoke every token in the chain) and the
`create` call now accepts `family_id` so a brand-new login gets a
fresh chain.
"""
from __future__ import annotations

from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import PyMongoError

from app.db.errors import NotFoundError, ValidationError
from app.db.indexes import REFRESH_TOKENS
from app.db.schemas import RefreshTokenCreate, RefreshTokenInDB
from app.repositories.base import BaseRepository


class RefreshTokenRepository(BaseRepository[RefreshTokenInDB, RefreshTokenCreate, None]):
    """CRUD for the `refresh_tokens` collection."""

    collection_name: ClassVar[str] = REFRESH_TOKENS

    def __init__(self, database: AsyncIOMotorDatabase[Any]) -> None:
        super().__init__(database)

    @staticmethod
    def _doc_to_in_db(doc: dict[str, Any]) -> RefreshTokenInDB:
        return RefreshTokenInDB.model_validate(BaseRepository._coerce_id(doc))

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, data: RefreshTokenCreate) -> RefreshTokenInDB:
        """Insert a new refresh token. Returns the persisted row."""
        doc = data.model_dump()
        doc["created_at"] = self._now()
        doc.setdefault("revoked_at", None)
        result = await self._collection.insert_one(doc)
        stored = await self._collection.find_one({"_id": result.inserted_id})
        if stored is None:
            raise NotFoundError(message_en="Refresh token disappeared after insert")
        return self._doc_to_in_db(stored)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_by_hash(self, token_hash: str) -> RefreshTokenInDB:
        """Look up by `token_hash`. Raises `NotFoundError` if missing."""
        doc = await self._collection.find_one({"token_hash": token_hash})
        if doc is None:
            raise NotFoundError(
                message_en="Refresh token not found",
                details={"token_hash_prefix": token_hash[:8]},
            )
        return self._doc_to_in_db(doc)

    async def list_for_user(self, user_id: str) -> list[RefreshTokenInDB]:
        """All tokens for a user, newest first.

        Admin force-logout uses this to render "active sessions".
        """
        oid = self.to_object_id(user_id)
        cursor = self._collection.find({"user_id": str(oid)}).sort("created_at", -1)
        return [self._doc_to_in_db(doc) async for doc in cursor]

    # ------------------------------------------------------------------
    # Revoke
    # ------------------------------------------------------------------

    async def revoke(self, token_hash: str) -> RefreshTokenInDB:
        """Stamp `revoked_at` on the token, returning the updated row.

        Idempotent: revoking an already-revoked token leaves the
        original `revoked_at` in place (compare-and-set on the existing
        NULL).
        """
        now = self._now()
        # Only stamp if currently not revoked. If already revoked, the
        # document is unchanged and we still return its current state
        # below.
        await self._collection.update_one(
            {"token_hash": token_hash, "revoked_at": None},
            {"$set": {"revoked_at": now}},
        )
        doc = await self._collection.find_one({"token_hash": token_hash})
        if doc is None:
            raise NotFoundError(
                message_en="Refresh token not found",
                details={"token_hash_prefix": token_hash[:8]},
            )
        return self._doc_to_in_db(doc)

    async def revoke_all_for_user(self, user_id: str) -> int:
        """Force-logout: revoke every non-expired token for a user.

        Returns the number of rows affected. Used by the admin
        force-logout endpoint (ADR-0009) and by the T08 logout flow.
        """
        oid = self.to_object_id(user_id)
        result = await self._collection.update_many(
            {"user_id": str(oid), "revoked_at": None},
            {"$set": {"revoked_at": self._now()}},
        )
        return result.modified_count

    async def revoke_family(self, family_id: str) -> int:
        """Revoke every active token in a rotation chain.

        T07 (#8) reuse detection: when a token that has already been
        rotated (i.e. `revoked_at IS NOT NULL`) is presented again,
        treat it as a compromise and revoke every token sharing its
        `family_id`. Returns the number of rows flipped from active
        to revoked so callers can log "n tokens revoked".
        """
        if not family_id:
            raise ValidationError(
                message_en="family_id must be a non-empty string",
                details={"family_id": family_id},
            )
        result = await self._collection.update_many(
            {"family_id": family_id, "revoked_at": None},
            {"$set": {"revoked_at": self._now()}},
        )
        return result.modified_count

    async def list_family(self, family_id: str) -> list[RefreshTokenInDB]:
        """Return every token in a family, oldest first.

        Used by tests / admin tooling to render the rotation chain. Not
        a hot path; the indexed lookup is cheap but unbounded on a
        compromised family.
        """
        cursor = self._collection.find({"family_id": family_id}).sort("created_at", 1)
        return [self._doc_to_in_db(doc) async for doc in cursor]

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------

    async def claim_for_rotation(self, token_hash: str) -> bool:
        """Atomically mark a token as revoked iff it is currently active.

        Returns `True` when the call flipped the row from active to
        revoked — meaning the caller "won" the right to insert the
        successor token. Returns `False` when the row was already
        revoked (or expired+TTL-purged): the caller must treat this as
        reuse / compromise and follow up with `revoke_family`.

        Why a single conditional `update_one` instead of a fetch +
        check + non-conditional update: between the snapshot read and
        the unconditional write, a concurrent refresh request can
        rotate the same token. The filter `{token_hash, revoked_at:
        None}` makes the "claim" step mutually exclusive at the
        Mongo-driver level — only one of N concurrent callers matches
        the predicate; the rest get `matched_count == 0` and treat
        the situation as reuse.
        """
        now = self._now()
        result = await self._collection.update_one(
            {"token_hash": token_hash, "revoked_at": None},
            {"$set": {"revoked_at": now}},
        )
        return result.matched_count == 1

    async def set_replaced_by(self, old_id: str, new_id: str) -> None:
        """Best-effort chain audit update: stamp the new token's id on the old row.

        `replaced_by` is reconstruction metadata for audit / chain
        traversal only — losing a write does NOT invalidate the new
        token. We tolerate pymongo driver failures here so a transient
        broker hiccup doesn't take down the rotation flow. Programming
        errors (bad id string, etc.) still surface as exceptions
        because they signal a real bug.
        """
        try:
            await self._collection.update_one(
                {"_id": self.to_object_id(old_id)},
                {"$set": {"replaced_by": new_id}},
            )
        except PyMongoError:  # noqa: BLE001 — chain metadata is not auth-critical
            pass

    async def rotate(
        self,
        old_hash: str,
        new_token: RefreshTokenCreate,
    ) -> tuple[RefreshTokenInDB, RefreshTokenInDB]:
        """Revoke the old token and insert the new one.

        Two writes, but they happen sequentially. The repository keeps
        the API simple; a future transaction-wrapped variant can
        replace this without touching callers.

        ⚠️ This method is racy under concurrent `/auth/refresh` calls
        presenting the same raw token. The service layer should use
        `claim_for_rotation` for the rotate hot path; this convenience
        method remains for callers that don't need atomic claim
        semantics (e.g. maintenance scripts).
        """
        old = await self.revoke(old_hash)
        new = await self.create(new_token)
        # Record the rotation chain (best-effort: failure here does
        # not invalidate the new token). Only Mongo driver failures are
        # tolerated; programming errors (e.g. typos in field names)
        # must surface so they get fixed.
        try:
            await self._collection.update_one(
                {"_id": self.to_object_id(old.id)},
                {"$set": {"replaced_by": new.id}},
            )
        except PyMongoError:  # noqa: BLE001 — chain metadata is not auth-critical
            pass
        return old, new
