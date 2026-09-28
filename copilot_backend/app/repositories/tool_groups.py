"""`ToolGroupRepository` — CRUD for the `tool_groups` collection.

T04 (#5) ships the schema; the Tools→Groups admin endpoints (ADR-0031)
land later. As with `RoleRepository`, this class is intentionally
minimal — get / create / list / update are enough for seed data and
admin tooling.
"""
from __future__ import annotations

from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError

from app.db.errors import NotFoundError
from app.db.indexes import TOOL_GROUPS
from app.db.schemas import ToolGroup, ToolGroupCreate, ToolGroupUpdate
from app.repositories.base import BaseRepository


class ToolGroupRepository(BaseRepository[ToolGroup, ToolGroupCreate, ToolGroupUpdate]):
    """CRUD for the `tool_groups` collection."""

    collection_name: ClassVar[str] = TOOL_GROUPS

    def __init__(self, database: AsyncIOMotorDatabase[Any]) -> None:
        super().__init__(database)

    @staticmethod
    def _doc_to_read(doc: dict[str, Any]) -> ToolGroup:
        return ToolGroup.model_validate(BaseRepository._coerce_id(doc))

    async def get_by_name(self, name: str) -> ToolGroup:
        doc = await self._collection.find_one({"name": name})
        if doc is None:
            raise NotFoundError(
                message_en=f"Tool group {name!r} not found",
                details={"name": name},
            )
        return self._doc_to_read(doc)

    async def create(self, data: ToolGroupCreate) -> ToolGroup:
        now = self._now()
        doc = data.model_dump()
        doc["created_at"] = now
        doc["updated_at"] = now
        try:
            await self._collection.insert_one(doc)
        except PyMongoDuplicateKeyError as exc:
            raise self._translate_duplicate(exc) from exc
        return self._doc_to_read(doc)

    async def list_all(self) -> list[ToolGroup]:
        cursor = self._collection.find({}).sort("name", 1)
        return [self._doc_to_read(doc) async for doc in cursor]

    async def update(self, group_id: str, patch: ToolGroupUpdate) -> ToolGroup:
        oid = self.to_object_id(group_id)
        update_doc = patch.model_dump(exclude_none=True)
        update_doc["updated_at"] = self._now()
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": update_doc},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"Tool group {group_id} not found",
                details={"group_id": group_id},
            )
        return self._doc_to_read(result)
