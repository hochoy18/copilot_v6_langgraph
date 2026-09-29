"""Unit tests for `PlannerService.submit_turn` — T18 / #16.

The service seam between the route and the LLM: ownership /
archived guards (ADR-0011), Turn persistence, snapshot freezing
(ADR-0027), Plan persistence (status `pending`, ADR-0004), and the
degradation ladder (LLM-layer failures leave the Turn persisted
with `plan=None` plus a warning — never a rolled-back request).

Repositories run against `mongomock_motor`; the chat model is a
fake answering with the `planner` JSON contract, so the whole
Turn → Plan write path is exercised without LangChain networking.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from mongomock_motor import AsyncMongoMockClient

from app.conversations.errors import (
    ConversationAccessDeniedError,
    ConversationArchivedError,
)
from app.db.errors import NotFoundError
from app.db.schemas import ConversationCreate, PlanEdge, ToolCreate, TurnCreate
from app.llm.prompts import PromptProvider
from app.planner.planner import ToolPlanner
from app.planner.service import PlannerService
from app.repositories.conversations import ConversationRepository
from app.repositories.plans import PlanRepository
from app.repositories.tools import ToolRepository
from app.repositories.turns import TurnRepository
from app.settings import Settings

_USER_ID = "507f1f77bcf86cd799439011"
_PLANNER_TEMPLATE = "CATALOG>>{{tools}}<<INSTRUCTION>>{{input}}<<"


class _FakeChatModel(BaseChatModel):
    response_text: str = '{"nodes": []}'
    should_fail: bool = False
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
        if self.should_fail:
            raise RuntimeError("upstream model exploded")
        generation = ChatGeneration(message=AIMessage(content=self.response_text))
        return ChatResult(generations=[generation])


@pytest.fixture
def database() -> Any:
    client = AsyncMongoMockClient()
    return client["planner_service_test"]


@pytest.fixture
def conversation_repo(database: Any) -> ConversationRepository:
    return ConversationRepository(database)


@pytest.fixture
def turn_repo(database: Any) -> TurnRepository:
    return TurnRepository(database)


@pytest.fixture
def plan_repo(database: Any) -> PlanRepository:
    return PlanRepository(database)


@pytest.fixture
def tool_repo(database: Any) -> ToolRepository:
    return ToolRepository(database)


def _planner(
    response_text: str,
    *,
    should_fail: bool = False,
    configured: bool = True,
) -> tuple[ToolPlanner, _FakeChatModel]:
    fake = _FakeChatModel(response_text=response_text, should_fail=should_fail)
    settings = Settings(
        llm_base_url="https://llm.example.com/v1" if configured else "",
        llm_api_key="sk-test" if configured else "",
    )
    provider = PromptProvider(
        settings=settings,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, json={"name": "planner", "version": 1, "prompt": _PLANNER_TEMPLATE}
                )
            )
        ),
    )
    return (
        ToolPlanner(
            settings=settings,
            prompt_provider=provider,
            chat_model_factory=lambda: fake,
        ),
        fake,
    )


def _service(
    *,
    conversation_repo: ConversationRepository,
    turn_repo: TurnRepository,
    plan_repo: PlanRepository,
    tool_repo: ToolRepository,
    planner: ToolPlanner,
) -> PlannerService:
    return PlannerService(
        conversation_repository=conversation_repo,
        turn_repository=turn_repo,
        plan_repository=plan_repo,
        tool_repository=tool_repo,
        planner=planner,
    )


async def _seed_conversation(
    repo: ConversationRepository,
    *,
    user_id: str = _USER_ID,
    status: str = "active",
) -> str:
    created = await repo.create(
        ConversationCreate(user_id=user_id, title="", status=status),  # type: ignore[arg-type]
    )
    return created.id


async def _seed_active_tool(repo: ToolRepository, name: str = "echo") -> str:
    created = await repo.create(
        ToolCreate(
            name=name,
            description="把传入的文本原样返回",
            risk_level="read",
            status="active",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            http_method="POST",
            http_url_template="https://api.example.test/echo",
            http_headers={"X-Static": "1"},
            http_body_template={"text": "{text}"},
            source="manual",
            source_ref=None,
        )
    )
    return created.id


_ECHO_PLAN_JSON = (
    '{"nodes": [{"tool": "echo", "parameters": {"text": "hello"}, '
    '"notes": "回显 hello"}]}'
)


class TestSubmitTurnHappyPath:
    async def test_echo_hello_persists_single_node_plan(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """AC #1 + #4: input echo hello → 1-node Plan, persisted to plans."""
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        planner, fake = _planner(_ECHO_PLAN_JSON)
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )

        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="echo hello",
        )

        assert outcome.plan is not None
        assert outcome.warnings == []
        assert fake.call_count == 1

        # Single node bound to the echo Tool.
        assert len(outcome.plan.nodes) == 1
        node = outcome.plan.nodes[0]
        assert node.node_id == "n1"
        assert node.tool == "echo"
        assert node.parameters == {"text": "hello"}
        assert node.notes == "回显 hello"
        assert outcome.plan.edges == []

        # Plan awaits HITL review (ADR-0004) and anchors on the Turn.
        assert outcome.plan.status == "pending"
        assert outcome.plan.conversation_id == conv_id
        assert outcome.plan.turn_id == outcome.turn.id

        # Turn is persisted and links back to the Plan.
        assert outcome.turn.role == "user"
        assert outcome.turn.content == "echo hello"
        assert outcome.turn.plan_id == outcome.plan.id
        stored_turn = await turn_repo.get(outcome.turn.id)
        assert stored_turn.plan_id == outcome.plan.id

        # AC #4: persisted to plans — re-read proves the write.
        stored = await plan_repo.get(outcome.plan.id)
        assert stored == outcome.plan

    async def test_snapshots_frozen_from_live_tool(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """AC #2: Plan carries tool_snapshots; ADR-0027 field-for-field."""
        conv_id = await _seed_conversation(conversation_repo)
        tool_id = await _seed_active_tool(tool_repo)
        planner, _ = _planner(_ECHO_PLAN_JSON)
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )

        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="echo hello",
        )
        assert outcome.plan is not None
        assert len(outcome.plan.tool_snapshots) == 1
        snap = outcome.plan.tool_snapshots[0]
        assert snap.tool_id == tool_id
        assert snap.name == "echo"
        assert snap.description == "把传入的文本原样返回"
        assert snap.risk_level == "read"
        assert snap.parameters_schema["required"] == ["text"]
        assert snap.http_method == "POST"
        assert snap.http_url_template == "https://api.example.test/echo"
        assert snap.http_headers == {"X-Static": "1"}
        assert snap.http_body_template == {"text": "{text}"}

    async def test_snapshot_does_not_leak_credentials_or_status(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """ADR-0002: the credential pointer and admin metadata stay out."""
        conv_id = await _seed_conversation(conversation_repo)
        await tool_repo.create(
            ToolCreate(
                name="echo",
                description="d",
                risk_level="read",
                status="active",
                parameters_schema={},
                http_method="GET",
                http_url_template="https://x.test/e",
                http_headers={},
                http_body_template=None,
                source="manual",
                source_ref=None,
                credentials_ref="507f1f77bcf86cd799439099",
            )
        )
        planner, _ = _planner('{"nodes": [{"tool": "echo"}]}')
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="echo",
        )
        assert outcome.plan is not None
        snap = outcome.plan.tool_snapshots[0]
        payload = snap.model_dump()
        assert "credentials_ref" not in payload
        assert "status" not in payload

    async def test_same_tool_twice_yields_one_snapshot(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """T25 forward-compat: N nodes, snapshot set stays name-unique."""
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        planner, _ = _planner(
            '{"nodes": [{"tool": "echo", "parameters": {"text": "a"}}, '
            '{"tool": "echo", "parameters": {"text": "b"}}]}'
        )
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="echo a then echo b",
        )
        assert outcome.plan is not None
        assert [n.node_id for n in outcome.plan.nodes] == ["n1", "n2"]
        assert len(outcome.plan.tool_snapshots) == 1

    async def test_echo_a_then_echo_b_persists_two_nodes_with_edge(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """T25 AC #1 + #2: echo A 然后 echo B → 2 nodes with 1 data-dependency edge.

        The LLM returns 1-based indices (1→2); the service maps these
        onto the assigned `n{index}` `node_id`s so the persisted Plan
        is the ADR-0012 DAG the executor and React Flow renderer expect.
        """
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        planner, _ = _planner(
            '{"nodes": ['
            '{"tool": "echo", "parameters": {"text": "A"}, "notes": "先回显 A"}, '
            '{"tool": "echo", "parameters": {"text": "B"}, "notes": "再回显 B"}'
            '], '
            '"edges": [{"source": 1, "target": 2}]}'
        )
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="先 echo A 再 echo B",
        )
        assert outcome.plan is not None
        assert len(outcome.plan.nodes) == 2
        assert [n.node_id for n in outcome.plan.nodes] == ["n1", "n2"]
        assert outcome.plan.edges == [
            PlanEdge(source="n1", target="n2")
        ]
        # Edges survive a re-read (they're persisted, not just returned).
        stored = await plan_repo.get(outcome.plan.id)
        assert stored.edges == [PlanEdge(source="n1", target="n2")]

    async def test_independent_parallel_nodes_have_no_edges(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """Two unrelated lookups → 2 nodes, no edges — ADR-0012 parallel branch."""
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        planner, _ = _planner(
            '{"nodes": ['
            '{"tool": "echo", "parameters": {"text": "x"}}, '
            '{"tool": "echo", "parameters": {"text": "y"}}'
            '], '
            '"edges": []}'
        )
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="并行查两件事",
        )
        assert outcome.plan is not None
        assert len(outcome.plan.nodes) == 2
        assert outcome.plan.edges == []

    async def test_three_node_chain_persists_two_edges(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """A → B → C chain: 3 nodes, 2 edges."""
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        planner, _ = _planner(
            '{"nodes": ['
            '{"tool": "echo", "parameters": {"text": "a"}}, '
            '{"tool": "echo", "parameters": {"text": "b"}}, '
            '{"tool": "echo", "parameters": {"text": "c"}}'
            '], '
            '"edges": [{"source": 1, "target": 2}, {"source": 2, "target": 3}]}'
        )
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="链式三步",
        )
        assert outcome.plan is not None
        assert len(outcome.plan.nodes) == 3
        assert outcome.plan.edges == [
            PlanEdge(source="n1", target="n2"),
            PlanEdge(source="n2", target="n3"),
        ]

    async def test_cyclic_edges_from_planner_are_dropped_before_persist(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """The parser drops cycle-closing edges; only acyclic ones reach MongoDB."""
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        planner, _ = _planner(
            '{"nodes": ['
            '{"tool": "echo"}, '
            '{"tool": "echo"}'
            '], '
            '"edges": [{"source": 1, "target": 2}, {"source": 2, "target": 1}]}'
        )
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="bad plan",
        )
        assert outcome.plan is not None
        assert outcome.plan.edges == [PlanEdge(source="n1", target="n2")]
        assert any("环" in w or "cycle" in w for w in outcome.warnings)

    async def test_turn_keeps_conversation_active(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """ADR-0011: a fresh user Turn bumps last_activity_at."""
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        before = await conversation_repo.get(conv_id)
        planner, _ = _planner(_ECHO_PLAN_JSON)
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="echo hello",
        )
        after = await conversation_repo.get(conv_id)
        assert after.last_activity_at >= before.last_activity_at


class TestSubmitTurnNoPlanPaths:
    async def test_smalltalk_turn_persists_without_plan(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """ADR-0004: nodes=[] → Turn stands alone, plan_id None."""
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        planner, _ = _planner('{"nodes": []}')
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="你好",
        )
        assert outcome.plan is None
        assert outcome.turn.plan_id is None
        assert outcome.warnings == []
        assert await plan_repo.list_by_conversation(conv_id) == []

    async def test_llm_not_configured_degrades_with_warning(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        planner, fake = _planner(_ECHO_PLAN_JSON, configured=False)
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="echo hello",
        )
        assert outcome.plan is None
        assert fake.call_count == 0
        assert any("未配置" in w for w in outcome.warnings)
        assert len(await turn_repo.list_by_conversation(conv_id)) == 1

    async def test_llm_failure_degrades_with_warning(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        planner, _ = _planner(_ECHO_PLAN_JSON, should_fail=True)
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="echo hello",
        )
        assert outcome.plan is None
        assert any("生成失败" in w for w in outcome.warnings)
        # Turn survives — the failure is announced, not swallowed.
        assert outcome.turn.id
        assert outcome.turn.plan_id is None

    async def test_empty_registry_skips_llm(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """No active Tools → nothing to prompt with; skip the call."""
        conv_id = await _seed_conversation(conversation_repo)
        planner, fake = _planner(_ECHO_PLAN_JSON)
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="echo hello",
        )
        assert outcome.plan is None
        assert fake.call_count == 0
        assert any("active Tool" in w for w in outcome.warnings)

    async def test_hallucinated_tool_never_persists_as_plan(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        planner, _ = _planner('{"nodes": [{"tool": "delete_everything"}]}')
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="删库",
        )
        assert outcome.plan is None
        assert any("delete_everything" in w for w in outcome.warnings)

    async def test_long_notes_are_clamped_to_schema_cap(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        conv_id = await _seed_conversation(conversation_repo)
        await _seed_active_tool(tool_repo)
        planner, _ = _planner(
            '{"nodes": [{"tool": "echo", "notes": "' + "很长的说明" * 200 + '"}]}'
        )
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        outcome = await svc.submit_turn(
            conversation_id=conv_id, user_id=_USER_ID, content="echo hello",
        )
        assert outcome.plan is not None
        assert len(outcome.plan.nodes[0].notes) <= 512


class TestSubmitTurnGuards:
    async def test_other_users_conversation_is_not_found(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """Ownership guard renders the 404 envelope, not an existence leak."""
        conv_id = await _seed_conversation(conversation_repo)
        planner, fake = _planner(_ECHO_PLAN_JSON)
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        with pytest.raises(ConversationAccessDeniedError):
            await svc.submit_turn(
                conversation_id=conv_id,
                user_id="507f1f77bcf86cd799439099",
                content="echo hello",
            )
        assert fake.call_count == 0
        assert await turn_repo.list_by_conversation(conv_id) == []

    async def test_archived_conversation_is_read_only(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        """ADR-0011: archived rows reject new Turns before any write."""
        conv_id = await _seed_conversation(conversation_repo, status="archived")
        planner, fake = _planner(_ECHO_PLAN_JSON)
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        with pytest.raises(ConversationArchivedError):
            await svc.submit_turn(
                conversation_id=conv_id, user_id=_USER_ID, content="echo hello",
            )
        assert fake.call_count == 0
        assert await turn_repo.list_by_conversation(conv_id) == []

    async def test_absent_conversation_raises_not_found(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        tool_repo: ToolRepository,
    ) -> None:
        planner, _ = _planner(_ECHO_PLAN_JSON)
        svc = _service(
            conversation_repo=conversation_repo, turn_repo=turn_repo,
            plan_repo=plan_repo, tool_repo=tool_repo, planner=planner,
        )
        with pytest.raises(NotFoundError):
            await svc.submit_turn(
                conversation_id="507f1f77bcf86cd799439999",
                user_id=_USER_ID,
                content="echo hello",
            )


class TestTurnRepositorySetPlanId:
    async def test_set_plan_id_links_without_touching_content(
        self,
        conversation_repo: ConversationRepository,
        turn_repo: TurnRepository,
    ) -> None:
        conv_id = await _seed_conversation(conversation_repo)
        turn = await turn_repo.create(
            TurnCreate(conversation_id=conv_id, role="user", content="echo hello")
        )
        assert turn.plan_id is None

        linked = await turn_repo.set_plan_id(turn.id, "507f1f77bcf86cd7994390aa")
        assert linked.plan_id == "507f1f77bcf86cd7994390aa"
        assert linked.content == "echo hello"
        assert linked.role == "user"
        assert linked.created_at == turn.created_at

    async def test_set_plan_id_missing_turn_raises(self, turn_repo: TurnRepository) -> None:
        with pytest.raises(NotFoundError):
            await turn_repo.set_plan_id("507f1f77bcf86cd799439999", "507f1f77bcf86cd7994390aa")
