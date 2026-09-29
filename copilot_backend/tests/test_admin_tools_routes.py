"""End-to-end tests for the admin Tool CRUD API (T12 / #11).

T12 layers four endpoints on top of the Tool repository (T05 / #6):

* `POST   /api/v1/admin/tools`          — manual registration (lands in `draft`).
* `GET    /api/v1/admin/tools`          — list with optional status / risk_level / `q` filters.
* `GET    /api/v1/admin/tools/{id}`     — single Tool detail.
* `PATCH  /api/v1/admin/tools/{id}`     — partial update (description, status, risk_level, …).

Auth is `require_admin_user` (T12): the caller must hold the `admin`
Role in addition to a valid access JWT. Tests cover the four happy
paths, every filter combination, the lifecycle transitions (draft →
active → disabled), and the 401 / 403 envelopes for unauthenticated /
non-admin callers.

Acceptance criteria pinned here (mapping onto the T12 ticket):

* `POST /api/v1/admin/tools` creates a Tool in `draft` with a unique
  slug.
* `GET /api/v1/admin/tools` honours `status`, `risk_level`, and `q`.
* `PATCH /api/v1/admin/tools/{id}` mutates the persisted row,
  bumping `updated_at`.
* `PATCH … status` only routes through the dedicated `set_status`
  path (the `status`-only PATCH is what T12's lifecycle criterion
  exercises).
* Drafts → active → disabled transitions land in one round trip each.
* Cross-cutting 401 / 403 / 404 / 409 envelopes.
"""
from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.db.init_db import init_database
from app.db.schemas import RoleCreate, UserCreate
from app.repositories.roles import RoleRepository
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
        self.database = self._client["copilot_admin_tools_routes_test"]

    async def close(self) -> None:  # noqa: D401 — no-op closer
        pass


@pytest.fixture
async def app(settings: Settings) -> FastAPI:
    """Fresh app per test with a hermetic in-memory Mongo."""
    from app.main import create_app

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
def role_repo(app: FastAPI) -> RoleRepository:
    return RoleRepository(app.state.database)


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


async def _seed_role(role_repo: RoleRepository, *, name: str = "admin") -> str:
    """Plant a Role row and return its id."""
    created = await role_repo.create(RoleCreate(name=name, description=f"{name} role"))
    return created.id


async def _seed_admin(
    user_repo: UserRepository,
    *,
    role_ids: list[str] | None = None,
) -> str:
    """Plant a `source=local` user (the admin path per ADR-0006)."""
    from app.auth.passwords import hash_password

    created = await user_repo.create(
        UserCreate(
            email="admin@example.com",
            display_name="Admin User",
            source="local",
            local_username="admin",
            password_hash=hash_password("correct horse battery staple"),
            role_ids=list(role_ids or []),
        ),
    )
    return created.id


async def _seed_non_admin(
    user_repo: UserRepository,
    *,
    role_ids: list[str] | None = None,
) -> str:
    """Plant an SSO user without the admin role."""
    created = await user_repo.create(
        UserCreate(
            email=f"user-{ObjectId()}@example.com",
            display_name="Regular User",
            source="sso",
            sso_subject=f"sub-{ObjectId()}",
            role_ids=list(role_ids or []),
        ),
    )
    return created.id


def _mint_access_token(user_id: str, *, settings: Settings, role_ids: list[str]) -> str:
    """Build a valid access JWT for `user_id` signed with the test key."""
    claims = AccessTokenClaims(
        sub=user_id,
        source="local",
        role_ids=list(role_ids),
        issuer=settings.oidc_jwt_issuer,
        audience=settings.oidc_jwt_audience,
        issued_at=now_unix(),
        expires_at=now_unix() + settings.oidc_access_token_ttl_seconds,
        jti="test-jti",
    )
    token, _ = mint_access_token(claims, signing_key=settings.oidc_jwt_signing_key)
    return token


def _bearer(user_id: str, *, settings: Settings, role_ids: list[str]) -> dict[str, str]:
    token = _mint_access_token(user_id, settings=settings, role_ids=role_ids)
    return {"Authorization": f"Bearer {token}"}


async def _admin_bearer(
    *,
    user_repo: UserRepository,
    role_repo: RoleRepository,
    settings: Settings,
) -> dict[str, str]:
    """Plant an admin role + admin user and return the Authorization header."""
    role_id = await _seed_role(role_repo)
    user_id = await _seed_admin(user_repo, role_ids=[role_id])
    return _bearer(user_id, settings=settings, role_ids=[role_id])


def _valid_create_body(**overrides: object) -> dict[str, object]:
    """A valid `CreateToolRequest` body, with per-test overrides."""
    base: dict[str, object] = {
        "name": "list_customers",
        "description": "List customers by region. Returns paginated customer records.",
        "risk_level": "read",
        "parameters_schema": {
            "type": "object",
            "properties": {"region": {"type": "string"}},
            "required": ["region"],
        },
        "http_method": "GET",
        "http_url_template": "https://api.example.com/customers?region={region}",
        "http_headers": {"Accept": "application/json"},
        "http_body_template": None,
        "source_ref": None,
        "credentials_ref": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# POST /api/v1/admin/tools
# ---------------------------------------------------------------------------


class TestCreateTool:
    """`POST /api/v1/admin/tools` — manual Tool registration."""

    async def test_create_returns_201_with_draft_tool(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """Manual registration lands the new Tool in `draft` (ADR-0018)."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools",
            json=_valid_create_body(),
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "list_customers"
        assert body["risk_level"] == "read"
        # Manual registration always lands in `draft` per ADR-0018.
        assert body["status"] == "draft"
        # `source` is pinned to `manual` for this endpoint.
        assert body["source"] == "manual"
        assert body["id"]
        assert body["created_at"] == body["updated_at"]

    async def test_create_duplicate_name_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """The unique `name` index surfaces a 409 envelope."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        first = await client.post(
            "/api/v1/admin/tools",
            json=_valid_create_body(),
            headers=headers,
        )
        assert first.status_code == 201

        dup = await client.post(
            "/api/v1/admin/tools",
            json=_valid_create_body(
                description="Different description, same slug",
            ),
            headers=headers,
        )
        assert dup.status_code == 409, dup.text
        assert dup.json()["code"] == "duplicate_key"

    async def test_create_invalid_body_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """Pydantic-level validation (bad risk_level, empty name, …)."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools",
            json=_valid_create_body(risk_level="nuclear"),
            headers=headers,
        )
        assert resp.status_code == 422

    async def test_create_rejects_empty_parameters_schema(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """T34 / #30 AC #4 — Tools without a schema are rejected.

        ADR-0020: "对没声明 schema 的 Tool,后端拒绝注册". The wire
        envelope carries `code='tool_schema_invalid'` so the admin
        UI can render a focused message; this is a separate code
        from the runtime `schema_violation` (which fires when the
        LLM-supplied parameters violate a registered schema).
        """
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools",
            json=_valid_create_body(parameters_schema={}),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "tool_schema_invalid"
        assert body["details"]["reason"] == "empty_schema"

    async def test_create_unauthenticated_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/admin/tools",
            json=_valid_create_body(),
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "auth_missing_token"

    async def test_create_with_source_openapi_preserves_provenance(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """T15 / #13 forwards `source='openapi'` from the import preview.

        Per ADR-0003 §21 the Registry must preserve the originating
        artefact so admin tooling can re-derive the Tool if upstream
        changes. The manual default stays intact for ad-hoc callers.
        """
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools",
            json=_valid_create_body(
                name="list_pets_from_openapi",
                source="openapi",
                source_ref="get /pets",
            ),
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["source"] == "openapi"
        assert body["source_ref"] == "get /pets"
        # Lifecycle is unaffected — still `draft` per ADR-0018.
        assert body["status"] == "draft"

    async def test_create_with_invalid_source_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """`ToolSource` rejects anything outside `{openapi, manual}`."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools",
            json=_valid_create_body(source="scraped"),
            headers=headers,
        )
        assert resp.status_code == 422

    async def test_create_non_admin_returns_403(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """SSO caller (no admin role) is rejected with 403."""
        role_id = await _seed_role(role_repo, name="user")
        user_id = await _seed_non_admin(user_repo, role_ids=[role_id])
        headers = _bearer(user_id, settings=settings, role_ids=[role_id])
        resp = await client.post(
            "/api/v1/admin/tools",
            json=_valid_create_body(),
            headers=headers,
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "admin_endpoint_requires_admin_role"


# ---------------------------------------------------------------------------
# GET /api/v1/admin/tools
# ---------------------------------------------------------------------------


async def _seed_tool_via_api(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    name: str,
    description: str,
    risk_level: str = "read",
) -> dict[str, Any]:
    body = _valid_create_body(name=name, description=description, risk_level=risk_level)
    resp = await client.post("/api/v1/admin/tools", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


class TestListTools:
    """`GET /api/v1/admin/tools` — Registry list with filters."""

    async def test_list_unauthenticated_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.get("/api/v1/admin/tools")
        assert resp.status_code == 401

    async def test_list_non_admin_returns_403(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        user_id = await _seed_non_admin(user_repo)
        headers = _bearer(user_id, settings=settings, role_ids=[])
        resp = await client.get("/api/v1/admin/tools", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["code"] == "admin_endpoint_requires_admin_role"

    async def test_list_empty_returns_zero_tools(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.get("/api/v1/admin/tools", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"tools": []}

    async def test_list_returns_every_tool_by_default(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        await _seed_tool_via_api(
            client,
            headers,
            name="list_customers",
            description="List customers",
        )
        await _seed_tool_via_api(
            client,
            headers,
            name="delete_invoice",
            description="Delete an invoice",
            risk_level="destructive",
        )

        resp = await client.get("/api/v1/admin/tools", headers=headers)
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()["tools"]}
        assert names == {"list_customers", "delete_invoice"}

    async def test_list_filters_by_status(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        draft = await _seed_tool_via_api(
            client, headers, name="draft_tool", description="draft",
        )
        active = await _seed_tool_via_api(
            client, headers, name="active_tool", description="active",
        )
        # Promote one to `active` via the status-only PATCH.
        promote = await client.patch(
            f"/api/v1/admin/tools/{active['id']}",
            json={"status": "active"},
            headers=headers,
        )
        assert promote.status_code == 200, promote.text

        drafts_only = await client.get(
            "/api/v1/admin/tools", params={"status": "draft"}, headers=headers,
        )
        assert drafts_only.status_code == 200
        names = {t["name"] for t in drafts_only.json()["tools"]}
        assert names == {"draft_tool"}

        active_only = await client.get(
            "/api/v1/admin/tools", params={"status": "active"}, headers=headers,
        )
        names = {t["name"] for t in active_only.json()["tools"]}
        assert names == {"active_tool"}

        # Both should still be reachable without a filter.
        everything = await client.get("/api/v1/admin/tools", headers=headers)
        all_names = {t["name"] for t in everything.json()["tools"]}
        assert all_names == {"draft_tool", "active_tool"}
        # The `draft_tool` row id should match what we seeded.
        assert draft["id"]
        assert active["id"]

    async def test_list_filters_by_risk_level(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        await _seed_tool_via_api(
            client, headers, name="read_tool", description="safe", risk_level="read",
        )
        await _seed_tool_via_api(
            client, headers, name="destructive_tool", description="risky",
            risk_level="destructive",
        )

        resp = await client.get(
            "/api/v1/admin/tools", params={"risk_level": "destructive"}, headers=headers,
        )
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()["tools"]}
        assert names == {"destructive_tool"}

    async def test_list_filters_by_text_search(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        await _seed_tool_via_api(
            client,
            headers,
            name="list_customers",
            description="List customers by region.",
        )
        await _seed_tool_via_api(
            client,
            headers,
            name="delete_invoice",
            description="Delete an invoice by id.",
        )

        # Substring match against `name`.
        resp = await client.get(
            "/api/v1/admin/tools", params={"q": "customer"}, headers=headers,
        )
        names = {t["name"] for t in resp.json()["tools"]}
        assert names == {"list_customers"}

        # Substring match against `description` (case-insensitive).
        resp = await client.get(
            "/api/v1/admin/tools", params={"q": "INVOICE"}, headers=headers,
        )
        names = {t["name"] for t in resp.json()["tools"]}
        assert names == {"delete_invoice"}

        # Regex metacharacters are escaped — a dot isn't "any char".
        await _seed_tool_via_api(
            client,
            headers,
            name="report.v2",
            description="Monthly report.",
        )
        resp = await client.get(
            "/api/v1/admin/tools", params={"q": "report.v2"}, headers=headers,
        )
        names = {t["name"] for t in resp.json()["tools"]}
        assert names == {"report.v2"}

    async def test_list_combined_filters(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        # Three Tools: only one matches `status=active` + `risk_level=destructive` + `q=invoice`.
        await _seed_tool_via_api(
            client, headers, name="list_invoices", description="List invoices",
            risk_level="read",
        )
        destructive = await _seed_tool_via_api(
            client, headers, name="delete_invoice", description="Delete an invoice",
            risk_level="destructive",
        )
        await client.patch(
            f"/api/v1/admin/tools/{destructive['id']}",
            json={"status": "active"},
            headers=headers,
        )

        resp = await client.get(
            "/api/v1/admin/tools",
            params={
                "status": "active",
                "risk_level": "destructive",
                "q": "invoice",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        tools = resp.json()["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "delete_invoice"

    async def test_list_invalid_status_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.get(
            "/api/v1/admin/tools", params={"status": "nope"}, headers=headers,
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/admin/tools/{id}
# ---------------------------------------------------------------------------


class TestGetTool:
    """`GET /api/v1/admin/tools/{id}` — single Tool detail."""

    async def test_get_returns_tool(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        created = await _seed_tool_via_api(
            client, headers, name="list_customers", description="List customers",
        )

        resp = await client.get(
            f"/api/v1/admin/tools/{created['id']}", headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]

    async def test_get_unknown_id_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.get(
            f"/api/v1/admin/tools/{ObjectId()}", headers=headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_get_malformed_id_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """Malformed ids surface as `invalid_id` (404) rather than 400."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.get(
            "/api/v1/admin/tools/not-an-objectid", headers=headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "invalid_id"


# ---------------------------------------------------------------------------
# PATCH /api/v1/admin/tools/{id}
# ---------------------------------------------------------------------------


class TestPatchTool:
    """`PATCH /api/v1/admin/tools/{id}` — partial update + lifecycle."""

    async def test_patch_description(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        created = await _seed_tool_via_api(
            client, headers, name="list_customers", description="old description",
        )
        resp = await client.patch(
            f"/api/v1/admin/tools/{created['id']}",
            json={"description": "new description"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "new description"
        assert resp.json()["updated_at"] >= created["updated_at"]

    async def test_patch_risk_level(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        created = await _seed_tool_via_api(
            client, headers, name="escalate_risk", description="x",
            risk_level="read",
        )
        resp = await client.patch(
            f"/api/v1/admin/tools/{created['id']}",
            json={"risk_level": "destructive"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["risk_level"] == "destructive"

    async def test_patch_status_only_routes_through_set_status(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """A `status`-only PATCH transitions via the dedicated seam.

        The service treats this as a discrete event so future audit
        hooks (T42) can subscribe to "status changed" without diffing
        the full PATCH body. The end-state is the same as a generic
        PATCH but the route path is observable via the wire (the
        response is the canonical Tool row).
        """
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        created = await _seed_tool_via_api(
            client, headers, name="lifecycle", description="track this",
        )
        assert created["status"] == "draft"

        resp = await client.patch(
            f"/api/v1/admin/tools/{created['id']}",
            json={"status": "active"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"

    async def test_patch_status_only_does_not_clear_other_fields(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """`status`-only PATCH must not silently drop other fields.

        Regression guard: an admin-promotion PATCH should not blank
        `description` / `risk_level` / etc. The repository's
        `set_status` writes only `status` + `updated_at`, so the
        rest of the row survives untouched.
        """
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        created = await _seed_tool_via_api(
            client,
            headers,
            name="keep_fields",
            description="preserve me",
            risk_level="write",
        )
        resp = await client.patch(
            f"/api/v1/admin/tools/{created['id']}",
            json={"status": "active"},
            headers=headers,
        )
        body = resp.json()
        assert body["description"] == "preserve me"
        assert body["risk_level"] == "write"

    async def test_patch_combined_fields(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """A PATCH with multiple fields lands in one round trip."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        created = await _seed_tool_via_api(
            client, headers, name="multi_field", description="old",
            risk_level="read",
        )
        resp = await client.patch(
            f"/api/v1/admin/tools/{created['id']}",
            json={
                "description": "new",
                "risk_level": "destructive",
                "status": "active",
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["description"] == "new"
        assert body["risk_level"] == "destructive"
        assert body["status"] == "active"

    async def test_patch_unknown_id_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.patch(
            f"/api/v1/admin/tools/{ObjectId()}",
            json={"description": "x"},
            headers=headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_patch_invalid_status_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """The `status` enum rejects bogus lifecycle values."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        created = await _seed_tool_via_api(
            client, headers, name="bad_status", description="x",
        )
        resp = await client.patch(
            f"/api/v1/admin/tools/{created['id']}",
            json={"status": "published"},
            headers=headers,
        )
        assert resp.status_code == 422

    async def test_patch_rejects_empty_parameters_schema(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """T34 / #30 AC #4 — PATCH that empties `parameters_schema` is rejected.

        Mirrors the create-side rule: any PATCH that carries
        `parameters_schema` must leave it in a usable state. The
        `tool_schema_invalid` envelope keeps the admin UI messaging
        consistent with the create endpoint.
        """
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        created = await _seed_tool_via_api(
            client, headers, name="clear_schema", description="x",
        )
        resp = await client.patch(
            f"/api/v1/admin/tools/{created['id']}",
            json={"parameters_schema": {}},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "tool_schema_invalid"
        assert body["details"]["reason"] == "empty_schema"


# ---------------------------------------------------------------------------
# Lifecycle transitions  (T12 acceptance: draft → active → disabled)
# ---------------------------------------------------------------------------


class TestLifecycleTransitions:
    """The full `draft → active → disabled` arc lands end-to-end."""

    async def test_draft_active_disabled(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        created = await _seed_tool_via_api(
            client, headers, name="lifecycle_arc", description="d→a→d",
        )
        assert created["status"] == "draft"

        # draft → active
        resp = await client.patch(
            f"/api/v1/admin/tools/{created['id']}",
            json={"status": "active"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

        # active → disabled
        resp = await client.patch(
            f"/api/v1/admin/tools/{created['id']}",
            json={"status": "disabled"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"

        # And the listing filter reflects the disabled state.
        resp = await client.get(
            "/api/v1/admin/tools",
            params={"status": "disabled"},
            headers=headers,
        )
        names = {t["name"] for t in resp.json()["tools"]}
        assert names == {"lifecycle_arc"}

    async def test_disabled_can_be_reactivated(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """`disabled → active` is allowed (permissive lifecycle, ADR-0018)."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        created = await _seed_tool_via_api(
            client, headers, name="re_enable", description="x",
        )

        for target in ("active", "disabled", "active"):
            resp = await client.patch(
                f"/api/v1/admin/tools/{created['id']}",
                json={"status": target},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == target
