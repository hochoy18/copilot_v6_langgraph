"""Tests for `PlanRepository` (T06 / #7; restructured by T17 / #15).

Acceptance criteria for T17: "nodes / edges / tool_snapshots 字段"
and "手动构造 Plan 可存可读". A hand-built Plan with all three
fields must survive the round trip verbatim — the snapshot freeze
(ADR-0027) is the point of the document. Structural invariants
(tool refs, DAG-ness) are pinned in `test_plan_schema.py`; this file
covers the persistence seam.
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
    PlanEdge,
    PlanNode,
    PlanUpdate,
    ToolSnapshot,
)
from app.repositories.plans import PlanRepository


@pytest.fixture
async def repo() -> PlanRepository:
    """A fresh `PlanRepository` against an isolated in-memory DB."""
    db = AsyncMongoMockClient()["copilot_plan_test"]
    await init_database(db)
    return PlanRepository(db)


def _snapshot(**overrides: object) -> ToolSnapshot:
    base: dict[str, Any] = {
        "tool_id": str(ObjectId()),
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
    return ToolSnapshot(**base)


def _node(**overrides: object) -> PlanNode:
    base: dict[str, Any] = {
        "node_id": "n1",
        "tool": "list_customers",
        "parameters": {"region": "emea"},
        "notes": "Q3 lookups",
    }
    base.update(overrides)
    return PlanNode(**base)


def _plan_input(**overrides: Any) -> PlanCreate:
    base: dict[str, Any] = {
        "conversation_id": str(ObjectId()),
        "turn_id": str(ObjectId()),
        "status": "pending",
        "nodes": [_node()],
        "edges": [],
        "tool_snapshots": [_snapshot()],
    }
    base.update(overrides)
    return PlanCreate(**base)


class TestPlanCreate:
    """`create` — persists the manual T17 shape; rejects empty DAGs."""

    @pytest.mark.asyncio
    async def test_manual_plan_with_all_three_fields_round_trips(
        self, repo: PlanRepository
    ) -> None:
        """The T17 acceptance criterion: a hand-built Plan carrying
        `nodes` / `edges` / `tool_snapshots` stores and reads back."""
        plan = await repo.create(_plan_input())
        fetched = await repo.get(plan.id)

        assert [n.node_id for n in fetched.nodes] == ["n1"]
        assert fetched.edges == []
        assert fetched.tool_snapshots[0].name == "list_customers"
        assert fetched.nodes[0].tool == "list_customers"

    @pytest.mark.asyncio
    async def test_create_persists_top_level_tool_snapshots(
        self, repo: PlanRepository
    ) -> None:
        """Snapshots live on the Plan document, not on nodes (ADR-0027).

        The persisted raw doc is asserted too — replays read the
        frozen schema from `plan.tool_snapshots`, so the shape must
        match the ADR wording (`plan.tool_snapshots: [...]`).
        """
        snap = _snapshot(name="list_customers", risk_level="write")
        plan = await repo.create(
            _plan_input(
                tool_snapshots=[snap],
                nodes=[_node(parameters={"region": "apac"})],
            )
        )
        raw = await repo._collection.find_one({"_id": ObjectId(plan.id)})
        assert raw is not None
        assert raw["tool_snapshots"][0]["risk_level"] == "write"
        assert raw["tool_snapshots"][0]["tool_id"] == snap.tool_id
        assert "tool_snapshot" not in raw["nodes"][0]
        assert raw["nodes"][0]["tool"] == "list_customers"

    @pytest.mark.asyncio
    async def test_create_with_multi_node_dag_and_shared_snapshot(
        self, repo: PlanRepository
    ) -> None:
        """Two nodes, one shared snapshot, one edge — the canonical
        multi-Tool Plan shape."""
        email = _snapshot(
            name="send_email",
            description="Send an email.",
            risk_level="write",
            http_method="POST",
            http_url_template="https://api.example.com/emails",
        )
        plan = await repo.create(
            _plan_input(
                nodes=[
                    _node(node_id="n1"),
                    _node(node_id="n2", tool="send_email", parameters={"to": "f@x"}),
                    _node(node_id="n3", parameters={"region": "apac"}),
                ],
                edges=[
                    PlanEdge(source="n1", target="n2"),
                    PlanEdge(source="n1", target="n3"),
                ],
                tool_snapshots=[_snapshot(), email],
            )
        )
        assert [n.node_id for n in plan.nodes] == ["n1", "n2", "n3"]
        assert [(e.source, e.target) for e in plan.edges] == [
            ("n1", "n2"),
            ("n1", "n3"),
        ]
        # The shared `list_customers` snapshot serves both n1 and n3.
        assert [s.name for s in plan.tool_snapshots] == ["list_customers", "send_email"]

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
    """`get`, `get_latest_for_conversation`, list variants."""

    @pytest.mark.asyncio
    async def test_get_by_id(self, repo: PlanRepository) -> None:
        created = await repo.create(_plan_input())
        fetched = await repo.get(created.id)
        assert fetched.id == created.id

    @pytest.mark.asyncio
    async def test_get_returns_canonical_shape_with_dag(
        self, repo: PlanRepository
    ) -> None:
        """`get` returns the full T17 trio so the React Flow renderer
        (T19) can lay out the graph without re-deriving anything.

        The T05 review noted a `get_in_db` seam was redundant for
        Plan — every persisted field is canonical. The audit
        subscriber (T42) reaches for `get` directly.
        """
        created = await repo.create(_plan_input())
        fetched = await repo.get(created.id)
        assert fetched.nodes[0].tool == "list_customers"
        assert fetched.tool_snapshots[0].name == "list_customers"
        assert "edges" in fetched.model_dump()

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
    async def test_list_by_conversation_returns_newest_first(
        self, repo: PlanRepository
    ) -> None:
        conv_id = str(ObjectId())
        first = await repo.create(_plan_input(conversation_id=conv_id))
        await asyncio.sleep(0.005)
        newest = await repo.create(_plan_input(conversation_id=conv_id))
        plans = await repo.list_by_conversation(conv_id)
        assert [p.id for p in plans] == [newest.id, first.id]

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
        """`record_edit` is the dedicated HITL edit path (ADR-0019).

        Only `parameters` / `notes` change — the T17 shape means the
        frozen `tool_snapshots` and the `edges` topology are never
        touched by an edit.
        """
        created = await repo.create(_plan_input(status="pending"))
        edited_nodes = [
            _node(parameters={"region": "amer"}, notes="Edited by finance user.")
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
        # Topology + snapshots survive the edit untouched.
        assert [s.name for s in updated.tool_snapshots] == ["list_customers"]
        assert updated.edges == []

    @pytest.mark.asyncio
    async def test_record_edit_rejects_repointed_tool(
        self, repo: PlanRepository
    ) -> None:
        """ADR-0019: only `parameters` / `notes` are editable. Pointing
        a node at another Tool (frozen or not) breaks the snapshot
        binding (ADR-0027) and is rejected as a repository-level
        `ValidationError`, with the stored doc untouched."""
        created = await repo.create(_plan_input(status="pending"))
        with pytest.raises(ValidationError, match="cannot change which Tool"):
            await repo.record_edit(created.id, [_node(tool="not_frozen")], {})
        untouched = await repo.get(created.id)
        assert untouched.nodes[0].tool == "list_customers"
        assert untouched.status == "pending"

    @pytest.mark.asyncio
    async def test_record_edit_rejects_added_and_removed_nodes(
        self, repo: PlanRepository
    ) -> None:
        """ADR-0019: "不可增删节点" — the node set must match exactly.
        An added node rides an already-frozen snapshot, so the
        PlanBase invariants alone would NOT catch it; the set check
        must."""
        created = await repo.create(_plan_input(status="pending"))
        with pytest.raises(ValidationError, match="cannot add or remove nodes") as exc:
            await repo.record_edit(
                created.id,
                [_node(), _node(node_id="n9")],  # add n9
                {},
            )
        assert exc.value.details == {"added": ["n9"], "removed": []}
        with pytest.raises(ValidationError, match="cannot add or remove nodes"):
            await repo.record_edit(created.id, [], {})
        untouched = await repo.get(created.id)
        assert [n.node_id for n in untouched.nodes] == ["n1"]
        assert untouched.status == "pending"

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
