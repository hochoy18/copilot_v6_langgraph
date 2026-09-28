"""`UserRepository` — CRUD for the `users` collection.

T04 (#5) introduces this class; T07 (OIDC login) and T08 (auth flow)
are the first callers. The repository exposes:

* `create(input: UserCreate) -> User` — insert + return canonical shape.
* `get(user_id) -> User` — primary-key lookup. Raises `NotFoundError`.
* `get_by_email`, `get_by_sso_subject`, `get_by_local_username` —
  secondary lookups backed by the indexes in `app.db.indexes`.
* `list(...)` — paginated reads (cursor by `_id`).
* `update(user_id, patch) -> User` — partial update, bumps `updated_at`.
* `delete(user_id) -> None` — hard delete; soft-delete is a domain
  concern that future tickets will layer on top.
* `set_role_ids(user_id, role_ids) -> User` — atomic replace for the
  role list, used by the admin role-grants endpoint (ADR-0031).

Security notes
--------------

* The persisted row carries `password_hash`; the **canonical read**
  shape drops it via `User.from_db`. Every method that returns a
  `User` runs through this filter so a forgotten conversion can't
  surface a hash through an API response.
* `get_by_email`, `get_by_sso_subject`, etc. intentionally return the
  canonical `User` (no hash). Login / refresh code that needs to
  compare hashes should use the `get_in_db_*` variants below — they
  are explicit about returning the persisted row.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError

from app.db.errors import NotFoundError, ValidationError
from app.db.indexes import USERS
from app.db.schemas import User, UserCreate, UserInDB, UserUpdate
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User, UserCreate, UserUpdate]):
    """CRUD for the `users` collection.

    Constructed against a `motor` database handle — see `app.main` for
    the FastAPI dependency wiring.
    """

    collection_name: ClassVar[str] = USERS

    def __init__(self, database: AsyncIOMotorDatabase[Any]) -> None:
        super().__init__(database)

    # ------------------------------------------------------------------
    # Internal — Mongo doc → read shape.
    # ------------------------------------------------------------------

    @staticmethod
    def _doc_to_in_db(doc: dict[str, Any]) -> UserInDB:
        """Parse a Mongo document into the persisted-shape Pydantic model."""
        return UserInDB.model_validate(BaseRepository._coerce_id(doc))

    @staticmethod
    def _doc_to_read(doc: dict[str, Any]) -> User:
        """Convert a Mongo document to the canonical read shape (no `password_hash`)."""
        return User.from_db(UserRepository._doc_to_in_db(doc))

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, data: UserCreate) -> User:
        """Insert a new user and return the canonical read shape.

        Validates source-specific invariants (sso_subject ↔ local_*
        exclusivity) before hitting the DB so we don't rely on the
        unique-sparse indexes to enforce them.

        Raises:
            ValidationError: source-specific fields are inconsistent.
            DuplicateKeyError: an email / sso_subject / local_username
                already exists (translated from pymongo's error).
        """
        if data.source == "sso":
            if not data.sso_subject:
                raise ValidationError(
                    message_en="sso users require `sso_subject`",
                    details={"source": data.source},
                )
            if data.password_hash is not None or data.local_username is not None:
                raise ValidationError(
                    message_en="sso users must not carry local credentials",
                    details={"source": data.source},
                )
        elif data.source == "local":
            if not data.local_username or not data.password_hash:
                raise ValidationError(
                    message_en="local users require `local_username` and `password_hash`",
                    details={"source": data.source},
                )
            if data.sso_subject is not None:
                raise ValidationError(
                    message_en="local users must not carry `sso_subject`",
                    details={"source": data.source},
                )

        now = self._now()
        doc = data.model_dump()
        doc["created_at"] = now
        doc["updated_at"] = now
        try:
            result = await self._collection.insert_one(doc)
        except PyMongoDuplicateKeyError as exc:
            raise self._translate_duplicate(exc) from exc

        created = await self._collection.find_one({"_id": result.inserted_id})
        if created is None:
            # Race / replication oddity: we just inserted, the row
            # disappeared. Surface as a 404 rather than returning None
            # silently.
            raise NotFoundError(message_en="User disappeared after insert")
        return self._doc_to_read(created)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, user_id: str) -> User:
        """Look up by primary key. Raises `NotFoundError` if missing."""
        oid = self.to_object_id(user_id)
        doc = await self._collection.find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                message_en=f"User {user_id} not found",
                details={"user_id": user_id},
            )
        return self._doc_to_read(doc)

    async def get_in_db(self, user_id: str) -> UserInDB:
        """Look up by primary key, returning the persisted-shape row.

        Auth flows that need `password_hash` for verification use this;
        API responses must use `get`.
        """
        oid = self.to_object_id(user_id)
        doc = await self._collection.find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                message_en=f"User {user_id} not found",
                details={"user_id": user_id},
            )
        return self._doc_to_in_db(doc)

    async def get_by_email(self, email: str) -> User:
        """Email lookup. Raises `NotFoundError` if no match."""
        doc = await self._collection.find_one({"email": email})
        if doc is None:
            raise NotFoundError(
                message_en=f"User with email {email!r} not found",
                details={"email": email},
            )
        return self._doc_to_read(doc)

    async def get_by_email_in_db(self, email: str) -> UserInDB:
        """Email lookup returning the persisted shape (incl. `password_hash`)."""
        doc = await self._collection.find_one({"email": email})
        if doc is None:
            raise NotFoundError(
                message_en=f"User with email {email!r} not found",
                details={"email": email},
            )
        return self._doc_to_in_db(doc)

    async def get_by_sso_subject(self, sso_subject: str) -> User:
        """Lookup by SSO `sub` claim. Raises `NotFoundError` if no match."""
        doc = await self._collection.find_one({"sso_subject": sso_subject})
        if doc is None:
            raise NotFoundError(
                message_en=f"SSO user with subject {sso_subject!r} not found",
                details={"sso_subject": sso_subject},
            )
        return self._doc_to_read(doc)

    async def get_by_local_username(self, local_username: str) -> User:
        """Lookup by local admin username. Raises `NotFoundError` if no match."""
        doc = await self._collection.find_one({"local_username": local_username})
        if doc is None:
            raise NotFoundError(
                message_en=f"Local user {local_username!r} not found",
                details={"local_username": local_username},
            )
        return self._doc_to_read(doc)

    async def get_by_local_username_in_db(self, local_username: str) -> UserInDB:
        """Lookup by local admin username, returning the persisted shape.

        The local-login path (T09 #10) needs `password_hash` for
        bcrypt verification. Returning the persisted row lets the
        service strip the hash with `User.from_db(...)` only after the
        password is confirmed — no other code path ever sees the
        hash.
        """
        doc = await self._collection.find_one({"local_username": local_username})
        if doc is None:
            raise NotFoundError(
                message_en=f"Local user {local_username!r} not found",
                details={"local_username": local_username},
            )
        return self._doc_to_in_db(doc)

    async def list_users(
        self,
        *,
        limit: int = 50,
        after_id: str | None = None,
    ) -> list[User]:
        """Paginated read by ascending `_id`.

        `after_id` is the cursor — pass the last id from the previous
        page to fetch the next page. SPEC chose cursor pagination
        (ADR-0031) so the page size is stable under inserts.
        """
        if limit < 1 or limit > 500:
            raise ValidationError(
                message_en=f"limit must be in [1, 500], got {limit}",
                details={"limit": limit},
            )
        query: dict[str, Any] = {}
        if after_id is not None:
            query["_id"] = {"$gt": self.to_object_id(after_id)}
        cursor = self._collection.find(query).sort("_id", ASCENDING).limit(limit)
        return [self._doc_to_read(doc) async for doc in cursor]

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(self, user_id: str, patch: UserUpdate) -> User:
        """Apply a partial update, bumping `updated_at`. Returns the new read shape.

        `role_ids` is replaced atomically (no merge). The `updated_at`
        stamp is unconditional — even an empty patch produces a fresh
        timestamp, which is what admin tooling expects.
        """
        oid = self.to_object_id(user_id)
        update_doc = patch.model_dump(exclude_none=True)
        update_doc["updated_at"] = self._now()
        try:
            result = await self._collection.find_one_and_update(
                {"_id": oid},
                {"$set": update_doc},
                return_document=True,  # = RETURN_DOCUMENT_AFTER (pymongo 4.x default)
            )
        except PyMongoDuplicateKeyError as exc:
            raise self._translate_duplicate(exc) from exc

        if result is None:
            raise NotFoundError(
                message_en=f"User {user_id} not found",
                details={"user_id": user_id},
            )
        return self._doc_to_read(result)

    async def set_role_ids(self, user_id: str, role_ids: list[str]) -> User:
        """Atomically replace the user's role grants.

        Used by `PUT /api/v1/admin/users/{id}/roles` (ADR-0031). The
        atomicity comes from Mongo's per-document guarantee: a partial
        write can't leave the role list in a torn state.
        """
        oid = self.to_object_id(user_id)
        now = self._now()
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": {"role_ids": list(role_ids), "updated_at": now}},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"User {user_id} not found",
                details={"user_id": user_id},
            )
        return self._doc_to_read(result)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, user_id: str) -> None:
        """Hard-delete the user. Raises `NotFoundError` if missing.

        Soft-delete / archive semantics live in the admin layer; the
        repository only knows about the raw delete operation.
        """
        oid = self.to_object_id(user_id)
        result = await self._collection.delete_one({"_id": oid})
        if result.deleted_count == 0:
            raise NotFoundError(
                message_en=f"User {user_id} not found",
                details={"user_id": user_id},
            )

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    async def count(self, *, is_active: bool | None = None) -> int:
        """Total users, optionally filtered by `is_active`.

        Admin dashboard hits this; the `by_is_active` index covers the
        common case.
        """
        query: dict[str, Any] = {}
        if is_active is not None:
            query["is_active"] = is_active
        return await self._collection.count_documents(query)

    async def last_updated(self, user_id: str) -> datetime | None:
        """Return the most recent `updated_at` for a user, or `None`.

        Useful for caches that want to skip re-fetching a user whose
        `updated_at` hasn't moved.
        """
        oid = self.to_object_id(user_id)
        doc: dict[str, Any] | None = await self._collection.find_one(
            {"_id": oid}, projection={"updated_at": 1, "_id": 0}
        )
        if doc is None:
            return None
        updated: Any = doc.get("updated_at")
        return updated if isinstance(updated, datetime) else None

    # ------------------------------------------------------------------
    # SSO lookup helpers (T08 / #46)
    # ------------------------------------------------------------------
    #
    # The repository intentionally does NOT bundle the
    # "find-or-create + mirror identity" orchestration: per the
    # module-level docstring the repo is a thin Mongo wrapper and
    # business logic lives in services. The login service composes
    # `get_by_sso_subject` + `update` itself so the audit-trail
    # decision ("should we PATCH? what changed?") sits next to
    # the code that knows about IdP claims.
