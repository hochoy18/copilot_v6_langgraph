"""End-to-end tests for `POST /api/v1/conversations/{id}/turns` — T18 / #16.

The test seam is the HTTP API (SPEC §测试 Decisions). The ticket's
acceptance criteria pin here:

* AC #1 — 输入 echo hello 返回 1 节点 Plan: the seeded registry's
  `echo` Tool plus a fake ChatModel answering the `planner` JSON
  contract yields a 201 whose `plan.nodes` has exactly one node.
* AC #2 — Plan 含 tool_snapshots: the response's Plan doc carries the
  ADR-0027 frozen snapshot of `echo`, and the T10 detail endpoint
  serves the same doc back.
* AC #3 — 用 Langfuse planner prompt: the fake ChatModel records the
  prompt text — it must contain the Tool catalog and the user
  instruction rendered through the (Langfuse-fetched) template.
* AC #4 — 持久化到 plans: `GET /conversations/{id}` lists the Plan,
  proving it landed in the collection and not just the response.

Degradation paths assert the shape too: LLM failures still create
the Turn, answer 201, and say why in `warnings` — never a silent
empty plan. Auth (401), cross-user (404), archived (409) and body
validation (422) round out the contract.

Fixture scaffolding mirrors `test_conversation_routes.py`.
"""
from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.db.dependencies import get_tool_planner
from app.db.init_db import init_database
from app.db.schemas import ToolCreate, UserCreate
from app.llm.prompts import PLANNER_PROMPT, PromptProvider
from app.main import create_app
from app.planner.planner import ToolPlanner
from app.repositories.conversations import ConversationRepository
from app.repositories.tools import ToolRepository
from app.repositories.users import UserRepository
from app.security.jwt import AccessTokenClaims, mint_access_token, now_unix
from app.settings import Settings

_PLANNER_TEMPLATE = "CATALOG>>{{tools}}<<INSTRUCTION>>{{input}}<<"
_ECHO_PLAN_JSON = (
    '{"nodes": [{"tool": "echo", "parameters": {"text": "hello"}, '
    '"notes": "回显 hello"}]}'
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        oidc_jwt_signing_key="internal-access-jwt-signing-key-for-tests",
        # LLM configured: `PlannerService.ready` is True, so routes
        # actually call the (fake) model.
        llm_base_url="https://llm.example.com/v1",
        llm_api_key="sk-test",
    )


class _AsyncMongoMockForLifespan:
    def __init__(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_turn_routes_test"]

    async def close(self) -> None:  # noqa: D401 — no-op closer
        pass


class _FakeChatModel(BaseChatModel):
    response_text: str = _ECHO_PLAN_JSON
    should_fail: bool = False
    seen_prompts: list[str] = []
    call_count: int = 0

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
        self.call_count += 1
        self.seen_prompts.append(str(messages[-1].content))
        if self.should_fail:
            raise RuntimeError("upstream model exploded")
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.response_text))]
        )


@pytest.fixture
def fake_model() -> _FakeChatModel:
    return _FakeChatModel()


@pytest.fixture
def planner(settings: Settings, fake_model: _FakeChatModel) -> ToolPlanner:
    """Planner wired to a mock-Langfuse (serving the template) + fake model."""
    provider = PromptProvider(
        settings=Settings(
            langfuse_host="https://langfuse.example.com",
            langfuse_public_key="pk-lf-test",
            langfuse_secret_key="sk-lf-test",
        ),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"name": PLANNER_PROMPT, "version": 1, "prompt": _PLANNER_TEMPLATE},
                )
            )
        ),
    )
    return ToolPlanner(
        settings=settings,
        prompt_provider=provider,
        chat_model_factory=lambda: fake_model,
    )


@pytest.fixture
async def app(
    settings: Settings,
    planner: ToolPlanner,
) -> FastAPI:
    app = create_app(settings=settings)
    client = _AsyncMongoMockForLifespan()
    app.state.mongo = client
    app.state.database = client.database
    app.state.oidc_adapter = None
    await init_database(app.state.database)
    # Canonical seam swap (same pattern T16 uses for the description
    # generator): the route runs for real, the model does not.
    app.dependency_overrides[get_tool_planner] = lambda: planner
    return app


@pytest.fixture(autouse=True)
def _override_settings(app: FastAPI, settings: Settings) -> Generator[None, None, None]:
    """Override `get_settings` so `get_current_user` decodes with the test signing key.

    The `app` fixture already registered the `get_tool_planner`
    override; this one adds `get_settings` on top (same dict, no
    clear) so both stay live for the test body. Teardown clears the
    whole map — the app is per-test anyway.
    """
    from app.settings import get_settings

    app.dependency_overrides[get_settings] = lambda: settings
    yield
    app.dependency_overrides.clear()


@pytest.fixture
async def client(app: FastAPI) -> Any:
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_user(app: FastAPI) -> str:
    repo = UserRepository(app.state.database)
    created = await repo.create(
        UserCreate(
            email=f"user-{ObjectId()}@example.com",
            display_name="tester",
            source="sso",
            sso_subject=f"sub-{ObjectId()}",
        )
    )
    return created.id


def _bearer(user_id: str, *, settings: Settings) -> dict[str, str]:
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
    return {"Authorization": f"Bearer {token}"}


async def _seed_conversation(app: FastAPI, user_id: str, *, status: str = "active") -> str:
    from app.db.schemas import ConversationCreate

    repo = ConversationRepository(app.state.database)
    created = await repo.create(
        ConversationCreate(user_id=user_id, title="", status=status),  # type: ignore[arg-type]
    )
    return created.id


async def _seed_active_echo(app: FastAPI) -> str:
    repo = ToolRepository(app.state.database)
    created = await repo.create(
        ToolCreate(
            name="echo",
            description="把传入的文本原样返回",
            risk_level="read",
            status="active",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要回显的文本"}},
                "required": ["text"],
            },
            http_method="POST",
            http_url_template="https://api.example.test/echo",
            http_headers={},
            http_body_template=None,
            source="manual",
            source_ref=None,
        )
    )
    return created.id


async def _submit(
    client: Any,
    conversation_id: str,
    *,
    headers: dict[str, str],
    content: str = "echo hello",
) -> Any:
    return await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"content": content},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# AC tests — the happy path through the wire
# ---------------------------------------------------------------------------


class TestSubmitTurnHappyPath:
    async def test_echo_hello_returns_single_node_plan(
        self,
        client: Any,
        app: FastAPI,
        settings: Settings,
        fake_model: _FakeChatModel,
    ) -> None:
        """AC #1: 输入 echo hello → 1 节点 Plan; AC #2: 含 tool_snapshots."""
        user_id = await _seed_user(app)
        conv_id = await _seed_conversation(app, user_id)
        tool_id = await _seed_active_echo(app)

        resp = await _submit(
            client, conv_id, headers=_bearer(user_id, settings=settings),
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["warnings"] == []

        turn = body["turn"]
        assert turn["role"] == "user"
        assert turn["content"] == "echo hello"
        assert turn["conversation_id"] == conv_id

        plan = body["plan"]
        assert plan is not None
        assert plan["status"] == "pending"  # HITL preview gate, ADR-0004
        assert plan["conversation_id"] == conv_id
        assert plan["turn_id"] == turn["id"]
        assert len(plan["nodes"]) == 1
        node = plan["nodes"][0]
        assert node["node_id"] == "n1"
        assert node["tool"] == "echo"
        assert node["parameters"] == {"text": "hello"}
        assert node["notes"] == "回显 hello"
        assert plan["edges"] == []

        snapshots = plan["tool_snapshots"]
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap["tool_id"] == tool_id
        assert snap["name"] == "echo"
        assert snap["risk_level"] == "read"
        assert snap["http_method"] == "POST"

        # Turn links back to the Plan.
        assert turn["plan_id"] == plan["id"]
        assert fake_model.call_count == 1

    async def test_planner_prompt_carries_catalog_and_instruction(
        self,
        client: Any,
        app: FastAPI,
        settings: Settings,
        fake_model: _FakeChatModel,
    ) -> None:
        """AC #3: the Langfuse `planner` template was fetched and rendered."""
        user_id = await _seed_user(app)
        conv_id = await _seed_conversation(app, user_id)
        await _seed_active_echo(app)

        await _submit(client, conv_id, headers=_bearer(user_id, settings=settings))

        prompt = fake_model.seen_prompts[0]
        assert prompt.startswith("CATALOG>>")  # Langfuse-served template
        assert "echo" in prompt  # active Tool catalog
        assert "把传入的文本原样返回" in prompt
        assert "INSTRUCTION>>echo hello<<" in prompt

    async def test_plan_is_persisted_and_served_by_detail(
        self,
        client: Any,
        app: FastAPI,
        settings: Settings,
    ) -> None:
        """AC #4: 持久化到 plans — read it back through the T10 endpoint."""
        user_id = await _seed_user(app)
        conv_id = await _seed_conversation(app, user_id)
        await _seed_active_echo(app)

        submit = await _submit(client, conv_id, headers=_bearer(user_id, settings=settings))
        plan_id = submit.json()["plan"]["id"]

        detail = await client.get(
            f"/api/v1/conversations/{conv_id}",
            headers=_bearer(user_id, settings=settings),
        )
        assert detail.status_code == 200
        plans = detail.json()["plans"]
        assert [p["id"] for p in plans] == [plan_id]
        assert plans[0]["tool_snapshots"][0]["name"] == "echo"
        turns = detail.json()["turns"]
        assert turns[0]["plan_id"] == plan_id


class TestSubmitTurnNoPlanPaths:
    async def test_smalltalk_returns_turn_without_plan(
        self,
        client: Any,
        app: FastAPI,
        settings: Settings,
        fake_model: _FakeChatModel,
    ) -> None:
        user_id = await _seed_user(app)
        conv_id = await _seed_conversation(app, user_id)
        await _seed_active_echo(app)
        fake_model.response_text = '{"nodes": []}'

        resp = await _submit(
            client, conv_id, headers=_bearer(user_id, settings=settings), content="你好",
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["plan"] is None
        assert body["turn"]["plan_id"] is None
        assert body["warnings"] == []

    async def test_llm_failure_degrades_to_warning_not_500(
        self,
        client: Any,
        app: FastAPI,
        settings: Settings,
        fake_model: _FakeChatModel,
    ) -> None:
        user_id = await _seed_user(app)
        conv_id = await _seed_conversation(app, user_id)
        await _seed_active_echo(app)
        fake_model.should_fail = True

        resp = await _submit(client, conv_id, headers=_bearer(user_id, settings=settings))

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["plan"] is None
        assert any("生成失败" in w for w in body["warnings"])
        # The Turn is still persisted — the instruction is not lost.
        detail = await client.get(
            f"/api/v1/conversations/{conv_id}",
            headers=_bearer(user_id, settings=settings),
        )
        assert len(detail.json()["turns"]) == 1

    async def test_hallucinated_tool_dropped_with_warning(
        self,
        client: Any,
        app: FastAPI,
        settings: Settings,
        fake_model: _FakeChatModel,
    ) -> None:
        user_id = await _seed_user(app)
        conv_id = await _seed_conversation(app, user_id)
        await _seed_active_echo(app)
        fake_model.response_text = '{"nodes": [{"tool": "drop_table"}]}'

        resp = await _submit(client, conv_id, headers=_bearer(user_id, settings=settings))

        assert resp.status_code == 201
        body = resp.json()
        assert body["plan"] is None
        assert any("drop_table" in w for w in body["warnings"])


class TestSubmitTurnGuards:
    async def test_unauthenticated_returns_401(self, client: Any, app: FastAPI) -> None:
        resp = await client.post(
            f"/api/v1/conversations/{ObjectId()}/turns",
            json={"content": "echo hello"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"]

    async def test_cross_user_conversation_returns_404(
        self,
        client: Any,
        app: FastAPI,
        settings: Settings,
    ) -> None:
        owner = await _seed_user(app)
        stranger = await _seed_user(app)
        conv_id = await _seed_conversation(app, owner)

        resp = await _submit(
            client, conv_id, headers=_bearer(stranger, settings=settings),
        )

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_archived_conversation_returns_409(
        self,
        client: Any,
        app: FastAPI,
        settings: Settings,
    ) -> None:
        user_id = await _seed_user(app)
        conv_id = await _seed_conversation(app, user_id, status="archived")

        resp = await _submit(client, conv_id, headers=_bearer(user_id, settings=settings))

        assert resp.status_code == 409
        assert resp.json()["code"] == "conversation_archived"

    async def test_absent_conversation_returns_404(
        self, client: Any, app: FastAPI, settings: Settings
    ) -> None:
        user_id = await _seed_user(app)
        resp = await _submit(
            client, str(ObjectId()), headers=_bearer(user_id, settings=settings),
        )
        assert resp.status_code == 404

    async def test_empty_content_rejected_by_wire_validation(
        self, client: Any, app: FastAPI, settings: Settings
    ) -> None:
        user_id = await _seed_user(app)
        conv_id = await _seed_conversation(app, user_id)
        resp = await _submit(
            client, conv_id, headers=_bearer(user_id, settings=settings), content="",
        )
        assert resp.status_code == 422
