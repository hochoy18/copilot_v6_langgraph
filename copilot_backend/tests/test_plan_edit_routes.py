"""End-to-end tests for `PATCH /api/v1/conversations/{id}/plan` — T26 / #44.

T26 ships the HITL Plan-edit endpoint described by ADR-0019 and
ADR-0027:

* **可改** — per-node `parameters` and `notes`.
* **不可改** — node set, node ↔ Tool binding, edges, tool_snapshots.

The route enforces the contract via the repository's
`PlanRepository.record_edit` (which raises `ValidationError` for
add/remove or repoint attempts) and writes an `audit_logs` row
capturing the diff so ADR-0019's "编辑后的 Plan 仍要进审计日志"
stays satisfiable without a separate `plan_edits` collection.

Acceptance criteria for T26:

* PATCH 改 param 接受 — happy path returns 200 + status `modified`.
* 不可增删节点 — added / removed / replaced nodes return 400.
* 审计含 diff — an `audit_logs` row is appended with the diff in
  the `response` field.
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
from app.repositories.audit_logs import AuditLogRepository
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
        self.database = self._client["copilot_plan_edit_routes_test"]

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
def _override_settings(
    app: FastAPI, settings: Settings
) -> Generator[None, None, None]:
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
def audit_repo(app: FastAPI) -> AuditLogRepository:
    return AuditLogRepository(app.state.database)


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
    plan_repo: PlanRepository,
    turn_repo: TurnRepository,
    *,
    conversation_id: str,
    parameters: dict[str, Any] | None = None,
    notes: str = "Q3 emea lookups",
    snapshot_name: str = "list_customers",
) -> str:
    """Plant one `pending` Plan anchored to a fresh user Turn.

    Mirrors `_seed_pending_plan` in `test_plan_approval_routes.py` so
    the route-test fixture pool stays uniform. The default snapshot
    uses a `list_customers`-shaped JSON Schema so a PATCH that
    mutates `parameters` is naturally accepted by the frozen
    `parameters_schema` (the route does not re-validate against the
    snapshot — that is the Worker's job at execution time, ADR-0020).
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
                    tool=snapshot_name,
                    parameters=parameters or {"region": "emea"},
                    notes=notes,
                ),
            ],
            edges=[],
            tool_snapshots=[
                ToolSnapshot(
                    name=snapshot_name,
                    description="List customers by region.",
                    risk_level="read",
                    parameters_schema={
                        "type": "object",
                        "properties": {"region": {"type": "string"}},
                    },
                    http_method="GET",
                    http_url_template="https://api.example.com/customers",
                ),
            ],
        ),
    )
    return plan.id


# ---------------------------------------------------------------------------
# PATCH /api/v1/conversations/{id}/plan
# ---------------------------------------------------------------------------


class TestEditPlan:
    """`PATCH /conversations/{id}/plan` — T26 / #44 / ADR-0019."""

    async def test_edit_parameters_returns_200_with_modified_status(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """Happy path: business user flips a single `parameters` value
        and the Plan comes back as `modified` with the new value
        persisted. Edges and tool_snapshots survive untouched
        (ADR-0019 / ADR-0027)."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )

        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "list_customers",
                        "parameters": {"region": "amer"},
                        "notes": "Q3 emea lookups",
                    }
                ]
            },
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == plan_id
        assert body["status"] == "modified"
        assert body["nodes"][0]["parameters"] == {"region": "amer"}
        # Frozen topology stays put.
        assert body["edges"] == []
        assert [s["name"] for s in body["tool_snapshots"]] == ["list_customers"]

        # The persisted row mirrors the response — no stale read.
        refreshed = await plan_repo.get(plan_id)
        assert refreshed.status == "modified"
        assert refreshed.nodes[0].parameters == {"region": "amer"}
        assert refreshed.edited_diff is not None
        assert "by_node_id" in refreshed.edited_diff

    async def test_edit_notes_returns_200(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """`notes` (Planner's LLM-facing annotation) is editable too
        (ADR-0019). This test pins the seam so a future "no notes
        edits" regression lands here."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )

        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "list_customers",
                        "parameters": {"region": "emea"},
                        "notes": "Finance refined scope.",
                    }
                ]
            },
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "modified"
        assert body["nodes"][0]["notes"] == "Finance refined scope."

    async def test_edit_writes_audit_log_with_diff(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        audit_repo: AuditLogRepository,
        settings: Settings,
    ) -> None:
        """ADR-0019: the edit lands in `audit_logs` with the diff so a
        later auditor can replay "before → after". The audit row's
        `tool_name` is `plan.edit` to keep it distinguishable from
        Tool-call rows (T42's UI surfaces the discriminator)."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )

        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "list_customers",
                        "parameters": {"region": "amer"},
                        "notes": "Q3 emea lookups",
                    }
                ]
            },
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200, resp.text

        rows = await audit_repo.list_by_conversation(conv.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.tool_name == "plan.edit"
        assert row.plan_id == plan_id
        assert row.conversation_id == conv.id
        assert row.actor_id == user_id
        assert row.status == "succeeded"
        # Plan-edit audit rows have no execution yet — `response`
        # carries the diff so the auditor sees "what changed" without
        # needing a second collection.
        assert row.response is not None
        diff = row.response
        assert "by_node_id" in diff
        assert "n1" in diff["by_node_id"]
        per_node = diff["by_node_id"]["n1"]
        # The diff captures the parameter that changed (and only it).
        assert "parameters.region" in per_node
        assert per_node["parameters.region"]["before"] == "emea"
        assert per_node["parameters.region"]["after"] == "amer"

    async def test_edit_rejects_added_node(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """ADR-0019: 不可增删节点. A PATCH carrying an extra `node_id`
        fails the repo's set-equality check and surfaces as 400; the
        persisted Plan is untouched."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )

        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "list_customers",
                        "parameters": {"region": "emea"},
                        "notes": "",
                    },
                    {
                        "node_id": "n9",
                        "tool": "list_customers",
                        "parameters": {"region": "emea"},
                        "notes": "",
                    },
                ]
            },
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "validation_error"
        # Persisted Plan untouched.
        refreshed = await plan_repo.get(plan_id)
        assert [n.node_id for n in refreshed.nodes] == ["n1"]
        assert refreshed.status == "pending"

    async def test_edit_rejects_removed_node(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """ADR-0019: empty node list fails the set-equality check the
        same way an added node does."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )

        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={"nodes": []},
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "validation_error"
        refreshed = await plan_repo.get(plan_id)
        assert refreshed.status == "pending"

    async def test_edit_rejects_repointed_tool(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """ADR-0019 + ADR-0027: a node's `tool` cannot be re-pointed
        to a different snapshot. The frozen binding is the whole
        point of having snapshots — flipping it would invalidate the
        audit replay."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )

        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "send_email",
                        "parameters": {"region": "emea"},
                        "notes": "",
                    }
                ]
            },
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "validation_error"
        refreshed = await plan_repo.get(plan_id)
        assert refreshed.nodes[0].tool == "list_customers"
        assert refreshed.status == "pending"

    async def test_edit_missing_conversation_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        """Cross-user / absent-conversation both surface as 404 (ADR-0002)."""
        user_id = await _seed_sso_user(user_repo)
        resp = await client.patch(
            f"/api/v1/conversations/{ObjectId()}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "list_customers",
                        "parameters": {"region": "emea"},
                        "notes": "",
                    }
                ]
            },
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_edit_cross_user_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """A stranger probing an owned conversation sees the same
        envelope as an absent one (ADR-0002 / privacy)."""
        from app.db.schemas import ConversationCreate

        alice = await _seed_sso_user(user_repo, email="alice@example.com", subject="alice")
        bob = await _seed_sso_user(user_repo, email="bob@example.com", subject="bob")
        conv = await conv_repo.create(ConversationCreate(user_id=alice, title="alice"))
        await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )

        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "list_customers",
                        "parameters": {"region": "emea"},
                        "notes": "",
                    }
                ]
            },
            headers=_bearer(bob, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_edit_without_pending_plan_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        """No Plan row → the latest-plan lookup raises `NotFoundError`,
        which the global error handler renders as 404 (same envelope
        as an absent conversation)."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="empty"))

        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "list_customers",
                        "parameters": {"region": "emea"},
                        "notes": "",
                    }
                ]
            },
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_edit_already_approved_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """A Plan already approved / rejected / executing is terminal
        for HITL purposes — re-editing would contradict the user's
        earlier decision and rewind the audit lifecycle (same
        `PlanNotPendingError` envelope as `approve_plan`)."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )
        await plan_repo.set_status(plan_id, "approved")

        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "list_customers",
                        "parameters": {"region": "emea"},
                        "notes": "",
                    }
                ]
            },
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "plan_not_pending"
        assert body["details"]["current_status"] == "approved"

    async def test_edit_modified_plan_allowed(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        settings: Settings,
    ) -> None:
        """Re-editing a `modified` Plan is allowed — the user may iterate
        parameters before approving. Per ADR-0019 the result is still
        `modified`, not double-counted."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        plan_id = await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
        )
        # First edit flips status to `modified`.
        await plan_repo.set_status(plan_id, "modified")

        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "list_customers",
                        "parameters": {"region": "apac"},
                        "notes": "",
                    }
                ]
            },
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "modified"
        assert resp.json()["nodes"][0]["parameters"] == {"region": "apac"}

    async def test_edit_unauthenticated_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.patch(
            f"/api/v1/conversations/{ObjectId()}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "list_customers",
                        "parameters": {"region": "emea"},
                        "notes": "",
                    }
                ]
            },
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "auth_missing_token"

    async def test_edit_invalid_body_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        """Body validation lands at the wire seam — missing required
        fields surface as 422 (Pydantic's standard envelope)."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))

        # `nodes` field missing entirely.
        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={},
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 422

    async def test_edit_audit_diff_records_parameter_removal(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        audit_repo: AuditLogRepository,
        settings: Settings,
    ) -> None:
        """A parameter that was present on the original Plan and
        dropped from the edit must surface in the diff as
        `after: null` (ADR-0019: 修改前 → 修改后). The
        `_compute_edit_diff_safe` helper iterates the union of
        keys; a regression that only walks `edited.parameters`
        silently swallows the removal and breaks the auditor's
        replay."""
        from app.db.schemas import ConversationCreate

        user_id = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="hitl"))
        await _seed_pending_plan(
            plan_repo=plan_repo,
            turn_repo=turn_repo,
            conversation_id=conv.id,
            parameters={"region": "emea", "limit": 100},
        )

        resp = await client.patch(
            f"/api/v1/conversations/{conv.id}/plan",
            json={
                "nodes": [
                    {
                        "node_id": "n1",
                        "tool": "list_customers",
                        # `limit` removed intentionally.
                        "parameters": {"region": "emea"},
                        "notes": "Q3 emea lookups",
                    }
                ]
            },
            headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 200, resp.text

        rows = await audit_repo.list_by_conversation(conv.id)
        assert len(rows) == 1
        assert rows[0].response is not None
        per_node = rows[0].response["by_node_id"]["n1"]
        assert per_node["parameters.limit"]["before"] == 100
        assert per_node["parameters.limit"]["after"] is None


__all__: list[Any] = []
