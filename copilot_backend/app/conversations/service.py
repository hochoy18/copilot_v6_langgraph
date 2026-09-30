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
from typing import Any

from app.conversations.errors import (
    ConversationAccessDeniedError,
    ConversationNotArchivedError,
    PlanNotPendingError,
)
from app.db.errors import ValidationError
from app.db.schemas import (
    AuditLogCreate,
    Conversation,
    ConversationCreate,
    ConversationStatus,
    Plan,
    PlanCreate,
    PlanNode,
    PlanStatus,
    ToolSnapshot,
    Turn,
    TurnCreate,
)
from app.repositories.audit_logs import AuditLogRepository
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


@dataclass(frozen=True)
class ReactivationResult:
    """Outcome of `ConversationService.reactivate` (T39 / #45).

    `conversation` is the freshly-active row the user is meant to
    continue in. `source_conversation_id` is the archived row the
    service read from — exposed on the wire so the Frontend can
    render "restored from <source>" without a follow-up read.
    `copied_turn_ids` and `copied_plan_id` are the FK ids the copy
    produced; the audit-log row T43 / #38 will eventually surface
    them so an auditor can replay the reactivate step-for-step.
    """

    conversation: Conversation
    source_conversation_id: str
    copied_turn_ids: list[str]
    copied_plan_id: str | None


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
        audit_log_repository: AuditLogRepository,
        memory_window_k: int = 5,
    ) -> None:
        self._conversations = conversation_repository
        self._turns = turn_repository
        self._plans = plan_repository
        self._audit = audit_log_repository
        self._memory_window_k = memory_window_k

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
    # Reactivate (archived → new active, copies K turns + latest Plan)
    # ------------------------------------------------------------------

    async def reactivate(
        self,
        *,
        conversation_id: str,
        user_id: str,
        title: str = "",
    ) -> ReactivationResult:
        """Reactivate an archived conversation (T39 / #45, ADR-0011).

        Per ADR-0011 reactivating an archived conversation creates a
        *new* `active` row that references the archived source. The
        service copies two things from the source so the user can
        continue seamlessly:

        1. **The most recent K turns** — same `memory_window_k` the
           Planner sees verbatim (ADR-0007). Older history stays on
           the archived source; a future planner-history deep-link
           can surface it without bloating the new conversation.
        2. **The latest Plan row** — `get_latest_for_conversation`
           returns the canonical most-recent Plan; its
           `tool_snapshots` (ADR-0027) carry the frozen Tool
           definitions so audit replay survives the reactivate.

        Turn / Plan copies get the new `conversation_id` so the
        chat panel reads cleanly. The `plan_id` FK on each copied
        Turn is preserved **only** when the original Turn pointed
        at the source's latest Plan (the one we copied); turns that
        referenced any older Plan lose the FK on copy because the
        older Plan row is in the source conversation the Frontend no
        longer shows. The Plan copy itself keeps the source Plan's
        `turn_id` verbatim as provenance — the triggering Turn row
        lives in the archived source, which the audit chain can
        still resolve via `reactivated_from_id`. The
        `reactivated_from_id` FK on the new conversation walks the
        audit chain back to the source.

        Ownership guard mirrors `archive`: cross-user access raises
        `ConversationAccessDeniedError` (404 envelope). A non-
        archived source raises `ConversationNotArchivedError` (409)
        so callers know to use `POST /conversations/{id}/archive`
        instead. The freshly-active row is returned to the route
        so the Frontend can re-mount the chat panel.
        """
        source = await self._conversations.get(conversation_id)
        _assert_owner(source, user_id)
        if source.status != "archived":
            raise ConversationNotArchivedError(
                details={
                    "conversation_id": source.id,
                    "current_status": source.status,
                },
            )

        # Insert the conversation first so we have a stable FK
        # for the Turn / Plan copies. The repository stamps the
        # `reactivated_from_id` pointer + bumps the counter.
        new_conversation = await self._conversations.create_from_reactivate(
            source=source,
            title=title,
        )

        copied_turn_ids, copied_plan_id = await self._copy_history_into(
            source=source,
            destination_id=new_conversation.id,
            limit=self._memory_window_k,
        )

        return ReactivationResult(
            conversation=new_conversation,
            copied_turn_ids=copied_turn_ids,
            copied_plan_id=copied_plan_id,
            source_conversation_id=source.id,
        )

    async def _copy_history_into(
        self,
        *,
        source: Conversation,
        destination_id: str,
        limit: int,
    ) -> tuple[list[str], str | None]:
        """Copy the source's recent K turns + latest Plan into the new conversation.

        Returns `(copied_turn_ids, copied_plan_id)` so the service
        caller can audit the copy. The Plan copy is `None` when the
        source had no Plans (a freshly-archived conversation that
        never reached the planner).

        FK rewriting:

        * The new Plan's `turn_id` points at the source's
          triggering Turn id verbatim. If that Turn is outside the
          copied K window, the new Plan still exists but no copied
          Turn references it (the audit UI surfaces the orphaned
          pointer; the chat panel just renders no DAG for it).
        * Each copied Turn's `plan_id` is preserved only when the
          original Turn referenced the latest Plan; turns that
          referenced older Plans (which we did not copy) get
          `plan_id=None` on copy to avoid dangling FKs.
        """
        # Plans first — the Turn copy decides which FKs to keep.
        copied_plan_id: str | None = None
        source_plan: Plan | None = None
        try:
            source_plan = await self._plans.get_latest_for_conversation(source.id)
        except Exception:  # noqa: BLE001 — No Plan is a documented branch
            source_plan = None
        if source_plan is not None:
            new_plan = await self._plans.create(
                PlanCreate(
                    conversation_id=destination_id,
                    turn_id=source_plan.turn_id,
                    status=source_plan.status,
                    nodes=list(source_plan.nodes),
                    edges=list(source_plan.edges),
                    tool_snapshots=list(source_plan.tool_snapshots),
                ),
            )
            copied_plan_id = new_plan.id

        # Fetch enough source turns to honor the K-window — the repo
        # is bounded by `limit`, so we may need to widen for the
        # K-from-tail slice. Use a large but bounded fetch (the
        # contract is "K most-recent", not "all history"), capped
        # at the documented `DEFAULT_TURN_LIMIT` to avoid unbounded
        # scans on long-lived sessions.
        fetch_limit = max(limit, DEFAULT_TURN_LIMIT)
        source_turns = await self._turns.list_by_conversation(
            source.id, limit=fetch_limit
        )

        copied_turn_ids: list[str] = []
        # We want the most-recent K turns; the Turn repo returns
        # ascending order so we slice from the tail.
        recent = source_turns[-limit:] if limit > 0 else []
        # `turn.plan_id` points at a `plans._id`; preserve the FK
        # only when the source Turn pointed at the latest source
        # Plan (the one we copied). Older Plan pointers are
        # dangling by construction — the source Plan rows live in
        # a conversation the Frontend no longer renders.
        latest_source_plan_id = source_plan.id if source_plan is not None else None
        for turn in recent:
            new_plan_id = (
                copied_plan_id if turn.plan_id == latest_source_plan_id else None
            )
            new_turn = await self._turns.create(
                TurnCreate(
                    conversation_id=destination_id,
                    role=turn.role,
                    content=turn.content,
                    plan_id=new_plan_id,
                    extra=dict(turn.extra),
                ),
            )
            copied_turn_ids.append(new_turn.id)

        return copied_turn_ids, copied_plan_id

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
    # Plan edit (T26 / #44 / ADR-0019)
    # ------------------------------------------------------------------

    async def edit_plan(
        self,
        *,
        conversation_id: str,
        user_id: str,
        edited_nodes: list[PlanNode],
    ) -> Plan:
        """HITL edit: apply `edited_nodes` to the conversation's latest
        pending / modified Plan and audit the diff.

        Per ADR-0019 the edit is **parameters / notes only** — the
        node set, the `tool` ↔ node binding, the edges, and the
        frozen tool_snapshots (ADR-0027) are immutable. The wire
        shape (`EditPlanRequest.nodes`) deliberately omits `edges`,
        so the edit endpoint cannot mutate the DAG topology; the
        repository's `record_edit` additionally re-validates the
        invariants on write as a defensive guard.

        Re-editing a `modified` Plan is allowed — the user may
        iterate parameters before approving. The endpoint is
        conversation-scoped (no `plan_id`) per ADR-0005.

        Order of operations: we **pre-validate** the structural
        invariants (set equality, tool binding) at the service
        seam so we can compute the diff against the same node-id
        set without a KeyError on a freshly-added id, then make a
        single `record_edit` write that persists the new nodes,
        the diff, and the `modified` status flag atomically.
        `record_edit` re-validates the same invariants defensively;
        a future repository change that loosens those checks won't
        silently let an invalid edit through.
        """
        conversation = await self._conversations.get(conversation_id)
        _assert_owner(conversation, user_id)

        current_plan = await self._plans.get_latest_for_conversation(conversation_id)
        if current_plan.status not in ("pending", "modified"):
            raise PlanNotPendingError(
                details={
                    "plan_id": current_plan.id,
                    "current_status": current_plan.status,
                    "expected": ["pending", "modified"],
                },
            )

        diff = _compute_edit_diff_safe(
            original_nodes=current_plan.nodes,
            edited_nodes=edited_nodes,
        )

        edited_plan = await self._plans.record_edit(
            current_plan.id,
            edited_nodes=edited_nodes,
            diff=diff,
        )

        await self._audit.create(_plan_edit_audit_row(plan=edited_plan, actor_id=user_id))

        return edited_plan

    # ------------------------------------------------------------------
    # Plan execution (T21 / #18)
    # ------------------------------------------------------------------

    async def get_owned_conversation(
        self,
        *,
        conversation_id: str,
        user_id: str,
    ) -> Conversation:
        """Fetch a single conversation with ownership check — T23 / #20.

        Returns the canonical `Conversation` row when `user_id` owns
        `conversation_id`. Cross-user access raises
        `ConversationAccessDeniedError` (same `not_found` envelope
        as an absent row, per ADR-0002).

        Used by the SSE stream endpoint: opening a stream only
        needs the ownership check, not the full turn / plan
        fan-out that `get_detail` performs. Keeping this method
        on the service (rather than reaching for the private
        `_conversations` repo handle from the route layer) preserves
        the seam that all other conversation endpoints use.
        """
        conversation = await self._conversations.get(conversation_id)
        _assert_owner(conversation, user_id)
        return conversation

    async def get_latest_approved_plan(
        self,
        *,
        conversation_id: str,
        user_id: str,
    ) -> Plan:
        """Return the conversation's latest `approved` / `modified` Plan.

        The Worker (T21 / #18) consumes from this seam: the React Flow
        drawer shows the latest Plan, and "execute" picks the
        approved / modified row (modified = post-ADR-0019 edit).
        Anything else (pending / rejected / executing / succeeded /
        failed) raises `PlanNotPendingError` (409) so the audit
        lifecycle never rewinds.
        """
        conversation = await self._conversations.get(conversation_id)
        _assert_owner(conversation, user_id)

        plan = await self._plans.get_latest_for_conversation(conversation_id)
        if plan.status not in ("approved", "modified"):
            raise PlanNotPendingError(
                details={
                    "plan_id": plan.id,
                    "current_status": plan.status,
                    "expected": ["approved", "modified"],
                },
            )
        return plan

    async def get_latest_turn_for_plan(self, plan: Plan) -> Turn:
        """Return the Turn that triggered `plan`.

        Used by the executor to back-fill the audit log row's
        `turn_id` FK pointer. The Plan always carries the FK, but
        the Turn repo is the canonical read seam.
        """
        from app.db.errors import NotFoundError

        try:
            return await self._turns.get(plan.turn_id)
        except NotFoundError as exc:  # pragma: no cover — defensive
            raise NotFoundError(
                message_en=(
                    f"Plan {plan.id} references missing Turn {plan.turn_id}"
                ),
                details={"plan_id": plan.id, "turn_id": plan.turn_id},
            ) from exc

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


def _compute_edit_diff_safe(
    *,
    original_nodes: list[PlanNode],
    edited_nodes: list[PlanNode],
) -> dict[str, Any]:
    """Pre-validate the edit invariants + build the `by_node_id` diff.

    Two responsibilities stacked together because they share the
    same pre-condition (the node-id sets must match):

    1. **Reject set-shape / tool-binding violations** with the same
       `ValidationError` envelope `PlanRepository.record_edit` would
       raise on write. Surfacing them at the service seam keeps the
       diff computation safe (no KeyError on a freshly-added id)
       and lets us make a single write below instead of
       write-then-update.
    2. **Build the diff** for every changed `parameters.<key>` /
       `notes` field. Per-node removal of a parameter (the key was
       on the original but absent from the edit) is recorded as a
       change with `after=None` so a downstream auditor can see
       "this argument was dropped" rather than missing it entirely.

    The output schema (`{"by_node_id": {"<path>": {"before",
    "after"}}}`) is dictated by ADR-0019.
    """
    original_by_id = {node.node_id: node for node in original_nodes}
    edited_by_id = {node.node_id: node for node in edited_nodes}

    added = sorted(set(edited_by_id) - set(original_by_id))
    removed = sorted(set(original_by_id) - set(edited_by_id))
    if added or removed:
        raise ValidationError(
            message_en="Plan edits cannot add or remove nodes (ADR-0019)",
            details={"added": added, "removed": removed},
        )

    repointed = {
        node_id: {"before": original_by_id[node_id].tool, "after": edited.tool}
        for node_id, edited in edited_by_id.items()
        if edited.tool != original_by_id[node_id].tool
    }
    if repointed:
        raise ValidationError(
            message_en="Plan edits cannot change which Tool a node invokes (ADR-0027)",
            details={"repointed": repointed},
        )

    by_node_id: dict[str, dict[str, dict[str, Any]]] = {}
    for node_id, edited in edited_by_id.items():
        original = original_by_id[node_id]
        per_node: dict[str, dict[str, Any]] = {}
        all_param_keys = set(original.parameters) | set(edited.parameters)
        for key in sorted(all_param_keys):
            before_value = original.parameters.get(key)
            after_value = edited.parameters.get(key)
            if before_value != after_value:
                per_node[f"parameters.{key}"] = {
                    "before": before_value,
                    "after": after_value,
                }
        if edited.notes != original.notes:
            per_node["notes"] = {"before": original.notes, "after": edited.notes}
        if per_node:
            by_node_id[node_id] = per_node
    return {"by_node_id": by_node_id}


def _plan_edit_audit_row(*, plan: Plan, actor_id: str) -> AuditLogCreate:
    """Build the `audit_logs` row for a Plan edit (T26 / ADR-0019).

    A Plan edit isn't a Tool call — there's no `tool_snapshot`,
    no `parameters`, no `response` from an upstream API. The audit
    row carries the diff (already computed by
    `_compute_edit_diff_safe` and persisted on `plan.edited_diff`)
    inside the `response` field so a single read of `audit_logs`
    answers "what changed for this Plan?" without joining the Plan
    row. `tool_name` is the sentinel `plan.edit` so the audit UI
    (T43) can render the row under a "user edits" section rather
    than the Tool-call table.

    `tool_snapshot` uses the same empty-string stub the executor's
    snapshot-missing fallback uses (`tools.executor._write_audit_log`):
    a Plan-edit isn't a Tool call, so no method/URL really apply.
    Reusing that fallback keeps a single shape for "non-Tool"
    audit rows so a future reader doesn't need to special-case
    plan.edit vs snapshot-missing.
    """
    return AuditLogCreate(
        actor_id=actor_id,
        conversation_id=plan.conversation_id,
        turn_id=plan.turn_id,
        plan_id=plan.id,
        plan_execution_id=None,
        tool_name="plan.edit",
        tool_snapshot=ToolSnapshot(
            name="plan.edit",
            description="Business-user edit of a Plan's node parameters (ADR-0019).",
            risk_level="read",
            parameters_schema={},
            http_method="",
            http_url_template="",
            http_headers={},
            http_body_template=None,
        ),
        parameters={},
        response=plan.edited_diff,
        status="succeeded",
        error=None,
        risk_level="read",
        retry_count=0,
    )


__all__ = [
    "ConversationService",
    "ConversationDetail",
    "DEFAULT_TURN_LIMIT",
    "DEFAULT_PLAN_LIMIT",
    "ReactivationResult",
]