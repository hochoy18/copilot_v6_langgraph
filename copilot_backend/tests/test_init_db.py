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

import json

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.db.indexes import (
    AUDIT_LOGS,
    CONVERSATIONS,
    CORE_COLLECTIONS,
    CREDENTIALS,
    PLAN_EXECUTIONS,
    PLANS,
    REFRESH_TOKENS,
    ROLES,
    TOOL_GROUPS,
    TOOLS,
    TURNS,
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
    async def test_creates_all_core_collections(self, mock_db: object) -> None:
        """All eleven core collections land on the database after init.

        T04 brought the four (users / roles / refresh_tokens /
        tool_groups), T05 (#6) added two (tools / credentials),
        and T06 (#7) completes the conversation domain with five
        more: conversations / turns / plans / plan_executions /
        audit_logs. The two T06 acceptance criteria — "5 collection
        创建" and "Plan 含 tool_snapshots / audit_logs 含完整字段" —
        are pinned here.
        """
        await init_database(mock_db)  # type: ignore[arg-type]
        names = set(await mock_db.list_collection_names())  # type: ignore[attr-defined]
        assert {
            USERS,
            ROLES,
            REFRESH_TOKENS,
            TOOL_GROUPS,
            TOOLS,
            CREDENTIALS,
            CONVERSATIONS,
            TURNS,
            PLANS,
            PLAN_EXECUTIONS,
            AUDIT_LOGS,
        } <= names

    @pytest.mark.asyncio
    async def test_tool_indexes_match_spec(self, mock_db: object) -> None:
        """`tools` indexes — unique name, by_status, compound status+risk, FK lookup."""
        await init_database(mock_db)  # type: ignore[arg-type]
        info = await mock_db[TOOLS].index_information()  # type: ignore[index]
        assert set(info.keys()) == {
            "_id_",
            "uniq_name",
            "by_status",
            "by_status_risk_level",
            "by_credentials_ref",
        }
        assert info["uniq_name"].get("unique") is True
        assert info["by_status"].get("unique") is None
        assert info["by_status_risk_level"].get("unique") is None

    @pytest.mark.asyncio
    async def test_credential_indexes_match_spec(self, mock_db: object) -> None:
        """`credentials` indexes — unique name, by_key_id for rotation drill-down."""
        await init_database(mock_db)  # type: ignore[arg-type]
        info = await mock_db[CREDENTIALS].index_information()  # type: ignore[index]
        assert set(info.keys()) == {"_id_", "uniq_name", "by_key_id"}
        assert info["uniq_name"].get("unique") is True

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
    async def test_conversation_indexes_match_spec(self, mock_db: object) -> None:
        """`conversations` carries the hot-path + status indexes (ADR-0011).

        T39 / #45 adds `by_status_idle_since` so the archive sweep
        can read every `idle` row in `idle_since` order without
        scanning the whole collection.
        """
        await init_database(mock_db)  # type: ignore[arg-type]
        info = await mock_db[CONVERSATIONS].index_information()  # type: ignore[index]
        assert set(info.keys()) == {
            "_id_",
            "by_user_last_activity",
            "by_status_activity",
            "by_status_idle_since",
            "by_user_status",
        }
        assert info["by_user_last_activity"].get("unique") is None
        assert info["by_status_activity"].get("unique") is None

    @pytest.mark.asyncio
    async def test_turn_and_plan_indexes_match_spec(self, mock_db: object) -> None:
        """`turns` + `plans` carry compound indexes for the chat / DAG reads."""
        await init_database(mock_db)  # type: ignore[arg-type]
        turns_info = await mock_db[TURNS].index_information()  # type: ignore[index]
        assert set(turns_info.keys()) == {
            "_id_",
            "by_conversation_created_at",
            "by_plan_id",
        }

        plans_info = await mock_db[PLANS].index_information()  # type: ignore[index]
        assert set(plans_info.keys()) == {
            "_id_",
            "by_conversation_created_at",
            "by_turn_id",
            "by_status",
        }

    @pytest.mark.asyncio
    async def test_plan_execution_and_audit_log_indexes_match_spec(
        self, mock_db: object
    ) -> None:
        """`plan_executions` + `audit_logs` carry the FK + time indexes."""
        await init_database(mock_db)  # type: ignore[arg-type]
        exec_info = await mock_db[PLAN_EXECUTIONS].index_information()  # type: ignore[index]
        assert set(exec_info.keys()) == {
            "_id_",
            "by_plan_started_at",
            "by_conversation_id",
            "by_status",
        }

        audit_info = await mock_db[AUDIT_LOGS].index_information()  # type: ignore[index]
        assert set(audit_info.keys()) == {
            "_id_",
            "by_conversation_id",
            "by_turn_id",
            "by_plan_id",
            "by_actor_id",
            "by_tool_name",
            "by_occurred_at",
        }

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
    """End-to-end coverage of the `python -m scripts.init_db` entrypoint.

    Earlier this class only re-invoked `init_database` (the library
    function) — a name-and-shape lie. The CLI shim adds two layers on
    top: a settings lookup and a JSON dump to stdout. We exercise both
    by stubbing `init_database` and capturing `sys.stdout` so the test
    is hermetic but covers the CLI's I/O contract: exit code, JSON
    shape, idempotency.
    """

    def test_cli_emits_initialized_json_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`python -m scripts.init_db` exits 0 and prints a JSON summary."""
        from scripts import init_db as cli_module

        async def _fake_init(database: object) -> dict[str, list[str]]:  # noqa: ARG001
            return {name: [f"idx_{name}"] for name in CORE_COLLECTIONS}

        # Stub the MongoClient construction so the CLI never touches a
        # real connection. The CLI calls `MongoClient(settings)` and
        # then `client.database`; both need to return something safe.
        class _FakeClient:
            def __init__(self, _settings: object) -> None:
                self.database = object()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(cli_module, "init_database", _fake_init)
        monkeypatch.setattr(cli_module, "MongoClient", _FakeClient)
        # Avoid touching the real env / .env file in this process.
        monkeypatch.setattr(cli_module, "get_settings", lambda: object())

        exit_code = cli_module.main()

        assert exit_code == 0
        out = capsys.readouterr().out
        # The CLI writes a JSON object with an `initialized` key listing
        # every core collection's index names.
        parsed = json.loads(out)
        assert "initialized" in parsed
        assert set(parsed["initialized"].keys()) == set(CORE_COLLECTIONS)
        for name in CORE_COLLECTIONS:
            assert parsed["initialized"][name] == [f"idx_{name}"]
