"""Tests for `PlanRepository` (T06 / #7).

Acceptance criterion for T06: "Plan 含 tool_snapshots 字段". The
`PlanCreate` test embeds a node with a `tool_snapshot` and verifies
the persisted document carries the freeze. Plan-edits (ADR-0019)
are exercised through `record_edit`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.db.errors import InvalidIdError, NotFoundError, ValidationError
from app.db.init_db import init_database
from app.db.schemas import (
    PlanCreate,
    PlanNode,
    PlanNodeToolSnapshot,
    PlanUpdate,
)
from app.repositories.plans import PlanRepository


@pytest.fixture
async def repo() -> PlanRepository:
    """A fresh `PlanRepository` against an isolated in-memory DB."""
    db = AsyncMongoMockClient()["copilot_plan_test"]
    await init_database(db)
    return PlanRepository(db)


def _snapshot(**overrides: object) -> PlanNodeToolSnapshot:
    base: dict[str, Any] = {
        "name": "list_customers",
        "description": "List customers by region.",
        "risk_level": "read",
        "parameters_schema": {
            "type": "object",
            "properties": {"region": {"type": "string"}},
            "required": ["region"],
        },
        "http_method": "GET",
        "http_url_template": "https://api.example.com/customers?region={region}",
        "http_headers": {"Accept": "application/json"},
        "http_body_template": None,
    }
    base.update(overrides)
    return PlanNodeToolSnapshot(**base)


def _node(**overrides: object) -> PlanNode:
    base: dict[str, Any] = {
        "node_id": "n1",
        "tool_snapshot": _snapshot(),
        "parameters": {"region": "emea"},
        "notes": "Q3 lookups",
        "depends_on": [],
    }
    base.update(overrides)
    return PlanNode(**base)


def _plan_input(**overrides: Any) -> PlanCreate:
    base: dict[str, Any] = {
        "conversation_id": str(ObjectId()),
        "turn_id": str(ObjectId()),
        "status": "pending",
        "nodes": [_node()],
    }
    base.update(overrides)
    return PlanCreate(**base)


class TestPlanCreate:
    """`create` — accepts valid DAGs, rejects empty-DAG inputs."""

    @pytest.mark.asyncio
    async def test_create_embeds_tool_snapshot_on_each_node(
        self, repo: PlanRepository
    ) -> None:
        """The acceptance criterion: Plan carries tool_snapshots (ADR-0027)."""
        snap = _snapshot(name="list_customers", risk_level="write")
        plan = await repo.create(
            _plan_input(nodes=[_node(tool_snapshot=snap, parameters={"region": "apac"})])
        )
        assert plan.nodes[0].tool_snapshot.name == "list_customers"
        assert plan.nodes[0].tool_snapshot.risk_level == "write"
        # And on the raw Mongo doc — the snapshot persisted verbatim.
        raw = await repo._collection.find_one({"_id": ObjectId(plan.id)})
        assert raw is not None
        assert raw["nodes"][0]["tool_snapshot"]["name"] == "list_customers"

    @pytest.mark.asyncio
    async def test_create_with_multi_node_dag(
        self, repo: PlanRepository
    ) -> None:
        n1 = _node(node_id="n1", parameters={"region": "emea"})
        n2 = _node(
            node_id="n2",
            tool_snapshot=_snapshot(name="send_email"),
            parameters={"to": "finance@x"},
            depends_on=["n1"],
        )
        plan = await repo.create(_plan_input(nodes=[n1, n2]))
        assert [n.node_id for n in plan.nodes] == ["n1", "n2"]
        assert plan.nodes[1].depends_on == ["n1"]

    @pytest.mark.asyncio
    async def test_create_rejects_empty_node_list(
        self, repo: PlanRepository
    ) -> None:
        """An empty DAG has no audit value and breaks the React Flow renderer."""
        with pytest.raises(ValidationError):
            await repo.create(_plan_input(nodes=[]))

    @pytest.mark.asyncio
    async def test_create_returns_canonical_shape(
        self, repo: PlanRepository
    ) -> None:
        created = await repo.create(_plan_input())
        assert created.status == "pending"
        assert created.id
        assert ObjectId(created.id)
        assert isinstance(created.created_at, datetime)
        assert created.created_at == created.updated_at


class TestPlanRead:
    """`get`, `get_in_db`, `get_latest_for_conversation`, list variants."""

    @pytest.mark.asyncio
    async def test_get_by_id(self, repo: PlanRepository) -> None:
        created = await repo.create(_plan_input())
        fetched = await repo.get(created.id)
        assert fetched.id == created.id

    @pytest.mark.asyncio
    async def test_get_returns_canonical_shape_with_dag(
        self, repo: PlanRepository
    ) -> None:
        """`get` returns the Plan with its embedded DAG and `tool_snapshots`.

        The T05 review noted a `get_in_db` seam was redundant for
        Plan — every persisted field is canonical. The audit
        subscriber (T42) reaches for `get` directly.
        """
        created = await repo.create(_plan_input())
        fetched = await repo.get(created.id)
        assert fetched.nodes[0].tool_snapshot.name == "list_customers"

    @pytest.mark.asyncio
    async def test_get_missing_raises_not_found(self, repo: PlanRepository) -> None:
        with pytest.raises(NotFoundError):
            await repo.get(str(ObjectId()))

    @pytest.mark.asyncio
    async def test_get_invalid_id_raises_invalid_id(
        self, repo: PlanRepository
    ) -> None:
        with pytest.raises(InvalidIdError):
            await repo.get("not-an-objectid")

    @pytest.mark.asyncio
    async def test_get_latest_for_conversation_returns_newest(
        self, repo: PlanRepository
    ) -> None:
        conv_id = str(ObjectId())
        p1 = await repo.create(_plan_input(conversation_id=conv_id))
        await asyncio.sleep(0.005)
        p2 = await repo.create(_plan_input(conversation_id=conv_id))
        latest = await repo.get_latest_for_conversation(conv_id)
        assert latest.id == p2.id
        assert latest.id != p1.id

    @pytest.mark.asyncio
    async def test_get_latest_for_conversation_missing_raises(
        self, repo: PlanRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.get_latest_for_conversation(str(ObjectId()))

    @pytest.mark.asyncio
    async def test_list_by_turn_returns_attached_plans(
        self, repo: PlanRepository
    ) -> None:
        turn_id = str(ObjectId())
        p1 = await repo.create(_plan_input(turn_id=turn_id))
        p2 = await repo.create(_plan_input(turn_id=turn_id))
        # Off-turn Plan — must NOT appear.
        await repo.create(_plan_input(turn_id=str(ObjectId())))
        ours = await repo.list_by_turn(turn_id)
        assert {p.id for p in ours} == {p1.id, p2.id}

    @pytest.mark.asyncio
    async def test_list_by_status_filters_correctly(
        self, repo: PlanRepository
    ) -> None:
        await repo.create(_plan_input(status="pending"))
        await repo.create(_plan_input(status="pending"))
        await repo.create(_plan_input(status="executing"))
        pending = await repo.list_by_status("pending")
        assert len(pending) == 2
        assert {p.status for p in pending} == {"pending"}


class TestPlanUpdate:
    """`update`, `set_status`, `record_edit`."""

    @pytest.mark.asyncio
    async def test_update_bumps_updated_at(self, repo: PlanRepository) -> None:
        created = await repo.create(_plan_input())
        before = created.updated_at
        await asyncio.sleep(0.005)
        updated = await repo.update(
            created.id, PlanUpdate(status="approved")
        )
        assert updated.status == "approved"
        assert updated.updated_at > before

    @pytest.mark.asyncio
    async def test_update_missing_raises_not_found(
        self, repo: PlanRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.update(str(ObjectId()), PlanUpdate(status="approved"))

    @pytest.mark.asyncio
    async def test_set_status_transitions_lifecycle(
        self, repo: PlanRepository
    ) -> None:
        created = await repo.create(_plan_input(status="pending"))
        assert created.status == "pending"
        approved = await repo.set_status(created.id, "approved")
        assert approved.status == "approved"
        executing = await repo.set_status(created.id, "executing")
        assert executing.status == "executing"
        done = await repo.set_status(created.id, "succeeded")
        assert done.status == "succeeded"

    @pytest.mark.asyncio
    async def test_record_edit_writes_diff_and_flips_status_to_modified(
        self, repo: PlanRepository
    ) -> None:
        """`record_edit` is the dedicated HITL edit path (ADR-0019)."""
        created = await repo.create(_plan_input(status="pending"))
        edited_nodes = [
            {
                "node_id": "n1",
                "tool_snapshot": _snapshot().model_dump(),
                "parameters": {"region": "amer"},  # emea -> amer
                "notes": "Edited by finance user.",
                "depends_on": [],
            }
        ]
        diff = {
            "by_node_id": {
                "n1": {"parameters.region": {"before": "emea", "after": "amer"}}
            }
        }
        updated = await repo.record_edit(created.id, edited_nodes, diff)
        assert updated.status == "modified"
        assert updated.nodes[0].parameters == {"region": "amer"}
        assert updated.edited_diff == diff

    @pytest.mark.asyncio
    async def test_record_edit_missing_raises_not_found(
        self, repo: PlanRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.record_edit(str(ObjectId()), [], {})


class TestPlanDelete:
    """`delete` — admin hard-delete."""

    @pytest.mark.asyncio
    async def test_delete_removes_plan(self, repo: PlanRepository) -> None:
        created = await repo.create(_plan_input())
        await repo.delete(created.id)
        with pytest.raises(NotFoundError):
            await repo.get(created.id)

    @pytest.mark.asyncio
    async def test_delete_missing_raises_not_found(
        self, repo: PlanRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.delete(str(ObjectId()))
