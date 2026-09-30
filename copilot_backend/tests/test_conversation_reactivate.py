"""Tests for `ConversationService.reactivate` (T39 / #45, ADR-0011).

Verifies the service-layer invariants: ownership guard, source
must be archived, the freshly-active row is returned with the
audit FK pointers, and the most-recent K turns + latest Plan are
copied onto the new conversation.
"""
from __future__ import annotations

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.conversations.errors import (
    ConversationAccessDeniedError,
    ConversationNotArchivedError,
)
from app.conversations.service import ConversationService
from app.db.init_db import init_database
from app.db.schemas import (
    ConversationCreate,
    PlanCreate,
    PlanNode,
    ToolSnapshot,
    TurnCreate,
)
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.plans import PlanRepository
from app.repositories.turns import TurnRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db() -> AsyncMongoMockClient:
    return AsyncMongoMockClient()["copilot_reactivate_service_test"]


@pytest.fixture
async def conv_repo(db: AsyncMongoMockClient) -> ConversationRepository:
    await init_database(db)
    return ConversationRepository(db)


@pytest.fixture
async def turn_repo(db: AsyncMongoMockClient) -> TurnRepository:
    return TurnRepository(db)


@pytest.fixture
async def plan_repo(db: AsyncMongoMockClient) -> PlanRepository:
    return PlanRepository(db)


@pytest.fixture
async def audit_repo(db: AsyncMongoMockClient) -> AuditLogRepository:
    return AuditLogRepository(db)


@pytest.fixture
def user_id() -> str:
    return str(ObjectId())


@pytest.fixture
def other_user_id() -> str:
    return str(ObjectId())


@pytest.fixture
def conv_input(user_id: str) -> ConversationCreate:
    return ConversationCreate(user_id=user_id, title="session")


@pytest.fixture
def svc(
    conv_repo: ConversationRepository,
    turn_repo: TurnRepository,
    plan_repo: PlanRepository,
    audit_repo: AuditLogRepository,
) -> ConversationService:
    return ConversationService(
        conversation_repository=conv_repo,
        turn_repository=turn_repo,
        plan_repository=plan_repo,
        audit_log_repository=audit_repo,
        memory_window_k=3,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_archived(
    conv_repo: ConversationRepository,
    turn_repo: TurnRepository,
    plan_repo: PlanRepository,
    *,
    user_id: str,
    turn_count: int = 0,
    with_plan: bool = False,
) -> tuple[str, list[str], str | None]:
    """Seed an archived conversation with `turn_count` turns + (optional) plan.

    Returns `(conversation_id, turn_ids, plan_id_or_None)` so the
    test can assert against the input rows. When `with_plan=True`,
    the latest seeded Turn's `plan_id` FK is backfilled to point
    at the freshly-created Plan — same shape a real Planner run
    leaves behind.
    """
    conv = await conv_repo.create(ConversationCreate(user_id=user_id, title="old"))
    await conv_repo.set_status(conv.id, "archived")
    turn_ids: list[str] = []
    plan_id: str | None = None
    for i in range(turn_count):
        turn = await turn_repo.create(
            TurnCreate(
                conversation_id=conv.id,
                role="user",
                content=f"hello {i}",
            ),
        )
        turn_ids.append(turn.id)
    if with_plan and turn_ids:
        plan = await plan_repo.create(
            PlanCreate(
                conversation_id=conv.id,
                turn_id=turn_ids[-1],
                status="succeeded",
                nodes=[PlanNode(node_id="n1", tool="echo", parameters={"x": 1})],
                edges=[],
                tool_snapshots=[
                    ToolSnapshot(
                        name="echo",
                        description="echo",
                        risk_level="read",
                        http_method="POST",
                        http_url_template="https://example.test/echo",
                    ),
                ],
            ),
        )
        plan_id = plan.id
        # Backfill the triggering Turn's FK so the seeded
        # conversation mirrors a real post-Planner state.
        await turn_repo.set_plan_id(turn_ids[-1], plan_id)
    return conv.id, turn_ids, plan_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestReactivateHappyPath:
    """The T39 acceptance criteria — reactivate flow works end-to-end."""

    @pytest.mark.asyncio
    async def test_creates_new_active_conversation(
        self,
        svc: ConversationService,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        user_id: str,
    ) -> None:
        source_id, _, _ = await _seed_archived(
            conv_repo, turn_repo, plan_repo, user_id=user_id
        )

        result = await svc.reactivate(
            conversation_id=source_id, user_id=user_id, title="new"
        )

        assert result.conversation.status == "active"
        assert result.conversation.title == "new"
        assert result.conversation.user_id == user_id
        assert result.conversation.reactivated_from_id == source_id
        assert result.conversation.reactivate_count == 1
        assert result.source_conversation_id == source_id

    @pytest.mark.asyncio
    async def test_source_remains_archived(
        self,
        svc: ConversationService,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        user_id: str,
    ) -> None:
        source_id, _, _ = await _seed_archived(
            conv_repo, turn_repo, plan_repo, user_id=user_id
        )

        await svc.reactivate(
            conversation_id=source_id, user_id=user_id
        )

        refreshed = await conv_repo.get(source_id)
        assert refreshed.status == "archived"

    @pytest.mark.asyncio
    async def test_copies_latest_plan(
        self,
        svc: ConversationService,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        user_id: str,
    ) -> None:
        source_id, _, _ = await _seed_archived(
            conv_repo,
            turn_repo,
            plan_repo,
            user_id=user_id,
            turn_count=1,
            with_plan=True,
        )

        result = await svc.reactivate(
            conversation_id=source_id, user_id=user_id
        )

        assert result.copied_plan_id is not None
        new_plan = await plan_repo.get(result.copied_plan_id)
        assert new_plan.conversation_id == result.conversation.id
        # The frozen snapshot survives the copy verbatim.
        assert new_plan.tool_snapshots[0].name == "echo"

    @pytest.mark.asyncio
    async def test_copied_turns_plan_id_fk_is_rewritten_to_new_plan(
        self,
        svc: ConversationService,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        user_id: str,
    ) -> None:
        """The triggering Turn's `plan_id` FK is rewritten to the new Plan id.

        AC4 (数据完整性保留): the turn that originally triggered
        the latest source Plan keeps a valid FK on copy — pointing
        at the freshly-inserted Plan row, not the source's.
        """
        source_id, _, _ = await _seed_archived(
            conv_repo,
            turn_repo,
            plan_repo,
            user_id=user_id,
            turn_count=1,
            with_plan=True,
        )

        result = await svc.reactivate(
            conversation_id=source_id, user_id=user_id
        )

        copied_turns = await turn_repo.list_by_conversation(
            result.conversation.id
        )
        assert len(copied_turns) == 1
        assert copied_turns[0].plan_id == result.copied_plan_id

    @pytest.mark.asyncio
    async def test_copied_turns_without_plan_id_remain_unlinked(
        self,
        svc: ConversationService,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        user_id: str,
    ) -> None:
        """Turns that didn't reference any Plan keep `plan_id=None` on copy."""
        source_id, _, _ = await _seed_archived(
            conv_repo, turn_repo, plan_repo, user_id=user_id, turn_count=2
        )
        # No plan seeded; all source turns have `plan_id=None`.

        result = await svc.reactivate(
            conversation_id=source_id, user_id=user_id
        )

        assert result.copied_plan_id is None
        copied_turns = await turn_repo.list_by_conversation(
            result.conversation.id
        )
        assert all(t.plan_id is None for t in copied_turns)


# ---------------------------------------------------------------------------
# Ownership guard
# ---------------------------------------------------------------------------


class TestReactivateOwnership:
    """Cross-user access renders the same 404 envelope as an absent row."""

    @pytest.mark.asyncio
    async def test_cross_user_raises_access_denied(
        self,
        svc: ConversationService,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        user_id: str,
        other_user_id: str,
    ) -> None:
        source_id, _, _ = await _seed_archived(
            conv_repo, turn_repo, plan_repo, user_id=user_id
        )

        with pytest.raises(ConversationAccessDeniedError):
            await svc.reactivate(
                conversation_id=source_id, user_id=other_user_id
            )

    @pytest.mark.asyncio
    async def test_missing_raises_not_found(
        self,
        svc: ConversationService,
        user_id: str,
    ) -> None:
        from app.db.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await svc.reactivate(
                conversation_id=str(ObjectId()), user_id=user_id
            )


# ---------------------------------------------------------------------------
# State guard
# ---------------------------------------------------------------------------


class TestReactivateStateGuard:
    """`active` / `idle` sources raise `ConversationNotArchivedError`."""

    @pytest.mark.asyncio
    async def test_active_source_raises(
        self,
        svc: ConversationService,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        user_id: str,
    ) -> None:
        source_id, _, _ = await _seed_archived(
            conv_repo, turn_repo, plan_repo, user_id=user_id
        )
        # Mutate the seed back to `active` — reactivate only
        # accepts archived sources.
        await conv_repo.set_status(source_id, "active")

        with pytest.raises(ConversationNotArchivedError):
            await svc.reactivate(
                conversation_id=source_id, user_id=user_id
            )

    @pytest.mark.asyncio
    async def test_idle_source_raises(
        self,
        svc: ConversationService,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        user_id: str,
    ) -> None:
        source_id, _, _ = await _seed_archived(
            conv_repo, turn_repo, plan_repo, user_id=user_id
        )
        # Idle is the in-between state — the user should hit
        # `/archive` first to push it into the archived branch.
        await conv_repo.set_status(source_id, "idle")

        with pytest.raises(ConversationNotArchivedError):
            await svc.reactivate(
                conversation_id=source_id, user_id=user_id
            )


# ---------------------------------------------------------------------------
# History copy semantics
# ---------------------------------------------------------------------------


class TestReactivateHistoryCopy:
    """The K-turn + latest-Plan copy is correct under the documented contract."""

    @pytest.mark.asyncio
    async def test_copies_only_most_recent_k_turns(
        self,
        svc: ConversationService,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        user_id: str,
    ) -> None:
        # memory_window_k=3 in the fixture; seed 5 turns.
        source_id, turn_ids, _ = await _seed_archived(
            conv_repo,
            turn_repo,
            plan_repo,
            user_id=user_id,
            turn_count=5,
        )
        assert len(turn_ids) == 5

        result = await svc.reactivate(
            conversation_id=source_id, user_id=user_id
        )

        assert len(result.copied_turn_ids) == 3
        # The copies point at the *new* conversation.
        copied_turns = await turn_repo.list_by_conversation(
            result.conversation.id
        )
        assert {t.conversation_id for t in copied_turns} == {
            result.conversation.id
        }
        assert len(copied_turns) == 3

        # AC1 (数据完整性保留) hinges on this slice: the K most-
        # RECENT turns survive, not the oldest K. The seed writes
        # `hello 0..4` in order; K=3 must keep `hello 2..4`. A
        # regression to `source_turns[:limit]` would lose the
        # most-recent and surface older history instead — the
        # opposite of the documented contract.
        copied_contents = [t.content for t in copied_turns]
        assert copied_contents == ["hello 2", "hello 3", "hello 4"]

    @pytest.mark.asyncio
    async def test_copied_turns_keep_content(
        self,
        svc: ConversationService,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        user_id: str,
    ) -> None:
        source_id, _, _ = await _seed_archived(
            conv_repo, turn_repo, plan_repo, user_id=user_id, turn_count=3
        )

        result = await svc.reactivate(
            conversation_id=source_id, user_id=user_id
        )

        copied = await turn_repo.list_by_conversation(result.conversation.id)
        # `list_by_conversation` returns ascending order; we seeded
        # `hello 0`, `hello 1`, `hello 2` and the K-window keeps the
        # 3 most-recent (which is all of them at K=3, but the assertion
        # is content-equality regardless).
        assert [t.content for t in copied] == [
            "hello 0",
            "hello 1",
            "hello 2",
        ]

    @pytest.mark.asyncio
    async def test_copies_nothing_when_source_has_no_turns(
        self,
        svc: ConversationService,
        conv_repo: ConversationRepository,
        turn_repo: TurnRepository,
        plan_repo: PlanRepository,
        user_id: str,
    ) -> None:
        source_id, _, _ = await _seed_archived(
            conv_repo, turn_repo, plan_repo, user_id=user_id
        )

        result = await svc.reactivate(
            conversation_id=source_id, user_id=user_id
        )

        assert result.copied_turn_ids == []
        assert result.copied_plan_id is None