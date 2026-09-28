"""`ToolPlanner` — the Planner LLM call (T18 / #16, ADR-0004 / ADR-0013).

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
* **T18 emits single-node Plans through the Prompt, not the code.**
  The bootstrap template constrains output to ≤1 Tool call; the
  parser accepts N nodes and returns them in order so T25 (#22) can
  upgrade the Langfuse copy without a deploy. Edges stay out of the
  contract until T25 (data-dependency binding is Worker-side,
  ADR-0012).
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
class PlanIntent:
    """Parsed Planner output: bound nodes plus non-fatal warnings.

    Empty `nodes` is a legitimate answer — ADR-0004 lets the Planner
    skip Plan generation when no Tool is involved (smalltalk, or a
    required parameter it could not resolve). `warnings` explain
    *why* nodes are missing when the drop was not the model's choice
    (unknown Tool names, catalog truncation).
    """

    nodes: list[PlannedNode]
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
    """Parse the `{"nodes": [...]}` contract and bind names to Tools.

    Binding is exact-match against the catalog (the active set):
    unknown names are dropped with a warning listing them, so a
    hallucinated Tool can never reach the HITL preview as an
    executable node. `parameters` must be a JSON object — anything
    else degrades that node's parameters to `{}` with a warning
    rather than rejecting the whole Plan.

    Raises:
        LLMGenerationError: output is not JSON, or `nodes` is missing
            / not a list, or an entry is not an object with a string
            `tool`.
    """
    by_name: dict[str, Tool] = {tool.name: tool for tool in tools}
    payload = extract_json_object(content)
    if payload is None:
        raise LLMGenerationError(
            message_en="Planner output is not the JSON contract",
            details={"reason": "no JSON object found"},
        )

    raw_nodes = payload.get("nodes")
    if raw_nodes is None:
        # Tolerate the smallest shape of "no plan" — an empty object
        # is semantically `{"nodes": []}` and shouldn't burn the turn.
        return PlanIntent(nodes=[], warnings=[])
    if not isinstance(raw_nodes, list):
        raise LLMGenerationError(
            message_en="Planner output is not the JSON contract",
            details={"reason": "'nodes' is not a list"},
        )

    nodes: list[PlannedNode] = []
    warnings: list[str] = []
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
    return PlanIntent(nodes=nodes, warnings=warnings)


__all__ = [
    "MAX_TOOLS_IN_CATALOG",
    "PlanIntent",
    "PlannedNode",
    "ToolPlanner",
    "parse_planner_output",
    "render_tool_catalog",
]
