"""End-to-end tests for the OpenAPI import endpoint (T14 / #12).

T14 ships `POST /api/v1/admin/tools/import/openapi`, the admin's
"upload a Swagger file → preview draft Tools" seam. Per ADR-0003 and
ADR-0018 the response is a *preview*, not a persisted row set — the
admin reviews the drafts, then confirms which ones to materialise
(future ticket). For T14 the acceptance criteria are:

* Spec is parsed (JSON or YAML), and every operation yields one
  draft Tool.
* Each draft carries the canonical Tool fields the manual
  registration path stamps, plus an `operation_ref` so the admin UI
  can render "GET /pets/{id}" without re-deriving it.
* Spec-level parse failures surface as a `400 openapi_parse_error`
  envelope with a message the admin can act on.
* Per-operation issues (e.g. missing `operationId`) do **not** drop
  the operation silently — they appear in the response's `warnings`
  field so the admin can rename / fix the slug before confirming.

Auth is `require_admin_user` (T12): only callers with the `admin`
Role can preview imports. Tests cover the 401 / 403 envelopes as
well as the happy paths and every error envelope.
"""
from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.db.init_db import init_database
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
        self.database = self._client["copilot_openapi_import_test"]

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
    created = await role_repo.create(RoleCreate(name=name, description=f"{name} role"))
    return created.id


async def _seed_admin(
    user_repo: UserRepository,
    *,
    role_ids: list[str] | None = None,
) -> str:
    from app.auth.passwords import hash_password
    from app.db.schemas import UserCreate

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
    from app.db.schemas import UserCreate

    created = await user_repo.create(
        UserCreate(
            email="user@example.com",
            display_name="Regular User",
            source="sso",
            sso_subject="sub-regular",
            role_ids=list(role_ids or []),
        ),
    )
    return created.id


def _mint_access_token(user_id: str, *, settings: Settings, role_ids: list[str]) -> str:
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
    role_id = await _seed_role(role_repo)
    user_id = await _seed_admin(user_repo, role_ids=[role_id])
    return _bearer(user_id, settings=settings, role_ids=[role_id])


# Re-export RoleCreate for the helper above.
from app.db.schemas import RoleCreate  # noqa: E402


# ---------------------------------------------------------------------------
# Sample OpenAPI specs
# ---------------------------------------------------------------------------


PETSTORE_SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Pet Store", "version": "1.2.0"},
    "servers": [{"url": "https://petstore.example.com/v1"}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List all pets",
                "description": "Returns a paginated list of pets, optionally filtered by tag.",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "description": "How many items to return",
                        "schema": {"type": "integer", "format": "int32"},
                    },
                    {
                        "name": "tag",
                        "in": "query",
                        "description": "Filter by tag",
                        "schema": {"type": "string"},
                    },
                ],
            },
            "post": {
                "operationId": "createPet",
                "summary": "Create a pet",
                "description": "Adds a new pet to the store.",
                "requestBody": {
                    "description": "Pet payload",
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "tag": {"type": "string"},
                                },
                                "required": ["name"],
                            },
                        },
                    },
                },
            },
        },
        "/pets/{petId}": {
            "parameters": [
                {
                    "name": "petId",
                    "in": "path",
                    "required": True,
                    "description": "ID of the pet",
                    "schema": {"type": "string"},
                },
            ],
            "get": {
                "operationId": "getPet",
                "summary": "Fetch a pet by id",
            },
            "delete": {
                "operationId": "deletePet",
                "summary": "Delete a pet by id",
                "description": "Removes a pet from the store.",
            },
        },
    },
}


# ---------------------------------------------------------------------------
# POST /api/v1/admin/tools/import/openapi
# ---------------------------------------------------------------------------


class TestImportOpenAPI:
    """`POST /api/v1/admin/tools/import/openapi` — preview draft Tools from a spec."""

    async def test_happy_path_returns_drafts(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """A multi-operation spec yields one draft per operation."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": PETSTORE_SPEC},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Metadata surfaces from the spec.
        assert body["title"] == "Pet Store"
        assert body["version"] == "1.2.0"
        assert body["server_url"] == "https://petstore.example.com/v1"
        assert body["source_format"] == "json"

        drafts = body["drafts"]
        # 4 operations: GET /pets, POST /pets, GET /pets/{petId}, DELETE /pets/{petId}
        assert len(drafts) == 4

        # The drafts are sorted by (method, path) so the admin UI renders
        # them in a predictable order across calls. The method order
        # follows `_HTTP_METHODS` — destructive methods first so the
        # admin sees the dangerous ones at the top of the preview list.
        refs = [d["operation_ref"] for d in drafts]
        assert refs == [
            "DELETE /pets/{petId}",
            "POST /pets",
            "GET /pets",
            "GET /pets/{petId}",
        ]

        # Each draft carries the canonical Tool fields a manual
        # registration would set, plus the LLM-facing slug derived
        # from `operationId`.
        names = {d["name"] for d in drafts}
        assert names == {"listPets", "createPet", "getPet", "deletePet"}

    async def test_get_operation_defaults_to_read_risk_level(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """A GET operation defaults to `risk_level='read'` per ADR-0004."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": PETSTORE_SPEC},
            headers=headers,
        )
        drafts = resp.json()["drafts"]
        list_pets = next(d for d in drafts if d["name"] == "listPets")
        assert list_pets["risk_level"] == "read"
        assert list_pets["http_method"] == "GET"

    async def test_state_mutating_operation_defaults_to_write_risk_level(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """POST/PUT/PATCH/DELETE default to `write` so they pause for HITL.

        Per ADR-0004 write operations require explicit approval; the
        default is the safe side rather than `read`. An admin can
        promote/demote after review.
        """
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": PETSTORE_SPEC},
            headers=headers,
        )
        drafts = resp.json()["drafts"]
        create_pet = next(d for d in drafts if d["name"] == "createPet")
        delete_pet = next(d for d in drafts if d["name"] == "deletePet")
        assert create_pet["risk_level"] == "write"
        assert delete_pet["risk_level"] == "write"

    async def test_draft_status_is_draft(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """Preview drafts carry `status='draft'` per ADR-0018 — LLM cannot see them."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": PETSTORE_SPEC},
            headers=headers,
        )
        for d in resp.json()["drafts"]:
            assert d["status"] == "draft"

    async def test_url_template_combines_server_and_path(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """`http_url_template` joins the first server URL with the operation path."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": PETSTORE_SPEC},
            headers=headers,
        )
        drafts = resp.json()["drafts"]
        get_pet = next(d for d in drafts if d["name"] == "getPet")
        assert (
            get_pet["http_url_template"]
            == "https://petstore.example.com/v1/pets/{petId}"
        )

    async def test_parameters_schema_includes_path_query_and_body(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """The derived JSON Schema folds in path, query, and body parameters."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": PETSTORE_SPEC},
            headers=headers,
        )
        drafts = resp.json()["drafts"]

        list_pets = next(d for d in drafts if d["name"] == "listPets")
        props = list_pets["parameters_schema"]["properties"]
        # Query params from the operation.
        assert "limit" in props
        assert "tag" in props
        # No required entries because neither query param is required.
        assert list_pets["parameters_schema"]["required"] == []

        create_pet = next(d for d in drafts if d["name"] == "createPet")
        # Body parameter is folded in as `body`.
        assert "body" in create_pet["parameters_schema"]["properties"]
        assert "body" in create_pet["parameters_schema"]["required"]

        get_pet = next(d for d in drafts if d["name"] == "getPet")
        # Path-level `petId` lands on every operation under the path.
        assert "petId" in get_pet["parameters_schema"]["properties"]
        assert "petId" in get_pet["parameters_schema"]["required"]

    async def test_missing_operationid_yields_warning(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """An operation without `operationId` still yields a draft, with a warning."""
        spec: dict[str, Any] = {
            "openapi": "3.0.0",
            "info": {"title": "Tiny", "version": "1.0.0"},
            "paths": {
                "/no-id": {
                    "get": {
                        "summary": "An operation without operationId",
                    },
                },
            },
        }
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": spec},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        drafts = resp.json()["drafts"]
        assert len(drafts) == 1
        # The draft is present (we don't silently drop), but the
        # warning flags the admin to rename before confirming.
        assert drafts[0]["operation_ref"] == "GET /no-id"
        assert drafts[0]["warnings"]
        assert any("operationId" in w for w in drafts[0]["warnings"])

    async def test_yaml_spec_is_parsed(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """A YAML spec string is parsed equivalently to JSON."""
        import yaml

        yaml_text = yaml.safe_dump(PETSTORE_SPEC, sort_keys=True)
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec_yaml": yaml_text},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source_format"] == "yaml"
        assert len(body["drafts"]) == 4

    async def test_invalid_spec_returns_openapi_parse_error(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """A malformed spec surfaces `400 openapi_parse_error` with a usable message."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        # Missing the required `openapi` version field.
        bad_spec: dict[str, Any] = {"info": {"title": "broken"}, "paths": {}}
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": bad_spec},
            headers=headers,
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "openapi_parse_error"
        assert body["message_zh"]
        assert body["message_en"]

    async def test_unsupported_version_returns_parse_error(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """An OpenAPI 2.x (Swagger) spec is rejected as unsupported."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        spec = {"swagger": "2.0", "info": {"title": "Old", "version": "1.0.0"}, "paths": {}}
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": spec},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "openapi_parse_error"

    async def test_no_source_provided_returns_422(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """Body with neither `spec` nor `spec_yaml` is a 422 (Pydantic)."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={},
            headers=headers,
        )
        assert resp.status_code == 422

    async def test_unauthenticated_returns_401(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": PETSTORE_SPEC},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "auth_missing_token"

    async def test_non_admin_returns_403(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        role_id = await _seed_role(role_repo, name="user")
        user_id = await _seed_non_admin(user_repo, role_ids=[role_id])
        headers = _bearer(user_id, settings=settings, role_ids=[role_id])
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": PETSTORE_SPEC},
            headers=headers,
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "admin_endpoint_requires_admin_role"

    async def test_empty_paths_returns_parse_error(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """A spec without any operations cannot yield drafts and is rejected."""
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Empty", "version": "1.0.0"},
            "paths": {},
        }
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": spec},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "openapi_parse_error"


# ---------------------------------------------------------------------------
# Parser unit tests — exercised directly so future changes to the
# service don't need a full HTTP round-trip to validate semantics.
# ---------------------------------------------------------------------------


class TestOpenAPIParserUnit:
    """`OpenAPIParser` direct-call tests (no FastAPI in the loop)."""

    async def test_path_level_parameters_apply_to_every_method(
        self,
    ) -> None:
        from app.tools.openapi_parser import OpenAPIParser

        spec: dict[str, Any] = {
            "openapi": "3.0.0",
            "info": {"title": "X", "version": "1"},
            "paths": {
                "/items/{id}": {
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "get": {"operationId": "fetchItem"},
                    "delete": {"operationId": "removeItem"},
                },
            },
        }
        result = OpenAPIParser().parse(spec)
        assert len(result.drafts) == 2
        for draft in result.drafts:
            assert "id" in draft.parameters_schema["properties"]
            assert "id" in draft.parameters_schema["required"]

    async def test_method_order_is_deterministic(
        self,
    ) -> None:
        """Multiple iterations produce identical draft ordering."""
        from app.tools.openapi_parser import OpenAPIParser

        parser = OpenAPIParser()
        first = parser.parse(PETSTORE_SPEC)
        second = parser.parse(PETSTORE_SPEC)
        assert [d.operation_ref for d in first.drafts] == [
            d.operation_ref for d in second.drafts
        ]

    async def test_does_not_persist_drafts(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """T14 returns *preview* drafts; the preview never inserts rows.

        After the import the admin Registry list is empty. Activation
        is the future `import/confirm` ticket's job.
        """
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": PETSTORE_SPEC},
            headers=headers,
        )
        assert resp.status_code == 200
        assert len(resp.json()["drafts"]) == 4

        # Nothing was written to Mongo.
        list_resp = await client.get("/api/v1/admin/tools", headers=headers)
        assert list_resp.status_code == 200
        assert list_resp.json()["tools"] == []

    async def test_non_json_body_surfaces_warning_not_silent_drop(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """A non-JSON request body is flagged in `warnings` rather than dropped.

        Per ADR-0003 the parser must not silently lose parts of an
        operation. The MVP can't render XML / multipart bodies, so the
        admin sees a clear "register manually" hint instead of a
        mysteriously-empty body field.
        """
        spec: dict[str, Any] = {
            "openapi": "3.0.0",
            "info": {"title": "Upload", "version": "1.0.0"},
            "paths": {
                "/upload": {
                    "post": {
                        "operationId": "uploadFile",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "multipart/form-data": {
                                    "schema": {"type": "object"},
                                },
                            },
                        },
                    },
                },
            },
        }
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": spec},
            headers=headers,
        )
        assert resp.status_code == 200
        drafts = resp.json()["drafts"]
        assert len(drafts) == 1
        warnings = drafts[0]["warnings"]
        assert any("multipart/form-data" in w for w in warnings)

    async def test_malformed_parameters_surface_warnings(
        self,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
    ) -> None:
        """`parameters: false` and non-object entries warn, don't crash."""
        spec: dict[str, Any] = {
            "openapi": "3.0.0",
            "info": {"title": "Mixed", "version": "1.0.0"},
            "paths": {
                "/items": {
                    "parameters": False,  # explicit opt-out
                    "get": {
                        "operationId": "listItems",
                        "parameters": [
                            {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                            "this is not a dict",  # invalid entry
                        ],
                    },
                },
            },
        }
        headers = await _admin_bearer(
            user_repo=user_repo,
            role_repo=role_repo,
            settings=settings,
        )
        resp = await client.post(
            "/api/v1/admin/tools/import/openapi",
            json={"spec": spec},
            headers=headers,
        )
        assert resp.status_code == 200
        drafts = resp.json()["drafts"]
        assert len(drafts) == 1
        warnings = drafts[0]["warnings"]
        # Both issues are surfaced — the admin can fix and retry.
        assert any("parameters: false" in w for w in warnings)
        assert any("not an object" in w for w in warnings)
        # The valid `limit` param still lands on the draft.
        assert "limit" in drafts[0]["parameters_schema"]["properties"]

    async def test_unknown_method_keys_are_ignored(
        self,
    ) -> None:
        """Non-HTTP keys under a path item are skipped without warnings.

        A custom key like `summary` at the path-item level is
        common in real-world specs; we just skip them silently
        because they're not operations to derive Tools from.
        """
        from app.tools.openapi_parser import OpenAPIParser

        spec: dict[str, Any] = {
            "openapi": "3.0.0",
            "info": {"title": "X", "version": "1"},
            "paths": {
                "/items": {
                    "summary": "Items resource",
                    "get": {"operationId": "listItems"},
                },
            },
        }
        result = OpenAPIParser().parse(spec)
        assert len(result.drafts) == 1
        assert result.drafts[0].operation_ref == "GET /items"
