"""`ToolService` — T12 / #11, T34 / #30.

The admin Tool CRUD endpoints (ADR-0031) live in `app.api.admin_tools`;
the repository is `app.repositories.tools`. This module sits between
them and owns the rules that aren't pure Mongo:

* **List filter + search.** The repository exposes `list_all` /
  `list_active` / `list_by_status`. The admin Registry UI (T13) needs
  `status` and `risk_level` filters plus a free-text `q` against the
  Tool `name` / `description`. Doing the filter here (not in the
  repo) lets the repository stay generic across any future caller.
* **Status transitions.** Per ADR-0018 the Tool lifecycle is
  `draft → active → disabled`. The repository already provides
  `set_status` for the atomic write; this service treats every
  status change as an explicit event so the eventual audit-log hook
  (T42) has a single seam to subscribe to.
* **Schema enforcement (T34 / #30, ADR-0020).** Tools must carry a
  usable JSON Schema for their parameters. `ToolService.create` /
  `update` reject empty / malformed `parameters_schema` values
  *before* the row reaches Mongo so the Worker's snapshot-binding
  can never be presented with a non-validatable schema.

Design notes:

* No cross-tenant or ownership logic — Tools are global to the
  registry (admin-only resource per ADR-0002). Any future per-tenant
  scoping lands in the service seam.
* Search is a substring match on `name` and `description` rather
  than a tokenised full-text search. The Registry UI needs "type a
  fragment, see results" semantics; a Mongo `$regex` covers the MVP
  case without dragging in an extra index.
"""
from __future__ import annotations

import re

from jsonschema import Draft202012Validator, SchemaError

from app.db.schemas import (
    Tool,
    ToolCreate,
    ToolRiskLevel,
    ToolStatus,
    ToolUpdate,
)
from app.repositories.tools import ToolRepository
from app.tools.errors import ToolSchemaInvalidError

# Cap on list results per call. Matches the conversation list default
# (T10 / #40) so the Frontend has a uniform pagination story; admin
# tools rarely exceed a few hundred rows so the ceiling is mostly
# defensive.
DEFAULT_LIST_LIMIT: int = 50
MAX_LIST_LIMIT: int = 200


class ToolService:
    """Admin Tool CRUD with list-filter semantics — T12 / #11.

    Holds a `ToolRepository` reference; stateless beyond that. One
    instance per request is fine (the service holds no I/O buffers).
    """

    def __init__(self, *, tool_repository: ToolRepository) -> None:
        self._tools = tool_repository

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, *, data: ToolCreate) -> Tool:
        """Insert a manually-registered Tool.

        Defaults to `status='draft'` per ADR-0018 — the LLM Planner
        cannot see it until an admin reviews and activates. The
        repository stamps `created_at` / `updated_at` and surfaces a
        `DuplicateKeyError` if `name` collides.

        Per ADR-0020 the `parameters_schema` is enforced *here*, not
        in the repository — once a row lands in Mongo the schema is
        frozen and the Worker's snapshot-binding makes the
        missing-schema condition sticky for every Plan that captures
        it. Rejecting at the seam keeps the registry's invariants in
        one place.
        """
        _enforce_parameters_schema(data.parameters_schema)
        return await self._tools.create(data)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def list_tools(
        self,
        *,
        status: ToolStatus | None = None,
        risk_level: ToolRiskLevel | None = None,
        q: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[Tool]:
        """List Tools with optional status / risk-level / text filters.

        `q` is a substring search against `name` and `description`,
        case-insensitive. Empty / whitespace-only `q` is treated as
        "no search". The result is sorted by `name` (the repository's
        default) so the Registry UI renders a stable order across
        calls.
        """
        limit = _clamp_limit(limit)
        needle = (q or "").strip()
        if not needle:
            rows = await self._load_candidates(status=status)
            if risk_level is not None:
                rows = [t for t in rows if t.risk_level == risk_level]
            return rows[:limit]
        # Text-search branch — repository has no native `$regex` helper
        # because the rest of the codebase filters by exact equality.
        # Pulling the candidate set from the index-backed status
        # filter (when present) keeps the in-memory scan bounded by
        # either status or risk_level rather than the full registry.
        candidates = await self._load_candidates(status=status)
        if risk_level is not None:
            candidates = [t for t in candidates if t.risk_level == risk_level]
        pattern = _escape_regex(needle)
        matches = [
            t
            for t in candidates
            if pattern.search(t.name)
            or (t.description and pattern.search(t.description))
        ]
        return matches[:limit]

    async def get_tool(self, tool_id: str) -> Tool:
        """Fetch a single Tool by id. Raises `NotFoundError`."""
        return await self._tools.get(tool_id)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(self, *, tool_id: str, patch: ToolUpdate) -> Tool:
        """Apply a partial update.

        Per the T12 acceptance criteria the admin uses this to revise
        description, status, and risk_level. The repository method
        bumps `updated_at` on every call so an "empty" PATCH still
        tells the audit log "someone touched this Tool".

        Schema enforcement (T34 / #30) only fires when the PATCH
        actually carries `parameters_schema` — a `None` value (field
        not touched) keeps the existing schema intact.
        """
        if patch.parameters_schema is not None:
            _enforce_parameters_schema(patch.parameters_schema)
        return await self._tools.update(tool_id, patch)

    async def set_status(self, *, tool_id: str, status: ToolStatus) -> Tool:
        """Atomic status transition. Bumps `updated_at`.

        Per ADR-0018 the lifecycle is `draft → active → disabled`.
        The repository accepts any target value; this service keeps
        the same permissive contract so admins can re-enable a
        disabled Tool or roll a Tool back to draft. If a future ticket
        tightens the lifecycle (e.g. disabled becomes terminal), the
        guard lands here.
        """
        return await self._tools.set_status(tool_id, status)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_candidates(
        self, *, status: ToolStatus | None,
    ) -> list[Tool]:
        """Pull the index-backed candidate set for list filtering.

        When `status` is provided we hit `list_by_status` so the
        query uses the `by_status` index; otherwise `list_all`. Both
        branches return rows sorted by `name` (the repository's
        default), keeping the in-memory post-filter ordering stable
        across calls.
        """
        if status is not None:
            return await self._tools.list_by_status(status)
        return await self._tools.list_all()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clamp_limit(value: int) -> int:
    """Clamp `value` into the supported range; default on invalid input.

    Mirrors the convention used by `ConversationService` (T10 / #40):
    a `limit` outside `[1, MAX_LIST_LIMIT]` is replaced with the
    default. This is the same policy the route applies via FastAPI's
    `Query(ge=..., le=...)`; the service-level clamp is the safety net
    for non-FastAPI callers (tests, future internal scripts).
    """
    if value < 1 or value > MAX_LIST_LIMIT:
        return DEFAULT_LIST_LIMIT
    return value


def _escape_regex(value: str) -> re.Pattern[str]:
    """Compile `value` into a case-insensitive substring-match pattern.

    `re.escape` neutralises any regex metacharacters in `value` so a
    search for `report.v2` doesn't suddenly mean "any character at
    `report.v2`'s dot". Case folding keeps the search forgiving.
    """
    return re.compile(re.escape(value), re.IGNORECASE)


def _enforce_parameters_schema(schema: object) -> None:
    """Reject `schema` shapes that can't drive JSON Schema validation — T34 / #30.

    Two failure modes are caught here:

    1. **Empty schema.** An admin who forgets to declare a schema
       passes `{}`. The Worker's `_validate_parameters` would then
       fall back to a no-op (no rules → nothing to fail), letting
       LLM hallucinations slip through unchecked. ADR-0020 says the
       registration must reject this case so every Tool that reaches
       the runtime has *some* declared shape.
    2. **Malformed schema.** `Draft202012Validator.check_schema`
       raises on a schema whose own keywords contradict themselves
       (e.g. `{"type": 1234}`). We surface this here too so the
       Worker's runtime `Draft202012Validator(...)` constructor
       stays a no-op-success path.

    The helper is intentionally narrow: anything that looks like a
    JSON Schema document passes — including `{"type": "object"}`
    for the no-parameter Tool case. The richer shape is the admin's
    responsibility, not ours.
    """
    if not isinstance(schema, dict) or not schema:
        raise ToolSchemaInvalidError(
            details={"reason": "empty_schema"},
        )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ToolSchemaInvalidError(
            details={
                "reason": "invalid_schema",
                "schema_error": exc.message,
                "violations": [],
            },
        ) from exc


__all__ = ["ToolService", "DEFAULT_LIST_LIMIT", "MAX_LIST_LIMIT"]
