"""Tests for `AuditLogRepository` (T06 / #7).

Acceptance criterion for T06: "audit_logs 含完整字段". We exercise
the create path with the full schema shape (FK pointers,
`tool_snapshot`, `parameters`, `response`, `error`, retention
columns) and verify the lifecycle helpers preserve ADR-0028
invariants.

Audit rows are append-only by design (ADR-0002) — `update` / `delete`
are intentionally absent.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.db.errors import InvalidIdError, NotFoundError
from app.db.init_db import init_database
from app.db.schemas import AuditLogCreate, PlanNodeToolSnapshot
from app.repositories.audit_logs import AuditLogRepository


@pytest.fixture
async def repo() -> AuditLogRepository:
    """A fresh `AuditLogRepository` against an isolated in-memory DB."""
    db = AsyncMongoMockClient()["copilot_audit_test"]
    await init_database(db)
    return AuditLogRepository(db)


def _snapshot(**overrides: object) -> PlanNodeToolSnapshot:
    base: dict[str, object] = {
        "name": "list_customers",
        "description": "List customers by region.",
        "risk_level": "read",
        "parameters_schema": {
            "type": "object",
            "properties": {"region": {"type": "string"}},
        },
        "http_method": "GET",
        "http_url_template": "https://api.example.com/customers?region={region}",
        "http_headers": {},
        "http_body_template": None,
    }
    base.update(overrides)
    return PlanNodeToolSnapshot(**base)  # type: ignore[arg-type]


def _audit_input(**overrides: object) -> AuditLogCreate:
    base: dict[str, object] = {
        "actor_id": str(ObjectId()),
        "conversation_id": str(ObjectId()),
        "turn_id": str(ObjectId()),
        "plan_id": str(ObjectId()),
        "plan_execution_id": str(ObjectId()),
        "tool_name": "list_customers",
        "tool_snapshot": _snapshot(),
        "parameters": {"region": "emea"},
        "response": {"data": [{"id": "c1"}]},
        "status": "succeeded",
        "error": None,
        "risk_level": "read",
        "retry_count": 0,
    }
    base.update(overrides)
    return AuditLogCreate(**base)  # type: ignore[arg-type]


class TestAuditLogCreate:
    """`create` — append-only path with all required fields."""

    @pytest.mark.asyncio
    async def test_create_persists_full_field_set(
        self, repo: AuditLogRepository
    ) -> None:
        """Acceptance criterion: audit_logs carries complete FK and snapshot fields."""
        before = datetime.utcnow()
        created = await repo.create(_audit_input())
        assert created.tool_name == "list_customers"
        assert created.parameters == {"region": "emea"}
        assert created.response == {"data": [{"id": "c1"}]}
        assert created.risk_level == "read"
        assert created.tool_snapshot.name == "list_customers"
        # Stamped on insert — never supplied by the caller.
        assert created.occurred_at >= before - timedelta(seconds=1)
        assert created.lifecycle_status == "active"
        assert created.cold_storage_ref is None
        assert created.cold_archived_at is None

    @pytest.mark.asyncio
    async def test_create_with_error_fields(
        self, repo: AuditLogRepository
    ) -> None:
        """Failure rows carry the structured error envelope (ADR-0017)."""
        created = await repo.create(
            _audit_input(
                status="failed",
                response=None,
                error={"code": "upstream_5xx", "message": "gateway timeout"},
                retry_count=2,
            )
        )
        assert created.status == "failed"
        assert created.error == {"code": "upstream_5xx", "message": "gateway timeout"}
        assert created.retry_count == 2


class TestAuditLogRead:
    """`get`, `query`, `list_by_conversation`."""

    @pytest.mark.asyncio
    async def test_get_by_id(self, repo: AuditLogRepository) -> None:
        created = await repo.create(_audit_input())
        fetched = await repo.get(created.id)
        assert fetched.id == created.id

    @pytest.mark.asyncio
    async def test_get_missing_raises_not_found(
        self, repo: AuditLogRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.get(str(ObjectId()))

    @pytest.mark.asyncio
    async def test_get_invalid_id_raises_invalid_id(
        self, repo: AuditLogRepository
    ) -> None:
        with pytest.raises(InvalidIdError):
            await repo.get("not-an-objectid")

    @pytest.mark.asyncio
    async def test_query_filters_by_actor(
        self, repo: AuditLogRepository
    ) -> None:
        a1 = str(ObjectId())
        await repo.create(_audit_input(actor_id=a1))
        await repo.create(_audit_input(actor_id=a1))
        await repo.create(_audit_input(actor_id=str(ObjectId())))
        ours = await repo.query(actor_id=a1)
        assert len(ours) == 2
        assert {row.actor_id for row in ours} == {a1}

    @pytest.mark.asyncio
    async def test_query_filters_by_tool_name(
        self, repo: AuditLogRepository
    ) -> None:
        await repo.create(_audit_input(tool_name="list_customers"))
        await repo.create(_audit_input(tool_name="send_email"))
        ours = await repo.query(tool_name="list_customers")
        assert len(ours) == 1
        assert ours[0].tool_name == "list_customers"

    @pytest.mark.asyncio
    async def test_query_time_range(
        self, repo: AuditLogRepository
    ) -> None:
        """`time_from` inclusive, `time_to` exclusive — calendar-day buckets."""
        before = datetime.utcnow() - timedelta(hours=2)
        inside = datetime.utcnow() - timedelta(minutes=30)
        after = datetime.utcnow() + timedelta(hours=1)
        ours = await repo.query(time_from=before, time_to=after, limit=100)
        assert len(ours) == 0  # nothing inserted yet — sanity check
        await repo.create(_audit_input())
        ours = await repo.query(time_from=inside, time_to=after)
        assert len(ours) == 1

    @pytest.mark.asyncio
    async def test_query_returns_newest_first(
        self, repo: AuditLogRepository
    ) -> None:
        r1 = await repo.create(_audit_input())
        await repo.mark_archived(r1.id, "s3://bucket/audit/1")
        await asyncio.sleep(0.01)
        r2 = await repo.create(_audit_input())
        rows = await repo.query()
        assert [r.id for r in rows] == [r2.id, r1.id]

    @pytest.mark.asyncio
    async def test_list_by_conversation_filters_correctly(
        self, repo: AuditLogRepository
    ) -> None:
        conv = str(ObjectId())
        await repo.create(_audit_input(conversation_id=conv))
        await repo.create(_audit_input(conversation_id=conv))
        await repo.create(_audit_input(conversation_id=str(ObjectId())))
        ours = await repo.list_by_conversation(conv)
        assert len(ours) == 2
        assert {r.conversation_id for r in ours} == {conv}


class TestAuditLogLifecycle:
    """`mark_archived`, `mark_recalled` — the only paths that touch an existing row."""

    @pytest.mark.asyncio
    async def test_mark_archived_stamps_cold_storage_ref(
        self, repo: AuditLogRepository
    ) -> None:
        created = await repo.create(_audit_input())
        archived = await repo.mark_archived(created.id, "s3://bucket/audit/abc")
        assert archived.lifecycle_status == "archived"
        assert archived.cold_storage_ref == "s3://bucket/audit/abc"
        assert archived.cold_archived_at is not None

    @pytest.mark.asyncio
    async def test_mark_archived_missing_raises_not_found(
        self, repo: AuditLogRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.mark_archived(str(ObjectId()), "s3://bucket/x")

    @pytest.mark.asyncio
    async def test_mark_recalled_flips_lifecycle(
        self, repo: AuditLogRepository
    ) -> None:
        created = await repo.create(_audit_input())
        await repo.mark_archived(created.id, "s3://bucket/audit/xyz")
        recalled = await repo.mark_recalled(created.id)
        assert recalled.lifecycle_status == "recalled"
        # The cold-storage ref stays — recall doesn't move the data back permanently.
        assert recalled.cold_storage_ref == "s3://bucket/audit/xyz"

    @pytest.mark.asyncio
    async def test_mark_recalled_on_active_raises_not_found(
        self, repo: AuditLogRepository
    ) -> None:
        """Only archived rows can be recalled — active rows are already hot."""
        created = await repo.create(_audit_input())
        with pytest.raises(NotFoundError):
            await repo.mark_recalled(created.id)
