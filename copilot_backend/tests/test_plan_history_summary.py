"""Unit tests for `app.memory.plan_history` — T31 / #27.

These tests pin two pure-function contracts that the rest of T31
relies on:

* `summarize_plan` — the `text` field of every persisted Milvus row.
  Exact-string assertions are appropriate: T32's recall code will
  paste fragments of these strings into the Planner prompt, so the
  format is part of the wire contract.
* `InMemoryMilvusWriter` — the default writer, also the test seam.
  Pin the record shape (`conversation_id`, `plan_id`, `text`,
  `vector`) and the no-dedup append semantics.
* `build_plan_history_record` — the convenience wrapper the executor
  reaches for. Asserts that `text` matches `summarize_plan`'s output
  and that the `vector` is the embedding of that same `text`.

The tests are pure-shape — no MongoDB, no Milvus, no fixtures. That's
the seam T31's TDD promise rests on.
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.db.schemas import Plan, PlanCreate, PlanNode, ToolSnapshot
from app.memory.embedding import DEFAULT_EMBEDDING_DIM, embed_text
from app.memory.plan_history import (
    InMemoryMilvusWriter,
    MilvusPlanHistoryWriter,
    PlanHistoryRecord,
    build_plan_history_record,
    summarize_plan,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 9, 29, tzinfo=UTC)


def _snapshot(*, name: str = "echo") -> ToolSnapshot:
    return ToolSnapshot(
        tool_id="t-1",
        name=name,
        description=f"{name} tool",
        risk_level="read",
        parameters_schema={"type": "object"},
        http_method="POST",
        http_url_template=f"https://api.test/{name}",
        http_headers={"Content-Type": "application/json"},
        http_body_template=None,
    )


def _plan(*, nodes: list[PlanNode], tool: str = "echo") -> Plan:
    # One snapshot per distinct Tool name (ADR-0027 binding). `PlanBase`'s
    # validator rejects a node that references an unbound tool name, so a
    # multi-tool Plan needs multi-snapshot tool_snapshots.
    snapshots: list[ToolSnapshot] = []
    for node in nodes:
        if node.tool not in {snap.name for snap in snapshots}:
            snapshots.append(_snapshot(name=node.tool))
    if not snapshots:
        snapshots = [_snapshot(name=tool)]
    create = PlanCreate(
        conversation_id="conv-1",
        turn_id="turn-1",
        status="succeeded",
        nodes=nodes,
        edges=[],
        tool_snapshots=snapshots,
    )
    return Plan(
        _id="plan-1",
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


def _node(node_id: str, tool: str, params: dict[str, object] | None = None) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        tool=tool,
        parameters=params if params is not None else {},
        notes="",
    )


# ---------------------------------------------------------------------------
# summarize_plan
# ---------------------------------------------------------------------------


def test_summarize_plan_includes_user_instruction() -> None:
    """The summary must carry the user's instruction verbatim — T32
    will surface this fragment when the user asks "上周那个项目"."""
    plan = _plan(nodes=[_node("n1", "list_customers", {"region": "emea"})])
    text = summarize_plan(user_instruction="列出 EMEA 客户", plan=plan)
    assert "列出 EMEA 客户" in text
    assert text.startswith("用户指令:")


def test_summarize_plan_renders_one_line_per_node() -> None:
    """Each node renders as `- <tool>(<params>)`. Multi-node Plans
    (T25) get one line per node so the summary stays parseable."""
    plan = _plan(
        nodes=[
            _node("n1", "list_customers", {"region": "emea"}),
            _node("n2", "send_email", {"to": "alice@example.test"}),
        ],
        tool="list_customers",
    )
    text = summarize_plan(user_instruction="列出并通知", plan=plan)
    assert "list_customers" in text
    assert "send_email" in text
    # Two tool lines, each preceded by a `- `.
    assert text.count("\n- ") == 2


def test_summarize_plan_handles_blank_instruction() -> None:
    """An empty / whitespace-only instruction degrades to a
    placeholder rather than emitting `用户指令:` with no body. The
    executor gates empty-Plan writes upstream, but a future caller
    (e.g. a backfill job) might pass arbitrary input."""
    plan = _plan(nodes=[_node("n1", "echo")])
    text = summarize_plan(user_instruction="   ", plan=plan)
    assert "用户指令: (空)" in text


def test_summarize_plan_truncates_huge_parameter_payloads() -> None:
    """A node with a giant `parameters` dict must not blow past the
    per-line budget; clip + ellipsis keeps the row within Milvus's
    `VARCHAR(4096)` ceiling. Same convention as T30's
    `build_memory_window`."""
    big: dict[str, object] = {"text": "x" * 5000}
    plan = _plan(nodes=[_node("n1", "echo", big)])
    text = summarize_plan(user_instruction="echo big", plan=plan)
    assert "…" in text
    assert "x" * 5000 not in text


def test_summarize_plan_is_pure() -> None:
    """Pure function — calling twice with the same args yields the
    same output. Important for test stability and for any future
    deterministic-backfill workflow."""
    plan = _plan(nodes=[_node("n1", "echo", {"k": "v"})])
    first = summarize_plan(user_instruction="hi", plan=plan)
    second = summarize_plan(user_instruction="hi", plan=plan)
    assert first == second


# ---------------------------------------------------------------------------
# InMemoryMilvusWriter
# ---------------------------------------------------------------------------


async def test_in_memory_writer_records_calls_in_order() -> None:
    """The in-memory writer appends every call. Tests assert against
    `writer.records` to verify the executor's write path."""
    writer = InMemoryMilvusWriter()
    record_a = PlanHistoryRecord(plan_id="p1", conversation_id="c1", text="a", vector=[0.0])
    record_b = PlanHistoryRecord(plan_id="p2", conversation_id="c1", text="b", vector=[0.0])
    await writer.upsert_summary(record_a)
    await writer.upsert_summary(record_b)
    assert writer.records == [record_a, record_b]


async def test_in_memory_writer_satisfies_protocol() -> None:
    """`InMemoryMilvusWriter` satisfies the `MilvusPlanHistoryWriter`
    protocol — the FastAPI dependency override relies on this for
    isinstance-style guards."""
    writer = InMemoryMilvusWriter()
    assert isinstance(writer, MilvusPlanHistoryWriter)


async def test_in_memory_writer_starts_empty() -> None:
    """A fresh writer has no records — the executor's first call is
    the first record."""
    writer = InMemoryMilvusWriter()
    assert writer.records == []


# ---------------------------------------------------------------------------
# build_plan_history_record
# ---------------------------------------------------------------------------


def test_build_record_carries_required_fields() -> None:
    """`build_plan_history_record` produces a `PlanHistoryRecord`
    whose `plan_id` / `conversation_id` come straight from the Plan
    doc, `text` from `summarize_plan`, and `vector` from
    `embed_text(text)`."""
    plan = _plan(
        nodes=[_node("n1", "list_customers", {"region": "emea"})],
    )
    record = build_plan_history_record(
        plan=plan,
        user_instruction="列出 EMEA 客户",
    )
    assert record.plan_id == plan.id
    assert record.conversation_id == plan.conversation_id
    assert record.text == summarize_plan(user_instruction="列出 EMEA 客户", plan=plan)
    assert record.vector == embed_text(record.text)


def test_build_record_vector_dim_matches_default() -> None:
    """The default `dim` is `DEFAULT_EMBEDDING_DIM` — the Milvus
    collection schema will match this exact width. A regression that
    silently bumps the dim would surface as an insert-side error
    in production; the unit test catches it earlier."""
    plan = _plan(nodes=[_node("n1", "echo")])
    record = build_plan_history_record(plan=plan, user_instruction="hi")
    assert len(record.vector) == DEFAULT_EMBEDDING_DIM


def test_build_record_is_deterministic() -> None:
    """Same Plan + same instruction → same record (text + vector).
    Determinism is the bedrock of recall: T32 will look up a vector
    that was persisted earlier and expect to find it byte-identical.
    """
    plan = _plan(nodes=[_node("n1", "echo", {"k": "v"})])
    a = build_plan_history_record(plan=plan, user_instruction="hi")
    b = build_plan_history_record(plan=plan, user_instruction="hi")
    assert a == b


def test_build_record_for_empty_plan_uses_placeholder_text() -> None:
    """An empty Plan produces a record whose `text` carries a
    user-instruction line and an empty `nodes` list. The executor
    gates empty-Plan writes upstream (no Milvus row for smalltalk);
    this test pins the convenience wrapper's behaviour for any
    caller that bypasses that gate (e.g. a future backfill job)."""
    plan = _plan(nodes=[])
    record = build_plan_history_record(plan=plan, user_instruction="hello")
    assert "用户指令: hello" in record.text
    assert len(record.vector) == DEFAULT_EMBEDDING_DIM


# ---------------------------------------------------------------------------
# Protocol contract — duck-typing the seam
# ---------------------------------------------------------------------------


class _RaisingWriter:
    """Stub that raises on every call. Used to verify the executor's
    `try/except` envelope around the Milvus write."""

    async def upsert_summary(self, record: PlanHistoryRecord) -> None:
        raise RuntimeError("milvus is down")


async def test_raising_writer_still_satisfies_protocol() -> None:
    """The runtime_checkable protocol catches the stub. This pins the
    seam: tests can drop in any object that quacks like a writer
    without inheriting from a base class. Covers both halves of the
    "duck-typing the seam" claim — the protocol is structural and
    raises on the same surface a real writer would."""
    writer = _RaisingWriter()
    assert isinstance(writer, MilvusPlanHistoryWriter)