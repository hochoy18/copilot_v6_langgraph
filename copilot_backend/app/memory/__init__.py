"""Long-term memory seam (ADR-0007 / ADR-0008).

T31 (#27) ships the *write* half: `PlanExecutor` funnels terminal
Plans into the `plan_history_vectors` Milvus collection so T32 (#28)
can recall them by semantic similarity. The seam is stable enough to
be the read side's input contract — recall lands in a follow-up.

The seam is best-effort by design (ADR-0008 "Milvus 重建不影响业务"):
a Milvus write failure must never unwind a Plan execution. The
writer contract is `upsert_summary(record)`; the production swap-in
is a `pymilvus`-backed implementation that lands alongside T32 (or
in a dedicated SDK ticket). For T31 the default implementation is
`InMemoryMilvusWriter` — same surface, recorded calls.
"""
from __future__ import annotations

from app.memory.embedding import DEFAULT_EMBEDDING_DIM, embed_text
from app.memory.plan_history import (
    InMemoryMilvusWriter,
    MilvusPlanHistoryWriter,
    PlanHistoryRecord,
    summarize_plan,
)

__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "InMemoryMilvusWriter",
    "MilvusPlanHistoryWriter",
    "PlanHistoryRecord",
    "embed_text",
    "summarize_plan",
]
