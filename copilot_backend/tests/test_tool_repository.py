"""Tests for `ToolRepository` (T05 / #6).

Acceptance criterion: "tools 含 risk_level / status / credentials_ref".

The seam is the public API of `ToolRepository`. We exercise every method
against an in-memory `mongomock_motor` database and verify the wire-level
shapes, the error contracts, and the canonical-read invariants.

The encryption layer is irrelevant here — `tools` documents carry only
the foreign-key pointer (`credentials_ref`), never the credential bytes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.db.errors import DuplicateKeyError, InvalidIdError, NotFoundError
from app.db.init_db import init_database
from app.db.schemas import ToolCreate, ToolUpdate
from app.repositories.tools import ToolRepository


@pytest.fixture
async def repo() -> ToolRepository:
    """A fresh `ToolRepository` against an isolated in-memory DB."""
    db = AsyncMongoMockClient()["copilot_tool_test"]
    await init_database(db)
    return ToolRepository(db)


def _tool_input(**overrides: object) -> ToolCreate:
    """A valid Tool input, with per-test overrides applied.

    `risk_level` defaults to `read` (the safe default for an LLM-facing
    Tool; admin promotes to `write` / `destructive` later via `update`).
    `status` defaults to `draft` per ADR-0018 — `active` is set by
    `set_status` once the admin has reviewed the description.
    """
    base: dict[str, object] = {
        "name": "list_customers",
        "description": "List customers by region. Returns paginated customer records.",
        "risk_level": "read",
        "status": "draft",
        "parameters_schema": {
            "type": "object",
            "properties": {"region": {"type": "string"}},
            "required": ["region"],
        },
        "http_method": "GET",
        "http_url_template": "https://api.example.com/customers?region={region}",
        "http_headers": {"Accept": "application/json"},
        "http_body_template": None,
        "source": "manual",
        "source_ref": None,
        "credentials_ref": None,
    }
    base.update(overrides)
    return ToolCreate(**base)  # type: ignore[arg-type]


class TestToolCreate:
    """`create` — happy path + duplicate-name handling."""

    @pytest.mark.asyncio
    async def test_create_persists_risk_level_status_credentials_ref(
        self, repo: ToolRepository
    ) -> None:
        """The three required fields land on the persisted document.

        This is the literal acceptance criterion: "tools 含 risk_level /
        status / credentials_ref". `credentials_ref` is exercised as a
        foreign-key pointer to a sibling collection's `_id`.
        """
        credential_id = str(ObjectId())
        tool = await repo.create(
            _tool_input(
                risk_level="write",
                status="active",
                credentials_ref=credential_id,
            )
        )
        assert tool.risk_level == "write"
        assert tool.status == "active"
        assert tool.credentials_ref == credential_id

        # And on the raw Mongo doc.
        raw = await repo._collection.find_one({"_id": ObjectId(tool.id)})
        assert raw is not None
        assert raw["risk_level"] == "write"
        assert raw["status"] == "active"
        assert raw["credentials_ref"] == credential_id

    @pytest.mark.asyncio
    async def test_create_returns_canonical_shape(self, repo: ToolRepository) -> None:
        """`create` returns a `Tool` with timestamps + id populated."""
        created = await repo.create(_tool_input())
        assert created.id
        assert ObjectId(created.id)
        assert isinstance(created.created_at, datetime)
        assert created.created_at == created.updated_at
        # Defaults from the schema land on the row.
        assert created.status == "draft"
        assert created.risk_level == "read"
        assert created.source == "manual"

    @pytest.mark.asyncio
    async def test_duplicate_name_raises_duplicate_key_error(
        self, repo: ToolRepository
    ) -> None:
        """A second Tool with the same `name` collides on the unique index."""
        await repo.create(_tool_input())
        with pytest.raises(DuplicateKeyError) as exc:
            await repo.create(_tool_input(description="Different description"))
        assert exc.value.code == "duplicate_key"
        assert exc.value.details is not None
        assert "index" in exc.value.details


class TestToolRead:
    """`get`, `get_in_db`, `get_by_name`, list helpers."""

    @pytest.mark.asyncio
    async def test_get_by_id(self, repo: ToolRepository) -> None:
        created = await repo.create(_tool_input())
        fetched = await repo.get(created.id)
        assert fetched.id == created.id
        assert fetched.name == "list_customers"

    @pytest.mark.asyncio
    async def test_get_missing_raises_not_found(self, repo: ToolRepository) -> None:
        with pytest.raises(NotFoundError):
            await repo.get(str(ObjectId()))

    @pytest.mark.asyncio
    async def test_get_with_invalid_id_raises_invalid_id(
        self, repo: ToolRepository
    ) -> None:
        with pytest.raises(InvalidIdError):
            await repo.get("not-an-objectid")

    @pytest.mark.asyncio
    async def test_get_by_name(self, repo: ToolRepository) -> None:
        await repo.create(_tool_input(name="find_me"))
        fetched = await repo.get_by_name("find_me")
        assert fetched.name == "find_me"

    @pytest.mark.asyncio
    async def test_get_by_name_missing_raises_not_found(
        self, repo: ToolRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.get_by_name("ghost")

    @pytest.mark.asyncio
    async def test_get_in_db_returns_persisted_shape(
        self, repo: ToolRepository
    ) -> None:
        """`get_in_db` returns the persisted-shape row (T44 will use this)."""
        created = await repo.create(_tool_input())
        in_db = await repo.get_in_db(created.id)
        assert in_db.id == created.id
        # Persisted shape carries the same fields as canonical; the split
        # exists for symmetry with other repositories.
        assert in_db.parameters_schema == created.parameters_schema


class TestToolList:
    """`list_active`, `list_all`, `list_by_status`, `list_by_credential`."""

    @pytest.mark.asyncio
    async def test_list_active_filters_correctly(self, repo: ToolRepository) -> None:
        """`list_active` returns only `status='active'` Tools (ADR-0018)."""
        await repo.create(_tool_input(name="draft_tool", status="draft"))
        await repo.create(_tool_input(name="active_tool", status="active"))
        await repo.create(_tool_input(name="disabled_tool", status="disabled"))
        active = await repo.list_active()
        assert {t.name for t in active} == {"active_tool"}
        # Sorted by name for stable LLM-facing catalog order.
        assert [t.name for t in active] == ["active_tool"]

    @pytest.mark.asyncio
    async def test_list_all_returns_every_tool(self, repo: ToolRepository) -> None:
        await repo.create(_tool_input(name="a"))
        await repo.create(_tool_input(name="b"))
        await repo.create(_tool_input(name="c"))
        names = {t.name for t in await repo.list_all()}
        assert names == {"a", "b", "c"}

    @pytest.mark.asyncio
    async def test_list_by_status(self, repo: ToolRepository) -> None:
        """Per-status filter for the admin Registry tabs."""
        await repo.create(_tool_input(name="d1", status="draft"))
        await repo.create(_tool_input(name="d2", status="draft"))
        await repo.create(_tool_input(name="a1", status="active"))
        drafts = await repo.list_by_status("draft")
        assert {t.name for t in drafts} == {"d1", "d2"}

    @pytest.mark.asyncio
    async def test_list_by_credential(self, repo: ToolRepository) -> None:
        """Used by credential-deletion pre-check."""
        cred = str(ObjectId())
        await repo.create(_tool_input(name="t1", credentials_ref=cred))
        await repo.create(_tool_input(name="t2", credentials_ref=cred))
        await repo.create(_tool_input(name="t3", credentials_ref=str(ObjectId())))
        using = await repo.list_by_credential(cred)
        assert {t.name for t in using} == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_count_by_status(self, repo: ToolRepository) -> None:
        assert await repo.count_by_status("active") == 0
        await repo.create(_tool_input(name="a", status="active"))
        await repo.create(_tool_input(name="b", status="active"))
        await repo.create(_tool_input(name="c", status="draft"))
        assert await repo.count_by_status("active") == 2
        assert await repo.count_by_status("draft") == 1
        assert await repo.count_by_status("disabled") == 0


class TestToolUpdate:
    """`update`, `set_status` — partial mutation paths."""

    @pytest.mark.asyncio
    async def test_update_changes_description_and_bumps_updated_at(
        self, repo: ToolRepository
    ) -> None:
        created = await repo.create(_tool_input())
        before = created.updated_at
        # Ensure monotonic clock difference.
        await asyncio.sleep(0.005)
        updated = await repo.update(
            created.id,
            ToolUpdate(description="Refined description after admin review."),
        )
        assert updated.description == "Refined description after admin review."
        assert updated.updated_at > before
        assert updated.created_at == created.created_at  # not touched

    @pytest.mark.asyncio
    async def test_update_can_promote_risk_level(self, repo: ToolRepository) -> None:
        """Promoting from `read` to `destructive` is a normal PATCH.

        Per ADR-0004 this changes the HITL behaviour for new Plans.
        Already-issued Plans carry their snapshot (ADR-0027), so this
        only affects future Plans.
        """
        created = await repo.create(_tool_input(risk_level="read"))
        assert created.risk_level == "read"
        updated = await repo.update(created.id, ToolUpdate(risk_level="destructive"))
        assert updated.risk_level == "destructive"

    @pytest.mark.asyncio
    async def test_update_can_rebind_credentials_ref(
        self, repo: ToolRepository
    ) -> None:
        """Rebinding to a new credential works as a partial update."""
        old_cred = str(ObjectId())
        new_cred = str(ObjectId())
        created = await repo.create(_tool_input(credentials_ref=old_cred))
        updated = await repo.update(created.id, ToolUpdate(credentials_ref=new_cred))
        assert updated.credentials_ref == new_cred

    @pytest.mark.asyncio
    async def test_update_missing_raises_not_found(
        self, repo: ToolRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.update(str(ObjectId()), ToolUpdate(description="x"))

    @pytest.mark.asyncio
    async def test_set_status_activates_and_disables(
        self, repo: ToolRepository
    ) -> None:
        """Activation / disablement is a dedicated path (audit hook target)."""
        created = await repo.create(_tool_input(status="draft"))
        activated = await repo.set_status(created.id, "active")
        assert activated.status == "active"
        disabled = await repo.set_status(activated.id, "disabled")
        assert disabled.status == "disabled"

    @pytest.mark.asyncio
    async def test_set_status_missing_raises_not_found(
        self, repo: ToolRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.set_status(str(ObjectId()), "active")


class TestToolDelete:
    """`delete` — hard delete for never-activated Tools."""

    @pytest.mark.asyncio
    async def test_delete_removes_tool(self, repo: ToolRepository) -> None:
        created = await repo.create(_tool_input())
        await repo.delete(created.id)
        with pytest.raises(NotFoundError):
            await repo.get(created.id)

    @pytest.mark.asyncio
    async def test_delete_missing_raises_not_found(
        self, repo: ToolRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.delete(str(ObjectId()))

    @pytest.mark.asyncio
    async def test_delete_invalid_id_raises_invalid_id(
        self, repo: ToolRepository
    ) -> None:
        with pytest.raises(InvalidIdError):
            await repo.delete("not-an-objectid")