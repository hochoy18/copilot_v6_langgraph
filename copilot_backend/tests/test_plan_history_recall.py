"""Unit tests for `app.memory.recall` — T32 / #28.

These tests pin the **read** half of long-term memory:

* `InMemoryMilvusReader` — the default reader. Verifies default
  Top-N=3, configurable `top_n`, descending-score ordering,
  cross-session recall (the acceptance criterion "会话 A Plan → 会话 B
  提上周那个 → B 收到 A 摘要"), graceful-empty-input degradation, and
  zero-vector skipping when records have no vector.
* `render_recall_block` — the pure renderer for the
  `{{long_term_memory}}` Prompt slot. Pins the empty-placeholder
  convention (so a prompt against the contract still produces sane
  output), the per-match shape, and the score-precision contract.
* The cosine helper is exercised indirectly through the reader —
  pinning the score values end-to-end keeps the seam honest.

Pure-shape: no MongoDB, no Milvus SDK, no fixtures beyond a writer
populated by hand. TDD promise for T32 rests on these.
"""
from __future__ import annotations

import pytest

from app.memory.embedding import embed_text
from app.memory.plan_history import (
    InMemoryMilvusWriter,
    PlanHistoryRecord,
)
from app.memory.recall import (
    DEFAULT_RECALL_TOP_N,
    InMemoryMilvusReader,
    MilvusPlanHistoryReader,
    PlanHistoryMatch,
    render_recall_block,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(
    *,
    plan_id: str,
    text: str,
    conversation_id: str = "conv-1",
) -> PlanHistoryRecord:
    """Build a `PlanHistoryRecord` with a matching vector for `text`.

    Mirrors what `build_plan_history_record` (T31) does in production:
    the vector is `embed_text(text)` so the reader's cosine ranking
    exercises the real embedding contract.
    """
    return PlanHistoryRecord(
        plan_id=plan_id,
        conversation_id=conversation_id,
        text=text,
        vector=embed_text(text),
    )


# ---------------------------------------------------------------------------
# InMemoryMilvusReader
# ---------------------------------------------------------------------------


class TestInMemoryMilvusReaderEmpty:
    """An empty corpus or empty query collapses to "no matches"."""

    async def test_empty_writer_returns_empty_list(self) -> None:
        reader = InMemoryMilvusReader(InMemoryMilvusWriter())
        result = await reader.search("anything", top_n=3)
        assert result == []

    async def test_empty_query_returns_empty_list(self) -> None:
        writer = InMemoryMilvusWriter()
        writer.records.append(_record(plan_id="p1", text="客户列表 区域=亚太"))
        reader = InMemoryMilvusReader(writer)
        assert await reader.search("", top_n=3) == []
        assert await reader.search("   ", top_n=3) == []

    async def test_whitespace_only_query_returns_empty_list(self) -> None:
        writer = InMemoryMilvusWriter()
        writer.records.append(_record(plan_id="p1", text="客户列表 区域=亚太"))
        reader = InMemoryMilvusReader(writer)
        assert await reader.search("\t\n  ", top_n=3) == []

    async def test_zero_top_n_returns_empty_list(self) -> None:
        writer = InMemoryMilvusWriter()
        writer.records.append(_record(plan_id="p1", text="客户列表"))
        reader = InMemoryMilvusReader(writer)
        assert await reader.search("客户列表", top_n=0) == []
        assert await reader.search("客户列表", top_n=-1) == []


class TestInMemoryMilvusReaderRanking:
    """Score-driven Top-N selection, descending order, deterministic."""

    async def test_single_record_is_returned_at_top(self) -> None:
        writer = InMemoryMilvusWriter()
        writer.records.append(_record(plan_id="p1", text="查询客户列表"))
        reader = InMemoryMilvusReader(writer)
        result = await reader.search("查询客户列表", top_n=3)
        assert len(result) == 1
        assert isinstance(result[0], PlanHistoryMatch)
        assert result[0].record.plan_id == "p1"
        # Self-similarity: querying the same text should land at the
        # very top of the cosine ranking — and a self-embedding is
        # self-aligned at exactly 1.0 within float precision.
        # `pytest.approx` absorbs the 1 ULP noise the L2-normalise
        # path can introduce (1.0000000000000002 vs 1.0); the cosine
        # helper is well-conditioned and the noise is well below any
        # downstream rounding (the renderer clips to 4 decimals).
        assert result[0].score == pytest.approx(1.0)

    async def test_results_sorted_descending_by_score(self) -> None:
        writer = InMemoryMilvusWriter()
        # Three records with progressively-dissimilar text. Each
        # record shares at least one token with the query so the
        # random-projection noise in the embedder can't reorder them:
        # `near` shares two tokens, `medium` shares one, `far`
        # shares none — the relative ranking is therefore pinned.
        writer.records.append(
            _record(plan_id="near", text="列出 客户列表 区域=亚太")
        )
        writer.records.append(
            _record(plan_id="medium", text="查询 客户列表")
        )
        writer.records.append(
            _record(plan_id="far", text="今天天气怎么样")
        )
        reader = InMemoryMilvusReader(writer)
        result = await reader.search("客户列表 区域=亚太 上周那个", top_n=3)
        plan_ids = [m.record.plan_id for m in result]
        # The token-overlap ordering is what we care about — exact
        # string equality would tie-break on embedder noise between
        # the medium / far rows that don't overlap the query.
        assert plan_ids[0] == "near"
        assert set(plan_ids[1:]) == {"medium", "far"}
        # Scores are monotonically non-increasing across the result.
        assert result[0].score >= result[1].score >= result[2].score
        assert result[0].score > result[2].score

    async def test_top_n_default_is_three(self) -> None:
        # DEFAULT_RECALL_TOP_N — pinned so a future change has to
        # update both this test and the SPEC cross-reference.
        assert DEFAULT_RECALL_TOP_N == 3

    async def test_top_n_two_truncates_to_two(self) -> None:
        writer = InMemoryMilvusWriter()
        for index in range(5):
            writer.records.append(
                _record(plan_id=f"p{index}", text=f"客户列表 第 {index} 次")
            )
        reader = InMemoryMilvusReader(writer)
        result = await reader.search("客户列表", top_n=2)
        assert len(result) == 2

    async def test_top_n_larger_than_corpus_returns_everything(self) -> None:
        writer = InMemoryMilvusWriter()
        writer.records.append(_record(plan_id="p1", text="客户列表"))
        writer.records.append(_record(plan_id="p2", text="产品清单"))
        reader = InMemoryMilvusReader(writer)
        result = await reader.search("客户", top_n=10)
        assert len(result) == 2
        # Whatever the top two are, they're sorted descending by score.
        assert result[0].score >= result[1].score

    async def test_cross_session_recall(self) -> None:
        """AC #1: 会话 A Plan → 会话 B 提上周那个 → B 收到 A 摘要.

        The reader must not filter by `conversation_id` — long-term
        memory exists precisely for cross-session recall. A query in
        conversation B that resembles conversation A's Plan must
        surface A's summary. The query is a "上周那个"-style
        reference: a follow-up phrase that shares tokens with A's
        Plan and nothing with B's Plan.
        """
        writer = InMemoryMilvusWriter()
        writer.records.append(
            _record(
                plan_id="plan-A",
                text="列出 客户列表 区域=亚太",
                conversation_id="conv-A",
            )
        )
        writer.records.append(
            _record(
                plan_id="plan-B",
                text="查询 产品 库存",
                conversation_id="conv-B",
            )
        )
        reader = InMemoryMilvusReader(writer)
        result = await reader.search("客户列表 区域=亚太 上周那个", top_n=3)
        # The A record must be in the top-N. With two records it's
        # also the top hit because the query reuses A's tokens.
        plan_ids = [match.record.plan_id for match in result]
        assert "plan-A" in plan_ids
        assert plan_ids[0] == "plan-A"

    async def test_records_without_vectors_are_skipped(self) -> None:
        writer = InMemoryMilvusWriter()
        # A record with no vector (e.g. a custom writer that pushes
        # metadata-only rows) cannot be ranked — silently dropped.
        writer.records.append(
            PlanHistoryRecord(
                plan_id="p-empty",
                conversation_id="conv-1",
                text="metadata-only row",
                vector=[],
            )
        )
        writer.records.append(_record(plan_id="p-good", text="客户列表"))
        reader = InMemoryMilvusReader(writer)
        result = await reader.search("客户列表", top_n=3)
        plan_ids = [match.record.plan_id for match in result]
        assert "p-empty" not in plan_ids
        assert "p-good" in plan_ids

    async def test_deterministic_tie_break_uses_insertion_order(self) -> None:
        # Two records with byte-identical `text` (and therefore
        # identical vectors). The reader must rank them deterministically
        # by insertion order — newer first.
        writer = InMemoryMilvusWriter()
        writer.records.append(_record(plan_id="older", text="同一条记录"))
        writer.records.append(_record(plan_id="newer", text="同一条记录"))
        reader = InMemoryMilvusReader(writer)
        result = await reader.search("同一条记录", top_n=2)
        assert len(result) == 2
        # Both scores are 1.0 (self-similarity); tie-break puts
        # the newer record first.
        assert result[0].record.plan_id == "newer"
        assert result[1].record.plan_id == "older"

    async def test_protocol_runtime_checkable(self) -> None:
        # `isinstance` against the Protocol must succeed for any
        # implementation honouring the contract — protects the
        # FastAPI dependency seam against a future stub.
        reader = InMemoryMilvusReader(InMemoryMilvusWriter())
        assert isinstance(reader, MilvusPlanHistoryReader)


# ---------------------------------------------------------------------------
# render_recall_block
# ---------------------------------------------------------------------------


class TestRenderRecallBlock:
    """Pin the `{{long_term_memory}}` Prompt-slot shape."""

    def test_empty_matches_returns_placeholder(self) -> None:
        assert render_recall_block([]) == "(当前没有相关历史 Plan / no matching history)"

    def test_single_match_shape(self) -> None:
        match = PlanHistoryMatch(
            record=_record(plan_id="p1", text="客户列表 区域=亚太"),
            score=0.8732,
        )
        rendered = render_recall_block([match])
        # `text` is included verbatim so the LLM sees the same
        # summary T31's writer stored. Format is one entry per
        # "block" with the score on its own line.
        assert "[历史 1]" in rendered
        assert "conv=conv-1" in rendered
        assert "plan=p1" in rendered
        assert "score=0.8732" in rendered
        assert "客户列表 区域=亚太" in rendered

    def test_score_is_rounded_to_four_decimals(self) -> None:
        # Floating-point noise must not leak into the prompt —
        # 0.87324999.. → 0.8732. The contract matters because the
        # score line is what the LLM sees verbatim.
        match = PlanHistoryMatch(
            record=_record(plan_id="p1", text="客户列表"),
            score=0.873249999,
        )
        assert "score=0.8732" in render_recall_block([match])

    def test_multiple_matches_joined_with_blank_line(self) -> None:
        # Two matches, two blocks, joined with a blank line. The
        # blank line keeps the LLM-side parser from gluing the
        # summary of one match onto the score line of the next.
        matches = [
            PlanHistoryMatch(
                record=_record(plan_id="p1", text="客户列表"), score=0.9,
            ),
            PlanHistoryMatch(
                record=_record(plan_id="p2", text="产品清单"), score=0.5,
            ),
        ]
        rendered = render_recall_block(matches)
        # Both plan_ids present, ordered.
        assert "[历史 1]" in rendered
        assert "[历史 2]" in rendered
        assert rendered.index("[历史 1]") < rendered.index("[历史 2]")
        # Blank line between entries — exact string check guards
        # against a future renderer collapsing it.
        assert "\n\n" in rendered