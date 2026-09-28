"""`PlanRepository` — CRUD for the `plans` collection.

T06 (#7) shipped the schema; T17 (#15) restructured it to the
`nodes` / `edges` / `tool_snapshots` trio. The Planner (T18/T25),
the Plan-edit endpoint (T26 / ADR-0019), and the Frontend React Flow
renderer (T19) reach for this seam.

Design notes:

* The document shape is the T17 (#15) trio — `nodes` / `edges` /
  `tool_snapshots` (ADR-0027). Snapshots live on the Plan, never in
  a side collection; the Worker replays a run by reading the node's
  intended `parameters` and the referenced snapshot's frozen schema
  from the same fetch. `PlanBase`'s model validator enforces the
  structural invariants (tool binding, DAG-ness) on every read and
  write — the repository only adds the "at least one node" rule.
* `set_status` is the dedicated path for the Plan lifecycle. The
  Worker (`plan_executions`) reaches for it as Plans move from
  `approved` → `executing` → `succeeded`/`failed`; audit log
  subscribers treat status transitions as discrete events without
  diffing arbitrary PATCHes.
* `record_edit` writes the diff between the original and the
  business-user-edited Plan (ADR-0019). Keeping the diff on the
  Plan itself means audit replays don't need a side-channel
  collection. The merged doc is re-validated before the write so a
  bad edit can never land.

The doc-parsing and post-insert refetch helpers come from
`app.repositories._common` — see that module for the rationale.
"""
from __future__ import annotations

from typing import Any, ClassVar

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import ValidationError as PydanticValidationError

from app.db.errors import NotFoundError, ValidationError
from app.db.indexes import PLANS
from app.db.schemas import (
    Plan,
    PlanCreate,
    PlanInDB,
    PlanNode,
    PlanStatus,
    PlanUpdate,
)
from app.repositories._common import doc_to_in_db, doc_to_read, refetch_after_insert
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
        edited_nodes: list[PlanNode],
        diff: dict[str, Any],
    ) -> Plan:
        """Apply a business-user edit (ADR-0019).

        ADR-0019: "可改参数,不可增删节点". The submitted node set must
        match the persisted one exactly — same `node_id`s, same `tool`
        per node — only `parameters` / `notes` may change. `edges` and
        `tool_snapshots` are never touched here, so the topology and
        the frozen definitions (ADR-0027) are immutable by
        construction. The merged post-edit doc is additionally
        validated against `PlanInDB` before the write as a catch-all
        for docs that drifted from the contract.

        The diff format is `{"by_node_id": {"param": {"before": …,
        "after": …}}}` — see ADR-0019 for the contract.
        """
        oid = self.to_object_id(plan_id)
        current = await self._collection.find_one({"_id": oid})
        if current is None:
            raise NotFoundError(
                message_en=f"Plan {plan_id} not found",
                details={"plan_id": plan_id},
            )
        frozen = {node["node_id"]: node["tool"] for node in current["nodes"]}
        submitted = {node.node_id: node.tool for node in edited_nodes}
        if submitted.keys() != frozen.keys():
            raise ValidationError(
                message_en="Plan edits cannot add or remove nodes (ADR-0019)",
                details={
                    "added": sorted(set(submitted) - set(frozen)),
                    "removed": sorted(set(frozen) - set(submitted)),
                },
            )
        repointed = {
            node.node_id: {"before": frozen[node.node_id], "after": node.tool}
            for node in edited_nodes
            if frozen[node.node_id] != node.tool
        }
        if repointed:
            raise ValidationError(
                message_en="Plan edits cannot change which Tool a node invokes (ADR-0027)",
                details={"repointed": repointed},
            )
        update_doc: dict[str, Any] = {
            "nodes": [node.model_dump() for node in edited_nodes],
            "edited_diff": diff,
            "status": "modified",
            "updated_at": self._now(),
        }
        try:
            doc_to_in_db({**current, **update_doc}, PlanInDB)
        except PydanticValidationError as exc:
            raise ValidationError(
                message_en="Edited Plan failed structural validation",
                details={
                    "errors": [err["msg"] for err in exc.errors(include_url=False)]
                },
            ) from exc
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
