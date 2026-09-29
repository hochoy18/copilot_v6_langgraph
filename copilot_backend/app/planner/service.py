"""`PlannerService` — user Turn → Plan orchestration (T18 / #16, T25 / #22).

One `POST /api/v1/conversations/{id}/turns` (ADR-0031 "发起用户轮次")
is one call into this module. It owns the write order and the
degradation ladder; the LLM mechanics live in `app.planner.planner`.

The order is deliberate:

1. **Ownership before anything.** The conversation must exist, belong
   to the caller, and not be `archived` (ADR-0011 makes archived
   read-only) — all before any write. Cross-user lookups surface the
   same `not_found` envelope as absent rows, mirroring
   `ConversationService`.
2. **Turn first, Plan second.** `PlanBase.turn_id` references the
   Turn, so the Turn must exist to anchor the Plan; the reverse link
   (`turn.plan_id`) is backfilled by `TurnRepository.set_plan_id`
   once the Plan row lands. A Turn with `plan_id=None` is a valid
   terminal state — ADR-0004 explicitly allows skipping Plan
   generation (smalltalk), and every failure path below leaves the
   Turn persisted with a warning instead of rolling it back.
3. **Freeze, then persist.** `ToolSnapshot`s are copied from the live
   `active` Tool rows at generation time (ADR-0027) — from here on
   the Plan is self-contained: the HITL preview (T20), the Worker
   (T21), and audit replay never read the mutable `tools` row.
4. **T25: map positional edges onto `node_id`s.** The LLM returns
   edges as 1-based indices into its own `nodes` array (see
   `PlannedEdge`); the service translates them onto the assigned
   `n{index}` `node_id`s before insert. The translation is the one
   place that knows both representations — keeping `PlannedEdge`
   positional preserves the LLM-side contract's brevity.

The Plan lands with status `pending`: no Tool ever executes from
this path (ADR-0004's mandatory preview). Approval is T20's endpoint.

Degradation follows T16's rule — the request answers 201 with
`plan=None` plus an explicit warning whenever the LLM layer is the
reason (unconfigured provider, prompt unavailable on every rung,
transport error, unparseable output). Silent failure is the failure
mode this guards against; the Frontend renders the warnings inline
and the user can rephrase.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.conversations.errors import (
    ConversationAccessDeniedError,
    ConversationArchivedError,
)
from app.db.errors import NotFoundError
from app.db.schemas import (
    Plan,
    PlanCreate,
    PlanEdge,
    PlanNode,
    Tool,
    ToolSnapshot,
    Turn,
    TurnCreate,
)
from app.llm.errors import (
    LLMConfigurationError,
    LLMGenerationError,
    PromptUnavailableError,
)
from app.memory.recall import (
    MilvusPlanHistoryReader,
    render_recall_block,
)
from app.planner.memory import build_memory_window
from app.planner.planner import PlannedEdge, PlannedNode, ToolPlanner
from app.repositories.conversations import ConversationRepository
from app.repositories.plans import PlanRepository
from app.repositories.tools import ToolRepository
from app.repositories.turns import TurnRepository

# Cap on the Planner-authored `notes` string — `PlanNode.notes`
# enforces 512 max; clamping beats a validation error over an LLM
# that rambles.
_MAX_NOTES_LENGTH = 512

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnOutcome:
    """Result of one submitted user Turn.

    `turn` is the persisted user Turn (with `plan_id` backfilled when
    a Plan was produced). `plan` is `None` on every no-plan path —
    smalltalk, no active Tools, or LLM-layer degradation. `warnings`
    explains which path was taken; empty on the happy path.
    """

    turn: Turn
    plan: Plan | None
    warnings: list[str]


class PlannerService:
    """Turn submission with Plan generation — T18 / #16.

    Stateless beyond its collaborator references; one instance per
    request is fine (the shared `ToolPlanner` caches the chat model
    itself). Tests override `get_planner_service` to swap in a
    fixture-built instance wired to a fake ChatModel.
    """

    def __init__(
        self,
        *,
        conversation_repository: ConversationRepository,
        turn_repository: TurnRepository,
        plan_repository: PlanRepository,
        tool_repository: ToolRepository,
        planner: ToolPlanner,
        milvus_reader: MilvusPlanHistoryReader | None = None,
    ) -> None:
        self._conversations = conversation_repository
        self._turns = turn_repository
        self._plans = plan_repository
        self._tools = tool_repository
        self._planner = planner
        # `None` is the documented graceful-degradation path: a
        # deployment that hasn't wired the reader yet (the seam is
        # best-effort, same contract as `milvus_writer` in the
        # Executor / T31) skips recall entirely. The rendered block
        # in that case is the empty placeholder — the LLM still sees
        # a recognisable string in the `{{long_term_memory}}` slot.
        self._milvus_reader = milvus_reader

    async def submit_turn(
        self,
        *,
        conversation_id: str,
        user_id: str,
        content: str,
    ) -> TurnOutcome:
        """Persist the user Turn, run the Planner, persist the Plan.

        Raises:
            NotFoundError: conversation absent or malformed id.
            ConversationAccessDeniedError: another user's conversation
                (same 404 envelope as absent — see `errors`).
            ConversationArchivedError: archived rows are read-only
                (ADR-0011).
        """
        conversation = await self._conversations.get(conversation_id)
        if conversation.user_id != user_id:
            raise ConversationAccessDeniedError(details={"user_id": user_id})
        if conversation.status == "archived":
            raise ConversationArchivedError(
                details={"conversation_id": conversation_id},
            )

        # ADR-0011: a fresh user Turn keeps the conversation active.
        await self._conversations.touch_activity(conversation_id)
        turn = await self._turns.create(
            TurnCreate(
                conversation_id=conversation_id,
                role="user",
                content=content,
            ),
        )

        warnings: list[str] = []
        plan: Plan | None = None
        if not self._planner.ready:
            warnings.append(
                "Planner LLM 未配置 (COPILOT_LLM_BASE_URL / "
                "COPILOT_LLM_API_KEY), 本轮未生成 Plan。"
            )
        else:
            tools = await self._tools.list_active()
            if not tools:
                warnings.append("Tool Registry 没有 active Tool, 本轮无法规划。")
            else:
                # T30 / ADR-0007: render the recent-K-turn memory window
                # *before* the Planner call so the LLM sees the prior
                # conversation verbatim. The fetch happens here (not at
                # tool-build time) because K is read off the configured
                # `ToolPlanner` and the conversation's transcript grows
                # during the lifespan of a long-lived planner.
                memory_window = await self._build_memory_window(
                    conversation_id=conversation_id,
                    excluding_turn_id=turn.id,
                )
                # T32 / #28 — long-term-memory recall. The reader
                # surfaces the Top-N most-similar historical Plan
                # summaries (across all conversations, including this
                # one) so the LLM can resolve cross-session references
                # like "上周那个". Best-effort: a reader-not-wired or
                # search-raised path falls back to the empty placeholder
                # rather than unwinding the Planner call — same
                # graceful-degradation contract as the writer (T31).
                long_term_memory = await self._build_long_term_memory(
                    instruction=content,
                )
                try:
                    intent = await self._planner.plan(
                        content,
                        tools,
                        memory_window=memory_window,
                        long_term_memory=long_term_memory,
                    )
                except (
                    LLMConfigurationError,
                    PromptUnavailableError,
                    LLMGenerationError,
                ) as exc:
                    # `message_zh` (not `_en`): the warning is a Chinese
                    # sentence for the chat panel; splicing English text
                    # into it would read as a half-translated UI string.
                    warnings.append(
                        f"Planner 生成失败: {exc.message_zh}; 本轮未生成 Plan。"
                    )
                else:
                    warnings.extend(intent.warnings)
                    plan = await self._persist_plan(
                        conversation_id=conversation_id,
                        turn=turn,
                        nodes=intent.nodes,
                        edges=intent.edges,
                    )
                    if plan is not None:
                        turn = await self._turns.set_plan_id(turn.id, plan.id)

        return TurnOutcome(turn=turn, plan=plan, warnings=warnings)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _build_memory_window(
        self,
        *,
        conversation_id: str,
        excluding_turn_id: str,
    ) -> str:
        """Render the recent-K-turn context the Planner will see (T30 / #26).

        Reads the previous K user turns (K is taken from the configured
        `ToolPlanner.memory_window_k`) plus their linked Plan rows
        when available, and hands the rendered block to the Planner.
        The freshly-created Turn is excluded — the user instruction
        that triggered this call lands in `instruction`, not in the
        window. Plans are looked up best-effort: a missing snapshot
        degrades to a "no Plan" line rather than aborting the call.

        The fetch size is `K + 1` because the most-recent turn in the
        list is the one we just persisted; `build_memory_window` then
        slices the last K user turns, dropping any assistant turns
        along the way. Keeping the round-trip small matters because
        per-Plan lookups can fan out: an idle conversation that hits
        `K = 5` may need up to 5 Plan fetches in series.
        """
        k = self._planner.memory_window_k
        # Pull K + 1 turns: the newest one is the Turn we just
        # created, so the prior K user turns are guaranteed inside
        # the slice once `build_memory_window` filters by role.
        recent = await self._turns.list_by_conversation(
            conversation_id,
            limit=k + 1,
        )
        prior = [
            turn for turn in recent if turn.id != excluding_turn_id
        ]

        plans_by_turn_id: dict[str, Plan | None] = {}
        for turn in prior:
            if turn.plan_id is None:
                continue
            try:
                plans_by_turn_id[turn.id] = await self._plans.get(turn.plan_id)
            except NotFoundError:
                # Plan may have been hard-deleted by an admin path
                # (T10); a missing snapshot is a non-fatal degrade.
                plans_by_turn_id[turn.id] = None

        return build_memory_window(
            prior,
            plans_by_turn_id,
            k=k,
        )

    async def _build_long_term_memory(self, *, instruction: str) -> str:
        """Recall the Top-N historical Plan summaries for `instruction` (T32 / #28).

        Delegates to `MilvusPlanHistoryReader.search` and renders the
        matches through `render_recall_block`. Two non-fatal paths:

        * Reader not wired (`None`) — returns the empty placeholder.
          Same graceful-degradation as the writer seam in the Executor:
          a deployment that hasn't installed the Milvus SDK skips
          recall without taking down the Planner call.
        * Search raised — logged and swallowed; returns the empty
          placeholder. ADR-0008 forbids the recall path from
          unwinding a successful Planner call.

        `top_n` is read off the configured `ToolPlanner` so the service
        never holds a parallel reference to the value — the seam is
        one place to change.
        """
        if self._milvus_reader is None:
            return render_recall_block([])
        try:
            matches = await self._milvus_reader.search(
                instruction,
                top_n=self._planner.memory_recall_top_n,
            )
        except Exception:
            logger.exception(
                "milvus plan_history recall failed (top_n=%d); "
                "falling back to empty long-term-memory placeholder",
                self._planner.memory_recall_top_n,
            )
            return render_recall_block([])
        return render_recall_block(matches)

    async def _persist_plan(
        self,
        *,
        conversation_id: str,
        turn: Turn,
        nodes: list[PlannedNode],
        edges: list[PlannedEdge],
    ) -> Plan | None:
        """Freeze snapshots and insert the Plan; `None` for no nodes.

        Snapshot set is one entry per *distinct* Tool (name-uniqueness
        is a `PlanBase` invariant), assigned in first-appearance order
        so re-planning the same instruction yields the same doc shape.

        Edges (T25 / #22) arrive as 1-based positional indices into
        the LLM's `nodes` array; this method translates them onto the
        assigned `n{index}` `node_id`s so the persisted Plan uses the
        ADR-0012 string-endpoint contract the executor and the React
        Flow renderer expect. `PlanBase`'s validator re-checks the
        edges at insert time as a defence-in-depth guard.
        """
        if not nodes:
            return None

        snapshots: dict[str, ToolSnapshot] = {}
        for node in nodes:
            if node.tool.name not in snapshots:
                snapshots[node.tool.name] = _snapshot_of(node.tool)

        # Stable per-Plan id assignment mirrors the LLM-side positional
        # convention (`n1` = first node, …), so the index → id mapping
        # is purely positional and needs no lookup table.
        node_ids = [f"n{index}" for index in range(1, len(nodes) + 1)]
        plan_edges = [
            PlanEdge(source=node_ids[edge.source - 1], target=node_ids[edge.target - 1])
            for edge in edges
        ]

        create = PlanCreate(
            conversation_id=conversation_id,
            turn_id=turn.id,
            status="pending",
            nodes=[
                PlanNode(
                    node_id=node_ids[index - 1],
                    tool=node.tool.name,
                    parameters=node.parameters,
                    notes=node.notes[:_MAX_NOTES_LENGTH],
                )
                for index, node in enumerate(nodes, start=1)
            ],
            edges=plan_edges,
            tool_snapshots=list(snapshots.values()),
        )
        return await self._plans.create(create)


def _snapshot_of(tool: Tool) -> ToolSnapshot:
    """Freeze one live `Tool` row into a Plan-embedded snapshot (ADR-0027).

    Field-for-field copy of the execution-relevant subset; admin
    provenance (`status`, `source`, `source_ref`) and the credential
    pointer (`credentials_ref`) stay out on purpose — the credential
    is resolved at call time by the Worker (ADR-0002), never by
    anything that can read the Plan doc.
    """
    return ToolSnapshot(
        tool_id=tool.id,
        name=tool.name,
        description=tool.description,
        risk_level=tool.risk_level,
        parameters_schema=dict(tool.parameters_schema),
        http_method=tool.http_method,
        http_url_template=tool.http_url_template,
        http_headers=dict(tool.http_headers),
        http_body_template=(
            dict(tool.http_body_template)
            if tool.http_body_template is not None
            else None
        ),
    )


__all__ = ["PlannerService", "TurnOutcome"]
