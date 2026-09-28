"""Tests for `UserRepository.upsert_sso_user` (T08 / #46).

The repo exposes a narrow "find-or-create + mirror" surface so the
OIDC login flow doesn't have to coordinate reads + writes itself.
Tests cover:

* Insert path — first time we see `sso_subject`, a row lands.
* Re-fetch path — second login with the same `sub` returns the
  existing row.
* Identity-mirror path — when the IdP says the user's email/name
  changed, the local row is updated and `updated_at` advances.
* Validation — empty `sso_subject` is rejected.

The user-repository acceptance criteria are in
`tests/test_user_repository.py`; this file is the additive surface
for the T08 SSO flow.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.db.errors import ValidationError
from app.db.init_db import init_database
from app.repositories.users import UserRepository


@pytest.fixture
async def repo() -> UserRepository:
    db = AsyncMongoMockClient()["copilot_upsert_test"]
    await init_database(db)
    return UserRepository(db)


class TestUpsertSSOUser:
    """Find-or-create on `sso_subject` + identity mirror."""

    @pytest.mark.asyncio
    async def test_first_call_inserts_user(self, repo: UserRepository) -> None:
        """A new `sso_subject` becomes a fresh `source=sso` row."""
        user, created = await repo.upsert_sso_user(
            sso_subject="okta|abc",
            email="alice@example.com",
            display_name="Alice",
        )
        assert created is True
        assert user.source == "sso"
        assert user.sso_subject == "okta|abc"
        assert user.email == "alice@example.com"
        assert user.display_name == "Alice"
        assert user.is_active is True
        assert user.role_ids == []

    @pytest.mark.asyncio
    async def test_repeat_call_returns_existing(
        self, repo: UserRepository
    ) -> None:
        """A second login with the same `sub` resolves to the same row."""
        first, _ = await repo.upsert_sso_user(
            sso_subject="okta|shared",
            email="bob@example.com",
            display_name="Bob",
        )
        second, created = await repo.upsert_sso_user(
            sso_subject="okta|shared",
            email="bob@example.com",
            display_name="Bob",
        )
        assert created is False
        assert second.id == first.id
        assert second.email == first.email

    @pytest.mark.asyncio
    async def test_email_change_is_mirrored(self, repo: UserRepository) -> None:
        """A different `email` from the IdP updates the row."""
        first, _ = await repo.upsert_sso_user(
            sso_subject="okta|email-change",
            email="old@example.com",
            display_name="Carol",
        )
        before_updated_at = first.updated_at

        # Sleep enough so updated_at can move forward (millisecond precision).
        import asyncio
        await asyncio.sleep(0.005)

        second, created = await repo.upsert_sso_user(
            sso_subject="okta|email-change",
            email="new@example.com",
            display_name="Carol",
        )
        assert created is False
        assert second.id == first.id
        assert second.email == "new@example.com"
        assert second.updated_at > before_updated_at

    @pytest.mark.asyncio
    async def test_display_name_change_is_mirrored(
        self, repo: UserRepository
    ) -> None:
        """A different `display_name` updates the row + bumps `updated_at`."""
        first, _ = await repo.upsert_sso_user(
            sso_subject="okta|name-change",
            email="dan@example.com",
            display_name="Dan Old",
        )
        import asyncio
        await asyncio.sleep(0.005)

        second, created = await repo.upsert_sso_user(
            sso_subject="okta|name-change",
            email="dan@example.com",
            display_name="Dan New",
        )
        assert created is False
        assert second.display_name == "Dan New"
        assert second.updated_at > first.updated_at

    @pytest.mark.asyncio
    async def test_no_change_does_not_bump_updated_at(
        self, repo: UserRepository
    ) -> None:
        """When identity claims are unchanged, no DB write is needed."""
        first, _ = await repo.upsert_sso_user(
            sso_subject="okta|same",
            email="eve@example.com",
            display_name="Eve",
        )
        before = first.updated_at

        second, created = await repo.upsert_sso_user(
            sso_subject="okta|same",
            email="eve@example.com",
            display_name="Eve",
        )
        assert created is False
        assert second.updated_at == before

    @pytest.mark.asyncio
    async def test_duplicate_subject_among_distinct_emails_creates(
        self, repo: UserRepository
    ) -> None:
        """Two different emails with the same `sub` map to one row.

        The IdP can change a user's email at any time. Two calls
        with `sso_subject="okta|x"` and different `email` values
        should converge on one row whose email follows the latest
        call.
        """
        first, _ = await repo.upsert_sso_user(
            sso_subject="okta|x",
            email="first@example.com",
            display_name="Frank",
        )
        second, _ = await repo.upsert_sso_user(
            sso_subject="okta|x",
            email="second@example.com",
            display_name="Frank",
        )
        assert first.id == second.id
        assert second.email == "second@example.com"

    @pytest.mark.asyncio
    async def test_empty_sso_subject_raises(self, repo: UserRepository) -> None:
        """`sso_subject` is required; empty values are rejected."""
        with pytest.raises(ValidationError):
            await repo.upsert_sso_user(
                sso_subject="",
                email="ghost@example.com",
                display_name="Ghost",
            )

    @pytest.mark.asyncio
    async def test_created_at_stamped_on_insert(
        self, repo: UserRepository
    ) -> None:
        """The freshly-inserted row carries the repo's clock stamp.

        `base.utcnow()` truncates to millisecond precision (per the
        docstring there) so we compare against the same truncation
        — `datetime.utcnow()` alone has microsecond precision and
        would yield a false negative for the 1-µs that got rounded
        away.
        """
        before = datetime.utcnow().replace(microsecond=0)
        user, _ = await repo.upsert_sso_user(
            sso_subject="okta|stamped",
            email="hank@example.com",
            display_name="Hank",
        )
        after = datetime.utcnow().replace(microsecond=0)
        # `created_at` is millisecond-resolution; ±1s slop for clock drift.
        assert before - timedelta(seconds=1) <= user.created_at <= after + timedelta(seconds=1)
        assert user.updated_at == user.created_at