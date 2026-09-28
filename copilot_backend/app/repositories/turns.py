"""`TurnRepository` — CRUD for the `turns` collection.

T06 (#7) ships the schema. The conversation-detail endpoint (T10)
and the SSE-driven Turn writer (T23) are the first callers.

Design notes:

* Turns are append-only — every test exercises the create + read
  paths that T10 (conversation detail) and T23 (SSE pipeline) reach
  for. There is no `update` path because editing a Turn would
  re-write history.
* `list_by_conversation` returns turns in `created_at` order so the
  Frontend chat panel can render directly. The compound index keeps
  the read an index scan even for sessions that span hundreds of
  Turns.
* `delete_by_conversation` is the cascading helper used when an
  admin hard-deletes a conversation (T10's owner-only remove path).

The doc-parsing and post-insert refetch helpers come from
`app.repositories._common` — see that module for the rationale.
"""
from __future__ import annotations

from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.errors import NotFoundError
from app.db.indexes import TURNS
from app.db.schemas import Turn, TurnCreate
from app.repositories._common import doc_to_read, refetch_after_insert
from app.repositories.base import BaseRepository


def _to_read(doc: dict[str, Any]) -> Turn:
    return doc_to_read(doc, Turn)


class TurnRepository(BaseRepository[Turn, TurnCreate, TurnCreate]):
    """CRUD for the `turns` collection.

    Turns are effectively append-only — there's no `Update` model
    because editing a Turn would re-write history. The repository
    inherits from `BaseRepository` for the shared scaffolding
    (`to_object_id`, `_coerce_id`, `_now`) but only exposes the
    `create` / `get` / list paths the Frontend and SSE pipeline
    actually need. The third generic slot is typed as `TurnCreate`
    only to satisfy `BaseRepository`'s parameterisation; no
    `update` method exists on this class.
    """

    collection_name: ClassVar[str] = TURNS

    def __init__(self, database: AsyncIOMotorDatabase[Any]) -> None:
        super().__init__(database)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, data: TurnCreate) -> Turn:
        """Append a Turn to the conversation transcript.

        No unique index enforcement here — duplicate Turn writes
        are the SSE layer's job to guard against (idempotent retries
        may legitimately resubmit). The `BaseRepository.translate_duplicate`
        hook stays available for callers that wrap inserts in a
        uniqueness contract later.
        """
        doc = data.model_dump()
        doc["created_at"] = self._now()
        await self._collection.insert_one(doc)
        return await refetch_after_insert(self._collection, doc, Turn)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, turn_id: str) -> Turn:
        """Look up by primary key. Raises `NotFoundError` if missing."""
        oid = self.to_object_id(turn_id)
        doc = await self._collection.find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                message_en=f"Turn {turn_id} not found",
                details={"turn_id": turn_id},
            )
        return _to_read(doc)

    async def list_by_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 500,
        after_id: str | None = None,
    ) -> list[Turn]:
        """List a conversation's Turns in `created_at` order.

        Newest-first is intentionally not exposed — chat transcripts
        read top-to-bottom in the Frontend, so we hand the rows back
        in chronological order and let the caller re-sort if their
        UI demands otherwise.
        """
        query: dict[str, Any] = {"conversation_id": conversation_id}
        if after_id is not None:
            query["_id"] = {"$gt": self.to_object_id(after_id)}
        cursor = (
            self._collection.find(query)
            .sort("created_at", 1)
            .limit(limit)
        )
        return [_to_read(doc) async for doc in cursor]

    async def list_by_plan(self, plan_id: str) -> list[Turn]:
        """All Turns attached to a Plan (audit / replay join)."""
        cursor = self._collection.find({"plan_id": plan_id}).sort("created_at", 1)
        return [_to_read(doc) async for doc in cursor]

    # ------------------------------------------------------------------
    # Delete (cascading — admin-only path)
    # ------------------------------------------------------------------

    async def delete_by_conversation(self, conversation_id: str) -> int:
        """Bulk-delete every Turn for a conversation. Returns the count.

        Called by `ConversationRepository.delete` follow-ups. The
        count is part of the return so the admin endpoint can log
        "removed conversation + 12 turns" without a second round
        trip.
        """
        result = await self._collection.delete_many({"conversation_id": conversation_id})
        return int(result.deleted_count)


__all__ = ["TurnRepository"]
