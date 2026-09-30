"""End-to-end tests for `POST /api/v1/conversations/{id}/reactivate` (T39 / #45).

Verifies the wire shape, status codes, and ownership guard at the
HTTP layer. The service-layer logic is covered by
`test_conversation_reactivate.py` — this file pins down the API
contract the Frontend will lean on.
"""
from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.db.init_db import init_database
from app.db.schemas import ConversationCreate, TurnCreate
from app.main import create_app
from app.repositories.conversations import ConversationRepository
from app.repositories.turns import TurnRepository
from app.repositories.users import UserRepository
from app.security.jwt import AccessTokenClaims, mint_access_token, now_unix
from app.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        oidc_issuer_url="https://test.example.com",
        oidc_id_token_signing_key="test-idp-hs256-key",
        oidc_access_token_ttl_seconds=900,
        oidc_state_ttl_seconds=600,
        oidc_discovery_cache_seconds=3600,
        oidc_jwt_signing_key="internal-access-jwt-signing-key-for-tests",
    )


class _AsyncMongoMockForLifespan:
    def __init__(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_reactivate_route_test"]

    async def close(self) -> None:
        pass


@pytest.fixture
async def app(settings: Settings) -> FastAPI:
    app = create_app(settings=settings)
    client = _AsyncMongoMockForLifespan()
    app.state.mongo = client
    app.state.database = client.database
    app.state.oidc_adapter = None
    await init_database(app.state.database)
    return app


@pytest.fixture(autouse=True)
def _override_settings(app: FastAPI, settings: Settings) -> Generator[None, None, None]:
    from app.settings import get_settings

    app.dependency_overrides[get_settings] = lambda: settings
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def user_repo(app: FastAPI) -> UserRepository:
    return UserRepository(app.state.database)


@pytest.fixture
def conv_repo(app: FastAPI) -> ConversationRepository:
    return ConversationRepository(app.state.database)


@pytest.fixture
def turn_repo(app: FastAPI) -> TurnRepository:
    return TurnRepository(app.state.database)


@pytest.fixture
async def client(app: FastAPI) -> Any:
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_sso_user(
    user_repo: UserRepository, *, email: str, subject: str
) -> str:
    from app.db.schemas import UserCreate

    created = await user_repo.create(
        UserCreate(
            email=email,
            display_name=email.split("@")[0],
            source="sso",
            sso_subject=subject,
        ),
    )
    return created.id


def _mint_token(user_id: str, *, settings: Settings) -> str:
    claims = AccessTokenClaims(
        sub=user_id,
        source="sso",
        role_ids=[],
        issuer=settings.oidc_jwt_issuer,
        audience=settings.oidc_jwt_audience,
        issued_at=now_unix(),
        expires_at=now_unix() + settings.oidc_access_token_ttl_seconds,
        jti="test-jti",
    )
    token, _ = mint_access_token(claims, signing_key=settings.oidc_jwt_signing_key)
    return token


def _bearer(user_id: str, *, settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {_mint_token(user_id, settings=settings)}"}


async def _seed_archived_conversation(
    *,
    conv_repo: ConversationRepository,
    turn_repo: TurnRepository,
    user_id: str,
    turn_count: int = 0,
) -> str:
    """Plant an archived conversation with optional turns; returns its id."""
    conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="archived"))
    await conv_repo.set_status(conv.id, "archived")
    for i in range(turn_count):
        await turn_repo.create(
            TurnCreate(
                conversation_id=conv.id,
                role="user",
                content=f"hello {i}",
            ),
        )
    return conv.id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestReactivateConversationRoute:
    """`POST /api/v1/conversations/{id}/reactivate` — the Frontend seam."""

    async def test_reactivate_returns_201_with_new_conversation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(
            user_repo, email="alice@example.com", subject="alice"
        )
        source_id = await _seed_archived_conversation(
            conv_repo=conv_repo,
            turn_repo=turn_repo,
            user_id=user_id,
            turn_count=2,
        )

        resp = await client.post(
            f"/api/v1/conversations/{source_id}/reactivate",
            json={"title": "resumed"},
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["conversation"]["status"] == "active"
        assert body["conversation"]["title"] == "resumed"
        assert body["conversation"]["user_id"] == user_id
        assert body["conversation"]["reactivated_from_id"] == source_id
        assert body["conversation"]["reactivate_count"] == 1
        assert body["source_conversation_id"] == source_id
        assert len(body["copied_turn_ids"]) == 2
        assert body["copied_plan_id"] is None

    async def test_reactivate_without_body_uses_empty_title(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(
            user_repo, email="alice@example.com", subject="alice"
        )
        source_id = await _seed_archived_conversation(
            conv_repo=conv_repo, turn_repo=turn_repo, user_id=user_id
        )

        resp = await client.post(
            f"/api/v1/conversations/{source_id}/reactivate",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 201
        assert resp.json()["conversation"]["title"] == ""


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestReactivateConversationFailure:
    """Auth, ownership, and state guard at the wire layer."""

    async def test_reactivate_unauthenticated_returns_401(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            f"/api/v1/conversations/{ObjectId()}/reactivate"
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "auth_missing_token"

    async def test_reactivate_cross_user_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        settings: Settings,
    ) -> None:
        alice = await _seed_sso_user(
            user_repo, email="alice@example.com", subject="alice"
        )
        bob = await _seed_sso_user(
            user_repo, email="bob@example.com", subject="bob"
        )
        bob_conv = await _seed_archived_conversation(
            conv_repo=conv_repo, turn_repo=turn_repo, user_id=bob
        )

        resp = await client.post(
            f"/api/v1/conversations/{bob_conv}/reactivate",
            headers=_bearer(alice, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_reactivate_missing_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(
            user_repo, email="alice@example.com", subject="alice"
        )
        resp = await client.post(
            f"/api/v1/conversations/{ObjectId()}/reactivate",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_reactivate_non_archived_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(
            user_repo, email="alice@example.com", subject="alice"
        )
        # Active source — reactivate is archived-only.
        conv = await conv_repo.create(
            ConversationCreate(user_id=user_id, title="active")
        )

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/reactivate",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conversation_not_archived"

    async def test_reactivate_idle_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(
            user_repo, email="alice@example.com", subject="alice"
        )
        conv = await conv_repo.create(
            ConversationCreate(user_id=user_id, title="idle")
        )
        await conv_repo.set_status(conv.id, "idle")

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/reactivate",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conversation_not_archived"