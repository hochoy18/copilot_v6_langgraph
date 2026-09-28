"""Tests for `ConversationRepository` (T06 / #7).

Acceptance criterion for T06: "5 collection 创建及索引". We exercise
the public API end-to-end against an in-memory `mongomock_motor` so
the seam is verified against the same shapes T10/T11/T39 will hit.

Per ADR-0011 lifecycle is a discrete state machine — every
transition is a dedicated repository method, not a generic PATCH.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.db.errors import InvalidIdError, NotFoundError
from app.db.init_db import init_database
from app.db.schemas import ConversationCreate, ConversationUpdate
from app.repositories.conversations import ConversationRepository


@pytest.fixture
async def repo() -> ConversationRepository:
    """A fresh `ConversationRepository` against an isolated in-memory DB."""
    db = AsyncMongoMockClient()["copilot_conv_test"]
    await init_database(db)
    return ConversationRepository(db)


def _conv_input(**overrides: object) -> ConversationCreate:
    base: dict[str, object] = {
        "user_id": str(ObjectId()),
        "title": "Q3 invoices",
        "status": "active",
    }
    base.update(overrides)
    return ConversationCreate(**base)  # type: ignore[arg-type]


class TestConversationCreate:
    """`create` — happy path + invariants."""

    @pytest.mark.asyncio
    async def test_create_stamps_last_activity_at_to_now(
        self, repo: ConversationRepository
    ) -> None:
        """Last-activity defaults to created_at so ADR-0011 'active in 15 min' holds immediately."""
        before = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
        conv = await repo.create(_conv_input())
        assert conv.last_activity_at >= before
        assert conv.last_activity_at == conv.created_at

    @pytest.mark.asyncio
    async def test_create_returns_canonical_shape(
        self, repo: ConversationRepository
    ) -> None:
        created = await repo.create(_conv_input(title="Q4 forecasts"))
        assert created.title == "Q4 forecasts"
        assert created.status == "active"
        assert created.id
        assert ObjectId(created.id)
        assert isinstance(created.created_at, datetime)
        assert created.created_at == created.updated_at

    @pytest.mark.asyncio
    async def test_empty_title_persists(self, repo: ConversationRepository) -> None:
        """An empty title is a deliberate state — the Planner titles later."""
        created = await repo.create(_conv_input(title=""))
        assert created.title == ""


class TestConversationRead:
    """`get`, `list_by_user`, `list_by_status`."""

    @pytest.mark.asyncio
    async def test_get_by_id(self, repo: ConversationRepository) -> None:
        created = await repo.create(_conv_input())
        fetched = await repo.get(created.id)
        assert fetched.id == created.id

    @pytest.mark.asyncio
    async def test_get_missing_raises_not_found(
        self, repo: ConversationRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.get(str(ObjectId()))

    @pytest.mark.asyncio
    async def test_get_invalid_id_raises_invalid_id(
        self, repo: ConversationRepository
    ) -> None:
        with pytest.raises(InvalidIdError):
            await repo.get("not-an-objectid")

    @pytest.mark.asyncio
    async def test_list_by_user_orders_by_last_activity(
        self, repo: ConversationRepository
    ) -> None:
        user_id = str(ObjectId())
        c1 = await repo.create(_conv_input(user_id=user_id, title="c1"))
        # Stamp distinct activity times so the order is unambiguous.
        await asyncio.sleep(0.01)
        c2 = await repo.create(_conv_input(user_id=user_id, title="c2"))
        await asyncio.sleep(0.01)
        c3 = await repo.create(_conv_input(user_id=user_id, title="c3"))

        ordered = await repo.list_by_user(user_id)
        # Newest first.
        assert [c.id for c in ordered] == [c3.id, c2.id, c1.id]

    @pytest.mark.asyncio
    async def test_list_by_user_with_status_filter(
        self, repo: ConversationRepository
    ) -> None:
        user_id = str(ObjectId())
        a = await repo.create(_conv_input(user_id=user_id))
        b = await repo.create(_conv_input(user_id=user_id))
        await repo.set_status(b.id, "idle")
        active = await repo.list_by_user(user_id, status="active")
        assert {c.id for c in active} == {a.id}

    @pytest.mark.asyncio
    async def test_list_by_user_excludes_other_users(
        self, repo: ConversationRepository
    ) -> None:
        u1, u2 = str(ObjectId()), str(ObjectId())
        c1 = await repo.create(_conv_input(user_id=u1))
        c2 = await repo.create(_conv_input(user_id=u2))
        assert {c.id for c in await repo.list_by_user(u1)} == {c1.id}
        assert {c.id for c in await repo.list_by_user(u2)} == {c2.id}

    @pytest.mark.asyncio
    async def test_list_by_status_returns_matching_conversations(
        self, repo: ConversationRepository
    ) -> None:
        a = await repo.create(_conv_input())
        b = await repo.create(_conv_input())
        await repo.set_status(b.id, "archived")
        archived = await repo.list_by_status("archived")
        assert {c.id for c in archived} == {b.id}
        active = await repo.list_by_status("active")
        assert {c.id for c in active} == {a.id}


class TestConversationUpdate:
    """`update`, `set_status`, `touch_activity`."""

    @pytest.mark.asyncio
    async def test_update_changes_title_and_bumps_updated_at(
        self, repo: ConversationRepository
    ) -> None:
        created = await repo.create(_conv_input(title="Draft"))
        before = created.updated_at
        await asyncio.sleep(0.005)
        updated = await repo.update(created.id, ConversationUpdate(title="Final"))
        assert updated.title == "Final"
        assert updated.updated_at > before

    @pytest.mark.asyncio
    async def test_update_missing_raises_not_found(
        self, repo: ConversationRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.update(str(ObjectId()), ConversationUpdate(title="x"))

    @pytest.mark.asyncio
    async def test_set_status_transitions_lifecycle(
        self, repo: ConversationRepository
    ) -> None:
        created = await repo.create(_conv_input(status="active"))
        assert created.status == "active"
        idle = await repo.set_status(created.id, "idle")
        assert idle.status == "idle"
        archived = await repo.set_status(created.id, "archived")
        assert archived.status == "archived"

    @pytest.mark.asyncio
    async def test_set_status_missing_raises_not_found(
        self, repo: ConversationRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.set_status(str(ObjectId()), "idle")

    @pytest.mark.asyncio
    async def test_touch_activity_advances_last_activity_at(
        self, repo: ConversationRepository
    ) -> None:
        """`touch_activity` is the per-Turn keep-alive path (ADR-0011)."""
        created = await repo.create(_conv_input())
        original = created.last_activity_at
        await asyncio.sleep(0.01)
        touched = await repo.touch_activity(created.id)
        assert touched.last_activity_at > original
        # Status does NOT change.
        assert touched.status == created.status

    @pytest.mark.asyncio
    async def test_touch_activity_missing_raises_not_found(
        self, repo: ConversationRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.touch_activity(str(ObjectId()))


class TestConversationDelete:
    """`delete` — hard delete for admin data-removal."""

    @pytest.mark.asyncio
    async def test_delete_removes_conversation(
        self, repo: ConversationRepository
    ) -> None:
        created = await repo.create(_conv_input())
        await repo.delete(created.id)
        with pytest.raises(NotFoundError):
            await repo.get(created.id)

    @pytest.mark.asyncio
    async def test_delete_missing_raises_not_found(
        self, repo: ConversationRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.delete(str(ObjectId()))


class TestConversationMisc:
    """`last_activity_at` lookup helper used by the SSE heartbeat."""

    @pytest.mark.asyncio
    async def test_last_activity_at_returns_timestamp(
        self, repo: ConversationRepository
    ) -> None:
        created = await repo.create(_conv_input())
        ts = await repo.last_activity_at(created.id)
        assert isinstance(ts, datetime)
        assert ts == created.last_activity_at

    @pytest.mark.asyncio
    async def test_last_activity_at_missing_returns_none(
        self, repo: ConversationRepository
    ) -> None:
        assert await repo.last_activity_at(str(ObjectId())) is None
