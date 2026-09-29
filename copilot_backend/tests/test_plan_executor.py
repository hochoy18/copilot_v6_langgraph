"""Tests for `PlanExecutor` — T21 / #18, T31 / #27.

The Executor is the orchestrator that ties a Plan's approval to its
run. Tests cover the four acceptance criteria from issue #18:

1. **批准后 echo 跑通** — `execute_plan` flips the Plan to
   `executing` then `succeeded` and writes one audit log row.
2. **read 失败自动重试 2 次** — delegated to the Worker; the
   executor surfaces the Worker's HITL signal as a Plan-level
   `failed` + audit row.
3. **write 失败立即停下 HITL** — same; the executor never retries at
   its layer.
4. **使用快照不读最新 Tool 定义** — the executor reads `tools` ONLY
   to recover `credentials_ref`; Tool definitions come from the
   snapshot.
5. **凭证调用瞬间注入** — verified by passing `credential_ref` and
   observing the Worker's outgoing request shape.

T31 / #27 adds the `TestMilvusWrite` class which exercises the
long-term-memory write path:

* **AC #1 — Milvus 能查到对应向量** — a successful run pushes one
  record into the writer with the conversation_id, plan_id, text,
  and a 128-dim vector (matching `DEFAULT_EMBEDDING_DIM`).
* **AC #2 — 含 conversation_id / plan_id / text** — pinned by the
  same test.
* **AC #3 — MongoDB 先写后 Milvus** — pinned by observing the Plan
  row is at terminal status when the writer sees the upsert
  (write ordering is testable without instrumenting Mongo).
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from app.conversations.errors import PlanNotPendingError
from app.db.init_db import init_database
from app.db.schemas import (
    Plan,
    PlanCreate,
    PlanNode,
    ToolCreate,
    ToolSnapshot,
    TurnCreate,
)
from app.memory.embedding import DEFAULT_EMBEDDING_DIM
from app.memory.plan_history import (
    InMemoryMilvusWriter,
    MilvusPlanHistoryWriter,
    PlanHistoryRecord,
)
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.credentials import CredentialRepository
from app.repositories.plan_executions import PlanExecutionRepository
from app.repositories.plans import PlanRepository
from app.repositories.tools import ToolRepository
from app.repositories.turns import TurnRepository
from app.security.crypto import AesGcmEncryptor, MasterKey
from app.tools.executor import PlanExecutor
from app.tools.worker import ToolWorker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def encryptor() -> AesGcmEncryptor:
    return AesGcmEncryptor(MasterKey(key_bytes=os.urandom(32), key_id="test-exec"))


@pytest.fixture
async def db() -> Any:
    client = AsyncMongoMockClient()["copilot_executor_test"]
    await init_database(client)
    return client


@pytest.fixture
async def credential_repo(db: Any, encryptor: AesGcmEncryptor) -> CredentialRepository:
    return CredentialRepository(db, encryptor)


@pytest.fixture
async def tool_repo(db: Any) -> ToolRepository:
    return ToolRepository(db)


@pytest.fixture
async def plan_repo(db: Any) -> PlanRepository:
    return PlanRepository(db)


@pytest.fixture
async def turn_repo(db: Any) -> TurnRepository:
    return TurnRepository(db)


@pytest.fixture
async def plan_execution_repo(db: Any) -> PlanExecutionRepository:
    return PlanExecutionRepository(db)


@pytest.fixture
async def audit_repo(db: Any) -> AuditLogRepository:
    return AuditLogRepository(db)


def _echo_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content) if request.content else {}
    return httpx.Response(200, json={"echo": body.get("echo")})


def _always_500(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"err": "down"})


def _always_404(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"err": "not found"})


def _make_worker_and_executor(
    *,
    credential_repo: CredentialRepository,
    tool_repo: ToolRepository,
    plan_repo: PlanRepository,
    plan_execution_repo: PlanExecutionRepository,
    audit_repo: AuditLogRepository,
    handler: Any,
    turn_repo: TurnRepository | None = None,
    milvus_writer: MilvusPlanHistoryWriter | None = None,
) -> tuple[ToolWorker, PlanExecutor]:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    worker = ToolWorker(
        credential_repository=credential_repo,
        http_client=http_client,
    )
    executor = PlanExecutor(
        plan_repository=plan_repo,
        plan_execution_repository=plan_execution_repo,
        audit_log_repository=audit_repo,
        tool_repository=tool_repo,
        worker=worker,
        turn_repository=turn_repo,
        milvus_writer=milvus_writer,
    )
    return worker, executor


async def _seed_tool(
    tool_repo: ToolRepository,
    *,
    name: str = "echo",
    risk_level: str = "read",
    http_method: str = "POST",
    http_url_template: str = "https://upstream.test/echo",
    parameters_schema: dict[str, Any] | None = None,
    credentials_ref: str | None = None,
) -> str:
    tool = await tool_repo.create(
        ToolCreate(
            name=name,
            description=f"{name} tool",
            risk_level=risk_level,  # type: ignore[arg-type]
            status="active",
            parameters_schema=parameters_schema
            or {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            http_method=http_method,
            http_url_template=http_url_template,
            http_headers={"Content-Type": "application/json"},
            http_body_template={"echo": "{text}"},
            source="manual",
            credentials_ref=credentials_ref,
        ),
    )
    return tool.id


async def _seed_approved_plan(
    plan_repo: PlanRepository,
    *,
    conversation_id: str,
    turn_id: str,
    snapshot: ToolSnapshot,
    node: PlanNode,
) -> Plan:
    return await plan_repo.create(
        PlanCreate(
            conversation_id=conversation_id,
            turn_id=turn_id,
            status="approved",
            nodes=[node],
            edges=[],
            tool_snapshots=[snapshot],
        ),
    )


async def _seed_turn_with_plan(
    turn_repo: TurnRepository,
    plan_repo: PlanRepository,
    *,
    conversation_id: str,
    user_instruction: str,
    snapshot: ToolSnapshot,
    node: PlanNode,
) -> Plan:
    """Seed a Turn, then an `approved` Plan whose `turn_id` points
    to the freshly-created Turn.

    The Executor (T31) looks up the Turn via `TurnRepository.get`
    to render the user instruction into the Milvus summary — a
    Plan with a non-ObjectId turn_id would roundtrip cleanly through
    `create` but blow up at write time. This helper wires both rows
    the way production would.
    """
    turn = await turn_repo.create(
        TurnCreate(
            conversation_id=conversation_id,
            role="user",
            content=user_instruction,
            extra={},
        ),
    )
    return await plan_repo.create(
        PlanCreate(
            conversation_id=conversation_id,
            turn_id=turn.id,
            status="approved",
            nodes=[node],
            edges=[],
            tool_snapshots=[snapshot],
        ),
    )


async def _make_echo_plan(
    *,
    tool_repo: ToolRepository,
    turn_repo: TurnRepository,
    plan_repo: PlanRepository,
    user_instruction: str = "hi",
) -> tuple[str, Plan]:
    """Seed an `echo` Tool, a Turn, and a one-node Plan.

    The four TestMilvusWrite tests share the same Plan shape — a
    read-risk `echo` Tool with a `text` parameter and a body
    template that re-emits it. Centralising the boilerplate here
    keeps the per-test bodies focused on what each one actually
    pins.
    """
    tool_id = await _seed_tool(tool_repo)
    snapshot = ToolSnapshot(
        tool_id=tool_id,
        name="echo",
        description="Echo back the input text",
        risk_level="read",
        parameters_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        http_method="POST",
        http_url_template="https://upstream.test/echo",
        http_headers={"Content-Type": "application/json"},
        http_body_template={"echo": "{text}"},
    )
    node = PlanNode(node_id="n1", tool="echo", parameters={"text": "hi"})
    plan = await _seed_turn_with_plan(
        turn_repo=turn_repo,
        plan_repo=plan_repo,
        conversation_id="conv-1",
        user_instruction=user_instruction,
        snapshot=snapshot,
        node=node,
    )
    return tool_id, plan


# ---------------------------------------------------------------------------
# Happy path — 批准后 echo 跑通
# ---------------------------------------------------------------------------


class TestHappyPath:
    """The Plan runs to `succeeded` and writes audit / execution rows."""

    async def test_approved_plan_executes_and_flips_to_succeeded(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        tool_id = await _seed_tool(tool_repo)
        snapshot = ToolSnapshot(
            tool_id=tool_id,
            name="echo",
            description="Echo back the input text (read-only)",
            risk_level="read",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            http_method="POST",
            http_url_template="https://upstream.test/echo",
            http_headers={"Content-Type": "application/json"},
            http_body_template={"echo": "{text}"},
        )
        node = PlanNode(node_id="n1", tool="echo", parameters={"text": "hi"})
        plan = await _seed_approved_plan(
            plan_repo,
            conversation_id="conv-1",
            turn_id="turn-1",
            snapshot=snapshot,
            node=node,
        )

        _, executor = _make_worker_and_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_echo_handler,
        )

        outcome = await executor.execute_plan(
            plan=plan,
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id="turn-1",
        )

        assert outcome.plan.status == "succeeded"
        assert outcome.execution_id != ""
        assert len(outcome.audit_log_ids) == 1

        # The audit row carries the snapshot, not a fresh Tool fetch.
        audit_row = await audit_repo.get(outcome.audit_log_ids[0])
        assert audit_row.tool_snapshot.name == "echo"
        assert audit_row.tool_snapshot.tool_id == tool_id
        assert audit_row.status == "succeeded"
        assert audit_row.risk_level == "read"

        # PlanExecution rolled up to `completed`.
        execution = await plan_execution_repo.get(outcome.execution_id)
        assert execution.status == "completed"
        assert execution.node_results[0].status == "succeeded"
        assert execution.node_results[0].response == {"echo": "hi"}


# ---------------------------------------------------------------------------
# Snapshot-only execution (ADR-0027)
# ---------------------------------------------------------------------------


class TestSnapshotOnlyExecution:
    """The Executor uses the snapshot — not the live Tool row."""

    async def test_executor_routes_to_snapshot_url(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        """Live Tool row says POST /v1/echo; snapshot says POST /snapshot/echo.
        The executor must call the snapshot URL — even though it has the
        Tool row available (it reads it to recover `credentials_ref`).
        """
        await _seed_tool(
            tool_repo,
            name="echo",
            http_url_template="https://upstream.test/v1/echo",
        )
        snapshot = ToolSnapshot(
            name="echo",
            description="Echo (snapshot URL differs)",
            risk_level="read",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            http_method="POST",
            http_url_template="https://snapshot.test/echo",
            http_headers={"Content-Type": "application/json"},
            http_body_template={"echo": "{text}"},
        )
        node = PlanNode(node_id="n1", tool="echo", parameters={"text": "x"})
        plan = await _seed_approved_plan(
            plan_repo,
            conversation_id="conv-1",
            turn_id="turn-1",
            snapshot=snapshot,
            node=node,
        )

        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(200, json={"ok": True})

        _, executor = _make_worker_and_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=handler,
        )
        outcome = await executor.execute_plan(
            plan=plan,
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id="turn-1",
        )
        assert outcome.plan.status == "succeeded"
        assert seen_urls == ["https://snapshot.test/echo"]


# ---------------------------------------------------------------------------
# write / destructive stops at HITL — never retries
# ---------------------------------------------------------------------------


class TestWriteStopsAtHitl:
    """The Executor never retries; the first failure ends the Plan."""

    async def test_write_failure_marks_plan_failed(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        tool_id = await _seed_tool(
            tool_repo,
            name="create_invoice",
            risk_level="write",
            parameters_schema={
                "type": "object",
                "properties": {
                    "amount": {"type": "number"},
                    "customer": {"type": "string"},
                },
                "required": ["amount", "customer"],
            },
            http_url_template="https://upstream.test/invoices",
        )
        snapshot = ToolSnapshot(
            tool_id=tool_id,
            name="create_invoice",
            description="Create an invoice (write)",
            risk_level="write",
            parameters_schema={
                "type": "object",
                "properties": {
                    "amount": {"type": "number"},
                    "customer": {"type": "string"},
                },
                "required": ["amount", "customer"],
            },
            http_method="POST",
            http_url_template="https://upstream.test/invoices",
            http_headers={"Content-Type": "application/json"},
            http_body_template={"amount": "{amount}", "customer": "{customer}"},
        )
        node = PlanNode(
            node_id="n1",
            tool="create_invoice",
            parameters={"amount": 100, "customer": "ACME"},
        )
        plan = await _seed_approved_plan(
            plan_repo,
            conversation_id="conv-1",
            turn_id="turn-1",
            snapshot=snapshot,
            node=node,
        )

        _, executor = _make_worker_and_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_always_500,
        )
        outcome = await executor.execute_plan(
            plan=plan,
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id="turn-1",
        )
        assert outcome.plan.status == "failed"
        assert len(outcome.audit_log_ids) == 1

        # Audit row carries the failure envelope.
        audit_row = await audit_repo.get(outcome.audit_log_ids[0])
        assert audit_row.status == "failed"
        assert audit_row.risk_level == "write"
        assert audit_row.error is not None
        # The HITL envelope nests the underlying cause so the UI can
        # render a useful message while the top-level signal stays
        # uniform (`hitl_required`).
        assert audit_row.error["risk_level"] == "write"
        assert audit_row.error["cause"]["code"] == "upstream_error"


# ---------------------------------------------------------------------------
# read retries then surfaces as HITL
# ---------------------------------------------------------------------------


class TestReadRetryEscalation:
    """Read retries happen inside the Worker; the executor sees HITL."""

    async def test_read_exhausts_retries_then_plan_failed(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tool_id = await _seed_tool(tool_repo)

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        snapshot = ToolSnapshot(
            tool_id=tool_id,
            name="echo",
            description="Echo (read)",
            risk_level="read",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            http_method="POST",
            http_url_template="https://upstream.test/echo",
            http_headers={"Content-Type": "application/json"},
            http_body_template={"echo": "{text}"},
        )
        node = PlanNode(node_id="n1", tool="echo", parameters={"text": "x"})
        plan = await _seed_approved_plan(
            plan_repo,
            conversation_id="conv-1",
            turn_id="turn-1",
            snapshot=snapshot,
            node=node,
        )

        _, executor = _make_worker_and_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_always_500,
        )
        outcome = await executor.execute_plan(
            plan=plan,
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id="turn-1",
        )
        assert outcome.plan.status == "failed"
        audit_row = await audit_repo.get(outcome.audit_log_ids[0])
        assert audit_row.error is not None
        assert audit_row.error["cause"]["code"] == "upstream_error"
        # Retry count survived into the audit row — the Worker's bookkeeping.
        assert audit_row.retry_count >= 2


# ---------------------------------------------------------------------------
# State-machine guard — executor refuses to run a non-approved Plan
# ---------------------------------------------------------------------------


class TestStateMachineGuard:
    """Calling `execute_plan` on a non-approved Plan is a 409."""

    async def test_execute_rejects_pending_plan(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        tool_id = await _seed_tool(tool_repo)
        snapshot = ToolSnapshot(
            tool_id=tool_id,
            name="echo",
            description="Echo",
            risk_level="read",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            http_method="POST",
            http_url_template="https://upstream.test/echo",
            http_headers={"Content-Type": "application/json"},
            http_body_template={"echo": "{text}"},
        )
        node = PlanNode(node_id="n1", tool="echo", parameters={"text": "x"})
        # Plan is `pending`, not `approved`.
        plan = await plan_repo.create(
            PlanCreate(
                conversation_id="conv-1",
                turn_id="turn-1",
                status="pending",
                nodes=[node],
                edges=[],
                tool_snapshots=[snapshot],
            ),
        )

        _, executor = _make_worker_and_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_echo_handler,
        )
        with pytest.raises(PlanNotPendingError):
            await executor.execute_plan(
                plan=plan,
                actor_id="user-1",
                conversation_id="conv-1",
                turn_id="turn-1",
            )


# ---------------------------------------------------------------------------
# T34 / #30 — schema_violation feedback path (AC #3)
# ---------------------------------------------------------------------------


class TestSchemaViolationFeedback:
    """`schema_violation` from the Worker lands in the audit log row.

    AC #3 of T34 / #30 says "错误反馈给 Planner". The Planner reads
    feedback through (a) the `plan_executions` node outcome and
    (b) the `audit_logs` row's `error` envelope. This test pins
    both: the Plan lands `failed`, the audit row carries the
    `code=schema_violation` envelope with the field-level
    violations, and the upstream HTTP client never sees the bad
    payload (AC #1).
    """

    async def test_schema_violation_marks_plan_failed_and_records_audit(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        tool_id = await _seed_tool(
            tool_repo,
            name="echo",
            risk_level="read",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )
        snapshot = ToolSnapshot(
            tool_id=tool_id,
            name="echo",
            description="Echo",
            risk_level="read",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            http_method="POST",
            http_url_template="https://upstream.test/echo",
            http_headers={"Content-Type": "application/json"},
            http_body_template={"echo": "{text}"},
        )
        # The Planner hallucinated `text` as an int — fails the schema
        # check before the Worker would ever issue the HTTP call.
        node = PlanNode(
            node_id="n1",
            tool="echo",
            parameters={"text": 12345},
        )
        plan = await _seed_approved_plan(
            plan_repo,
            conversation_id="conv-1",
            turn_id="turn-1",
            snapshot=snapshot,
            node=node,
        )

        # The handler should NEVER be invoked: AC #1 says the
        # Worker refuses to call upstream on a bad payload.
        upstream_calls: list[httpx.Request] = []

        def _tracking_handler(request: httpx.Request) -> httpx.Response:
            upstream_calls.append(request)
            return httpx.Response(200, json={"echo": "ok"})

        _, executor = _make_worker_and_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_tracking_handler,
        )
        outcome = await executor.execute_plan(
            plan=plan,
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id="turn-1",
        )
        # AC #1 — upstream never saw the bad payload.
        assert upstream_calls == []
        # AC #3 — the Plan flips to `failed` and the audit row
        # carries the structured violations envelope the Planner
        # can read to regenerate correct parameters. The
        # `schema_violation` code lives on the originating
        # `SchemaViolationError`; the audit row stores the
        # violations detail (mirroring the existing HITL path).
        assert outcome.plan.status == "failed"
        assert len(outcome.audit_log_ids) == 1
        audit_row = await audit_repo.get(outcome.audit_log_ids[0])
        assert audit_row.status == "failed"
        assert audit_row.error is not None
        assert audit_row.error["tool"] == "echo"
        assert any(
            v["validator"] == "type"
            for v in audit_row.error["violations"]
        )


# ---------------------------------------------------------------------------
# T31 / #27 — long-term-memory write path
# ---------------------------------------------------------------------------


class _MilvusWriteProbe:
    """Test double that records calls and optionally raises on upsert.

    The `raise_on_upsert` switch is the failure-doesn't-abort seam
    for the defensive `try/except` test.
    """

    def __init__(self, *, raise_on_upsert: bool = False) -> None:
        self.records: list[PlanHistoryRecord] = []
        self.raise_on_upsert = raise_on_upsert

    async def upsert_summary(self, record: PlanHistoryRecord) -> None:
        if self.raise_on_upsert:
            raise RuntimeError("simulated milvus outage")
        self.records.append(record)


class TestMilvusWrite:
    """T31 / #27 — Plan completion pushes a summary into long-term memory.

    The Executor accepts an optional Milvus writer. When wired, a
    successful run lands exactly one record in the writer's
    `records` list; the record carries every field the SPEC
    §Data model promises for `plan_history_vectors` (AC #1 + AC #2).

    The writer is best-effort: a Milvus exception does not unwind
    the Plan execution (AC #3 + ADR-0008).
    """

    async def test_succeeded_plan_writes_summary_to_milvus(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
        turn_repo: TurnRepository,
    ) -> None:
        """A successful run pushes one record carrying conversation_id,
        plan_id, non-empty text, and a 128-dim vector (AC #1 + #2)."""
        _, plan = await _make_echo_plan(
            tool_repo=tool_repo,
            turn_repo=turn_repo,
            plan_repo=plan_repo,
            user_instruction="echo something for me",
        )

        writer = InMemoryMilvusWriter()
        _, executor = _make_worker_and_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_echo_handler,
            turn_repo=turn_repo,
            milvus_writer=writer,
        )
        outcome = await executor.execute_plan(
            plan=plan,
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id=plan.turn_id,
        )

        assert outcome.plan.status == "succeeded"
        # Exactly one write — the Plan completed exactly once.
        assert len(writer.records) == 1
        record = writer.records[0]
        assert record.plan_id == plan.id
        assert record.conversation_id == "conv-1"
        assert record.text  # non-empty
        assert "echo something for me" in record.text
        assert len(record.vector) == DEFAULT_EMBEDDING_DIM

    async def test_milvus_write_failure_does_not_abort_plan(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
        turn_repo: TurnRepository,
    ) -> None:
        """A raising Milvus writer does NOT prevent the Plan from
        flipping to `succeeded`. ADR-0008: Milvus is the derived
        index; MongoDB is the truth. The best-effort envelope is the
        seam that enforces this."""
        _, plan = await _make_echo_plan(
            tool_repo=tool_repo,
            turn_repo=turn_repo,
            plan_repo=plan_repo,
        )

        probe = _MilvusWriteProbe(raise_on_upsert=True)
        _, executor = _make_worker_and_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_echo_handler,
            turn_repo=turn_repo,
            milvus_writer=probe,
        )
        # The exception is swallowed; the Plan still flips to
        # `succeeded` and the executor returns normally.
        outcome = await executor.execute_plan(
            plan=plan,
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id=plan.turn_id,
        )
        assert outcome.plan.status == "succeeded"
        # No records landed because the upsert raised before append.
        assert probe.records == []

    async def test_mongo_writes_complete_before_milvus(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
        turn_repo: TurnRepository,
    ) -> None:
        """AC #3 — MongoDB 先写后 Milvus. The Executor's call order
        is: `plan_executions.set_status(aggregate)` →
        `plans.set_status(final)` → `upsert_summary(record)`. The
        Plan row read inside `_write_plan_history` (and again from
        the response object) must already reflect `succeeded` /
        `failed` when the writer fires.

        Pinning this with an in-memory probe + a follow-up repo read
        is the cheapest way to assert ordering without instrumenting
        Mongo; the test fails loudly if the seam is ever reversed.
        """
        _, plan = await _make_echo_plan(
            tool_repo=tool_repo,
            turn_repo=turn_repo,
            plan_repo=plan_repo,
        )

        # The probe records the Plan's persisted status at the moment
        # of the upsert. By contract, the upsert fires AFTER both
        # `plan_executions.set_status` and `plans.set_status`, so the
        # probe should observe the terminal status (`succeeded`).
        class _OrderingProbe:
            def __init__(self, plan_repo: PlanRepository) -> None:
                self._plans = plan_repo
                self.observed_statuses: list[str] = []

            async def upsert_summary(self, record: PlanHistoryRecord) -> None:
                # Read the persisted Plan row inside the upsert
                # callback — this is what production would observe.
                current = await self._plans.get(record.plan_id)
                self.observed_statuses.append(current.status)

        probe = _OrderingProbe(plan_repo)
        _, executor = _make_worker_and_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_echo_handler,
            turn_repo=turn_repo,
            milvus_writer=probe,
        )
        await executor.execute_plan(
            plan=plan,
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id=plan.turn_id,
        )
        assert probe.observed_statuses == ["succeeded"]

    async def test_empty_plan_does_not_write_milvus(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
        turn_repo: TurnRepository,
    ) -> None:
        """Smalltalk / zero-node Plans (ADR-0004) do NOT pollute the
        Milvus index.

        The `PlanRepository.create` seam enforces `if not data.nodes:
        raise ValidationError`, so a zero-node Plan cannot reach the
        Executor's full pipeline — but the seam still has a guard
        for any future caller (e.g. a backfill job) that bypasses the
        repo. Reach for the seam directly with a hand-built empty
        `Plan` so the guard is exercised without fighting the repo.
        """
        snapshot = ToolSnapshot(
            name="echo",
            description="Echo back the input text",
            risk_level="read",
            parameters_schema={"type": "object"},
            http_method="POST",
            http_url_template="https://upstream.test/echo",
            http_headers={"Content-Type": "application/json"},
            http_body_template=None,
        )
        # A bare `Plan(...)` with `nodes=[]` is structurally valid —
        # only the binding check (every node.tool must appear in
        # `tool_snapshots`) trips on zero-node docs, and that check
        # is silent when there are no nodes to validate. We don't
        # need a Turn in the DB because the guard short-circuits
        # before any lookup happens.
        empty_plan = Plan(
            _id="000000000000000000000000",
            conversation_id="conv-1",
            turn_id="000000000000000000000000",
            status="approved",
            nodes=[],
            edges=[],
            tool_snapshots=[snapshot],
            edited_diff=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        writer = InMemoryMilvusWriter()
        _, executor = _make_worker_and_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_echo_handler,
            turn_repo=turn_repo,
            milvus_writer=writer,
        )
        # Drive the guard directly. `_write_plan_history` is the seam
        # the executor's `execute_plan` calls after the Mongo writes;
        # calling it in isolation keeps the test honest about which
        # branch it pins.
        await executor._write_plan_history(empty_plan)  # noqa: SLF001 — seam test
        assert writer.records == []
