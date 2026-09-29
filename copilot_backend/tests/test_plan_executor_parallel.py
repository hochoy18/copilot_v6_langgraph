"""Parallel-branch execution tests for `PlanExecutor` — T28 / #24 / ADR-0012.

These tests cover the three acceptance criteria from issue #24:

1. **Two root nodes run concurrently** — a Plan with `echo_a` and
   `echo_b` as roots has both invocations in flight at the same
   time. We assert this by recording the start / finish timestamps
   on a stub transport: the *earliest finish* must come AFTER the
   *latest start* would not be the right metric for small deltas,
   so we instead record the maximum concurrency observed during
   the run — both nodes must overlap.
2. **All complete before the next level** — a Plan with `n1 -> n2`
   ensures `n2` starts only after `n1` finishes.
3. **A failure in one branch doesn't kill the others** — `echo_a`
   returns 200 and `echo_b` returns 500. Both ran. `echo_a` is
   `succeeded`, `echo_b` is `failed`. The aggregate Plan rolls up
   to `failed`.

The executor seam is the same as T21 — `PlanExecutor.execute_plan`.
The implementation lands in `app.tools.dag` (LangGraph StateGraph)
plus a refactor of `app.tools.executor` to delegate the inner loop
to the DAG runner. Tests below build multi-node Plans on top of
the same hermetic fixtures `test_plan_executor.py` already uses.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Any

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from app.db.init_db import init_database
from app.db.schemas import (
    PlanCreate,
    PlanEdge,
    PlanNode,
    ToolCreate,
    ToolSnapshot,
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
# Fixtures — same shape as test_plan_executor.py
# ---------------------------------------------------------------------------


@pytest.fixture
def encryptor() -> AesGcmEncryptor:
    return AesGcmEncryptor(MasterKey(key_bytes=os.urandom(32), key_id="test-par"))


@pytest.fixture
async def db() -> Any:
    client = AsyncMongoMockClient()["copilot_parallel_test"]
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


def _echo_snapshot(name: str, *, url: str = "https://upstream.test/echo") -> ToolSnapshot:
    return ToolSnapshot(
        name=name,
        description=f"{name} tool",
        risk_level="read",
        parameters_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        http_method="POST",
        http_url_template=url,
        http_headers={"Content-Type": "application/json"},
        http_body_template={"echo": "{text}"},
    )


async def _seed_tool(
    tool_repo: ToolRepository,
    *,
    name: str,
    http_url_template: str = "https://upstream.test/echo",
) -> str:
    tool = await tool_repo.create(
        ToolCreate(
            name=name,
            description=f"{name} tool",
            risk_level="read",
            status="active",
            parameters_schema={
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


def _make_executor(
    *,
    credential_repo: CredentialRepository,
    tool_repo: ToolRepository,
    plan_repo: PlanRepository,
    plan_execution_repo: PlanExecutionRepository,
    audit_repo: AuditLogRepository,
    handler: Any,
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
    )
    return worker, executor


# ---------------------------------------------------------------------------
# AC1 — two root nodes run concurrently
# ---------------------------------------------------------------------------


class TestParallelRoots:
    """Two root nodes fan out from START and run concurrently."""

    async def test_two_root_nodes_both_succeed(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        await _seed_tool(tool_repo, name="echo_a")
        await _seed_tool(tool_repo, name="echo_b")
        snapshots = [
            _echo_snapshot("echo_a", url="https://upstream.test/a"),
            _echo_snapshot("echo_b", url="https://upstream.test/b"),
        ]
        plan = await plan_repo.create(
            PlanCreate(
                conversation_id="conv-1",
                turn_id="turn-1",
                status="approved",
                nodes=[
                    PlanNode(node_id="n1", tool="echo_a", parameters={"text": "a"}),
                    PlanNode(node_id="n2", tool="echo_b", parameters={"text": "b"}),
                ],
                edges=[],
                tool_snapshots=snapshots,
            )
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"echo": json.loads(request.content)["echo"]})

        _, executor = _make_executor(
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
        execution = await plan_execution_repo.get(outcome.execution_id)
        by_node = {r.node_id: r for r in execution.node_results}
        assert by_node["n1"].status == "succeeded"
        assert by_node["n2"].status == "succeeded"

    async def test_two_root_nodes_run_concurrently(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        """Both invocations must be in flight at once — observed via a
        transport stub that blocks each request until released by an
        event. If the executor ran them serially, the second request
        would never arrive until the first event is set."""
        await _seed_tool(tool_repo, name="echo_a")
        await _seed_tool(tool_repo, name="echo_b")

        started: dict[str, datetime] = {}
        # `started_event` fires once both roots have entered the
        # transport; the test awaits it directly rather than polling
        # `len(started) < 2` with `asyncio.sleep` (which ruff flags
        # under ASYNC110 — `anyio.Event` integration needs extra
        # setup for httpx handlers that isn't worth it here).
        started_event = asyncio.Event()
        block = asyncio.Event()
        inflight = 0
        max_inflight = 0
        lock = asyncio.Lock()

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal inflight, max_inflight
            body = json.loads(request.content)
            started[body["echo"]] = datetime.utcnow()
            async with lock:
                inflight += 1
                max_inflight = max(max_inflight, inflight)
                if len(started) >= 2:
                    started_event.set()
            # First request blocks until the test releases it; the
            # second request must still arrive while we wait.
            await block.wait()
            async with lock:
                inflight -= 1
            return httpx.Response(200, json={"echo": body["echo"]})

        snapshots = [
            _echo_snapshot("echo_a", url="https://upstream.test/a"),
            _echo_snapshot("echo_b", url="https://upstream.test/b"),
        ]
        plan = await plan_repo.create(
            PlanCreate(
                conversation_id="conv-1",
                turn_id="turn-1",
                status="approved",
                nodes=[
                    PlanNode(node_id="n1", tool="echo_a", parameters={"text": "a"}),
                    PlanNode(node_id="n2", tool="echo_b", parameters={"text": "b"}),
                ],
                edges=[],
                tool_snapshots=snapshots,
            )
        )

        _, executor = _make_executor(
            credential_repo=credential_repo,
            tool_repo=tool_repo,
            plan_repo=plan_repo,
            plan_execution_repo=plan_execution_repo,
            audit_repo=audit_repo,
            handler=handler,
        )

        run_task = asyncio.create_task(
            executor.execute_plan(
                plan=plan,
                actor_id="user-1",
                conversation_id="conv-1",
                turn_id="turn-1",
            )
        )

        # Wait until BOTH nodes have entered the transport. If the
        # executor is sequential, only one will ever arrive and the
        # event stays unset — the bounded `wait_for` keeps the test
        # from hanging if the bug is reintroduced.
        try:
            await asyncio.wait_for(started_event.wait(), timeout=5.0)
        except TimeoutError:
            pass
        assert len(started) == 2, f"only one node arrived at the transport: {list(started)}"
        assert max_inflight == 2, f"expected concurrency 2, observed {max_inflight}"

        block.set()
        outcome = await run_task
        assert outcome.plan.status == "succeeded"


# ---------------------------------------------------------------------------
# AC2 — all complete before the next level
# ---------------------------------------------------------------------------


class TestTopologicalGate:
    """A downstream node starts only after every predecessor finishes."""

    async def test_linear_chain_n2_after_n1(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        await _seed_tool(tool_repo, name="echo_a", http_url_template="https://upstream.test/a")
        await _seed_tool(tool_repo, name="echo_b", http_url_template="https://upstream.test/b")

        finishes: list[tuple[str, datetime]] = []
        starts: list[tuple[str, datetime]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            label = body["echo"]
            starts.append((label, datetime.utcnow()))
            finishes.append((label, datetime.utcnow()))
            return httpx.Response(200, json={"echo": label})

        plan = await plan_repo.create(
            PlanCreate(
                conversation_id="conv-1",
                turn_id="turn-1",
                status="approved",
                nodes=[
                    PlanNode(node_id="n1", tool="echo_a", parameters={"text": "a"}),
                    PlanNode(node_id="n2", tool="echo_b", parameters={"text": "b"}),
                ],
                edges=[PlanEdge(source="n1", target="n2")],
                tool_snapshots=[
                    _echo_snapshot("echo_a", url="https://upstream.test/a"),
                    _echo_snapshot("echo_b", url="https://upstream.test/b"),
                ],
            )
        )

        _, executor = _make_executor(
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

        # The mock transport records finish immediately after start;
        # so `finishes[i]` reflects when `n_i` finished (handing the
        # response back). `starts[i]` reflects when `n_i` entered the
        # handler. The downstream `n2`'s start must come after the
        # upstream `n1`'s finish.
        n1_finish = next(t for label, t in finishes if label == "a")
        n2_start = next(t for label, t in starts if label == "b")
        assert n2_start >= n1_finish, (
            f"downstream n2 started ({n2_start}) before upstream n1 "
            f"finished ({n1_finish}); topological gate violated"
        )

    async def test_diamond_c_runs_after_both_roots(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        """A -> C, B -> C. C runs after both A and B finish."""
        await _seed_tool(tool_repo, name="echo_a")
        await _seed_tool(tool_repo, name="echo_b")
        await _seed_tool(tool_repo, name="echo_c")

        finishes: list[tuple[str, datetime]] = []
        starts: list[tuple[str, datetime]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            label = body["echo"]
            starts.append((label, datetime.utcnow()))
            finishes.append((label, datetime.utcnow()))
            return httpx.Response(200, json={"echo": label})

        plan = await plan_repo.create(
            PlanCreate(
                conversation_id="conv-1",
                turn_id="turn-1",
                status="approved",
                nodes=[
                    PlanNode(node_id="n1", tool="echo_a", parameters={"text": "a"}),
                    PlanNode(node_id="n2", tool="echo_b", parameters={"text": "b"}),
                    PlanNode(node_id="n3", tool="echo_c", parameters={"text": "c"}),
                ],
                edges=[
                    PlanEdge(source="n1", target="n3"),
                    PlanEdge(source="n2", target="n3"),
                ],
                tool_snapshots=[
                    _echo_snapshot("echo_a"),
                    _echo_snapshot("echo_b"),
                    _echo_snapshot("echo_c"),
                ],
            )
        )

        _, executor = _make_executor(
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

        a_finish = next(t for label, t in finishes if label == "a")
        b_finish = next(t for label, t in finishes if label == "b")
        c_start = next(t for label, t in starts if label == "c")
        assert c_start >= a_finish
        assert c_start >= b_finish


# ---------------------------------------------------------------------------
# AC3 — failure in one branch does not affect the others
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    """A failed parallel sibling does not stop sibling branches."""

    async def test_one_failure_does_not_block_sibling(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_tool(tool_repo, name="echo_a")
        await _seed_tool(tool_repo, name="echo_b")

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["echo"] == "a":
                return httpx.Response(500, json={"err": "down"})
            return httpx.Response(200, json={"echo": body["echo"]})

        plan = await plan_repo.create(
            PlanCreate(
                conversation_id="conv-1",
                turn_id="turn-1",
                status="approved",
                nodes=[
                    PlanNode(node_id="n1", tool="echo_a", parameters={"text": "a"}),
                    PlanNode(node_id="n2", tool="echo_b", parameters={"text": "b"}),
                ],
                edges=[],
                tool_snapshots=[
                    _echo_snapshot("echo_a"),
                    _echo_snapshot("echo_b"),
                ],
            )
        )

        _, executor = _make_executor(
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

        # Plan-level rolled up to failed because at least one node failed.
        assert outcome.plan.status == "failed"

        execution = await plan_execution_repo.get(outcome.execution_id)
        by_node = {r.node_id: r for r in execution.node_results}
        # Both ran (failure isolation) — n1 is failed, n2 is succeeded.
        assert by_node["n1"].status == "failed"
        assert by_node["n2"].status == "succeeded"
        assert by_node["n1"].error is not None

    async def test_downstream_node_skipped_when_upstream_fails(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """n1 -> n2 with n1 failing: n2 is marked `skipped`, never ran."""
        await _seed_tool(tool_repo, name="echo_a")
        await _seed_tool(tool_repo, name="echo_b")

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        n2_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal n2_called
            body = json.loads(request.content)
            if body["echo"] == "a":
                return httpx.Response(500, json={"err": "down"})
            n2_called = True
            return httpx.Response(200, json={"echo": body["echo"]})

        plan = await plan_repo.create(
            PlanCreate(
                conversation_id="conv-1",
                turn_id="turn-1",
                status="approved",
                nodes=[
                    PlanNode(node_id="n1", tool="echo_a", parameters={"text": "a"}),
                    PlanNode(node_id="n2", tool="echo_b", parameters={"text": "b"}),
                ],
                edges=[PlanEdge(source="n1", target="n2")],
                tool_snapshots=[
                    _echo_snapshot("echo_a"),
                    _echo_snapshot("echo_b"),
                ],
            )
        )

        _, executor = _make_executor(
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
        assert outcome.plan.status == "failed"
        assert n2_called is False, "downstream n2 must not run when n1 failed"

        execution = await plan_execution_repo.get(outcome.execution_id)
        by_node = {r.node_id: r for r in execution.node_results}
        assert by_node["n2"].status == "skipped"

    async def test_diamond_failure_skips_fan_in(
        self,
        credential_repo: CredentialRepository,
        tool_repo: ToolRepository,
        plan_repo: PlanRepository,
        plan_execution_repo: PlanExecutionRepository,
        audit_repo: AuditLogRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A -> C, B -> C. A fails, B succeeds. C is skipped (ADR-0012)."""
        await _seed_tool(tool_repo, name="echo_a")
        await _seed_tool(tool_repo, name="echo_b")
        await _seed_tool(tool_repo, name="echo_c")

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["echo"] == "a":
                return httpx.Response(500, json={"err": "down"})
            return httpx.Response(200, json={"echo": body["echo"]})

        plan = await plan_repo.create(
            PlanCreate(
                conversation_id="conv-1",
                turn_id="turn-1",
                status="approved",
                nodes=[
                    PlanNode(node_id="n1", tool="echo_a", parameters={"text": "a"}),
                    PlanNode(node_id="n2", tool="echo_b", parameters={"text": "b"}),
                    PlanNode(node_id="n3", tool="echo_c", parameters={"text": "c"}),
                ],
                edges=[
                    PlanEdge(source="n1", target="n3"),
                    PlanEdge(source="n2", target="n3"),
                ],
                tool_snapshots=[
                    _echo_snapshot("echo_a"),
                    _echo_snapshot("echo_b"),
                    _echo_snapshot("echo_c"),
                ],
            )
        )

        _, executor = _make_executor(
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
        assert outcome.plan.status == "failed"
        execution = await plan_execution_repo.get(outcome.execution_id)
        by_node = {r.node_id: r for r in execution.node_results}
        assert by_node["n1"].status == "failed"
        assert by_node["n2"].status == "succeeded"
        assert by_node["n3"].status == "skipped"
