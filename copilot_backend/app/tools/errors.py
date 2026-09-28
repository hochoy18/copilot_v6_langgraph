"""Tool-domain exceptions (T14 / #12).

The Tool repository raises generic database-shaped errors
(`NotFoundError`, `DuplicateKeyError`, …). The OpenAPI import path
needs finer-grained errors so callers can render the right HTTP
envelope:

* `OpenAPIParseError` — the spec itself is malformed (unsupported
  version, missing required fields, etc.). Surfaced as `400` so the
  admin can fix the spec and retry.

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


__all__ = ["OpenAPIParseError"]
