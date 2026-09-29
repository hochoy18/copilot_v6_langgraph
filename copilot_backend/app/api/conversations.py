"""`/api/v1/conversations` router — T10 / #40, T18 / #16, T20 / #43, T22 / #19, T26 / #44.

The thin HTTP seam for conversation CRUD, turn submission, and the
HITL Plan approve / reject / edit endpoints. Eight endpoints per
ADR-0031:

* `POST /api/v1/conversations` — create.
* `GET  /api/v1/conversations` — list (with optional `status` filter).
* `GET  /api/v1/conversations/{id}` — detail (with turns + plans).
* `POST /api/v1/conversations/{id}/turns` — submit a user Turn and
  run the Planner (T18). Synchronous for now: the response carries
  the generated Plan (status `pending`, awaiting the HITL preview —
  ADR-0004); the SSE event stream `plan.generated` etc. is T23 (#20).
* `POST /api/v1/conversations/{id}/plan/approve` — HITL approval
  (T20 / #43 / ADR-0004). Flips the conversation's latest Plan to
  `approved`; the Worker (T21) picks it up from there.
* `POST /api/v1/conversations/{id}/plan/reject` — HITL rejection
  (T20 / #43 / ADR-0004). Flips the latest Plan to `rejected`; the
  Turn stays so the conversation can be re-decided.
* `PATCH /api/v1/conversations/{id}/plan` — HITL edit
  (T26 / #44 / ADR-0019). Mutates per-node `parameters` / `notes`
  only; the diff lands in `audit_logs`. Returns 200 + status
  `modified` so the React Flow drawer refreshes without a follow-up
  fetch.
* `POST /api/v1/conversations/{id}/archive` — manual archive
  (transitions `active` / `idle` → `idle`, ADR-0011).

The router is intentionally thin: business logic lives in
`app.conversations.service.ConversationService` (CRUD + Plan
decisions) and `app.planner.service.PlannerService` (turn → Plan).
This file exists only to translate Pydantic wire shapes into service
calls and back.

Authentication is supplied by `get_current_user` (T09 / #10), which
returns the canonical `User` row. Ownership is then enforced at the
service seam — a cross-user lookup surfaces the same `not_found`
envelope as an absent row (ADR-0002).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.answer.service import AnswerService
from app.conversations.service import (
    ConversationDetail,
    ConversationService,
)
from app.db.dependencies import (
    get_answer_service,
    get_conversation_service,
    get_plan_execution_repository,
    get_plan_executor,
    get_planner_service,
)
from app.db.schemas import (
    Conversation,
    ConversationStatus,
    Plan,
    PlanNode,
    Turn,
    User,
)
from app.llm.errors import (
    LLMConfigurationError,
    LLMGenerationError,
    PromptUnavailableError,
)
from app.planner.service import PlannerService, TurnOutcome
from app.repositories.plan_executions import PlanExecutionRepository
from app.security.auth import get_current_user
from app.tools.executor import PlanExecutionOutcome, PlanExecutor

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class CreateConversationRequest(BaseModel):
    """Body of `POST /api/v1/conversations`.

    `title` is optional; the Planner titles later (T18), or the
    Frontend renders a slice of the first user Turn. Keeping the
    request shape minimal lets the front-end POST an empty body when
    the user just clicks "new conversation".
    """

    title: str = Field(
        default="",
        max_length=256,
        description="Optional display title. Empty until Planner or user sets one.",
    )


class ConversationResponse(BaseModel):
    """Canonical conversation wire shape.

    Mirrors `app.db.schemas.Conversation` — the Frontend's
    conversation list / detail panels are typed against this. The
    `mode='json'` rendering in the router turns BSON ObjectId strings
    and datetimes into JSON-safe values without leaking BSON-specific
    types.
    """

    id: str = Field(description="ObjectId of the conversation row.")
    user_id: str = Field(description="ObjectId of the owning user.")
    title: str
    status: ConversationStatus
    last_activity_at: str = Field(description="ISO 8601 timestamp.")
    created_at: str
    updated_at: str


class ConversationDetailResponse(BaseModel):
    """Wire shape of `GET /api/v1/conversations/{id}`.

    Wraps the conversation row alongside the message history and the
    Plan DAG history. The Frontend's chat panel reads `turns`; the
    React Flow renderer reads `plans` (T19).
    """

    conversation: ConversationResponse
    turns: list[dict[str, Any]] = Field(
        description=(
            "Conversation turns in `created_at` order. Each row carries "
            "`role` (`user` / `assistant` / `system`), `content`, and an "
            "optional `plan_id`."
        ),
    )
    plans: list[dict[str, Any]] = Field(
        description=(
            "Plans in `created_at` DESC order. Each row carries the "
            "T17 shape — `nodes` / `edges` / `tool_snapshots` per "
            "ADR-0027; nodes bind to snapshots by `tool` name."
        ),
    )


class ConversationListResponse(BaseModel):
    """Wire shape of `GET /api/v1/conversations`.

    Wrapped in a single-key envelope so future pagination metadata
    (`next_cursor`, `has_more`) can land without breaking the
    client-side parser.
    """

    conversations: list[ConversationResponse] = Field(
        description="Conversations in `last_activity_at` DESC order.",
    )


class CreateTurnRequest(BaseModel):
    """Body of `POST /api/v1/conversations/{id}/turns` (T18 / #16).

    `content` bounds: one user Turn is a natural-language instruction,
    so the floor rejects empty messages and the ceiling keeps the
    Planner prompt (and the `turns` document) bounded. The bound is
    enforced at the wire, not in the service.
    """

    content: str = Field(
        min_length=1,
        max_length=4000,
        description="Natural-language instruction from the business user.",
    )


class TurnResponse(BaseModel):
    """Wire shape of `POST /api/v1/conversations/{id}/turns`.

    `turn` is the persisted user Turn (its `plan_id` is backfilled
    when a Plan was produced). `plan` is the generated Plan doc in
    the T17 shape (`nodes` / `edges` / `tool_snapshots`, ADR-0027),
    status `pending` — the HITL preview (T19/T20) consumes it from
    here; `None` when the Turn needed no Plan (smalltalk) or the
    Planner degraded (see `warnings`). Dict-typed like `turns` in the
    detail response so new Plan fields don't force a client redeploy.
    """

    turn: dict[str, Any] = Field(description="The created user Turn row.")
    plan: dict[str, Any] | None = Field(
        description="The pending Plan, or null when no Tool call was planned."
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Non-fatal notes explaining a missing/partial Plan "
            "(Planner degradation, unknown Tools, catalog truncation). "
            "Empty on the happy path."
        ),
    )


# ---------------------------------------------------------------------------
# Conversions — internal helpers
# ---------------------------------------------------------------------------


def _conversation_to_response(conv: Conversation) -> ConversationResponse:
    """Render a canonical `Conversation` row as the wire shape.

    Centralising the conversion keeps the four routes from
    duplicating `model_dump(mode='json')`; a future field added to
    the wire shape only needs one site to change.
    """
    return ConversationResponse(
        id=conv.id,
        user_id=conv.user_id,
        title=conv.title,
        status=conv.status,
        last_activity_at=conv.last_activity_at.isoformat(),
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
    )


def _turn_to_dict(turn: Turn) -> dict[str, Any]:
    """Render a Turn row as a JSON-safe dict.

    The Frontend renders these verbatim; we keep them as dicts
    rather than fixed Pydantic models so a new Turn field (e.g.
    `extra` audit metadata, T40) doesn't require a client redeploy
    keyed on the wire shape.
    """
    return turn.model_dump(mode="json")


def _plan_to_dict(plan: Plan) -> dict[str, Any]:
    """Render a Plan row as a JSON-safe dict.

    The `nodes` / `edges` / `tool_snapshots` trio (ADR-0027, T17)
    flows through verbatim — the React Flow renderer consumes edges
    directly and resolves each node's Tool definition from the
    Plan-level snapshot list without re-deriving anything.
    """
    return plan.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new conversation owned by the authenticated user",
)
async def create_conversation(
    body: CreateConversationRequest | None = None,
    user: User = Depends(get_current_user),  # noqa: B008
    svc: ConversationService = Depends(get_conversation_service),  # noqa: B008
) -> ConversationResponse:
    """`POST /api/v1/conversations` — start a new session.

    New conversations are `active` by definition (ADR-0011) — the
    Frontend can immediately render them in the "active" tab and
    start streaming Turns. The request body is optional so the
    Frontend's "new conversation" button can POST an empty body
    without a JS-side `{}` shim.
    """
    title = body.title if body is not None else ""
    conv = await svc.create(user_id=user.id, title=title)
    return _conversation_to_response(conv)


@router.get(
    "",
    response_model=ConversationListResponse,
    summary="List the authenticated user's conversations",
)
async def list_conversations(
    status_filter: ConversationStatus | None = Query(  # noqa: B008 — FastAPI idiom
        default=None,
        alias="status",
        description="Optional lifecycle filter (`active` / `idle` / `archived`).",
    ),
    limit: int = Query(  # noqa: B008 — FastAPI idiom
        default=50,
        ge=1,
        le=200,
        description="Maximum number of conversations to return.",
    ),
    after_id: str | None = Query(  # noqa: B008 — FastAPI idiom
        default=None,
        description=(
            "Cursor pagination — return conversations whose `_id` sorts "
            "after this id. Frontend-driven infinite scroll passes the "
            "last id from the previous page."
        ),
    ),
    user: User = Depends(get_current_user),  # noqa: B008
    svc: ConversationService = Depends(get_conversation_service),  # noqa: B008
) -> ConversationListResponse:
    """`GET /api/v1/conversations` — Frontend's three-tab view.

    ADR-0011's three tabs (active / idle / archived) map onto the
    `status` query param. Without `status` the response includes every
    conversation the user owns, ordered by `last_activity_at DESC` so
    the most-recent session lands at the top.
    """
    rows = await svc.list_for_user(
        user_id=user.id,
        status=status_filter,
        limit=limit,
        after_id=after_id,
    )
    return ConversationListResponse(
        conversations=[_conversation_to_response(c) for c in rows],
    )


@router.get(
    "/{conversation_id}",
    response_model=ConversationDetailResponse,
    summary="Fetch a conversation plus its turns and plans",
)
async def get_conversation_detail(
    conversation_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    svc: ConversationService = Depends(get_conversation_service),  # noqa: B008
) -> ConversationDetailResponse:
    """`GET /api/v1/conversations/{id}` — session-level read.

    Composes `conversations` / `turns` / `plans` into one envelope so
    the Frontend's chat panel + React Flow renderer can hydrate from
    a single round trip. Cross-user access renders the same 404
    envelope as an absent conversation (see `ConversationService`).
    """
    detail: ConversationDetail = await svc.get_detail(
        conversation_id=conversation_id,
        user_id=user.id,
    )
    return ConversationDetailResponse(
        conversation=_conversation_to_response(detail.conversation),
        turns=[_turn_to_dict(t) for t in detail.turns],
        plans=[_plan_to_dict(p) for p in detail.plans],
    )


@router.post(
    "/{conversation_id}/turns",
    response_model=TurnResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a user Turn and run the Planner over it",
)
async def submit_turn(
    conversation_id: str,
    body: CreateTurnRequest,
    user: User = Depends(get_current_user),  # noqa: B008
    svc: PlannerService = Depends(get_planner_service),  # noqa: B008
) -> TurnResponse:
    """`POST /api/v1/conversations/{id}/turns` — the chat write path.

    Delegates the whole Turn → Plan flow to `PlannerService`
    (ownership guard, Turn persistence, LLM Planning, snapshot
    freezing, Plan persistence). Everything the Planner decides it
    cannot do — no Tool matched, no LLM configured, unparseable
    answer — still answers 201 with `plan=None` plus a warning
    (ADR-0004 permits Plan-less Turns; silent failure is what this
    shape prevents). State conflicts raise: another user's
    conversation renders the 404 envelope, an archived one 409
    (ADR-0011).
    """
    outcome: TurnOutcome = await svc.submit_turn(
        conversation_id=conversation_id,
        user_id=user.id,
        content=body.content,
    )
    return TurnResponse(
        turn=_turn_to_dict(outcome.turn),
        plan=_plan_to_dict(outcome.plan) if outcome.plan is not None else None,
        warnings=outcome.warnings,
    )


@router.post(
    "/{conversation_id}/archive",
    response_model=ConversationResponse,
    summary="Manually archive (end) a conversation",
)
async def archive_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    svc: ConversationService = Depends(get_conversation_service),  # noqa: B008
) -> ConversationResponse:
    """`POST /api/v1/conversations/{id}/archive` — manual end.

    Per ADR-0011 the explicit "结束会话" path moves a row into
    `idle`. The Frontend's "结束会话" button hits this endpoint;
    re-archiving is a no-op (already-archived rows stay archived).
    """
    conv = await svc.archive(
        conversation_id=conversation_id,
        user_id=user.id,
    )
    return _conversation_to_response(conv)


@router.post(
    "/{conversation_id}/plan/approve",
    response_model=dict[str, Any],
    summary="HITL approve the conversation's pending Plan (T20 / #43)",
)
async def approve_plan(
    conversation_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    svc: ConversationService = Depends(get_conversation_service),  # noqa: B008
) -> dict[str, Any]:
    """`POST /api/v1/conversations/{id}/plan/approve` — HITL approval.

    Per ADR-0004 the Plan preview is mandatory: this endpoint is the
    "approve as-is" branch of the Plan-preview buttons (T20 / #43).
    The Drawer header (T19) renders one approve / reject pair bound
    to the latest Plan; the Worker (T21) picks up from here.

    The path is conversation-scoped (no `plan_id`) because per
    ADR-0005 a conversation has at most one "active" Plan at a time
    — that's the row the React Flow drawer is rendering. Status
    conflicts (Plan already approved / rejected / executing) raise
    409 with code `plan_not_pending`; cross-user access surfaces the
    same 404 envelope as an absent row.
    """
    plan = await svc.approve_plan(
        conversation_id=conversation_id,
        user_id=user.id,
    )
    return _plan_to_dict(plan)


@router.post(
    "/{conversation_id}/plan/reject",
    response_model=dict[str, Any],
    summary="HITL reject the conversation's pending Plan (T20 / #43)",
)
async def reject_plan(
    conversation_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    svc: ConversationService = Depends(get_conversation_service),  # noqa: B008
) -> dict[str, Any]:
    """`POST /api/v1/conversations/{id}/plan/reject` — HITL rejection.

    Mirrors `approve_plan` — same ownership + status-guard contract.
    The Turn stays so the user can refine the instruction and
    resubmit; a rejected Plan is terminal from the Worker's POV but
    not from the conversation's.
    """
    plan = await svc.reject_plan(
        conversation_id=conversation_id,
        user_id=user.id,
    )
    return _plan_to_dict(plan)


class EditPlanRequest(BaseModel):
    """Body of `PATCH /api/v1/conversations/{id}/plan` — T26 / #44 / ADR-0019.

    Carries the post-edit node list. Only `parameters` and `notes`
    fields are mutable; the repository's `record_edit` enforces
    "same node-ids, same `tool` per node" so a PATCH that tries to
    add / remove / repoint surfaces as 400 — the wire envelope
    stays minimal (the Frontend ships the full edited Plan) and
    structural invariants stay at the seam.
    """

    nodes: list[PlanNode] = Field(
        description=(
            "Edited node set. `node_id`s and `tool` slugs must match "
            "the persisted Plan exactly — only `parameters` and "
            "`notes` are mutable (ADR-0019)."
        ),
    )


@router.patch(
    "/{conversation_id}/plan",
    response_model=dict[str, Any],
    summary="HITL edit the conversation's pending / modified Plan (T26 / #44)",
)
async def edit_plan(
    conversation_id: str,
    body: EditPlanRequest,
    user: User = Depends(get_current_user),  # noqa: B008
    svc: ConversationService = Depends(get_conversation_service),  # noqa: B008
) -> dict[str, Any]:
    """`PATCH /api/v1/conversations/{id}/plan` — HITL Plan edit.

    Per ADR-0019 the business user can tweak a Plan's per-node
    `parameters` / `notes` before approving. The endpoint is the
    sibling of `approve_plan` / `reject_plan`: same conversation-
    scoped path, same ownership guard, same status-guard surface
    (`pending` or `modified` are editable; already-approved /
    executing / rejected raise `PlanNotPendingError`, 409).

    The diff is computed at the service seam and lands in
    `audit_logs` (T26 acceptance criterion: 审计含 diff) — a future
    audit UI replays "what changed" without re-reading the Plan
    row. The Plan comes back as `modified` so the React Flow drawer
    can refresh without re-fetching.

    Acceptance criteria:

    * PATCH 改 param 接受 — happy path returns 200 + status
      `modified` with the new parameter values persisted.
    * 不可增删节点 — added / removed / repointed nodes surface as
      `validation_error` (400) from `PlanRepository.record_edit`,
      and the persisted Plan is untouched.
    * 审计含 diff — one `audit_logs` row is appended per edit,
      with the diff in `response` and a `plan.edit` `tool_name`
      sentinel.
    """
    plan = await svc.edit_plan(
        conversation_id=conversation_id,
        user_id=user.id,
        edited_nodes=body.nodes,
    )
    return _plan_to_dict(plan)


class PlanExecutionResponse(BaseModel):
    """Wire shape of `POST /api/v1/conversations/{id}/plan/execute` — T21 / #18, T22 / #19.

    Returns the post-execution Plan (status `succeeded` / `failed`)
    plus the `audit_log_ids` the Frontend can render alongside the
    per-node lifecycle. The per-node execution detail lives on
    `plan_executions.node_results` and is fetched by a future
    conversation-detail endpoint; T21 only ships the high-level
    envelope.

    T22 adds the streaming final-answer fields:

    * `assistant_turn_id` — the persisted `assistant` Turn when the
      final-answer LLM produced a non-empty reply. `None` on every
      degraded path (LLM not configured, Plan failed, empty stream).
    * `answer_degraded` — the human-readable reason when the LLM
      step was skipped. Empty on the happy path so the chat panel
      renders nothing extra.
    """

    plan: dict[str, Any] = Field(
        description="The post-execution Plan row."
    )
    execution_id: str = Field(
        description="ObjectId of the `plan_executions` row created for this run."
    )
    audit_log_ids: list[str] = Field(
        description="One `audit_logs` row id per node, in execution order."
    )
    assistant_turn_id: str | None = Field(
        default=None,
        description=(
            "ObjectId of the `assistant` Turn persisted by the final-answer "
            "stream (T22 / #19). `null` when the LLM step was skipped "
            "(degraded configuration, Plan failed, empty stream)."
        ),
    )
    answer_degraded: str = Field(
        default="",
        description=(
            "Human-readable reason the final-answer stream was skipped, "
            "empty on success. Surfaced to the chat panel."
        ),
    )


@router.post(
    "/{conversation_id}/plan/execute",
    response_model=PlanExecutionResponse,
    summary="Execute the conversation's approved Plan (T21 / #18, T22 / #19)",
)
async def execute_plan(
    conversation_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    svc: ConversationService = Depends(get_conversation_service),  # noqa: B008
    executor: PlanExecutor = Depends(get_plan_executor),  # noqa: B008
    answer_service: AnswerService = Depends(get_answer_service),  # noqa: B008
    plan_execution_repo: PlanExecutionRepository = Depends(  # noqa: B008
        get_plan_execution_repository
    ),
) -> PlanExecutionResponse:
    """`POST /api/v1/conversations/{id}/plan/execute` — Worker + answer trigger.

    Per ADR-0004 the Plan preview is mandatory and one-shot: the
    HITL flow is approve → execute. Approval flips the Plan to
    `approved`; this endpoint drives the Worker (T21) over every
    node, writes the audit trail, and (T22 / #19) streams the LLM
    final-answer reply on a successful run.

    Cross-user access surfaces the same 404 envelope as an absent
    conversation; a Plan that isn't in `approved` / `modified` raises
    409 (the same envelope as `PlanNotPendingError`).

    Failures (HITL escalation, schema violation, upstream error)
    land here as a 200 with `plan.status = "failed"` — the call
    succeeded, the Plan didn't. The audit log ids tell the Frontend
    where to drill in. The final-answer stream is skipped on every
    non-`succeeded` terminal (per `AnswerService.stream_final_answer`'s
    contract); an LLM-layer error during streaming surfaces as the
    same degradation warning the Planner path uses, never as a 5xx.
    """
    plan = await svc.get_latest_approved_plan(
        conversation_id=conversation_id,
        user_id=user.id,
    )
    turn = await svc.get_latest_turn_for_plan(plan)
    outcome: PlanExecutionOutcome = await executor.execute_plan(
        plan=plan,
        actor_id=user.id,
        conversation_id=conversation_id,
        turn_id=turn.id,
    )

    # T22 / #19 — on a successful Plan, fetch the per-node results
    # and stream the final-answer reply. We reload from the
    # repository rather than threading `node_results` through the
    # executor's outcome to keep T21's outcome shape stable.
    assistant_turn_id: str | None = None
    answer_degraded: str = ""
    if outcome.plan.status == "succeeded":
        execution_row = await plan_execution_repo.get(outcome.execution_id)
        try:
            answer_outcome = await answer_service.stream_final_answer(
                conversation_id=conversation_id,
                user_turn_id=turn.id,
                plan=outcome.plan,
                instruction=turn.content,
                node_results=execution_row.node_results,
            )
        except (LLMConfigurationError, PromptUnavailableError, LLMGenerationError):
            # Same degradation envelope the Planner path uses — an
            # LLM-layer hiccup must not turn a successful Plan into
            # a 5xx. The chat panel renders the tool outcomes from
            # the audit log; the user just doesn't see an LLM
            # summary.
            answer_degraded = (
                "Final-answer LLM stream failed; Plan results are visible "
                "in the audit log without an LLM-written summary."
            )
        else:
            if answer_outcome.assistant_turn is not None:
                assistant_turn_id = answer_outcome.assistant_turn.id
            answer_degraded = answer_outcome.degraded

    return PlanExecutionResponse(
        plan=_plan_to_dict(outcome.plan),
        execution_id=outcome.execution_id,
        audit_log_ids=outcome.audit_log_ids,
        assistant_turn_id=assistant_turn_id,
        answer_degraded=answer_degraded,
    )


__all__ = ["router"]
