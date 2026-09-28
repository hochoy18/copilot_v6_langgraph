"""Tests for `TurnRepository` (T06 / #7).

Turns are append-only — every test exercises the create + read paths
that T10 (conversation detail) and T23 (SSE pipeline) reach for.
There is no `update` path because editing a Turn would re-write
history.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.db.errors import InvalidIdError, NotFoundError
from app.db.init_db import init_database
from app.db.schemas import TurnCreate
from app.repositories.turns import TurnRepository


@pytest.fixture
async def repo() -> TurnRepository:
    """A fresh `TurnRepository` against an isolated in-memory DB."""
    db = AsyncMongoMockClient()["copilot_turn_test"]
    await init_database(db)
    return TurnRepository(db)


def _user_input(**overrides: object) -> TurnCreate:
    base: dict[str, object] = {
        "conversation_id": str(ObjectId()),
        "role": "user",
        "content": "List Q3 invoices for Acme.",
        "plan_id": None,
        "extra": {},
    }
    base.update(overrides)
    return TurnCreate(**base)  # type: ignore[arg-type]


def _assistant_input(**overrides: object) -> TurnCreate:
    base: dict[str, object] = {
        "conversation_id": str(ObjectId()),
        "role": "assistant",
        "content": "Here are Acme's Q3 invoices.",
        "plan_id": None,
        "extra": {},
    }
    base.update(overrides)
    return TurnCreate(**base)  # type: ignore[arg-type]


class TestTurnCreate:
    """`create` — append-only path."""

    @pytest.mark.asyncio
    async def test_create_returns_canonical_shape(
        self, repo: TurnRepository
    ) -> None:
        created = await repo.create(_user_input())
        assert created.id
        assert ObjectId(created.id)
        assert isinstance(created.created_at, datetime)
        assert created.role == "user"
        assert created.extra == {}

    @pytest.mark.asyncio
    async def test_create_persists_extra_metadata(
        self, repo: TurnRepository
    ) -> None:
        created = await repo.create(
            _user_input(
                extra={"model": "gpt-4o-mini", "sse_done_ms": 1240}
            )
        )
        assert created.extra == {"model": "gpt-4o-mini", "sse_done_ms": 1240}

    @pytest.mark.asyncio
    async def test_create_with_plan_id_attaches_plan(
        self, repo: TurnRepository
    ) -> None:
        plan_id = str(ObjectId())
        created = await repo.create(_user_input(plan_id=plan_id))
        assert created.plan_id == plan_id


class TestTurnRead:
    """`get`, `list_by_conversation`, `list_by_plan`."""

    @pytest.mark.asyncio
    async def test_get_by_id(self, repo: TurnRepository) -> None:
        created = await repo.create(_user_input())
        fetched = await repo.get(created.id)
        assert fetched.id == created.id
        assert fetched.content == "List Q3 invoices for Acme."

    @pytest.mark.asyncio
    async def test_get_missing_raises_not_found(self, repo: TurnRepository) -> None:
        with pytest.raises(NotFoundError):
            await repo.get(str(ObjectId()))

    @pytest.mark.asyncio
    async def test_get_invalid_id_raises_invalid_id(
        self, repo: TurnRepository
    ) -> None:
        with pytest.raises(InvalidIdError):
            await repo.get("not-an-objectid")

    @pytest.mark.asyncio
    async def test_list_by_conversation_returns_in_chronological_order(
        self, repo: TurnRepository
    ) -> None:
        conv_id = str(ObjectId())
        t1 = await repo.create(_user_input(conversation_id=conv_id, content="one"))
        t2 = await repo.create(
            _assistant_input(conversation_id=conv_id, content="two")
        )
        t3 = await repo.create(_user_input(conversation_id=conv_id, content="three"))

        turns = await repo.list_by_conversation(conv_id)
        assert [t.id for t in turns] == [t1.id, t2.id, t3.id]

    @pytest.mark.asyncio
    async def test_list_by_conversation_excludes_other_sessions(
        self, repo: TurnRepository
    ) -> None:
        c1, c2 = str(ObjectId()), str(ObjectId())
        t1 = await repo.create(_user_input(conversation_id=c1))
        t2 = await repo.create(_assistant_input(conversation_id=c2))
        ours = await repo.list_by_conversation(c1)
        assert {t.id for t in ours} == {t1.id}
        assert t2.id not in {t.id for t in ours}

    @pytest.mark.asyncio
    async def test_list_by_plan_returns_attached_turns(
        self, repo: TurnRepository
    ) -> None:
        plan_id = str(ObjectId())
        t1 = await repo.create(_user_input(plan_id=plan_id, content="p1"))
        t2 = await repo.create(_user_input(plan_id=plan_id, content="p2"))
        # Unattached turn — must NOT appear.
        await repo.create(_user_input(plan_id=None))
        ours = await repo.list_by_plan(plan_id)
        assert {t.id for t in ours} == {t1.id, t2.id}


class TestTurnDelete:
    """`delete_by_conversation` — cascading helper for admin hard-delete."""

    @pytest.mark.asyncio
    async def test_delete_by_conversation_removes_all_turns(
        self, repo: TurnRepository
    ) -> None:
        c1, c2 = str(ObjectId()), str(ObjectId())
        await repo.create(_user_input(conversation_id=c1))
        await repo.create(_user_input(conversation_id=c1))
        # Unrelated conversation — must survive.
        survivor = await repo.create(_user_input(conversation_id=c2))

        deleted = await repo.delete_by_conversation(c1)
        assert deleted == 2
        assert await repo.list_by_conversation(c1) == []
        assert len(await repo.list_by_conversation(c2)) == 1
        assert (await repo.get(survivor.id)).id == survivor.id

    @pytest.mark.asyncio
    async def test_delete_by_conversation_on_empty_returns_zero(
        self, repo: TurnRepository
    ) -> None:
        assert await repo.delete_by_conversation(str(ObjectId())) == 0
