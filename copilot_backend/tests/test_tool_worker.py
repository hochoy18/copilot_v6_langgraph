"""Tests for `ToolWorker` — T21 / #18.

The Worker is the runtime seam that takes a `PlanNode` + frozen
`ToolSnapshot`, validates parameters against the snapshot's JSON
Schema (ADR-0020), decrypts the credential at call time (ADR-0002),
issues the HTTP request, and applies the ADR-0017 retry matrix
keyed on the snapshot's `risk_level`.

The tests below use a stub `httpx` transport so the worker's
network behaviour is observable end-to-end without a real upstream.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from app.db.init_db import init_database
from app.db.schemas import (
    CredentialCreate,
    PlanNode,
    ToolSnapshot,
)
from app.repositories.credentials import CredentialRepository
from app.security.crypto import AesGcmEncryptor, MasterKey
from app.tools.worker import (
    DEFAULT_TIMEOUT_SECONDS,
    READ_MAX_RETRIES,
    RETRY_BACKOFF_CAP_SECONDS,
    RETRY_BACKOFF_SECONDS,
    ToolWorker,
)
from app.tools.worker_errors import (
    CredentialInvalidError,
    HITLRequiredError,
    SchemaViolationError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def encryptor() -> AesGcmEncryptor:
    return AesGcmEncryptor(MasterKey(key_bytes=os.urandom(32), key_id="test-worker"))


@pytest.fixture
async def credential_repo(encryptor: AesGcmEncryptor) -> CredentialRepository:
    db = AsyncMongoMockClient()["copilot_worker_cred_test"]
    await init_database(db)
    return CredentialRepository(db, encryptor)


def _echo_snapshot() -> ToolSnapshot:
    """A minimal read-class Tool snapshot — drives the happy path."""
    return ToolSnapshot(
        name="echo",
        description="Echo back the input text (read-only)",
        risk_level="read",
        parameters_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        http_method="POST",
        http_url_template="https://upstream.test/echo",
        http_headers={"Content-Type": "application/json"},
        http_body_template={"echo": "{text}"},
    )


def _write_snapshot() -> ToolSnapshot:
    return ToolSnapshot(
        name="create_invoice",
        description="Create an invoice (write)",
        risk_level="write",
        parameters_schema={
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "customer": {"type": "string"},
            },
            "required": ["amount", "customer"],
        },
        http_method="POST",
        http_url_template="https://upstream.test/invoices",
        http_headers={"Content-Type": "application/json"},
        http_body_template={"amount": "{amount}", "customer": "{customer}"},
    )


def _destructive_snapshot() -> ToolSnapshot:
    return ToolSnapshot(
        name="purge_account",
        description="Hard-delete an account (destructive)",
        risk_level="destructive",
        parameters_schema={
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
            "required": ["account_id"],
        },
        http_method="DELETE",
        http_url_template="https://upstream.test/accounts/{account_id}",
        http_headers={"Content-Type": "application/json"},
        http_body_template=None,
    )


def _echo_node(parameters: dict[str, Any] | None = None) -> PlanNode:
    return PlanNode(
        node_id="n1",
        tool="echo",
        parameters=parameters if parameters is not None else {"text": "hello"},
    )


def _echo_handler(request: httpx.Request) -> httpx.Response:
    """Stub upstream: echo the JSON body back under `body`."""
    body = json.loads(request.content) if request.content else {}
    return httpx.Response(200, json={"echo": body.get("echo")})


@pytest.fixture
def echo_transport() -> httpx.MockTransport:
    return httpx.MockTransport(_echo_handler)


@pytest.fixture
async def echo_client(
    echo_transport: httpx.MockTransport,
) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=echo_transport) as client:
        yield client


@pytest.fixture
def worker(
    credential_repo: CredentialRepository,
    echo_client: httpx.AsyncClient,
) -> ToolWorker:
    return ToolWorker(
        credential_repository=credential_repo,
        http_client=echo_client,
    )


# ---------------------------------------------------------------------------
# Schema validation (ADR-0020)
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """The Worker rejects malformed parameters before any upstream call."""

    async def test_valid_parameters_pass_through_to_upstream(
        self, worker: ToolWorker
    ) -> None:
        result = await worker.execute_with_credential(
            plan_id="plan-1",
            node=_echo_node({"text": "hi"}),
            snapshot=_echo_snapshot(),
            actor_id="user-1",
            credential_ref=None,
        )
        assert result.status == "succeeded"
        assert result.retry_count == 0
        assert result.request["method"] == "POST"
        assert result.request["url"] == "https://upstream.test/echo"

    async def test_missing_required_field_raises_schema_violation(
        self, worker: ToolWorker
    ) -> None:
        """An empty `parameters` dict fails `required: ['text']`."""
        with pytest.raises(SchemaViolationError) as exc_info:
            await worker.execute_with_credential(
                plan_id="plan-1",
                node=_echo_node({}),
                snapshot=_echo_snapshot(),
                actor_id="user-1",
                credential_ref=None,
            )
        details = exc_info.value.details
        assert details is not None
        assert details["tool"] == "echo"
        assert any(
            v["validator"] == "required"
            for v in details["violations"]
        )

    async def test_wrong_type_raises_schema_violation(
        self, worker: ToolWorker
    ) -> None:
        with pytest.raises(SchemaViolationError) as exc_info:
            await worker.execute_with_credential(
                plan_id="plan-1",
                node=_echo_node({"text": 12345}),
                snapshot=_echo_snapshot(),
                actor_id="user-1",
                credential_ref=None,
            )
        details = exc_info.value.details
        assert details is not None
        assert any(
            v["validator"] == "type"
            for v in details["violations"]
        )

    async def test_no_schema_declared_raises_schema_violation(
        self, echo_client: httpx.AsyncClient, credential_repo: CredentialRepository
    ) -> None:
        """T34 / #30 defense-in-depth — a snapshot without a schema is rejected.

        ADR-0020 says registration rejects Tools without a schema,
        so by the time a snapshot reaches the Worker the schema
        should always be non-empty. If a legacy row or hand-built
        snapshot somehow lacks one, the Worker surfaces
        `SchemaViolationError` (rather than silently bypassing) so
        the LLM never sees an unvalidated upstream call.
        """
        snap = _echo_snapshot().model_copy(
            update={"parameters_schema": {}}
        )
        w = ToolWorker(
            credential_repository=credential_repo,
            http_client=echo_client,
        )
        with pytest.raises(SchemaViolationError) as exc_info:
            await w.execute_with_credential(
                plan_id="plan-1",
                node=_echo_node({"text": "ok"}),
                snapshot=snap,
                actor_id="user-1",
                credential_ref=None,
            )
        details = exc_info.value.details
        assert details is not None
        assert details["tool"] == "echo"
        assert details["reason"] == "missing_schema"
        # Uniform envelope — even the missing-schema path carries
        # `violations: []` so consumers can rely on the shape.
        assert details["violations"] == []


# ---------------------------------------------------------------------------
# Snapshot-only execution (ADR-0027)
# ---------------------------------------------------------------------------


class TestSnapshotOnlyExecution:
    """The Worker never reads the live `tools` collection.

    Acceptance criterion: "使用快照不读最新 Tool 定义". The Worker's
    only Tool input is the `ToolSnapshot`; even if the live `Tool`
    row diverges, the Worker executes against the snapshot.
    """

    async def test_execute_uses_snapshot_url_not_live_tool(
        self,
        credential_repo: CredentialRepository,
        echo_client: httpx.AsyncClient,
    ) -> None:
        """Snapshot URL = `https://snapshot.test/echo` — the live row
        might point elsewhere, but the Worker hits the snapshot.
        """
        snap = _echo_snapshot().model_copy(
            update={"http_url_template": "https://snapshot.test/echo"}
        )
        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(credential_repository=credential_repo, http_client=client)
            await w.execute_with_credential(
                plan_id="p",
                node=_echo_node({"text": "x"}),
                snapshot=snap,
                actor_id="u",
                credential_ref=None,
            )
        assert seen_urls == ["https://snapshot.test/echo"]


# ---------------------------------------------------------------------------
# Credential injection (ADR-0002)
# ---------------------------------------------------------------------------


class TestCredentialInjection:
    """Credentials decrypt at call time and are stripped from audit."""

    async def test_credential_header_injected_into_outgoing_request(
        self,
        credential_repo: CredentialRepository,
    ) -> None:
        await credential_repo.create(
            CredentialCreate(
                name="echo-key",
                auth_type="api_key",
                plaintext_payload={"api_key": "sk-secret-value"},
            )
        )
        creds = await credential_repo.list_all()
        assert len(creds) == 1
        # Re-fetch with the FK pointer the executor would supply.
        row = await credential_repo.get_in_db(creds[0].id)

        seen_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(dict(request.headers))
            return httpx.Response(200, json={"ok": True})

        snap = _echo_snapshot()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(credential_repository=credential_repo, http_client=client)
            result = await w.execute_with_credential(
                plan_id="p",
                node=_echo_node({"text": "hi"}),
                snapshot=snap,
                actor_id="u",
                credential_ref=row.id,
            )
        # The secret landed in the outgoing request as `X-Api-Key`.
        assert seen_headers.get("x-api-key") == "sk-secret-value"
        # But it's redacted in the audit-grade result envelope.
        assert result.request["headers"].get("X-Api-Key") == "[REDACTED]"

    async def test_invalid_credential_payload_raises_credential_invalid(
        self,
        credential_repo: CredentialRepository,
        echo_client: httpx.AsyncClient,
    ) -> None:
        await credential_repo.create(
            CredentialCreate(
                name="bad-key",
                auth_type="api_key",
                plaintext_payload={"unknown_field": "value"},
            )
        )
        creds = await credential_repo.list_all()
        row = await credential_repo.get_in_db(creds[0].id)

        w = ToolWorker(credential_repository=credential_repo, http_client=echo_client)
        with pytest.raises(CredentialInvalidError):
            await w.execute_with_credential(
                plan_id="p",
                node=_echo_node({"text": "x"}),
                snapshot=_echo_snapshot(),
                actor_id="u",
                credential_ref=row.id,
            )

    async def test_no_credential_ref_skips_auth_header(
        self, worker: ToolWorker
    ) -> None:
        """`credential_ref=None` → no auth header in the outgoing request."""
        result = await worker.execute_with_credential(
            plan_id="p",
            node=_echo_node({"text": "x"}),
            snapshot=_echo_snapshot(),
            actor_id="u",
            credential_ref=None,
        )
        # The static `Content-Type` from the snapshot lands; no auth header.
        assert "X-Api-Key" not in result.request["headers"]
        assert result.request["headers"]["Content-Type"] == "application/json"

    async def test_body_credential_field_redacted_in_audit_envelope(
        self,
        credential_repo: CredentialRepository,
    ) -> None:
        """T33 / #29: an `api_key` body field is scrubbed from the audit envelope.

        Defence-in-depth: the Worker's header scrubber (T21) caught
        `Authorization` / `X-Api-Key`, but an upstream API that takes
        the credential inside the JSON body would have leaked. The
        new envelope scrubber walks the body recursively so the
        `audit_logs` row + future Langfuse trace never see the secret.
        """
        snap = ToolSnapshot(
            name="body_cred",
            description="Tool that takes an api_key in body",
            risk_level="read",
            parameters_schema={
                "type": "object",
                "properties": {"customer": {"type": "string"}},
                "required": ["customer"],
            },
            http_method="POST",
            http_url_template="https://upstream.test/body",
            http_headers={"Content-Type": "application/json"},
            http_body_template={"customer": "{customer}", "api_key": "sk-secret-1234"},
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(credential_repository=credential_repo, http_client=client)
            result = await w.execute_with_credential(
                plan_id="p",
                node=PlanNode(
                    node_id="n1",
                    tool="body_cred",
                    parameters={"customer": "ACME"},
                ),
                snapshot=snap,
                actor_id="u",
                credential_ref=None,
            )
        # The outgoing request carried the secret (the upstream needs it).
        # But the audit envelope sees it scrubbed.
        assert result.request["body"]["api_key"] == "[REDACTED]"
        assert result.request["body"]["customer"] == "ACME"

    async def test_audit_envelope_has_no_credential_bytes(
        self,
        credential_repo: CredentialRepository,
    ) -> None:
        """T33 acceptance — `result.request` carries no plaintext credential.

        Mirrors the worker's full outgoing-request envelope and asserts
        that a downstream consumer (audit log, Langfuse trace) can't
        recover the secret by walking the dict.
        """
        await credential_repo.create(
            CredentialCreate(
                name="echo-key-2",
                auth_type="api_key",
                plaintext_payload={"api_key": "sk-secret-value"},
            )
        )
        creds = await credential_repo.list_all()
        row = await credential_repo.get_in_db(creds[0].id)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(credential_repository=credential_repo, http_client=client)
            result = await w.execute_with_credential(
                plan_id="p",
                node=_echo_node({"text": "x"}),
                snapshot=_echo_snapshot(),
                actor_id="u",
                credential_ref=row.id,
            )
        # Walk the whole envelope to be sure no field carries the secret.
        serialised = json.dumps(result.request)
        assert "sk-secret-value" not in serialised
        assert result.request["headers"].get("X-Api-Key") == "[REDACTED]"


# ---------------------------------------------------------------------------
# Risk-level retry matrix (ADR-0017)
# ---------------------------------------------------------------------------


class TestRetryMatrix:
    """Read-class retries; write/destructive escalate immediately."""

    async def test_read_class_retries_on_5xx_then_succeeds(
        self,
        credential_repo: CredentialRepository,
    ) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] < 3:
                return httpx.Response(503, json={"err": "down"})
            return httpx.Response(200, json={"echo": "ok"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(
                credential_repository=credential_repo,
                http_client=client,
            )
            result = await w.execute_with_credential(
                plan_id="p",
                node=_echo_node({"text": "x"}),
                snapshot=_echo_snapshot(),  # risk_level=read
                actor_id="u",
                credential_ref=None,
            )
        # 1 initial + 2 retries = 3 calls; succeeded on the third.
        assert call_count["n"] == 1 + READ_MAX_RETRIES
        assert result.status == "succeeded"
        assert result.retry_count == READ_MAX_RETRIES

    async def test_read_class_exhausts_retries_then_escalates_to_hitl(
        self,
        credential_repo: CredentialRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Read budget exhausted → HITLRequiredError, not silent failure."""
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(503, json={"err": "down"})

        # Sleep zero so the test doesn't wait on real backoff.
        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(
                credential_repository=credential_repo,
                http_client=client,
            )
            with pytest.raises(HITLRequiredError) as exc_info:
                await w.execute_with_credential(
                    plan_id="p",
                    node=_echo_node({"text": "x"}),
                    snapshot=_echo_snapshot(),
                    actor_id="u",
                    credential_ref=None,
                )
        # 1 initial + 2 retries = 3 calls before escalation.
        assert call_count["n"] == 1 + READ_MAX_RETRIES
        details = exc_info.value.details
        assert details is not None
        assert details["tool"] == "echo"
        # Cause carries the underlying error code so the UI can render
        # a useful message.
        assert details["cause"]["code"] == "upstream_error"

    async def test_read_class_4xx_does_not_retry(
        self,
        credential_repo: CredentialRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(404, json={"err": "not found"})

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(
                credential_repository=credential_repo,
                http_client=client,
            )
            with pytest.raises(HITLRequiredError):
                await w.execute_with_credential(
                    plan_id="p",
                    node=_echo_node({"text": "x"}),
                    snapshot=_echo_snapshot(),
                    actor_id="u",
                    credential_ref=None,
                )
        # 4xx is deterministic — exactly one attempt.
        assert call_count["n"] == 1

    async def test_write_class_does_not_retry_on_5xx(
        self,
        credential_repo: CredentialRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(500, json={"err": "down"})

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(
                credential_repository=credential_repo,
                http_client=client,
            )
            with pytest.raises(HITLRequiredError):
                await w.execute_with_credential(
                    plan_id="p",
                    node=PlanNode(
                        node_id="n1",
                        tool="create_invoice",
                        parameters={"amount": 100, "customer": "ACME"},
                    ),
                    snapshot=_write_snapshot(),
                    actor_id="u",
                    credential_ref=None,
                )
        # write: exactly one attempt, then HITL — no auto-retry.
        assert call_count["n"] == 1

    async def test_write_class_escalates_immediately_on_4xx(
        self,
        credential_repo: CredentialRepository,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"err": "bad"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(
                credential_repository=credential_repo,
                http_client=client,
            )
            with pytest.raises(HITLRequiredError) as exc_info:
                await w.execute_with_credential(
                    plan_id="p",
                    node=PlanNode(
                        node_id="n1",
                        tool="create_invoice",
                        parameters={"amount": 100, "customer": "ACME"},
                    ),
                    snapshot=_write_snapshot(),
                    actor_id="u",
                    credential_ref=None,
                )
        details = exc_info.value.details
        assert details is not None
        assert details["risk_level"] == "write"

    async def test_destructive_class_also_does_not_retry(
        self,
        credential_repo: CredentialRepository,
    ) -> None:
        """Destructive behaves the same as write per ADR-0017."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"err": "down"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(
                credential_repository=credential_repo,
                http_client=client,
            )
            with pytest.raises(HITLRequiredError) as exc_info:
                await w.execute_with_credential(
                    plan_id="p",
                    node=PlanNode(
                        node_id="n1",
                        tool="purge_account",
                        parameters={"account_id": "acct-123"},
                    ),
                    snapshot=_destructive_snapshot(),
                    actor_id="u",
                    credential_ref=None,
                )
        details = exc_info.value.details
        assert details is not None
        assert details["risk_level"] == "destructive"


# ---------------------------------------------------------------------------
# Timeout (ADR-0026)
# ---------------------------------------------------------------------------


class TestTimeout:
    """Per-Tool timeout drives the error envelope."""

    async def test_timeout_classified_as_separate_error_code(
        self,
        credential_repo: CredentialRepository,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated")

        # Tight timeout so a real `asyncio.sleep` isn't needed.
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(
                credential_repository=credential_repo,
                http_client=client,
                timeout_seconds=0.01,
            )
            with pytest.raises(HITLRequiredError) as exc_info:
                await w.execute_with_credential(
                    plan_id="p",
                    node=_echo_node({"text": "x"}),
                    snapshot=_echo_snapshot(),
                    actor_id="u",
                    credential_ref=None,
                )
        details = exc_info.value.details
        assert details is not None
        assert details["cause"]["code"] == "timeout"


# ---------------------------------------------------------------------------
# Timeout × risk-level retry matrix (T35 / #31 — ADR-0026)
# ---------------------------------------------------------------------------


class TestTimeoutRetryByRiskLevel:
    """T35 / #31 — timeout follows the ADR-0017 risk-level matrix.

    Acceptance criteria:

    * read + timeout → retry up to 2 times, then escalate with `code=timeout`.
    * write / destructive + timeout → stop immediately, no retry.

    The retry logic doesn't distinguish 5xx from timeout — both flow
    through the same retryable branch in `_call_with_retry`. These
    tests pin that behaviour so a future refactor can't silently
    demote timeouts to non-retryable (which would turn transient
    upstream slowness into a permanent failure).
    """

    async def test_read_class_retries_on_timeout_then_succeeds(
        self,
        credential_repo: CredentialRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """read + intermittent timeout → 1 initial + 2 retries, then succeed."""
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise httpx.TimeoutException("simulated")
            return httpx.Response(200, json={"echo": "ok"})

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(
                credential_repository=credential_repo,
                http_client=client,
                timeout_seconds=0.01,
            )
            result = await w.execute_with_credential(
                plan_id="p",
                node=_echo_node({"text": "x"}),
                snapshot=_echo_snapshot(),  # risk_level=read
                actor_id="u",
                credential_ref=None,
            )
        # 1 initial + 2 retries = 3 calls; succeeded on the third.
        assert call_count["n"] == 1 + READ_MAX_RETRIES
        assert result.status == "succeeded"
        assert result.retry_count == READ_MAX_RETRIES

    async def test_read_class_exhausts_timeout_retries_then_escalates_to_hitl(
        self,
        credential_repo: CredentialRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """read + persistent timeout → 3 attempts, escalate with code='timeout'."""
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            raise httpx.TimeoutException("simulated")

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(
                credential_repository=credential_repo,
                http_client=client,
                timeout_seconds=0.01,
            )
            with pytest.raises(HITLRequiredError) as exc_info:
                await w.execute_with_credential(
                    plan_id="p",
                    node=_echo_node({"text": "x"}),
                    snapshot=_echo_snapshot(),
                    actor_id="u",
                    credential_ref=None,
                )
        # T35 / #31 — read exhausted, hit HITL with `code=timeout`.
        assert call_count["n"] == 1 + READ_MAX_RETRIES
        details = exc_info.value.details
        assert details is not None
        assert details["cause"]["code"] == "timeout"
        # The attempts log captures every observed timeout — Planner
        # / audit UI can show how long the upstream was unresponsive.
        # 3 attempts total: 1 initial + 2 retries.
        attempts = details["attempts"]
        assert len(attempts) == 1 + READ_MAX_RETRIES
        assert all(a["error"] == "timeout" for a in attempts)

    async def test_write_class_stops_on_timeout_no_retry(
        self,
        credential_repo: CredentialRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """write + timeout → exactly 1 attempt, then HITL with code='timeout'.

        Per ADR-0017 the write class never auto-retries — a second
        POST could re-create the row the user already saw fail, and
        we don't want to double-charge. T35 / #31 pins this for the
        timeout path specifically.
        """
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            raise httpx.TimeoutException("simulated")

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(
                credential_repository=credential_repo,
                http_client=client,
                timeout_seconds=0.01,
            )
            with pytest.raises(HITLRequiredError) as exc_info:
                await w.execute_with_credential(
                    plan_id="p",
                    node=PlanNode(
                        node_id="n1",
                        tool="create_invoice",
                        parameters={"amount": 100, "customer": "ACME"},
                    ),
                    snapshot=_write_snapshot(),
                    actor_id="u",
                    credential_ref=None,
                )
        # write: exactly one attempt, no retry.
        assert call_count["n"] == 1
        details = exc_info.value.details
        assert details is not None
        assert details["risk_level"] == "write"
        assert details["cause"]["code"] == "timeout"
        # write / destructive don't accumulate an `attempts` array —
        # they fail on the first call and the human decides from
        # there.
        assert "attempts" not in details

    async def test_destructive_class_stops_on_timeout_no_retry(
        self,
        credential_repo: CredentialRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """destructive + timeout → exactly 1 attempt, then HITL with code='timeout'."""
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            raise httpx.TimeoutException("simulated")

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _no_sleep)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            w = ToolWorker(
                credential_repository=credential_repo,
                http_client=client,
                timeout_seconds=0.01,
            )
            with pytest.raises(HITLRequiredError) as exc_info:
                await w.execute_with_credential(
                    plan_id="p",
                    node=PlanNode(
                        node_id="n1",
                        tool="purge_account",
                        parameters={"account_id": "acct-123"},
                    ),
                    snapshot=_destructive_snapshot(),
                    actor_id="u",
                    credential_ref=None,
                )
        assert call_count["n"] == 1
        details = exc_info.value.details
        assert details is not None
        assert details["risk_level"] == "destructive"
        assert details["cause"]["code"] == "timeout"


# ---------------------------------------------------------------------------
# Exponential backoff schedule (T35 / #31 — ADR-0017 §1)
# ---------------------------------------------------------------------------


class TestExponentialBackoffSchedule:
    """T35 / #31 — backoff is 1s → 2s → 4s, capped at 8s.

    The schedule is private to `_backoff`; these tests pin it so an
    off-by-one (e.g. `2 ** attempt` instead of `2 ** (attempt-1)`)
    can't silently shift the curve. We invoke `_backoff` directly
    with a stub `asyncio.sleep` that records the requested delay
    rather than waiting.
    """

    async def test_backoff_schedule_1_2_4_then_cap(
        self,
        credential_repo: CredentialRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `_backoff` is pure — it only touches `asyncio.sleep`. We
        # give the Worker a placeholder http client so the constructor
        # accepts it; the client is never invoked by this test.
        worker = ToolWorker(
            credential_repository=credential_repo,
            http_client=httpx.AsyncClient(),
        )
        delays: list[float] = []

        async def _record_sleep(delay: float) -> None:
            delays.append(delay)

        # `_backoff` reads `asyncio.sleep` from the module scope; the
        # cleanest way to stub it is `monkeypatch.setattr` on the
        # module — `app.tools.worker.asyncio` works because the import
        # statement puts the module reference on the module's globals.
        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _record_sleep)
        for attempt in (1, 2, 3, 4, 5):
            await worker._backoff(attempt)

        # T35 / #31 — exponential 1s → 2s → 4s, then the 8s cap
        # absorbs attempt 4 (8 * 1) and any deeper attempt.
        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]
        # Pin the cap constant too — an admin changing the global
        # `RETRY_BACKOFF_CAP_SECONDS` should break this test loudly.
        assert RETRY_BACKOFF_CAP_SECONDS == 8.0

    async def test_backoff_uses_capped_formula(
        self,
        credential_repo: CredentialRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity check: a very large attempt number never exceeds the cap."""
        worker = ToolWorker(
            credential_repository=credential_repo,
            http_client=httpx.AsyncClient(),
        )
        delays: list[float] = []

        async def _record_sleep(delay: float) -> None:
            delays.append(delay)

        monkeypatch.setattr("app.tools.worker.asyncio.sleep", _record_sleep)
        for attempt in (10, 20):
            await worker._backoff(attempt)

        assert delays == [RETRY_BACKOFF_CAP_SECONDS, RETRY_BACKOFF_CAP_SECONDS]


# ---------------------------------------------------------------------------
# Constants — guard against accidental drift
# ---------------------------------------------------------------------------


def test_default_constants_match_adr() -> None:
    """Pin the ADR-0017 / ADR-0026 defaults so an accidental change is caught."""
    assert READ_MAX_RETRIES == 2
    assert RETRY_BACKOFF_SECONDS == 1.0
    assert DEFAULT_TIMEOUT_SECONDS == 30.0


__all__: list[str] = []
