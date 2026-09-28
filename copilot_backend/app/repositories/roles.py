"""`RoleRepository` — CRUD for the `roles` collection.

T04 (#5) ships the schema; full role-management (admin endpoints per
ADR-0031) lands with the admin ticket. This class is intentionally
minimal — enough to seed the catalog and let admin tooling exercise
lookups, no more.
"""
from __future__ import annotations

from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError

from app.db.errors import NotFoundError
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
