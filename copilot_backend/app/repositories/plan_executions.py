"""`PlanExecutionRepository` — CRUD for the `plan_executions` collection.

T06 (#7) ships the schema. The Worker (T21) creates the row when
execution starts; per-node appends land via `upsert_node_result`;
the aggregate terminal transition lands via `set_status`.

Design notes:

* One Plan may have many execution rows — a re-execution after a
  business-user retry creates a new row rather than mutating the
  prior one (audit semantics: each attempt is its own row).
* `node_results` is a per-node snapshot list. Per-node appends use
  the `upsert_array_element` helper, which atomically replaces an
  existing entry by `node_id` (positional `$`) and falls back to a
  `$push` (also filtered by absence). The two ops are individually
  atomic at the document level so a concurrent SSE `tool.finished`
  event for another node never clobbers the first — earlier
  versions loaded the entire array, mutated client-side, and wrote
  it back, which had a TOCTOU window between nodes.
* The aggregate `status` is a rollup the Worker maintains in the
  same `set_status` call that records the final node; the Frontend
  dashboard reads it directly rather than re-deriving from the
  per-node list.

The doc-parsing and post-insert refetch helpers come from
`app.repositories._common` — see that module for the rationale.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.errors import NotFoundError
from app.db.indexes import PLAN_EXECUTIONS
from app.db.schemas import (
    PlanExecution,
    PlanExecutionCreate,
    PlanExecutionUpdate,
    PlanNodeResult,
    PlanNodeStatus,
)
from app.repositories._common import (
    doc_to_read,
    refetch_after_insert,
    upsert_array_element,
)
from app.repositories.base import BaseRepository

AggregateStatus = Literal["running", "completed", "failed", "aborted"]


def _to_read(doc: dict[str, Any]) -> PlanExecution:
    return doc_to_read(doc, PlanExecution)


def _merge_node_result(
    prior: PlanNodeResult | None,
    *,
    node_id: str,
    status: PlanNodeStatus,
    started_at: Any | None,
    finished_at: Any | None,
    error: dict[str, Any] | None,
    retry_count: int | None,
) -> PlanNodeResult:
    """Merge a `mark_node_status` call onto the prior `PlanNodeResult`.

    Each supplied field wins; the prior's value carries through
    when the caller omits the field. Extracted from the parent
    method so the merge rules stay readable — and so future
    per-node helpers (`record_request_payload`, etc.) reuse the
    same merge logic.
    """
    return PlanNodeResult(
        node_id=node_id,
        status=status,
        started_at=started_at if started_at is not None else (prior.started_at if prior else None),
        finished_at=(
            finished_at if finished_at is not None else (prior.finished_at if prior else None)
        ),
        request=prior.request if prior else None,
        response=prior.response if prior else None,
        error=error if error is not None else (prior.error if prior else None),
        retry_count=(
            retry_count if retry_count is not None else (prior.retry_count if prior else 0)
        ),
    )


class PlanExecutionRepository(
    BaseRepository[PlanExecution, PlanExecutionCreate, PlanExecutionUpdate]
):
    """CRUD for the `plan_executions` collection."""

    collection_name: ClassVar[str] = PLAN_EXECUTIONS

    def __init__(self, database: AsyncIOMotorDatabase[Any]) -> None:
        super().__init__(database)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, data: PlanExecutionCreate) -> PlanExecution:
        """Open a new execution for the given Plan, stamping `started_at`.

        Re-executing a Plan (admin retry, agent re-run) inserts a
        second row — never mutates the prior one — so audit trails
        see every attempt as a discrete object.
        """
        now = self._now()
        doc = data.model_dump()
        doc["started_at"] = now
        doc["finished_at"] = None
        doc["created_at"] = now
        doc["updated_at"] = now
        await self._collection.insert_one(doc)
        return await refetch_after_insert(self._collection, doc, PlanExecution)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, execution_id: str) -> PlanExecution:
        """Look up by primary key. Raises `NotFoundError` if missing."""
        oid = self.to_object_id(execution_id)
        doc = await self._collection.find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                message_en=f"PlanExecution {execution_id} not found",
                details={"execution_id": execution_id},
            )
        return _to_read(doc)

    async def list_by_plan(self, plan_id: str) -> list[PlanExecution]:
        """Every execution of a Plan, newest first.

        Backed by `by_plan_started_at`. Replays / audits reach for
        this to walk the Plan's full attempt history.
        """
        cursor = self._collection.find({"plan_id": plan_id}).sort("started_at", -1)
        return [_to_read(doc) async for doc in cursor]

    async def get_latest_for_plan(self, plan_id: str) -> PlanExecution:
        """Most recent execution for a Plan — the Worker's current run."""
        doc = await self._collection.find_one(
            {"plan_id": plan_id},
            sort=[("started_at", -1)],
        )
        if doc is None:
            raise NotFoundError(
                message_en=f"No PlanExecution found for plan {plan_id}",
                details={"plan_id": plan_id},
            )
        return _to_read(doc)

    async def list_running(self) -> list[PlanExecution]:
        """Every execution still in `running` status.

        Backs the Frontend's "live executions" panel and the
        supervisor's stale-execution detector.
        """
        cursor = self._collection.find({"status": "running"}).sort("started_at", -1)
        return [_to_read(doc) async for doc in cursor]

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(
        self, execution_id: str, patch: PlanExecutionUpdate
    ) -> PlanExecution:
        """Apply a partial update, bumping `updated_at`."""
        oid = self.to_object_id(execution_id)
        update_doc = patch.model_dump(exclude_none=True)
        update_doc["updated_at"] = self._now()
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": update_doc},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"PlanExecution {execution_id} not found",
                details={"execution_id": execution_id},
            )
        return _to_read(result)

    async def set_status(
        self,
        execution_id: str,
        status: AggregateStatus,
    ) -> PlanExecution:
        """Atomic aggregate status transition; stamps `finished_at` on terminal."""
        oid = self.to_object_id(execution_id)
        now = self._now()
        update_doc: dict[str, Any] = {"status": status, "updated_at": now}
        if status in {"completed", "failed", "aborted"}:
            update_doc["finished_at"] = now
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": update_doc},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"PlanExecution {execution_id} not found",
                details={"execution_id": execution_id},
            )
        return _to_read(result)

    async def upsert_node_result(
        self,
        execution_id: str,
        node_result: PlanNodeResult,
    ) -> PlanExecution:
        """Append / replace a `node_results` entry by `node_id`.

        Race-free against concurrent SSE events for sibling nodes:

        1. The helper tries a positional `$` $set matching the
           existing `node_id`. The filter uses Mongo's array-element
           predicate (`<array>.<field>: value`) so the positional
           refers unambiguously to that element. If found, that's
           one round trip and the document is returned.
        2. If no entry matched, the helper issues a `$push` filtered
           by `node_results.node_id: {$ne: value}`, so a concurrent
           insert for the same element can't double-write.

        Both ops are document-level atomic; the helper never reads
        the array client-side.
        """
        oid = self.to_object_id(execution_id)
        result = await upsert_array_element(
            self._collection,
            filter_doc={"_id": oid},
            array_field="node_results",
            element_id_field="node_id",
            element_id_value=node_result.node_id,
            new_element=node_result.model_dump(),
            not_found_details={"execution_id": execution_id, "entity": "PlanExecution"},
        )
        # Bump `updated_at` separately so the captured write gets stamped.
        bumped = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": {"updated_at": self._now()}},
            return_document=True,
        )
        return _to_read(bumped if bumped is not None else result)

    async def mark_node_status(
        self,
        execution_id: str,
        node_id: str,
        status: PlanNodeStatus,
        *,
        started_at: Any | None = None,
        finished_at: Any | None = None,
        error: dict[str, Any] | None = None,
        retry_count: int | None = None,
    ) -> PlanExecution:
        """Per-node lifecycle helper.

        Most Worker flows reach for `upsert_node_result` with a
        fully-built `PlanNodeResult`; this convenience method exists
        so the Worker can stamp `started_at` on the `tool.started`
        SSE event without rebuilding the full record.

        Implementation: read the current row, merge the supplied
        fields onto the prior element, and write the merged element
        back via the race-free `upsert_node_result`. The
        read-modify-write window is small but technically TOCTOU
        for the *same* node — concurrent calls to `mark_node_status`
        for sibling nodes are race-free (each op targets its own
        `node_id` via the helper). For the realistic Worker case
        (a single per-node SSE event stream), this is safe.
        """
        existing = await self.get(execution_id)
        prior = next(
            (n for n in existing.node_results if n.node_id == node_id),
            None,
        )
        merged = _merge_node_result(
            prior,
            node_id=node_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            error=error,
            retry_count=retry_count,
        )
        return await self.upsert_node_result(execution_id, merged)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, execution_id: str) -> None:
        """Hard-delete a PlanExecution.

        Reserved for the admin data-removal path; `audit_logs` rows
        keep their own FK pointer to the Plan.
        """
        oid = self.to_object_id(execution_id)
        result = await self._collection.delete_one({"_id": oid})
        if result.deleted_count == 0:
            raise NotFoundError(
                message_en=f"PlanExecution {execution_id} not found",
                details={"execution_id": execution_id},
            )


__all__ = ["PlanExecutionRepository", "AggregateStatus"]
