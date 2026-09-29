"""`AnswerService` — T22 / #19.

The orchestrator that sits between the `PlanExecutor` (which owns
Tool-call outcomes) and the chat panel (which renders the streamed
reply). It runs the `AnswerGenerator` end-to-end and does three
things on success:

1. **Publishes each token through `SseEventBus` as `llm.token`.** The
   Frontend's SSE consumer concatenates them per `turn_id` to render
   the assistant's streaming reply (T24 / #21).
2. **Persists the assembled text as an `assistant` Turn** (role =
   `assistant`, content = the concatenated reply, plan_id = the
   executed plan's id so audit replays stay linked to the call graph).
3. **Touches the conversation's `last_activity_at`.** Same lifecycle
   rule as `PlannerService.submit_turn` (ADR-0011): a fresh assistant
   Turn keeps the conversation active.

Behavioural rules worth knowing before editing:

* **Degradation follows T16 / T18.** An unconfigured or failing LLM
  must not turn a successful Plan execution into a failed request —
  the user still sees the Plan results in the audit log; they just
  don't see an LLM-written summary. The orchestrator's return value
  (`StreamOutcome.assistant_turn`) is `None` on every degraded path
  and the route layer hands the caller a normal 200.
* **The Plan's terminal status drives the call.** We only stream an
  answer on a `succeeded` Plan; a `failed` Plan is its own answer
  (the user sees the per-node errors), and on `rejected` / `pending`
  / etc. there is nothing to summarise.
* **`turn_id` reuse is intentional.** The `llm.token` event carries
  the *user* Turn's id (the one that triggered the Plan). This is
  the same id the Frontend uses to anchor the streaming reply in the
  chat panel — sending a different id (the assistant Turn's id) would
  arrive before the row lands and the Frontend would lose the
  association. The assistant Turn's id is recoverable through
  `TurnRepository.get(turn_id)` after the persist, but the wire id
  stays on the user Turn so existing renderers keep working.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.answer.generator import AnswerGenerator, NodeSummary
from app.db.schemas import (
    Plan,
    PlanNodeResult,
    PlanStatus,
    Turn,
    TurnCreate,
)
from app.llm.errors import (
    LLMConfigurationError,
    LLMGenerationError,
    PromptUnavailableError,
)
from app.realtime.bus import SseEventBus
from app.realtime.events import llm_token
from app.repositories.conversations import ConversationRepository
from app.repositories.turns import TurnRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamOutcome:
    """Result of one final-answer streaming attempt.

    `assistant_turn` is the persisted `assistant` Turn on the happy
    path (and on every silent-degradation path where the LLM
    produced an empty answer); `None` when streaming was skipped
    (LLM not configured, Plan failed, no user instruction). The
    route layer reads this to decide whether to surface an answer
    in the chat panel.

    `degraded` is the human-readable reason when streaming was
    skipped — empty on success so the chat panel renders nothing
    extra. The Frontend uses this to show "回答生成已跳过: LLM 未配置"
    etc. rather than leaving the user guessing.
    """

    assistant_turn: Turn | None
    degraded: str = ""


class AnswerService:
    """Orchestrate the T22 final-answer streaming + persistence.

    Stateless beyond its collaborator references; one instance per
    request is fine (the shared `AnswerGenerator` caches its chat
    model itself). Tests override `get_answer_service` to swap in
    a fixture-built instance wired to a fake `BaseChatModel`.
    """

    def __init__(
        self,
        *,
        generator: AnswerGenerator,
        sse_bus: SseEventBus,
        turn_repository: TurnRepository,
        conversation_repository: ConversationRepository,
        now_fn: Callable[[], datetime],
    ) -> None:
        self._generator = generator
        self._sse_bus = sse_bus
        self._turns = turn_repository
        self._conversations = conversation_repository
        self._now = now_fn

    # ------------------------------------------------------------------
    # Public seam
    # ------------------------------------------------------------------

    async def stream_final_answer(
        self,
        *,
        conversation_id: str,
        user_turn_id: str,
        plan: Plan,
        instruction: str,
        node_results: list[PlanNodeResult],
    ) -> StreamOutcome:
        """Run the final-answer streaming path end-to-end.

        Order of operations:

        1. **Gate on Plan terminal status.** Only `succeeded` Plans
           get a summarised answer; `failed` Plans are their own
           answer (per-node errors are already on the audit log /
           SSE channel).
        2. **Gate on LLM configuration.** An unconfigured `ready`
           short-circuits with a degradation reason — the Plan is
           still a success, the user just doesn't see an LLM
           summary.
        3. **Render NodeSummary list.** Per-node response / error
           envelopes go through the generator's pure helper so the
           Prompt body stays bounded.
        4. **Iterate the generator, publish each token.** `llm.token`
           events carry `payload.turn_id = user_turn_id` so the
           Frontend's chat-panel renderer keeps the streaming reply
           anchored to the user message that triggered it.
        5. **Persist the assembled reply.** A single `assistant`
           Turn with `plan_id = plan.id` lands after the stream
           finishes; the conversation's `last_activity_at` is
           touched to keep the row `active` per ADR-0011.

        Raises:
            LLMConfigurationError / PromptUnavailableError /
            LLMGenerationError: same propagation contract as
                Planner / description generator. The orchestrator
                does NOT swallow these — the route layer renders
                them as the same degradation warnings the Planner
                path uses.
        """
        if plan.status != "succeeded":
            return StreamOutcome(
                assistant_turn=None,
                degraded=f"Plan {plan.id} did not succeed; final answer skipped.",
            )

        if not self._generator.ready:
            return StreamOutcome(
                assistant_turn=None,
                degraded=(
                    "Final-answer LLM not configured "
                    "(COPILOT_LLM_BASE_URL / COPILOT_LLM_API_KEY); "
                    "Plan succeeded but no LLM summary."
                ),
            )

        summaries = build_summaries(plan, node_results)

        chunks: list[str] = []
        try:
            async for token in self._generator.astream(
                instruction=instruction,
                results=summaries,
            ):
                chunks.append(token)
                await self._publish_token(
                    conversation_id=conversation_id,
                    turn_id=user_turn_id,
                    token=token,
                )
        except (
            LLMConfigurationError,
            PromptUnavailableError,
            LLMGenerationError,
        ):
            # Let the route layer render the same degradation envelope
            # the Planner path uses; a partial streamed reply is
            # discarded (the Frontend will rerender the chat panel on
            # the next conversation detail fetch).
            raise

        full_text = "".join(chunks).rstrip()
        # `rstrip` only, not `strip` — leading newlines from the LLM
        # are intentional (paragraph breaks, indented code blocks
        # inside Markdown fences), and stripping them silently mangles
        # the rendered reply. We only drop trailing whitespace.
        if not full_text:
            # Empty streamed answer — same degradation shape as a
            # transport failure but with a clearer reason.
            return StreamOutcome(
                assistant_turn=None,
                degraded="LLM produced an empty final answer.",
            )

        await self._conversations.touch_activity(conversation_id)
        assistant = await self._turns.create(
            TurnCreate(
                conversation_id=conversation_id,
                role="assistant",
                content=full_text,
                plan_id=plan.id,
                extra={
                    "answer_source": "result-summarizer",
                    "node_count": len(node_results),
                },
            ),
        )
        return StreamOutcome(assistant_turn=assistant)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _publish_token(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        token: str,
    ) -> None:
        """Allocate an event id and publish `llm_token(...)` on the bus.

        The Frontend's `useEventStore` (T24 / #21) accumulates
        `payload.token` per `turn_id`; routing through `bus.publish`
        means the token lands in the replay buffer too, so a
        reconnecting consumer catches up on the partial answer
        instead of seeing a blank chat panel.
        """
        event_id = await self._sse_bus.next_event_id(conversation_id)
        await self._sse_bus.publish(
            llm_token(
                event_id=event_id,
                conversation_id=conversation_id,
                token=token,
                turn_id=turn_id,
                now=self._now(),
            )
        )


# ---------------------------------------------------------------------------
# Module-level pure helpers — importable for targeted tests
# ---------------------------------------------------------------------------


def build_summaries(
    plan: Plan,
    node_results: list[PlanNodeResult],
) -> list[NodeSummary]:
    """Project `node_results` onto the summarizer's `NodeSummary` shape.

    `plan.nodes` carries the per-node `notes`; `node_results` carries
    the runtime status / response / error. Both lists index by
    `node_id`, so the join is a single pass — unknown `node_id`s are
    skipped (defensive, mirrors the executor's snapshot-missing
    branch).

    The `response_text` field captures a compact string view of the
    response so the Prompt's `{{results}}` block stays readable;
    the structured `response` field is also passed through for cases
    where a structured excerpt matters more than the textual head.
    """
    notes_by_id: dict[str, str] = {node.node_id: node.notes for node in plan.nodes}
    summaries: list[NodeSummary] = []
    for result in node_results:
        node_id = result.node_id
        tool_name = next(
            (node.tool for node in plan.nodes if node.node_id == node_id),
            node_id,
        )
        summaries.append(
            NodeSummary(
                node_id=node_id,
                tool_name=tool_name,
                notes=notes_by_id.get(node_id, ""),
                status=result.status,
                response=result.response,
                error=result.error,
                response_text=_extract_text(result.response),
            )
        )
    return summaries


def _extract_text(response: dict[str, Any] | None) -> str:
    """Pull the first readable string out of a Tool response payload.

    Most business APIs return `{"text": "..."}` or `{"data": "..."}`
    or a `{"results": [...]}` envelope; the LLM only needs the
    textual head. Empty string when nothing matches.
    """
    if not isinstance(response, dict):
        return ""
    for key in ("text", "message", "summary", "data"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


__all__ = [
    "AnswerService",
    "StreamOutcome",
    "build_summaries",
]