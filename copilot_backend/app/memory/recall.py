"""Plan-history read seam (T32 / #28, ADR-0007 / ADR-0008).

T31 (#27) shipped the **write** half of long-term memory: every Plan
that finishes execution lands one row in Milvus `plan_history_vectors`.
T32 (#28) is the **read** half — the Planner asks "have we done
something like this before?" and gets Top-N most-similar Plan
summaries back, rendered into the `{{long_term_memory}}` Prompt slot.

The seam mirrors T31's writer one-for-one, on purpose — a future
`pymilvus`-backed reader drops in without touching call sites:

* `PlanHistoryMatch` — a `PlanHistoryRecord` plus the cosine score the
  match was returned at. The score is informational: the renderer
  surfaces it in the Prompt so the LLM can rank fragments; tests pin
  the descending-score ordering the search must honour.
* `MilvusPlanHistoryReader` — the protocol every implementation
  honours. `search(query, *, top_n)` is the only entry point.
* `InMemoryMilvusReader` — the default implementation. Walks the
  `InMemoryMilvusWriter`'s records list, embeds the query with the
  same `embed_text` the writer uses, ranks by cosine similarity, and
  returns the Top-N. Used by tests and by deployments that haven't
  installed the Milvus SDK (ADR-0008 explicitly permits this
  degraded path: Milvus is the derived index, MongoDB is the truth).
* `render_recall_block` — pure helper that renders the matches into
  the `{{long_term_memory}}` Prompt section. Same shape as
  `build_memory_window`'s renderer (T30) so the LLM sees consistent
  text-block formatting for both memory streams.

`search` is async because the real Milvus SDK uses async gRPC; the
in-memory implementation stays trivially synchronous under the hood
but presents the same surface so the swap is transparent.

The cross-session scenario (acceptance criterion #1 — "会话 A Plan →
会话 B 提上周那个 → B 收到 A 摘要") falls out of the design naturally:
the reader does not filter by `conversation_id` because the whole
point of long-term memory is *cross-session* recall. A conversation
filter would defeat the seam; the only filters Milvus indexes are the
ones an operator adds at the collection level (out of scope here).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.memory.embedding import embed_text
from app.memory.plan_history import (
    InMemoryMilvusWriter,
    PlanHistoryRecord,
)

# Default Top-N — matches the SPEC §长期记忆 floor and the T32
# acceptance criterion "Top-N 默认 3 可配置". Operators override via
# `Settings.memory_recall_top_n`; the constant is the single
# authoritative default so a missing setting never silently widens /
# narrows recall.
DEFAULT_RECALL_TOP_N = 3

# Placeholder string for the "no matches" case. Same convention as
# `build_memory_window` (T30): the LLM-facing Prompt never sees an
# empty slot — there is always a recognisable string. Distinct from
# the window placeholder so a debug log can tell which memory stream
# came up empty without parsing the rest of the Prompt.
_EMPTY_RECALL_PLACEHOLDER = "(当前没有相关历史 Plan / no matching history)"


@dataclass(frozen=True, slots=True)
class PlanHistoryMatch:
    """One hit returned by `MilvusPlanHistoryReader.search`.

    Carries the original `record` (so the renderer can paste `text`,
    `plan_id`, and `conversation_id` straight into the Prompt) plus the
    `score` the reader returned it at. `score` is the cosine
    similarity in `[-1.0, 1.0]` — `embed_text` L2-normalises every
    non-empty vector, so cosine is a plain dot product.

    `score` is informational; the renderer surfaces it inline so the
    LLM can see how confident the recall was without forcing it to
    rank blindly. Tests assert descending-by-score ordering across the
    returned list.
    """

    record: PlanHistoryRecord
    score: float


@runtime_checkable
class MilvusPlanHistoryReader(Protocol):
    """Protocol every Plan-history Milvus reader honours.

    The seam exists so the production `pymilvus`-backed reader can
    drop in without touching call sites. `runtime_checkable` lets
    tests use `isinstance(reader, MilvusPlanHistoryReader)` to verify
    a stub satisfies the contract — useful when a fixture overrides
    the FastAPI dependency.
    """

    async def search(
        self,
        query: str,
        *,
        top_n: int,
    ) -> list[PlanHistoryMatch]:
        """Return the Top-N most-similar historical Plan summaries.

        Implementations are free to choose their similarity function;
        the in-memory default uses cosine on the `embed_text` vectors
        so the ranking is deterministic for a given query + corpus.
        Returns `[]` when the corpus is empty or every score falls
        below the implementation's floor — the renderer treats that
        as "no relevant history" and surfaces the placeholder.
        """
        ...


class InMemoryMilvusReader:
    """Reads from an `InMemoryMilvusWriter` — the default / dev / test implementation.

    Mirrors the `MilvusPlanHistoryReader` protocol one-for-one. The
    `writer` reference is the shared backing store: T31's executor
    pushes records into `writer.records`, T32's planner service
    queries the same records through this reader. Production
    deployments that want the real Milvus SDK swap the implementation
    via the FastAPI dependency override — neither call site knows.

    Thread-safety: not required. FastAPI runs request handlers in a
    single event-loop iteration, and tests do the same.
    """

    def __init__(self, writer: InMemoryMilvusWriter) -> None:
        self._writer = writer

    async def search(
        self,
        query: str,
        *,
        top_n: int,
    ) -> list[PlanHistoryMatch]:
        """Return the Top-N records ranked by cosine similarity.

        Empty / whitespace-only `query` returns `[]` — the embedder
        produces a zero vector, and dot-producting it against any
        L2-normalised vector is 0.0, which would otherwise rank every
        record identically. The renderer treats `[]` as "no relevant
        history".

        `top_n <= 0` collapses to `[]` — a degenerate configuration
        must not crash the seam; the caller renders the placeholder.

        Results are sorted **descending by `score`**, then **descending
        by insertion order** as a deterministic tie-break (insertion
        order == the order records landed in `writer.records`, which
        is also chronological for a healthy executor). Tests pin both
        axes — equal-score ties cannot drift between runs.
        """
        if top_n <= 0:
            return []
        if not query or not query.strip():
            return []

        query_vector = embed_text(query)
        # `embed_text` returns the zero vector for empty input. The
        # guard above catches that case for the literal-empty-input
        # path; we re-check the vector itself so a future embedder
        # swap that loses the empty-string short-circuit still
        # degrades to "no matches" rather than ranking everything.
        if _is_zero_vector(query_vector):
            return []

        scored: list[tuple[float, int, PlanHistoryRecord]] = []
        for index, record in enumerate(self._writer.records):
            if not record.vector:
                # A row without a vector cannot be ranked. This is the
                # expected shape for records produced by the default
                # writer (always populated by `build_plan_history_record`)
                # but a future custom writer that pushes metadata-only
                # rows would land here. Silently skipped — the seam
                # is best-effort.
                continue
            score = _cosine_similarity(query_vector, record.vector)
            scored.append((score, index, record))

        # Sort by score descending; tie-break by **insertion index
        # descending** so a more-recently-written record wins on equal
        # score. The intuition matches the "上周那个" reference — when
        # two Plans are equally similar to the current instruction,
        # the newer one is more likely the one the user means. Python's
        # `sort` is stable, but explicit tie-break keys keep the order
        # deterministic across runs even if a future refactor changes
        # the comparator or the corpus size.
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        top = scored[:top_n]
        return [
            PlanHistoryMatch(record=record, score=score)
            for score, _, record in top
        ]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity over two equally-sized float vectors.

    Both `a` and `b` are L2-normalised (`embed_text`'s contract), so
    cosine reduces to a dot product. The full formula is kept anyway
    — a future reader that ingests an unnormalised vector (e.g. one
    written by a custom writer) still gets the right answer without
    a silent cap.

    Returns `0.0` for empty vectors, mismatched lengths, or zero
    norm on either side — same safe-degradation rule as `embed_text`.
    """
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    if dot == 0.0:
        return 0.0
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _is_zero_vector(vector: list[float]) -> bool:
    """True iff every component is exactly 0.0.

    `embed_text` returns a zero vector for empty / whitespace-only
    input. Tests assert the contract; this helper exists so the
    reader can guard on it without re-implementing the check.
    """
    return all(component == 0.0 for component in vector)


def render_recall_block(matches: list[PlanHistoryMatch]) -> str:
    """Render the Top-N matches into the `{{long_term_memory}}` Prompt slot.

    Empty `matches` returns the documented placeholder so the prompt
    never presents an empty memory section silently. One match per
    line: `[历史 N] conv=<conversation_id> plan=<plan_id> score=<score>\
        <text>` — the score gives the LLM a confidence signal without
    forcing it to parse the `text`, and the conversation / plan ids
    give it a way to ask follow-up questions when it can't resolve
    the reference ("上周那个" → "哪次会话?"). The `text` field is the
    same one T31's `summarize_plan` rendered, so the LLM sees
    consistent formatting across the memory window and the recall
    block.

    Pure function — no I/O, no LLM, no MongoDB. Tests can pin the
    exact output line-for-line, including the empty-placeholder case.
    """
    if not matches:
        return _EMPTY_RECALL_PLACEHOLDER
    lines: list[str] = []
    for index, match in enumerate(matches, start=1):
        score_text = f"{match.score:.4f}"
        lines.append(
            f"[历史 {index}] conv={match.record.conversation_id} "
            f"plan={match.record.plan_id} score={score_text}\n"
            f"{match.record.text}"
        )
    return "\n\n".join(lines)


__all__ = [
    "DEFAULT_RECALL_TOP_N",
    "InMemoryMilvusReader",
    "MilvusPlanHistoryReader",
    "PlanHistoryMatch",
    "render_recall_block",
]