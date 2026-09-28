"""`ToolRepository` — CRUD for the `tools` collection.

T05 (#6) ships the schema. The Tool Registry admin endpoints (ADR-0031
`/admin/tools/...`) and the OpenAPI import flow (T14 / T15) land later;
this repository is the seam they reach for.

Design notes:

* `create` returns the canonical `Tool` — there's nothing secret in the
  document so `Tool` and `ToolInDB` share every field. The split mirrors
  the rest of the codebase for uniformity.
* `list_active` is the read call the runtime cares about (ADR-0018).
  The `by_status` index makes it an index scan rather than a collection
  scan — at Tool-registry scale (hundreds per row at most) the
  difference is modest, but the API is right.
* `list_by_credential` supports credential rotation: before
  `CredentialRepository.delete` runs, an admin UI can warn "this
  credential is in use by N Tools".
* `set_status` is the dedicated path for activation / disablement —
  audit logs (T42) will subscribe to it specifically rather than
  treating `update` as opaque.
"""
from __future__ import annotations

from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError

from app.db.errors import NotFoundError
from app.db.indexes import TOOLS
from app.db.schemas import Tool, ToolCreate, ToolInDB, ToolStatus, ToolUpdate
from app.repositories.base import BaseRepository


class ToolRepository(BaseRepository[Tool, ToolCreate, ToolUpdate]):
    """CRUD for the `tools` collection."""

    collection_name: ClassVar[str] = TOOLS

    def __init__(self, database: AsyncIOMotorDatabase[Any]) -> None:
        super().__init__(database)

    # ------------------------------------------------------------------
    # Internal — Mongo doc → read shape.
    # ------------------------------------------------------------------

    @staticmethod
    def _doc_to_read(doc: dict[str, Any]) -> Tool:
        return Tool.model_validate(BaseRepository._coerce_id(doc))

    @staticmethod
    def _doc_to_in_db(doc: dict[str, Any]) -> ToolInDB:
        return ToolInDB.model_validate(BaseRepository._coerce_id(doc))

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, data: ToolCreate) -> Tool:
        """Insert a new Tool. Defaults to `status='draft'` per ADR-0018.

        Raises:
            DuplicateKeyError: a Tool with the same `name` already exists.
        """
        now = self._now()
        doc = data.model_dump()
        doc["created_at"] = now
        doc["updated_at"] = now
        try:
            await self._collection.insert_one(doc)
        except PyMongoDuplicateKeyError as exc:
            raise self._translate_duplicate(exc) from exc
        # Re-fetch so the canonical read shape reflects the persisted
        # row exactly (mirrors `UserRepository.create`).
        stored = await self._collection.find_one({"_id": doc["_id"]})
        if stored is None:
            raise NotFoundError(message_en="Tool disappeared after insert")
        return self._doc_to_read(stored)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, tool_id: str) -> Tool:
        """Look up by primary key. Raises `NotFoundError` if missing."""
        oid = self.to_object_id(tool_id)
        doc = await self._collection.find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                message_en=f"Tool {tool_id} not found",
                details={"tool_id": tool_id},
            )
        return self._doc_to_read(doc)

    async def get_in_db(self, tool_id: str) -> ToolInDB:
        """Look up by primary key, returning the persisted-shape row.

        Future callers that need raw `parameters_schema` (Plan-Tool
        snapshot binding, T44) use this; API responses must use `get`.
        """
        oid = self.to_object_id(tool_id)
        doc = await self._collection.find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                message_en=f"Tool {tool_id} not found",
                details={"tool_id": tool_id},
            )
        return self._doc_to_in_db(doc)

    async def get_by_name(self, name: str) -> Tool:
        """Look up by LLM-facing slug. Raises `NotFoundError` if missing."""
        doc = await self._collection.find_one({"name": name})
        if doc is None:
            raise NotFoundError(
                message_en=f"Tool {name!r} not found",
                details={"name": name},
            )
        return self._doc_to_read(doc)

    async def list_active(self) -> list[Tool]:
        """Every `status='active'` Tool — the Planner's view per ADR-0018.

        Backed by the `by_status` index. Returned in `name` order so
        the LLM-facing catalog is stable across calls.
        """
        cursor = self._collection.find({"status": "active"}).sort("name", 1)
        return [self._doc_to_read(doc) async for doc in cursor]

    async def list_all(self) -> list[Tool]:
        """Every Tool regardless of status. Admin Registry UI uses this."""
        cursor = self._collection.find({}).sort("name", 1)
        return [self._doc_to_read(doc) async for doc in cursor]

    async def list_by_status(self, status: ToolStatus) -> list[Tool]:
        """Tools filtered by exact `status` value.

        Convenience wrapper around the same `by_status` index for the
        admin "drafts queue" / "disabled" tabs.
        """
        cursor = self._collection.find({"status": status}).sort("name", 1)
        return [self._doc_to_read(doc) async for doc in cursor]

    async def list_by_credential(self, credentials_ref: str) -> list[Tool]:
        """Tools pointing at a given `credentials_ref`.

        The credential-deletion pre-check uses this: refuse to delete a
        credential that's still in use.
        """
        cursor = self._collection.find({"credentials_ref": credentials_ref}).sort("name", 1)
        return [self._doc_to_read(doc) async for doc in cursor]

    async def count_by_status(self, status: ToolStatus) -> int:
        """Total Tools in the given `status`. Admin dashboard metric."""
        return await self._collection.count_documents({"status": status})

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(self, tool_id: str, patch: ToolUpdate) -> Tool:
        """Apply a partial update, bumping `updated_at`."""
        oid = self.to_object_id(tool_id)
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
                message_en=f"Tool {tool_id} not found",
                details={"tool_id": tool_id},
            )
        return self._doc_to_read(result)

    async def set_status(self, tool_id: str, status: ToolStatus) -> Tool:
        """Atomic activation / disablement. Bumps `updated_at`.

        Separated from `update` so audit-log hooks (T42) can subscribe
        to "status changed" events without diffing every PATCH.
        """
        oid = self.to_object_id(tool_id)
        now = self._now()
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": {"status": status, "updated_at": now}},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"Tool {tool_id} not found",
                details={"tool_id": tool_id},
            )
        return self._doc_to_read(result)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, tool_id: str) -> None:
        """Hard-delete the Tool. Raises `NotFoundError` if missing.

        Admin tooling can also `set_status(..., 'disabled')` to take a
        Tool offline while preserving the row for history. Hard delete is
        only for "registered by mistake, never activated".
        """
        oid = self.to_object_id(tool_id)
        result = await self._collection.delete_one({"_id": oid})
        if result.deleted_count == 0:
            raise NotFoundError(
                message_en=f"Tool {tool_id} not found",
                details={"tool_id": tool_id},
            )