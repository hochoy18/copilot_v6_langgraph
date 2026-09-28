"""Schema-level tests for the T17 (#15) Plan document structure.

T17 fixes the `plans` wire shape to the trio SPEC §Data model
promises: `nodes` / `edges` / `tool_snapshots` (ADR-0027). Everything
the Planner emits and the Worker consumes is validated here at the
model boundary — the repository tests in `test_plan_repository.py`
cover the persistence seam.

The invariants pinned below are the ones the downstream consumers
depend on:

* one frozen snapshot per distinct Tool, referenced by `name`
  (ADR-0027) — the Worker validates parameters and decides HITL from
  the snapshot, never from the live `tools` row;
* edges reference existing nodes and form a DAG (ADR-0012) — the
  LangGraph executor topologically sorts them, so a cycle would hang
  the run;
* ids and names are unique — duplicate `node_id`s make `edited_diff`
  keys ambiguous and duplicate snapshot names make `tool` references
  non-deterministic.
"""
from __future__ import annotations

from typing import Any

import pytest
from bson import ObjectId
from pydantic import ValidationError as PydanticValidationError

from app.db.schemas import PlanCreate, PlanEdge, PlanInDB, PlanNode, ToolSnapshot


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


def _plan(**overrides: Any) -> PlanCreate:
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


class TestToolSnapshot:
    """The frozen Tool definition embedded in `plan.tool_snapshots`."""

    def test_minimal_snapshot_defaults(self) -> None:
        """`tool_id` / `parameters_schema` / headers are optional at the edge.

        A hand-built Plan (the T17 acceptance criterion) should not
        have to restate defaults; `tool_id` is the only field T44
        needs for drift detection and stays optional so older
        shapes still parse.
        """
        snap = ToolSnapshot(
            name="echo",
            description="Echo the input.",
            risk_level="read",
            http_method="GET",
            http_url_template="https://api.example.com/echo",
        )
        assert snap.tool_id is None
        assert snap.parameters_schema == {}
        assert snap.http_headers == {}
        assert snap.http_body_template is None

    def test_rejects_unknown_fields(self) -> None:
        """`extra="forbid"` keeps the frozen shape closed — a snapshot
        that silently grows fields would break replay equality."""
        with pytest.raises(PydanticValidationError):
            _snapshot(status="active")  # live-Tool field must not leak in

    def test_rejects_unknown_risk_level(self) -> None:
        with pytest.raises(PydanticValidationError):
            _snapshot(risk_level="yolo")


class TestPlanNode:
    """A node references its Tool by snapshot name — no embedded snapshot."""

    def test_node_carries_only_invocation_fields(self) -> None:
        node = _node()
        dumped = node.model_dump()
        assert set(dumped) == {"node_id", "tool", "parameters", "notes"}

    def test_rejects_embedded_snapshot(self) -> None:
        """The T06 per-node embedding is gone: nodes are topology,
        definitions live in `tool_snapshots` (ADR-0027)."""
        with pytest.raises(PydanticValidationError):
            _node(tool_snapshot=_snapshot())

    def test_rejects_parameters_schema_override(self) -> None:
        """Parameters must validate against the *snapshot's* schema —
        a node carrying its own schema would fork the source of truth."""
        with pytest.raises(PydanticValidationError):
            _node(parameters_schema={"type": "object"})


class TestPlanEdge:
    def test_edge_is_source_target_pair(self) -> None:
        edge = PlanEdge(source="n1", target="n2")
        assert edge.model_dump() == {"source": "n1", "target": "n2"}

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(PydanticValidationError):
            PlanEdge(source="n1", target="n2", label="carries id")  # type: ignore[call-arg]


class TestPlanDagInvariants:
    """Model-level structural validation — the T17 schema contract."""

    def test_valid_single_node_plan_constructs(self) -> None:
        plan = _plan()
        assert [n.node_id for n in plan.nodes] == ["n1"]
        assert plan.edges == []
        assert [s.name for s in plan.tool_snapshots] == ["list_customers"]

    def test_multi_node_dag_constructs(self) -> None:
        plan = _plan(
            nodes=[
                _node(node_id="n1"),
                _node(node_id="n2", tool="send_email", parameters={"to": "x@y"}),
            ],
            edges=[PlanEdge(source="n1", target="n2")],
            tool_snapshots=[
                _snapshot(),
                _snapshot(
                    name="send_email",
                    description="Send an email.",
                    risk_level="write",
                    http_method="POST",
                    http_url_template="https://api.example.com/emails",
                ),
            ],
        )
        assert plan.nodes[1].tool == "send_email"
        assert plan.edges[0].target == "n2"

    def test_two_nodes_may_share_one_snapshot(self) -> None:
        """The same Tool called twice freezes one snapshot — ADR-0027
        binds the Plan to the definition, not to each call site."""
        plan = _plan(
            nodes=[
                _node(node_id="n1", parameters={"region": "emea"}),
                _node(node_id="n2", parameters={"region": "apac"}),
            ],
            edges=[PlanEdge(source="n1", target="n2")],
        )
        assert len(plan.tool_snapshots) == 1

    def test_diamond_dependencies_construct(self) -> None:
        plan = _plan(
            nodes=[_node(node_id=n) for n in ("n1", "n2", "n3", "n4")],
            edges=[
                PlanEdge(source="n1", target="n2"),
                PlanEdge(source="n1", target="n3"),
                PlanEdge(source="n2", target="n4"),
                PlanEdge(source="n3", target="n4"),
            ],
        )
        assert len(plan.edges) == 4

    def test_rejects_duplicate_node_ids(self) -> None:
        """`edited_diff` keys on `node_id` (ADR-0019) — duplicates make
        the audit diff ambiguous."""
        with pytest.raises(PydanticValidationError, match="duplicate node_id"):
            _plan(nodes=[_node(), _node()])

    def test_rejects_unknown_tool_reference(self) -> None:
        """Every node must bind to a frozen snapshot (ADR-0027)."""
        with pytest.raises(PydanticValidationError, match="unknown tool snapshot"):
            _plan(nodes=[_node(tool="does_not_exist")])

    def test_rejects_duplicate_snapshot_names(self) -> None:
        """`tool` references resolve by name — two snapshots sharing a
        name make binding non-deterministic."""
        with pytest.raises(PydanticValidationError, match="duplicate tool_snapshot"):
            _plan(tool_snapshots=[_snapshot(), _snapshot()])

    def test_rejects_edge_to_unknown_node(self) -> None:
        with pytest.raises(PydanticValidationError, match="unknown node"):
            _plan(edges=[PlanEdge(source="n1", target="ghost")])

    def test_rejects_edge_from_unknown_node(self) -> None:
        with pytest.raises(PydanticValidationError, match="unknown node"):
            _plan(edges=[PlanEdge(source="ghost", target="n1")])

    def test_rejects_self_loop(self) -> None:
        with pytest.raises(PydanticValidationError, match="self-referencing"):
            _plan(edges=[PlanEdge(source="n1", target="n1")])

    def test_rejects_duplicate_edges(self) -> None:
        """React Flow would render the dependency twice; the topology
        is a set."""
        with pytest.raises(PydanticValidationError, match="duplicate edge"):
            _plan(
                nodes=[_node(node_id="n1"), _node(node_id="n2")],
                edges=[
                    PlanEdge(source="n1", target="n2"),
                    PlanEdge(source="n1", target="n2"),
                ],
            )

    def test_rejects_cycle(self) -> None:
        """ADR-0012's executor topologically sorts the Plan; a cycle
        would hang the run before any HITL checkpoint."""
        with pytest.raises(PydanticValidationError, match="cycle"):
            _plan(
                nodes=[_node(node_id="n1"), _node(node_id="n2")],
                edges=[
                    PlanEdge(source="n1", target="n2"),
                    PlanEdge(source="n2", target="n1"),
                ],
            )

    def test_rejects_longer_cycle_inside_valid_dag_prefix(self) -> None:
        """A cycle among a subset of nodes is still a cycle even when
        the rest of the graph is a well-formed DAG."""
        with pytest.raises(PydanticValidationError, match="cycle"):
            _plan(
                nodes=[_node(node_id=n) for n in ("n1", "n2", "n3", "n4")],
                edges=[
                    PlanEdge(source="n1", target="n2"),
                    PlanEdge(source="n2", target="n3"),
                    PlanEdge(source="n3", target="n2"),
                    PlanEdge(source="n3", target="n4"),
                ],
            )

    def test_invariants_rechecked_on_read_shapes(self) -> None:
        """The validators live on the shared base, so a Mongo doc that
        drifted from the contract fails loudly instead of surfacing a
        half-valid Plan to the API."""
        doc = _plan().model_dump()
        doc.update(
            {
                "_id": str(ObjectId()),
                "created_at": "2026-09-28T00:00:00Z",
                "updated_at": "2026-09-28T00:00:00Z",
                "edited_diff": None,
            }
        )
        parsed = PlanInDB.model_validate(doc)
        assert parsed.nodes[0].node_id == "n1"

        doc["edges"] = [{"source": "n1", "target": "ghost"}]
        with pytest.raises(PydanticValidationError, match="unknown node"):
            PlanInDB.model_validate(doc)
