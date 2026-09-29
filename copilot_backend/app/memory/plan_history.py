"""Plan-history writer seam (T31 / #27, ADR-0007 / ADR-0008).

T31 ships the **write** half of long-term memory: every Plan that
finishes execution gets summarised and pushed into Milvus
`plan_history_vectors`. T32 (#28) will recall from the same collection
when the next Planner call asks "have we done something like this
before?".

The seam is intentionally small and stable — T32 reads from it, and
the production swap-in (`pymilvus`-backed) will replace
`InMemoryMilvusWriter` without changing the wire shape:

* `PlanHistoryRecord` — the value object pushed into Milvus. Carries
  `conversation_id` / `plan_id` / `text` / `vector` per the SPEC
  §Data model and the T31 acceptance criteria. This dataclass is
  the contract between the writer and the future recall code.
* `MilvusPlanHistoryWriter` — the protocol every implementation
  honours. `upsert_summary` is the only entry point.
* `InMemoryMilvusWriter` — the default implementation. Records every
  call. Used by tests and by dev environments that don't have the
  Milvus SDK installed (ADR-0008 explicitly permits this degraded
  path: Milvus is the derived index, MongoDB is the truth).
* `summarize_plan` — pure helper that renders the
  `PlanHistoryRecord.text` from the Plan doc plus the triggering
  user instruction. Pure-function shape means tests pin the exact
  output without any I/O.

`upsert_summary` is async because the real Milvus SDK uses async
gRPC; the in-memory implementation stays trivially synchronous under
the hood but presents the same surface so the swap is transparent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.db.schemas import Plan, PlanNode
from app.memory.embedding import DEFAULT_EMBEDDING_DIM, embed_text

# Per-line cap on parameter serialisation inside the Plan summary.
# A Plan node whose `parameters` dict exceeds this many chars would
# blow past a sane summary budget; clipping + ellipsis keeps the
# `text` field well under the Milvus `VARCHAR(4096)` ceiling. Same
# rationale as `app.planner.memory._MAX_PARAMS_SERIALIZED` (T30):
# the LLM only needs to recognise the shape, not parse every byte.
_MAX_PARAMS_SERIALIZED = 200

# Per-node cap on the rendered `tool(params)` line. Keeps the
# summary stable in length even when a single Plan happens to ship
# a node with a giant parameter set — important because the Milvus
# collection's `text` column has a hard length cap, and a runaway
# line that exceeds it would fail the insert.
_MAX_NODE_LINE = 240


@dataclass(frozen=True, slots=True)
class PlanHistoryRecord:
    """One row pushed into Milvus `plan_history_vectors` (T31 / #27).

    Field-by-field:

    * `plan_id` / `conversation_id` — the foreign-key pointers T32's
      recall code filters by. Both are stored as plain `VARCHAR`s;
      no numeric encoding (Milvus happily indexes strings, and a
      future re-ingest won't need to back-fill integer ids).
    * `text` — the human-readable summary. Pushed verbatim into the
      `text` column; this is also what the embedding was computed
      from, so the vector is *centred on* the same string T32 will
      see in recall.
    * `vector` — the L2-normalised embedding of `text`. The writer
      protocol allows callers to pass an externally-computed vector
      (e.g. a real model in a future ticket); the in-memory default
      computes one via `embed_text` when the caller doesn't supply
      one.
    """

    plan_id: str
    conversation_id: str
    text: str
    vector: list[float] = field(default_factory=list)


@runtime_checkable
class MilvusPlanHistoryWriter(Protocol):
    """Protocol every Plan-history Milvus writer honours.

    The seam exists so the production `pymilvus`-backed writer can
    drop in without touching call sites. `runtime_checkable` lets
    tests use `isinstance(writer, MilvusPlanHistoryWriter)` to verify
    a stub satisfies the contract — useful when a fixture overrides
    the FastAPI dependency.
    """

    async def upsert_summary(self, record: PlanHistoryRecord) -> None:
        """Insert (or update) one summary into Milvus.

        Implementations may treat `plan_id` as the primary key so a
        re-execution of the same Plan replaces the prior row. The
        in-memory default appends, which keeps tests deterministic.
        """
        ...


class InMemoryMilvusWriter:
    """Records calls — the default / dev / test implementation.

    Mirrors the `MilvusPlanHistoryWriter` protocol one-for-one. The
    `records` list is the public read-back seam; tests assert against
    it directly. Production deployments that want the real Milvus SDK
    swap the implementation via the FastAPI dependency override —
    the executor's call site never knows.

    Thread-safety: not required. FastAPI runs request handlers in a
    single event-loop iteration, and tests do the same.
    """

    def __init__(self) -> None:
        self.records: list[PlanHistoryRecord] = []

    async def upsert_summary(self, record: PlanHistoryRecord) -> None:
        """Append `record` to the in-memory store.

        No dedup — the protocol allows but doesn't require
        upsert-by-`plan_id`. Tests that care use the `records` list
        directly; the executor doesn't depend on dedup behaviour.
        """
        self.records.append(record)


def summarize_plan(*, user_instruction: str, plan: Plan) -> str:
    """Render the `text` field of a `PlanHistoryRecord` from a Plan doc.

    The summary is the user's instruction followed by one line per
    node: `<tool>(<params>)`. Same shape the Planner's memory window
    uses (T30 / `build_memory_window`) — keeping the two renderers
    consistent means T32's recall code can paste recalled fragments
    into the prompt without a format mismatch.

    Pure function — no I/O, no LLM, no MongoDB. Tests can pin the
    exact output line-for-line.

    Smalltalk (zero-node Plans, ADR-0004) is filtered at the
    Executor seam, not here — the executor's `_write_plan_history`
    short-circuits before this function ever runs for an empty Plan,
    so `summarize_plan` is never asked to produce a placeholder.
    Keeping the function single-purpose avoids two layers of
    smalltalk-handling logic that have to stay in sync.
    """
    header = (
        f"用户指令: {user_instruction.strip()}"
        if user_instruction.strip()
        else "用户指令: (空)"
    )

    lines: list[str] = [header]
    for node in plan.nodes:
        lines.append(_render_node_line(node))
    return "\n".join(lines)


def _render_node_line(node: PlanNode) -> str:
    """Render one `<tool>(<params>)` line, clipped to `_MAX_NODE_LINE`."""
    params_repr = _truncate_repr(node.parameters, _MAX_PARAMS_SERIALIZED)
    line = f"- {node.tool}({params_repr})"
    if len(line) <= _MAX_NODE_LINE:
        return line
    return line[: _MAX_NODE_LINE - 1] + "…"


def _truncate_repr(value: Any, limit: int) -> str:
    """`repr(value)` clipped to `limit` chars with an ellipsis.

    Same convention as `app.planner.memory._truncate_repr`: JSON
    quoting via `repr` matches the prompt-side contract (`parameters`
    is a JSON object per the LLM contract).
    """
    rendered = repr(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"


def build_plan_history_record(
    *,
    plan: Plan,
    user_instruction: str,
) -> PlanHistoryRecord:
    """Render a Plan into a `PlanHistoryRecord` (text + vector).

    Convenience wrapper around `summarize_plan` + `embed_text` so the
    executor's call site stays one-liner-clean.

    The default embedding dim (`DEFAULT_EMBEDDING_DIM`) matches the
    Milvus collection schema; tests assert against this exact value.
    """
    text = summarize_plan(user_instruction=user_instruction, plan=plan)
    vector = embed_text(text)
    return PlanHistoryRecord(
        plan_id=plan.id,
        conversation_id=plan.conversation_id,
        text=text,
        vector=vector,
    )


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "InMemoryMilvusWriter",
    "MilvusPlanHistoryWriter",
    "PlanHistoryRecord",
    "build_plan_history_record",
    "summarize_plan",
]
