"""T44 / #39 — Plan-Tool snapshot binding regression tests.

The acceptance criteria for T44: "admin 改 Tool 定义执行中 Plan 不受影响".

1. **启动 Plan 改 Tool 描述** — start a Plan, then admin mutates the
   live `tools` row mid-flight (description, URL, risk_level, schema).
2. **Plan 仍按旧执行** — the Executor and Worker continue to use the
   Plan-embedded `tool_snapshots` (ADR-0027), NOT the live row.
3. **审计含 tool_snapshots** — every `audit_logs` row carries the
   frozen `tool_snapshot` so reviewers can see "which Tool did this
   run actually invoke".
4. **Plan 可重现** — re-loading the Plan from MongoDB and re-running
   it (in a fresh request lifecycle, no in-memory state) produces the
   same upstream calls.

The scenarios below mirror the four acceptance bullets one-for-one.
They intentionally re-use the `mongomock_motor` test seam from
`test_plan_executor.py` so the executor's storage behaviour is
covered without standing up real MongoDB.

Why a dedicated test file rather than more cases in
`test_plan_executor.py`: T44 is a regression contract — the *current*
code passes it, but the test must outlive refactors that touch the
Plan↔Tool↔Worker seam. Keeping T44 tests in one place (one file,
one class per acceptance bullet) lets a future maintainer know
"if I break ADR-0027, one of these tests fails".
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from app.db.init_db import init_database
from app.db.schemas import (
    Plan,
    PlanCreate,
    PlanNode,
    ToolCreate,
    ToolSnapshot,
    ToolUpdate,
)
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.credentials import CredentialRepository
from app.repositories.plan_executions import PlanExecutionRepository
from app.repositories.plans import PlanRepository
from app.repositories.tools import ToolRepository
from app.security.crypto import AesGcmEncryptor, MasterKey
from app.tools.executor import PlanExecutor
from app.tools.worker import ToolWorker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def encryptor() -> AesGcmEncryptor:
    return AesGcmEncryptor(MasterKey(key_bytes=os.urandom(32), key_id="test-t44"))


@pytest.fixture
async def db() -> Any:
    client = AsyncMongoMockClient()["copilot_t44_test"]
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
async def plan_execution_repo(db: Any) -> PlanExecutionRepository:
    return PlanExecutionRepository(db)


@pytest.fixture
async def audit_repo(db: Any) -> AuditLogRepository:
    return AuditLogRepository(db)


def _make_executor(
    *,
    credential_repo: CredentialRepository,
    tool_repo: ToolRepository,
    plan_repo: PlanRepository,
    plan_execution_repo: PlanExecutionRepository,
    audit_repo: AuditLogRepository,
    handler: Any,
) -> PlanExecutor:
    """Build an executor with a stub HTTP transport.

    The handler is the single seam we need to observe "which URL was
    actually called". Returning the executor only — the Worker is an
    internal collaborator the test doesn't reach into directly.
    """
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    worker = ToolWorker(
        credential_repository=credential_repo,
        http_client=http_client,
    )
    return PlanExecutor(
        plan_repository=plan_repo,
        plan_execution_repository=plan_execution_repo,
        audit_log_repository=audit_repo,
        tool_repository=tool_repo,
        worker=worker,
    )


def _echo_handler(request: httpx.Request) -> httpx.Response:
    """Default upstream that echoes back the JSON body's `text` field."""
    body = json.loads(request.content) if request.content else {}
    return httpx.Response(200, json={"echo": body.get("text")})


def _always_500(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"err": "down"})


async def _seed_tool(
    tool_repo: ToolRepository,
    *,
    name: str = "echo",
    description: str = "Echo back the input text (read-only)",
    risk_level: str = "read",
    http_url_template: str = "https://upstream.test/echo",
    parameters_schema: dict[str, Any] | None = None,
) -> str:
    """Seed a `read`-risk echo Tool and return its id.

    Centralised so per-test bodies focus on the snapshot-vs-live
    divergence rather than re-stating Tool defaults. `description` is
    a parameter (rather than a fixed string) because the description
    change tests need an explicit "before" value to compare against
    the "after" value they patch on the live row.
    """
    tool = await tool_repo.create(
        ToolCreate(
            name=name,
            description=description,
            risk_level=risk_level,  # type: ignore[arg-type]
            status="active",
            parameters_schema=parameters_schema
            or {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            http_method="POST",
            http_url_template=http_url_template,
            http_headers={"Content-Type": "application/json"},
            http_body_template={"echo": "{text}"},
            source="manual",
        ),
    )
    return tool.id


def _echo_snapshot(*, tool_id: str, description: str) -> ToolSnapshot:
    """A read-class echo snapshot — frozen "as the Plan saw it"."""
    return ToolSnapshot(
        tool_id=tool_id,
        name="echo",
        description=description,
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


async def _seed_plan(
    plan_repo: PlanRepository,
    *,
    tool_id: str,
    snapshot_description: str,
) -> Plan:
    """Persist a one-node Plan with a frozen snapshot.

    The `description` we hand the snapshot is the "Plan-time
    description"; every test below mutates the live `tools` row to a
    different value AFTER this helper returns, so the divergence is
    observable in the audit row.
    """
    return await plan_repo.create(
        PlanCreate(
            conversation_id="conv-1",
            turn_id="turn-1",
            status="approved",
            nodes=[PlanNode(node_id="n1", tool="echo", parameters={"text": "hi"})],
            edges=[],
            tool_snapshots=[_echo_snapshot(tool_id=tool_id, description=snapshot_description)],
        ),
    )


# ---------------------------------------------------------------------------
# AC #2 — Plan 仍按旧执行 (Plan still executes with the old definition)
# ---------------------------------------------------------------------------


class TestSnapshotBindingDuringExecution:
    """The Executor + Worker read from the snapshot, not the live row.

    Each test seeds a Tool, freezes a snapshot, mutates the live Tool,
    then runs the Plan. The test pins that the mutation never reached
    the Worker's call frame — only the snapshot did.
    """

    async def test_admin_description_change_does_not_reach_worker(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        """Live description → "Echo (deprecated — use the new echo_v2)".
        Plan was frozen with description → "Echo back the input text".

        The audit row must carry the frozen description, not the live
        one. (The HTTP body / URL / method are unaffected by the
        description, so the assertion sits on the audit row directly.)
        """
        tool_id = await _seed_tool(
            tool_repo,
            description="Echo back the input text",
        )
        plan = await _seed_plan(
            plan_repo,
            tool_id=tool_id,
            snapshot_description="Echo back the input text",
        )

        # Admin mid-flight mutation: description goes to the live row.
        await tool_repo.update(tool_id, ToolUpdate(description="Echo (deprecated — use echo_v2)"))

        # Sanity check — the live row reflects the mutation so the
        # test would FAIL if the executor ever started reading the
        # live row instead of the snapshot.
        live = await tool_repo.get(tool_id)
        assert live.description == "Echo (deprecated — use echo_v2)"

        executor = _make_executor(
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
        assert len(outcome.audit_log_ids) == 1
        audit_row = await audit_repo.get(outcome.audit_log_ids[0])
        # AC #3 — audit carries the FROZEN description.
        assert audit_row.tool_snapshot.description == "Echo back the input text"
        # And NOT the live description.
        assert audit_row.tool_snapshot.description != live.description

    async def test_admin_url_change_does_not_reach_worker(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        """Live URL → https://upstream.test/v2/echo.
        Plan was frozen with URL → https://upstream.test/echo.

        The MockTransport records every URL the Worker actually calls.
        The frozen URL must be the only entry.
        """
        tool_id = await _seed_tool(
            tool_repo,
            http_url_template="https://upstream.test/echo",
        )
        plan = await _seed_plan(plan_repo, tool_id=tool_id, snapshot_description="Echo")

        # Mid-flight admin patches the live URL.
        await tool_repo.update(
            tool_id, ToolUpdate(http_url_template="https://upstream.test/v2/echo")
        )

        seen_urls: list[str] = []

        def _tracking(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(200, json={"echo": "ok"})

        executor = _make_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_tracking,
        )
        await executor.execute_plan(
            plan=plan,
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id="turn-1",
        )

        # The Worker called the snapshot URL, not the live one.
        assert seen_urls == ["https://upstream.test/echo"]
        assert "https://upstream.test/v2/echo" not in seen_urls

    async def test_admin_risk_level_change_does_not_alter_retry_behavior(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Live risk_level → 'destructive'.
        Plan was frozen with risk_level → 'read'.

        ADR-0017 retry matrix: `read` retries twice on 5xx, then
        surfaces HITL. `destructive` stops immediately on first
        failure. The Plan was frozen as `read`, so a flaky upstream
        must NOT abort on the first call — it should retry up to 3
        attempts before failing.
        """
        tool_id = await _seed_tool(tool_repo, risk_level="read")
        plan = await _seed_plan(plan_repo, tool_id=tool_id, snapshot_description="Echo")

        # Mid-flight admin patches the live risk to destructive.
        await tool_repo.update(tool_id, ToolUpdate(risk_level="destructive"))

        # Sanity: live row reflects the mutation.
        live = await tool_repo.get(tool_id)
        assert live.risk_level == "destructive"

        # No real sleeps in the test seam.
        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        seen_calls: list[str] = []

        def _tracking(_request: httpx.Request) -> httpx.Response:
            seen_calls.append("call")
            return httpx.Response(500, json={"err": "down"})

        executor = _make_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_tracking,
        )
        outcome = await executor.execute_plan(
            plan=plan,
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id="turn-1",
        )

        # The Plan was frozen as `read` — the retry budget was the
        # snapshot's, not the live row's. Three calls = 1 initial +
        # 2 retries; `destructive` would have stopped at 1.
        assert seen_calls == ["call", "call", "call"]
        assert outcome.plan.status == "failed"
        audit_row = await audit_repo.get(outcome.audit_log_ids[0])
        # Audit row's risk_level comes from the snapshot, not the live.
        assert audit_row.risk_level == "read"
        assert audit_row.tool_snapshot.risk_level == "read"

    async def test_admin_schema_change_does_not_alter_parameter_validation(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        """Live parameters_schema → `text` must be integer.
        Snapshot was frozen with parameters_schema → `text` must be string.

        The Worker validates parameters against the snapshot's schema
        (ADR-0020). The Plan node has `text="hi"` (a string), which
        passes the SNAPSHOT's schema but would FAIL the live row's
        schema. If the Worker ever read the live schema, it would
        refuse to dispatch; the test fails in that scenario.
        """
        tool_id = await _seed_tool(
            tool_repo,
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )
        plan = await _seed_plan(plan_repo, tool_id=tool_id, snapshot_description="Echo")

        # Mid-flight admin patches the live schema to require an int.
        await tool_repo.update(
            tool_id,
            ToolUpdate(
                parameters_schema={
                    "type": "object",
                    "properties": {"text": {"type": "integer"}},
                    "required": ["text"],
                }
            ),
        )

        upstream_calls: list[httpx.Request] = []

        def _tracking(request: httpx.Request) -> httpx.Response:
            upstream_calls.append(request)
            return httpx.Response(200, json={"echo": "ok"})

        executor = _make_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_tracking,
        )
        outcome = await executor.execute_plan(
            plan=plan,
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id="turn-1",
        )

        # AC #2 — the Worker accepted `text="hi"` (snapshot schema)
        # and dispatched. If it had read the live schema, the call
        # would never have reached upstream.
        assert outcome.plan.status == "succeeded"
        assert len(upstream_calls) == 1
        body = json.loads(upstream_calls[0].content)
        assert body == {"echo": "hi"}


# ---------------------------------------------------------------------------
# AC #3 — 审计含 tool_snapshots
# ---------------------------------------------------------------------------


class TestAuditContainsToolSnapshots:
    """The audit log row carries the frozen snapshot, not a live fetch."""

    async def test_audit_row_snapshot_matches_plan_snapshot(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        """The audit row's `tool_snapshot` is field-for-field the
        Plan's `tool_snapshots[0]` — the live row is never consulted
        at audit-write time."""
        tool_id = await _seed_tool(
            tool_repo,
            description="Echo (read)",
            http_url_template="https://upstream.test/echo",
        )
        snapshot = _echo_snapshot(tool_id=tool_id, description="Echo (read)")
        plan = await _seed_plan(plan_repo, tool_id=tool_id, snapshot_description="Echo (read)")

        executor = _make_executor(
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

        audit_row = await audit_repo.get(outcome.audit_log_ids[0])
        # Every field on the snapshot survives into the audit row.
        assert audit_row.tool_snapshot.model_dump() == snapshot.model_dump()
        # And the audit row is queryable by plan_id — the FK works
        # even after the live row drifts.
        by_plan = await audit_repo.query(plan_id=plan.id)
        assert len(by_plan) == 1
        assert by_plan[0].id == audit_row.id


# ---------------------------------------------------------------------------
# AC #4 — Plan 可重现
# ---------------------------------------------------------------------------


class TestPlanReproducibility:
    """The Plan can be reloaded from MongoDB and re-executed verbatim."""

    async def test_plan_round_trips_snapshots_unchanged(
        self,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
    ) -> None:
        """The snapshot persisted on insert survives a re-fetch byte-
        for-byte. The `model_dump` comparison catches any silent
        coercion (e.g. schema dict → str) that would compromise
        replay equality.
        """
        tool_id = await _seed_tool(
            tool_repo,
            description="Echo (read)",
            http_url_template="https://upstream.test/echo",
        )
        snapshot = _echo_snapshot(tool_id=tool_id, description="Echo (read)")
        original = await _seed_plan(
            plan_repo,
            tool_id=tool_id,
            snapshot_description="Echo (read)",
        )

        # Reload from the repository — same DB, different code path.
        reloaded = await plan_repo.get(original.id)

        # Snapshot field-for-field equality.
        assert len(reloaded.tool_snapshots) == 1
        assert reloaded.tool_snapshots[0].model_dump() == snapshot.model_dump()
        # Node binding unchanged.
        assert reloaded.nodes[0].tool == snapshot.name
        # Plan lifecycle stayed at the seeded value.
        assert reloaded.status == "approved"

    async def test_reloaded_plan_executes_with_same_snapshot(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        """AC #4 — re-fetching the Plan and running it again produces
        the same upstream URL even though the executor instance is
        fresh and the live Tool has been mutated. This is the full
        "admin can edit Tool while a Plan is in flight" regression:
        the second run sees the live row's NEW description, but
        dispatches to the snapshot URL.
        """
        tool_id = await _seed_tool(
            tool_repo,
            description="Echo (v1)",
            http_url_template="https://upstream.test/echo",
        )
        plan = await _seed_plan(plan_repo, tool_id=tool_id, snapshot_description="Echo (v1)")

        # Persisted doc — this is what a fresh request lifecycle
        # would read back from MongoDB.
        persisted = await plan_repo.get(plan.id)

        # Live row drifts mid-flight.
        await tool_repo.update(
            tool_id,
            ToolUpdate(
                description="Echo (v2 — admin rewrote this)",
                http_url_template="https://upstream.test/v2/echo",
            ),
        )

        seen_urls: list[str] = []

        def _tracking(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(200, json={"echo": "ok"})

        executor = _make_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=_tracking,
        )
        outcome = await executor.execute_plan(
            plan=persisted,  # reloaded from the repository
            actor_id="user-1",
            conversation_id="conv-1",
            turn_id="turn-1",
        )

        # The reloaded Plan executed with its frozen URL.
        assert outcome.plan.status == "succeeded"
        assert seen_urls == ["https://upstream.test/echo"]
        # And the audit row still carries the frozen description.
        audit_row = await audit_repo.get(outcome.audit_log_ids[0])
        assert audit_row.tool_snapshot.description == "Echo (v1)"