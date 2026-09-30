"""`ConversationRepository` — CRUD for the `conversations` collection.

T06 (#7) ships the schema. The conversation list endpoints (T10/T11)
and the idle-sweep job (T39 / ADR-0011) are the first callers.

Design notes:

* `create` stamps `last_activity_at = created_at` so the ADR-0011
  "active in last 15 min" predicate holds from the moment the row
  lands. The state machine (`active` / `idle` / `archived`) lives
  here rather than in the API layer so audit hooks see every
  transition as a discrete repository call.
* `touch_activity` is the dedicated path for "user just sent a
  Turn" — every T10 endpoint and the SSE heartbeat reach for it.
  The lifecycle sweep derives from the same field, keeping the
  timing model single-sourced.
* `list_by_user` returns conversations in `last_activity_at DESC`
  order so the Frontend's "recent conversations" panel renders
  top-to-bottom without a client-side sort.

The doc-parsing and post-insert refetch helpers come from
`app.repositories._common` — see that module for the rationale.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.errors import NotFoundError
from app.db.indexes import CONVERSATIONS
from app.db.schemas import (
    Conversation,
    ConversationCreate,
    ConversationStatus,
    ConversationUpdate,
)
from app.repositories._common import doc_to_read, refetch_after_insert
from app.repositories.base import BaseRepository


def _to_read(doc: dict[str, Any]) -> Conversation:
    return doc_to_read(doc, Conversation)


class ConversationRepository(
    BaseRepository[Conversation, ConversationCreate, ConversationUpdate]
):
    """CRUD for the `conversations` collection."""

    collection_name: ClassVar[str] = CONVERSATIONS

    def __init__(self, database: AsyncIOMotorDatabase[Any]) -> None:
        super().__init__(database)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, data: ConversationCreate) -> Conversation:
        """Insert a new conversation, stamping `last_activity_at = now`.

        Newly created conversations are `active` by definition (the
        user just opened them) so the idle sweep doesn't immediately
        reclassify them.
        """
        now = self._now()
        doc = data.model_dump()
        doc["last_activity_at"] = now
        doc["created_at"] = now
        doc["updated_at"] = now
        await self._collection.insert_one(doc)
        return await refetch_after_insert(self._collection, doc, Conversation)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, conversation_id: str) -> Conversation:
        """Look up by primary key. Raises `NotFoundError` if missing."""
        oid = self.to_object_id(conversation_id)
        doc = await self._collection.find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                message_en=f"Conversation {conversation_id} not found",
                details={"conversation_id": conversation_id},
            )
        return _to_read(doc)

    async def list_by_user(
        self,
        user_id: str,
        *,
        status: ConversationStatus | None = None,
        limit: int = 50,
        after_id: str | None = None,
    ) -> list[Conversation]:
        """List a user's conversations, newest activity first.

        Backed by `by_user_last_activity` (default) or `by_user_status`
        (when filtering). The cursor pagination mirrors `UserRepository`
        — `after_id` is the last id from the previous page, so the page
        size is stable under live inserts.
        """
        query: dict[str, Any] = {"user_id": user_id}
        if status is not None:
            query["status"] = status
        if after_id is not None:
            query["_id"] = {"$gt": self.to_object_id(after_id)}
        cursor = (
            self._collection.find(query)
            .sort("last_activity_at", -1)
            .limit(limit)
        )
        return [_to_read(doc) async for doc in cursor]

    async def list_by_status(self, status: ConversationStatus) -> list[Conversation]:
        """All conversations in a given lifecycle state.

        T39 (the idle sweep) calls this with `status='active'` and
        filters in Python by the staleness threshold; Mongo only
        ships the cheap compound-index lookup.
        """
        cursor = self._collection.find({"status": status}).sort("last_activity_at", 1)
        return [_to_read(doc) async for doc in cursor]

    async def list_idle_candidates(
        self,
        *,
        threshold: datetime,
    ) -> list[Conversation]:
        """Every `active` conversation whose `last_activity_at < threshold`.

        Backs the `active → idle` branch of the lifecycle sweep (T39).
        Filtering on `status='active'` plus the cutoff date lets
        Mongo use the `by_status_activity` compound index for the
        cheap lookup; `last_activity_at < threshold` is the second
        sort key. The sweep then iterates and atomically flips each
        row via `mark_idle` so concurrent activity doesn't race.
        """
        cursor = (
            self._collection.find(
                {"status": "active", "last_activity_at": {"$lt": threshold}},
            )
            .sort("last_activity_at", 1)
        )
        return [_to_read(doc) async for doc in cursor]

    async def list_archive_candidates(
        self,
        *,
        threshold: datetime,
    ) -> list[Conversation]:
        """Every `idle` conversation whose `idle_since < threshold`.

        Backs the `idle → archived` branch of the lifecycle sweep
        (T39). The candidate set is small at any instant (the user
        must have stopped chatting for 30 days) so an index scan is
        cheap; a future index `by_status_idle_since` would tighten
        further if needed.
        """
        cursor = (
            self._collection.find(
                {"status": "idle", "idle_since": {"$lt": threshold}},
            )
            .sort("idle_since", 1)
        )
        return [_to_read(doc) async for doc in cursor]

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(self, conversation_id: str, patch: ConversationUpdate) -> Conversation:
        """Apply a partial update, bumping `updated_at`."""
        oid = self.to_object_id(conversation_id)
        update_doc = patch.model_dump(exclude_none=True)
        update_doc["updated_at"] = self._now()
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": update_doc},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"Conversation {conversation_id} not found",
                details={"conversation_id": conversation_id},
            )
        return _to_read(result)

    async def set_status(
        self, conversation_id: str, status: ConversationStatus
    ) -> Conversation:
        """Atomic status transition (ADR-0011 lifecycle).

        Bumps `updated_at` so audit hooks see the event. Split from
        `update` so subscribers don't have to diff every PATCH.

        Note: lifecycle transitions that need to stamp a side
        timestamp (`idle_since` / `archived_since`) should reach for
        `mark_idle` / `mark_archived` instead — `set_status` only
        touches `status` + `updated_at`, matching the manual-archive
        path that intentionally doesn't stamp `idle_since` (the
        sweep job owns that timestamp per ADR-0011).
        """
        oid = self.to_object_id(conversation_id)
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": {"status": status, "updated_at": self._now()}},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"Conversation {conversation_id} not found",
                details={"conversation_id": conversation_id},
            )
        return _to_read(result)

    async def mark_idle(
        self, conversation_id: str, *, now: datetime | None = None
    ) -> Conversation:
        """Atomic `active → idle` transition (T39 / ADR-0011).

        Stamps both `status` and `idle_since` in a single
        `find_one_and_update` so audit log subscribers never see a
        half-transitioned row (one without the other). The `now`
        parameter is injectable for deterministic tests; production
        callers omit it and rely on the repository clock.
        """
        oid = self.to_object_id(conversation_id)
        stamped = now if now is not None else self._now()
        result = await self._collection.find_one_and_update(
            {"_id": oid, "status": "active"},
            {
                "$set": {
                    "status": "idle",
                    "idle_since": stamped,
                    "updated_at": stamped,
                },
            },
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"Conversation {conversation_id} not found or not active",
                details={"conversation_id": conversation_id},
            )
        return _to_read(result)

    async def mark_archived(
        self, conversation_id: str, *, now: datetime | None = None
    ) -> Conversation:
        """Atomic `idle → archived` transition (T39 / ADR-0011).

        Stamps both `status` and `archived_since` in a single write.
        The `status: 'idle'` filter on the update means a row that
        raced into `active` again (re-shifted by user activity
        between the candidate scan and the per-row write) survives
        the sweep untouched — the next tick will retry.
        """
        oid = self.to_object_id(conversation_id)
        stamped = now if now is not None else self._now()
        result = await self._collection.find_one_and_update(
            {"_id": oid, "status": "idle"},
            {
                "$set": {
                    "status": "archived",
                    "archived_since": stamped,
                    "updated_at": stamped,
                },
            },
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"Conversation {conversation_id} not found or not idle",
                details={"conversation_id": conversation_id},
            )
        return _to_read(result)

    async def create_from_reactivate(
        self,
        *,
        source: Conversation,
        title: str = "",
        now: datetime | None = None,
    ) -> Conversation:
        """Insert a fresh conversation that inherits from an archived one (T39).

        Per ADR-0011 reactivating an archived conversation creates a
        new active row that copies the most recent K turns and the
        latest Plan (the seam in `ConversationService.reactivate`).
        This repository method only inserts the conversation row —
        the Turn / Plan copy is a service-layer responsibility
        because it spans three repositories.

        `reactivated_from_id` is the FK pointer back to `source`;
        `reactivate_count` is the audit counter. `last_activity_at`
        is stamped to `now` so the freshly-active row lands outside
        the idle-sweep window. The row goes through
        `ConversationCreate` (like `create`) so Pydantic validation
        runs on the reactivate path too.
        """
        data = ConversationCreate(
            user_id=source.user_id,
            title=title,
            status="active",
            idle_since=None,
            archived_since=None,
            reactivated_from_id=source.id,
            reactivate_count=source.reactivate_count + 1,
        )
        doc = data.model_dump()
        stamped = now if now is not None else self._now()
        doc["last_activity_at"] = stamped
        doc["created_at"] = stamped
        doc["updated_at"] = stamped
        await self._collection.insert_one(doc)
        return await refetch_after_insert(self._collection, doc, Conversation)

    async def touch_activity(self, conversation_id: str) -> Conversation:
        """Stamp `last_activity_at` so the idle-sweep sees fresh activity.

        The user just sent a Turn / opened the conversation / got a
        Plan preview. We only bump `last_activity_at` and `updated_at`
        — touching `status` would race with the sweep and risk flipping
        a freshly idle conversation back to `active` mid-day.
        """
        oid = self.to_object_id(conversation_id)
        now = self._now()
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": {"last_activity_at": now, "updated_at": now}},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"Conversation {conversation_id} not found",
                details={"conversation_id": conversation_id},
            )
        return _to_read(result)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, conversation_id: str) -> None:
        """Hard-delete the conversation.

        ADR-0011 keeps the soft lifecycle on the same row; `delete` is
        for admin-side data-removal only. Calling code should `archive`
        via `set_status` first to preserve the audit trail.
        """
        oid = self.to_object_id(conversation_id)
        result = await self._collection.delete_one({"_id": oid})
        if result.deleted_count == 0:
            raise NotFoundError(
                message_en=f"Conversation {conversation_id} not found",
                details={"conversation_id": conversation_id},
            )

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    async def last_activity_at(self, conversation_id: str) -> datetime | None:
        """Return the most recent `last_activity_at`, or `None` if missing."""
        oid = self.to_object_id(conversation_id)
        doc: dict[str, Any] | None = await self._collection.find_one(
            {"_id": oid},
            projection={"last_activity_at": 1, "_id": 0},
        )
        if doc is None:
            return None
        ts: Any = doc.get("last_activity_at")
        return ts if isinstance(ts, datetime) else None


__all__ = ["ConversationRepository"]
