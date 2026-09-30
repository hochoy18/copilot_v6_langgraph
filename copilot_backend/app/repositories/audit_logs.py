"""`AuditLogRepository` — append-only writer + admin drill-down reader.

T06 (#7) ships the schema; the Worker / SSE pipeline writes to this
seam (T21), and the admin audit UI (T43) reads from it. T42 / #37
adds the retention surface:

* `list_archive_candidates` — sweep query for the cold-storage job
  (`occurred_at < threshold`, `lifecycle_status == "active"`,
  sorted ASC so the oldest row migrates first).
* `mark_archived` — accepts an optional `tombstone_overrides` so
  the sweep can slim the heavy payload fields (`parameters`,
  `response`, `error`) in the same atomic write that flips the
  lifecycle. Without the slim the row keeps the full original
  document and "migration" is just a backup copy — the hot tier
  never shrinks.
* `restore_payload` — recall path. Replaces the slim tombstone
  values from the cold blob and flips `lifecycle_status` to
  `recalled` atomically; the filter guards on
  `lifecycle_status == "archived"` so a concurrent recall or
  rewrite can't double-write.
* `query` — extends with optional `before_occurred_at` /
  `before_id` cursor pagination (T43's UI uses `useInfiniteQuery`).
  Sort tiebreak on `_id DESC` keeps pages stable when two rows
  share `occurred_at` to the millisecond.

Design notes:

* `create` is the only mutation in normal operation. Audit rows are
  append-only by design (ADR-0002) — no generic `update`, no
  `delete` for application code. Retention sweeps (T42) get the
  dedicated `mark_archived` / `restore_payload` pair so lifecycle
  column changes can be distinguished from row-content changes.
* `query` is the read seam for the admin filter UI: a single call
  takes optional filter scalars plus an optional cursor. The
  implementation is deliberately one `find` with optional clauses
  rather than several specialised query methods — the index list
  (`by_actor_id`, `by_occurred_at`, …) covers every reasonable
  combination.
* The `tombstone_overrides` argument to `mark_archived` is the only
  payload-mutation path; the caller is responsible for keeping the
  override keys to slim placeholders (`{}`, `None`, etc.) — the
  repository never inspects or rewrites the overrides.

The doc-parsing and post-insert refetch helpers come from
`app.repositories._common` — see that module for the rationale.

The generic-typed `Update` slot is `AuditLogCreate` only because
`BaseRepository` requires three parameters — there is no `update`
path. The lifecycle helpers (`mark_archived`, `restore_payload`)
take the place of an `Update` model.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.errors import NotFoundError
from app.db.indexes import AUDIT_LOGS
from app.db.schemas import AuditLog, AuditLogCreate, AuditLogStatus
from app.repositories._common import doc_to_read, refetch_after_insert
from app.repositories.base import BaseRepository


def _to_read(doc: dict[str, Any]) -> AuditLog:
    return doc_to_read(doc, AuditLog)


class AuditLogRepository(BaseRepository[AuditLog, AuditLogCreate, AuditLogCreate]):
    """Append-only audit log writer + admin drill-down reader."""

    collection_name: ClassVar[str] = AUDIT_LOGS

    def __init__(self, database: AsyncIOMotorDatabase[Any]) -> None:
        super().__init__(database)

    # ------------------------------------------------------------------
    # Create (the only normal write path)
    # ------------------------------------------------------------------

    async def create(self, data: AuditLogCreate) -> AuditLog:
        """Append an immutable audit row.

        Stamps `occurred_at = now()` and initialises the lifecycle
        column to `active`. ADR-0002 forbids UPDATE on audit rows
        from application code; the lifecycle helpers below are the
        sole exception and are themselves append-like state changes.
        """
        doc = data.model_dump()
        doc["occurred_at"] = self._now()
        doc["lifecycle_status"] = "active"
        doc["cold_storage_ref"] = None
        doc["cold_archived_at"] = None
        await self._collection.insert_one(doc)
        return await refetch_after_insert(self._collection, doc, AuditLog)

    # ------------------------------------------------------------------
    # Read — admin drill-down
    # ------------------------------------------------------------------

    async def get(self, audit_log_id: str) -> AuditLog:
        """Look up by primary key. Raises `NotFoundError` on miss."""
        oid = self.to_object_id(audit_log_id)
        doc = await self._collection.find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                message_en=f"AuditLog {audit_log_id} not found",
                details={"audit_log_id": audit_log_id},
            )
        return _to_read(doc)

    async def query(
        self,
        *,
        actor_id: str | None = None,
        conversation_id: str | None = None,
        plan_id: str | None = None,
        tool_name: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        lifecycle_status: AuditLogStatus | None = None,
        before_occurred_at: datetime | None = None,
        before_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditLog]:
        """Filtered audit-log query for the admin UI.

        Every clause is optional; the function AND-s the non-None
        ones. Time bounds are inclusive at `time_from` and exclusive
        at `time_to` — the admin UI shows calendar-day buckets so
        the [start-of-day, start-of-next-day) convention maps
        cleanly.

        Cursor pagination (`before_occurred_at` + `before_id`) is
        the `useInfiniteQuery` seam (T43). When set, the filter
        selects the next slice "strictly after" the last row of the
        previous page in `occurred_at DESC, _id DESC` order:

            occurred_at < before_occurred_at
              OR (occurred_at == before_occurred_at AND _id < before_id)

        The `_id` tiebreak is required: `occurred_at` is millisecond
        precision, so two rows in the same tick collide; without the
        tiebreak the cursor would skip or duplicate on the boundary.
        Both cursor arguments must be supplied together — passing
        one without the other is a programmer error and raises
        `ValueError` to surface it at the seam.

        Clumping eight optional scalars into one parameter list is
        acceptable at this size; if the filter UI grows beyond this,
        the right refactor is to introduce a small `AuditLogQuery`
        dataclass.
        """
        if (before_occurred_at is None) != (before_id is None):
            raise ValueError(
                "before_occurred_at and before_id must be supplied together",
            )

        query: dict[str, Any] = {}
        if actor_id is not None:
            query["actor_id"] = actor_id
        if conversation_id is not None:
            query["conversation_id"] = conversation_id
        if plan_id is not None:
            query["plan_id"] = plan_id
        if tool_name is not None:
            query["tool_name"] = tool_name
        if lifecycle_status is not None:
            query["lifecycle_status"] = lifecycle_status
        if time_from is not None or time_to is not None:
            time_clause: dict[str, Any] = {}
            if time_from is not None:
                time_clause["$gte"] = time_from
            if time_to is not None:
                time_clause["$lt"] = time_to
            query["occurred_at"] = time_clause
        if before_occurred_at is not None and before_id is not None:
            cursor_oid = ObjectId(before_id)
            cursor_clause: dict[str, Any] = {
                "$or": [
                    {"occurred_at": {"$lt": before_occurred_at}},
                    {
                        "occurred_at": before_occurred_at,
                        "_id": {"$lt": cursor_oid},
                    },
                ],
            }
            # `and`-combine with any prior `occurred_at` clause so the
            # cursor's outer range still respects calendar-bucket filters.
            existing_time = query.get("occurred_at")
            if existing_time is not None:
                query["$and"] = [
                    {"occurred_at": existing_time},
                    cursor_clause,
                ]
                del query["occurred_at"]
            else:
                query.update(cursor_clause)

        cursor = (
            self._collection.find(query)
            .sort([("occurred_at", -1), ("_id", -1)])
            .limit(limit)
        )
        return [_to_read(doc) async for doc in cursor]

    async def list_by_conversation(self, conversation_id: str) -> list[AuditLog]:
        """All audit rows for a single conversation, newest first.

        Used by the conversation-detail "audit trail" tab.
        """
        cursor = self._collection.find({"conversation_id": conversation_id}).sort(
            "occurred_at", -1
        )
        return [_to_read(doc) async for doc in cursor]

    # ------------------------------------------------------------------
    # Retention sweep — T42 / #37 (ADR-0028)
    # ------------------------------------------------------------------

    async def list_archive_candidates(
        self,
        *,
        threshold: datetime,
        limit: int,
    ) -> list[AuditLog]:
        """Hot-tier rows older than `threshold` that haven't been archived.

        Filters on `lifecycle_status == "active"` (ADR-0028) so the
        sweep is idempotent: a row already archived or recalled is
        never re-archived. Sorted ASC by `occurred_at` so the
        oldest row migrates first within a batch.

        The companion compound index
        `retention_sweep_by_lifecycle_time` (`lifecycle_status`,
        `occurred_at` DESC — direction doesn't matter for an
        equality-on-first-key scan) covers this query.
        """
        cursor = (
            self._collection.find(
                {
                    "lifecycle_status": "active",
                    "occurred_at": {"$lt": threshold},
                },
            )
            .sort("occurred_at", 1)
            .limit(limit)
        )
        return [_to_read(doc) async for doc in cursor]

    # ------------------------------------------------------------------
    # Lifecycle transitions — the only paths that touch an existing row.
    # ------------------------------------------------------------------

    async def mark_archived(
        self,
        audit_log_id: str,
        cold_storage_ref: str,
        *,
        tombstone_overrides: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Move the row to cold storage (ADR-0028).

        Stamps `cold_archived_at` and the `cold_storage_ref` in a
        single update — partial writes would leave the row's
        lifecycle ambiguous. `tombstone_overrides` (optional) lets
        the caller slim heavy payload fields (`parameters`,
        `response`, `error`, …) atomically with the lifecycle flip;
        the repository never inspects the overrides — only the
        retention sweep knows the right slim values, and it owns the
        decision.

        Without overrides the row's content is preserved verbatim
        — T06 / #7's existing test contract relies on this (the
        lifecycle helpers exist precisely so the existing row
        contract doesn't have to change).
        """
        oid = self.to_object_id(audit_log_id)
        set_doc: dict[str, Any] = {
            "lifecycle_status": "archived",
            "cold_storage_ref": cold_storage_ref,
            "cold_archived_at": self._now(),
        }
        if tombstone_overrides:
            set_doc.update(tombstone_overrides)
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": set_doc},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"AuditLog {audit_log_id} not found",
                details={"audit_log_id": audit_log_id},
            )
        return _to_read(result)

    async def restore_payload(
        self,
        audit_log_id: str,
        payload: dict[str, Any],
    ) -> AuditLog:
        """Hydrate the slim tombstone back from cold storage (ADR-0028).

        Atomically writes the recalled payload fields
        (`parameters`, `response`, `error`) and flips
        `lifecycle_status` to `recalled`. The filter
        `{_id, lifecycle_status: "archived"}` guards against a
        concurrent recall or a row that raced into a different
        state — `NotFoundError` surfaces that contract.

        `cold_storage_ref` is preserved so an audit of the recall
        can trace back to the cold blob without a second query.
        """
        oid = self.to_object_id(audit_log_id)
        result = await self._collection.find_one_and_update(
            {"_id": oid, "lifecycle_status": "archived"},
            {"$set": {**payload, "lifecycle_status": "recalled"}},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"AuditLog {audit_log_id} not archived",
                details={"audit_log_id": audit_log_id},
            )
        return _to_read(result)

    async def mark_recalled(self, audit_log_id: str) -> AuditLog:
        """Mark a previously-archived row as recalled (ADR-0028).

        Flag-only flip; the retention sweep uses `restore_payload`
        instead because it has to write the recalled payload back
        atomically. Kept for the T06 contract — older callers that
        just want to flip the lifecycle column without a payload
        round-trip go through this method.
        """
        oid = self.to_object_id(audit_log_id)
        result = await self._collection.find_one_and_update(
            {"_id": oid, "lifecycle_status": "archived"},
            {"$set": {"lifecycle_status": "recalled"}},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"AuditLog {audit_log_id} not archived",
                details={"audit_log_id": audit_log_id},
            )
        return _to_read(result)

    # NOTE: no `update` or `delete`. Audit rows are append-only per
    # ADR-0002; lifecycle changes use the methods above so the audit
    # subscriber never sees an opaque mutation on a payload field.


__all__ = ["AuditLogRepository"]