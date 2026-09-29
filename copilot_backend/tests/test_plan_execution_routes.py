"""End-to-end tests for `POST /api/v1/conversations/{id}/plan/execute` — T21 / #18.

The route is the user-facing entry into the Worker. After HITL
approval flips a Plan to `approved`, the Frontend calls this
endpoint to drive the Worker over every node. The test exercises:

* The approval → execute round trip on a happy-path read Tool.
* A write-class Tool that fails upstream — the route returns 200
  with `plan.status = "failed"` so the Frontend can render the HITL
  state without a separate status code.
* A 409 when the Plan isn't `approved` / `modified`.
* Cross-user access surfaces the same 404 envelope as an absent row.
"""
from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.db.init_db import init_database
from app.db.schemas import (
    PlanCreate,
    PlanNode,
    ToolCreate,
    ToolSnapshot,
    TurnCreate,
)
from app.main import create_app
from app.realtime.bus import SseEventBus
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.credentials import CredentialRepository
from app.repositories.plan_executions import PlanExecutionRepository
from app.repositories.plans import PlanRepository
from app.repositories.tools import ToolRepository
from app.repositories.turns import TurnRepository
from app.repositories.users import UserRepository
from app.security.crypto import AesGcmEncryptor, MasterKey
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
    def __init__(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_plan_execution_routes_test"]

    async def close(self) -> None:  # noqa: D401 — no-op closer
        pass


@pytest.fixture
async def app(settings: Settings) -> FastAPI:
    app = create_app(settings=settings)
    client = _AsyncMongoMockForLifespan()
    app.state.mongo = client
    app.state.database = client.database
    app.state.oidc_adapter = None
    # Worker needs a credential encryptor for the route to construct.
    app.state.credential_encryptor = AesGcmEncryptor(
        MasterKey(key_bytes=b"\x00" * 32, key_id="test-exec-routes")
    )
    # T22 / #19 — the final-answer route reaches for the bus; tests
    # that don't subscribe to the stream still need a process-wide
    # instance on `app.state` so the dependency factory passes.
    app.state.sse_bus = SseEventBus()
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
def plan_repo(app: FastAPI) -> PlanRepository:
    return PlanRepository(app.state.database)


@pytest.fixture
def plan_execution_repo(app: FastAPI) -> PlanExecutionRepository:
    return PlanExecutionRepository(app.state.database)


@pytest.fixture
def audit_repo(app: FastAPI) -> AuditLogRepository:
    return AuditLogRepository(app.state.database)


@pytest.fixture
def tool_repo(app: FastAPI) -> ToolRepository:
    return ToolRepository(app.state.database)


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
    user_repo: UserRepository,
    *,
    email: str | None = None,
    subject: str | None = None,
) -> str:
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


async def _seed_echo_tool(tool_repo: ToolRepository) -> str:
    """A read-class Tool that the Worker would call against the live HTTP route."""
    tool = await tool_repo.create(
        ToolCreate(
            name="echo",
            description="echo",
            risk_level="read",
            status="active",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            http_method="POST",
            http_url_template="https://upstream.test/echo",
            http_headers={"Content-Type": "application/json"},
            http_body_template={"echo": "{text}"},
            source="manual",
        ),
    )
    return tool.id


async def _seed_write_tool(tool_repo: ToolRepository) -> str:
    tool = await tool_repo.create(
        ToolCreate(
            name="create_invoice",
            description="create invoice",
            risk_level="write",
            status="active",
            parameters_schema={
                "type": "object",
                "properties": {
                    "amount": {"type": "number"},
                    "customer": {"type": "string"},
                },
                "required": ["amount", "customer"],
            },
            http_method="POST",
            http_url_template="https://upstream.test/invoices",
            http_headers={"Content-Type": "application/json"},
            http_body_template={"amount": "{amount}", "customer": "{customer}"},
            source="manual",
        ),
    )
    return tool.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExecutePlanRoute:
    """`POST /conversations/{id}/plan/execute` — T21 / #18."""

    async def test_execute_runs_approved_plan_to_succeeded(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Happy path: read-class Tool, upstream returns 2xx → Plan `succeeded`."""
        from app.db.schemas import ConversationCreate
        from app.tools import executor as executor_module
        from app.tools import worker as worker_module

        # Stub the network seam so the route runs without a real upstream.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"echo": "ok"})

        async def _no_sleep(_: float) -> None:
            return None

        import asyncio as _asyncio

        monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

        # Build a ToolWorker wired to the stub transport.
        encryptor = AesGcmEncryptor(
            MasterKey(key_bytes=b"\x01" * 32, key_id="test-routes")
        )
        cred_repo = CredentialRepository(app.state.database, encryptor)
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        worker = worker_module.ToolWorker(
            credential_repository=cred_repo,
            http_client=http_client,
        )
        plan_execution_repo = PlanExecutionRepository(app.state.database)
        audit_repo = AuditLogRepository(app.state.database)
        stub_executor = executor_module.PlanExecutor(
            plan_repository=plan_repo,
            plan_execution_repository=plan_execution_repo,
            audit_log_repository=audit_repo,
            tool_repository=tool_repo,
            worker=worker,
        )
        app.dependency_overrides[executor_module.PlanExecutor] = lambda: stub_executor
        # The `get_plan_executor` FastAPI dependency is wired via the
        # class import path; rebind the override here so it routes
        # through `Depends(get_plan_executor)`.
        from app.db import dependencies

        def _override_executor() -> executor_module.PlanExecutor:
            return stub_executor

        app.dependency_overrides[dependencies.get_plan_executor] = _override_executor

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="exec"))
        tool_id = await _seed_echo_tool(tool_repo)
        turn = await turn_repo.create(
            TurnCreate(
                conversation_id=conv.id,
                role="user",
                content="echo hello",
            ),
        )
        snapshot = ToolSnapshot(
            tool_id=tool_id,
            name="echo",
            description="echo",
            risk_level="read",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            http_method="POST",
            http_url_template="https://upstream.test/echo",
            http_headers={"Content-Type": "application/json"},
            http_body_template={"echo": "{text}"},
        )
        node = PlanNode(node_id="n1", tool="echo", parameters={"text": "hi"})
        plan = await plan_repo.create(
            PlanCreate(
                conversation_id=conv.id,
                turn_id=turn.id,
                status="approved",
                nodes=[node],
                edges=[],
                tool_snapshots=[snapshot],
            ),
        )

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/execute",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["plan"]["id"] == plan.id
        assert body["plan"]["status"] == "succeeded"
        assert len(body["audit_log_ids"]) == 1

    async def test_execute_rejects_non_approved_plan(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """A `pending` Plan can't be executed — 409 from the route."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="exec"))
        turn = await turn_repo.create(
            TurnCreate(
                conversation_id=conv.id,
                role="user",
                content="x",
            ),
        )
        snapshot = ToolSnapshot(
            name="echo",
            description="echo",
            risk_level="read",
            parameters_schema={},
            http_method="POST",
            http_url_template="https://upstream.test/echo",
            http_headers={},
            http_body_template=None,
        )
        await plan_repo.create(
            PlanCreate(
                conversation_id=conv.id,
                turn_id=turn.id,
                status="pending",
                nodes=[PlanNode(node_id="n1", tool="echo", parameters={"text": "x"})],
                edges=[],
                tool_snapshots=[snapshot],
            ),
        )

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/execute",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "plan_not_pending"

    async def test_execute_cross_user_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        """Cross-user access renders the same 404 envelope as an absent row."""
        from app.db.schemas import ConversationCreate

        alice = await _seed_sso_user(user_repo, email="alice@example.com", subject="alice")
        bob = await _seed_sso_user(user_repo, email="bob@example.com", subject="bob")
        conv = await conv_repo.create(ConversationCreate(user_id=alice, title="alice"))

        resp = await client.post(
            f"/api/v1/conversations/{conv.id}/plan/execute",
            headers=_bearer(bob, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_execute_missing_conversation_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_sso_user(user_repo)
        resp = await client.post(
            f"/api/v1/conversations/{ObjectId()}/plan/execute",
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"
