"""`/api/v1/conversations` router — T10 / #40, T18 / #16.

The thin HTTP seam for conversation CRUD and turn submission. Five
endpoints per ADR-0031:

* `POST /api/v1/conversations` — create.
* `GET  /api/v1/conversations` — list (with optional `status` filter).
* `GET  /api/v1/conversations/{id}` — detail (with turns + plans).
* `POST /api/v1/conversations/{id}/turns` — submit a user Turn and
  run the Planner (T18). Synchronous for now: the response carries
  the generated Plan (status `pending`, awaiting the HITL preview —
  ADR-0004); the SSE event stream `plan.generated` etc. is T23 (#20).
* `POST /api/v1/conversations/{id}/archive` — manual archive
  (transitions `active` / `idle` → `idle`, ADR-0011).

The router is intentionally thin: business logic lives in
`app.conversations.service.ConversationService` (CRUD) and
`app.planner.service.PlannerService` (turn → Plan). This file exists
only to translate Pydantic wire shapes into service calls and back.

Authentication is supplied by `get_current_user` (T09 / #10), which
returns the canonical `User` row. Ownership is then enforced at the
service seam — a cross-user lookup surfaces the same `not_found`
envelope as an absent row (ADR-0002).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.conversations.service import (
    ConversationDetail,
    ConversationService,
)
from app.db.dependencies import get_conversation_service, get_planner_service
from app.db.schemas import (
    Conversation,
    ConversationStatus,
    Plan,
    Turn,
    User,
)
from app.planner.service import PlannerService, TurnOutcome
from app.security.auth import get_current_user

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

    Wraps the conversation row alongside the message history and
    the Plan DAG history. The Frontend's chat panel reads `turns`;
    the React Flow renderer reads `plans` (T19).
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
    detail response so new Plan fields don't force a client
    redeploy.
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

    Per ADR-0011 the explicit "结束会话" path moves the row into
    `idle`. The Frontend's "结束会话" button hits this endpoint;
    re-archiving is a no-op (already-archived rows stay archived).
    """
    conv = await svc.archive(
        conversation_id=conversation_id,
        user_id=user.id,
    )
    return _conversation_to_response(conv)


__all__ = ["router"]