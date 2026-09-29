"""Long-term memory seam (ADR-0007 / ADR-0008).

T31 (#27) shipped the *write* half: `PlanExecutor` funnels terminal
Plans into the `plan_history_vectors` Milvus collection. T32 (#28)
ships the *read* half: the Planner asks the reader for the Top-N
most-similar historical Plan summaries and renders them into the
`{{long_term_memory}}` Prompt section.

The seam is best-effort by design (ADR-0008 "Milvus 重建不影响业务"):
a Milvus write or recall failure must never unwind a Plan execution
or a Planner call. The writer contract is `upsert_summary(record)`;
the reader contract is `search(query, *, top_n)`. The production
swap-in is a `pymilvus`-backed pair that lands in a dedicated SDK
ticket; for T31/T32 the default is the in-memory recorder pair —
same surface, recorded calls.
"""
from __future__ import annotations

from app.memory.embedding import DEFAULT_EMBEDDING_DIM, embed_text
from app.memory.plan_history import (
    InMemoryMilvusWriter,
    MilvusPlanHistoryWriter,
    PlanHistoryRecord,
    summarize_plan,
)
from app.memory.recall import (
    DEFAULT_RECALL_TOP_N,
    InMemoryMilvusReader,
    MilvusPlanHistoryReader,
    PlanHistoryMatch,
    render_recall_block,
)

__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "DEFAULT_RECALL_TOP_N",
    "InMemoryMilvusReader",
    "InMemoryMilvusWriter",
    "MilvusPlanHistoryReader",
    "MilvusPlanHistoryWriter",
    "PlanHistoryMatch",
    "PlanHistoryRecord",
    "embed_text",
    "render_recall_block",
    "summarize_plan",
]