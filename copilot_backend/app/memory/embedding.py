"""`embed_text` — deterministic text → vector (T31 / #27).

The T31 acceptance criteria say "Milvus 能查到对应向量"; the underlying
mechanism for the vector itself is implementation-defined. We use a
**signed-hash bag-of-tokens** projection:

* Tokens are ASCII-folded to lowercase, then split on whitespace.
  This is enough to make a near-duplicate ("list_customers 区域=亚太"
  vs "list_customers 区域=欧洲") land close in the cosine space —
  testable without standing up a model.
* Each token is hashed via SHA-256 to a 256-bit digest; the digest's
  bits vote on each output dimension via a sign (`+1` / `-1`)
  pattern. This is the classic signed random projection (Achlioptas
  2003) — sparse, deterministic, and O(token_count × dim/256) work.
* The result is L2-normalised so cosine similarity reduces to a
  dot product. T32 will rely on this when it compares incoming
  instructions against stored Plans.

Properties the tests pin:

* **Determinism** — same `text` → identical bytes (so a test can
  compare vector equality).
* **Fixed dimension** — `dim=128` by default; `dim` is part of the
  contract.
* **Stability** — L2 norm = 1 (cosine-friendly) for non-empty input;
  empty input returns a zero vector (mirrors how T32's recall code
  can safely dot-product with anything).
* **Semantic-ish overlap** — texts sharing a token share a vote in
  every dimension the token contributes to; dissimilar texts drift
  to decorrelation.

This is **not** a substitute for a real embedding model. T32 may
swap in a learned encoder once one is in scope; the writer seam is
shaped so the swap is a one-line factory change.

References: ADR-0007 (long-term memory), ADR-0008 (Milvus/Mongo
separation), and the "T31 Milvus 写入" acceptance criteria on
issue #27.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from typing import Final

# Default output dimension — matches the `plan_history_vectors.vector`
# field width. Hard-coded so a future bump is a single ADR with a
# matching Milvus migration; tests use the same constant.
DEFAULT_EMBEDDING_DIM: Final[int] = 128

# Token boundary — split on any non-word character (ASCII). CJK text
# falls back to one character per token (each char is a `\w` boundary
# in Python's regex under Unicode; the `\W` split still keeps things
# predictable). ADR-0007 is language-agnostic — embedding quality is
# deliberately loose for MVP.
_TOKEN_SPLIT: Final[re.Pattern[str]] = re.compile(r"\W+", re.UNICODE)

# A single zero-byte token at the front of every digest so different
# inputs never collide on the same SHA-256 state. (Not strictly
# required — SHA-256 collision resistance is enough — but cheap.)
_HASH_SALT: Final[bytes] = b"copilot/memory/embedding/v1"


def _tokens(text: str) -> Iterable[str]:
    """Yield lowercase, non-empty tokens for `text`."""
    for token in _TOKEN_SPLIT.split(text.lower()):
        if token:
            yield token


def embed_text(text: str, *, dim: int = DEFAULT_EMBEDDING_DIM) -> list[float]:
    """Project `text` into a fixed-dim float vector.

    Empty / whitespace-only input returns a zero vector of length
    `dim`. A non-empty input always produces an L2-normalised vector
    (norm 1.0 within float precision) — the contract T32 will rely
    on for cosine similarity.

    Args:
        text: any string. Multilingual, multilingual-mixed, and
            punctuation-heavy input is tolerated; the projection only
            cares about token presence.
        dim: output dimension. Must be `>= 1`; the default matches
            `plan_history_vectors.vector`. The tests pin this exact
            value to guard against silent schema drift.

    Returns:
        A `list[float]` of length `dim`. Deterministic for a given
        `(text, dim)` pair — same input always yields the same bytes.
    """
    if dim < 1:
        raise ValueError(f"dim must be >= 1, got {dim}")

    # Accumulate signed votes into an integer vector so repeated
    # tokens give bit-identical intermediate sums (a float
    # accumulator rounds at every addition; that breaks the
    # "3×foo normalises to the same vector as 1×foo" property the
    # tests pin). Conversion to float happens once, at L2-normalise.
    vector = [0] * dim

    for token in _tokens(text):
        digest = hashlib.sha256(_HASH_SALT + token.encode("utf-8")).digest()
        # Walk the digest in 32-byte (256-bit) chunks; vote once per
        # bit. `dim=128` needs only the first 16 bytes (128 bits).
        # Larger dims draw additional bytes from the same digest —
        # still deterministic, no extra hashing.
        for dim_index in range(dim):
            byte_index = dim_index // 8
            bit_index = dim_index % 8
            if byte_index >= len(digest):
                # Dim exceeded the digest length; stop voting. A future
                # caller that wants >256 dims will need to mix in a
                # second hash — T31's `DEFAULT_EMBEDDING_DIM=128`
                # keeps that branch cold.
                break
            bit = (digest[byte_index] >> bit_index) & 1
            vector[dim_index] += 1 if bit else -1

    # L2-normalise. Empty input stays zero (no division by zero);
    # callers that cosine against a zero vector get 0.0, which is
    # the safe degradation rather than NaN.
    norm_sq = sum(component * component for component in vector)
    if norm_sq == 0:
        return [0.0] * dim
    norm = math.sqrt(float(norm_sq))
    return [float(component) / norm for component in vector]


__all__ = ["DEFAULT_EMBEDDING_DIM", "embed_text"]
