"""`PlanExecutor` — T21 / #18.

The orchestrator that ties a Plan's approval to its execution. The
HITL "approve" endpoint (T20 / #43, `ConversationService.approve_plan`)
flips the Plan to `approved`; the Executor picks it up from there,
walks the DAG node by node through the `ToolWorker`, and writes
`plan_executions` + `audit_logs` rows along the way.

Acceptance criteria (issue #18):

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

The MVP executor walks the DAG sequentially. Parallel branches land
in T25 / T28 — they read the same Worker seam, so swapping in a
topological scheduler here is a no-op for downstream consumers.

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
    PlanNodeResult,
    PlanStatus,
    ToolRiskLevel,
    ToolSnapshot,
)
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.plan_executions import PlanExecutionRepository
from app.repositories.plans import PlanRepository
from app.repositories.tools import ToolRepository
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
    """Orchestrates the execution of one Plan — T21 / #18.

    Stateless beyond the collaborator references; one instance per
    request is fine. The Worker is injected so tests can swap it for
    a stub that returns canned `ToolCallResult`s without touching
    the network.
    """

    def __init__(
        self,
        *,
        plan_repository: PlanRepository,
        plan_execution_repository: PlanExecutionRepository,
        audit_log_repository: AuditLogRepository,
        tool_repository: ToolRepository,
        worker: ToolWorker,
    ) -> None:
        self._plans = plan_repository
        self._plan_executions = plan_execution_repository
        self._audit = audit_log_repository
        self._tools = tool_repository
        self._worker = worker

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
        final_status: PlanStatus = "succeeded"

        for node in executing_plan.nodes:
            snapshot = snapshots_by_name.get(node.tool)
            if snapshot is None:
                # Defensive — `PlanBase._validate_structure` already
                # enforces this, but a future schema loosening
                # shouldn't take down execution silently.
                had_failure = True
                final_status = "failed"
                error_envelope = self._envelope(
                    code="snapshot_missing",
                    message_en=(
                        f"Plan references Tool '{node.tool}' but no snapshot "
                        "is bound to it — cannot execute (ADR-0027)."
                    ),
                )
                await self._mark_node_failed(
                    execution_id=execution.id,
                    node_id=node.node_id,
                    error_envelope=error_envelope,
                )
                audit_log_ids.append(
                    await self._write_audit_log(
                        plan=executing_plan,
                        snapshot_or_none=None,
                        node=node,
                        actor_id=actor_id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        outcome=None,
                        error_envelope=error_envelope,
                    ),
                )
                break

            try:
                credential_ref = await self._resolve_credential_ref(
                    snapshot=snapshot,
                    node=node,
                )
                result = await self._worker.execute_with_credential(
                    plan_id=executing_plan.id,
                    node=node,
                    snapshot=snapshot,
                    actor_id=actor_id,
                    credential_ref=credential_ref,
                )
            except HITLRequiredError as exc:
                # Worker surfaced HITL — record the failure and stop
                # the Plan. The conversation stays alive so the user
                # can refine the instruction.
                had_failure = True
                final_status = "failed"
                details = exc.details or {}
                await self._mark_node_failed(
                    execution_id=execution.id,
                    node_id=node.node_id,
                    error_envelope=details,
                )
                audit_log_ids.append(
                    await self._write_audit_log(
                        plan=executing_plan,
                        snapshot_or_none=snapshot,
                        node=node,
                        actor_id=actor_id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        outcome=None,
                        error_envelope=details,
                        retry_count=self._retry_count_from_details(details),
                    ),
                )
                break
            except (SchemaViolationError, ToolWorkerError) as exc:
                # Non-retriable from the executor's POV. Same
                # bookkeeping as HITL but the Plan status is `failed`
                # rather than awaiting further decision.
                had_failure = True
                final_status = "failed"
                details = exc.details or {}
                await self._mark_node_failed(
                    execution_id=execution.id,
                    node_id=node.node_id,
                    error_envelope=details,
                )
                audit_log_ids.append(
                    await self._write_audit_log(
                        plan=executing_plan,
                        snapshot_or_none=snapshot,
                        node=node,
                        actor_id=actor_id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        outcome=None,
                        error_envelope=details,
                        retry_count=self._retry_count_from_details(details),
                    ),
                )
                break
            else:
                await self._mark_node_succeeded(
                    execution_id=execution.id,
                    node_id=node.node_id,
                    outcome=result,
                )
                audit_log_ids.append(
                    await self._write_audit_log(
                        plan=executing_plan,
                        snapshot_or_none=snapshot,
                        node=node,
                        actor_id=actor_id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        outcome=result,
                        error_envelope=None,
                    ),
                )

        # Aggregate terminal: failed > succeeded.
        aggregate: Literal["running", "completed", "failed", "aborted"] = (
            "failed" if had_failure else "completed"
        )
        await self._plan_executions.set_status(execution.id, aggregate)
        terminal_plan = await self._plans.set_status(executing_plan.id, final_status)
        return PlanExecutionOutcome(
            plan=terminal_plan,
            execution_id=execution.id,
            audit_log_ids=audit_log_ids,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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

        row = await self._audit.create(
            AuditLogCreate(
                actor_id=actor_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                plan_id=plan.id,
                plan_execution_id="",  # back-filled below
                tool_name=snapshot_for_audit.name,
                tool_snapshot=snapshot_for_audit,
                parameters=node.parameters,
                response=response_body,
                status=status,
                error=error_envelope,
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


__all__ = ["PlanExecutor", "PlanExecutionOutcome"]
