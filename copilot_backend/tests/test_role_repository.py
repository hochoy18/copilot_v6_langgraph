"""Tests for `RoleRepository` — covers `list_by_ids` (T12 / #11).

`list_by_ids` is the seam the admin-role guard (`app.security.admin`)
reaches for. It must:

* Return the matching Roles in `name` order.
* Tolerate corrupt ids (string that isn't an ObjectId) without
  raising.
* Return an empty list for empty / all-invalid input rather than
  blowing up — the admin guard relies on the empty list to deny
  access.
"""
from __future__ import annotations

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.db.init_db import init_database
from app.db.schemas import RoleCreate
from app.repositories.roles import RoleRepository


@pytest.fixture
async def repo() -> RoleRepository:
    """A fresh `RoleRepository` against an isolated in-memory DB."""
    db = AsyncMongoMockClient()["copilot_role_repo_test"]
    await init_database(db)
    return RoleRepository(db)


class TestListByIds:
    """`RoleRepository.list_by_ids` — batched lookup by `_id`."""

    @pytest.mark.asyncio
    async def test_returns_matching_roles_sorted_by_name(
        self, repo: RoleRepository
    ) -> None:
        admin = await repo.create(RoleCreate(name="admin", description="x"))
        user_role = await repo.create(RoleCreate(name="user", description="y"))
        other = await repo.create(RoleCreate(name="viewer", description="z"))

        rows = await repo.list_by_ids([other.id, admin.id, user_role.id])
        assert [r.name for r in rows] == ["admin", "user", "viewer"]

    @pytest.mark.asyncio
    async def test_skips_unknown_ids(self, repo: RoleRepository) -> None:
        admin = await repo.create(RoleCreate(name="admin"))
        rows = await repo.list_by_ids([admin.id, str(ObjectId())])
        assert [r.name for r in rows] == ["admin"]

    @pytest.mark.asyncio
    async def test_skips_corrupt_ids(self, repo: RoleRepository) -> None:
        """A non-ObjectId string is treated as "no such role"."""
        admin = await repo.create(RoleCreate(name="admin"))
        rows = await repo.list_by_ids([admin.id, "not-an-objectid"])
        assert [r.name for r in rows] == ["admin"]

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_list(self, repo: RoleRepository) -> None:
        assert await repo.list_by_ids([]) == []

    @pytest.mark.asyncio
    async def test_all_invalid_returns_empty_list(self, repo: RoleRepository) -> None:
        """No matches → empty list. The admin guard relies on this for denial."""
        assert await repo.list_by_ids(["not-an-objectid", "also-not"]) == []
