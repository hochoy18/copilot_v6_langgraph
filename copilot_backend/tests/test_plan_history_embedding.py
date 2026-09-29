"""Unit tests for `app.memory.embedding.embed_text` — T31 / #27.

The embedder is the only piece of T31 that has zero MongoDB / Milvus
state — every property it needs to satisfy can be tested without any
fixture standing up. These tests pin the contract T32's recall code
will lean on:

* **Determinism** — same input → same vector (so the persisted
  `PlanHistoryRecord.vector` is reproducible across runs).
* **Fixed dimension** — output length is `DEFAULT_EMBEDDING_DIM`,
  matching the Milvus collection schema we'll create alongside
  the SDK swap-in.
* **L2 normalisation** — cosine similarity reduces to a dot product,
  which is what every ANN search index expects.
* **Bag-of-tokens semantics** — texts sharing tokens vote similarly
  in the projection, so semantic-ish recall is plausible.

The values pinned here are deliberately conservative (5 decimals) so
a future bump to a learned encoder doesn't accidentally pass these
checks via floating-point drift.
"""
from __future__ import annotations

import math

from app.memory.embedding import DEFAULT_EMBEDDING_DIM, embed_text

# ---------------------------------------------------------------------------
# Shape contract
# ---------------------------------------------------------------------------


def test_default_dim_is_documented_value() -> None:
    """The default dim is the value the Milvus collection schema will
    use — a silent change here would force a schema migration. Pin
    it at the unit level so the wire / write sides stay in lockstep.
    """
    assert DEFAULT_EMBEDDING_DIM == 128


def test_output_length_matches_dim() -> None:
    """Every call returns exactly `dim` floats.

    The Milvus collection's `vector` field is dimension-bound; a
    mismatched length raises a `ParamError` on insert. The embedder
    is the upstream gate.
    """
    vector = embed_text("hello world")
    assert len(vector) == DEFAULT_EMBEDDING_DIM
    assert all(isinstance(component, float) for component in vector)


def test_custom_dim_is_respected() -> None:
    """`dim` is part of the public surface — a different dim produces
    a different-length vector."""
    assert len(embed_text("hello", dim=64)) == 64
    assert len(embed_text("hello", dim=256)) == 256


def test_invalid_dim_raises() -> None:
    """`dim < 1` is rejected at the seam so a misconfigured caller
    fails loudly (rather than producing a zero-vector that drifts
    silently into the index)."""
    import pytest

    with pytest.raises(ValueError):
        embed_text("anything", dim=0)
    with pytest.raises(ValueError):
        embed_text("anything", dim=-1)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_input_yields_identical_vector() -> None:
    """Determinism is the bedrock of the contract — T32 will compare
    vectors by exact-byte equality in the test seam and by cosine
    similarity in the recall code. Either path needs reproducibility.
    """
    first = embed_text("list_customers region=emea")
    second = embed_text("list_customers region=emea")
    assert first == second


def test_case_insensitive_tokenisation() -> None:
    """Case-folding happens before hashing so the same logical token
    contributes the same votes regardless of how the LLM wrote it.
    Without this, a Planner that emits `List_Customers` and a future
    Plan that emits `list_customers` would drift apart in the index
    — defeating the recall seam.
    """
    assert embed_text("list_customers region=emea") == embed_text(
        "LIST_CUSTOMERS REGION=EMEA"
    )


def test_punctuation_is_stripped_before_tokenisation() -> None:
    """The tokeniser splits on `\W` (non-word characters), so
    punctuation never enters the hash bag. `hello-world` and
    `hello world` tokenise to the same pair (`hello`, `world`) and
    therefore embed to the same vector.

    Pinning this contract means a future change to the regex (e.g.
    to handle CJK without losing punctuation semantics) fails loudly
    rather than silently drifting the index. The downstream effect
    is that punctuation is *not* a recall signal — fine for MVP.
    """
    assert embed_text("hello world") == embed_text("hello-world")
    assert embed_text("hello, world!") == embed_text("hello world")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_non_empty_vector_is_l2_normalised() -> None:
    """L2 norm is 1.0 within float precision for any non-empty input.

    Cosine similarity reduces to a dot product on unit vectors; T32's
    recall code will rely on this. A non-normalised vector would make
    the cosine measure length-sensitive — meaningless for retrieval.
    """
    vector = embed_text("list_customers region=emea")
    norm = math.sqrt(sum(component * component for component in vector))
    assert round(norm, 6) == 1.0


def test_empty_input_returns_zero_vector() -> None:
    """Empty / whitespace-only input returns a zero vector (no
    division by zero). A zero vector cosine-scores to 0.0 against
    anything, which is the safe degradation rather than NaN."""
    assert embed_text("") == [0.0] * DEFAULT_EMBEDDING_DIM
    assert embed_text("   \n\t  ") == [0.0] * DEFAULT_EMBEDDING_DIM


def test_vector_values_are_bounded() -> None:
    """Each component is bounded by `[-1, 1]` because the vector is
    L2-normalised. Pinning the bound catches a regression where the
    normaliser is dropped (e.g. someone replaces the final divide
    with a no-op) before the bug leaks into the persisted index.
    """
    vector = embed_text("anything goes here " * 32)
    for component in vector:
        assert -1.0 <= component <= 1.0


# ---------------------------------------------------------------------------
# Semantic-ish behaviour
# ---------------------------------------------------------------------------


def test_overlapping_texts_score_high_similarity() -> None:
    """Two texts that share most tokens land close in cosine space.

    The exact threshold here is loose — the projection is bag-of-
    tokens, so `region=emea` and `region=apac` share two of three
    tokens (after splitting on `\W`). We just want the cosine to
    clearly exceed the orthogonal baseline; the test pins a number
    that is comfortably above zero so a regression to "all vectors
    look the same" fails loudly.
    """
    a = embed_text("list_customers region=emea")
    b = embed_text("list_customers region=apac")
    cosine = sum(ai * bi for ai, bi in zip(a, b, strict=True))
    assert cosine > 0.3, f"expected overlap, got cosine={cosine}"


def test_unrelated_texts_decorrelate() -> None:
    r"""Texts that share no meaningful tokens decorrelate — cosine
    stays close to zero. The projection isn't a true random
    projection in the statistical sense (256 bits per hash, dim
    may exceed 256), so the cosine is bounded away from exact 0;
    pin the bound so we don't silently regress into "everything
    looks similar"."""
    a = embed_text("alpha beta gamma delta")
    b = embed_text("opqr stuv wxyz mnop")
    cosine = sum(ai * bi for ai, bi in zip(a, b, strict=True))
    # Loose bound — projection noise keeps this comfortably below 0.3
    # for the chosen inputs. If a future change to the hash widens
    # the bound, the test will fail loudly and force the change to
    # be a deliberate decision.
    assert abs(cosine) < 0.3


def test_repeated_token_reinforces_same_direction() -> None:
    """Bag-of-tokens semantics: repeating the same token in the
    input accumulates votes in the same direction. The output vector
    for `foo foo foo` is the normalised version of the unnormalised
    accumulator for a single `foo`, scaled up by 3 (each token votes
    identically, then the L2 norm rescales). The normalised result
    is mathematically identical to the single-token result — the
    accumulator is integer-typed to keep the sum exact, and only the
    L2-normalise step rounds.

    The assertion uses a per-component tolerance of 1e-9 because the
    IEEE-754 `sqrt(N)` and `sqrt(9*N)/3` paths differ by up to 1 ULP
    for the dim=128 default — the value is still well below the
    retrieval-precision floor T32 will work at.
    """
    single = embed_text("foo")
    repeated = embed_text("foo foo foo")
    assert len(single) == len(repeated)
    for s, r in zip(single, repeated, strict=True):
        assert abs(s - r) < 1e-9, f"component drifted: {s!r} vs {r!r}"
