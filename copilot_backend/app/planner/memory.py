"""`build_memory_window` — the Planner's recent-K-turn context (T30 / #26, ADR-0007).

ADR-0007 splits the Planner's per-turn context into two streams:

* **记忆窗口 (this module)** — the recent K user turns, verbatim,
  with each turn's frozen Plan summarised inline. K is configurable,
  default 5. Rendered straight into the Langfuse `planner` Prompt's
  `{{memory_window}}` slot — the LLM sees the prior conversation
  without signal loss.
* **长期记忆 (Milvus, T31 / T32)** — semantic recall over historical
  Plans and Tool calls; lands on a separate Prompt section once
  those tickets ship.

The acceptance criteria for T30 (#26) are pure-shape and verified at
the unit level here:

* the *n*-th Planner call sees the previous K user turns when the
  conversation has at least K prior turns; older turns are dropped
  (the window slides forward, never grows);
* K is configurable so an operator can widen / narrow it without a
  code change;
* assistant / system turns and turns with no linked Plan do not
  derail the formatter — they degrade gracefully.

Implementation notes worth knowing before editing:

* The window is rendered as a **plain-text block** for the LLM, not
  a structured payload. Same convention as `render_tool_catalog`: the
  Prompt is Langfuse-owned text and we hand it a string. JSON / YAML
  inside the slot would tie the LLM contract to a parser that's
  strictly more fragile than the prose form.
* The window is **last-K-only**: no chronological reversal, no
  summarisation, no extra turns. ADR-0007's "原文进 LLM,信号无损"
  rule forbids both lossy transforms and out-of-order rendering.
* The Plan snapshot for each turn is a **compact summary**, not the
  full frozen doc: tool slug + parameters per node, plus a one-line
  total. The full snapshot already rides on `plans._id` and is
  reachable through audit replay (ADR-0027) — the prompt only needs
  enough for the LLM to recognise "the previous Plan called X with Y".
* An empty window (no prior turns) renders as the documented
  placeholder rather than the literal string "None", so a prompt
  written against the contract still produces a sane sentence.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.db.schemas import Plan, Turn

# Default K — matches the SPEC §记忆窗口 and ADR-0007 floor. Operators
# override via `Settings.memory_window_k`; the constant is the single
# authoritative default so a missing setting never silently widens /
# narrows the window.
DEFAULT_MEMORY_WINDOW_K = 5

# Per-line cap on parameter serialisation inside the snapshot summary.
# A Plan node whose `parameters` dict exceeds this many chars would
# blow past a sane per-line budget; clipping + ellipsis keeps each
# turn on roughly one terminal row.
_MAX_PARAMS_SERIALIZED = 200

# Placeholder string for the "no prior turns" case. Same convention as
# `render_tool_catalog`: the LLM-facing Prompt never sees an empty
# slot — there is always a recognisable string.
_EMPTY_WINDOW_PLACEHOLDER = "(当前没有历史对话 / no prior turns)"


def build_memory_window(
    turns: Iterable[Turn],
    plans_by_turn_id: Mapping[str, Plan | None],
    *,
    k: int,
) -> str:
    """Render the last K user turns (with linked Plan summaries) as a Prompt block.

    Args:
        turns: every Turn in the conversation, **already in chronological
            order** (oldest first). Callers pass the result of
            `TurnRepository.list_by_conversation`, which sorts ascending
            by `created_at`. Non-user turns (assistant / system) and
            turns that fall outside the last K are dropped silently —
            see ADR-0007's "原文进 LLM" rule.
        plans_by_turn_id: lookup of `Plan` rows keyed by `Turn.id` for
            each turn with a `plan_id`. Missing keys degrade to a
            "no Plan" line for that turn rather than raising — the
            window is best-effort and a missing snapshot must not
            abort a Planner call.
        k: how many user turns to include. Must be `>= 1`; values <=0
            collapse to the empty placeholder. The window slides
            forward — older turns past the K-th newest are dropped.

    Returns:
        The rendered text block (always non-empty). Each turn occupies
        one or two lines: the user instruction verbatim, optionally
        followed by a one-line Plan summary.
    """
    if k <= 0:
        return _EMPTY_WINDOW_PLACEHOLDER

    user_turns = [turn for turn in turns if turn.role == "user"]
    recent = user_turns[-k:]
    if not recent:
        return _EMPTY_WINDOW_PLACEHOLDER

    lines: list[str] = []
    for index, turn in enumerate(recent, start=1):
        lines.append(f"[轮次 {index}] 用户: {turn.content}")
        summary = _plan_summary_for(turn, plans_by_turn_id)
        if summary is not None:
            lines.append(f"  本轮 Plan: {summary}")
    return "\n".join(lines)


def _plan_summary_for(
    turn: Turn,
    plans_by_turn_id: Mapping[str, Plan | None],
) -> str | None:
    """One-line summary of the Plan attached to `turn`, or `None`.

    A `None` summary means "no Plan to summarise" — either the turn
    had no `plan_id` (smalltalk / degraded-LLM path, ADR-0004) or the
    lookup did not find the Plan (a deleted / orphaned row). Returning
    `None` lets the caller decide whether to render a Plan line at
    all rather than fudging one with a guess.
    """
    if turn.plan_id is None:
        return None
    plan = plans_by_turn_id.get(turn.id)
    if plan is None:
        return "(本轮未生成 Plan)"

    node_count = len(plan.nodes)
    if node_count == 0:
        return "(本轮 Planner 输出空 Plan, 已与用户闲聊)"

    node_descriptions: list[str] = []
    for node in plan.nodes:
        params_repr = _truncate_repr(node.parameters, _MAX_PARAMS_SERIALIZED)
        node_descriptions.append(f"{node.tool}({params_repr})")
    nodes_part = "; ".join(node_descriptions)
    return f"{node_count} 个节点 — {nodes_part}"


def _truncate_repr(value: Any, limit: int) -> str:
    """`repr(value)` clipped to `limit` chars with an ellipsis.

    Keeping the per-line budget small matters more than round-trip
    fidelity: the LLM only needs to recognise a parameter shape, not
    parse every byte. JSON quoting via `repr` matches the prompt-side
    convention (`parameters` is a JSON object per the contract).
    """
    rendered = repr(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"


__all__ = [
    "DEFAULT_MEMORY_WINDOW_K",
    "build_memory_window",
]