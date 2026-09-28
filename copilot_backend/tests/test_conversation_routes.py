"""End-to-end tests for the conversation CRUD API (T10 / #40).

T10 layers four endpoints on top of the conversation-domain
repositories (T06 / #7):

* `POST   /api/v1/conversations`                  — create.
* `GET    /api/v1/conversations`                  — list (with `status` filter).
* `GET    /api/v1/conversations/{id}`             — detail (turns + plans).
* `POST   /api/v1/conversations/{id}/archive`     — manual archive → idle.

The router delegates everything to `ConversationService`; this file
exercises the routes end-to-end against an in-memory `mongomock_motor`
so the wire shape, the auth seam, and the ownership guard are all
verified together.

Acceptance criteria pinned here (mapping onto the T10 ticket):

* POST creates a conversation owned by the caller as `active`.
* GET list supports the `status` query param and excludes other
  users' conversations.
* GET detail returns the conversation + turns + plans.
* POST archive transitions `active` / `idle` → `idle` and is a no-op
  on `archived`.
* Cross-user access renders the same `not_found` envelope as absent
  rows.
"""
from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.db.init_db import init_database
from app.db.schemas import PlanCreate, PlanNode, PlanNodeToolSnapshot, TurnCreate
from app.main import create_app
from app.repositories.conversations import ConversationRepository
from app.repositories.plans import PlanRepository
from app.repositories.turns import TurnRepository
from app.repositories.users import UserRepository
from app.security.jwt import AccessTokenClaims, mint_access_token, now_unix
from app.settings import Settings

# ---------------------------------------------------------------------------
# Settings + fixtures
# ---------------------------------------------------------------------------


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
    """A minimal stand-in for `MongoClient` that the lifespan can close."""

    def __init__(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_conversation_routes_test"]

    async def close(self) -> None:  # noqa: D401 — no-op closer
        pass


@pytest.fixture
async def app(settings: Settings) -> FastAPI:
    """Fresh app per test with a hermetic in-memory Mongo."""
    app = create_app(settings=settings)
    client = _AsyncMongoMockForLifespan()
    app.state.mongo = client
    app.state.database = client.database
    app.state.oidc_adapter = None
    await init_database(app.state.database)
    return app


@pytest.fixture(autouse=True)
def _override_settings(app: FastAPI, settings: Settings) -> Generator[None, None, None]:
    """Override `get_settings` so `get_current_user` decodes with the test signing key."""
    from app.settings import get_settings

    app.dependency_overrides[get_settings] = lambda: settings
    yield
    app.dependency_overrides.clear()


# Repository fixtures — built against the in-memory mock Mongo so tests
# can both seed and re-read rows in one go.
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
def plan_repo(app: FastAPI) -> PlanRepository:
    return PlanRepository(app.state.database)


@pytest.fixture
async def client(app: FastAPI) -> Any:
    """Async HTTP client wired directly to the ASGI app (no network)."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_sso_user(
    user_repo: UserRepository,
    *,
    email: str | None = None,
    subject: str | None = None,
) -> str:
    """Plant an SSO user (so `get_current_user` decodes via the OIDC path)."""
    from app.db.schemas import UserCreate

    email = email or f"user-{subject}@example.com"
    subject = subject or f"sub-{ObjectId()}"
    created = await user_repo.create(
        UserCreate(
            email=email,
            display_name=email.split("@")[0],
            source="sso",
            sso_subject=subject,
        ),
    )
    return created.id


def _mint_access_token(user_id: str, *, settings: Settings) -> str:
    """Build a valid access JWT for `user_id` signed with the test key."""
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
    return {"Authorization": f"Bearer {_mint_access_token(user_id, settings=settings)}"}


async def _seed_turn(
    *,
    turn_repo: TurnRepository,
    conversation_id: str,
    content: str,
    role: str = "user",
) -> str:
    created = await turn_repo.create(
        TurnCreate(
            conversation_id=conversation_id,
            role=role,  # type: ignore[arg-type]
            content=content,
        ),
    )
    return created.id


async def _seed_plan(
    *,
    plan_repo: PlanRepository,
    conversation_id: str,
    turn_id: str,
) -> str:
    created = await plan_repo.create(
        PlanCreate(
            conversation_id=conversation_id,
            turn_id=turn_id,
            status="pending",
            nodes=[
                PlanNode(
                    node_id="n1",
                    tool_snapshot=PlanNodeToolSnapshot(
                        name="echo",
                        description="echo tool",
                        risk_level="read",
                        http_method="POST",
                        http_url_template="https://example.test/echo",
                    ),
                    parameters={"text": "hello"},
                ),
            ],
        ),
    )
    return created.id


# ---------------------------------------------------------------------------
# POST /api/v1/conversations
# ---------------------------------------------------------------------------


class TestCreateConversation:
    """`POST /api/v1/conversations` — the start of a session."""

    async def test_create_returns_201_with_active_conversation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(user_repo)
        resp = await client.post(
            "/api/v1/conversations",
            json={"title": "Q3 invoices"},
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == user_id
        assert body["status"] == "active"
        assert body["title"] == "Q3 invoices"
        assert body["id"]
        # last_activity_at equals created_at on a fresh row.
        assert body["last_activity_at"] == body["created_at"]

    async def test_create_without_body_uses_empty_title(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        """The Frontend's "new conversation" button posts an empty body."""
        user_id = await _seed_sso_user(user_repo)
        resp = await client.post(
            "/api/v1/conversations",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["title"] == ""

    async def test_create_unauthenticated_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/conversations",
            json={"title": "x"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "auth_missing_token"

    async def test_create_bad_token_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/conversations",
            json={"title": "x"},
            headers={"Authorization": "Bearer not.a.jwt"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "auth_invalid_token"

    async def test_create_title_too_long_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(user_repo)
        resp = await client.post(
            "/api/v1/conversations",
            json={"title": "x" * 257},
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/conversations
# ---------------------------------------------------------------------------


class TestListConversations:
    """`GET /api/v1/conversations` — Frontend's three-tab view."""

    async def test_list_returns_only_callers_conversations(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        alice = await _seed_sso_user(user_repo, email="alice@example.com", subject="alice")
        bob = await _seed_sso_user(user_repo, email="bob@example.com", subject="bob")
        from app.db.schemas import ConversationCreate

        await conv_repo.create(ConversationCreate(user_id=alice, title="alice-1"))
        await conv_repo.create(ConversationCreate(user_id=alice, title="alice-2"))
        await conv_repo.create(ConversationCreate(user_id=bob, title="bob-1"))

        resp = await client.get(
            "/api/v1/conversations",
            headers=_bearer(alice, settings=settings),
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()["conversations"]
        assert {r["title"] for r in rows} == {"alice-1", "alice-2"}
        assert {r["user_id"] for r in rows} == {alice}

    async def test_list_with_status_filter(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        active = await conv_repo.create(ConversationCreate(user_id=user_id, title="A"))
        await conv_repo.create(ConversationCreate(user_id=user_id, title="B"))
        await conv_repo.set_status(active.id, "idle")

        active_resp = await client.get(
            "/api/v1/conversations",
            params={"status": "active"},
            headers=_bearer(user_id, settings=settings),
        )
        assert active_resp.status_code == 200
        assert {r["title"] for r in active_resp.json()["conversations"]} == {"B"}

        idle_resp = await client.get(
            "/api/v1/conversations",
            params={"status": "idle"},
            headers=_bearer(user_id, settings=settings),
        )
        assert {r["title"] for r in idle_resp.json()["conversations"]} == {"A"}

    async def test_list_with_invalid_status_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(user_repo)
        resp = await client.get(
            "/api/v1/conversations",
            params={"status": "bogus"},
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 422

    async def test_list_unauthenticated_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.get("/api/v1/conversations")
        assert resp.status_code == 401

    async def test_list_empty(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(user_repo)
        resp = await client.get(
            "/api/v1/conversations",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200
        assert resp.json() == {"conversations": []}


# ---------------------------------------------------------------------------
# GET /api/v1/conversations/{id}
# ---------------------------------------------------------------------------


class TestGetConversationDetail:
    """`GET /api/v1/conversations/{id}` — composed session read."""

    async def test_detail_returns_conversation_turns_and_plans(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="composed"))
        turn_id = await _seed_turn(
            turn_repo=turn_repo,
            conversation_id=conv.id,
            content="echo hello",
        )
        await _seed_plan(
            plan_repo=plan_repo,
            conversation_id=conv.id,
            turn_id=turn_id,
        )

        resp = await client.get(
            f"/api/v1/conversations/{conv.id}",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["conversation"]["id"] == conv.id
        assert body["conversation"]["title"] == "composed"
        assert len(body["turns"]) == 1
        assert body["turns"][0]["content"] == "echo hello"
        assert body["turns"][0]["role"] == "user"
        assert len(body["plans"]) == 1
        # The embedded tool_snapshot survives the round trip (ADR-0027).
        assert body["plans"][0]["nodes"][0]["tool_snapshot"]["name"] == "echo"

    async def test_detail_missing_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(user_repo)
        missing = str(ObjectId())
        resp = await client.get(
            f"/api/v1/conversations/{missing}",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_detail_cross_user_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        """A stranger probing another user's conversation sees the same
        404 envelope as an absent row — existence must not be leakable.
        """
        from app.db.schemas import ConversationCreate

        alice = await _seed_sso_user(user_repo, email="alice@example.com", subject="alice")
        bob = await _seed_sso_user(user_repo, email="bob@example.com", subject="bob")
        bob_conv = await conv_repo.create(ConversationCreate(user_id=bob, title="bob-priv"))

        resp = await client.get(
            f"/api/v1/conversations/{bob_conv.id}",
            headers=_bearer(alice, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_detail_invalid_id_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(user_repo)
        resp = await client.get(
            "/api/v1/conversations/not-an-objectid",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "invalid_id"

    async def test_detail_unauthenticated_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.get(f"/api/v1/conversations/{ObjectId()}")
        assert resp.status_code == 401

    async def test_detail_with_no_turns_or_plans(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="empty"))
        resp = await client.get(
            f"/api/v1/conversations/{conv.id}",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["turns"] == []
        assert body["plans"] == []


# ---------------------------------------------------------------------------
# POST /api/v1/conversations/{id}/archive
# ---------------------------------------------------------------------------


class TestArchiveConversation:
    """`POST /api/v1/conversations/{id}/archive` — manual end → idle."""

    async def test_archive_transitions_active_to_idle(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="active"))

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/archive",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "idle"

        # The persisted row also reflects the transition.
        refreshed = await conv_repo.get(conv.id)
        assert refreshed.status == "idle"

    async def test_archive_idle_is_a_no_op(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id))
        await conv_repo.set_status(conv.id, "idle")

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/archive",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "idle"

    async def test_archive_archived_stays_archived(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        """Per ADR-0011 the sweep job (T39) is the only path that takes
        a row from `idle` to `archived`. Re-archiving an `archived` row
        is a no-op rather than a state-machine error.
        """
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id))
        await conv_repo.set_status(conv.id, "archived")

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/archive",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

    async def test_archive_missing_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(user_repo)
        resp = await client.post(
            f"/api/v1/conversations/{ObjectId()}/archive",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_archive_cross_user_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        from app.db.schemas import ConversationCreate

        alice = await _seed_sso_user(user_repo, email="alice@example.com", subject="alice")
        bob = await _seed_sso_user(user_repo, email="bob@example.com", subject="bob")
        bob_conv = await conv_repo.create(ConversationCreate(user_id=bob, title="bob-priv"))

        resp = await client.post(
            f"/api/v1/conversations/{bob_conv.id}/archive",
            headers=_bearer(alice, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"
        # Confirm Bob's row was not mutated.
        refreshed = await conv_repo.get(bob_conv.id)
        assert refreshed.status == "active"

    async def test_archive_unauthenticated_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(f"/api/v1/conversations/{ObjectId()}/archive")
        assert resp.status_code == 401


__all__: list[Any] = []