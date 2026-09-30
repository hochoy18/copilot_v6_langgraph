"""Tests for the lifecycle extensions on `ConversationRepository` (T39 / #45).

Covers the four seams the lifecycle sweep + reactivate flow depend
on:

* `mark_idle` — atomic `active → idle` with `idle_since` stamping.
* `mark_archived` — atomic `idle → archived` with `archived_since`.
* `list_idle_candidates` / `list_archive_candidates` — threshold
  filters that the sweep drives.
* `create_from_reactivate` — inserts a new `active` row with the
  `reactivated_from_id` FK + counter.

The clock is injected through the repository's `now=` parameter
where applicable so tests don't have to `sleep(...)` their way
across the 15-minute / 30-day boundaries.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.db.errors import NotFoundError
from app.db.init_db import init_database
from app.db.schemas import ConversationCreate
from app.repositories.base import utcnow
from app.repositories.conversations import ConversationRepository


@pytest.fixture
async def repo() -> ConversationRepository:
    """A fresh `ConversationRepository` against an isolated in-memory DB."""
    db = AsyncMongoMockClient()["copilot_conv_lifecycle_repo_test"]
    await init_database(db)
    return ConversationRepository(db)


def _conv_input(**overrides: object) -> ConversationCreate:
    base: dict[str, object] = {
        "user_id": str(ObjectId()),
        "title": "session",
        "status": "active",
    }
    base.update(overrides)
    return ConversationCreate(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# mark_idle
# ---------------------------------------------------------------------------


class TestMarkIdle:
    """`mark_idle` — atomic transition that stamps `idle_since`."""

    @pytest.mark.asyncio
    async def test_marks_idle_and_stamps_idle_since(
        self, repo: ConversationRepository
    ) -> None:
        created = await repo.create(_conv_input())
        stamp = utcnow() + timedelta(minutes=20)
        marked = await repo.mark_idle(created.id, now=stamp)

        assert marked.status == "idle"
        assert marked.idle_since == stamp
        assert marked.updated_at == stamp

    @pytest.mark.asyncio
    async def test_idle_since_persists_on_re_read(
        self, repo: ConversationRepository
    ) -> None:
        created = await repo.create(_conv_input())
        stamp = utcnow() + timedelta(minutes=30)
        await repo.mark_idle(created.id, now=stamp)

        refreshed = await repo.get(created.id)
        assert refreshed.idle_since == stamp
        assert refreshed.status == "idle"

    @pytest.mark.asyncio
    async def test_mark_idle_on_non_active_raises(
        self, repo: ConversationRepository
    ) -> None:
        """Per ADR-0011 only `active` rows are eligible for the sweep.

        The atomic `status: "active"` filter on the update means a
        row that already moved to `idle` or `archived` surfaces as
        `NotFoundError` rather than corrupting the lifecycle state.
        """
        created = await repo.create(_conv_input())
        await repo.set_status(created.id, "idle")
        with pytest.raises(NotFoundError):
            await repo.mark_idle(created.id)

    @pytest.mark.asyncio
    async def test_mark_idle_missing_raises(
        self, repo: ConversationRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repo.mark_idle(str(ObjectId()))


# ---------------------------------------------------------------------------
# mark_archived
# ---------------------------------------------------------------------------


class TestMarkArchived:
    """`mark_archived` — atomic `idle → archived` transition."""

    @pytest.mark.asyncio
    async def test_marks_archived_and_stamps_archived_since(
        self, repo: ConversationRepository
    ) -> None:
        created = await repo.create(_conv_input())
        await repo.set_status(created.id, "idle")
        stamp = utcnow() + timedelta(days=31)
        marked = await repo.mark_archived(created.id, now=stamp)

        assert marked.status == "archived"
        assert marked.archived_since == stamp
        assert marked.updated_at == stamp

    @pytest.mark.asyncio
    async def test_mark_archived_on_non_idle_raises(
        self, repo: ConversationRepository
    ) -> None:
        created = await repo.create(_conv_input())
        with pytest.raises(NotFoundError):
            await repo.mark_archived(created.id)


# ---------------------------------------------------------------------------
# list_idle_candidates / list_archive_candidates
# ---------------------------------------------------------------------------


class TestListIdleCandidates:
    """`list_idle_candidates` — sweep's source for the `active → idle` branch."""

    @pytest.mark.asyncio
    async def test_returns_only_active_rows_older_than_threshold(
        self, repo: ConversationRepository
    ) -> None:
        user_id = str(ObjectId())
        old = await repo.create(_conv_input(user_id=user_id, title="old"))
        fresh = await repo.create(_conv_input(user_id=user_id, title="fresh"))
        # Stamp `old.last_activity_at` 20 minutes in the past, `fresh` to now.
        threshold = utcnow() - timedelta(minutes=15)
        await repo._collection.update_one(
            {"_id": ObjectId(old.id)},
            {"$set": {"last_activity_at": threshold - timedelta(minutes=5)}},
        )
        # `fresh` keeps its `last_activity_at = now()` from `create`.

        candidates = await repo.list_idle_candidates(threshold=threshold)
        candidate_ids = {c.id for c in candidates}
        assert old.id in candidate_ids
        # `fresh` is above the threshold so it stays out of the candidate set.
        assert fresh.id not in candidate_ids

    @pytest.mark.asyncio
    async def test_excludes_non_active_status(
        self, repo: ConversationRepository
    ) -> None:
        idle = await repo.create(_conv_input())
        archived = await repo.create(_conv_input())
        await repo.set_status(idle.id, "idle")
        await repo.set_status(archived.id, "archived")
        # Both stale but neither is `active` — the candidate set is empty.
        threshold = utcnow() - timedelta(minutes=15)
        assert await repo.list_idle_candidates(threshold=threshold) == []

    @pytest.mark.asyncio
    async def test_empty_when_no_candidates(
        self, repo: ConversationRepository
    ) -> None:
        await repo.create(_conv_input())
        threshold = utcnow() - timedelta(minutes=15)
        assert await repo.list_idle_candidates(threshold=threshold) == []


class TestListArchiveCandidates:
    """`list_archive_candidates` — sweep's source for `idle → archived`."""

    @pytest.mark.asyncio
    async def test_returns_only_idle_rows_with_old_idle_since(
        self, repo: ConversationRepository
    ) -> None:
        old_idle = await repo.create(_conv_input(title="old-idle"))
        new_idle = await repo.create(_conv_input(title="new-idle"))
        await repo.set_status(old_idle.id, "idle")
        await repo.set_status(new_idle.id, "idle")
        # Backdate old_idle's idle_since to 31 days ago.
        await repo._collection.update_one(
            {"_id": ObjectId(old_idle.id)},
            {
                "$set": {
                        "idle_since": utcnow() - timedelta(days=31),
                    }
                },
            )
        # `new_idle` got idle_since = now() via set_status, so it's still fresh.

        threshold = utcnow() - timedelta(days=30)
        candidates = await repo.list_archive_candidates(threshold=threshold)
        assert {c.id for c in candidates} == {old_idle.id}


# ---------------------------------------------------------------------------
# create_from_reactivate
# ---------------------------------------------------------------------------


class TestCreateFromReactivate:
    """`create_from_reactivate` — insert a new `active` row from an archived one."""

    @pytest.mark.asyncio
    async def test_creates_active_row_with_reactivated_from_id(
        self, repo: ConversationRepository
    ) -> None:
        source = await repo.create(_conv_input(title="old"))
        await repo.set_status(source.id, "archived")

        new_conv = await repo.create_from_reactivate(source=source, title="new")

        assert new_conv.status == "active"
        assert new_conv.user_id == source.user_id
        assert new_conv.title == "new"
        assert new_conv.reactivated_from_id == source.id
        assert new_conv.reactivate_count == source.reactivate_count + 1
        # New row carries fresh `last_activity_at` so the idle sweep
        # can't immediately re-classify it.
        assert new_conv.last_activity_at == new_conv.created_at
        assert new_conv.idle_since is None
        assert new_conv.archived_since is None

    @pytest.mark.asyncio
    async def test_default_title_is_empty(
        self, repo: ConversationRepository
    ) -> None:
        source = await repo.create(_conv_input())
        await repo.set_status(source.id, "archived")

        new_conv = await repo.create_from_reactivate(source=source)
        assert new_conv.title == ""

    @pytest.mark.asyncio
    async def test_source_remains_archived(
        self, repo: ConversationRepository
    ) -> None:
        """Reactivate must NOT mutate the source row — it's the audit chain."""
        source = await repo.create(_conv_input())
        await repo.set_status(source.id, "archived")
        await repo.create_from_reactivate(source=source)

        refreshed = await repo.get(source.id)
        assert refreshed.status == "archived"

    @pytest.mark.asyncio
    async def test_reactivate_count_increments_from_source(
        self, repo: ConversationRepository
    ) -> None:
        source = await repo.create(_conv_input())
        await repo.set_status(source.id, "archived")
        # The source itself was fresh, so its count is 0; the new
        # row should land at 1.
        new_conv = await repo.create_from_reactivate(source=source)
        assert new_conv.reactivate_count == 1