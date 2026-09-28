"""`PlanRepository` — CRUD for the `plans` collection.

T06 (#7) ships the schema. The Planner (T18/T25), the Plan-edit
endpoint (T26 / ADR-0019), and the Frontend React Flow renderer
(T19) reach for this seam.

Design notes:

* `nodes` carries the embedded `tool_snapshot`s per ADR-0027 — the
  acceptance criterion for T06. The repository never splits the
  snapshot off into a separate collection; replays read both the
  node's intended `parameters` and the snapshot's frozen schema in
  one fetch.
* `set_status` is the dedicated path for the Plan lifecycle. The
  Worker (`plan_executions`) reaches for it as Plans move from
  `approved` → `executing` → `succeeded`/`failed`; audit log
  subscribers treat status transitions as discrete events without
  diffing arbitrary PATCHes.
* `record_edit` writes the diff between the original and the
  business-user-edited Plan (ADR-0019). Keeping the diff on the
  Plan itself means audit replays don't need a side-channel
  collection.

The doc-parsing and post-insert refetch helpers come from
`app.repositories._common` — see that module for the rationale.
"""
from __future__ import annotations

from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.errors import NotFoundError, ValidationError
from app.db.indexes import PLANS
from app.db.schemas import Plan, PlanCreate, PlanStatus, PlanUpdate
from app.repositories._common import doc_to_read, refetch_after_insert
from app.repositories.base import BaseRepository


def _to_read(doc: dict[str, Any]) -> Plan:
    return doc_to_read(doc, Plan)


class PlanRepository(BaseRepository[Plan, PlanCreate, PlanUpdate]):
    """CRUD for the `plans` collection."""

    collection_name: ClassVar[str] = PLANS

    def __init__(self, database: AsyncIOMotorDatabase[Any]) -> None:
        super().__init__(database)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, data: PlanCreate) -> Plan:
        """Insert a Plan, rejecting empty-DAG inputs at the seam.

        A Plan with zero nodes has no audit value and confuses the
        React Flow renderer (which expects at least one node for
        layout), so we surface that as a 400 instead of letting it
        reach the DB.
        """
        if not data.nodes:
            raise ValidationError(
                message_en="A Plan must contain at least one node",
                details={"nodes": data.nodes},
            )
        now = self._now()
        doc = data.model_dump()
        doc["created_at"] = now
        doc["updated_at"] = now
        await self._collection.insert_one(doc)
        return await refetch_after_insert(self._collection, doc, Plan)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, plan_id: str) -> Plan:
        """Look up by primary key. Raises `NotFoundError` if missing."""
        oid = self.to_object_id(plan_id)
        doc = await self._collection.find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                message_en=f"Plan {plan_id} not found",
                details={"plan_id": plan_id},
            )
        return _to_read(doc)

    async def get_latest_for_conversation(self, conversation_id: str) -> Plan:
        """The most-recent Plan in a conversation — the Frontend's DAG view.

        Per ADR-0005 each conversation has at most one "active" Plan
        at a time; this is the read that hands that Plan to the
        React Flow renderer.
        """
        doc = await self._collection.find_one(
            {"conversation_id": conversation_id},
            sort=[("created_at", -1)],
        )
        if doc is None:
            raise NotFoundError(
                message_en=f"No Plan found for conversation {conversation_id}",
                details={"conversation_id": conversation_id},
            )
        return _to_read(doc)

    async def list_by_turn(self, turn_id: str) -> list[Plan]:
        """Every Plan attached to a given Turn (cross-turn replay)."""
        cursor = self._collection.find({"turn_id": turn_id}).sort("created_at", -1)
        return [_to_read(doc) async for doc in cursor]

    async def list_by_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
    ) -> list[Plan]:
        """Every Plan in a conversation, newest first.

        T10's detail endpoint composes this with
        `TurnRepository.list_by_conversation` to render the Frontend's
        session-level history. Limit defaults to a defensive ceiling
        (T11's audit / admin read path will widen this if needed).
        """
        cursor = (
            self._collection.find({"conversation_id": conversation_id})
            .sort("created_at", -1)
            .limit(limit)
        )
        return [_to_read(doc) async for doc in cursor]

    async def list_by_status(self, status: PlanStatus) -> list[Plan]:
        """All Plans in a given lifecycle state.

        Drives the HITL pending queue (`status: pending`) and the
        admin "in flight" dashboard (`status: executing`).
        """
        cursor = self._collection.find({"status": status}).sort("created_at", -1)
        return [_to_read(doc) async for doc in cursor]

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(self, plan_id: str, patch: PlanUpdate) -> Plan:
        """Apply a partial update, bumping `updated_at`."""
        oid = self.to_object_id(plan_id)
        update_doc = patch.model_dump(exclude_none=True)
        update_doc["updated_at"] = self._now()
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": update_doc},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"Plan {plan_id} not found",
                details={"plan_id": plan_id},
            )
        return _to_read(result)

    async def set_status(self, plan_id: str, status: PlanStatus) -> Plan:
        """Atomic status transition. Bumps `updated_at`.

        Lifecycle moves (`pending` → `approved` / `modified` /
        `executing` → `succeeded` / `failed`) land here so audit
        subscribers see them as discrete events rather than
        diffing arbitrary PATCH bodies.
        """
        oid = self.to_object_id(plan_id)
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {"$set": {"status": status, "updated_at": self._now()}},
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"Plan {plan_id} not found",
                details={"plan_id": plan_id},
            )
        return _to_read(result)

    async def record_edit(
        self,
        plan_id: str,
        edited_nodes: list[dict[str, Any]],
        diff: dict[str, Any],
    ) -> Plan:
        """Apply a business-user edit (ADR-0019).

        Writes the edited `nodes` array and the `edited_diff` JSON
        in one atomic update, then flips `status` to `modified`.
        The diff format is `{"by_node_id": {"param": {"before": …,
        "after": …}}}` — see ADR-0019 for the contract.
        """
        oid = self.to_object_id(plan_id)
        now = self._now()
        result = await self._collection.find_one_and_update(
            {"_id": oid},
            {
                "$set": {
                    "nodes": edited_nodes,
                    "edited_diff": diff,
                    "status": "modified",
                    "updated_at": now,
                }
            },
            return_document=True,
        )
        if result is None:
            raise NotFoundError(
                message_en=f"Plan {plan_id} not found",
                details={"plan_id": plan_id},
            )
        return _to_read(result)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, plan_id: str) -> None:
        """Hard-delete a Plan.

        Used by the admin data-removal path. `plan_executions` and
        `audit_logs` rows for this Plan keep their FK pointer; the
        audit UI surfaces "Plan deleted" rather than crashing on
        the lookup.
        """
        oid = self.to_object_id(plan_id)
        result = await self._collection.delete_one({"_id": oid})
        if result.deleted_count == 0:
            raise NotFoundError(
                message_en=f"Plan {plan_id} not found",
                details={"plan_id": plan_id},
            )


__all__ = ["PlanRepository"]
