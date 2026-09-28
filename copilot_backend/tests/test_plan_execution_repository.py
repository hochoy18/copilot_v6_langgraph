"""Tests for `PlanExecutionRepository` (T06 / #7).

The execution-row lifecycle couples with the Plan's status. Per-node
appends go through `upsert_node_result` / `mark_node_status`; the
aggregate transition lands via `set_status`. This suite verifies each
branch against an in-memory `mongomock_motor`.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.db.errors import InvalidIdError, NotFoundError
from app.db.init_db import init_database
from app.db.schemas import PlanExecutionCreate, PlanNodeResult, PlanNodeStatus
from app.repositories.plan_executions import PlanExecutionRepository


@pytest.fixture
async def repo() -> PlanExecutionRepository:
    """A fresh `PlanExecutionRepository` against an isolated in-memory DB."""
    db = AsyncMongoMockClient()["copilot_exec_test"]
    await init_database(db)
    return PlanExecutionRepository(db)


def _exec_input(**overrides: object) -> PlanExecutionCreate:
    base: dict[str, object] = {
        "plan_id": str(ObjectId()),
        "conversation_id": str(ObjectId()),
        "status": "running",
        "node_results": [],
    }
    base.update(overrides)
    return PlanExecutionCreate(**base)  # type: ignore[arg-type]


def _result(**overrides: object) -> PlanNodeResult:
    base: dict[str, object] = {
        "node_id": "n1",
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "request": None,
        "response": None,
        "error": None,
        "retry_count": 0,
    }
    base.update(overrides)
    return PlanNodeResult(**base)  # type: ignore[arg-type]


class TestPlanExecutionCreate:
    """`create` — stamps `started_at = now`, leaves `finished_at` null."""

    @pytest.mark.asyncio
    async def test_create_returns_canonical_shape(
        self, repo: PlanExecutionRepository
    ) -> None:
        before = datetime.utcnow()
        created = await repo.create(_exec_input())
        assert created.id
        assert ObjectId(created.id)
        assert created.status == "running"
        assert created.node_results == []
        assert created.finished_at is None
        assert created.started_at >= before - timedelta(seconds=1)

    @pytest.mark.asyncio
    async def test_create_isolates_per_plan(
        self, repo: PlanExecutionRepository
    ) -> None:
        plan_id = str(ObjectId())
        e1 = await repo.create(_exec_input(plan_id=plan_id))
        e2 = await repo.create(_exec_input(plan_id=plan_id))
        assert e1.id != e2.id
        ours = await repo.list_by_plan(plan_id)
        assert {e.id for e in ours} == {e1.id, e2.id}


class TestPlanExecutionRead:
    """`get`, `list_by_plan`, `get_latest_for_plan`, `list_running`."""

    @pytest.mark.asyncio
    async def test_get_by_id(self, repo: PlanExecutionRepository) -> None:
        created = await repo.create(_exec_input())
        fetched = await repo.get(created.id)
        assert fetched.id == created.id

    @pytest.mark.asyncio
    async def test_get_missing_raises_not_found(
        self, repo: PlanExecutionRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.get(str(ObjectId()))

    @pytest.mark.asyncio
    async def test_get_invalid_id_raises_invalid_id(
        self, repo: PlanExecutionRepository
    ) -> None:
        with pytest.raises(InvalidIdError):
            await repo.get("not-an-objectid")

    @pytest.mark.asyncio
    async def test_list_by_plan_returns_newest_first(
        self, repo: PlanExecutionRepository
    ) -> None:
        plan_id = str(ObjectId())
        e1 = await repo.create(_exec_input(plan_id=plan_id))
        await repo.set_status(e1.id, "failed")
        # Wait a millisecond so the second row has a strictly greater `started_at`.
        await asyncio.sleep(0.01)
        e2 = await repo.create(_exec_input(plan_id=plan_id))
        ours = await repo.list_by_plan(plan_id)
        assert [e.id for e in ours] == [e2.id, e1.id]

    @pytest.mark.asyncio
    async def test_get_latest_for_plan_returns_most_recent(
        self, repo: PlanExecutionRepository
    ) -> None:
        plan_id = str(ObjectId())
        await repo.create(_exec_input(plan_id=plan_id))
        await asyncio.sleep(0.01)
        e2 = await repo.create(_exec_input(plan_id=plan_id))
        latest = await repo.get_latest_for_plan(plan_id)
        assert latest.id == e2.id

    @pytest.mark.asyncio
    async def test_get_latest_for_plan_missing_raises(
        self, repo: PlanExecutionRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.get_latest_for_plan(str(ObjectId()))

    @pytest.mark.asyncio
    async def test_list_running_filters_by_status(
        self, repo: PlanExecutionRepository
    ) -> None:
        e1 = await repo.create(_exec_input(status="running"))
        e2 = await repo.create(_exec_input(status="running"))
        await repo.set_status(e2.id, "completed")
        running = await repo.list_running()
        assert {e.id for e in running} == {e1.id}


class TestPlanExecutionUpdate:
    """`update`, `set_status`, `upsert_node_result`, `mark_node_status`."""

    @pytest.mark.asyncio
    async def test_set_status_stamps_finished_at_on_terminal(
        self, repo: PlanExecutionRepository
    ) -> None:
        """Aggregate terminal transitions stamp `finished_at` automatically."""
        created = await repo.create(_exec_input())
        assert created.finished_at is None
        done = await repo.set_status(created.id, "completed")
        assert done.status == "completed"
        assert done.finished_at is not None
        assert done.finished_at >= created.started_at

    @pytest.mark.asyncio
    async def test_set_status_does_not_stamp_finished_for_running(
        self, repo: PlanExecutionRepository
    ) -> None:
        """Running is not a terminal state — `finished_at` stays null."""
        created = await repo.create(_exec_input())
        still_running = await repo.set_status(created.id, "running")
        assert still_running.finished_at is None

    @pytest.mark.asyncio
    async def test_set_status_aborted_records_finished_at(
        self, repo: PlanExecutionRepository
    ) -> None:
        """`aborted` is a business-user-initiated terminal — same convention."""
        created = await repo.create(_exec_input())
        aborted = await repo.set_status(created.id, "aborted")
        assert aborted.status == "aborted"
        assert aborted.finished_at is not None

    @pytest.mark.asyncio
    async def test_set_status_missing_raises_not_found(
        self, repo: PlanExecutionRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.set_status(str(ObjectId()), "completed")

    @pytest.mark.asyncio
    async def test_upsert_node_result_appends_new_node(
        self, repo: PlanExecutionRepository
    ) -> None:
        created = await repo.create(_exec_input())
        result = _result(node_id="n1", status="running")
        updated = await repo.upsert_node_result(created.id, result)
        assert len(updated.node_results) == 1
        assert updated.node_results[0].node_id == "n1"
        assert updated.node_results[0].status == "running"

    @pytest.mark.asyncio
    async def test_upsert_node_result_replaces_existing_by_node_id(
        self, repo: PlanExecutionRepository
    ) -> None:
        """A duplicate SSE event lands on the same row, not a fork."""
        created = await repo.create(_exec_input())
        await repo.upsert_node_result(created.id, _result(node_id="n1", status="running"))
        await repo.upsert_node_result(created.id, _result(node_id="n1", status="succeeded"))
        final = await repo.get(created.id)
        assert len(final.node_results) == 1
        assert final.node_results[0].status == "succeeded"

    @pytest.mark.asyncio
    async def test_upsert_node_result_keeps_other_nodes_intact(
        self, repo: PlanExecutionRepository
    ) -> None:
        created = await repo.create(_exec_input())
        await repo.upsert_node_result(created.id, _result(node_id="n1", status="succeeded"))
        await repo.upsert_node_result(created.id, _result(node_id="n2", status="running"))
        final = await repo.get(created.id)
        node_by_id = {n.node_id: n for n in final.node_results}
        assert set(node_by_id) == {"n1", "n2"}
        assert node_by_id["n1"].status == "succeeded"
        assert node_by_id["n2"].status == "running"

    @pytest.mark.asyncio
    async def test_mark_node_status_creates_when_missing(
        self, repo: PlanExecutionRepository
    ) -> None:
        created = await repo.create(_exec_input())
        # Match the repository's ms-precision naive-UTC convention.
        now = datetime.now(UTC).replace(tzinfo=None)
        before = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        updated = await repo.mark_node_status(
            created.id,
            "n1",
            "running",
            started_at=before,
        )
        assert len(updated.node_results) == 1
        assert updated.node_results[0].started_at == before

    @pytest.mark.asyncio
    async def test_mark_node_status_updates_existing(
        self, repo: PlanExecutionRepository
    ) -> None:
        created = await repo.create(_exec_input())
        await repo.mark_node_status(created.id, "n1", "running")
        # Match the repository's millisecond precision — anything finer
        # is truncated the moment the row hits Mongo.
        now = datetime.now(UTC).replace(tzinfo=None)
        after = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        final = await repo.mark_node_status(
            created.id,
            "n1",
            "succeeded",
            finished_at=after,
        )
        assert len(final.node_results) == 1
        assert final.node_results[0].status == "succeeded"
        assert final.node_results[0].finished_at == after

    @pytest.mark.asyncio
    async def test_mark_node_status_records_error(
        self, repo: PlanExecutionRepository
    ) -> None:
        """Per-node failure carries a structured error envelope (ADR-0017)."""
        created = await repo.create(_exec_input())
        err = {"code": "upstream_5xx", "message": "API gateway timeout"}
        final = await repo.mark_node_status(
            created.id,
            "n1",
            "failed",
            error=err,
            retry_count=2,
        )
        assert final.node_results[0].status == "failed"
        assert final.node_results[0].error == err
        assert final.node_results[0].retry_count == 2

    @pytest.mark.asyncio
    async def test_mark_node_status_missing_execution_raises(
        self, repo: PlanExecutionRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.mark_node_status(str(ObjectId()), "n1", "running")


class TestPlanExecutionDelete:
    """`delete` — admin hard-delete."""

    @pytest.mark.asyncio
    async def test_delete_removes_execution(self, repo: PlanExecutionRepository) -> None:
        created = await repo.create(_exec_input())
        await repo.delete(created.id)
        with pytest.raises(NotFoundError):
            await repo.get(created.id)

    @pytest.mark.asyncio
    async def test_delete_missing_raises_not_found(
        self, repo: PlanExecutionRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.delete(str(ObjectId()))


# Reference the unused `PlanNodeStatus` import to anchor the type-symmetry hint.
_ = PlanNodeStatus
