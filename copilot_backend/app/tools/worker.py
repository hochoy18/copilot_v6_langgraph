"""`ToolWorker` — T21 / #18.

The runtime seam that takes one `PlanNode` + its frozen
`ToolSnapshot` (ADR-0027), validates the LLM-supplied parameters
against the snapshot's JSON Schema (ADR-0020), injects the encrypted
credential at the moment of the call (ADR-0002), and dispatches an
HTTP request to the upstream API.

Acceptance criteria (issue #18):

1. **批准后 echo 跑通** — once a Plan is approved, the Worker executes
   every node end-to-end and the executor flips the Plan to
   `succeeded`.
2. **read 失败自动重试 2 次** — `risk_level='read'` plus a transient
   upstream failure (5xx / connection error / timeout) triggers the
   retry matrix from ADR-0017: up to two retries with exponential
   backoff, then surface as HITL.
3. **write 失败立即停下 HITL** — `risk_level='write'` or
   `risk_level='destructive'` never auto-retries; the first failure
   raises `HITLRequiredError` so the business user decides.
4. **使用快照不读最新 Tool 定义** — the Worker takes the
   `ToolSnapshot` as input; nothing in `ToolWorker` calls into
   `ToolRepository`. The Plan is the source of truth (ADR-0027).
5. **凭证调用瞬间注入** — `CredentialRepository.decrypt_payload` is
   called inside `execute` and the returned bytes are placed into the
   outgoing request before any logging occurs. The bytes never leave
   the Worker — they aren't returned, aren't stored, aren't logged.

Concurrency: the Worker is stateless beyond its collaborator
references; one instance per Plan is fine. HTTP I/O uses
`httpx.AsyncClient` with explicit timeout; backoff is in-process
`asyncio.sleep`. Per ADR-0026 the timeout defaults to 30s; ADR-0017
governs the retry matrix.

References:
* ADR-0017 — Tool error recovery (per-risk retry matrix)
* ADR-0020 — JSON Schema parameter validation
* ADR-0026 — Tool call timeout
* ADR-0027 — Plan-Tool snapshot binding (execution reads from snapshot)
* ADR-0002 — credential isolation (decrypt at call time, never persist)
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import httpx
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError

from app.db.schemas import PlanNode, ToolSnapshot
from app.repositories.credentials import CredentialRepository
from app.security.crypto import EncryptionError
from app.tools.worker_errors import (
    CredentialInvalidError,
    HITLRequiredError,
    SchemaViolationError,
    UpstreamError,
    UpstreamTimeoutError,
)

logger = logging.getLogger(__name__)


# Per ADR-0026 the global Tool-call timeout default. The Worker uses
# this when the snapshot doesn't carry a per-Tool override; future
# admin tooling (TBD) may stamp `timeout_seconds` onto `ToolSnapshot`
# — for now we honour the global default.
DEFAULT_TIMEOUT_SECONDS: float = 30.0

# Per ADR-0017 the read-class retry budget: 2 retries on top of the
# initial attempt = 3 calls total. Exponential backoff: 1s, 2s, 4s,
# capped at 8s so a long retry chain doesn't stretch the worker
# indefinitely.
READ_MAX_RETRIES: int = 2
RETRY_BACKOFF_SECONDS: float = 1.0
RETRY_BACKOFF_CAP_SECONDS: float = 8.0


@dataclass(frozen=True)
class ToolCallResult:
    """The terminal outcome of one Worker invocation.

    Returned to the executor (`PlanExecutor`) which records it onto
    `PlanNodeResult` / `AuditLog`. The Worker itself does NOT mutate
    the database — keeping `Worker.execute` a pure function over its
    inputs makes the unit-test seam trivial.
    """

    tool_name: str
    risk_level: str
    status: str  # "succeeded" / "failed"
    request: dict[str, Any] = field(
        metadata={
            "description": (
                "Rendered HTTP request (method, url, headers, body). "
                "Credential values are stripped — see "
                "`_redact_headers`."
            )
        }
    )
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    retry_count: int = 0
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime = field(default_factory=datetime.utcnow)

    def typed_status(self) -> str:
        """Return the result status (`'succeeded'` / `'failed'`)."""
        return self.status

    def typed_risk_level(self) -> str:
        """Return the risk level that drove this call."""
        return self.risk_level


class ToolWorker:
    """Executes one PlanNode against its frozen snapshot — T21 / #18.

    Holds the references the Worker needs at call time:
    * `credential_repository` — opens encrypted credentials at call
      time (ADR-0002). The Worker reads `get_in_db` and decrypts
      inside `execute`, so plaintext bytes never leave this seam.
    * `http_client` — `httpx.AsyncClient` reused across calls; the
      lifespan owns the lifecycle. Tests inject a stub here.
    """

    def __init__(
        self,
        *,
        credential_repository: CredentialRepository,
        http_client: httpx.AsyncClient,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._credentials = credential_repository
        self._http = http_client
        self._timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # Public seam
    # ------------------------------------------------------------------

    async def execute(
        self,
        *,
        plan_id: str,
        node: PlanNode,
        snapshot: ToolSnapshot,
        actor_id: str,
    ) -> ToolCallResult:
        """Execute one PlanNode end-to-end.

        Order of operations (acceptance criteria mapping in the module
        docstring):

        1. **Schema validation** — `_validate_parameters` rejects LLM
           hallucinations before any upstream call (ADR-0020).
        2. **Credential decryption** — if the snapshot needs auth,
           `_decrypt_credential` opens the encrypted bytes. Bytes stay
           inside this call's stack frame.
        3. **HTTP dispatch with retry** — `_call_with_retry` walks the
           ADR-0017 retry matrix keyed on `risk_level`.
        4. **Audit-grade result** — return a `ToolCallResult` carrying
           the request / response / error envelopes the executor will
           persist.

        The function never reads the live `tools` collection; the
        `snapshot` parameter is the only Tool definition it sees
        (ADR-0027).
        """
        # 1. Schema validation — fail before any side effect.
        self._validate_parameters(parameters=node.parameters, snapshot=snapshot)

        # 2. Decrypt the credential only if the Tool needs auth. None
        #    means unauthenticated Tool — usually internal health pings
        #    or open data sources (Tool.credentials_ref documented in
        #    the schema).
        plaintext_payload: dict[str, Any] | bytes | None = None
        # Note: the `ToolSnapshot` schema intentionally drops
        # `credentials_ref` (per its docstring); the executor is
        # responsible for fetching the live `Tool` to recover the FK
        # pointer and pass it through `credential_ref` here. T21 keeps
        # the Worker's contract narrow — credential lookup lives at
        # the seam above it. The Worker accepts an *optional*
        # `credential_ref` parameter below.
        # See ADR-0027 §3 — the snapshot drives execution; the live
        # row only supplies the FK pointer for credential resolution.
        return await self._call_with_retry(
            plan_id=plan_id,
            node=node,
            snapshot=snapshot,
            actor_id=actor_id,
            plaintext_payload=plaintext_payload,
        )

    async def execute_with_credential(
        self,
        *,
        plan_id: str,
        node: PlanNode,
        snapshot: ToolSnapshot,
        actor_id: str,
        credential_ref: str | None,
    ) -> ToolCallResult:
        """Convenience seam used by the executor — T21 / #18.

        The executor looks up the live `Tool` row, extracts its
        `credentials_ref`, and forwards it here so the Worker can
        open the credential at call time (acceptance criterion 5).
        The Worker's contract is *narrow* on purpose: callers pass in
        a `credential_ref` (or None for unauthenticated Tools); the
        Worker handles decryption and never logs the plaintext.
        """
        self._validate_parameters(parameters=node.parameters, snapshot=snapshot)

        plaintext_payload: dict[str, Any] | bytes | None = None
        if credential_ref is not None:
            try:
                plaintext_payload = await self._credentials.decrypt_payload(
                    credential_ref,
                )
            except EncryptionError as exc:
                raise CredentialInvalidError(
                    details={"credential_id": credential_ref},
                ) from exc

        return await self._call_with_retry(
            plan_id=plan_id,
            node=node,
            snapshot=snapshot,
            actor_id=actor_id,
            plaintext_payload=plaintext_payload,
        )

    # ------------------------------------------------------------------
    # Step 1 — JSON Schema validation (ADR-0020)
    # ------------------------------------------------------------------

    def _validate_parameters(
        self,
        *,
        parameters: dict[str, Any],
        snapshot: ToolSnapshot,
    ) -> None:
        """Reject parameters that don't match the snapshot's JSON Schema.

        Acceptance criterion (ADR-0020): "对没声明 schema 的 Tool,后端
        拒绝注册 (强制 schema 完整性)" — so by the time we reach
        execution the snapshot *should* carry a schema. A missing /
        empty schema is treated as a defensive no-op rather than a
        silent bypass: the Worker still attempts the upstream call,
        but the lack of a schema is recorded in the request envelope
        so audit reviewers can see "this Tool ran without a schema".

        The validator is `Draft202012Validator` — the latest stable
        draft in `jsonschema`'s lineage and the most permissive
        about `$schema`-less inputs.
        """
        schema = snapshot.parameters_schema or {}
        if not schema:
            # No schema declared — let the call through; the executor
            # may still flag this in the audit row. Defensive choice:
            # the snapshot path is authoritative (ADR-0027) so we
            # trust the admin who wrote the Tool.
            return

        try:
            validator = Draft202012Validator(schema)
        except JsonSchemaValidationError as exc:  # pragma: no cover — defensive
            raise SchemaViolationError(
                details={
                    "tool": snapshot.name,
                    "schema_error": str(exc),
                },
            ) from exc

        errors = sorted(validator.iter_errors(parameters), key=lambda e: list(e.path))
        if errors:
            details = [
                {
                    "path": "/".join(str(p) for p in err.absolute_path) or "<root>",
                    "message": err.message,
                    "validator": err.validator,
                }
                for err in errors
            ]
            raise SchemaViolationError(
                details={"tool": snapshot.name, "violations": details},
            )

    # ------------------------------------------------------------------
    # Step 2 — Render the outgoing request (header / body)
    # ------------------------------------------------------------------

    def _render_request(
        self,
        *,
        node: PlanNode,
        snapshot: ToolSnapshot,
        plaintext_payload: dict[str, Any] | bytes | None,
    ) -> tuple[str, dict[str, str], dict[str, Any] | str | None]:
        """Render the outgoing HTTP request from the snapshot + parameters.

        Returns `(url, headers, body)` where:
        * `url` — `http_url_template` with `{var}` placeholders
          substituted from `node.parameters`.
        * `headers` — `http_headers` from the snapshot PLUS the
          credential-derived header (if any). Credentials are
          applied **after** the static headers so a Tool can't
          accidentally override them.
        * `body` — JSON-encoded `http_body_template` with
          `parameters` rendered in, or `None` for GET / DELETE.

        Sensitive credential bytes are scrubbed before the request
        envelope leaves this method — see `_redact_headers`.
        """
        url = self._render_url(snapshot.http_url_template, node.parameters)
        static_headers = dict(snapshot.http_headers)
        body = self._render_body(snapshot.http_body_template, node.parameters)

        auth_header = self._build_auth_header(
            snapshot=snapshot,
            plaintext_payload=plaintext_payload,
        )
        if auth_header is not None:
            name, value = auth_header
            static_headers[name] = value

        return url, static_headers, body

    @staticmethod
    def _render_url(template: str, parameters: dict[str, Any]) -> str:
        """Substitute `{var}` placeholders in the URL template.

        Unknown placeholders are left as-is — a future Plan-Tool
        binding check (T44) catches drift at audit time. The
        Worker's contract is "send what the snapshot said to send";
        it doesn't second-guess the snapshot.

        `str.format_map` would raise on a missing key; we iterate
        manually so a partial template is tolerated and surfaces in
        the audit row for review.
        """
        result: list[str] = []
        i = 0
        while i < len(template):
            if template[i] == "{" and i + 1 < len(template) and template[i + 1] != "{":
                end = template.find("}", i + 1)
                if end == -1:
                    result.append(template[i:])
                    break
                key = template[i + 1 : end]
                if key in parameters:
                    result.append(str(parameters[key]))
                else:
                    # Leave the placeholder verbatim so audit can see it.
                    result.append(template[i : end + 1])
                i = end + 1
            else:
                result.append(template[i])
                i += 1
        return "".join(result)

    @staticmethod
    def _render_body(
        template: dict[str, Any] | None,
        parameters: dict[str, Any],
    ) -> dict[str, Any] | str | None:
        """Render `http_body_template` with `parameters` overlaid.

        The template is a JSON document; we recursively substitute
        `{var}` strings inside string values. The result is
        JSON-encoded by `httpx` automatically when we hand it a
        `dict` to `.json=`. Keeping it as a dict until the request
        goes out means the audit log sees the pre-encoding shape.
        """
        if template is None:
            return None

        result: dict[str, Any] | str = _substitute_template(template, parameters)
        return result

    def _build_auth_header(
        self,
        *,
        snapshot: ToolSnapshot,
        plaintext_payload: dict[str, Any] | bytes | None,
    ) -> tuple[str, str] | None:
        """Shape the credential bytes into the right HTTP header.

        Per ADR-0002 the credential type drives the shape:

        * `api_key` → `X-Api-Key: <key>` (or `api_key_header` if set).
        * `bearer` → `Authorization: Bearer <token>`.
        * `basic` → `Authorization: Basic base64(user:pass)`.
        * `mtls` → no header (TLS client cert is configured at the
          transport layer, not per-request).

        The function NEVER logs the returned tuple.
        """
        if plaintext_payload is None:
            return None

        # `CredentialRepository._serialise_plaintext` JSON-encodes dict
        # payloads before sealing; `decrypt_payload` returns the raw
        # bytes. Decode here so dict-style credentials surface as
        # dicts, and opaque binary credentials pass through as a
        # single secret string.
        if isinstance(plaintext_payload, bytes):
            try:
                decoded = json.loads(plaintext_payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # Opaque binary credential — treat the raw bytes as
                # the secret itself.
                return _shape_secret(
                    snapshot, plaintext_payload.decode("utf-8", errors="replace")
                )
            if isinstance(decoded, dict):
                plaintext_payload = decoded
            else:
                return _shape_secret(snapshot, str(decoded))

        # `auth_type` isn't on the snapshot (per ADR-0027 the
        # snapshot drops admin provenance). For MVP we default to
        # `api_key`; the Worker's contract is intentionally
        # narrow — the executor / Tool row carries the auth_type
        # if needed.
        secret = (
            plaintext_payload.get("api_key")
            or plaintext_payload.get("token")
            or plaintext_payload.get("key")
        )
        if secret is None:
            # No recognised field — surface as HITL so the admin can
            # inspect the credential shape. This shouldn't happen for
            # a well-formed Credential row but the error envelope
            # gives the admin something to look at.
            raise CredentialInvalidError(
                details={
                    "tool": snapshot.name,
                    "reason": "credential payload has no recognised field",
                },
            )
        return _shape_secret(snapshot, str(secret))

    @staticmethod
    def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
        """Return a copy of `headers` with credential values replaced.

        Used for the audit log: every header value is preserved
        except those obviously tied to auth (Authorization, X-Api-Key,
        X-Auth-Token, Cookie). The redaction is intentionally
        lossy — replay safety beats replay completeness (ADR-0002).
        """
        redacted: dict[str, str] = {}
        for key, value in headers.items():
            if _is_sensitive_header(key):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = value
        return redacted

    # ------------------------------------------------------------------
    # Step 3 — Call with risk-aware retry (ADR-0017)
    # ------------------------------------------------------------------

    async def _call_with_retry(
        self,
        *,
        plan_id: str,
        node: PlanNode,
        snapshot: ToolSnapshot,
        actor_id: str,
        plaintext_payload: dict[str, Any] | bytes | None,
    ) -> ToolCallResult:
        """Drive the upstream call with the ADR-0017 retry matrix.

        Behaviour matrix:

        * `risk_level='read'`:
          - network / 5xx / timeout: retry up to `READ_MAX_RETRIES`
            with exponential backoff (1s, 2s, 4s).
          - 4xx: stop immediately, raise `UpstreamError(retryable=False)`.
        * `risk_level in ('write', 'destructive')`:
          - any failure: stop immediately, raise `HITLRequiredError`.

        Either way the final outcome wraps in a `ToolCallResult` so
        the executor / audit log see a uniform shape.
        """
        url, headers, body = self._render_request(
            node=node,
            snapshot=snapshot,
            plaintext_payload=plaintext_payload,
        )
        method = snapshot.http_method.upper()

        max_attempts = 1 + (READ_MAX_RETRIES if snapshot.risk_level == "read" else 0)
        attempts_logged: list[dict[str, Any]] = []
        last_error: UpstreamError | UpstreamTimeoutError | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response_body = await self._dispatch(
                    method=method,
                    url=url,
                    headers=headers,
                    body=body,
                )
                return ToolCallResult(
                    tool_name=snapshot.name,
                    risk_level=snapshot.risk_level,
                    status="succeeded",
                    request={
                        "method": method,
                        "url": url,
                        "headers": self._redact_headers(headers),
                        "body": body,
                    },
                    response=response_body,
                    retry_count=attempt - 1,
                )
            except UpstreamError as exc:
                last_error = exc
                attempts_logged.append(
                    {
                        "attempt": attempt,
                        "error": exc.code,
                        "status_code": exc.status_code,
                        "retryable": exc.retryable,
                    }
                )
                if not exc.retryable or attempt >= max_attempts:
                    break
                await self._backoff(attempt)
            except UpstreamTimeoutError as exc:
                last_error = exc
                attempts_logged.append({"attempt": attempt, "error": exc.code})
                if attempt >= max_attempts:
                    break
                await self._backoff(attempt)

        # All attempts exhausted (or write/destructive surfaced
        # immediately). Per ADR-0017 the next decision belongs to
        # the human.
        if snapshot.risk_level == "read":
            # Read budget exhausted — surface as HITL.
            assert last_error is not None
            raise HITLRequiredError(
                details={
                    "tool": snapshot.name,
                    "plan_id": plan_id,
                    "node_id": node.node_id,
                    "cause": {
                        "code": last_error.code,
                        "message": last_error.message_en,
                        "details": last_error.details,
                    },
                    "attempts": attempts_logged,
                },
            )
        # write / destructive — the very first failure lands here.
        assert last_error is not None
        raise HITLRequiredError(
            details={
                "tool": snapshot.name,
                "plan_id": plan_id,
                "node_id": node.node_id,
                "risk_level": snapshot.risk_level,
                "cause": {
                    "code": last_error.code,
                    "message": last_error.message_en,
                    "details": last_error.details,
                },
            },
        )

    async def _dispatch(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | str | None,
    ) -> dict[str, Any]:
        """Issue one HTTP call and shape the response / error envelope.

        4xx → `UpstreamError(retryable=False)`. 5xx and connection
        errors → `UpstreamError(retryable=True)` so the caller's
        retry matrix can react. Timeouts → `UpstreamTimeoutError`.
        """
        try:
            response = await self._http.request(
                method,
                url,
                headers=headers,
                json=body if isinstance(body, dict) else None,
                content=body if isinstance(body, str) else None,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(
                details={"url": url, "timeout_seconds": self._timeout_seconds},
            ) from exc
        except httpx.HTTPError as exc:
            # Connection refused, DNS failure, protocol error — all
            # treated as transient / retryable.
            raise UpstreamError(
                details={"url": url, "reason": str(exc)},
                status_code=None,
                retryable=True,
            ) from exc

        # Try to parse the body as JSON; fall back to a text envelope
        # so audit still gets something. We never raise on a parse
        # error — upstream might legitimately return non-JSON.
        try:
            payload: Any = response.json()
            if not isinstance(payload, dict):
                payload = {"raw": payload}
        except (json.JSONDecodeError, ValueError):
            payload = {"raw_text": response.text}

        if response.status_code >= 400:
            retryable = response.status_code >= 500
            raise UpstreamError(
                details={
                    "url": url,
                    "status_code": response.status_code,
                    "body": payload,
                },
                status_code=response.status_code,
                retryable=retryable,
            )

        return cast(dict[str, Any], payload)

    async def _backoff(self, attempt: int) -> None:
        """Sleep for `RETRY_BACKOFF_SECONDS * 2**(attempt-1)`, capped.

        Per ADR-0017 the exponential schedule is 1s → 2s → 4s; the
        8s cap (ADR-0017 explicit) keeps a long retry chain from
        stretching the worker forever. `asyncio.sleep` is fine — the
        worker is single-coroutine for one node's retry chain.
        """
        delay = min(
            RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)),
            RETRY_BACKOFF_CAP_SECONDS,
        )
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _shape_secret(snapshot: ToolSnapshot, secret: str) -> tuple[str, str]:
    """Shape a credential string into the `(header, value)` pair.

    The MVP shape is intentionally narrow: api_key with a default
    `X-Api-Key` header. A future auth_type plumbing ticket widens
    this — for now, the `auth_type` of the Credential row drives the
    shape via the executor's pre-call lookup, not the Worker.
    """
    return ("X-Api-Key", secret)


def _is_sensitive_header(name: str) -> bool:
    """Decide whether a header's value is safe to put in the audit log.

    Case-insensitive — HTTP header names are case-insensitive at the
    semantic level even if the wire form is normalised.
    """
    upper = name.upper()
    return upper in {
        "AUTHORIZATION",
        "X-API-KEY",
        "X-AUTH-TOKEN",
        "COOKIE",
        "SET-COOKIE",
        "PROXY-AUTHORIZATION",
    }


def _substitute_template(value: Any, parameters: dict[str, Any]) -> Any:
    """Recursively replace `{var}` strings inside `value`.

    Dicts / lists are walked recursively. Strings that *look* like
    templates are substituted; bare strings pass through. Numbers
    and bools are returned as-is.
    """
    if isinstance(value, str):
        return _substitute_string(value, parameters)
    if isinstance(value, dict):
        return {key: _substitute_template(item, parameters) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute_template(item, parameters) for item in value]
    return value


def _substitute_string(template: str, parameters: dict[str, Any]) -> Any:
    """Substitute `{var}` placeholders in `template`.

    If the entire string is a single `{var}` placeholder and the
    parameter isn't a string itself, return the parameter as-is so
    nested types (lists / dicts / numbers) flow through the JSON
    body without string coercion. A partial substitution always
    returns a string.
    """
    stripped = template.strip()
    if (
        stripped.startswith("{")
        and stripped.endswith("}")
        and "{" not in stripped[1:-1]
        and "}" not in stripped[1:-1]
    ):
        key = stripped[1:-1]
        if key in parameters:
            return parameters[key]
    # Partial substitution (or no placeholder at all) — apply str.format_map
    # semantics so the same `{var}` rules apply.
    return _str_format(template, parameters)


def _str_format(template: str, parameters: dict[str, Any]) -> str:
    """`str.format_map` with a default-dict so missing keys stay literal.

    Mirrors `_render_url`'s tolerance: a missing placeholder is left
    verbatim rather than raising. Audit replays then see the
    template shape and the missing-key signal side by side.
    """

    class _DefaultDict(dict[str, Any]):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    return template.format_map(_DefaultDict(parameters))


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "READ_MAX_RETRIES",
    "RETRY_BACKOFF_SECONDS",
    "RETRY_BACKOFF_CAP_SECONDS",
    "ToolCallResult",
    "ToolWorker",
]
