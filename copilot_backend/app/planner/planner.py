"""`ToolPlanner` — the Planner LLM call (T18 / #16, T25 / #22, ADR-0004 / ADR-0013).

Implements the "Planner LLM 调用" step of SPEC §Planner 执行流: render
the Langfuse `planner` Prompt with the active-Tool catalog and the
user instruction, invoke the ChatModel, and parse the strict-JSON
answer back into Tool calls bound to live `Tool` rows.

Behavioural rules worth knowing before editing:

* **The catalog is the only Tool surface the LLM sees.** Only
  `active` Tools reach this call (ADR-0018 — `draft` / `disabled`
  are invisible to the Planner), and a name the model returns that
  isn't in the catalog is *dropped with a warning*, never bound to a
  guessed row. A hallucinated Tool must not become an executable
  Plan.
* **All failure paths are typed, none silent.** Unparseable output
  raises `LLMGenerationError`; the caller (`PlannerService`) owns the
  degradation decision, same split as T16's description generator vs
  the import route. The Prompt ladder (ADR-0013) stays invisible
  here — `PromptProvider.get_prompt` already degrades fetch → cache
  → bootstrap.
* **T25 emits multi-node Plans through the Prompt.** The bootstrap
  template asks for `nodes` (N invocations) plus `edges`
  (`{"source": i, "target": j}` 1-based pairs, ADR-0012 data-
  dependency edges); the parser accepts up to N nodes and a matching
  edge list. Edge endpoints are positional indices into the LLM's
  own `nodes` array — the service maps them onto the assigned
  `n{index}` `node_id`s once they survive validation.
* **Edges are validated with the same "drop-with-warning, never bind
  to a guessed row" rule.** Self-loops, unknown indices, duplicate
  pairs, and cycle-closing edges are silently dropped (with a
  warning) so a single malformed edge cannot reject an otherwise
  valid Plan. The `PlanBase` validator still re-checks the surviving
  edges at persistence time as a defence-in-depth guard.
* **Parameters are not schema-validated here.** The Worker validates
  against the frozen `parameters_schema` before invoking (ADR-0020,
  T21 / T34); a half-filled argument set is reviewable in the HITL
  preview (ADR-0004) and remains editable (ADR-0019, T26).

The chat model arrives through a factory exactly like
`ToolDescriptionGenerator`'s: `ChatOpenAI` construction needs a
configured key, so an unconfigured backend stays constructible and
`ready` tells callers before they try.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.db.schemas import Tool
from app.llm.errors import LLMGenerationError
from app.llm.output import content_to_text, extract_json_object
from app.llm.prompts import PLANNER_PROMPT, PromptProvider, render_template
from app.settings import Settings
from app.tools.description_generator import summarize_parameters

# Upper bound on Tools rendered into the catalog. Beyond it the tail
# is dropped and the drop is announced in `PlanIntent.warnings` —
# same "no silent caps" rule as T16's `MAX_DESCRIPTIONS_PER_IMPORT`.
# MVP registries are far below this; a retrieval-based candidate
# selection is the T25+ conversation.
MAX_TOOLS_IN_CATALOG = 50

# Per-line cap on the Tool description inside the catalog.
# `ToolBase.description` allows 4096 chars; the Planner mostly needs
# the first business sentence, and 300 keeps a 50-Tool catalog under
# a sane prompt budget.
_MAX_DESCRIPTION_IN_CATALOG = 300

# Upper bound on the Planner-authored Plan DAG size — T25 / #22.
# A bound here keeps a hallucinated `"nodes": [...9999 entries...]`
# from turning one user Turn into a Plan the executor hangs on.
# MVP Plans are <10 nodes; the cap leaves headroom without inviting
# unbounded generation.
MAX_NODES_PER_PLAN = 50


@dataclass(frozen=True, slots=True)
class PlannedNode:
    """One Planner-chosen Tool invocation, bound to its live Tool row.

    Deliberately *not* a `PlanNode`: the Planner answers with Tool
    names, the service freezes snapshots and assigns stable
    `node_id`s. Keeping the LLM-side shape separate stops a future
    Plan-doc refactor from rippling into the parse contract.
    """

    tool: Tool
    parameters: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True, slots=True)
class PlannedEdge:
    """One data-dependency edge in the LLM-side contract (T25 / #22).

    `source` / `target` are 1-based indices into the LLM's `nodes`
    array, NOT the assigned `node_id`s — those land at persistence
    time (`PlannerService._persist_plan` maps the positions onto the
    `n{index}` convention). Positional indices keep the LLM contract
    terse and side-step stringly-typed mistakes like `"n1"` / `"N1"`.
    """

    source: int
    target: int


@dataclass(frozen=True, slots=True)
class PlanIntent:
    """Parsed Planner output: bound nodes, edges, and non-fatal warnings.

    Empty `nodes` is a legitimate answer — ADR-0004 lets the Planner
    skip Plan generation when no Tool is involved (smalltalk, or a
    required parameter it could not resolve). `edges` are positional
    (see `PlannedEdge`); the service resolves them to `node_id`s.
    `warnings` explain *why* nodes / edges were dropped when the drop
    was not the model's choice (unknown Tool names, catalog
    truncation, malformed edges, cycles).
    """

    nodes: list[PlannedNode]
    edges: list[PlannedEdge] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ToolPlanner:
    """Turns one user instruction into bound Tool calls.

    One instance lives per process (built in the lifespan alongside
    the description generator); `plan` is the only public method.
    Tests hand in a fake `BaseChatModel` via `chat_model_factory`.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        prompt_provider: PromptProvider,
        chat_model_factory: Callable[[], BaseChatModel],
    ) -> None:
        self._settings = settings
        self._prompts = prompt_provider
        self._model_factory = chat_model_factory
        self._model: BaseChatModel | None = None

    @property
    def ready(self) -> bool:
        """Cheap "is the LLM configured" check — never touches the network."""
        return bool(self._settings.llm_base_url and self._settings.llm_api_key)

    async def plan(self, instruction: str, tools: Sequence[Tool]) -> PlanIntent:
        """Run one Planner turn against the given catalog.

        Args:
            instruction: the user's natural-language message.
            tools: the `active` Tool rows the model may choose from.

        Raises:
            LLMConfigurationError: provider refused by config (ADR-0016)
                — surfaced by the factory on first use.
            PromptUnavailableError: no `planner` template on any rung.
            LLMGenerationError: transport failure or output the JSON
                contract can't be honoured.
        """
        template = await self._prompts.get_prompt(PLANNER_PROMPT)
        prompt_text = render_template(
            template.text,
            {
                "tools": render_tool_catalog(tools),
                "input": instruction,
            },
        )

        model = self._chat_model()
        try:
            response = await model.ainvoke(prompt_text)
        except LLMGenerationError:
            raise
        except Exception as exc:  # langchain wraps provider errors variably
            raise LLMGenerationError(
                message_en=f"Planner LLM call failed: {exc}",
                details={"error_type": type(exc).__name__},
            ) from exc

        return parse_planner_output(content_to_text(response.content), tools)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _chat_model(self) -> BaseChatModel:
        """Lazily build (and cache) the configured chat model."""
        if self._model is None:
            self._model = self._model_factory()
        return self._model


# ---------------------------------------------------------------------------
# Module-level pure helpers — importable for targeted tests
# ---------------------------------------------------------------------------


def render_tool_catalog(tools: Sequence[Tool]) -> str:
    """Render the Planner's Tool catalog for `{{tools}}`.

    One line per Tool: `name`, the LLM-facing description (clipped),
    and the same parameter digest T16 uses for description prompts.
    Returns a placeholder line when the registry has no active Tools
    so the prompt never presents an empty catalog silently.
    """
    if not tools:
        return "(当前没有可用 Tool / no active tools)"
    lines: list[str] = []
    for tool in tools[:MAX_TOOLS_IN_CATALOG]:
        description = tool.description.strip()[:_MAX_DESCRIPTION_IN_CATALOG]
        lines.append(
            f"- {tool.name}: {description} | 参数: "
            f"{summarize_parameters(tool.parameters_schema)}"
        )
    if len(tools) > MAX_TOOLS_IN_CATALOG:
        lines.append(
            f"(目录已截断: 仅前 {MAX_TOOLS_IN_CATALOG} 个 Tool, "
            f"共 {len(tools)} 个)"
        )
    return "\n".join(lines)


def parse_planner_output(content: str, tools: Sequence[Tool]) -> PlanIntent:
    """Parse the `{"nodes": [...], "edges": [...]}` contract (T25 / #22).

    Binding is exact-match against the catalog (the active set):
    unknown names are dropped with a warning listing them, so a
    hallucinated Tool can never reach the HITL preview as an
    executable node. `parameters` must be a JSON object — anything
    else degrades that node's parameters to `{}` with a warning
    rather than rejecting the whole Plan.

    Edges are positional (`{"source": i, "target": j}` 1-based
    indices into the LLM's `nodes` array). The validation ladder is
    "drop-with-warning, never bind to a guessed row": self-loops,
    unknown indices, duplicate pairs, and cycle-closing edges are
    silently removed from the Plan and surfaced as warnings — one
    bad edge cannot reject an otherwise valid Plan. `PlanBase`'s
    validator still re-checks the surviving edges at persistence
    time as a defence-in-depth guard (ADR-0012: a cycle would hang
    the executor).

    Raises:
        LLMGenerationError: output is not JSON, or `nodes` is missing
            / not a list, or an entry is not an object with a string
            `tool`, or `edges` is present but not a list, or an edge
            entry lacks an integer `source` / `target`.
    """
    by_name: dict[str, Tool] = {tool.name: tool for tool in tools}
    payload = extract_json_object(content)
    if payload is None:
        raise LLMGenerationError(
            message_en="Planner output is not the JSON contract",
            details={"reason": "no JSON object found"},
        )

    # `nodes` must be present: a well-formed answer that lacks it (a
    # hallucinated wrapper key, a model that "forgot" the contract) is
    # indistinguishable from legitimate smalltalk if we tolerate it —
    # so the contract stays strict and the failure is typed + visible.
    if "nodes" not in payload:
        raise LLMGenerationError(
            message_en="Planner output is not the JSON contract",
            details={"reason": "'nodes' key missing"},
        )
    raw_nodes = payload["nodes"]
    if not isinstance(raw_nodes, list):
        raise LLMGenerationError(
            message_en="Planner output is not the JSON contract",
            details={"reason": "'nodes' is not a list"},
        )

    warnings: list[str] = []

    # Same "no silent caps" rule as `MAX_TOOLS_IN_CATALOG`: a runaway
    # LLM that returns thousands of nodes would turn one user Turn
    # into a Plan the executor hangs on. Truncate + announce.
    if len(raw_nodes) > MAX_NODES_PER_PLAN:
        warnings.append(
            f"Planner 返回的节点数({len(raw_nodes)})超过上限 {MAX_NODES_PER_PLAN}, "
            "已截断到前 "
            f"{MAX_NODES_PER_PLAN} 个节点。"
        )
        raw_nodes = raw_nodes[:MAX_NODES_PER_PLAN]

    nodes: list[PlannedNode] = []
    unknown: list[str] = []
    for entry in raw_nodes:
        if not isinstance(entry, dict) or not isinstance(entry.get("tool"), str):
            raise LLMGenerationError(
                message_en="Planner output is not the JSON contract",
                details={"reason": "a node lacks a string 'tool'"},
            )
        name = entry["tool"].strip()
        tool = by_name.get(name)
        if tool is None:
            if name not in unknown:
                unknown.append(name)
            continue
        parameters = entry.get("parameters")
        if parameters is None:
            parameters = {}
        elif not isinstance(parameters, dict):
            warnings.append(
                f"Tool '{name}' 的参数不是 JSON 对象, 已按无参数处理。"
            )
            parameters = {}
        notes = entry.get("notes")
        nodes.append(
            PlannedNode(
                tool=tool,
                parameters=parameters,
                notes=notes if isinstance(notes, str) else "",
            )
        )

    if unknown:
        warnings.append(
            "Planner 返回了目录之外的 Tool, 已忽略: "
            + ", ".join(unknown)
            + "。"
        )

    edges = _parse_edges(payload.get("edges"), node_count=len(nodes), warnings=warnings)

    return PlanIntent(nodes=nodes, edges=edges, warnings=warnings)


def _parse_edges(
    raw_edges: object,
    *,
    node_count: int,
    warnings: list[str],
) -> list[PlannedEdge]:
    """Parse the LLM-side `edges` array into validated `PlannedEdge`s.

    Runs the same drop-with-warning ladder as `parse_planner_output`
    for nodes: unknown indices, self-loops, duplicates, and cycle-
    closing edges are silently dropped (each with a warning) so one
    bad edge never rejects an otherwise valid Plan. A missing /
    `None` `edges` key is treated as `[]` — the T18 contract never
    had one, so a model still on the old prompt is not penalised.
    """
    if raw_edges is None:
        return []
    if not isinstance(raw_edges, list):
        raise LLMGenerationError(
            message_en="Planner output is not the JSON contract",
            details={"reason": "'edges' is not a list"},
        )
    # Per-reason drop bucket — warnings are batched at the end so a
    # batch of self-loops produces one message, not N. Keys match the
    # human-readable reason emitted below; values are the dropped
    # `source -> target` pair labels.
    drops: dict[str, list[str]] = {
        "self_loop": [],
        "unknown_index": [],
        "duplicate": [],
        "cycle_closer": [],
    }

    accepted: list[PlannedEdge] = []
    seen: set[tuple[int, int]] = set()
    # Forward adjacency of already-accepted edges, fed to the per-edge
    # cycle check. Stays small: capped by `MAX_NODES_PER_PLAN`, and
    # only surviving edges land here.
    outgoing: dict[int, list[int]] = {}

    for entry in raw_edges:
        if not isinstance(entry, dict):
            raise LLMGenerationError(
                message_en="Planner output is not the JSON contract",
                details={"reason": "an edge is not an object"},
            )
        raw_source = entry.get("source")
        raw_target = entry.get("target")
        if (
            not isinstance(raw_source, int)
            or isinstance(raw_source, bool)
            or not isinstance(raw_target, int)
            or isinstance(raw_target, bool)
        ):
            raise LLMGenerationError(
                message_en="Planner output is not the JSON contract",
                details={"reason": "an edge lacks integer source/target"},
            )
        pair_label = f"{raw_source} -> {raw_target}"

        if raw_source == raw_target:
            drops["self_loop"].append(pair_label)
            continue
        if not (1 <= raw_source <= node_count) or not (1 <= raw_target <= node_count):
            drops["unknown_index"].append(pair_label)
            continue
        key = (raw_source, raw_target)
        if key in seen:
            drops["duplicate"].append(pair_label)
            continue

        # Cycle check: would adding this edge close a cycle in the
        # graph of already-accepted edges? Reach forward from target
        # — if source is reachable from target, the new edge closes
        # the loop. A per-edge forward-DFS is O(N) per insertion,
        # cheap at the `MAX_NODES_PER_PLAN` bound.
        if _closes_cycle(outgoing, raw_source, raw_target):
            drops["cycle_closer"].append(pair_label)
            continue

        seen.add(key)
        outgoing.setdefault(raw_source, []).append(raw_target)
        accepted.append(PlannedEdge(source=raw_source, target=raw_target))

    # Emit one warning per non-empty bucket — same batched-shape rule
    # as the nodes ladder. Keys here are bucket IDs; values are the
    # Chinese-rendered reason the parser uses for that drop class.
    _DROP_REASONS: dict[str, str] = {
        "unknown_index": "引用未知节点",
        "self_loop": "自环边",
        "duplicate": "重复的边",
        "cycle_closer": "会产生环路的边",
    }
    for reason_key, dropped in drops.items():
        if dropped:
            warnings.append(
                f"Planner 返回了{_DROP_REASONS[reason_key]}, 已忽略: "
                + ", ".join(dropped)
                + "。"
            )
    return accepted


def _closes_cycle(
    outgoing: dict[int, list[int]],
    source: int,
    target: int,
) -> bool:
    """True iff adding `source -> target` would close a cycle.

    Walks the existing forward adjacency from `target`; if `source`
    is reachable, the new edge closes a loop. Stack-based DFS keeps
    the implementation allocation-light for the small N a Planner
    Plan ever sees.
    """
    if source == target:
        return True
    stack: list[int] = [target]
    visited: set[int] = set()
    while stack:
        current = stack.pop()
        if current == source:
            return True
        if current in visited:
            continue
        visited.add(current)
        for nxt in outgoing.get(current, ()):
            if nxt not in visited:
                stack.append(nxt)
    return False


__all__ = [
    "MAX_NODES_PER_PLAN",
    "MAX_TOOLS_IN_CATALOG",
    "PlanIntent",
    "PlannedEdge",
    "PlannedNode",
    "ToolPlanner",
    "parse_planner_output",
    "render_tool_catalog",
]
