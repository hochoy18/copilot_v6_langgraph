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

    async def test_no_schema_declared_skips_validation(
        self, echo_client: httpx.AsyncClient, credential_repo: CredentialRepository
    ) -> None:
        """A snapshot without a schema still runs — defensive choice (ADR-0027)."""
        snap = _echo_snapshot().model_copy(
            update={"parameters_schema": {}}
        )
        w = ToolWorker(
            credential_repository=credential_repo,
            http_client=echo_client,
        )
        result = await w.execute_with_credential(
            plan_id="plan-1",
            node=_echo_node({"text": "ok", "extra": "data"}),
            snapshot=snap,
            actor_id="user-1",
            credential_ref=None,
        )
        assert result.status == "succeeded"


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
# Constants — guard against accidental drift
# ---------------------------------------------------------------------------


def test_default_constants_match_adr() -> None:
    """Pin the ADR-0017 / ADR-0026 defaults so an accidental change is caught."""
    assert READ_MAX_RETRIES == 2
    assert RETRY_BACKOFF_SECONDS == 1.0
    assert DEFAULT_TIMEOUT_SECONDS == 30.0


__all__: list[str] = []
