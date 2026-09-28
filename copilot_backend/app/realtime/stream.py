"""`GET /api/v1/conversations/{id}/stream` — T23 / #20.

The SSE endpoint that Frontend EventSource consumers open to receive
real-time progress for one conversation. Implements ADR-0010 end to
end:

* **Query-param auth** — the access JWT travels in `?token=` because
  `EventSource` cannot set custom headers (see
  `app.realtime.auth.get_sse_user`).
* **Ownership guard** — a conversation id is only streamable by its
  owning user; cross-user access renders the same `not_found`
  envelope as an absent conversation (ADR-0002).
* **Event replay** — `Last-Event-ID` is read on connect; the bus's
  replay buffer fills the gap before live events start streaming.
* **Heartbeat** — `: heartbeat\\n\\n` comment lines every
  `heartbeat_interval_seconds` keep SSE proxies from idle-dropping
  the connection.
* **Token expiry → close** — the bus emits `auth.expired` once when
  the JWT's `exp` elapses, then closes the stream so the Frontend
  can render "session ended" and trigger a `/auth/refresh`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse

from app.conversations.service import ConversationService
from app.db.dependencies import (
    get_conversation_service,
    get_sse_bus,
)
from app.db.schemas import User
from app.realtime.auth import AuthenticatedSseToken, get_sse_user
from app.realtime.bus import SseEventBus, Subscriber, iter_events
from app.realtime.events import (
    auth_expired,
    serialize_event,
    stream_opened,
)

logger = logging.getLogger(__name__)

# Heartbeat cadence. Long enough to avoid flooding the proxy with
# keep-alives; short enough to keep idle SSE connections from being
# recycled by intermediaries that drop silent sockets after ~60s.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: float = 15.0

# How often we re-check the access token's `exp`. Token validation
# happens once on connect; the watcher only re-decodes the same JWT
# to detect expiry, so this is a cheap lookup. Bound on the lower
# side by 0.5s so a misconfigured deployment can't busy-loop.
TOKEN_EXPIRY_CHECK_INTERVAL_SECONDS: float = 1.0


router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    """UTC wall-clock — `EventBase.occurred_at` stamps this on publish.

    Centralised so tests can monkeypatch a single seam for
    deterministic timestamps; the bus itself is unaware of clock
    injection.
    """
    return datetime.now(timezone.utc)


async def _ownership_check(
    *,
    conversation_id: str,
    user: User,
    svc: ConversationService,
) -> None:
    """Verify `user` owns `conversation_id`; raise `NotFoundError` otherwise.

    Mirrors the service-layer guard every other conversations route
    uses so cross-user access surfaces the same `not_found` envelope
    as an absent conversation. We use `get_owned_conversation` —
    the public seam added in T23 — rather than reaching for the
    private repository handle: the SSE endpoint only needs the
    ownership check, not the full turn / plan fan-out that
    `get_detail` performs.
    """
    await svc.get_owned_conversation(
        conversation_id=conversation_id,
        user_id=user.id,
    )


def _parse_last_event_id(header_value: str | None) -> int:
    """Decode the `Last-Event-ID` header into an integer cursor.

    Returns 0 (start from the buffer's tail-end) for an absent or
    unparseable header — the standard SSE reconnect semantic is
    "resume from where you left off, or restart from now".
    """
    if not header_value:
        return 0
    try:
        return int(header_value.strip())
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


async def _event_stream(
    *,
    subscriber: Subscriber,
    bus: SseEventBus,
    token_payload: dict[str, object],
    heartbeat_interval_seconds: float,
    token_expiry_check_interval_seconds: float,
    now_fn: "_ClockFn",
) -> AsyncIterator[str]:
    """Yield SSE-encoded strings until the connection should close.

    Sequence:

    1. Drain the bus-side replay slice (events that landed before the
       consumer subscribed or while it was disconnected) so the
       Frontend sees a contiguous history on first connect.
    2. Emit `stream.opened` carrying the buffer's high-water mark.
       Routed through `bus.publish` so it lands in the replay buffer
       too — a reconnecting consumer with `Last-Event-ID` sees the
       full event history including `stream.opened`.
    3. Loop on the subscriber queue, interleaving heartbeats so
       idle SSE proxies don't recycle the connection.
    4. When the JWT's `exp` elapses, emit `auth.expired` and close
       gracefully — the Frontend uses that event to trigger a
       `/auth/refresh` rather than guessing from the disconnect.
    """
    # Step 1 — replay slice already pre-loaded into subscriber._queue
    # by `bus.subscribe`. The `iter_events` loop drains them alongside
    # live events; nothing to do here.

    # Step 2 — publish `stream.opened` through the bus so the event
    # lands in the replay buffer. This guarantees a reconnecting
    # consumer with `Last-Event-ID` sees the same event history as a
    # fresh consumer — every event the consumer receives also lives
    # in the buffer, no id-gaps.
    opened_id = await bus.next_event_id(subscriber.conversation_id)
    opened_event = stream_opened(
        event_id=opened_id,
        conversation_id=subscriber.conversation_id,
        now=now_fn(),
    )
    await bus.publish(opened_event)
    subscriber.last_seen_id = max(subscriber.last_seen_id, opened_event.id)

    # Step 3 — interleave live events + heartbeats. We use a small
    # task that publishes heartbeats through the same bus, rather
    # than yielding from two coroutines directly, because the SSE
    # encoding must be byte-by-byte sequential — the bus's
    # `Subscriber.push` keeps the stream serialised automatically.
    # Routing through `bus.publish` ensures heartbeats are also
    # captured in the replay buffer.
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(
            subscriber=subscriber,
            conversation_id=subscriber.conversation_id,
            bus=bus,
            interval_seconds=heartbeat_interval_seconds,
            now_fn=now_fn,
        )
    )
    expiry_task = asyncio.create_task(
        _token_expiry_watcher(
            subscriber=subscriber,
            conversation_id=subscriber.conversation_id,
            bus=bus,
            token_payload=token_payload,
            check_interval_seconds=token_expiry_check_interval_seconds,
            now_fn=now_fn,
        )
    )
    try:
        async for event in iter_events(subscriber):
            yield serialize_event(event)
    finally:
        heartbeat_task.cancel()
        expiry_task.cancel()
        for t in (heartbeat_task, expiry_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await bus.unsubscribe(subscriber)


async def _heartbeat_loop(
    *,
    subscriber: Subscriber,
    conversation_id: str,
    bus: SseEventBus,
    interval_seconds: float,
    now_fn: "_ClockFn",
) -> None:
    """Push a synthetic `stream.heartbeat` event every `interval_seconds`.

    The Frontend's `useEventSource` hook (T24) ignores heartbeat
    events by `event === 'stream.heartbeat'`; the SSE-encoded data
    line keeps the connection alive across idle stretches. We use a
    typed event rather than a bare `: heartbeat` comment so the
    `Last-Event-ID` cursor advances on every tick — a reconnecting
    client that catches up via `Last-Event-ID` is always at least
    one heartbeat behind the server's wall-clock.

    Heartbeats are routed through `bus.publish` (not
    `subscriber.push`) so they land in the replay buffer alongside
    the lifecycle events — without this a reconnect with
    `Last-Event-ID=N` would skip heartbeat ids, leaving id gaps in
    the buffer.
    """
    from app.realtime.events import heartbeat  # local import — avoids cycle

    try:
        while True:
            await asyncio.sleep(interval_seconds)
            event_id = await bus.next_event_id(conversation_id)
            await bus.publish(
                heartbeat(
                    event_id=event_id,
                    conversation_id=conversation_id,
                    now=now_fn(),
                )
            )
    except asyncio.CancelledError:
        return


async def _token_expiry_watcher(
    *,
    subscriber: Subscriber,
    conversation_id: str,
    bus: SseEventBus,
    token_payload: dict[str, object],
    check_interval_seconds: float,
    now_fn: "_ClockFn",
) -> None:
    """Emit `auth.expired` and close the subscriber when the JWT elapses.

    `decode_jwt` already validated `exp` on connect; this watcher
    only fires after the *original* expiry instant — i.e. the
    connection is still alive past the JWT's TTL because the SSE
    stream doesn't re-validate on every event. The `auth.expired`
    event gives the Frontend a single, typed signal to refresh
    the access token without re-opening the stream.
    """
    exp = token_payload.get("exp")
    if not isinstance(exp, int):
        # No `exp` claim — token is somehow non-expiring, which we
        # never mint; bail out rather than busy-loop.
        return

    try:
        while True:
            await asyncio.sleep(check_interval_seconds)
            now_unix = int(now_fn().timestamp())
            if now_unix >= exp:
                event_id = await bus.next_event_id(conversation_id)
                await bus.publish(
                    auth_expired(
                        event_id=event_id,
                        conversation_id=conversation_id,
                        now=now_fn(),
                    )
                )
                # Give the consumer one event-loop tick to flush
                # the SSE frame, then close.
                await asyncio.sleep(0)
                await subscriber.close()
                return
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


# Local clock-fn alias for readability — kept module-private so the
# monkeypatch seam is the `now_fn` parameter of `_event_stream`.
from typing import Callable as _Callable

_ClockFn = _Callable[[], datetime]


@router.get(
    "/{conversation_id}/stream",
    summary="Subscribe to SSE progress events for one conversation (T23 / #20)",
    responses={
        200: {
            "description": "text/event-stream — SSE-encoded events per ADR-0010.",
            "content": {"text/event-stream": {}},
        },
        401: {"description": "Missing or invalid `?token=` query parameter."},
        404: {"description": "Conversation not found (or not owned by caller)."},
    },
)
async def stream_conversation(
    request: Request,
    conversation_id: Annotated[str, Path(description="ObjectId of the conversation.")],
    auth: Annotated[AuthenticatedSseToken, Depends(get_sse_user)],
    svc: Annotated[ConversationService, Depends(get_conversation_service)],
    bus: Annotated[SseEventBus, Depends(get_sse_bus)],
    last_event_id: Annotated[
        int,
        Query(
            alias="last_event_id",
            description=(
                "Optional resume cursor. EventSource also sends the standard "
                "`Last-Event-ID` header on reconnect; the query parameter is "
                "a convenience for clients that can't set headers."
            ),
            ge=0,
        ),
    ] = 0,
    heartbeat_interval_seconds: Annotated[
        float,
        Query(
            alias="heartbeat_seconds",
            description=(
                "Heartbeat cadence in seconds. Defaults to 15s; configurable "
                "per-call so test clients can shorten it."
            ),
            ge=0.5,
            le=120.0,
        ),
    ] = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> StreamingResponse:
    """Open an SSE stream for `conversation_id`.

    Sequence:

    1. Authenticate the `?token=` query parameter (returns 401 on
       missing / invalid / expired JWT — the dependency renders the
       envelope).
    2. Verify the caller owns `conversation_id` (returns 404 on
       cross-user access — same envelope as an absent conversation).
    3. Subscribe to the bus, passing the `Last-Event-ID` header (or
       the `?last_event_id=` fallback) as the replay cursor.
    4. Return a `StreamingResponse` whose generator yields
       SSE-encoded frames until the JWT expires (`auth.expired`
       then close), the client disconnects (the bus's `unsubscribe`
       cleanup runs), or the conversation's terminal event lands.
    """
    user = auth.user
    payload = auth.payload

    # Ownership guard — same envelope as `get_conversation_detail`.
    await _ownership_check(
        conversation_id=conversation_id,
        user=user,
        svc=svc,
    )

    # Replay cursor — `Last-Event-ID` header wins over the query
    # fallback when both are present.
    header_last_event_id = _parse_last_event_id(
        request.headers.get("Last-Event-ID"),
    )
    cursor = header_last_event_id or last_event_id

    subscriber, _replay = await bus.subscribe(
        conversation_id,
        last_seen_id=cursor,
    )

    generator = _event_stream(
        subscriber=subscriber,
        bus=bus,
        token_payload=payload,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        token_expiry_check_interval_seconds=TOKEN_EXPIRY_CHECK_INTERVAL_SECONDS,
        now_fn=_now_utc,
    )

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            # Disable proxy buffering so heartbeats + events flush
            # without nginx / CloudFront holding frames for 30+s.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "TOKEN_EXPIRY_CHECK_INTERVAL_SECONDS",
    "router",
    "stream_conversation",
]
