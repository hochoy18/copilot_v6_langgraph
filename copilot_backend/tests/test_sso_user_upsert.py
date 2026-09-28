"""Tests for the SSO user upsert path (T08 / #46).

The repo intentionally exposes thin primitives — `get_by_sso_subject`,
`create`, `update` — and the find-or-create orchestration lives on
`OIDCLoginService._upsert_sso_user`. These tests cover that path so
a refactor that splits the lookup / mirror logic can't silently
break login.

We exercise the service directly (not via the HTTP route) so the
mirroring decision is pinned without touching the rest of the
login flow.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.auth.login import (
    OIDCLoginService,
    build_state_store,
)
from app.auth.oidc import OIDCAdapter, VerifiedIDTokenClaims
from app.auth.tokens import RefreshTokenService
from app.db.init_db import init_database
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.settings import Settings


@pytest.fixture
async def env() -> tuple[UserRepository, RefreshTokenService, Settings]:
    db = AsyncMongoMockClient()["copilot_upsert_test"]
    await init_database(db)
    settings = Settings()
    return UserRepository(db), RefreshTokenService(RefreshTokenRepository(db)), settings


def _login_service(
    *, users: UserRepository, refresh: RefreshTokenService, settings: Settings
) -> OIDCLoginService:
    adapter = OIDCAdapter(settings)
    state_store = build_state_store(settings)
    return OIDCLoginService(
        settings=settings,
        oidc_adapter=adapter,
        state_store=state_store,
        user_repository=users,
        refresh_service=refresh,
    )


def _claims(**overrides: object) -> VerifiedIDTokenClaims:
    base: dict[str, object] = {
        "sub": "okta|abc",
        "email": "alice@example.com",
        "email_verified": True,
        "name": "Alice",
        "issuer": "https://idp.test",
        "audience": "copilot-api",
        "nonce": "nonce-1",
        "expires_at": 1_700_000_000,
    }
    base.update(overrides)
    return VerifiedIDTokenClaims(**base)  # type: ignore[arg-type]


class TestSSOUserUpsert:
    """Service-level coverage for the find-or-create + mirror path."""

    @pytest.mark.asyncio
    async def test_first_call_inserts_user(
        self, env: tuple[UserRepository, RefreshTokenService, Settings]
    ) -> None:
        """A new `sub` becomes a fresh `source=sso` row."""
        users, refresh, settings = env
        svc = _login_service(users=users, refresh=refresh, settings=settings)
        user = await svc._upsert_sso_user(_claims())
        assert user.source == "sso"
        assert user.sso_subject == "okta|abc"
        assert user.email == "alice@example.com"
        assert user.display_name == "Alice"
        assert user.is_active is True
        assert user.role_ids == []

    @pytest.mark.asyncio
    async def test_repeat_call_returns_existing(
        self, env: tuple[UserRepository, RefreshTokenService, Settings]
    ) -> None:
        """A second call with the same `sub` resolves to the same row."""
        users, refresh, settings = env
        svc = _login_service(users=users, refresh=refresh, settings=settings)
        first = await svc._upsert_sso_user(_claims())
        second = await svc._upsert_sso_user(_claims())
        assert second.id == first.id
        assert second.email == first.email
        assert second.updated_at == first.updated_at

    @pytest.mark.asyncio
    async def test_email_change_is_mirrored(
        self, env: tuple[UserRepository, RefreshTokenService, Settings]
    ) -> None:
        """A different email mirrors onto the row + bumps `updated_at`."""
        users, refresh, settings = env
        svc = _login_service(users=users, refresh=refresh, settings=settings)
        first = await svc._upsert_sso_user(_claims(email="old@example.com"))
        # Sleep enough so the ms-precision `updated_at` moves.
        import asyncio
        await asyncio.sleep(0.005)
        second = await svc._upsert_sso_user(_claims(email="new@example.com"))
        assert second.id == first.id
        assert second.email == "new@example.com"
        assert second.updated_at > first.updated_at

    @pytest.mark.asyncio
    async def test_display_name_change_is_mirrored(
        self, env: tuple[UserRepository, RefreshTokenService, Settings]
    ) -> None:
        users, refresh, settings = env
        svc = _login_service(users=users, refresh=refresh, settings=settings)
        first = await svc._upsert_sso_user(_claims(name="Dan Old"))
        import asyncio
        await asyncio.sleep(0.005)
        second = await svc._upsert_sso_user(_claims(name="Dan New"))
        assert second.id == first.id
        assert second.display_name == "Dan New"
        assert second.updated_at > first.updated_at

    @pytest.mark.asyncio
    async def test_no_change_does_not_bump_updated_at(
        self, env: tuple[UserRepository, RefreshTokenService, Settings]
    ) -> None:
        users, refresh, settings = env
        svc = _login_service(users=users, refresh=refresh, settings=settings)
        first = await svc._upsert_sso_user(_claims())
        before = first.updated_at
        second = await svc._upsert_sso_user(_claims())
        assert second.updated_at == before

    @pytest.mark.asyncio
    async def test_duplicate_subject_among_distinct_emails_converges(
        self, env: tuple[UserRepository, RefreshTokenService, Settings]
    ) -> None:
        users, refresh, settings = env
        svc = _login_service(users=users, refresh=refresh, settings=settings)
        first = await svc._upsert_sso_user(_claims(email="first@example.com"))
        second = await svc._upsert_sso_user(_claims(email="second@example.com"))
        assert first.id == second.id
        assert second.email == "second@example.com"

    @pytest.mark.asyncio
    async def test_created_at_stamped_on_insert(
        self, env: tuple[UserRepository, RefreshTokenService, Settings]
    ) -> None:
        """The freshly-inserted row carries the repo's clock stamp.

        `base.utcnow()` truncates to millisecond precision (per the
        docstring there) so we compare against the same truncation
        — `datetime.utcnow()` alone has microsecond precision and
        would yield a false negative for the 1-µs that got rounded
        away.
        """
        users, refresh, settings = env
        svc = _login_service(users=users, refresh=refresh, settings=settings)
        before = datetime.utcnow().replace(microsecond=0)
        user = await svc._upsert_sso_user(_claims())
        after = datetime.utcnow().replace(microsecond=0)
        assert before - timedelta(seconds=1) <= user.created_at <= after + timedelta(seconds=1)
        assert user.updated_at == user.created_at