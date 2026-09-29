"""`PlanDagRunner` — T28 / #24 / ADR-0012.

The runtime that walks a Plan's DAG of Tool invocations in
topological order, running independent sibling nodes concurrently
and gating downstream levels until their predecessors finish.

Built on `langgraph.graph.StateGraph` so the parallel semantics
match what the ADR describes — no hand-rolled scheduler to keep
in sync with future LangGraph improvements. The graph's only
state channel is the per-node outcome map; the Worker's
side-effecting concerns (audit log writes, `PlanExecution` row
updates, Plan lifecycle flips) live one layer up in
`PlanExecutor._run_one_node`, which the runner invokes as an
async callback.

Failure model (issue #24 acceptance criteria):

* **Concurrent siblings.** A node with multiple incoming edges waits
  for every predecessor to terminalise before it dispatches.
* **Isolated failures.** A Worker's `HITLRequiredError` /
  `SchemaViolationError` / unexpected `Exception` is caught inside
  the LangGraph node function and recorded as a `failed` outcome;
  the graph keeps running so parallel siblings finish.
* **Downstream skip.** A downstream node whose upstream is `failed`
  short-circuits to a `skipped` outcome without invoking the
  Worker — matches ADR-0012's "任意并行节点失败 → 整个分支标记失败,
  等待 HITL 决策".

Why LangGraph (rather than `asyncio.gather` over a topological
level list): the user-facing ticket says "LangGraph StateGraph
并行分支" — the project standardised on LangGraph as the
parallel-scheduling primitive, and the StateGraph API gives us
the merge-reducer semantics for sibling writes "for free" without
hand-managing a per-step bookkeeping channel.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.db.schemas import Plan, PlanNode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public data shapes
# ---------------------------------------------------------------------------


# Status literals kept aligned with `PlanNodeStatus` (`app.db.schemas`) —
# the executor persists the runner's outcome directly onto
# `PlanExecution.node_results`, so the two vocabularies must match. We
# don't import the Literal type here to avoid a circular import between
# `app.db.schemas` (pydantic models) and this module — the conversion
# is enforced at the executor seam.
NodeRunStatus = Literal["succeeded", "failed", "skipped"]


@dataclass(frozen=True)
class NodeRunOutcome:
    """One node's terminal state after the runner passes through it.

    `request` / `response` are the Worker's outgoing HTTP request
    and parsed upstream response for `succeeded` / `failed`
    outcomes; both are `None` for `skipped` because the Worker
    never ran. `error_envelope` is the typed failure payload (from
    the Worker or from a structural failure) and is `None` on
    success / skip. `retry_count` survives so the executor's audit
    row can record the Worker's retry budget.
    """

    status: NodeRunStatus
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    error_envelope: dict[str, Any] | None = None
    started_at: Any | None = None
    finished_at: Any | None = None
    retry_count: int = 0


# Callable signature the executor hands the runner — the executor
# owns the Worker call, audit log write, and `PlanExecution` row
# update; the runner just decides *when* to invoke it. Per-node
# exceptions are absorbed by the runner, so the callable itself
# should return a `NodeRunOutcome` (it does NOT raise).
RunOneNode = Callable[[PlanNode], Awaitable[NodeRunOutcome]]


# ---------------------------------------------------------------------------
# Internal LangGraph state shape
# ---------------------------------------------------------------------------


# Parallel siblings update the same `outcomes` channel in the same
# step — LangGraph needs a reducer to merge the partial maps
# instead of complaining about concurrent writes (`INVALID_CONCURRENT_
# GRAPH_UPDATE`). The merge keeps later keys winning on collision;
# we never produce overlapping keys at the same step in practice
# (each node writes its own `node_id` once).
def _merge_outcome_dicts(
    left: dict[str, NodeRunOutcome],
    right: dict[str, NodeRunOutcome],
) -> dict[str, NodeRunOutcome]:
    return {**left, **right}


class _RunnerState(TypedDict, total=False):
    """State carried through one `ainvoke` pass.

    `outcomes` is keyed by Plan `node_id` and is the only channel
    the graph nodes mutate. The merge reducer is `_merge_outcome_dicts`.
    """

    outcomes: Annotated[dict[str, NodeRunOutcome], _merge_outcome_dicts]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class PlanDagRunner:
    """Drives one Plan's DAG with concurrent siblings (T28 / ADR-0012).

    Stateless beyond the `run_node` callback. Build a fresh runner
    per `PlanExecutor.execute_plan` call — the graph itself is
    per-Plan (its edge topology is the Plan's edge list), so caching
    across calls would need a plan-shape key the executor doesn't
    expose. The graph-compile cost is small (one Plan has at most
    `MAX_NODES_PER_PLAN` nodes — see `app.planner.planner`).
    """

    def __init__(self, *, run_node: RunOneNode) -> None:
        self._run_node = run_node

    async def run(self, plan: Plan) -> dict[str, NodeRunOutcome]:
        """Walk `plan` and return `{node_id: NodeRunOutcome}`.

        The runner always returns an outcome for every node in the
        Plan — including downstream nodes that were skipped because
        an upstream failed. Callers (the executor) iterate the
        returned dict to write per-node audit / execution rows.
        """
        graph = self._build_graph(plan)
        compiled = graph.compile()
        # `ainvoke` is typed against the schema with `dict[Never,
        # Never]` for the channel-merge intermediate; the ignore
        # captures a real-but-narrow quirk of the 0.6.x typing
        # rather than a substantive shape problem.
        final_state = await compiled.ainvoke({"outcomes": {}})  # type: ignore[arg-type]
        outcomes: dict[str, NodeRunOutcome] = final_state["outcomes"]
        return outcomes

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self, plan: Plan) -> StateGraph:  # type: ignore[type-arg]
        """Build the LangGraph `StateGraph` for one Plan.

        Edge topology mirrors `plan.edges`; root nodes (no incoming
        edge) get `START -> node`; every node connects to `END` if
        it has no outgoing edges. The graph is acyclic by
        `PlanBase._validate_structure`, so Kahn's-level checks aren't
        needed here — LangGraph itself walks the topology.
        """
        # LangGraph's `StateGraph` generic parameters are
        # `[StateT, ContextT, InputT, OutputT]`. We only need
        # `StateT` (the schema) — the rest default to `None` / the
        # state schema. The `type: ignore[type-arg]` keeps mypy's
        # `disallow-any-explicit` off the call site for the
        # unused generic parameters, which is a LangGraph typing
        # quirk rather than a substantive shape problem.
        graph: StateGraph = StateGraph(_RunnerState)  # type: ignore[type-arg]

        # Pre-compute predecessor lists so each LangGraph node
        # function can decide whether its upstream failed (→ skip
        # the Worker call) without re-walking the Plan.
        predecessors: dict[str, list[str]] = {n.node_id: [] for n in plan.nodes}
        outgoing: dict[str, list[str]] = {n.node_id: [] for n in plan.nodes}
        for edge in plan.edges:
            outgoing[edge.source].append(edge.target)
            predecessors[edge.target].append(edge.source)

        for node in plan.nodes:
            graph.add_node(  # type: ignore[call-overload]
                node.node_id,
                _make_node_fn_for(
                    self._run_node,
                    node,
                    predecessors[node.node_id],
                ),
            )

            # Roots → START; leaves → END. The downstream level gate
            # is automatic: LangGraph only fires a node after every
            # incoming edge has resolved.
            if not predecessors[node.node_id]:
                graph.add_edge(START, node.node_id)
            if not outgoing[node.node_id]:
                graph.add_edge(node.node_id, END)

        # Internal edges mirror `plan.edges`. Without them a node
        # with both incoming and outgoing Plan edges (a diamond
        # interior, a linear chain's middle) is orphaned — START
        # points at the root, END points at the leaf, but the body
        # nodes have no edges between them.
        for edge in plan.edges:
            graph.add_edge(edge.source, edge.target)

        return graph


# ---------------------------------------------------------------------------
# Internal node-function factory
# ---------------------------------------------------------------------------


def _make_node_fn_for(
    run_node: RunOneNode,
    node: PlanNode,
    predecessors: list[str],
) -> Callable[[_RunnerState], Awaitable[dict[str, Any]]]:
    """Build the LangGraph node function for one Plan node.

    The wrapper handles the upstream-failure short-circuit and the
    exception→`failed`-outcome translation that ADR-0012 asks for.
    Without it, a single Worker's exception would abort the entire
    graph (LangGraph's default) — which would block parallel
    siblings from finishing.
    """
    node_id = node.node_id
    # Freeze the tuple so the closure survives any later mutation of
    # the Plan graph; the runner builds a fresh graph per call so
    # this is more about defensive immutability than hot-path perf.
    predecessor_ids = tuple(predecessors)

    async def _fn(state: _RunnerState) -> dict[str, Any]:
        # ADR-0012: a downstream node whose upstream is `failed`
        # short-circuits to `skipped` without invoking the Worker.
        outcomes = state.get("outcomes", {})
        for predecessor_id in predecessor_ids:
            predecessor = outcomes.get(predecessor_id)
            if predecessor is not None and predecessor.status == "failed":
                return {
                    "outcomes": {
                        node_id: NodeRunOutcome(status="skipped"),
                    },
                }

        try:
            outcome = await run_node(node)
        except Exception as exc:  # noqa: BLE001 — failure isolation
            # Defensive: `run_node` (the executor's `_run_one_node`)
            # already absorbs known Worker errors and returns a
            # `failed` outcome. Catching here is a backstop for
            # *truly* unexpected runtime errors so the graph keeps
            # running siblings; the log line is what a future
            # operator reaches for when a Plan fails oddly. Narrowing
            # to typed exceptions would risk masking programming bugs
            # in `_run_one_node` itself, so the broad catch stays —
            # but it's logged, not silent.
            logger.exception(
                "PlanDagRunner caught unexpected error in node %s; "
                "recording as failed and continuing.",
                node_id,
            )
            return {
                "outcomes": {
                    node_id: NodeRunOutcome(
                        status="failed",
                        error_envelope={
                            "code": "unexpected_error",
                            "message_en": f"{type(exc).__name__}: {exc}",
                        },
                    ),
                },
            }
        return {"outcomes": {node_id: outcome}}

    return _fn


__all__ = ["NodeRunOutcome", "NodeRunStatus", "PlanDagRunner", "RunOneNode"]
