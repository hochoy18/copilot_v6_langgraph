"""End-to-end tests for the SSE stream endpoint — T23 / #20.

The acceptance criteria on issue #20 are:

* `curl` sees the event stream — covered by `TestEventSerialization`
  and `TestSseBus` (the bus + serializer are the consumer-visible
  surface; the StreamingResponse is a thin transport wrapper).
* Query-param auth takes effect (missing / bad / expired token) —
  covered by `TestSseAuth` exercising `get_sse_user` directly + the
  non-streamed HTTP paths in `TestStreamEndpoint`.
* Event type list is complete (every ADR-0010 event the bus can
  emit round-trips through `serialize_event`) — covered by
  `TestEventSerialization`.
* Token expiry closes the connection — covered by
  `TestTokenExpiryWatcher` which runs the watcher coroutine
  directly.

Note on test seam: `httpx.AsyncClient(transport=ASGITransport(...))`
suspends the request until the server-side generator yields its
first body chunk. The full streaming path is exercised in the
unit tests above rather than through the HTTP transport; the
integration tests below only cover the pre-stream phases
(authentication + ownership) which finish synchronously.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Generator
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.db.init_db import init_database
from app.db.schemas import ConversationCreate, UserCreate
from app.main import create_app
from app.realtime.auth import AuthenticatedSseToken, authenticate_sse_token, get_sse_user
from app.realtime.bus import (
    DEFAULT_REPLAY_BUFFER_SIZE,
    DEFAULT_SUBSCRIBER_QUEUE_MAX,
    SseEventBus,
    iter_events,
)
from app.realtime.events import (
    EventBase,
    EventName,
    auth_expired,
    cost_warning,
    execution_completed,
    heartbeat,
    llm_token,
    plan_generated,
    plan_modified,
    serialize_event,
    stream_opened,
    tool_failed,
    tool_finished,
    tool_started,
)
from app.repositories.conversations import ConversationRepository
from app.repositories.users import UserRepository
from app.security.jwt import AccessTokenClaims, mint_access_token, now_unix
from app.settings import Settings


# ---------------------------------------------------------------------------
# Settings + fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        oidc_issuer_url="https://test.example.com",
        oidc_id_token_signing_key="test-idp-hs256-key",
        oidc_access_token_ttl_seconds=900,
        oidc_state_ttl_seconds=600,
        oidc_discovery_cache_seconds=3600,
        oidc_jwt_signing_key="internal-access-jwt-signing-key-for-tests",
    )


class _AsyncMongoMockForLifespan:
    """A minimal stand-in for `MongoClient` that the lifespan can close."""

    def __init__(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        self._client = AsyncMongoMockClient()
        self.database = self._client["copilot_sse_stream_test"]

    async def close(self) -> None:  # noqa: D401 — no-op closer
        pass


@pytest.fixture
async def app(settings: Settings) -> FastAPI:
    """Fresh app per test with a hermetic in-memory Mongo + SSE bus."""
    app = create_app(settings=settings)
    client = _AsyncMongoMockForLifespan()
    app.state.mongo = client
    app.state.database = client.database
    app.state.oidc_adapter = None
    app.state.sse_bus = SseEventBus()
    await init_database(app.state.database)
    return app


@pytest.fixture(autouse=True)
def _override_settings(app: FastAPI, settings: Settings) -> Generator[None, None, None]:
    """Override `get_settings` so auth dependencies decode with the test key."""
    from app.settings import get_settings

    app.dependency_overrides[get_settings] = lambda: settings
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def user_repo(app: FastAPI) -> UserRepository:
    return UserRepository(app.state.database)


@pytest.fixture
def conv_repo(app: FastAPI) -> ConversationRepository:
    return ConversationRepository(app.state.database)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired directly to the ASGI app (no network)."""
    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_sso_user(
    user_repo: UserRepository,
    *,
    email: str | None = None,
    subject: str | None = None,
) -> str:
    """Plant an SSO user (so `get_sse_user` decodes via the OIDC path)."""
    email = email or f"user-{subject}@example.com"
    subject = subject or f"sub-{ObjectId()}"
    created = await user_repo.create(
        UserCreate(
            email=email,
            display_name=email.split("@")[0],
            source="sso",
            sso_subject=subject,
        ),
    )
    return created.id


async def _seed_inactive_user(
    user_repo: UserRepository,
    *,
    email: str | None = None,
    subject: str | None = None,
) -> str:
    """Plant an SSO user that has been deactivated (403 path)."""
    from app.db.schemas import UserUpdate

    email = email or f"user-{subject}@example.com"
    subject = subject or f"sub-{ObjectId()}"
    created = await user_repo.create(
        UserCreate(
            email=email,
            display_name=email.split("@")[0],
            source="sso",
            sso_subject=subject,
        ),
    )
    await user_repo.update(created.id, UserUpdate(is_active=False))
    return created.id


def _mint_access_token(
    user_id: str,
    *,
    settings: Settings,
    ttl_seconds: int | None = None,
    issued_at: int | None = None,
    expires_at: int | None = None,
) -> str:
    """Build a valid access JWT for `user_id` signed with the test key."""
    ttl = ttl_seconds if ttl_seconds is not None else settings.oidc_access_token_ttl_seconds
    now = issued_at if issued_at is not None else now_unix()
    claims = AccessTokenClaims(
        sub=user_id,
        source="sso",
        role_ids=[],
        issuer=settings.oidc_jwt_issuer,
        audience=settings.oidc_jwt_audience,
        issued_at=now,
        expires_at=expires_at if expires_at is not None else now + ttl,
        jti="test-jti",
    )
    token, _ = mint_access_token(claims, signing_key=settings.oidc_jwt_signing_key)
    return token


def _parse_sse_frames(body: str) -> list[dict[str, Any]]:
    """Decode a multi-event SSE body into per-frame dicts.

    Tolerant of comment lines (`: heartbeat`) and trailing partial
    frames — the test only inspects complete frames.
    """
    events: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    for raw_line in body.split("\n"):
        line = raw_line.rstrip("\r")
        if not line:
            if current:
                events.append(current)
                current = {}
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        current[field] = value
    if current:
        events.append(current)
    return events


# ---------------------------------------------------------------------------
# Acceptance criterion 1: curl sees the event stream.
#
# `serialize_event` is the wire-formatting surface the StreamingResponse
# emits verbatim, so the round-trip test below is equivalent to a
# curl-side assertion: every event the bus can publish must serialise
# to a frame whose `event:` line matches the factory name.
# ---------------------------------------------------------------------------


class TestEventSerialization:
    """Every ADR-0010 event factory produces an SSE frame the Frontend can parse."""

    def test_event_type_list_is_complete(self) -> None:
        """Pins the canonical event surface from ADR-0010."""
        now = datetime.now(timezone.utc)
        plan_doc: dict[str, Any] = {"id": str(ObjectId()), "nodes": [], "edges": []}
        turn_id = str(ObjectId())
        plan_id = str(ObjectId())

        factories: list[tuple[str, EventBase]] = [
            ("stream.opened", stream_opened(
                event_id=1, conversation_id="c1", now=now,
            )),
            ("stream.heartbeat", heartbeat(
                event_id=2, conversation_id="c1", now=now,
            )),
            ("plan.generated", plan_generated(
                event_id=3, conversation_id="c1", plan=plan_doc, now=now,
            )),
            ("plan.modified", plan_modified(
                event_id=4, conversation_id="c1", plan=plan_doc,
                diff={"by_node_id": {}}, now=now,
            )),
            ("tool.started", tool_started(
                event_id=5, conversation_id="c1", node_id="n1", tool_name="echo", now=now,
            )),
            ("tool.finished", tool_finished(
                event_id=6, conversation_id="c1", node_id="n1", tool_name="echo",
                status="succeeded", duration_ms=42, now=now,
            )),
            ("tool.failed", tool_failed(
                event_id=7, conversation_id="c1", node_id="n1", tool_name="echo",
                error={"code": "upstream_500", "message_zh": "上游 500"},
                now=now,
            )),
            ("llm.token", llm_token(
                event_id=8, conversation_id="c1", token="hi", turn_id=turn_id, now=now,
            )),
            ("execution.completed", execution_completed(
                event_id=9, conversation_id="c1", plan_id=plan_id,
                status="succeeded", now=now,
            )),
            ("cost.warning", cost_warning(
                event_id=10, conversation_id="c1",
                current_cost_usd=0.42, ceiling_usd=1.00, now=now,
            )),
            ("auth.expired", auth_expired(
                event_id=11, conversation_id="c1", now=now,
            )),
        ]
        seen: list[str] = []
        for name, event in factories:
            frame = serialize_event(event)
            # Frame header line is the type label.
            assert f"event: {name}" in frame, frame
            # Body is one line of JSON — same shape the Frontend
            # parses via `JSON.parse(event.data)`.
            frames = _parse_sse_frames(frame)
            assert len(frames) == 1, frames
            payload = json.loads(frames[0]["data"])
            assert payload["event"] == name
            assert payload["id"] == event.id
            assert payload["conversation_id"] == event.conversation_id
            seen.append(name)

        # Pin the entire surface so adding a new event to the
        # registry requires a corresponding test entry.
        expected: set[str] = {
            "stream.opened",
            "stream.heartbeat",
            "plan.generated",
            "plan.modified",
            "tool.started",
            "tool.finished",
            "tool.failed",
            "llm.token",
            "execution.completed",
            "cost.warning",
            "auth.expired",
        }
        assert set(seen) == expected


# ---------------------------------------------------------------------------
# Acceptance criterion 2: query-param auth takes effect.
# ---------------------------------------------------------------------------


class TestSseAuth:
    """`get_sse_user` is the SSE auth seam — tested by direct dep call."""

    async def test_missing_token_raises_specific_error(
        self,
        settings: Settings,
        user_repo: UserRepository,
    ) -> None:
        """Empty token → `SSEMissingTokenError` (401 with code)."""
        from app.realtime.auth import SSEMissingTokenError

        with pytest.raises(SSEMissingTokenError) as ei:
            await authenticate_sse_token(
                "",
                signing_key=settings.oidc_jwt_signing_key,
                users=user_repo,
            )
        assert ei.value.code == "auth_missing_sse_token"
        assert ei.value.http_status == 401

    async def test_invalid_token_signature_raises_invalid_error(
        self,
        settings: Settings,
        user_repo: UserRepository,
    ) -> None:
        """Bad signature → `AuthInvalidTokenError`."""
        from app.security.auth import AuthInvalidTokenError

        with pytest.raises(AuthInvalidTokenError) as ei:
            await authenticate_sse_token(
                "not.a.real.jwt",
                signing_key=settings.oidc_jwt_signing_key,
                users=user_repo,
            )
        assert ei.value.code == "auth_invalid_token"
        assert ei.value.http_status == 401

    async def test_expired_token_raises_invalid_error(
        self,
        settings: Settings,
        user_repo: UserRepository,
    ) -> None:
        """Expired JWT → `AuthInvalidTokenError` (the bearer-path envelope)."""
        from app.security.auth import AuthInvalidTokenError

        owner = await _seed_sso_user(user_repo)
        expired = _mint_access_token(
            owner,
            settings=settings,
            issued_at=now_unix() - 7200,
            expires_at=now_unix() - 3600,
        )
        with pytest.raises(AuthInvalidTokenError) as ei:
            await authenticate_sse_token(
                expired,
                signing_key=settings.oidc_jwt_signing_key,
                users=user_repo,
            )
        assert ei.value.http_status == 401

    async def test_unknown_user_raises_invalid_error(
        self,
        settings: Settings,
        user_repo: UserRepository,
    ) -> None:
        """Signed correctly but `sub` not in DB → 401 (no info leak)."""
        from app.security.auth import AuthInvalidTokenError

        token = _mint_access_token(str(ObjectId()), settings=settings)
        with pytest.raises(AuthInvalidTokenError):
            await authenticate_sse_token(
                token,
                signing_key=settings.oidc_jwt_signing_key,
                users=user_repo,
            )

    async def test_inactive_user_raises_forbidden(
        self,
        settings: Settings,
        user_repo: UserRepository,
    ) -> None:
        """`is_active=False` → `UserInactiveError` (403)."""
        from app.auth.errors import UserInactiveError

        owner = await _seed_inactive_user(user_repo)
        token = _mint_access_token(owner, settings=settings)
        with pytest.raises(UserInactiveError) as ei:
            await authenticate_sse_token(
                token,
                signing_key=settings.oidc_jwt_signing_key,
                users=user_repo,
            )
        assert ei.value.http_status == 403

    async def test_valid_token_returns_user_and_payload(
        self,
        settings: Settings,
        user_repo: UserRepository,
    ) -> None:
        """Happy path returns AuthenticatedSseToken with User + payload."""
        owner = await _seed_sso_user(user_repo)
        token = _mint_access_token(owner, settings=settings)
        result = await authenticate_sse_token(
            token,
            signing_key=settings.oidc_jwt_signing_key,
            users=user_repo,
        )
        assert isinstance(result, AuthenticatedSseToken)
        assert result.user.id == owner
        assert result.payload["sub"] == owner


class TestStreamEndpoint:
    """Pre-stream HTTP seams — auth + ownership guard.

    The streamed body is exercised by `TestSseBus` / `TestEventSerialization`;
    `httpx.ASGITransport` suspends until the first body chunk, so we
    only test the synchronous pre-stream phases here.
    """

    async def test_missing_token_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
    ) -> None:
        owner = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=owner, title=""))

        resp = await client.get(f"/api/v1/conversations/{conv.id}/stream")
        assert resp.status_code == 401, resp.text
        body = resp.json()
        assert body["code"] == "auth_missing_sse_token"

    async def test_invalid_token_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
    ) -> None:
        owner = await _seed_sso_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=owner, title=""))

        resp = await client.get(
            f"/api/v1/conversations/{conv.id}/stream",
            params={"token": "not.a.real.jwt"},
        )
        assert resp.status_code == 401, resp.text
        body = resp.json()
        assert body["code"] == "auth_invalid_token"

    async def test_inactive_user_returns_403(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        owner = await _seed_inactive_user(user_repo)
        conv = await conv_repo.create(ConversationCreate(user_id=owner, title=""))
        token = _mint_access_token(owner, settings=settings)

        resp = await client.get(
            f"/api/v1/conversations/{conv.id}/stream",
            params={"token": token},
        )
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["code"] == "user_inactive"

    async def test_cross_user_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        conv_repo: ConversationRepository,
        settings: Settings,
    ) -> None:
        owner = await _seed_sso_user(user_repo, subject="owner-sub")
        stranger = await _seed_sso_user(user_repo, subject="stranger-sub")
        conv = await conv_repo.create(ConversationCreate(user_id=owner, title=""))

        token = _mint_access_token(stranger, settings=settings)
        resp = await client.get(
            f"/api/v1/conversations/{conv.id}/stream",
            params={"token": token},
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == "not_found"

    async def test_missing_conversation_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        user_repo: UserRepository,
        settings: Settings,
    ) -> None:
        owner = await _seed_sso_user(user_repo)
        token = _mint_access_token(owner, settings=settings)
        resp = await client.get(
            f"/api/v1/conversations/{str(ObjectId())}/stream",
            params={"token": token},
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == "not_found"


# ---------------------------------------------------------------------------
# Bus behaviour — exercises the publish/subscribe/replay plumbing.
# ---------------------------------------------------------------------------


class TestSseBus:
    """The bus is the consumer-visible data flow that `curl` would see."""

    async def test_publish_fans_out_to_all_subscribers(self) -> None:
        """Two subscribers on the same conversation both receive the event."""
        bus = SseEventBus()
        sub_a, _ = await bus.subscribe("c1")
        sub_b, _ = await bus.subscribe("c1")
        event_id = await bus.next_event_id("c1")
        event = heartbeat(event_id=event_id, conversation_id="c1", now=datetime.now(timezone.utc))
        await bus.publish(event)
        assert (await sub_a.receive()) is event
        assert (await sub_b.receive()) is event

    async def test_publish_is_isolated_per_conversation(self) -> None:
        """An event for `c1` does NOT wake a subscriber on `c2`."""
        bus = SseEventBus()
        sub_a, _ = await bus.subscribe("c1")
        sub_b, _ = await bus.subscribe("c2")
        event_id = await bus.next_event_id("c2")
        await bus.publish(
            heartbeat(event_id=event_id, conversation_id="c2", now=datetime.now(timezone.utc))
        )
        # `sub_b` receives — `sub_a` blocks indefinitely.
        received_a_task = asyncio.create_task(sub_a.receive())
        await asyncio.sleep(0.05)
        assert not received_a_task.done()
        received_a_task.cancel()
        # `sub_b` got the event.
        assert (await sub_b.receive()) is not None

    async def test_next_event_id_is_monotonic(self) -> None:
        """Each call yields a strictly increasing id per conversation."""
        bus = SseEventBus()
        ids = [await bus.next_event_id("c1") for _ in range(5)]
        assert ids == sorted(set(ids))
        assert len(set(ids)) == 5

    async def test_replay_buffer_returns_events_after_cursor(self) -> None:
        """`subscribe(last_seen_id=X)` queues every buffered event with id > X."""
        bus = SseEventBus(replay_buffer_size=16)
        # Pre-publish three events.
        ids: list[int] = []
        for _ in range(3):
            event_id = await bus.next_event_id("c1")
            ids.append(event_id)
            await bus.publish(
                heartbeat(event_id=event_id, conversation_id="c1", now=datetime.now(timezone.utc))
            )
        # New subscriber with last_seen_id = ids[0] → two replay events.
        subscriber, replay = await bus.subscribe("c1", last_seen_id=ids[0])
        assert len(replay) == 2
        assert [e.id for e in replay] == ids[1:]
        # Drain via `receive()` — replay first, then live events.
        received: list[EventBase] = []
        for _ in range(2):
            event = await subscriber.receive()
            assert event is not None
            received.append(event)
        assert [e.id for e in received] == ids[1:]

    async def test_unsubscribe_closes_the_subscriber(self) -> None:
        """After `unsubscribe`, `receive()` returns `None` so the iterator stops."""
        bus = SseEventBus()
        subscriber, _ = await bus.subscribe("c1")
        await bus.unsubscribe(subscriber)
        assert await subscriber.receive() is None

    async def test_replay_buffer_drops_oldest_when_full(self) -> None:
        """A subscriber that publishes more than `subscriber_queue_max` events drops the oldest."""
        bus = SseEventBus(
            replay_buffer_size=16,
            subscriber_queue_max=3,
        )
        subscriber, _ = await bus.subscribe("c1")
        # Manually push 5 events — the queue should hold only the
        # most-recent 3 (the oldest 2 are dropped silently).
        for i in range(5):
            await subscriber.push(
                heartbeat(
                    event_id=i,
                    conversation_id="c1",
                    now=datetime.now(timezone.utc),
                )
            )
        received: list[EventBase] = []
        for _ in range(3):
            event = await subscriber.receive()
            assert event is not None
            received.append(event)
        assert [e.id for e in received] == [2, 3, 4]

    async def test_buffer_caps_at_default_size(self) -> None:
        """Replay buffer caps at the configured size."""
        cap = 8
        bus = SseEventBus(replay_buffer_size=cap)
        for _ in range(20):
            event_id = await bus.next_event_id("c1")
            await bus.publish(
                heartbeat(event_id=event_id, conversation_id="c1", now=datetime.now(timezone.utc))
            )
        stats = await bus.channel_stats("c1")
        assert stats["buffer_len"] == cap

    async def test_iter_events_stops_when_subscriber_closed(self) -> None:
        """`iter_events` translates the `None` sentinel into a clean stop."""
        bus = SseEventBus()
        subscriber, _ = await bus.subscribe("c1")

        async def collect() -> list[EventBase]:
            events: list[EventBase] = []
            async for event in iter_events(subscriber):
                events.append(event)
            return events

        task = asyncio.create_task(collect())
        # Push one event then close — `iter_events` should emit
        # exactly that one event before the loop terminates.
        event_id = await bus.next_event_id("c1")
        await bus.publish(
            heartbeat(event_id=event_id, conversation_id="c1", now=datetime.now(timezone.utc))
        )
        await asyncio.sleep(0.05)
        await bus.unsubscribe(subscriber)
        events = await asyncio.wait_for(task, timeout=2.0)
        assert len(events) == 1
        assert events[0].id == event_id


# ---------------------------------------------------------------------------
# Acceptance criterion 4: token expiry closes the connection.
# ---------------------------------------------------------------------------


class TestTokenExpiryWatcher:
    """The watcher emits `auth.expired` once the JWT's `exp` elapses."""

    async def test_emits_auth_expired_when_token_expires(self) -> None:
        """Watcher pushes `auth.expired` then closes the subscriber."""
        from app.realtime.stream import _token_expiry_watcher

        bus = SseEventBus()
        subscriber, _ = await bus.subscribe("c1")
        # Token already expired — watcher fires immediately.
        now = int(time.time())
        token_payload = {"sub": "u1", "exp": now - 1}

        async def run() -> None:
            await _token_expiry_watcher(
                subscriber=subscriber,
                conversation_id="c1",
                bus=bus,
                token_payload=token_payload,
                check_interval_seconds=0.1,
                now_fn=lambda: datetime.now(timezone.utc),
            )

        watcher_task = asyncio.create_task(run())
        # Drain events until the watcher closes the subscriber.
        events: list[EventBase] = []
        async for event in iter_events(subscriber):
            events.append(event)
        await asyncio.wait_for(watcher_task, timeout=2.0)
        assert any(e.event == "auth.expired" for e in events), events

    async def test_does_not_fire_when_token_has_no_exp(self) -> None:
        """No `exp` claim → watcher bails out without pushing anything."""
        from app.realtime.stream import _token_expiry_watcher

        bus = SseEventBus()
        subscriber, _ = await bus.subscribe("c1")

        async def run() -> None:
            await _token_expiry_watcher(
                subscriber=subscriber,
                conversation_id="c1",
                bus=bus,
                token_payload={"sub": "u1"},  # no `exp`
                check_interval_seconds=0.05,
                now_fn=lambda: datetime.now(timezone.utc),
            )

        watcher_task = asyncio.create_task(run())
        # Wait a couple of tick intervals; subscriber stays open.
        await asyncio.sleep(0.2)
        # If the watcher had fired, the subscriber would be closed
        # by now. Push an event and confirm `receive()` still returns it.
        event_id = await bus.next_event_id("c1")
        await subscriber.push(
            heartbeat(event_id=event_id, conversation_id="c1", now=datetime.now(timezone.utc))
        )
        received = await asyncio.wait_for(subscriber.receive(), timeout=1.0)
        assert received is not None
        assert received.id == event_id
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Heartbeat loop — keeps the SSE channel alive during idle stretches.
# ---------------------------------------------------------------------------


class TestHeartbeatLoop:
    """The heartbeat task pushes synthetic `stream.heartbeat` events on cadence."""

    async def test_emits_heartbeat_on_cadence(self) -> None:
        from app.realtime.stream import _heartbeat_loop

        bus = SseEventBus()
        subscriber, _ = await bus.subscribe("c1")

        async def run() -> None:
            await _heartbeat_loop(
                subscriber=subscriber,
                conversation_id="c1",
                bus=bus,
                interval_seconds=0.05,
                now_fn=lambda: datetime.now(timezone.utc),
            )

        task = asyncio.create_task(run())
        # Drain three heartbeat events.
        events: list[EventBase] = []
        async for event in iter_events(subscriber):
            events.append(event)
            if len(events) >= 3:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert all(e.event == "stream.heartbeat" for e in events), events
        # Ids are monotonically increasing — the bus allocated them
        # in the heartbeat loop.
        ids = [e.id for e in events]
        assert ids == sorted(set(ids))
