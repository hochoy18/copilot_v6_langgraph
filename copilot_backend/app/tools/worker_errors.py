"""Typed errors raised by the Tool Worker — T21 / #18.

These are the per-call error shapes the Worker emits. The seams that
consume them (`PlanExecutor`, the audit log writer, the SSE bridge in
T23) pattern-match on `code` to decide whether to retry, escalate to
HITL, or mark the node terminal.

Codes mirror the ADR vocabulary:

* `schema_violation` — Tool parameters failed JSON Schema validation
  against the snapshot (ADR-0020). Stop immediately: there is no
  retry path for LLM hallucinations against a typed schema.
* `credential_invalid` — credential decryption failed or the row
  points at a malformed payload (ADR-0002 / ADR-0024). Surface as
  HITL so the admin can rotate.
* `timeout` — upstream did not respond within `timeout_seconds`
  (ADR-0026). Behaviour follows the risk-level retry matrix
  (ADR-0017): read tools retry, write/destructive escalate.
* `upstream_error` — upstream returned 4xx or 5xx, or the connection
  failed. 4xx is treated as a deterministic business error and never
  retried; 5xx + connection errors follow the ADR-0017 retry matrix.
* `hitl_required` — synthetic error raised when the Worker's retry
  budget exhausts (or the risk tier is write/destructive) and the
  next decision belongs to the business user.
"""
from __future__ import annotations

from typing import Any

from app.exceptions import AppError


class ToolWorkerError(AppError):
    """Base class for every error the Worker emits.

    Subclasses set `code` so callers don't have to inspect the class
    hierarchy; the SSE bridge (T23) and audit log writer route purely
    on `code`.
    """


class SchemaViolationError(ToolWorkerError):
    """LLM-supplied parameters failed JSON Schema validation (ADR-0020).

    The Worker raises this **before** any upstream call so a malformed
    request never leaves the system boundary. The error envelope
    carries the field-level details so the Planner can retry the LLM
    with a focused correction prompt.
    """

    code: str = "schema_violation"
    message_zh: str = "Tool 参数未通过 schema 校验"
    message_en: str = "Tool parameters failed schema validation"
    http_status: int = 422  # Unprocessable Entity


class CredentialInvalidError(ToolWorkerError):
    """Credential decryption failed or the row is malformed (ADR-0002).

    Distinct from a runtime 401/403 (`upstream_error`) — this is the
    Worker's own failure to *open* the encrypted bytes. It points at
    a rotation problem (T38) or a corrupt row, not at a transient
    upstream hiccup, so the Worker surfaces it as HITL and never
    retries.
    """

    code: str = "credential_invalid"
    message_zh: str = "凭证解密失败,需要管理员介入"
    message_en: str = "Tool credential could not be decrypted"
    http_status: int = 502  # Bad Gateway — upstream of us is broken


class UpstreamTimeoutError(ToolWorkerError):
    """Upstream API exceeded the per-Tool `timeout_seconds` (ADR-0026).

    Funnel for both `httpx.TimeoutException` and a request that
    completed after the configured deadline. The retry decision is
    the Worker's, not the caller's — this error just carries the
    fact.
    """

    code: str = "timeout"
    message_zh: str = "上游 API 调用超时"
    message_en: str = "Tool upstream call timed out"
    http_status: int = 504  # Gateway Timeout


class UpstreamError(ToolWorkerError):
    """Upstream API returned 4xx / 5xx or refused the connection.

    `status_code` is the upstream HTTP code when one was returned;
    network failures surface as `status_code=None`. The Worker
    classifies 4xx as deterministic business errors and never
    retries; 5xx + connection errors follow the ADR-0017 matrix.
    """

    code: str = "upstream_error"
    message_zh: str = "上游 API 返回错误"
    message_en: str = "Tool upstream call failed"
    http_status: int = 502

    def __init__(
        self,
        *,
        code: str | None = None,
        message_zh: str | None = None,
        message_en: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(
            code=code,
            message_zh=message_zh,
            message_en=message_en,
            details=details,
        )
        self.status_code = status_code
        self.retryable = retryable


class HITLRequiredError(ToolWorkerError):
    """Synthetic error: retry budget exhausted, escalate to a human.

    The Worker raises this when (a) `risk_level` is write / destructive
    and any failure surfaces, or (b) read retries have all failed
    (ADR-0017). The error envelope carries the last underlying
    failure in `details["cause"]` so the UI can render a useful
    message. The PlanExecutor catches this specifically to halt the
    Plan cleanly without retrying at the executor layer.
    """

    code: str = "hitl_required"
    message_zh: str = "需要业务人员确认后才能继续"
    message_en: str = "Human confirmation required to continue"
    http_status: int = 409  # Conflict — Plan can't proceed unattended


__all__ = [
    "ToolWorkerError",
    "SchemaViolationError",
    "CredentialInvalidError",
    "UpstreamTimeoutError",
    "UpstreamError",
    "HITLRequiredError",
]
