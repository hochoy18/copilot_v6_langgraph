"""Tool-domain exceptions (T14 / #12, T34 / #30).

The Tool repository raises generic database-shaped errors
(`NotFoundError`, `DuplicateKeyError`, …). The OpenAPI import path
needs finer-grained errors so callers can render the right HTTP
envelope:

* `OpenAPIParseError` — the spec itself is malformed (unsupported
  version, missing required fields, etc.). Surfaced as `400` so the
  admin can fix the spec and retry.
* `ToolSchemaInvalidError` — the proposed `parameters_schema` is not
  a usable JSON Schema document. Surfaced as `422` so the admin UI
  can prompt for a schema fix before retrying registration. This is
  the registration-side sibling of the runtime `SchemaViolationError`
  (`app.tools.worker_errors`) which fires when the LLM-supplied
  parameters fail validation against an already-registered schema.

All classes derive from `app.exceptions.AppError` so the global
exception handler renders them uniformly (ADR-0031).
"""
from __future__ import annotations

from fastapi import status

from app.exceptions import AppError


class OpenAPIParseError(AppError):
    """Raised when the supplied OpenAPI document cannot be parsed.

    Covers every "the spec itself is broken" path:

    * unsupported `openapi` version (anything outside 3.x)
    * missing required top-level fields (`openapi`, `paths`)
    * `paths` is present but empty (no operations to derive Tools from)
    * YAML / JSON shape failures (delegated here so the route stays
      declarative)

    Surfaced as 400 so the admin knows the spec needs editing rather
    than something on our side being down. `details` carries the
    underlying parser message so ops can correlate against
    OpenAPI tooling logs.
    """

    code = "openapi_parse_error"
    message_zh = "OpenAPI 文档解析失败"
    message_en = "Failed to parse OpenAPI document"
    http_status = status.HTTP_400_BAD_REQUEST


class ToolSchemaInvalidError(AppError):
    """Raised at Tool registration when `parameters_schema` is unusable — T34 / #30.

    Per ADR-0020: "对没声明 schema 的 Tool,后端拒绝注册 (强制 schema
    完整性)". The service layer enforces this rule before the row
    reaches Mongo so a Tool can never land in `active` state with
    a schema the Worker cannot validate against.

    `details["reason"]` is one of:

    * `empty_schema` — the schema dict is `{}` (or otherwise has no
      keys). Admin must declare at least `{"type": "object"}` or
      a richer shape; an empty dict is rejected so the Worker's
      `_validate_parameters` never has to decide "should I trust
      this?".
    * `invalid_schema` — `Draft202012Validator.check_schema`
      rejected the document. `details["schema_check"]` carries the
      underlying message.

    Surfaced as `422` because the admin request itself is well-formed
    but semantically rejected — the same status the runtime
    `SchemaViolationError` returns, keeping the wire status
    consistent across the schema-related error surface.
    """

    code = "tool_schema_invalid"
    message_zh = "Tool 参数 schema 不合法"
    message_en = "Tool parameters_schema is not a usable JSON Schema"
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY


__all__ = ["OpenAPIParseError", "ToolSchemaInvalidError"]
