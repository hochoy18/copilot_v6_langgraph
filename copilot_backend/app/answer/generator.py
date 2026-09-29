"""`AnswerGenerator` — T22 / #19.

Stream the LLM's final-answer text for a completed Plan. Takes the
user's instruction plus the per-node Tool execution results, renders
the Langfuse `result-summarizer` Prompt, and yields incremental
tokens through LangChain's `BaseChatModel.astream(...)` interface.

Behavioural rules worth knowing before editing:

* **Pure LLM seam.** No SSE, no persistence, no bus — the generator
  only knows how to ask the model for tokens. The orchestrator
  (`app.answer.service.AnswerService`) is the layer that publishes
  each token through `SseEventBus` and persists the assembled text
  as an `assistant` Turn. Keeping these separate keeps the streaming
  contract a one-method `astream(...)` that tests can drive with a
  fake `BaseChatModel`.
* **Streaming via `astream`, not `ainvoke`.** The Frontend expects
  per-token events (`llm.token` in ADR-0010 / T23) so the user
  sees the answer land character-by-character; `ainvoke` would
  block until the entire reply is ready, which defeats the purpose.
  LangChain falls back to `ainvoke` automatically when no async
  stream is implemented; the bootstrap model (OpenAI-compatible)
  supports `astream` natively.
* **Failure modes are typed.** A transport error or a chunk the
  extractor can't decode raises `LLMGenerationError`; the
  orchestrator's job is to decide degradation. `PromptUnavailableError`
  / `LLMConfigurationError` propagate untouched (same shape as the
  Planner / description generator — see ADR-0013 / ADR-0016).
* **`ready` is the "is the LLM configured" check.** Tests use it to
  short-circuit before constructing a fake model; production
  callers use it to decide whether to skip the streaming step
  (degraded boot is allowed — `/healthz` reports 503 but the API
  still answers).
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.llm.errors import LLMGenerationError
from app.llm.output import content_to_text
from app.llm.prompts import (
    RESULT_SUMMARIZER_PROMPT,
    PromptProvider,
    render_template,
)
from app.settings import Settings

# Cap on the rendered `{{results}}` block. Tool responses can be very
# large — the LLM primarily needs the contract shape and key fields,
# not the full payload (T36 / ADR-0023 layers a separate "compress
# oversized response" seam on top of this for the long tail). 4 KB
# matches the upper bound on the rendered Tool catalog and keeps the
# final-answer prompt under most provider token budgets even when
# many nodes finish.
_MAX_RESULTS_LENGTH = 4096

# Per-node response truncation. Each result is clipped to this many
# characters before joining; the LLM sees the head and the status /
# error envelope, not the raw API payload. The cap is per-node so a
# 5-node Plan still gives each call a reasonable slice.
_MAX_PER_NODE_LENGTH = 800


@dataclass(frozen=True, slots=True)
class NodeSummary:
    """One Plan node's outcome, shaped for the summarizer Prompt.

    Deliberately separate from `app.db.schemas.PlanNodeResult` — the
    generator's contract is "what does the LLM need to see", not
    "what does the audit log persist". Keeping the LLM-side shape
    standalone stops a future audit-log field (e.g. `retry_history`)
    from leaking into the Prompt.
    """

    node_id: str
    tool_name: str
    notes: str
    status: str  # "succeeded" / "failed" / "skipped" / "cancelled"
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    response_text: str = field(default="")


class AnswerGenerator:
    """Streaming LLM final-answer call — T22 / #19.

    One instance lives per process (built in the lifespan alongside
    the description generator and the Planner); `astream` is the
    only public method. Tests hand in a fake `BaseChatModel` via
    `chat_model_factory`.
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

    async def astream(
        self,
        *,
        instruction: str,
        results: list[NodeSummary],
    ) -> AsyncIterator[str]:
        """Yield incremental text tokens for the final answer.

        Each yielded value is the textual content of one streaming
        chunk from the model — concatenating them produces the full
        reply. The orchestrator must publish each token through the
        SSE bus for the Frontend to render it live.

        Raises:
            LLMConfigurationError: provider refused by config (ADR-0016).
            PromptUnavailableError: no `result-summarizer` template on
                any rung (only for prompt names without a bootstrap).
            LLMGenerationError: transport failure or a chunk the
                extractor can't decode.
        """
        template = await self._prompts.get_prompt(RESULT_SUMMARIZER_PROMPT)
        prompt_text = render_template(
            template.text,
            {
                "instruction": instruction,
                "results": render_node_results(results),
            },
        )

        model = self._chat_model()
        try:
            async for chunk in model.astream(prompt_text):
                text = _chunk_to_text(chunk)
                if text:
                    yield text
        except LLMGenerationError:
            raise
        except Exception as exc:  # langchain wraps provider errors variably
            raise LLMGenerationError(
                message_en=f"Final-answer LLM call failed: {exc}",
                details={"error_type": type(exc).__name__},
            ) from exc

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


def render_node_results(results: list[NodeSummary]) -> str:
    """Render one line per node for the `{{results}}` placeholder.

    Empty result list is a no-Plan path (the Planner produced no
    nodes, or every node failed before responding); the placeholder
    line keeps the Prompt body valid instead of silently rendering
    an empty block.
    """
    if not results:
        return "(本次未执行任何 Tool / no tool results)"
    lines: list[str] = []
    for index, node in enumerate(results, start=1):
        lines.append(_render_one_node(index, node))
    joined = "\n".join(lines)
    if len(joined) > _MAX_RESULTS_LENGTH:
        joined = joined[:_MAX_RESULTS_LENGTH] + "\n... (后续结果已截断)"
    return joined


def _render_one_node(index: int, node: NodeSummary) -> str:
    """Render one `NodeSummary` into a single Prompt block.

    Shape:
        [1] echo (n1) — succeeded
            notes: ...
            response: {"text": "hello"}
    """
    head = f"[{index}] {node.tool_name} ({node.node_id}) — {node.status}"
    pieces: list[str] = [head]
    if node.notes:
        pieces.append(f"  notes: {node.notes}")
    if node.response is not None:
        # Always render the structured `response` via the same
        # `_truncate_repr` helper so the truncation primitive is
        # uniform — earlier drafts preferred a separate `response_text`
        # path, but the two branches diverged in their limits and
        # made the cap ineffective. `NodeSummary.response_text` is
        # still populated by `AnswerService.build_summaries` (it
        # powers the chat-panel preview the Frontend may render), but
        # the Prompt body sees the structured shape.
        pieces.append(
            f"  response: {_truncate_repr(node.response, _MAX_PER_NODE_LENGTH)}"
        )
    if node.error is not None:
        pieces.append(f"  error: {_truncate_repr(node.error, _MAX_PER_NODE_LENGTH)}")
    return "\n".join(pieces)


def _truncate_repr(value: Any, limit: int) -> str:
    """`repr(value)` clipped to `limit` characters with a tail marker.

    Plain `str(value)` would lose JSON structure (e.g. nested dicts
    flatten to `[]` for some types); `repr` keeps the wire shape. The
    limit mirrors `_MAX_RESULTS_LENGTH`'s role for the per-node slot.
    """
    rendered = repr(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + "...(已截断)"


def _chunk_to_text(chunk: Any) -> str:
    """Extract the textual portion of one LangChain stream chunk.

    LangChain returns `AIMessageChunk` whose `content` may be a `str`
    or a list of typed content blocks (OpenAI tool-call + text
    streams). Reuses the Planner / description-generator helper so the
    text-extraction rules stay in one place.
    """
    content = getattr(chunk, "content", None)
    if content is None:
        return ""
    return content_to_text(content)


__all__ = [
    "AnswerGenerator",
    "NodeSummary",
    "render_node_results",
]