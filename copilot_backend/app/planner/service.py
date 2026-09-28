"""`PlannerService` — user Turn → Plan orchestration (T18 / #16).

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

from dataclasses import dataclass

from app.conversations.errors import (
    ConversationAccessDeniedError,
    ConversationArchivedError,
)
from app.db.schemas import (
    Plan,
    PlanCreate,
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
from app.planner.planner import PlannedNode, ToolPlanner
from app.repositories.conversations import ConversationRepository
from app.repositories.plans import PlanRepository
from app.repositories.tools import ToolRepository
from app.repositories.turns import TurnRepository

# Cap on the Planner-authored `notes` string — `PlanNode.notes`
# enforces 512 max; clamping beats a validation error over an LLM
# that rambles.
_MAX_NOTES_LENGTH = 512


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
    ) -> None:
        self._conversations = conversation_repository
        self._turns = turn_repository
        self._plans = plan_repository
        self._tools = tool_repository
        self._planner = planner

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
                try:
                    intent = await self._planner.plan(content, tools)
                except (
                    LLMConfigurationError,
                    PromptUnavailableError,
                    LLMGenerationError,
                ) as exc:
                    warnings.append(
                        f"Planner 生成失败: {exc.message_en}; 本轮未生成 Plan。"
                    )
                else:
                    warnings.extend(intent.warnings)
                    plan = await self._persist_plan(
                        conversation_id=conversation_id,
                        turn=turn,
                        nodes=intent.nodes,
                    )
                    if plan is not None:
                        turn = await self._turns.set_plan_id(turn.id, plan.id)

        return TurnOutcome(turn=turn, plan=plan, warnings=warnings)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _persist_plan(
        self,
        *,
        conversation_id: str,
        turn: Turn,
        nodes: list[PlannedNode],
    ) -> Plan | None:
        """Freeze snapshots and insert the Plan; `None` for no nodes.

        Snapshot set is one entry per *distinct* Tool (name-uniqueness
        is a `PlanBase` invariant), assigned in first-appearance order
        so re-planning the same instruction yields the same doc shape.
        """
        if not nodes:
            return None

        snapshots: dict[str, ToolSnapshot] = {}
        for node in nodes:
            if node.tool.name not in snapshots:
                snapshots[node.tool.name] = _snapshot_of(node.tool)

        create = PlanCreate(
            conversation_id=conversation_id,
            turn_id=turn.id,
            status="pending",
            nodes=[
                PlanNode(
                    node_id=f"n{index}",
                    tool=node.tool.name,
                    parameters=node.parameters,
                    notes=node.notes[:_MAX_NOTES_LENGTH],
                )
                for index, node in enumerate(nodes, start=1)
            ],
            edges=[],
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
