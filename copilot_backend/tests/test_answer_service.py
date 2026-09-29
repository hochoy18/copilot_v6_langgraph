"""Integration tests for `AnswerService` — T22 / #19.

The orchestrator sits between the LLM streaming layer and the SSE /
Turn persistence layer. The seams exercised here:

* `build_summaries` — the pure projection that joins `Plan.nodes`
  (notes) with `PlanNodeResult`s (status / response / error) into the
  `NodeSummary` list the generator consumes.
* `AnswerService.stream_final_answer` — end-to-end with an in-memory
  bus + stub repos:
    - happy path: tokens fan out as `llm.token` events; an
      `assistant` Turn lands with the assembled text,
    - failed Plan: streaming is skipped, no Turn is persisted,
    - unconfigured LLM: degraded `StreamOutcome` with the
      configuration reason,
    - empty streamed answer: degraded outcome, no Turn,
    - LLM-layer exception: propagates untouched so the route can
      render the same Planner-path degradation envelope.

The HTTP-level wiring (SSE end-to-end through the route) lives in
`test_plan_execution_routes.py`; the generator's pure unit tests
live in `test_answer_generator.py`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.answer.generator import AnswerGenerator, NodeSummary
from app.answer.service import AnswerService, build_summaries
from app.db.schemas import (
    ConversationCreate,
    Plan,
    PlanCreate,
    PlanNode,
    PlanNodeResult,
    ToolSnapshot,
    TurnCreate,
)
from app.llm.errors import LLMGenerationError
from app.realtime.bus import SseEventBus, iter_events
from app.repositories.conversations import ConversationRepository
from app.repositories.turns import TurnRepository


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeBus(SseEventBus):
    """Subclass that doesn't import any extra deps — passes as an `SseEventBus`."""


class _FakeGenerator:
    """Test stand-in for `AnswerGenerator` — bypasses the LLM entirely.

    `tokens` is the sequence the streaming loop yields; `raise_exc` is
    the exception the streaming loop raises (or `None`).
    """

    def __init__(
        self,
        *,
        tokens: list[str] | None = None,
        raise_exc: BaseException | None = None,
        ready: bool = True,
        seen_instructions: list[str] | None = None,
        seen_results: list[list[NodeSummary]] | None = None,
    ) -> None:
        self.tokens = tokens or []
        self.raise_exc = raise_exc
        self._ready = ready
        self.seen_instructions = seen_instructions if seen_instructions is not None else []
        self.seen_results = seen_results if seen_results is not None else []

    @property
    def ready(self) -> bool:
        return self._ready

    async def astream(
        self,
        *,
        instruction: str,
        results: list[NodeSummary],
    ) -> AsyncIterator[str]:
        self.seen_instructions.append(instruction)
        self.seen_results.append(list(results))
        if self.raise_exc is not None:
            raise self.raise_exc
        for token in self.tokens:
            yield token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
async def conv_repo() -> AsyncIterator[ConversationRepository]:
    db = AsyncMongoMockClient()["copilot_answer_service_test"]
    yield ConversationRepository(db)


@pytest.fixture
async def turn_repo() -> AsyncIterator[TurnRepository]:
    db = AsyncMongoMockClient()["copilot_answer_service_test"]
    yield TurnRepository(db)


@pytest.fixture
async def conv(fixed_now: datetime, conv_repo: ConversationRepository) -> Any:
    return await conv_repo.create(
        ConversationCreate(user_id=str(ObjectId()), title="")
    )


@pytest.fixture
def bus() -> SseEventBus:
    return _FakeBus()


# Module-level fixed timestamp for fixtures that don't take the
# `fixed_now` parameter (pure unit tests for `build_summaries` etc.).
_FIXED_TS = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_plan(
    *,
    status: str = "succeeded",
    nodes: list[PlanNode] | None = None,
) -> Plan:
    """Build a Plan row with one snapshot and the given nodes."""
    snapshot = ToolSnapshot(
        tool_id=str(ObjectId()),
        name="echo",
        description="回显文本",
        risk_level="read",
        parameters_schema={},
        http_method="POST",
        http_url_template="https://upstream.test/echo",
        http_headers={},
        http_body_template=None,
    )
    plan_nodes = nodes if nodes is not None else [
        PlanNode(node_id="n1", tool="echo", parameters={"text": "hi"}, notes="回显 hello")
    ]
    create = PlanCreate(
        conversation_id=str(ObjectId()),
        turn_id=str(ObjectId()),
        status=status,  # type: ignore[arg-type]
        nodes=plan_nodes,
        edges=[],
        tool_snapshots=[snapshot],
    )
    # The orchestrator only reads `.status`, `.id`, `.nodes`; building
    # via the Pydantic canonical shape is enough.
    return Plan(
        **create.model_dump(),
        _id=str(ObjectId()),
        edited_diff=None,
        created_at=_FIXED_TS,
        updated_at=_FIXED_TS,
    )


def _make_node_result(
    *,
    node_id: str = "n1",
    status: str = "succeeded",
    response: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> PlanNodeResult:
    return PlanNodeResult(
        node_id=node_id,
        status=status,  # type: ignore[arg-type]
        started_at=_FIXED_TS,
        finished_at=_FIXED_TS,
        request={"method": "POST", "url": "https://upstream.test/echo"},
        response=response,
        error=error,
        retry_count=0,
    )


# ---------------------------------------------------------------------------
# build_summaries — pure projection
# ---------------------------------------------------------------------------


class TestBuildSummaries:
    def test_projects_status_response_error_per_node(self) -> None:
        plan = _make_plan()
        summaries = build_summaries(
            plan,
            [
                _make_node_result(
                    node_id="n1",
                    status="succeeded",
                    response={"text": "hello"},
                ),
            ],
        )
        assert len(summaries) == 1
        assert summaries[0].node_id == "n1"
        assert summaries[0].tool_name == "echo"
        assert summaries[0].status == "succeeded"
        assert summaries[0].notes == "回显 hello"
        assert summaries[0].response_text == "hello"

    def test_unknown_node_id_falls_back_to_node_id_label(self) -> None:
        """A `node_id` on the result that isn't in the Plan keeps the
        LLM-side label distinct (the orchestrator can still render it
        even when the Plan-side binding is missing)."""
        plan = _make_plan()
        summaries = build_summaries(
            plan,
            [_make_node_result(node_id="ghost", status="failed")],
        )
        assert summaries[0].tool_name == "ghost"

    def test_empty_results_returns_empty_list(self) -> None:
        assert build_summaries(_make_plan(), []) == []


# ---------------------------------------------------------------------------
# AnswerService.stream_final_answer — happy path
# ---------------------------------------------------------------------------


class TestStreamFinalAnswerHappyPath:
    async def test_streams_tokens_and_persists_assistant_turn(
        self,
        bus: SseEventBus,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        conv: Any,
        fixed_now: datetime,
    ) -> None:
        user_turn = await turn_repo.create(
            TurnCreate(
                conversation_id=conv.id,
                role="user",
                content="echo hello",
                plan_id=None,
            ),
        )
        generator = _FakeGenerator(tokens=["你好", "，", "世界"])
        service = AnswerService(
            generator=generator,  # type: ignore[arg-type]
            sse_bus=bus,
            turn_repository=turn_repo,
            conversation_repository=conv_repo,
            now_fn=lambda: fixed_now,
        )

        outcome = await service.stream_final_answer(
            conversation_id=conv.id,
            user_turn_id=user_turn.id,
            plan=_make_plan(),
            instruction="echo hello",
            node_results=[_make_node_result(response={"text": "hello"})],
        )

        assert outcome.assistant_turn is not None
        assert outcome.assistant_turn.role == "assistant"
        assert outcome.assistant_turn.content == "你好，世界"
        assert outcome.assistant_turn.plan_id is not None
        assert outcome.assistant_turn.conversation_id == conv.id
        assert outcome.degraded == ""

        # Tokens flowed through the bus — inspect the replay buffer
        # (the bus keeps the last events per conversation). This avoids
        # the subscriber-block-on-empty-queue pitfall the live
        # `iter_events` loop has when the test exits before more
        # events arrive.
        stats = await bus.channel_stats(conv.id)
        # Re-fetch the buffer directly: the channel exposes it via
        # `subscribe`'s replay slice; we already consumed that one,
        # but the bus appends every event to its channel buffer. A new
        # subscriber with `last_seen_id=0` gets the replay.
        _, replay = await bus.subscribe(conv.id, last_seen_id=0)
        token_events = [e for e in replay if e.event == "llm.token"]
        assert len(token_events) >= 1
        # Every event's payload.turn_id points at the user turn.
        for ev in token_events:
            assert ev.payload["turn_id"] == user_turn.id
            assert ev.payload["token"] in {"你好", "，", "世界"}

        # Generator saw the instruction + summaries.
        assert generator.seen_instructions == ["echo hello"]
        assert len(generator.seen_results[0]) == 1
        # Buffer stats reflect the published tokens.
        assert stats["buffer_len"] >= 1

    async def test_empty_streamed_answer_returns_degraded_outcome(
        self,
        bus: SseEventBus,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        conv: Any,
        fixed_now: datetime,
    ) -> None:
        user_turn = await turn_repo.create(
            TurnCreate(
                conversation_id=conv.id,
                role="user",
                content="echo",
                plan_id=None,
            ),
        )
        generator = _FakeGenerator(tokens=["", " ", "\n"])  # whitespace only
        service = AnswerService(
            generator=generator,  # type: ignore[arg-type]
            sse_bus=bus,
            turn_repository=turn_repo,
            conversation_repository=conv_repo,
            now_fn=lambda: fixed_now,
        )

        outcome = await service.stream_final_answer(
            conversation_id=conv.id,
            user_turn_id=user_turn.id,
            plan=_make_plan(),
            instruction="echo",
            node_results=[_make_node_result()],
        )

        assert outcome.assistant_turn is None
        assert "empty" in outcome.degraded.lower()

        # No assistant Turn was persisted.
        all_turns = await turn_repo.list_by_conversation(conv.id)
        assert all(t.role == "user" for t in all_turns)


# ---------------------------------------------------------------------------
# AnswerService.stream_final_answer — degradation paths
# ---------------------------------------------------------------------------


class TestStreamFinalAnswerDegradation:
    async def test_failed_plan_skips_streaming(
        self,
        bus: SseEventBus,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        conv: Any,
        fixed_now: datetime,
    ) -> None:
        generator = _FakeGenerator(tokens=["不应该出现"])
        service = AnswerService(
            generator=generator,  # type: ignore[arg-type]
            sse_bus=bus,
            turn_repository=turn_repo,
            conversation_repository=conv_repo,
            now_fn=lambda: fixed_now,
        )

        outcome = await service.stream_final_answer(
            conversation_id=conv.id,
            user_turn_id=str(ObjectId()),
            plan=_make_plan(status="failed"),
            instruction="echo",
            node_results=[_make_node_result(status="failed")],
        )

        assert outcome.assistant_turn is None
        assert "did not succeed" in outcome.degraded
        # The generator was never invoked.
        assert generator.seen_instructions == []

    async def test_unconfigured_llm_skips_streaming(
        self,
        bus: SseEventBus,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        conv: Any,
        fixed_now: datetime,
    ) -> None:
        generator = _FakeGenerator(tokens=["x"], ready=False)
        service = AnswerService(
            generator=generator,  # type: ignore[arg-type]
            sse_bus=bus,
            turn_repository=turn_repo,
            conversation_repository=conv_repo,
            now_fn=lambda: fixed_now,
        )

        outcome = await service.stream_final_answer(
            conversation_id=conv.id,
            user_turn_id=str(ObjectId()),
            plan=_make_plan(),
            instruction="echo",
            node_results=[_make_node_result()],
        )

        assert outcome.assistant_turn is None
        assert "not configured" in outcome.degraded

    async def test_llm_exception_propagates_untouched(
        self,
        bus: SseEventBus,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        conv: Any,
        fixed_now: datetime,
    ) -> None:
        boom = LLMGenerationError(message_en="model offline")
        generator = _FakeGenerator(raise_exc=boom)
        service = AnswerService(
            generator=generator,  # type: ignore[arg-type]
            sse_bus=bus,
            turn_repository=turn_repo,
            conversation_repository=conv_repo,
            now_fn=lambda: fixed_now,
        )

        with pytest.raises(LLMGenerationError):
            await service.stream_final_answer(
                conversation_id=conv.id,
                user_turn_id=str(ObjectId()),
                plan=_make_plan(),
                instruction="echo",
                node_results=[_make_node_result()],
            )