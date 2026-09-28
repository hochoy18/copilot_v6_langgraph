"""`AuditLogRepository` — append-only writer + admin drill-down reader.

T06 (#7) ships the schema; the Worker / SSE pipeline will write to
this seam as soon as the Tool Worker lands (T21), and the admin
audit UI (T43) reads from it. The schema carries ADR-0028 retention
fields directly on the row so the hot/cold split can be enforced
without a second collection.

Design notes:

* `create` is the only mutation in normal operation. Audit rows are
  append-only by design (ADR-0002) — no generic `update`, no
  `delete` for application code. Admin retention sweeps (T42) get
  a dedicated `mark_archived` / `mark_recalled` pair so the
  lifecycle column changes can be distinguished from row-content
  changes.
* `query` is the read seam for the admin filter UI: a single call
  takes a `time_range` plus zero-or-more FK lookups (actor,
  conversation, plan, tool). The implementation is deliberately
  one `find` with optional clauses rather than several
  specialised query methods — the index list (`by_actor_id`,
  `by_occurred_at`, …) covers every reasonable combination.
* `cold_archive` is the boundary call the cold-storage job uses.
  It stamps `cold_archived_at` and the storage ref atomically — a
  partial write would leave the row in a state where it's missing
  from MongoDB but the cold-storage move hasn't completed either.

The doc-parsing and post-insert refetch helpers come from
`app.repositories._common` — see that module for the rationale.

The generic-typed `Update` slot is `AuditLogCreate` only because
`BaseRepository` requires three parameters — there is no `update`
path. The lifecycle helpers (`mark_archived`, `mark_recalled`) take
the place of an `Update` model.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

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
        limit: int = 100,
    ) -> list[AuditLog]:
        """Filtered audit-log query for the admin UI.

        Every clause is optional; the function AND-s the non-None
        ones. Time bounds are inclusive at `time_from` and exclusive
        at `time_to` — the admin UI shows calendar-day buckets so
        the [start-of-day, start-of-next-day) convention maps
        cleanly. Clumping six optional scalars into one parameter
        list is acceptable at this size; if the filter UI grows
        beyond this, the right refactor is to introduce a small
        `AuditLogQuery` dataclass.
        """
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

        cursor = self._collection.find(query).sort("occurred_at", -1).limit(limit)
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
    # Lifecycle transitions — the only paths that touch an existing row.
    # ------------------------------------------------------------------

    async def mark_archived(
        self,
        audit_log_id: str,
        cold_storage_ref: str,
    ) -> AuditLog:
        """Move the row to cold storage (ADR-0028).

        Stamps `cold_archived_at` and the `cold_storage_ref` in a
        single update — partial writes would leave the row's
        lifecycle ambiguous. The Worker / sweeper inserts a
        tombstone with `lifecycle_status=archived` so the row
        remains visible (and filterable) in Mongo while the heavy
        data lives in cold storage.
        """
        oid = self.to_object_id(audit_log_id)
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {
                "$set": {
                    "lifecycle_status": "archived",
                    "cold_storage_ref": cold_storage_ref,
                    "cold_archived_at": self._now(),
                }
            },
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"AuditLog {audit_log_id} not found",
                details={"audit_log_id": audit_log_id},
            )
        return _to_read(result)

    async def mark_recalled(self, audit_log_id: str) -> AuditLog:
        """Mark a previously-archived row as recalled (ADR-0028).

        The 5-minute SLO from `cold_archived_at` to "row queryable
        again" lives in T42's recall workflow; the repository only
        records the flag flip once the cold-storage hydration
        completes.
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
