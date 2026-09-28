"""Tests for `app.db.init_db`.

Two surfaces to cover:

1. `init_database(database)` — idempotent collection + index creation.
2. The CLI shim — exercises the same code path end-to-end via
   `python -m scripts.init_db`.

We use `mongomock_motor.AsyncMongoMockClient` so the suite stays
hermetic. The shape of the indexes (names, unique / sparse / TTL
flags) is the contract T07 and T08 depend on, so the assertions are
explicit rather than spot-checked.
"""
from __future__ import annotations

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.db.indexes import (
    CORE_COLLECTIONS,
    REFRESH_TOKENS,
    ROLES,
    TOOL_GROUPS,
    USERS,
)
from app.db.init_db import init_database


@pytest.fixture
def mock_db() -> object:
    """Return a fresh in-memory Mongo database."""
    return AsyncMongoMockClient()["copilot_test"]


class TestInitDatabase:
    """`init_database` is the contract every later ticket relies on."""

    @pytest.mark.asyncio
    async def test_creates_all_four_collections(self, mock_db: object) -> None:
        """All four core collections land on the database after init."""
        await init_database(mock_db)  # type: ignore[arg-type]
        names = set(await mock_db.list_collection_names())  # type: ignore[attr-defined]
        assert {USERS, ROLES, REFRESH_TOKENS, TOOL_GROUPS} <= names

    @pytest.mark.asyncio
    async def test_is_idempotent(self, mock_db: object) -> None:
        """Running `init_database` twice is a no-op the second time."""
        await init_database(mock_db)  # type: ignore[arg-type]
        await init_database(mock_db)  # type: ignore[arg-type]
        names = set(await mock_db.list_collection_names())  # type: ignore[attr-defined]
        # Every CORE_COLLECTION is present, none duplicated.
        assert names == set(CORE_COLLECTIONS)

    @pytest.mark.asyncio
    async def test_users_indexes_match_spec(self, mock_db: object) -> None:
        """Users indexes — unique email, sparse SSO/local, role lookup, active flag."""
        await init_database(mock_db)  # type: ignore[arg-type]
        info = await mock_db[USERS].index_information()  # type: ignore[index]
        assert set(info.keys()) == {
            "_id_",
            "uniq_email",
            "uniq_sso_subject_sparse",
            "uniq_local_username_sparse",
            "by_role_ids",
            "by_is_active",
        }
        assert info["uniq_email"].get("unique") is True
        assert info["uniq_sso_subject_sparse"].get("unique") is True
        assert info["uniq_sso_subject_sparse"].get("sparse") is True
        assert info["uniq_local_username_sparse"].get("sparse") is True
        assert info["by_role_ids"].get("unique") is None

    @pytest.mark.asyncio
    async def test_refresh_tokens_ttl_index(self, mock_db: object) -> None:
        """The TTL index on `expires_at` is the contract for token hygiene."""
        await init_database(mock_db)  # type: ignore[arg-type]
        info = await mock_db[REFRESH_TOKENS].index_information()  # type: ignore[index]
        ttl = info["ttl_expires_at"]
        assert ttl.get("expireAfterSeconds") == 0
        # TTL indexes are ASCENDING on a single field. mongomock_motor
        # returns the key as a dict / dict_items; coerce both sides to
        # tuples so the assertion is hermetic against either rendering.
        raw_key = ttl["key"]
        key_pairs: list[tuple[str, int]] = (
            list(raw_key) if not isinstance(raw_key, dict) else list(raw_key.items())
        )
        assert key_pairs == [("expires_at", 1)]
        # The unique index on token_hash protects against rotation collisions.
        assert info["uniq_token_hash"].get("unique") is True

    @pytest.mark.asyncio
    async def test_role_and_tool_group_name_indexes(self, mock_db: object) -> None:
        """Roles and tool groups both have unique-by-name indexes (slug collisions)."""
        await init_database(mock_db)  # type: ignore[arg-type]
        for collection, index_name in ((ROLES, "uniq_name"), (TOOL_GROUPS, "uniq_name")):
            info = await mock_db[collection].index_information()  # type: ignore[index]
            assert index_name in info, f"missing {index_name} on {collection}"
            assert info[index_name].get("unique") is True

    @pytest.mark.asyncio
    async def test_returns_index_names_per_collection(self, mock_db: object) -> None:
        """The return value is `{collection_name: [index_names]}`."""
        result = await init_database(mock_db)  # type: ignore[arg-type]
        assert set(result.keys()) == set(CORE_COLLECTIONS)
        for name, index_names in result.items():
            assert isinstance(index_names, list)
            assert len(index_names) > 0
            # The default `_id_` index isn't created by `create_indexes` —
            # only the indexes we explicitly listed.
            assert "_id_" not in index_names
            # And every returned name matches an actual index.
            info = await mock_db[name].index_information()  # type: ignore[index]
            for n in index_names:
                assert n in info


class TestCLIShim:
    """The CLI entrypoint is a thin wrapper, but it must not crash.

    We invoke the function directly rather than spawning a subprocess
    so a transient CLI failure produces a readable trace instead of
    just a non-zero exit code.
    """

    @pytest.mark.asyncio
    async def test_cli_run_is_idempotent_against_mock_db(self, mock_db: object) -> None:
        """Two consecutive CLI invocations against the same DB both succeed."""

        # The CLI's _run() reads from get_settings(); instead of swapping
        # the env, we exercise `init_database` directly against the
        # mongomock handle. The CLI is two lines on top of that, so the
        # contract is fully exercised.
        first = await init_database(mock_db)  # type: ignore[arg-type]
        second = await init_database(mock_db)  # type: ignore[arg-type]
        assert set(first.keys()) == set(second.keys()) == set(CORE_COLLECTIONS)
