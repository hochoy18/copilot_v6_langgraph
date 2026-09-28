"""Conversation service — T10 / #40.

The router (`app.api.conversations`) translates HTTP envelopes; the
three repositories (`conversations` / `turns` / `plans`) translate
Mongo; this module owns the business rules that bridge the two.

Design notes:

* **Ownership at the seam.** Every read or mutation checks
  `conversation.user_id == current_user.id` and surfaces a cross-user
  lookup as `ConversationAccessDeniedError` — the same `not_found`
  envelope an absent conversation would surface. ADR-0005 treats a
  conversation as a private resource per user; telling another user
  "this exists but isn't yours" leaks existence.
* **`archive` transitions to `idle`, not `archived`.** Per ADR-0011
  the manual "结束会话" path puts a conversation into `idle`; the
  `idle → archived` move is the sweep job's responsibility (T39).
  Already-archived rows stay archived — repeated archives are a
  no-op rather than an error.
* **Detail read composes three repositories.** The detail endpoint
  returns the conversation plus its turns and its plans. The
  repositories have no notion of "ownership"; the service stitches
  the read and the check together. Keeping the composition here
  (not in the router) means a future Plan-editing endpoint can
  reuse the same ownership guard without re-deriving it.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.conversations.errors import (
    ConversationAccessDeniedError,
    PlanNotPendingError,
)
from app.db.schemas import (
    Conversation,
    ConversationCreate,
    ConversationStatus,
    Plan,
    PlanStatus,
    Turn,
)
from app.repositories.conversations import ConversationRepository
from app.repositories.plans import PlanRepository
from app.repositories.turns import TurnRepository

# How many turns to fold into the detail response by default. The
# Frontend's chat panel renders a fixed-height scrollback so an
# unbounded fetch would be wasted bandwidth. Higher limits are
# reachable via cursor pagination (T26 / ADR-0007) — T10 only ships
# the default.
DEFAULT_TURN_LIMIT: int = 200

# Same default for plans: one conversation rarely has more than a few
# dozen Plans over its lifetime, so this is a defensive ceiling more
# than a UX constraint.
DEFAULT_PLAN_LIMIT: int = 50


@dataclass(frozen=True)
class ConversationDetail:
    """The composed payload of `ConversationService.get_detail`.

    `conversation` carries the canonical conversation row; `turns`
    and `plans` are the message history and the Plan DAG history in
    chronological order. Pydantic-flavoured data classes are not
    required — the router owns the wire-shape serialisation.
    """

    conversation: Conversation
    turns: list[Turn]
    plans: list[Plan]


class ConversationService:
    """Conversation CRUD with ownership enforcement — T10 / #40.

    Holds references to the three repositories touched by the
    conversation endpoints. Stateless beyond those references — a
    single instance can be reused across requests.
    """

    def __init__(
        self,
        *,
        conversation_repository: ConversationRepository,
        turn_repository: TurnRepository,
        plan_repository: PlanRepository,
    ) -> None:
        self._conversations = conversation_repository
        self._turns = turn_repository
        self._plans = plan_repository

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, *, user_id: str, title: str = "") -> Conversation:
        """Create a new conversation owned by `user_id`.

        New conversations are `active` by definition (ADR-0011): the
        user just opened them, the idle sweep won't reclassify them
        on the next tick. `last_activity_at` is stamped to `now()` by
        `ConversationRepository.create` so the "active in last 15 min"
        predicate holds from the moment the row lands.

        `title` defaults to empty — the Planner titles later (T18),
        or the Frontend renders a slice of the first user Turn
        (ADR-0029).
        """
        data = ConversationCreate(
            user_id=user_id,
            title=title,
            status="active",
        )
        return await self._conversations.create(data)

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list_for_user(
        self,
        *,
        user_id: str,
        status: ConversationStatus | None = None,
        limit: int = 50,
        after_id: str | None = None,
    ) -> list[Conversation]:
        """List a user's conversations, newest activity first.

        `status` is optional; the Frontend's three-tab view (active /
        idle / archived — ADR-0011, T11) sends it down. The repo
        already orders by `last_activity_at DESC` so the Frontend
        renders top-to-bottom without re-sorting.
        """
        return await self._conversations.list_by_user(
            user_id,
            status=status,
            limit=limit,
            after_id=after_id,
        )

    # ------------------------------------------------------------------
    # Detail
    # ------------------------------------------------------------------

    async def get_detail(
        self,
        *,
        conversation_id: str,
        user_id: str,
    ) -> ConversationDetail:
        """Fetch a conversation plus its turns and plans.

        Cross-user access raises `ConversationAccessDeniedError` (404
        envelope, same shape as `NotFoundError`); absent rows do the
        same — callers cannot tell the two apart.

        The composition order is deliberate:

        1. Look up the conversation. If it doesn't exist *or* the
           owner doesn't match, raise the 404.
        2. Fetch turns and plans only after the ownership check
           lands. A stranger probing an unknown id pays exactly one
           Mongo round trip; a stranger probing a known id pays one
           + two fan-out queries.
        """
        conversation = await self._conversations.get(conversation_id)
        _assert_owner(conversation, user_id)

        # Fan-out: turns and plans in parallel would be a future
        # optimisation; the latency cost of sequential reads at MVP
        # scale is negligible and keeps the test seam simple.
        turns = await self._turns.list_by_conversation(
            conversation_id,
            limit=DEFAULT_TURN_LIMIT,
        )
        plans = await self._plans.list_by_conversation(
            conversation_id,
            limit=DEFAULT_PLAN_LIMIT,
        )

        return ConversationDetail(
            conversation=conversation,
            turns=turns,
            plans=plans,
        )

    # ------------------------------------------------------------------
    # Archive (manual end → idle)
    # ------------------------------------------------------------------

    async def archive(
        self,
        *,
        conversation_id: str,
        user_id: str,
    ) -> Conversation:
        """Manual archive: transition `active` / `idle` → `idle`.

        Per ADR-0011 the explicit "结束会话" path moves the row into
        `idle`. Already-archived rows stay archived; the sweep job
        (T39) is the only path that takes a row from `idle` to
        `archived`, so re-archiving an archived row is a no-op rather
        than a state-machine error.

        Cross-user access raises `ConversationAccessDeniedError`
        (404 envelope). Absent rows raise `NotFoundError`. The two
        shapes match by inheritance.
        """
        conversation = await self._conversations.get(conversation_id)
        _assert_owner(conversation, user_id)

        # Re-archiving an already-idle row is a true no-op — no Mongo
        # write, no `updated_at` bump. Already-archived rows stay
        # archived (the sweep job, T39, is the only path that takes
        # a row from `idle` to `archived`).
        if conversation.status in ("idle", "archived"):
            return conversation

        return await self._conversations.set_status(conversation_id, "idle")

    # ------------------------------------------------------------------
    # Plan approve / reject (T20 / #43)
    # ------------------------------------------------------------------

    async def approve_plan(
        self,
        *,
        conversation_id: str,
        user_id: str,
    ) -> Plan:
        """HITL approval: flip the conversation's pending Plan to `approved`.

        Per ADR-0004 the Plan preview is a one-shot checkpoint — the
        business user approves the Plan "as-is" here; edits (ADR-0019)
        are a sibling endpoint that lands in T26. Ownership guard
        mirrors `archive`: a stranger can't tell "this exists but
        isn't yours" apart from "this doesn't exist". The Plan must
        be in `pending` — already-approved / rejected / executing
        rows raise `PlanNotPendingError` (409) so the audit lifecycle
        never rewinds.

        The endpoint is conversation-scoped (no `plan_id` in the
        path) because per ADR-0005 a conversation has at most one
        "active" Plan at a time. The latest Plan is the one the
        React Flow drawer (T19) is rendering — that's the row the
        button in the drawer header approves.
        """
        return await self._decide_plan(
            conversation_id=conversation_id,
            user_id=user_id,
            target_status="approved",
        )

    async def reject_plan(
        self,
        *,
        conversation_id: str,
        user_id: str,
    ) -> Plan:
        """HITL rejection: flip the conversation's pending Plan to `rejected`.

        Mirrors `approve_plan` — same ownership + status-guard
        contract. A rejected Plan keeps the conversation alive
        (ADR-0011's `archived` transition is independent of Plan
        lifecycle); the user can refine the instruction and submit
        a new Turn, which produces a new Plan row.
        """
        return await self._decide_plan(
            conversation_id=conversation_id,
            user_id=user_id,
            target_status="rejected",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _decide_plan(
        self,
        *,
        conversation_id: str,
        user_id: str,
        target_status: PlanStatus,
    ) -> Plan:
        """Shared approval / rejection seam.

        Order: ownership check first (404 envelope on cross-user or
        absent rows), then Plan lookup, then status guard (409 on
        non-pending Plans). The repo's `set_status` is the atomic
        write so audit subscribers see exactly one status event.
        """
        conversation = await self._conversations.get(conversation_id)
        _assert_owner(conversation, user_id)

        plan = await self._plans.get_latest_for_conversation(conversation_id)
        if plan.status != "pending":
            raise PlanNotPendingError(
                details={"plan_id": plan.id, "current_status": plan.status},
            )

        return await self._plans.set_status(plan.id, target_status)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assert_owner(conversation: Conversation, user_id: str) -> None:
    """Raise `ConversationAccessDeniedError` if `user_id` doesn't own `conversation`.

    Centralised so every service method enforces ownership with one
    pair of eyes. The detail envelope carries the caller's user id
    but never the conversation id — see the rationale in
    `errors.ConversationAccessDeniedError`.
    """
    if conversation.user_id != user_id:
        raise ConversationAccessDeniedError(
            details={"user_id": user_id},
        )


__all__ = [
    "ConversationService",
    "ConversationDetail",
    "DEFAULT_TURN_LIMIT",
    "DEFAULT_PLAN_LIMIT",
]