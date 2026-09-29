"""`PlanExecutor` — T21 / #18, T28 / #24.

The orchestrator that ties a Plan's approval to its execution. The
HITL "approve" endpoint (T20 / #43, `ConversationService.approve_plan`)
flips the Plan to `approved`; the Executor picks it up from there,
walks the DAG through the `ToolWorker` (concurrently where ADR-0012
permits), and writes `plan_executions` + `audit_logs` rows along
the way.

Acceptance criteria (issue #18, then #24 for the parallel layer):

1. **批准后 echo 跑通** — `execute_plan(plan)` flips the Plan to
   `executing`, runs every node, and on the happy path flips it to
   `succeeded`.
2. **read 失败自动重试 2 次** — delegated to the Worker (the executor
   just surfaces the Worker's HITL signal).
3. **write 失败立即停下 HITL** — same delegation; the executor
   translates the Worker's `HITLRequiredError` into a Plan-level
   `failed` + audit row.
4. **使用快照不读最新 Tool 定义** — the executor reads `tools` ONLY
   to recover the `credentials_ref` FK pointer (the snapshot strips
   it per ADR-0027). The Tool definition used for execution is the
   snapshot's.
5. **凭证调用瞬间注入** — the executor passes `credential_ref` into
   `ToolWorker.execute_with_credential`; the Worker decrypts inside
   its own call frame.
6. **并行分支同时执行** (T28) — sibling nodes run concurrently via
   `PlanDagRunner`; a downstream node only fires after every
   predecessor finishes, and a sibling's failure does not stop the
   other sibling.

T31 / #27 adds the long-term-memory side-effect: after the terminal
Plan writes land in MongoDB, the Executor summarises the Plan and
asks the optional `MilvusPlanHistoryWriter` to upsert the row into
Milvus `plan_history_vectors`. The write is best-effort — a Milvus
failure logs and swallows, never rolls back the Plan execution
(ADR-0008: Milvus is the derived index, MongoDB is the truth).

Why a separate module from the Worker: the Worker is a pure async
function over its inputs (test seam). The Executor is the
side-effecting glue: it owns Plan / PlanExecution / AuditLog writes
and emits the `tool.started` / `tool.finished` / `tool.failed` SSE
events (T23 consumes the same hook surface).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, cast

from app.conversations.errors import PlanNotPendingError
from app.db.schemas import (
    AuditLogCreate,
    Plan,
    PlanExecutionCreate,
    PlanNode,
    PlanNodeResult,
    PlanStatus,
    ToolRiskLevel,
    ToolSnapshot,
)
from app.memory.plan_history import (
    MilvusPlanHistoryWriter,
    build_plan_history_record,
)
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.plan_executions import PlanExecutionRepository
from app.repositories.plans import PlanRepository
from app.repositories.tools import ToolRepository
from app.repositories.turns import TurnRepository
from app.security.redactor import redact
from app.tools.dag import NodeRunOutcome, PlanDagRunner
from app.tools.worker import ToolCallResult, ToolWorker
from app.tools.worker_errors import (
    HITLRequiredError,
    SchemaViolationError,
    ToolWorkerError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanExecutionOutcome:
    """The terminal state of an Executor run.

    `plan` reflects the post-execution row (status `succeeded` /
    `failed`). `audit_log_ids` carries the per-node `audit_logs`
    rows so callers can deep-link to the audit UI without a
    follow-up query.
    """

    plan: Plan
    execution_id: str
    audit_log_ids: list[str]


class PlanExecutor:
    """Orchestrates the execution of one Plan — T21 / #18, T31 / #27.

    Stateless beyond the collaborator references; one instance per
    request is fine. The Worker is injected so tests can swap it for
    a stub that returns canned `ToolCallResult`s without touching
    the network. The Milvus writer is **optional** so the existing
    T21 test seam (no Milvus) keeps passing — when wired, T31 writes
    the terminal Plan's summary to long-term memory.
    """

    def __init__(
        self,
        *,
        plan_repository: PlanRepository,
        plan_execution_repository: PlanExecutionRepository,
        audit_log_repository: AuditLogRepository,
        tool_repository: ToolRepository,
        worker: ToolWorker,
        turn_repository: TurnRepository | None = None,
        milvus_writer: MilvusPlanHistoryWriter | None = None,
    ) -> None:
        self._plans = plan_repository
        self._plan_executions = plan_execution_repository
        self._audit = audit_log_repository
        self._tools = tool_repository
        self._worker = worker
        # T31 / #27 — optional collaborators for the long-term-memory
        # write path. `None` means "no memory write" — used by tests
        # that haven't wired T31 yet, and by deployments that haven't
        # enabled Milvus. The production lifespan wires both.
        self._turns = turn_repository
        self._milvus = milvus_writer

    # ------------------------------------------------------------------
    # Public seam
    # ------------------------------------------------------------------

    async def execute_plan(
        self,
        *,
        plan: Plan,
        actor_id: str,
        conversation_id: str,
        turn_id: str,
    ) -> PlanExecutionOutcome:
        """Run `plan` end-to-end.

        Caller contract: `plan.status` MUST be `approved` (or
        `modified`, the post-ADR-0019 sibling). Anything else is a
        state-machine error and raises `PlanNotPendingError` so the
        audit lifecycle never rewinds.

        Returns the final Plan plus the per-node audit log ids so
        callers can build the response envelope without a follow-up
        Mongo read.

        The method never raises `HITLRequiredError` — it catches the
        Worker's signal, marks the Plan `failed`, writes the audit
        row, and returns. The SSE bridge (T23) reads the Plan's
        terminal status to push `execution.completed` / `tool.failed`
        events.
        """
        if plan.status not in ("approved", "modified"):
            raise PlanNotPendingError(
                details={
                    "plan_id": plan.id,
                    "current_status": plan.status,
                    "expected": ["approved", "modified"],
                },
            )

        snapshots_by_name = {snap.name: snap for snap in plan.tool_snapshots}

        # Flip the Plan to `executing` so the React Flow renderer
        # (T19) and audit subscribers see the lifecycle transition.
        executing_plan = await self._plans.set_status(plan.id, "executing")

        # Open a `plan_executions` row — one row per attempt; a
        # re-execution creates a second row rather than mutating the
        # prior one (audit semantics).
        execution = await self._plan_executions.create(
            PlanExecutionCreate(
                plan_id=executing_plan.id,
                conversation_id=conversation_id,
                status="running",
                node_results=[],
            ),
        )

        audit_log_ids: list[str] = []
        had_failure = False

        # T28 / #24 — drive the DAG with concurrent siblings via
        # `PlanDagRunner`. The runner decides the dispatch order
        # (topological, parallel where independent) and short-circuits
        # downstream nodes whose upstream failed; this method just
        # feeds it the per-node side-effect closure and persists the
        # outcomes it gets back.
        runner = PlanDagRunner(
            run_node=self._build_run_one_node(
                plan=executing_plan,
                snapshots_by_name=snapshots_by_name,
                execution_id=execution.id,
                actor_id=actor_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                audit_log_ids=audit_log_ids,
            ),
        )

        outcomes = await runner.run(executing_plan)

        for node in executing_plan.nodes:
            outcome = outcomes.get(node.node_id)
            if outcome is None:
                # The runner should have produced an outcome for
                # every node; missing entries are a bug we want to
                # surface, not paper over.
                raise RuntimeError(
                    f"PlanDagRunner produced no outcome for node "
                    f"'{node.node_id}' (plan {executing_plan.id})"
                )
            if outcome.status == "failed":
                had_failure = True
            elif outcome.status == "skipped":
                # ADR-0012 — a downstream node whose upstream failed
                # is marked `skipped`; the runner short-circuited the
                # Worker call, so there's no audit row, only the
                # `plan_executions` status flip.
                await self._mark_node_skipped(
                    execution_id=execution.id,
                    node_id=node.node_id,
                )

        # Aggregate terminal: failed > succeeded.
        final_status: PlanStatus = "failed" if had_failure else "succeeded"
        aggregate: Literal["running", "completed", "failed", "aborted"] = (
            "failed" if had_failure else "completed"
        )
        await self._plan_executions.set_status(execution.id, aggregate)
        terminal_plan = await self._plans.set_status(executing_plan.id, final_status)

        # T31 / #27 + ADR-0008 — Milvus write happens AFTER every
        # Mongo write is durable. The write is best-effort: a Milvus
        # exception logs and swallows so a degraded Milvus cannot
        # unwind a successful Plan execution. Mongo is the truth;
        # Milvus is the derived index that T32 will recall from.
        await self._write_plan_history(terminal_plan)

        return PlanExecutionOutcome(
            plan=terminal_plan,
            execution_id=execution.id,
            audit_log_ids=audit_log_ids,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_run_one_node(
        self,
        *,
        plan: Plan,
        snapshots_by_name: dict[str, ToolSnapshot],
        execution_id: str,
        actor_id: str,
        conversation_id: str,
        turn_id: str,
        audit_log_ids: list[str],
    ) -> Any:
        """Build the per-node async callback the DAG runner invokes.

        The returned coroutine performs the same side-effects the
        pre-T28 executor did inline — Worker call, audit log write,
        `PlanExecution` row update — but returns a `NodeRunOutcome`
        instead of writing to the Plan status (the runner aggregates
        the terminal status across all nodes).

        `audit_log_ids` is mutated in-place as a side effect of the
        closure — keeping the executor's outcome shape stable across
        the T21 → T28 refactor. The DAG runner doesn't see the list;
        it just gets the closure back.
        """

        async def _run_one_node(node: PlanNode) -> NodeRunOutcome:
            snapshot = snapshots_by_name.get(node.tool)
            if snapshot is None:
                # Defensive — `PlanBase._validate_structure` already
                # enforces this, but a future schema loosening
                # shouldn't take down execution silently.
                error_envelope = self._envelope(
                    code="snapshot_missing",
                    message_en=(
                        f"Plan references Tool '{node.tool}' but no snapshot "
                        "is bound to it — cannot execute (ADR-0027)."
                    ),
                )
                return await self._record_node_failure(
                    plan=plan,
                    node=node,
                    snapshot_or_none=None,
                    execution_id=execution_id,
                    actor_id=actor_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    error_envelope=error_envelope,
                    retry_count=0,
                    audit_log_ids=audit_log_ids,
                )

            try:
                credential_ref = await self._resolve_credential_ref(
                    snapshot=snapshot,
                    node=node,
                )
                result = await self._worker.execute_with_credential(
                    plan_id=plan.id,
                    node=node,
                    snapshot=snapshot,
                    actor_id=actor_id,
                    credential_ref=credential_ref,
                )
            except HITLRequiredError as exc:
                # Worker surfaced HITL (read retries exhausted / write-
                # destructive first-failure per ADR-0017). Record the
                # failure and let the DAG runner continue — parallel
                # siblings still get a chance to finish. ADR-0012's
                # "等待 HITL 决策" gate is the post-execution pause
                # where the user decides retry / skip / abort for the
                # whole Plan; that's a follow-up surface, not in this
                # node's call frame.
                details = exc.details or {}
                return await self._record_node_failure(
                    plan=plan,
                    node=node,
                    snapshot_or_none=snapshot,
                    execution_id=execution_id,
                    actor_id=actor_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    error_envelope=details,
                    retry_count=self._retry_count_from_details(details),
                    audit_log_ids=audit_log_ids,
                )
            except (SchemaViolationError, ToolWorkerError) as exc:
                # Non-retriable from the executor's POV.
                details = exc.details or {}
                return await self._record_node_failure(
                    plan=plan,
                    node=node,
                    snapshot_or_none=snapshot,
                    execution_id=execution_id,
                    actor_id=actor_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    error_envelope=details,
                    retry_count=self._retry_count_from_details(details),
                    audit_log_ids=audit_log_ids,
                )

            await self._mark_node_succeeded(
                execution_id=execution_id,
                node_id=node.node_id,
                outcome=result,
            )
            audit_log_ids.append(
                await self._write_audit_log(
                    plan=plan,
                    snapshot_or_none=snapshot,
                    node=node,
                    actor_id=actor_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    outcome=result,
                    error_envelope=None,
                ),
            )
            return NodeRunOutcome(
                status="succeeded",
                request=result.request,
                response=result.response,
                started_at=result.started_at,
                finished_at=result.finished_at,
                retry_count=result.retry_count,
            )

        return _run_one_node

    async def _record_node_failure(
        self,
        *,
        plan: Plan,
        node: PlanNode,
        snapshot_or_none: ToolSnapshot | None,
        execution_id: str,
        actor_id: str,
        conversation_id: str,
        turn_id: str,
        error_envelope: dict[str, Any],
        retry_count: int,
        audit_log_ids: list[str],
    ) -> NodeRunOutcome:
        """Persist a failed node outcome and return the runner outcome.

        Centralises the four-step ritual the failure branches share:
        mark the `plan_executions` row `failed`, append an
        `audit_logs` row, and return a `NodeRunOutcome` carrying the
        envelope so the DAG runner can aggregate the Plan-level
        status. Extracted from `_run_one_node` so the three failure
        paths (`snapshot_missing`, `HITLRequiredError`,
        `SchemaViolationError | ToolWorkerError`) read uniformly
        and the audit / execution side-effects stay in one place.
        """
        await self._mark_node_failed(
            execution_id=execution_id,
            node_id=node.node_id,
            error_envelope=error_envelope,
        )
        audit_log_ids.append(
            await self._write_audit_log(
                plan=plan,
                snapshot_or_none=snapshot_or_none,
                node=node,
                actor_id=actor_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                outcome=None,
                error_envelope=error_envelope,
                retry_count=retry_count,
            ),
        )
        return NodeRunOutcome(
            status="failed",
            error_envelope=error_envelope,
            retry_count=retry_count,
        )

    async def _mark_node_skipped(
        self,
        *,
        execution_id: str,
        node_id: str,
    ) -> None:
        """Record a `skipped` outcome on the `plan_executions` row.

        Triggered by the DAG runner when a downstream node's upstream
        failed (ADR-0012). Audit log rows are NOT written for skipped
        nodes — the Worker never ran, so there's nothing to audit.
        """
        from datetime import datetime

        now = datetime.utcnow()
        await self._plan_executions.upsert_node_result(
            execution_id,
            PlanNodeResult(
                node_id=node_id,
                status="skipped",
                started_at=None,
                finished_at=now,
                request=None,
                response=None,
                error=None,
                retry_count=0,
            ),
        )

    async def _resolve_credential_ref(
        self,
        *,
        snapshot: ToolSnapshot,
        node: Any,  # PlanNode — kept loose to avoid a circular import comment
    ) -> str | None:
        """Recover the live Tool's `credentials_ref` for the Worker.

        The snapshot drops `credentials_ref` (ADR-0027 strips admin
        provenance), so the executor looks up the live Tool row to
        hand the FK pointer to the Worker. If the snapshot's `tool_id`
        is missing (older Plans / hand-built snapshots), the lookup
        falls back to a slug match by name.
        """
        tool = None
        if snapshot.tool_id is not None:
            try:
                tool = await self._tools.get_in_db(snapshot.tool_id)
            except Exception:  # pragma: no cover — defensive
                tool = None
        if tool is None:
            try:
                tool = await self._tools.get_by_name(snapshot.name)
            except Exception:  # pragma: no cover — defensive
                tool = None
        if tool is None:
            # Unauthenticated Tool (rare; usually internal health
            # pings). The Worker accepts `credential_ref=None`.
            return None
        return tool.credentials_ref

    async def _mark_node_succeeded(
        self,
        *,
        execution_id: str,
        node_id: str,
        outcome: ToolCallResult,
    ) -> None:
        """Record a terminal `succeeded` node outcome."""
        await self._plan_executions.upsert_node_result(
            execution_id,
            PlanNodeResult(
                node_id=node_id,
                status="succeeded",
                started_at=outcome.started_at,
                finished_at=outcome.finished_at,
                request=outcome.request,
                response=outcome.response,
                error=None,
                retry_count=outcome.retry_count,
            ),
        )

    async def _mark_node_failed(
        self,
        *,
        execution_id: str,
        node_id: str,
        error_envelope: dict[str, Any],
    ) -> None:
        """Record a terminal `failed` node outcome."""
        from datetime import datetime

        now = datetime.utcnow()
        await self._plan_executions.upsert_node_result(
            execution_id,
            PlanNodeResult(
                node_id=node_id,
                status="failed",
                started_at=now,
                finished_at=now,
                request=None,
                response=None,
                error=error_envelope,
                retry_count=0,
            ),
        )

    async def _write_audit_log(
        self,
        *,
        plan: Plan,
        snapshot_or_none: ToolSnapshot | None,
        node: Any,
        actor_id: str,
        conversation_id: str,
        turn_id: str,
        outcome: ToolCallResult | None,
        error_envelope: dict[str, Any] | None,
        retry_count: int | None = None,
    ) -> str:
        """Append an `audit_logs` row for one node outcome.

        `tool_snapshot` is the snapshot actually executed (ADR-0027),
        not a freshly-fetched live Tool — that's the whole point of
        the snapshot binding (T44's drift check relies on this).
        """
        if snapshot_or_none is None:
            # Defensive — Plan integrity was broken; emit a stub
            # snapshot so the audit row stays parseable.
            snapshot_for_audit = ToolSnapshot(
                name=node.tool,
                description="",
                risk_level="read",
                parameters_schema={},
                http_method="GET",
                http_url_template="",
                http_headers={},
                http_body_template=None,
            )
        else:
            snapshot_for_audit = snapshot_or_none

        if outcome is not None:
            status: Literal["running", "succeeded", "failed", "skipped"] = "succeeded"
            response_body = outcome.response
            risk_level: ToolRiskLevel = cast(ToolRiskLevel, outcome.risk_level)
            final_retry_count = outcome.retry_count
        else:
            status = "failed"
            response_body = None
            risk_level = snapshot_for_audit.risk_level
            final_retry_count = retry_count if retry_count is not None else 0

        # T33 / #29 — defence-in-depth credential scrub before the
        # audit row leaves the executor. The Worker's request
        # envelope is already redacted (`ToolCallResult.request`), but
        # `parameters` and `response_body` flow straight from the
        # upstream API into the row. An upstream that echoes an
        # `api_key` field (or a parameter that happens to be named
        # `password`) must not survive into MongoDB. The same
        # redactor scrubs the audit UI surface (T43) and any future
        # Langfuse trace (T40), so a single seam protects every sink.
        scrubbed_parameters = redact(node.parameters)
        scrubbed_response = redact(response_body) if response_body is not None else None
        scrubbed_error = redact(error_envelope) if error_envelope is not None else None

        row = await self._audit.create(
            AuditLogCreate(
                actor_id=actor_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                plan_id=plan.id,
                plan_execution_id="",  # back-filled below
                tool_name=snapshot_for_audit.name,
                tool_snapshot=snapshot_for_audit,
                parameters=scrubbed_parameters,
                response=scrubbed_response,
                status=status,
                error=scrubbed_error,
                risk_level=risk_level,
                retry_count=final_retry_count,
            ),
        )
        return row.id

    @staticmethod
    def _retry_count_from_details(details: dict[str, Any]) -> int:
        """Recover the Worker's retry count from the HITL envelope.

        The Worker embeds `attempts: [{"attempt": N, ...}, ...]` in
        the HITL envelope when the read budget exhausts; the last
        entry's `attempt` is the total call count, so `retry_count =
        attempts - 1`. For non-HITL failures (write/destructive,
        schema violation) there are no retries — return 0.
        """
        attempts = details.get("attempts") if isinstance(details, dict) else None
        if not attempts:
            return 0
        last = attempts[-1]
        if not isinstance(last, dict):
            return 0
        attempt_no = last.get("attempt")
        if not isinstance(attempt_no, int):
            return 0
        return max(0, attempt_no - 1)

    @staticmethod
    def _envelope(*, code: str, message_en: str) -> dict[str, Any]:
        """Build a structured error envelope for non-Worker failures."""
        return {
            "code": code,
            "message_en": message_en,
            "message_zh": message_en,  # MVP: messages are EN-only
        }

    # ------------------------------------------------------------------
    # T31 / #27 — long-term-memory write seam
    # ------------------------------------------------------------------

    async def _write_plan_history(self, terminal_plan: Plan) -> None:
        """Push the terminal Plan's summary into Milvus (best-effort).

        Two collaborators are required: a `MilvusPlanHistoryWriter` for
        the upsert and a `TurnRepository` for the user instruction that
        triggered the Plan (the Plan doc doesn't carry the instruction
        — only the FK to its triggering Turn). Missing either means
        the write is skipped silently: T31 ships the seam, and an
        operator that hasn't wired Milvus yet sees no behaviour change.

        Errors at any stage (Turn lookup, record build, upsert) are
        logged and swallowed — `MongoDB 先写后 Milvus` is the contract;
        the inverse ("Milvus fails ⇒ Plan rolls back") is explicitly
        forbidden by ADR-0008.
        """
        if self._milvus is None or self._turns is None:
            return
        if not terminal_plan.nodes:
            # Smalltalk / no-Plan path — skip so the index doesn't
            # accumulate noise rows the recall code can't act on.
            return
        try:
            turn = await self._turns.get(terminal_plan.turn_id)
            record = build_plan_history_record(
                plan=terminal_plan,
                user_instruction=turn.content,
            )
        except Exception:
            # Build-side errors (missing Turn, embedding failure).
            # Same swallow policy as the upsert path: the Plan is
            # already terminal in Mongo; the index can be rebuilt from
            # MongoDB later if the failure persists.
            logger.exception(
                "milvus plan_history record build failed (plan_id=%s)",
                terminal_plan.id,
            )
            return
        try:
            await self._milvus.upsert_summary(record)
        except Exception:
            logger.exception(
                "milvus plan_history upsert failed (plan_id=%s)",
                terminal_plan.id,
            )


__all__ = ["PlanExecutor", "PlanExecutionOutcome"]
