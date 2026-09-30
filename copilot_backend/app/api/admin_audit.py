"""`/api/v1/admin/audit-logs` router — T42 / #37, ADR-0028 / ADR-0031.

The admin-facing HTTP seam for the Audit Log retention surface.
Endpoints:

* `GET    /api/v1/admin/audit-logs`               — filterable list
                                                     with cursor
                                                     pagination
                                                     (`useInfiniteQuery`
                                                     friendly per
                                                     ADR-0031).
* `POST   /api/v1/admin/audit-logs/{id}/recall`   — trigger
                                                     cold-storage
                                                     hydration. The
                                                     P95 < 5-minute
                                                     SLO from
                                                     ADR-0028 lives
                                                     in the retention
                                                     service; the
                                                     route just kicks
                                                     the call.

Auth is enforced by `require_admin_user` (T12 / #11), which
layers on top of `get_current_user` to also verify the caller
holds the `admin` Role (ADR-0006 / ADR-0031). Routes below that
line run with the canonical `User` row attached, never with a
raw role lookup.

Why this lives in `app.api.admin_audit` rather than in
`app.api.conversations` (where audit-row strings appear in the
admin detail): the T43 admin UI treats the audit log as a
first-class drill-down surface, and ADR-0031 pins the URL under
`/admin/audit-logs` — a dedicated module lets the OpenAPI tag
group cleanly (`admin-audit` next to `admin-tools`).
"""
from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.audit.retention import AuditRetentionService
from app.db.dependencies import (
    get_audit_log_repository,
    get_audit_retention_service,
)
from app.db.errors import ValidationError
from app.db.schemas import AuditLog, User
from app.repositories.audit_logs import AuditLogRepository
from app.security.admin import require_admin_user

router = APIRouter(prefix="/api/v1/admin/audit-logs", tags=["admin-audit"])


# ---------------------------------------------------------------------------
# Wire shapes — mirror the Frontend's `copilot_frontend/src/types/audit-log.ts`
# 1:1 so the JSON payload assigns directly without a mapping layer.
# ---------------------------------------------------------------------------


class AuditLogResponse(BaseModel):
    """Canonical wire shape of a single `audit_logs` row.

    Every persisted field is surfaced — the admin UI's row-expand
    panel renders `tool_snapshot` / `parameters` / `response` /
    `error` as JSON, and a rename on the backend side is a
    typecheck failure here before the page breaks.
    """

    id: str
    actor_id: str
    conversation_id: str
    turn_id: str
    plan_id: str
    plan_execution_id: str | None
    tool_name: str
    tool_snapshot: dict[str, Any]
    parameters: dict[str, Any]
    response: dict[str, Any] | None
    status: str
    error: dict[str, Any] | None
    risk_level: str
    retry_count: int
    occurred_at: str
    lifecycle_status: str
    cold_storage_ref: str | None
    cold_archived_at: str | None


class AuditLogListResponse(BaseModel):
    """Envelope of `GET /api/v1/admin/audit-logs`.

    Cursor pagination fields (`next_cursor`, `has_more`) match the
    convention noted in ADR-0031 ("列表分页规范 … 选用 cursor,
    前端无限滚动友好"). `next_cursor` is the canonical end-of-list
    signal the UI reads (via `getNextPageParam`); `has_more`
    mirrors it server-side so a consumer can check either without
    decoding the opaque cursor.
    """

    logs: list[AuditLogResponse] = Field(
        description="Audit rows, ordered by `occurred_at` DESC, `_id` DESC tiebreak.",
    )
    next_cursor: str | None = Field(
        description=(
            "Opaque cursor for the next page. `null` when there are no more rows."
        ),
    )
    has_more: bool = Field(
        description="True iff there are more rows after this page.",
    )


# ---------------------------------------------------------------------------
# Cursor helpers — opaque base64 JSON the UI never decodes.
# ---------------------------------------------------------------------------


def _encode_cursor(occurred_at: object, audit_log_id: str) -> str:
    """Pack the (occurred_at, id) tuple into an opaque cursor token.

    The cursor is the boundary marker for the next page in
    `occurred_at DESC, _id DESC` order; encoding it as base64
    JSON keeps the wire field compact and opaque to consumers
    that don't need to introspect it.
    """
    raw = json.dumps(
        {"o": str(occurred_at), "i": audit_log_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(token: str) -> tuple[str, str]:
    """Reverse `_encode_cursor`. Raises `ValueError` on malformed input.

    The route catches `ValueError` and re-raises as a 422 via the
    global handler — a malformed cursor is a client bug, not a
    domain error. Every decode failure (base64, UTF-8, JSON shape)
    funnels into the same `ValueError` so the caller doesn't need
    to enumerate subclasses.
    """
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError) as exc:
        # `binascii.Error` is a `ValueError` subclass; the union
        # covers base64 + JSON + UTF-8 failures without leaking the
        # raw exception class to the route layer.
        raise ValueError(f"invalid cursor: {exc}") from exc
    if not isinstance(data, dict) or "o" not in data or "i" not in data:
        raise ValueError("cursor missing required keys")
    return cast(str, data["o"]), cast(str, data["i"])


def _row_to_response(row: AuditLog) -> AuditLogResponse:
    """Render a canonical `AuditLog` row as the wire shape.

    `model_dump(mode="json")` renders `tool_snapshot` and the
    payload dicts as JSON-safe values, and the
    `datetime → ISO 8601` conversion keeps the string fields
    identical to the schema's persisted shape.
    """
    dumped = row.model_dump(mode="json")
    return AuditLogResponse(
        id=dumped["id"],
        actor_id=dumped["actor_id"],
        conversation_id=dumped["conversation_id"],
        turn_id=dumped["turn_id"],
        plan_id=dumped["plan_id"],
        plan_execution_id=dumped.get("plan_execution_id"),
        tool_name=dumped["tool_name"],
        tool_snapshot=dumped["tool_snapshot"],
        parameters=dumped["parameters"],
        response=dumped["response"],
        status=dumped["status"],
        error=dumped["error"],
        risk_level=dumped["risk_level"],
        retry_count=dumped["retry_count"],
        occurred_at=dumped["occurred_at"],
        lifecycle_status=dumped["lifecycle_status"],
        cold_storage_ref=dumped.get("cold_storage_ref"),
        cold_archived_at=dumped.get("cold_archived_at"),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


@router.get(
    "",
    response_model=AuditLogListResponse,
    summary="List audit logs (admin only) with filters + cursor pagination",
)
async def list_audit_logs(
    tool_name: str | None = Query(  # noqa: B008 — FastAPI idiom
        default=None,
        max_length=128,
        description="Optional filter — exact match against `tool_name`.",
    ),
    actor_id: str | None = Query(  # noqa: B008
        default=None,
        max_length=64,
        description="Optional filter — exact match against `actor_id`.",
    ),
    lifecycle_status: str | None = Query(  # noqa: B008
        default=None,
        description=(
            "Optional lifecycle filter — `active` / `archived` / `recalled`."
        ),
    ),
    time_from: str | None = Query(  # noqa: B008
        default=None,
        description=(
            "Optional inclusive lower bound on `occurred_at`. "
            "UTC ISO 8601 string (the Frontend's `toUtcIso` helper "
            "normalizes the calendar widget's local string)."
        ),
    ),
    time_to: str | None = Query(  # noqa: B008
        default=None,
        description="Optional exclusive upper bound on `occurred_at`. UTC ISO 8601 string.",
    ),
    cursor: str | None = Query(  # noqa: B008
        default=None,
        description="Opaque cursor returned by the previous page; `null` for the first page.",
    ),
    limit: int = Query(  # noqa: B008
        default=_DEFAULT_LIMIT,
        ge=1,
        le=_MAX_LIMIT,
        description="Maximum number of rows per page.",
    ),
    _admin: User = Depends(require_admin_user),  # noqa: B008
    repo: AuditLogRepository = Depends(get_audit_log_repository),  # noqa: B008
) -> AuditLogListResponse:
    """`GET /api/v1/admin/audit-logs` — admin audit drill-down.

    All filter params are optional and compose; the response is
    cursor-paginated by (`occurred_at` DESC, `_id` DESC) so the
    Frontend's `useInfiniteQuery` can chain pages without gaps.
    `next_cursor` is `null` (and `has_more=False`) when the page
    is the last one; both fields are mirrored so the UI can pick
    whichever is cheaper to inspect.
    """
    # Parse the time bounds + cursor up front so a malformed
    # value surfaces as a 400 envelope before we touch Mongo.
    parsed_time_from = _parse_iso_or_none(time_from, "time_from")
    parsed_time_to = _parse_iso_or_none(time_to, "time_to")
    parsed_cursor_occurred_at: datetime | None = None
    parsed_cursor_id: str | None = None
    if cursor:
        try:
            parsed_cursor_occurred_at_str, parsed_cursor_id = _decode_cursor(cursor)
            parsed_cursor_occurred_at = datetime.fromisoformat(
                parsed_cursor_occurred_at_str,
            )
        except ValueError as exc:
            raise ValidationError(
                message_en=f"invalid cursor: {exc}",
                details={"cursor": "malformed"},
            ) from exc

    # Fetch one extra row so we can decide `has_more` without a
    # second Mongo round-trip. If the extra row came back, the
    # next-page cursor points at the last *returned* row (the
    # extra row is dropped from the page).
    rows = await repo.query(
        tool_name=tool_name,
        actor_id=actor_id,
        lifecycle_status=cast(Any, lifecycle_status) if lifecycle_status else None,
        time_from=parsed_time_from,
        time_to=parsed_time_to,
        before_occurred_at=parsed_cursor_occurred_at,
        before_id=parsed_cursor_id,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(last.occurred_at, last.id)
    return AuditLogListResponse(
        logs=[_row_to_response(row) for row in page_rows],
        next_cursor=next_cursor,
        has_more=has_more,
    )


def _parse_iso_or_none(value: str | None, field_name: str) -> datetime | None:
    """Parse an ISO 8601 string into a `datetime`, or `None` for absent.

    Raises `ValidationError` for malformed values so the global
    handler converts it into the unified error envelope. An empty /
    whitespace-only value is treated as absent (`None`) to mirror
    the Frontend's `search.set` guards.
    """
    if value is None or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            message_en=f"{field_name!r} is not a valid ISO 8601 timestamp: {exc}",
            details={field_name: value},
        ) from exc


@router.post(
    "/{audit_log_id}/recall",
    response_model=AuditLogResponse,
    summary="Trigger cold-storage hydration (调档) for one archived audit row",
)
async def recall_audit_log(
    audit_log_id: str,
    _admin: User = Depends(require_admin_user),  # noqa: B008
    service: AuditRetentionService = Depends(get_audit_retention_service),  # noqa: B008
) -> AuditLogResponse:
    """`POST /api/v1/admin/audit-logs/{id}/recall` — T42 / #37 + ADR-0028.

    Hydrates the slim tombstone back to queryable hot state. The
    P95 < 5-minute SLO lives in the retention service; this
    route just kicks the call and returns the restored row. The
    Frontend uses the response's `lifecycle_status` to know
    whether the row is now `recalled` (success) or still
    `archived` (which the route wouldn't have produced — the
    4xx envelopes above are how the UI learns it failed).

    Errors:

    * 404 (`not_found` / `invalid_id`) — unknown or malformed id.
    * 409 (`audit_log_not_archived`) — the row is still `active`
      and not in cold storage.
    """
    row = await service.recall(audit_log_id)
    return _row_to_response(row)


__all__ = ["router"]