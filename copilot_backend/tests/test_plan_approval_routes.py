"""End-to-end tests for the HITL Plan approve / reject routes — T20 / #43.

T20 layers two endpoints on top of the conversation-domain service
(`ConversationService.approve_plan` / `reject_plan`):

* `POST /api/v1/conversations/{id}/plan/approve` — flip the
  conversation's latest `pending` Plan to `approved`.
* `POST /api/v1/conversations/{id}/plan/reject` — flip the
  conversation's latest `pending` Plan to `rejected`.

Per ADR-0004 the Plan preview is mandatory and one-shot: the
business user can only decide on a `pending` Plan. Already-decided
Plans raise `PlanNotPendingError` (409). Cross-user access surfaces
the same `not_found` envelope as an absent conversation, mirroring
the rest of the conversation routes.

The tests exercise the routes end-to-end against an in-memory
`mongomock_motor`, so the wire shape, the auth seam, and the
ownership guard are all verified together.
"""
from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.db.init_db import init_database
from app.db.schemas import PlanCreate, PlanNode, ToolSnapshot, TurnCreate
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
        self.database = self._client["copilot_plan_approval_routes_test"]

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


async def _seed_pending_plan(
    *,
    plan_repo: PlanRepository,
    turn_repo: TurnRepository,
    conversation_id: str,
) -> str:
    """Plant one `pending` Plan anchored to a fresh user Turn.

    Mirrors `_seed_plan` in `test_conversation_routes.py` — the
    single-node `echo` Plan is enough surface for the HITL
    approve / reject assertions, and reusing the seed shape keeps
    the test fixture pool uniform across the route suites.
    """
    turn = await turn_repo.create(
        TurnCreate(
            conversation_id=conversation_id,
            role="user",
            content="list emea customers",
        ),
    )
    plan = await plan_repo.create(
        PlanCreate(
            conversation_id=conversation_id,
            turn_id=turn.id,
            status="pending",
            nodes=[
                PlanNode(
                    node_id="n1",
                    tool="echo",
                    parameters={"text": "hello"},
                ),
            ],
            edges=[],
            tool_snapshots=[
                ToolSnapshot(
                    name="echo",
                    description="echo tool",
                    risk_level="read",
                    http_method="POST",
                    http_url_template="https://example.test/echo",
                ),
            ],
        ),
    )
    return plan.id


# ---------------------------------------------------------------------------
# POST /api/v1/conversations/{id}/plan/approve
# ---------------------------------------------------------------------------


class TestApprovePlan:
    """`POST /conversations/{id}/plan/approve` — HITL approval."""

    async def test_approve_pending_plan_returns_200_with_status(
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
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/approve",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == plan_id
        assert body["status"] == "approved"

        # The persisted row mirrors the response — no stale read.
        refreshed = await plan_repo.get(plan_id)
        assert refreshed.status == "approved"

    async def test_approve_missing_conversation_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(user_repo)
        resp = await client.post(
            f"/api/v1/conversations/{ObjectId()}/plan/approve",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_approve_cross_user_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """Cross-user access must look identical to an absent row (ADR-0002)."""
        from app.db.schemas import ConversationCreate

        alice = await _seed_sso_user(user_repo, email="alice@example.com", subject="alice")
        bob = await _seed_sso_user(user_repo, email="bob@example.com", subject="bob")
        conv = await conv_repo.create(ConversationCreate(user_id=alice, title="alice"))
        await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/approve",
            headers=_bearer(bob, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_approve_without_pending_plan_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """No Plan yet — HITL preview can't approve a Plan that doesn't exist.

        The conversation exists but has no Plan row; the latest-plan
        lookup raises `NotFoundError`, which the global error handler
        renders as the standard 404 envelope (same as an absent
        conversation).
        """
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="empty"))

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/approve",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_approve_already_approved_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """ADR-0004: the Plan preview is one-shot. Re-approving an
        already-approved Plan would rewind its audit lifecycle."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )
        await plan_repo.set_status(plan_id, "approved")

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/approve",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "plan_not_pending"
        assert body["details"]["current_status"] == "approved"
        assert body["details"]["plan_id"] == plan_id

    async def test_approve_rejected_plan_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """The two HITL decisions are mutually exclusive — approving
        a rejected Plan would contradict the user's earlier choice."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )
        await plan_repo.set_status(plan_id, "rejected")

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/approve",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "plan_not_pending"

    async def test_approve_unauthenticated_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(f"/api/v1/conversations/{ObjectId()}/plan/approve")
        assert resp.status_code == 401
        assert resp.json()["code"] == "auth_missing_token"


# ---------------------------------------------------------------------------
# POST /api/v1/conversations/{id}/plan/reject
# ---------------------------------------------------------------------------


class TestRejectPlan:
    """`POST /conversations/{id}/plan/reject` — HITL rejection."""

    async def test_reject_pending_plan_returns_200_with_status(
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
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/reject",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == plan_id
        assert body["status"] == "rejected"

        refreshed = await plan_repo.get(plan_id)
        assert refreshed.status == "rejected"

    async def test_reject_cross_user_returns_404(
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

        alice = await _seed_sso_user(user_repo, email="alice@example.com", subject="alice")
        bob = await _seed_sso_user(user_repo, email="bob@example.com", subject="bob")
        conv = await conv_repo.create(ConversationCreate(user_id=alice, title="alice"))
        await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/reject",
            headers=_bearer(bob, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_reject_already_rejected_returns_409(
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
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )
        await plan_repo.set_status(plan_id, "rejected")

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/reject",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "plan_not_pending"
        assert body["details"]["current_status"] == "rejected"

    async def test_reject_after_approve_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """Once a Plan is approved, rejecting it would contradict the
        user's earlier decision and rewind the audit lifecycle."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )
        await plan_repo.set_status(plan_id, "approved")

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/reject",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "plan_not_pending"

    async def test_reject_unauthenticated_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(f"/api/v1/conversations/{ObjectId()}/plan/reject")
        assert resp.status_code == 401
        assert resp.json()["code"] == "auth_missing_token"


__all__: list[Any] = []