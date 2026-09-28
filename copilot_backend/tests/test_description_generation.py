"""End-to-end tests for T16 description generation on the import preview.

Spec seams (per docs/SPEC.md "测试 seam 是 HTTP API"): the ticket's three
acceptance criteria are asserted against
`POST /api/v1/admin/tools/import/openapi` —

* 描述从原文变 LLM-friendly 版本 → draft `description` carries the
  rewrite, `original_description` carries the raw OpenAPI text;
* 含典型用例提示 → the rewrite's text includes the 典型用例 section;
* 管理员可 review → the preview stays usable on every degradation path
  (unconfigured LLM keeps raw text + import warning; failing model
  keeps raw text + per-draft warning).

The generator seam (`get_description_generator`) is overridden with a
real `ToolDescriptionGenerator` wired to a fake `BaseChatModel` — the
HTTP behaviour under test is the route's, not the model's.
"""
from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.db.dependencies import get_description_generator
from app.db.init_db import init_database
from app.llm.prompts import PromptProvider
from app.repositories.roles import RoleRepository
from app.repositories.users import UserRepository
from app.security.jwt import AccessTokenClaims, mint_access_token, now_unix
from app.settings import Settings
from app.tools.description_generator import ToolDescriptionGenerator

# ---------------------------------------------------------------------------
# Fixtures — mirror the route-test pattern of T14 / #12
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        oidc_jwt_signing_key="internal-access-jwt-signing-key-for-tests",
        llm_base_url="https://llm.example.com/v1",
        llm_api_key="sk-test",
    )


class _AsyncMongoMockForLifespan:
    def __init__(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_description_generation_test"]

    async def close(self) -> None:  # noqa: D401 — no-op closer
        pass


@pytest.fixture
async def app(settings: Settings) -> FastAPI:
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
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeChatModel(BaseChatModel):
    """Canned-response chat model; `should_fail` exercises the degradation path."""

    response_text: str = ""
    should_fail: bool = False

    @property
    def _llm_type(self) -> str:
        return "fake-test-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.should_fail:
            raise RuntimeError("model unavailable")
        generation = ChatGeneration(message=AIMessage(content=self.response_text))
        return ChatResult(generations=[generation])


def _wire_generator(
    app: FastAPI,
    settings: Settings,
    model: BaseChatModel,
) -> None:
    """Install a generator over `model` behind the route's dependency seam."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "tool-description-generator",
                "version": 1,
                "prompt": (
                    "把 {{name}}（{{method}} {{path}}，{{description}}，"
                    "参数 {{parameters}}）改写为 JSON"
                ),
            },
        )

    provider = PromptProvider(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    generator = ToolDescriptionGenerator(
        settings=settings,
        prompt_provider=provider,
        chat_model_factory=lambda: model,
    )
    app.dependency_overrides[get_description_generator] = lambda: generator


async def _admin_headers(app: FastAPI, settings: Settings) -> dict[str, str]:
    from app.auth.passwords import hash_password
    from app.db.schemas import RoleCreate, UserCreate

    role_repo = RoleRepository(app.state.database)
    user_repo = UserRepository(app.state.database)
    role = await role_repo.create(RoleCreate(name="admin", description="admin"))
    user = await user_repo.create(
        UserCreate(
            email="admin@example.com",
            display_name="Admin",
            source="local",
            local_username="admin",
            password_hash=hash_password("x"),
            role_ids=[role.id],
        )
    )
    claims = AccessTokenClaims(
        sub=user.id,
        source="local",
        role_ids=[role.id],
        issuer=settings.oidc_jwt_issuer,
        audience=settings.oidc_jwt_audience,
        issued_at=now_unix(),
        expires_at=now_unix() + settings.oidc_access_token_ttl_seconds,
        jti="test-jti",
    )
    token, _ = mint_access_token(claims, signing_key=settings.oidc_jwt_signing_key)
    return {"Authorization": f"Bearer {token}"}


MINI_SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Mini", "version": "1.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/pets/{id}": {
            "get": {
                "operationId": "getPetById",
                "description": "Returns a user by ID. See Swagger section 4.",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
            }
        },
        "/pets": {
            "post": {
                "operationId": "addPet",
                "summary": "Add a new pet to the store",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"type": "object"}},
                    },
                },
            }
        },
    },
}

_GENERATED_JSON = (
    '{"description": "按编号查询宠物资料", '
    '"typical_use_cases": ["查一下 7 号宠物的信息", "宠物 3 的档案是什么"]}'
)


# ---------------------------------------------------------------------------
# AC #1 + #2: rewrite with use-case hints, original preserved for review
# ---------------------------------------------------------------------------


async def test_import_preview_carries_llm_generated_descriptions(
    client: Any, app: FastAPI, settings: Settings
) -> None:
    _wire_generator(app, settings, _FakeChatModel(response_text=_GENERATED_JSON))
    headers = await _admin_headers(app, settings)

    response = await client.post(
        "/api/v1/admin/tools/import/openapi",
        json={"spec": MINI_SPEC},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    drafts = response.json()["drafts"]
    assert len(drafts) == 2
    by_name = {d["name"]: d for d in drafts}

    rewritten = by_name["getPetById"]
    # AC #1 — description moved from raw text to the LLM-friendly version…
    assert rewritten["description"].startswith("按编号查询宠物资料")
    # …and the raw text is preserved so the admin can compare (AC #3).
    assert rewritten["original_description"] == "Returns a user by ID. See Swagger section 4."
    assert rewritten["description_generated"] is True

    # AC #2 — typical use cases ride in the stored text.
    assert "典型用例:" in rewritten["description"]
    assert "- 查一下 7 号宠物的信息" in rewritten["description"]

    # addPet has only a summary; the generator rewrites whatever it gets.
    add_pet = by_name["addPet"]
    assert add_pet["description_generated"] is True
    assert add_pet["original_description"] == "Add a new pet to the store"

    # No global issues on this path.
    assert response.json()["warnings"] == []


async def test_generated_draft_activates_through_manual_create_path(
    client: Any, app: FastAPI, settings: Settings
) -> None:
    """The rewritten description fits `POST /admin/tools` — the activation
    flow (T15 preview → create → activate) carries it into a persisted row."""
    _wire_generator(app, settings, _FakeChatModel(response_text=_GENERATED_JSON))
    headers = await _admin_headers(app, settings)

    preview = await client.post(
        "/api/v1/admin/tools/import/openapi",
        json={"spec": MINI_SPEC},
        headers=headers,
    )
    draft = {d["name"]: d for d in preview.json()["drafts"]}["getPetById"]

    created = await client.post(
        "/api/v1/admin/tools",
        headers=headers,
        json={
            "name": draft["name"],
            "description": draft["description"],
            "risk_level": draft["risk_level"],
            "parameters_schema": draft["parameters_schema"],
            "http_method": draft["http_method"],
            "http_url_template": draft["http_url_template"],
            "http_headers": draft["http_headers"],
            "http_body_template": draft["http_body_template"],
            "source": draft["source"],
            "source_ref": draft["source_ref"],
        },
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["description"] == draft["description"]
    assert row["status"] == "draft"  # ADR-0018: LLM output awaits review


# ---------------------------------------------------------------------------
# AC #3: degradation paths keep the preview reviewable
# ---------------------------------------------------------------------------


async def test_import_preview_warns_and_keeps_raw_when_llm_unconfigured(
    client: Any, app: FastAPI, settings: Settings
) -> None:
    """Default `Settings()` ships no LLM keys — the ADR-0003 no-silent-drop
    rule turns that into an import-level warning instead of a failure."""
    unconfigured = Settings(
        oidc_jwt_signing_key="internal-access-jwt-signing-key-for-tests"
    )
    app.dependency_overrides[get_description_generator] = lambda: ToolDescriptionGenerator(
        settings=unconfigured,
        prompt_provider=PromptProvider(
            settings=unconfigured,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _r: httpx.Response(200, json={})
                )
            ),
        ),
        chat_model_factory=lambda: _FakeChatModel(response_text=_GENERATED_JSON),
    )
    headers = await _admin_headers(app, settings)

    response = await client.post(
        "/api/v1/admin/tools/import/openapi",
        json={"spec": MINI_SPEC},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["warnings"]) == 1
    assert "not configured" in payload["warnings"][0]
    for draft in payload["drafts"]:
        assert draft["description_generated"] is False
        assert draft["original_description"] is None
    # Raw OpenAPI text survives untouched.
    raws = {d["name"]: d["description"] for d in payload["drafts"]}
    assert raws["getPetById"] == "Returns a user by ID. See Swagger section 4."


async def test_import_preview_degrades_per_draft_when_model_fails(
    client: Any, app: FastAPI, settings: Settings
) -> None:
    _wire_generator(app, settings, _FakeChatModel(should_fail=True))
    headers = await _admin_headers(app, settings)

    response = await client.post(
        "/api/v1/admin/tools/import/openapi",
        json={"spec": MINI_SPEC},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["warnings"] == []  # global parse is fine; failures are per-draft
    for draft in payload["drafts"]:
        assert draft["description_generated"] is False
        assert any("LLM" in w for w in draft["warnings"])
    assert payload["drafts"][0]["description"]  # raw text still present, reviewable
