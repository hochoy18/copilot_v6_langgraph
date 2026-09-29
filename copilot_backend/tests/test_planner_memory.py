"""Unit tests for `app.planner.memory` — T30 / #26 (ADR-0007).

The memory window is the Planner's per-turn context block: the
previous K user turns verbatim, with each turn's linked Plan
summarised inline. K is configurable, default 5 per SPEC.

These tests pin the three acceptance criteria from issue #26:

1. **6 轮会话第 6 轮 Planner 输入含前 5 轮** — at the 6th turn, the
   rendered window carries the first five user turns and *only*
   those five. AC#1.
2. **超 K 轮早期截断** — once a conversation has more than K user
   turns, older turns drop off the front of the window. AC#2.
3. **K 可配置** — the same conversation can be re-rendered under a
   different K without code changes; the planner-side K surfaces on
   the `ToolPlanner` and is read by the service to size its
   turn-fetch query. AC#3.

A handful of degradation paths are pinned here too: missing Plan
rows, assistant-only turns, and an empty conversation. They live in
this file (not in `test_planner_service.py`) because they're
pure-shape properties of the renderer — no I/O, no scheduler.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.db.schemas import (
    Plan,
    PlanCreate,
    PlanNode,
    ToolSnapshot,
    Turn,
)
from app.planner.memory import DEFAULT_MEMORY_WINDOW_K, build_memory_window

# A stable "now" so Plan / Turn timestamps don't fight Pydantic when
# the suite instantiates them inline.
_NOW = datetime(2026, 9, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Test fixtures (lightweight, no I/O)
# ---------------------------------------------------------------------------


def _user_turn(content: str, *, plan_id: str | None = None, idx: int = 0) -> Turn:
    return Turn(
        _id=f"u-turn-{idx}",
        conversation_id="c-1",
        role="user",
        content=content,
        plan_id=plan_id,
        extra={},
        created_at=_NOW,
    )


def _assistant_turn(content: str, *, idx: int = 0) -> Turn:
    return Turn(
        _id=f"a-turn-{idx}",
        conversation_id="c-1",
        role="assistant",
        content=content,
        plan_id=None,
        extra={},
        created_at=_NOW,
    )


def _plan(*, node_count: int = 1, tool: str = "echo", params: dict[str, Any] | None = None) -> Plan:
    """One Plan with `node_count` nodes binding the same Tool snapshot.

    The Plan doc shape is the T17 trio — `nodes` / `edges` /
    `tool_snapshots` (see `app.db.schemas.Plan`). The snapshot is
    reused across nodes because the renderer only inspects
    `node.tool` + `node.parameters`, not the snapshots list.
    """
    snapshot = ToolSnapshot(
        tool_id="t-1",
        name=tool,
        description="echo",
        risk_level="read",
        parameters_schema={"type": "object"},
        http_method="POST",
        http_url_template="https://api.example.test/echo",
        http_headers={},
        http_body_template=None,
    )
    nodes = [
        PlanNode(
            node_id=f"n{i}",
            tool=tool,
            parameters=params or {"text": f"hello {i}"},
            notes="",
        )
        for i in range(1, node_count + 1)
    ]
    create = PlanCreate(
        conversation_id="c-1",
        turn_id="u-turn-1",
        status="succeeded",
        nodes=nodes,
        edges=[],
        tool_snapshots=[snapshot],
    )
    return Plan(
        _id="p-1",
        conversation_id=create.conversation_id,
        turn_id=create.turn_id,
        status=create.status,
        nodes=create.nodes,
        edges=create.edges,
        tool_snapshots=create.tool_snapshots,
        edited_diff=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Default K + render shape
# ---------------------------------------------------------------------------


def test_default_k_is_five() -> None:
    """SPEC floor — K=5 unless overridden via settings.

    The constant is the single authoritative default; a misconfigured
    `Settings.memory_window_k` falls back to it but a missing
    `ToolPlanner.memory_window_k` argument takes this exact value.
    """
    assert DEFAULT_MEMORY_WINDOW_K == 5


def test_empty_history_renders_placeholder() -> None:
    """A cold-start conversation has no turns; the LLM-facing Prompt
    must never see an empty slot — render the placeholder string so
    the prompt's prose remains a well-formed sentence."""
    rendered = build_memory_window([], {}, k=5)
    assert rendered == "(当前没有历史对话 / no prior turns)"


def test_assistant_only_conversation_renders_placeholder() -> None:
    """An assistant-only transcript has no user turns to surface.

    The window only carries user turns (ADR-0007's "原文"); the
    LLM's own prior answers are reachable through the Plan row
    rather than the chat transcript.
    """
    turns = [_assistant_turn("hi", idx=1)]
    rendered = build_memory_window(turns, {}, k=5)
    assert rendered == "(当前没有历史对话 / no prior turns)"


# ---------------------------------------------------------------------------
# AC#1: 6-turn conversation → 6th turn's window carries the first 5
# ---------------------------------------------------------------------------


def test_six_turn_window_contains_first_five_turns() -> None:
    """AC#1 (T30): the 6th turn's Planner input must contain the
    prior 5 turns. Older turns (the 1st in this scenario) are
    truncated once the conversation outgrows K."""
    turns = [_user_turn(f"指令 {i}", idx=i) for i in range(1, 7)]
    rendered = build_memory_window(turns, {}, k=5)

    # The 2nd through 6th turns land in the window — they are the
    # last K=5 user turns at the moment the 7th user message is
    # being processed (which is the scenario the spec describes).
    for i in range(2, 7):
        assert f"指令 {i}" in rendered, (
            f"turn {i} missing from window: {rendered!r}"
        )
    # The 1st turn falls off the front once K turns have passed.
    assert "指令 1" not in rendered


def test_window_is_chronological_not_reversed() -> None:
    """ADR-0007: window renders in chronological order so the LLM
    can track the conversation as a left-to-right narrative."""
    turns = [_user_turn(f"turn {i}", idx=i) for i in range(1, 4)]
    rendered = build_memory_window(turns, {}, k=5)
    assert rendered.index("turn 1") < rendered.index("turn 2")
    assert rendered.index("turn 2") < rendered.index("turn 3")


def test_window_uses_round_index_label() -> None:
    """The renderer's `[轮次 N]` label restarts at 1 for the
    earliest surviving turn — the LLM must not see `[轮次 6]` for
    what was, from its perspective, the first prior message."""
    turns = [_user_turn(f"指令 {i}", idx=i) for i in range(1, 4)]
    rendered = build_memory_window(turns, {}, k=5)
    assert "[轮次 1]" in rendered
    assert "[轮次 3]" in rendered
    assert "[轮次 4]" not in rendered


# ---------------------------------------------------------------------------
# AC#2: early turns truncated once K is exceeded
# ---------------------------------------------------------------------------


def test_long_conversation_truncates_older_turns() -> None:
    """AC#2: a 10-turn conversation rendered with K=5 surfaces only
    the last 5 user turns. The first 5 are dropped silently — the
    spec calls for "超 K 轮早期截断", not pagination."""
    turns = [_user_turn(f"指令 {i:02d}", idx=i) for i in range(1, 11)]
    rendered = build_memory_window(turns, {}, k=5)
    # Last five (06..10) are present.
    for i in range(6, 11):
        assert f"指令 {i:02d}" in rendered
    # First five (01..05) are dropped.
    for i in range(1, 6):
        assert f"指令 {i:02d}" not in rendered


def test_short_conversation_keeps_every_turn() -> None:
    """The window grows up to K; below K it carries every turn.

    The "truncation" rule applies only when the conversation has
    strictly more than K turns — a 3-turn conversation under K=5
    keeps all 3.
    """
    turns = [_user_turn(f"指令 {i}", idx=i) for i in range(1, 4)]
    rendered = build_memory_window(turns, {}, k=5)
    for i in range(1, 4):
        assert f"指令 {i}" in rendered


# ---------------------------------------------------------------------------
# AC#3: K is configurable
# ---------------------------------------------------------------------------


def test_k_three_window_keeps_only_last_three() -> None:
    """Same conversation, K=3: only the last three user turns survive."""
    turns = [_user_turn(f"指令 {i}", idx=i) for i in range(1, 8)]
    rendered = build_memory_window(turns, {}, k=3)
    for i in range(5, 8):
        assert f"指令 {i}" in rendered
    for i in range(1, 5):
        assert f"指令 {i}" not in rendered


def test_k_one_window_keeps_only_last_turn() -> None:
    """K=1 is the floor (Settings.memory_window_k ge=1) — only the
    most recent prior turn is surfaced."""
    turns = [_user_turn(f"指令 {i}", idx=i) for i in range(1, 4)]
    rendered = build_memory_window(turns, {}, k=1)
    assert "指令 3" in rendered
    assert "指令 1" not in rendered
    assert "指令 2" not in rendered


def test_k_zero_or_negative_renders_placeholder() -> None:
    """`k <= 0` is treated as "no window" — the placeholder is
    rendered rather than raising. Operators that misconfigure K
    must not crash the Planner."""
    turns = [_user_turn(f"指令 {i}", idx=i) for i in range(1, 4)]
    for bad_k in (0, -1, -100):
        rendered = build_memory_window(turns, {}, k=bad_k)
        assert rendered == "(当前没有历史对话 / no prior turns)"


# ---------------------------------------------------------------------------
# Plan summary lines
# ---------------------------------------------------------------------------


def test_turn_with_plan_renders_inline_summary() -> None:
    """A user Turn whose `plan_id` is set renders an inline Plan
    summary so the LLM knows which Tool was called with which
    parameters. The summary stays compact (tool slug + parameter
    repr) — full snapshot lives on `plans._id` for audit replay."""
    plan = _plan(node_count=2, tool="echo", params={"text": "hello"})
    turn = _user_turn("echo hello", plan_id="p-1", idx=1)
    plans_by_turn_id = {turn.id: plan}
    rendered = build_memory_window([turn], plans_by_turn_id, k=5)

    assert "echo hello" in rendered
    assert "本轮 Plan" in rendered
    assert "2 个节点" in rendered
    assert "echo" in rendered


def test_turn_without_plan_drops_summary_line() -> None:
    """A Turn with `plan_id=None` (smalltalk, degraded-LLM path,
    ADR-0004) renders no Plan summary — the LLM just sees the user
    message verbatim."""
    turn = _user_turn("你好", plan_id=None, idx=1)
    rendered = build_memory_window([turn], {}, k=5)
    assert "你好" in rendered
    assert "本轮 Plan" not in rendered


def test_missing_plan_snapshot_degrades_to_placeholder_line() -> None:
    """A Turn with a `plan_id` but a missing snapshot (admin
    hard-deleted the Plan) must not abort the Planner call — the
    renderer emits a "no Plan" line instead. Same degradation rule
    as `PlannerService._build_memory_window`."""
    turn = _user_turn("echo", plan_id="p-1", idx=1)
    plans_by_turn_id = {turn.id: None}
    rendered = build_memory_window([turn], plans_by_turn_id, k=5)
    assert "本轮未生成 Plan" in rendered


def test_zero_node_plan_renders_chitchat_placeholder() -> None:
    """A Plan with zero nodes is the legitimate smalltalk answer
    (ADR-0004). Surface this fact to the LLM so it doesn't wonder
    why the prior message had no Plan attached."""
    plan = _plan(node_count=0)
    turn = _user_turn("嗨", plan_id="p-1", idx=1)
    rendered = build_memory_window([turn], {turn.id: plan}, k=5)
    assert "本轮 Planner 输出空 Plan" in rendered


# ---------------------------------------------------------------------------
# Turn filtering
# ---------------------------------------------------------------------------


def test_assistant_turns_are_filtered_out() -> None:
    """The window only carries user turns (ADR-0007 '原文进 LLM').

    Assistant turns would clutter the slot with the LLM's own
    answers — those are derivable from the Plan row and don't need
    a second copy in the prompt.
    """
    turns = [
        _user_turn("hello", idx=1),
        _assistant_turn("hi there", idx=1),
        _user_turn("how are you?", idx=2),
    ]
    rendered = build_memory_window(turns, {}, k=5)
    assert "hello" in rendered
    assert "how are you?" in rendered
    assert "hi there" not in rendered


def test_window_size_does_not_count_assistant_turns() -> None:
    """K counts user turns, not raw turns. A conversation with 3
    user turns and 5 assistant turns must still surface all 3 user
    turns when K=5 (the LLM does not lose context to assistant
    interleaving)."""
    turns: list[Turn] = []
    for i in range(1, 4):
        turns.append(_user_turn(f"指令 {i}", idx=i))
        turns.append(_assistant_turn(f"回答 {i}", idx=i))
    rendered = build_memory_window(turns, {}, k=5)
    for i in range(1, 4):
        assert f"指令 {i}" in rendered
    for i in range(1, 4):
        assert f"回答 {i}" not in rendered


# ---------------------------------------------------------------------------
# Params truncation (per-line budget)
# ---------------------------------------------------------------------------


def test_large_parameters_dict_is_truncated() -> None:
    """A node with a big `parameters` blob must not blow past the
    per-line budget — clip + ellipsis beats a wall of text that
    steals budget from the rest of the window."""
    big_params = {"text": "x" * 5000}
    plan = _plan(node_count=1, tool="echo", params=big_params)
    turn = _user_turn("echo big", plan_id="p-1", idx=1)
    rendered = build_memory_window([turn], {turn.id: plan}, k=5)
    # The `…` ellipsis marks the clip point; the full 5000-byte
    # value must NOT appear verbatim.
    assert "…" in rendered
    assert "x" * 5000 not in rendered