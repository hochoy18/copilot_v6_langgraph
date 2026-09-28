"""`/api/v1/admin/tools` router — T12 / #11.

The admin-facing HTTP seam for Tool CRUD per ADR-0003 / ADR-0018 /
ADR-0031. Four endpoints:

* `POST   /api/v1/admin/tools`          — manual registration.
* `GET    /api/v1/admin/tools`          — list with optional filters.
* `GET    /api/v1/admin/tools/{id}`     — single Tool detail.
* `PATCH  /api/v1/admin/tools/{id}`     — partial update (description,
                                          risk_level, status, …).

Auth is enforced by `require_admin_user` (T12 / #11), which layers on
top of `get_current_user` to also verify the caller holds the
`admin` Role (ADR-0006 / ADR-0031). Routes below that line run with
the canonical `User` row attached, never with a raw role lookup.

The router is intentionally thin: every byte of business logic lives
in `app.tools.service.ToolService`. This file exists only to translate
Pydantic wire shapes into service calls and back.

Why `admin_router` lives here rather than in `app.api.auth`
-----------------------------------------------------------

T09 (/10) put `/admin/me` next to the rest of the auth router so the
OpenAPI tags grouped cleanly. T12 grows the admin surface enough that
a dedicated module pays off: `app.api.admin_tools` owns the `admin`
tag and groups every future `/api/v1/admin/*` route under one prefix
without further edits to `main.py`.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.db.dependencies import get_tool_service
from app.db.schemas import (
    Tool,
    ToolCreate,
    ToolRiskLevel,
    ToolStatus,
    ToolUpdate,
    User,
)
from app.security.admin import require_admin_user
from app.tools.service import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, ToolService

router = APIRouter(prefix="/api/v1/admin/tools", tags=["admin"])


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class CreateToolRequest(BaseModel):
    """Body of `POST /api/v1/admin/tools`.

    Mirrors `ToolCreate` minus `_id` / `created_at` / `updated_at`
    (the repository stamps those). The wire shape deliberately omits
    `status`: the manual-registration path always lands in `draft`
    (ADR-0018). An admin promotes the row to `active` via the PATCH
    endpoint once the description is reviewed.
    """

    name: str = Field(
        min_length=1,
        max_length=128,
        description="LLM-facing slug for `tool_call.name`. Indexed unique.",
    )
    description: str = Field(
        min_length=1,
        max_length=4096,
        description="LLM-friendly description.",
    )
    risk_level: ToolRiskLevel = Field(
        description="`read` runs unattended; `write` / `destructive` pause for HITL.",
    )
    parameters_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for the Tool's arguments (validated by the Worker, ADR-0020).",
    )
    http_method: str = Field(
        min_length=1,
        max_length=16,
        description="HTTP method for the upstream call.",
    )
    http_url_template: str = Field(
        min_length=1,
        max_length=2048,
        description="URL template with `{var}` placeholders.",
    )
    http_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Static headers attached to every call.",
    )
    http_body_template: dict[str, Any] | None = Field(
        default=None,
        description="Optional JSON body template; the Worker renders `parameters` into it.",
    )
    source: str = Field(
        default="manual",
        description="Originating path per ADR-0003. Manual registrations pin `manual`.",
    )
    source_ref: str | None = Field(
        default=None,
        max_length=512,
        description="Opaque pointer to the source document (manual draft id, etc.).",
    )
    credentials_ref: str | None = Field(
        default=None,
        description="ObjectId of `credentials._id`. None for unauthenticated Tools.",
    )


class PatchToolRequest(BaseModel):
    """Body of `PATCH /api/v1/admin/tools/{id}`.

    Every field is optional so partial updates hold (an admin that
    only wants to flip status sends `{"status": "active"}`). The
    state-transition validator (the `status` enum itself) is the only
    cross-field rule; lifecycle gating (e.g. disabled → ?) lives in
    the service layer.
    """

    description: str | None = Field(default=None, min_length=1, max_length=4096)
    risk_level: ToolRiskLevel | None = None
    status: ToolStatus | None = None
    parameters_schema: dict[str, Any] | None = None
    http_method: str | None = Field(default=None, min_length=1, max_length=16)
    http_url_template: str | None = Field(default=None, min_length=1, max_length=2048)
    http_headers: dict[str, str] | None = None
    http_body_template: dict[str, Any] | None = None
    credentials_ref: str | None = None


class ToolResponse(BaseModel):
    """Canonical wire shape for a single Tool.

    Mirrors `app.db.schemas.Tool`; `model_dump(mode="json")` renders
    the embedded `parameters_schema` / `http_body_template` dicts as
    JSON-safe values. The Frontend's Tool Registry table (T13) reads
    every field directly off this shape.
    """

    id: str
    name: str
    description: str
    risk_level: ToolRiskLevel
    status: ToolStatus
    parameters_schema: dict[str, Any]
    http_method: str
    http_url_template: str
    http_headers: dict[str, str]
    http_body_template: dict[str, Any] | None
    source: str
    source_ref: str | None
    credentials_ref: str | None
    created_at: str
    updated_at: str


class ToolListResponse(BaseModel):
    """Wire shape of `GET /api/v1/admin/tools`.

    Single-key envelope so future pagination metadata (`next_cursor`,
    `has_more`) lands without breaking the client-side parser, mirroring
    `ConversationListResponse` (T10 / #40).
    """

    tools: list[ToolResponse] = Field(
        description="Tools in `name` order, post-filter, capped by `limit`.",
    )


# ---------------------------------------------------------------------------
# Conversions — internal helpers
# ---------------------------------------------------------------------------


def _tool_to_response(tool: Tool) -> ToolResponse:
    """Render a canonical `Tool` row as the wire shape.

    Centralising the conversion keeps the routes from duplicating
    `model_dump(mode="json")`; a future wire field addition only
    needs one site to change.
    """
    return ToolResponse(
        id=tool.id,
        name=tool.name,
        description=tool.description,
        risk_level=tool.risk_level,
        status=tool.status,
        parameters_schema=tool.parameters_schema,
        http_method=tool.http_method,
        http_url_template=tool.http_url_template,
        http_headers=dict(tool.http_headers),
        http_body_template=tool.http_body_template,
        source=tool.source,
        source_ref=tool.source_ref,
        credentials_ref=tool.credentials_ref,
        created_at=tool.created_at.isoformat(),
        updated_at=tool.updated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=ToolResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Manually register a new Tool (admin only)",
)
async def create_tool(
    body: CreateToolRequest,
    _admin: User = Depends(require_admin_user),  # noqa: B008
    svc: ToolService = Depends(get_tool_service),  # noqa: B008
) -> ToolResponse:
    """`POST /api/v1/admin/tools` — manual Tool registration.

    Per ADR-0018 the new Tool lands in `draft`; the LLM Planner
    cannot see it until an admin reviews the description and
    promotes it to `active` via `PATCH …/{id}` with `{"status":
    "active"}`. The repository rejects duplicate `name`s with a 409
    (`duplicate_key` envelope) so the wire contract is consistent
    with the rest of the codebase.
    """
    data = ToolCreate(
        name=body.name,
        description=body.description,
        risk_level=body.risk_level,
        status="draft",
        parameters_schema=body.parameters_schema,
        http_method=body.http_method,
        http_url_template=body.http_url_template,
        http_headers=body.http_headers,
        http_body_template=body.http_body_template,
        # `source` is pinned to `manual` for this route per ADR-0003
        # (the OpenAPI import path is a separate endpoint, T14).
        source="manual",
        source_ref=body.source_ref,
        credentials_ref=body.credentials_ref,
    )
    tool = await svc.create(data=data)
    return _tool_to_response(tool)


@router.get(
    "",
    response_model=ToolListResponse,
    summary="List Tools (admin only) with optional filters",
)
async def list_tools(
    status_filter: ToolStatus | None = Query(  # noqa: B008 — FastAPI idiom
        default=None,
        alias="status",
        description="Optional lifecycle filter (`draft` / `active` / `disabled`).",
    ),
    risk_level: ToolRiskLevel | None = Query(  # noqa: B008
        default=None,
        description="Optional risk-tier filter (`read` / `write` / `destructive`).",
    ),
    q: str | None = Query(  # noqa: B008
        default=None,
        max_length=256,
        description=(
            "Optional case-insensitive substring match against `name` and `description`. "
            "Empty / whitespace-only is treated as no search."
        ),
    ),
    limit: int = Query(  # noqa: B008
        default=DEFAULT_LIST_LIMIT,
        ge=1,
        le=MAX_LIST_LIMIT,
        description="Maximum number of Tools to return.",
    ),
    _admin: User = Depends(require_admin_user),  # noqa: B008
    svc: ToolService = Depends(get_tool_service),  # noqa: B008
) -> ToolListResponse:
    """`GET /api/v1/admin/tools` — Registry UI's primary list.

    The Frontend's three tabs (`draft` / `active` / `disabled`) map
    onto the `status` query param; the `risk_level` filter powers the
    admin "show me all destructive Tools" view; `q` is the search
    box. All three are optional and compose, so the wire can express
    "active destructive Tools matching `report`" in one round trip.
    """
    rows = await svc.list_tools(
        status=status_filter,
        risk_level=risk_level,
        q=q,
        limit=limit,
    )
    return ToolListResponse(tools=[_tool_to_response(t) for t in rows])


@router.get(
    "/{tool_id}",
    response_model=ToolResponse,
    summary="Fetch a single Tool by id (admin only)",
)
async def get_tool(
    tool_id: str,
    _admin: User = Depends(require_admin_user),  # noqa: B008
    svc: ToolService = Depends(get_tool_service),  # noqa: B008
) -> ToolResponse:
    """`GET /api/v1/admin/tools/{id}` — Tool detail.

    Composes the row read with the auth seam so cross-tenant probing
    (a future ticket's per-tenant scoping) lands here. The repository
    surfaces `NotFoundError` (404 envelope) for absent or malformed
    ids; the global handler renders it uniformly.
    """
    tool = await svc.get_tool(tool_id=tool_id)
    return _tool_to_response(tool)


@router.patch(
    "/{tool_id}",
    response_model=ToolResponse,
    summary="Update a Tool (admin only) — description / status / risk_level / etc.",
)
async def patch_tool(
    tool_id: str,
    body: PatchToolRequest,
    _admin: User = Depends(require_admin_user),  # noqa: B008
    svc: ToolService = Depends(get_tool_service),  # noqa: B008
) -> ToolResponse:
    """`PATCH /api/v1/admin/tools/{id}` — partial update.

    Drives the T12 acceptance criteria:

    * **Description edit.** Admin rewrites the LLM-facing description
      after review (ADR-0018).
    * **Risk-level change.** Promote `read` → `write` →
      `destructive` without re-creating the row (ADR-0027 preserves
      the prior value in Plan snapshots for already-issued Plans).
    * **Lifecycle transition.** `draft → active` (admin review done)
      or `active → disabled` (admin takes offline). Any target
      value is accepted so the admin can roll a Tool back to `draft`
      or re-activate a previously disabled one.

    Status-only transitions route through the dedicated
    `set_status` path so audit-log hooks (T42) see them as discrete
    events rather than diffing the full PATCH body.
    """
    patch = ToolUpdate(
        description=body.description,
        risk_level=body.risk_level,
        status=body.status,
        parameters_schema=body.parameters_schema,
        http_method=body.http_method,
        http_url_template=body.http_url_template,
        http_headers=body.http_headers,
        http_body_template=body.http_body_template,
        credentials_ref=body.credentials_ref,
    )
    # `status`-only transitions route through `set_status` so audit
    # hooks (T42) see them as discrete events. Any patch that touches
    # other fields falls through to the general update path so the
    # whole set lands in one Mongo write. `model_fields_set` collapses
    # the per-field `is None` enumeration to one membership check, so
    # adding a new optional field to `PatchToolRequest` doesn't
    # silently drop into `set_status`.
    only_status = set(body.model_fields_set) == {"status"} and body.status is not None
    if only_status:
        # `only_status=True` guarantees `body.status is not None`;
        # the `assert` is the type-narrowing seam so mypy sees a
        # concrete `ToolStatus` rather than `ToolStatus | None`.
        assert body.status is not None
        tool = await svc.set_status(tool_id=tool_id, status=body.status)
    else:
        tool = await svc.update(tool_id=tool_id, patch=patch)
    return _tool_to_response(tool)


__all__ = ["router"]
