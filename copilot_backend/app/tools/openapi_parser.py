"""`OpenAPIParser` — T14 / #12.

Per ADR-0003 the Tool Registry's primary ingestion path is OpenAPI.
This module owns the spec → draft Tool transformation:

* Walk `paths` and, for each operation (`get` / `post` / `…`), emit
  one `ToolDraft`.
* Derive the LLM-facing slug from `operationId` when present;
  otherwise synthesize one from the path and warn the admin so they
  can rename before confirming.
* Fold path-level parameters + operation parameters + the JSON
  request body into a single JSON Schema for `parameters_schema`.
* Combine the first `servers[].url` entry with the operation path
  to produce `http_url_template`. Server variables / templated URLs
  are deliberately deferred — the admin can edit the URL template
  after activation.

The parser is **stateless and side-effect-free**. Every entry point
returns a new value; nothing is persisted. `POST
/api/v1/admin/tools/import/openapi` calls into this parser and
returns its output verbatim. A future `import/confirm` endpoint
takes the admin's selections and inserts the rows via the existing
`ToolRepository.create` path.

Why a hand-rolled parser rather than `openapi-spec-validator` /
`prance`
----------------------------------------------------------------

* ADR-0015 reserves adding new runtime deps for architectural
  changes. PyYAML is already a transitive dep (LangChain pulls it
  in), so we don't grow the dependency tree.
* The MVP needs paths + operations + parameters + request bodies —
  four fields. A 1,000-line parser library is overkill for that
  surface. If a future ticket needs `$ref` resolution or webhook
  handling, we revisit the dependency question.

Why `OpenAPIParser` is a class wrapping pure functions
------------------------------------------------------

The class exists to give `app.db.dependencies.get_openapi_parser`
a stable seam so tests can override it with a stub. The parser
holds no instance state; every helper lives at module level so
they're individually importable for tests that want to exercise a
single rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from app.tools.errors import OpenAPIParseError

# HTTP methods recognised by the OpenAPI 3.x spec. We accept
# `head` / `options` / `trace` too so a future "Tool variant" admin
# UI can preview them; the LLM-facing risk-level default still
# distinguishes read vs state-mutating (see `_default_risk_level`).
#
# Order matters — this tuple doubles as the preview's sort key, so
# the operations an admin should review first (the destructive ones)
# appear at the top of the list. The ordering is "destructive
# before non-destructive"; PATCH is destructive, GET/HEAD/OPTIONS
# are not.
_METHOD_SORT_ORDER: tuple[str, ...] = (
    "delete",
    "patch",
    "post",
    "put",
    "options",
    "head",
    "get",
    "trace",
)

# Quick membership check used when walking a path item; cheaper than
# scanning `_METHOD_SORT_ORDER` per operation.
_KNOWN_METHODS: frozenset[str] = frozenset(_METHOD_SORT_ORDER)


@dataclass(slots=True)
class ToolDraft:
    """One OpenAPI operation rendered as a candidate Tool row.

    Mirrors the canonical `ToolCreate` shape so a future
    `import/confirm` endpoint can pass the dict straight into the
    existing `ToolRepository.create` path. The two preview-only
    fields (`operation_ref`, `warnings`) live alongside so the admin
    UI has everything it needs without re-deriving.
    """

    operation_ref: str
    name: str
    description: str
    risk_level: str
    parameters_schema: dict[str, Any]
    http_method: str
    http_url_template: str
    http_headers: dict[str, str]
    http_body_template: dict[str, Any] | None
    status: str
    source: str
    source_ref: str | None
    credentials_ref: None  # imported Tools never bind a credential at preview time
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class OpenAPIParseResult:
    """The full result of parsing one OpenAPI document."""

    title: str | None
    version: str | None
    server_url: str | None
    drafts: list[ToolDraft]
    source_format: Literal["json", "yaml"]


class OpenAPIParser:
    """Stateless OpenAPI 3.x → `ToolDraft` transformer.

    A fresh instance per request is fine — the parser holds no I/O
    state and constructing it is cheap. Tests instantiate one
    directly; the FastAPI dependency seam (`get_openapi_parser`)
    hands a fresh one to each route handler.
    """

    def parse(self, spec: dict[str, Any]) -> OpenAPIParseResult:
        """Parse an in-memory OpenAPI spec dict.

        Raises:
            OpenAPIParseError: the document is unsupported, missing
                required fields, or has no operations to derive.
        """
        return _parse_spec_dict(spec, source_format="json")

    def parse_yaml(self, text: str) -> OpenAPIParseResult:
        """Parse a YAML string into an OpenAPI parse result.

        Decoding YAML lives here rather than in the route so the
        thin-router pattern holds. On shape / version failure this
        surfaces `OpenAPIParseError` exactly like `parse`.
        """
        spec = _decode_yaml(text)
        return _parse_spec_dict(spec, source_format="yaml")


# ---------------------------------------------------------------------------
# Module-level helpers — exposed for tests but not part of the public
# service contract.
# ---------------------------------------------------------------------------


def _decode_yaml(text: str) -> dict[str, Any]:
    """Decode a YAML string into a JSON-object dict.

    Wraps `yaml.safe_load` so the parser can report a typed error
    (`OpenAPIParseError`) and so tests don't import PyYAML directly.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise OpenAPIParseError(
            message_en="Invalid YAML in OpenAPI document",
            details={"yaml_error": str(exc)},
        ) from exc
    if not isinstance(data, dict):
        raise OpenAPIParseError(
            message_en="OpenAPI document must decode to a JSON object",
            details={"type": type(data).__name__},
        )
    return data


def _parse_spec_dict(
    spec: dict[str, Any],
    *,
    source_format: Literal["json", "yaml"],
) -> OpenAPIParseResult:
    """Walk a parsed spec dict and emit one `ToolDraft` per operation.

    Pulled out of `OpenAPIParser.parse` so `parse_yaml` reuses the
    same skeleton without duplicating the walk.
    """
    _validate_spec_shape(spec)
    info = spec.get("info") or {}
    title = _str_or_none(info.get("title"))
    version = _str_or_none(info.get("version"))

    servers = spec.get("servers") or []
    server_url: str | None = None
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            server_url = _str_or_none(first.get("url"))

    paths = spec.get("paths") or {}
    if not isinstance(paths, dict) or not paths:
        raise OpenAPIParseError(
            message_en="OpenAPI document declares no paths",
            details={"paths": paths},
        )

    drafts: list[ToolDraft] = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        path_level_params, path_warnings = _coerce_params(path_item.get("parameters"))
        for method in _KNOWN_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            draft = _build_draft(
                method=method,
                path=str(path),
                operation=operation,
                path_level_params=path_level_params,
                path_warnings=path_warnings,
                server_url=server_url,
            )
            drafts.append(draft)

    # Deterministic order so the admin UI renders identically
    # across calls. `_METHOD_SORT_ORDER` puts destructive methods
    # first, so admins see the dangerous operations at the top of
    # the preview list.
    drafts.sort(key=_draft_sort_key)

    return OpenAPIParseResult(
        title=title,
        version=version,
        server_url=server_url,
        drafts=drafts,
        source_format=source_format,
    )


# ---------------------------------------------------------------------------
# Internal — pure functions used by `parse`
# ---------------------------------------------------------------------------


def _str_or_none(value: Any) -> str | None:
    """Coerce `value` to a stripped `str` or `None`."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _validate_spec_shape(spec: dict[str, Any]) -> None:
    """Reject specs that aren't OpenAPI 3.x or are missing essentials.

    Co-located with `_parse_spec_dict` so the route can stay
    declarative; the validation rules are stable enough not to need
    their own seam.
    """
    if not isinstance(spec, dict):
        raise OpenAPIParseError(
            message_en="OpenAPI document must be a JSON object",
            details={"type": type(spec).__name__},
        )
    openapi_version = spec.get("openapi")
    if not isinstance(openapi_version, str) or not openapi_version.startswith("3."):
        raise OpenAPIParseError(
            message_en="Unsupported OpenAPI version (only 3.x is supported)",
            details={"openapi": openapi_version},
        )
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise OpenAPIParseError(
            message_en="OpenAPI document is missing `paths`",
            details={"paths": paths},
        )
    if not paths:
        raise OpenAPIParseError(
            message_en="OpenAPI document has no operations to derive",
            details={"paths": paths},
        )


def _coerce_params(
    value: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Coerce a `parameters` field to a list, surfacing skipped entries.

    Per ADR-0003 the parser must not silently drop operations (or
    parts of them). When the source field is structurally wrong —
    `parameters: false`, a non-list, or contains non-dict entries
    — we still proceed, but the warnings travel onto every draft
    that walks through this path so the admin can fix and retry.
    """
    warnings: list[str] = []
    if value is None:
        return [], warnings
    if value is False:  # OpenAPI lets a path item opt out of inherited params
        warnings.append("`parameters: false` declared; inherited params dropped.")
        return [], warnings
    if not isinstance(value, list):
        warnings.append("`parameters` was not a list; treating as empty.")
        return [], warnings
    params: list[dict[str, Any]] = []
    for index, entry in enumerate(value):
        if isinstance(entry, dict):
            params.append(entry)
        else:
            warnings.append(f"`parameters[{index}]` was not an object; skipped.")
    return params, warnings


def _build_draft(
    *,
    method: str,
    path: str,
    operation: dict[str, Any],
    path_level_params: list[dict[str, Any]],
    path_warnings: list[str],
    server_url: str | None,
) -> ToolDraft:
    """Render one operation as a `ToolDraft`.

    Pulled out of `_parse_spec_dict` so the per-operation rules have
    a stable home and a future `parameter-schema-validator` ticket
    can slot in here without touching the parser skeleton.
    """
    warnings: list[str] = list(path_warnings)
    op_params, op_warnings = _coerce_params(operation.get("parameters"))
    warnings.extend(op_warnings)
    all_params = [*path_level_params, *op_params]

    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in all_params:
        name = _str_or_none(param.get("name"))
        location = _str_or_none(param.get("in"))
        if not name:
            warnings.append("Parameter missing `name`; skipped.")
            continue
        if location not in {"path", "query", "header", "cookie"}:
            warnings.append(
                f"Parameter {name!r} has unrecognised `in` value; treated as opaque."
            )
        schema = param.get("schema")
        schema_dict: dict[str, Any] = schema if isinstance(schema, dict) else {}
        description = _str_or_none(param.get("description"))
        # Copy so we don't mutate the caller's spec.
        property_schema: dict[str, Any] = dict(schema_dict)
        if description:
            property_schema["description"] = description
        # Mark where the parameter ends up so the Worker can route
        # it correctly when it materialises the call.
        if location:
            property_schema["__location__"] = location
        properties[name] = property_schema
        if param.get("required") is True or location == "path":
            required.append(name)

    body_template: dict[str, Any] | None = None
    body_schema, body_warning = _json_request_body_schema(operation.get("requestBody"))
    if body_schema is not None:
        body_description = ""
        rb = operation.get("requestBody") or {}
        if isinstance(rb, dict):
            desc = _str_or_none(rb.get("description"))
            body_description = desc or ""
        body_schema_with_desc = {k: v for k, v in body_schema.items()}
        if body_description:
            body_schema_with_desc["description"] = body_description
        body_schema_with_desc["__location__"] = "body"
        properties["body"] = body_schema_with_desc
        rb_required = (
            isinstance(rb, dict) and rb.get("required") is True
        )
        if rb_required:
            required.append("body")
    if body_warning:
        warnings.append(body_warning)

    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
    }

    url_template = _build_url_template(server_url=server_url, path=path)

    name = _derive_name(operation=operation, method=method, path=path, warnings=warnings)
    description = _derive_description(operation=operation, method=method, path=path)

    return ToolDraft(
        operation_ref=f"{method.upper()} {path}",
        name=name,
        description=description,
        risk_level=_default_risk_level(method),
        parameters_schema=parameters_schema,
        http_method=method.upper(),
        http_url_template=url_template,
        http_headers={"Accept": "application/json"},
        http_body_template=body_template,
        status="draft",  # ADR-0018 — preview drafts never go live directly
        source="openapi",
        source_ref=f"{method.lower()} {path}",
        credentials_ref=None,
        warnings=warnings,
    )


def _json_request_body_schema(request_body: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return the JSON body schema and any warning produced.

    OpenAPI operations may declare `requestBody.content` for any
    media type — `application/json`, `application/xml`,
    `multipart/form-data`, etc. The MVP only knows how to render
    JSON, so non-JSON media types surface as a warning rather than
    silently dropping the body (ADR-0003 graceful-degradation rule).
    """
    if not isinstance(request_body, dict):
        return None, None
    content = request_body.get("content")
    if not isinstance(content, dict):
        return None, None
    if "application/json" in content:
        json_media = content["application/json"]
        if not isinstance(json_media, dict):
            return None, "Request body `application/json` media is malformed."
        schema = json_media.get("schema")
        if not isinstance(schema, dict):
            return None, "Request body `application/json` has no schema."
        return {k: v for k, v in schema.items()}, None
    # Non-JSON media type — surface the mismatch so the admin can
    # decide whether to fall back to manual registration.
    media_keys = sorted(content.keys())
    return None, (
        f"Request body media type {media_keys!r} is not JSON; "
        "schema not derived — register this Tool manually."
    )


def _build_url_template(*, server_url: str | None, path: str) -> str:
    """Join the first server URL with the operation path.

    Strips trailing slashes from the server URL so `/pets` and
    `https://api.example.com/` don't produce `…com//pets`. Server
    variables (`{scheme}`, `{region}`) are deliberately left intact
    — the admin can substitute them after activation.
    """
    if server_url:
        return f"{server_url.rstrip('/')}{path}"
    return path


def _derive_name(
    *,
    operation: dict[str, Any],
    method: str,
    path: str,
    warnings: list[str],
) -> str:
    """Pick the LLM-facing slug for the operation.

    Prefers `operationId` because that's what OpenAPI tooling tends
    to keep stable across regenerations. The slug is preserved
    verbatim — `listPets` stays `listPets` rather than getting
    lowercased to `listpets` — because the OpenAPI spec already
    constrains `operationId` to a safe ASCII identifier.

    Falls back to a synthesised slug when absent, and warns so the
    admin can rename before confirming.
    """
    operation_id = _str_or_none(operation.get("operationId"))
    if operation_id:
        return operation_id[:128]
    warnings.append(
        "Operation is missing `operationId`; slug was synthesised from method + path."
    )
    return _slugify(f"{method}_{path}")[:128]


_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slugify(value: str) -> str:
    """Lowercase + collapse non-alphanumerics to single underscores.

    Only the synthesised fallback path uses this; `operationId` is
    preserved verbatim. The slug must be safe in every context
    (URL, JSON key, Mongo `_id` component for the `name` index);
    ASCII alphanumerics and the underscore separator cover that.
    """
    lowered = value.lower().lstrip("_")
    slug = _SLUG_RE.sub("_", lowered).strip("_")
    return slug or "operation"


def _derive_description(*, operation: dict[str, Any], method: str, path: str) -> str:
    """Build the LLM-friendly description from `summary` + `description`.

    OpenAPI's `summary` is a one-line tagline (often missing) and
    `description` is the longer Markdown body. The LLM generation
    step (T16) will replace this once Langfuse's
    `tool-description-generator` runs; for T14 the raw OpenAPI text
    is the best we have.
    """
    summary = _str_or_none(operation.get("summary")) or ""
    description = _str_or_none(operation.get("description")) or ""
    if summary and description:
        return f"{summary}\n\n{description}"
    if summary:
        return summary
    if description:
        return description
    return f"Auto-generated description for {method.upper()} {path}."


def _default_risk_level(method: str) -> str:
    """Map an HTTP method to its safe-default `risk_level`.

    Per ADR-0004 `read` runs unattended; `write` / `destructive`
    pause for HITL. We default state-mutating methods to `write`
    (not `destructive`) because destructive is a stronger claim the
    admin should make explicitly — `DELETE /items/{id}` is often
    soft-delete. The admin promotes per-Tool after review.
    """
    if method == "get":
        return "read"
    return "write"


def _draft_sort_key(draft: ToolDraft) -> tuple[int, str]:
    """Sort drafts by (method index, path) for deterministic ordering.

    `_METHOD_SORT_ORDER` puts destructive methods first so the
    preview list opens with the operations the admin needs to
    review most carefully.
    """
    method_index = _METHOD_SORT_ORDER.index(draft.http_method.lower())
    return (method_index, draft.operation_ref)


__all__ = [
    "OpenAPIParser",
    "OpenAPIParseResult",
    "ToolDraft",
]
