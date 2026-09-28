"""`RoleRepository` — CRUD for the `roles` collection.

T04 (#5) ships the schema; full role-management (admin endpoints per
ADR-0031) lands with the admin ticket. This class is intentionally
minimal — enough to seed the catalog and let admin tooling exercise
lookups, no more.
"""
from __future__ import annotations

from typing import Any, ClassVar

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError

from app.db.errors import InvalidIdError, NotFoundError
from app.db.indexes import ROLES
from app.db.schemas import Role, RoleCreate, RoleInDB, RoleUpdate
from app.repositories.base import BaseRepository


class RoleRepository(BaseRepository[Role, RoleCreate, RoleUpdate]):
    """CRUD for the `roles` collection."""

    collection_name: ClassVar[str] = ROLES

    def __init__(self, database: AsyncIOMotorDatabase[Any]) -> None:
        super().__init__(database)

    @staticmethod
    def _doc_to_read(doc: dict[str, Any]) -> Role:
        return Role.model_validate(BaseRepository._coerce_id(doc))

    async def get_by_name(self, name: str) -> Role:
        """Look up by role slug. Raises `NotFoundError` if missing."""
        doc = await self._collection.find_one({"name": name})
        if doc is None:
            raise NotFoundError(
                message_en=f"Role {name!r} not found",
                details={"name": name},
            )
        return self._doc_to_read(doc)

    async def get_by_name_in_db(self, name: str) -> RoleInDB:
        doc = await self._collection.find_one({"name": name})
        if doc is None:
            raise NotFoundError(
                message_en=f"Role {name!r} not found",
                details={"name": name},
            )
        return RoleInDB.model_validate(BaseRepository._coerce_id(doc))

    async def create(self, data: RoleCreate) -> Role:
        now = self._now()
        doc = data.model_dump()
        doc["created_at"] = now
        doc["updated_at"] = now
        try:
            await self._collection.insert_one(doc)
        except PyMongoDuplicateKeyError as exc:
            raise self._translate_duplicate(exc) from exc
        return self._doc_to_read(doc)

    async def list_all(self) -> list[Role]:
        cursor = self._collection.find({}).sort("name", 1)
        return [self._doc_to_read(doc) async for doc in cursor]

    async def list_by_ids(self, role_ids: list[str]) -> list[Role]:
        """Look up a batch of Roles by `_id`, returning whatever resolved.

        Admin authorisation (`app.security.admin.require_admin_user`)
        needs to ask "does this user hold the `admin` role?" without
        paying N round-trips. `list_by_ids` runs a single `$in` query
        against the indexed `_id` field and skips ids that fail the
        ObjectId coercion — a corrupt id is treated as "no such role"
        rather than raising, so the auth path stays simple.

        Order is not preserved — callers that care about ordering
        should sort the result themselves. Empty / all-invalid input
        returns an empty list.
        """
        if not role_ids:
            return []
        object_ids: list[ObjectId] = []
        for raw in role_ids:
            try:
                object_ids.append(self.to_object_id(raw))
            except InvalidIdError:
                continue
        if not object_ids:
            return []
        cursor = self._collection.find({"_id": {"$in": object_ids}}).sort("name", 1)
        return [self._doc_to_read(doc) async for doc in cursor]
