"""`/api/v1/admin/tools` router — T12 / #11 + T14 / #12 + T15 / #13.

The admin-facing HTTP seam for Tool CRUD per ADR-0003 / ADR-0018 /
ADR-0031. Endpoints:

* `POST   /api/v1/admin/tools`               — manual registration, OR
                                               activate-an-OpenAPI-draft
                                               (T15 / #13 carries the
                                               `source` field forward).
* `GET    /api/v1/admin/tools`               — list with optional filters.
* `GET    /api/v1/admin/tools/{id}`          — single Tool detail.
* `PATCH  /api/v1/admin/tools/{id}`          — partial update (description,
                                               risk_level, status, …).
* `POST   /api/v1/admin/tools/import/openapi` — OpenAPI spec → draft preview
                                               (T14 / #12; rows are NOT
                                               persisted — see ADR-0018).
                                               T16 / #14 rewrites each
                                               draft's description with
                                               the LLM before the admin
                                               reviews it.

Auth is enforced by `require_admin_user` (T12 / #11), which layers on
top of `get_current_user` to also verify the caller holds the
`admin` Role (ADR-0006 / ADR-0031). Routes below that line run with
the canonical `User` row attached, never with a raw role lookup.

The router is intentionally thin: every byte of business logic lives
in `app.tools.service.ToolService` (manual CRUD),
`app.tools.openapi_parser.OpenAPIParser` (T14 / #12), or
`app.tools.description_generator.ToolDescriptionGenerator` (T16 / #14).
This file exists only to translate Pydantic wire shapes into service
calls and back.

Why `admin_router` lives here rather than in `app.api.auth`
-----------------------------------------------------------

T09 (/10) put `/admin/me` next to the rest of the auth router so the
OpenAPI tags grouped cleanly. T12 grows the admin surface enough that
a dedicated module pays off: `app.api.admin_tools` owns the `admin`
tag and groups every future `/api/v1/admin/*` route under one prefix
without further edits to `main.py`.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, model_validator

from app.db.dependencies import get_description_generator, get_openapi_parser, get_tool_service
from app.db.schemas import (
    Tool,
    ToolCreate,
    ToolRiskLevel,
    ToolSource,
    ToolStatus,
    ToolUpdate,
    User,
)
from app.security.admin import require_admin_user
from app.tools.description_generator import ToolDescriptionGenerator
from app.tools.openapi_parser import OpenAPIParser
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
    source: ToolSource = Field(
        default="manual",
        description=(
            "Originating path per ADR-0003. The default `manual` covers the "
            "ad-hoc registration flow; T15 / #13's OpenAPI import preview "
            "forwards `openapi` so the persisted row keeps its provenance "
            "(per ADR-0003 §21)."
        ),
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
        # `source` defaults to `manual` for the ad-hoc registration
        # flow (T12). T15 / #13's OpenAPI import preview forwards
        # `openapi` so the persisted row keeps its provenance per
        # ADR-0003 §21 ("注册中心保留原始导入产物…与活跃 Tool 的对
        # 应关系"). The default is preserved so existing manual
        # callers (T13 ToolsTable's future "create" button, ad-hoc
        # admin scripts) keep working without rewrites.
        source=body.source,
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


# ---------------------------------------------------------------------------
# OpenAPI import — T14 / #12
# ---------------------------------------------------------------------------
#
# Per ADR-0003 / ADR-0018 this endpoint accepts an OpenAPI 3.x spec
# and returns a *preview* of the draft Tools it would derive. The
# admin reviews the preview, optionally edits slugs / risk levels,
# then a future `import/confirm` endpoint (out of T14's scope) turns
# the selections into persisted rows. No row is written here.


class ImportOpenAPIRequest(BaseModel):
    """Body of `POST /api/v1/admin/tools/import/openapi`.

    Exactly one of `spec` / `spec_yaml` must be set — Pydantic
    surfaces that as `422` so the admin UI gets a clear "choose one
    source" message rather than silently picking one. URL fetching
    is a future ticket; the field is intentionally absent so the
    wire doesn't carry an always-400 path.

    The source discriminator is the field the admin fills in; the
    route hands the right shape to `OpenAPIParser.parse*` and lets
    the parser own YAML/JSON text decoding.
    """

    spec: dict[str, Any] | None = Field(
        default=None,
        description="Inline OpenAPI document as a JSON object.",
    )
    spec_yaml: str | None = Field(
        default=None,
        max_length=2 * 1024 * 1024,
        description=(
            "Inline OpenAPI document as a YAML string. "
            "2 MiB cap mirrors common gateway limits."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ImportOpenAPIRequest:
        """Reject requests that supply zero or multiple source fields.

        Empty bodies fall through to Pydantic's `422` envelope; multi-
        source requests would force the route to pick one arbitrarily,
        which is worse than failing fast. The validator lives on the
        request model so the route stays declarative.
        """
        present = [s for s in (self.spec, self.spec_yaml) if s is not None]
        if len(present) != 1:
            raise ValueError(
                "exactly one of `spec` / `spec_yaml` must be set",
            )
        return self


class ToolDraftResponse(BaseModel):
    """Wire shape of a single OpenAPI-derived draft Tool.

    Mirrors the manual-registration `ToolResponse` plus two preview-
    only fields (`operation_ref`, `warnings`). `operation_ref` is the
    human-readable pointer the admin UI renders in the preview list
    ("GET /pets/{id}") — re-derivable, but pinning it on the wire
    means the UI never has to parse `http_method` + `http_url_template`
    back into a label.

    `warnings` is the per-draft issue list (missing `operationId`,
    non-JSON request body, …) so the admin can fix issues before
    confirming. An empty list means the operation parsed cleanly.
    """

    operation_ref: str = Field(
        description="Human-readable pointer for the preview list, e.g. `GET /pets/{id}`.",
    )
    name: str = Field(
        max_length=128,
        description="LLM-facing slug; derived from `operationId` or synthesised from path.",
    )
    description: str = Field(
        max_length=4096,
        description=(
            "LLM-friendly description. T16 / #14: auto-rewritten via the "
            "Langfuse `tool-description-generator` Prompt on import preview "
            "(ADR-0018); raw OpenAPI text when generation is skipped or fails."
        ),
    )
    original_description: str | None = Field(
        default=None,
        description=(
            "Raw OpenAPI summary/description the LLM rewrite replaced. "
            "`None` when `description` is still the raw text — the admin "
            "UI shows it side by side for review."
        ),
    )
    description_generated: bool = Field(
        default=False,
        description="True when `description` is the LLM-generated draft awaiting review.",
    )
    # T16-followup / #50 — parameter description rewrite (ADR-0018).
    # `original_parameters_schema` is the raw OpenAPI schema the LLM
    # notes were applied on top of; `None` when no notes were
    # generated. `parameters_schema_generated` is the analogue of
    # `description_generated` for the schema side.
    original_parameters_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Raw OpenAPI `parameters_schema` before the LLM's per-parameter "
            "rewrites were applied. `None` when the model didn't return any "
            "`parameter_notes` — the admin can still activate via "
            "`POST /api/v1/admin/tools` and edit by hand."
        ),
    )
    parameters_schema_generated: bool = Field(
        default=False,
        description=(
            "True when `parameters_schema` carries the LLM-rewritten "
            "per-parameter descriptions (T16-followup / #50)."
        ),
    )
    risk_level: ToolRiskLevel
    status: ToolStatus = Field(
        description="Always `draft` per ADR-0018 — preview rows are never active.",
    )
    parameters_schema: dict[str, Any]
    http_method: str
    http_url_template: str
    http_headers: dict[str, str]
    http_body_template: dict[str, Any] | None
    source: str = Field(
        description=(
            "Pinned to `openapi` so the persisted row stays "
            "distinguishable from manual entries."
        ),
    )
    source_ref: str | None = Field(
        description="Pointer back into the source spec: `method path`.",
    )
    credentials_ref: str | None = Field(
        description="Always `None` at preview time — credential binding happens at confirm-time.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Per-operation issues the admin should review before confirming.",
    )


class ImportOpenAPIResponse(BaseModel):
    """Wire shape of `POST /api/v1/admin/tools/import/openapi`.

    The preview collection lives under `drafts` so a future ticket
    can add pagination / filter metadata without breaking the parser.
    `title` / `version` / `server_url` are surfaced verbatim from the
    spec for the UI's header banner — no re-derivation.
    """

    drafts: list[ToolDraftResponse] = Field(
        description="One draft per operation, sorted by (method, path) deterministically.",
    )
    title: str | None = Field(
        default=None,
        description="From `info.title`. Shown in the preview header.",
    )
    version: str | None = Field(
        default=None,
        description="From `info.version`. Shown alongside the title.",
    )
    server_url: str | None = Field(
        default=None,
        description=(
            "Resolved base URL (first `servers[].url`). "
            "Used for `http_url_template` derivation."
        ),
    )
    source_format: Literal["json", "yaml"] = Field(
        description="Format the spec was parsed in. UI uses this for the 'parsed from' badge.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Import-level notices (T16 / #14): LLM not configured, the "
            "per-import generation cap being hit, etc. Per-operation "
            "issues stay on each draft's own `warnings` list."
        ),
    )


@router.post(
    "/import/openapi",
    response_model=ImportOpenAPIResponse,
    summary="Preview draft Tools derived from an OpenAPI spec (admin only)",
)
async def import_openapi(
    body: ImportOpenAPIRequest,
    _admin: User = Depends(require_admin_user),  # noqa: B008
    parser: OpenAPIParser = Depends(get_openapi_parser),  # noqa: B008
    generator: ToolDescriptionGenerator = Depends(get_description_generator),  # noqa: B008
) -> ImportOpenAPIResponse:
    """`POST /api/v1/admin/tools/import/openapi` — T14 / #12 + T16 / #14.

    Accepts an OpenAPI 3.x spec (JSON or YAML, inline) and returns
    one draft Tool per operation. The preview is **not persisted**;
    a future confirm endpoint takes the admin's selections and
    inserts rows via `ToolRepository.create`.

    Per ADR-0018 every draft lands in `status='draft'`, and per the
    same ADR the description is rewritten by the LLM
    (`ToolDescriptionGenerator.enrich_drafts`) before the admin sees
    it: the rewrite goes into `description`, the raw OpenAPI text into
    `original_description`. Generation never blocks the import — an
    unconfigured or failing LLM degrades to raw text plus a warning
    (ADR-0003's no-silent-drop rule), which is why there is no
    dedicated error envelope on this path.

    The admin reviews `description` / `risk_level` per draft and
    activates selected ones via `POST /api/v1/admin/tools` +
    `PATCH /api/v1/admin/tools/{id}`.

    The route dispatches on which source field is set so the YAML
    decoding stays inside `OpenAPIParser.parse_yaml` rather than
    leaking into the wire-shape translation layer.
    """
    if body.spec is not None:
        result = parser.parse(body.spec)
    else:
        # `_exactly_one_source` guarantees `spec_yaml` is set.
        assert body.spec_yaml is not None  # narrow for mypy
        result = parser.parse_yaml(body.spec_yaml)

    generation_warnings = await generator.enrich_drafts(result.drafts)

    return ImportOpenAPIResponse(
        drafts=[ToolDraftResponse(**asdict(d)) for d in result.drafts],
        title=result.title,
        version=result.version,
        server_url=result.server_url,
        source_format=result.source_format,
        warnings=generation_warnings,
    )


__all__ = ["router"]
