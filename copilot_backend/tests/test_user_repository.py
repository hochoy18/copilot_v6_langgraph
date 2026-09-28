"""Tests for `UserRepository`.

Acceptance criterion for T04 (#5): "repository 读写 User 测试通过".
The seam is the public API of `UserRepository` — we exercise every
method against an in-memory `mongomock_motor` database and verify the
shapes, the error contracts, and the canonical-read-shape invariant
(`password_hash` never leaves the repository).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.db.errors import DuplicateKeyError, InvalidIdError, NotFoundError, ValidationError
from app.db.init_db import init_database
from app.db.schemas import UserCreate, UserUpdate
from app.repositories.users import UserRepository


@pytest.fixture
async def repo() -> UserRepository:
    """A fresh `UserRepository` against an isolated in-memory DB.

    Each test gets its own DB instance — mongomock state is per-client,
    so this avoids cross-test bleed.
    """
    db = AsyncMongoMockClient()["copilot_user_test"]
    await init_database(db)
    return UserRepository(db)


def _sso_input(**overrides: object) -> UserCreate:
    """A valid SSO user input, with per-test overrides applied."""
    base: dict[str, object] = {
        "email": "alice@example.com",
        "display_name": "Alice",
        "source": "sso",
        "sso_subject": "okta|abc-123",
        "is_active": True,
        "role_ids": [],
    }
    base.update(overrides)
    return UserCreate(**base)  # type: ignore[arg-type]


def _local_input(**overrides: object) -> UserCreate:
    """A valid local admin user input."""
    base: dict[str, object] = {
        "email": "admin@example.com",
        "display_name": "Admin",
        "source": "local",
        "local_username": "admin",
        "password_hash": "argon2id$v=19$m=65536,t=3,p=4$...$...",
        "is_active": True,
        "role_ids": [],
    }
    base.update(overrides)
    return UserCreate(**base)  # type: ignore[arg-type]


class TestUserCreate:
    """`create` — happy path + validation + duplicate-key handling."""

    @pytest.mark.asyncio
    async def test_create_sso_user_returns_canonical_shape(self, repo: UserRepository) -> None:
        """SSO users get a canonical `User` back, no `password_hash` field."""
        created = await repo.create(_sso_input())
        assert created.email == "alice@example.com"
        assert created.source == "sso"
        assert created.sso_subject == "okta|abc-123"
        assert created.id  # MongoDB ObjectId as string
        assert ObjectId(created.id)  # parses as ObjectId
        assert created.is_active is True
        # Canonical read shape — password_hash MUST NOT be present.
        assert "password_hash" not in created.model_dump()
        # Timestamps stamped by the repository.
        assert isinstance(created.created_at, datetime)
        assert created.created_at == created.updated_at

    @pytest.mark.asyncio
    async def test_create_local_user_strips_password_hash_from_read(
        self, repo: UserRepository
    ) -> None:
        """Local users get a row, but `password_hash` never leaks into `User`."""
        created = await repo.create(_local_input())
        # The persisted row keeps the hash.
        in_db = await repo.get_in_db(created.id)
        assert in_db.password_hash is not None
        # The canonical read shape does NOT.
        fresh = await repo.get(created.id)
        assert "password_hash" not in fresh.model_dump()

    @pytest.mark.asyncio
    async def test_sso_user_must_have_sso_subject(self, repo: UserRepository) -> None:
        """An SSO user without `sso_subject` is rejected before insert."""
        with pytest.raises(ValidationError) as exc:
            await repo.create(_sso_input(sso_subject=None))
        assert exc.value.code == "validation_error"

    @pytest.mark.asyncio
    async def test_sso_user_must_not_have_local_credentials(
        self, repo: UserRepository
    ) -> None:
        """An SSO user carrying `local_username` is rejected."""
        with pytest.raises(ValidationError):
            await repo.create(_sso_input(local_username="hacker", password_hash="x"))

    @pytest.mark.asyncio
    async def test_local_user_must_have_username_and_hash(
        self, repo: UserRepository
    ) -> None:
        """A local user without `local_username` is rejected."""
        with pytest.raises(ValidationError):
            await repo.create(_local_input(local_username=None))
        with pytest.raises(ValidationError):
            await repo.create(_local_input(password_hash=None))

    @pytest.mark.asyncio
    async def test_duplicate_email_raises_duplicate_key_error(
        self, repo: UserRepository
    ) -> None:
        """A second user with the same email fails the unique index."""
        await repo.create(_sso_input(email="dup@example.com"))
        with pytest.raises(DuplicateKeyError) as exc:
            await repo.create(_local_input(email="dup@example.com"))
        # The duplicate-key envelope surfaces the offending index.
        assert exc.value.code == "duplicate_key"
        assert exc.value.details is not None
        assert "index" in exc.value.details

    @pytest.mark.asyncio
    async def test_duplicate_sso_subject_raises_duplicate_key_error(
        self, repo: UserRepository
    ) -> None:
        """Two SSO users with the same `sso_subject` collide on the sparse-unique index."""
        await repo.create(_sso_input(sso_subject="okta|shared"))
        with pytest.raises(DuplicateKeyError):
            await repo.create(
                _sso_input(email="other@example.com", sso_subject="okta|shared")
            )

    @pytest.mark.asyncio
    async def test_local_user_without_sso_subject_does_not_collide(
        self, repo: UserRepository
    ) -> None:
        """Sparse-unique lets multiple local users (no SSO subject) coexist."""
        await repo.create(_local_input(local_username="admin1", email="a1@example.com"))
        # Second local user — `sso_subject` is None, but sparse-unique
        # skips None-to-None collisions.
        await repo.create(_local_input(local_username="admin2", email="a2@example.com"))


class TestUserRead:
    """`get`, `get_in_db`, `get_by_email`, `get_by_sso_subject`, etc."""

    @pytest.mark.asyncio
    async def test_get_by_id_returns_user(self, repo: UserRepository) -> None:
        created = await repo.create(_sso_input())
        fetched = await repo.get(created.id)
        assert fetched.id == created.id
        assert fetched.email == created.email

    @pytest.mark.asyncio
    async def test_get_missing_id_raises_not_found(self, repo: UserRepository) -> None:
        with pytest.raises(NotFoundError):
            await repo.get(str(ObjectId()))

    @pytest.mark.asyncio
    async def test_get_with_invalid_id_raises_invalid_id(self, repo: UserRepository) -> None:
        """Garbage ids surface as `InvalidIdError` (code=`invalid_id`), not a 500."""
        with pytest.raises(InvalidIdError) as exc:
            await repo.get("not-an-objectid")
        assert exc.value.code == "invalid_id"

    @pytest.mark.asyncio
    async def test_get_by_email_returns_user(self, repo: UserRepository) -> None:
        await repo.create(_sso_input(email="findme@example.com"))
        fetched = await repo.get_by_email("findme@example.com")
        assert fetched.email == "findme@example.com"

    @pytest.mark.asyncio
    async def test_get_by_email_missing_raises_not_found(self, repo: UserRepository) -> None:
        with pytest.raises(NotFoundError):
            await repo.get_by_email("nobody@example.com")

    @pytest.mark.asyncio
    async def test_get_by_sso_subject(self, repo: UserRepository) -> None:
        await repo.create(_sso_input(sso_subject="okta|known"))
        fetched = await repo.get_by_sso_subject("okta|known")
        assert fetched.sso_subject == "okta|known"

    @pytest.mark.asyncio
    async def test_get_by_local_username(self, repo: UserRepository) -> None:
        await repo.create(_local_input(local_username="root"))
        fetched = await repo.get_by_local_username("root")
        assert fetched.local_username == "root"


class TestUserList:
    """Cursor-paginated reads."""

    @pytest.mark.asyncio
    async def test_list_returns_all_users(self, repo: UserRepository) -> None:
        for i in range(3):
            await repo.create(_sso_input(email=f"u{i}@example.com", sso_subject=f"subj{i}"))
        users = await repo.list_users()
        assert {u.email for u in users} == {
            "u0@example.com",
            "u1@example.com",
            "u2@example.com",
        }

    @pytest.mark.asyncio
    async def test_list_pagination_after_id(self, repo: UserRepository) -> None:
        """`after_id` cursor returns rows whose `_id` is strictly greater."""
        created = []
        for i in range(5):
            created.append(
                await repo.create(
                    _sso_input(email=f"p{i}@example.com", sso_subject=f"subj{i}")
                )
            )
        page1 = await repo.list_users(limit=2)
        assert len(page1) == 2
        # Cursor on the last id of page 1.
        page2 = await repo.list_users(limit=2, after_id=page1[-1].id)
        assert len(page2) == 2
        # No overlap, strictly increasing ids.
        assert page1[-1].id != page2[0].id
        assert page1[-1].id < page2[0].id

    @pytest.mark.asyncio
    async def test_list_rejects_out_of_range_limit(self, repo: UserRepository) -> None:
        with pytest.raises(ValidationError):
            await repo.list_users(limit=0)
        with pytest.raises(ValidationError):
            await repo.list_users(limit=10_000)


class TestUserUpdate:
    """`update` + `set_role_ids`."""

    @pytest.mark.asyncio
    async def test_update_changes_display_name_and_bumps_updated_at(
        self, repo: UserRepository
    ) -> None:
        created = await repo.create(_sso_input())
        before = created.updated_at
        # Ensure monotonic clock difference.
        import asyncio

        await asyncio.sleep(0.005)
        updated = await repo.update(
            created.id, UserUpdate(display_name="Alice Updated")
        )
        assert updated.display_name == "Alice Updated"
        assert updated.updated_at > before
        assert updated.created_at == created.created_at  # not touched

    @pytest.mark.asyncio
    async def test_update_missing_user_raises_not_found(self, repo: UserRepository) -> None:
        with pytest.raises(NotFoundError):
            await repo.update(str(ObjectId()), UserUpdate(display_name="x"))

    @pytest.mark.asyncio
    async def test_set_role_ids_replaces_list(self, repo: UserRepository) -> None:
        created = await repo.create(_sso_input())
        role_a = str(ObjectId())
        role_b = str(ObjectId())
        updated = await repo.set_role_ids(created.id, [role_a, role_b])
        assert updated.role_ids == [role_a, role_b]
        # Replacing again is atomic — the old list isn't merged.
        updated2 = await repo.set_role_ids(created.id, [role_a])
        assert updated2.role_ids == [role_a]


class TestUserDelete:
    """`delete` is hard; soft-delete lives elsewhere."""

    @pytest.mark.asyncio
    async def test_delete_removes_user(self, repo: UserRepository) -> None:
        created = await repo.create(_sso_input())
        await repo.delete(created.id)
        with pytest.raises(NotFoundError):
            await repo.get(created.id)

    @pytest.mark.asyncio
    async def test_delete_missing_user_raises_not_found(self, repo: UserRepository) -> None:
        with pytest.raises(NotFoundError):
            await repo.delete(str(ObjectId()))


class TestUserCountAndLastUpdated:
    """`count` and `last_updated` — small admin helpers."""

    @pytest.mark.asyncio
    async def test_count_total(self, repo: UserRepository) -> None:
        assert await repo.count() == 0
        await repo.create(_sso_input())
        await repo.create(_sso_input(email="x@example.com", sso_subject="okta|x"))
        assert await repo.count() == 2

    @pytest.mark.asyncio
    async def test_count_active_filter(self, repo: UserRepository) -> None:
        await repo.create(_sso_input(is_active=True))
        await repo.create(
            _sso_input(
                email="inactive@example.com",
                sso_subject="okta|off",
                is_active=False,
            )
        )
        assert await repo.count(is_active=True) == 1
        assert await repo.count(is_active=False) == 1

    @pytest.mark.asyncio
    async def test_last_updated_returns_datetime(self, repo: UserRepository) -> None:
        created = await repo.create(_sso_input())
        last = await repo.last_updated(created.id)
        assert isinstance(last, datetime)

    @pytest.mark.asyncio
    async def test_last_updated_returns_none_for_missing(self, repo: UserRepository) -> None:
        assert await repo.last_updated(str(ObjectId())) is None


class TestRefreshTokenShape:
    """Acceptance: refresh_tokens has token_hash + user_id + revoked_at.

    Pin the shape via the persisted collection (not just the Pydantic
    model) — the schema is what /healthz and T07 will rely on.
    """

    @pytest.mark.asyncio
    async def test_refresh_token_persists_required_fields(self) -> None:
        """The persisted refresh_token document carries the required fields.

        T07 (#8) adds `family_id` to the schema; the four required
        fields persisted today are `token_hash`, `user_id`,
        `family_id`, `expires_at`.
        """
        from app.db.schemas import RefreshTokenCreate
        from app.repositories.refresh_tokens import RefreshTokenRepository

        db = AsyncMongoMockClient()["copilot_rt_test"]
        await init_database(db)
        rt_repo = RefreshTokenRepository(db)

        user_repo = UserRepository(db)
        user = await user_repo.create(_sso_input())

        expires = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=7)
        token = await rt_repo.create(
            RefreshTokenCreate(
                token_hash="a" * 64,
                user_id=user.id,
                family_id="0c0a4f48-dead-beef-cafe-000000000001",
                expires_at=expires,
            )
        )

        # All four required fields are on the persisted row.
        assert token.token_hash == "a" * 64
        assert token.user_id == user.id
        assert token.family_id == "0c0a4f48-dead-beef-cafe-000000000001"
        assert token.revoked_at is None  # fresh tokens are unrevoked

        # And on the raw Mongo doc too.
        raw = await db[rt_repo.collection_name].find_one({"_id": ObjectId(token.id)})
        assert raw is not None
        assert raw["token_hash"] == "a" * 64
        assert raw["user_id"] == user.id
        assert raw["family_id"] == "0c0a4f48-dead-beef-cafe-000000000001"
        assert "revoked_at" in raw

    @pytest.mark.asyncio
    async def test_refresh_token_revoke_stamps_revoked_at(self) -> None:
        """Revoking a token sets `revoked_at` to a non-null datetime."""
        from app.db.schemas import RefreshTokenCreate
        from app.repositories.refresh_tokens import RefreshTokenRepository

        db = AsyncMongoMockClient()["copilot_rt_test"]
        await init_database(db)
        rt_repo = RefreshTokenRepository(db)
        user_repo = UserRepository(db)
        user = await user_repo.create(_sso_input())

        token = await rt_repo.create(
            RefreshTokenCreate(
                token_hash="b" * 64,
                user_id=user.id,
                family_id="0c0a4f48-dead-beef-cafe-000000000002",
                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=7),
            )
        )
        assert token.revoked_at is None
        revoked = await rt_repo.revoke(token.token_hash)
        assert revoked.revoked_at is not None
