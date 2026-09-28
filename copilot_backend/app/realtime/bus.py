"""In-memory pub/sub bus for SSE events — T23 / #20.

The `/conversations/{id}/stream` endpoint reads events for one
conversation at a time. Each connected SSE consumer registers as a
`Subscriber`; the Worker / Planner / cost-watchdog publish events
through `SseEventBus.publish(...)` and every subscriber on that
conversation id wakes up to read its copy.

Bounded replay buffer
---------------------

EventSource (WHATWG) sends `Last-Event-ID` on automatic reconnect.
The bus keeps a bounded ring buffer of the most-recent events per
conversation so a late-joining or reconnecting consumer can ask
"give me everything since id X" without the publisher having to
remember who is connected. The buffer is in-process; the
ADR-0010-permitted behaviour is "replay the buffer for the lifetime
of the active worker process" — a future Redis-backed bus
(`SseEventBus` becomes an interface) is the persistence story
beyond MVP, not a T23 concern.

Backpressure & queue semantics
------------------------------

A subscriber's queue is bounded (`subscriber_queue_max`). When the
queue fills (slow consumer), the bus drops the *oldest* event for
that subscriber — the consumer's `Last-Event-ID` re-syncs via the
replay buffer on reconnect. We deliberately don't block publishers
on slow consumers because the Worker is a hot-path emitter: one
stuck EventSource shouldn't back up the DAG executor.

Thread safety
-------------

This module is async-only; the GIL plus cooperative scheduling
mean an `asyncio.Lock` per conversation is sufficient. The bus is
singleton per process — held on `app.state.sse_bus` — so every
endpoint request shares the same registry.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.realtime.events import EventBase

logger = logging.getLogger(__name__)


# Replay buffer length per conversation. Sized so a slow / reconnecting
# EventSource can catch up to ~5 minutes of activity at ~1 Hz. Larger
# values waste memory; smaller values cause `Last-Event-ID` consumers to
# fall through to a re-poll of the conversation state.
DEFAULT_REPLAY_BUFFER_SIZE: int = 256

# Per-subscriber queue cap. Slow consumers block on `subscribed()` so
# they can drain at their own pace; we cap memory by dropping the
# oldest event when the queue overflows. The cap is intentionally
# generous — the Worker emits a handful of events per Plan, not a
# sustained torrent.
DEFAULT_SUBSCRIBER_QUEUE_MAX: int = 64


@dataclass
class _ConversationChannel:
    """Per-conversation state held inside the bus.

    `buffer` is the replay ring (read by reconnecting consumers via
    `replay_since`). `subscribers` is the live subscriber set; each
    one owns its own bounded deque + condition so a slow consumer
    doesn't block other consumers on the same conversation.
    """

    buffer: deque[EventBase] = field(
        default_factory=lambda: deque(maxlen=DEFAULT_REPLAY_BUFFER_SIZE),
    )
    subscribers: set["Subscriber"] = field(default_factory=set)
    next_event_id: int = 1


@dataclass
class Subscriber:
    """A single SSE consumer registered against one conversation.

    The bus hands new events to `_queue`; the async generator in
    `app.realtime.stream` reads from it. `last_seen_id` lets the
    bus know the consumer's reconnect cursor when `replay_since`
    is called on a *new* subscriber.

    `_condition` is the wakeup signal: the bus calls
    `condition.notify_all()` after appending to the queue, so the
    generator unblocks promptly. Using a per-subscriber condition
    means one slow subscriber can't park a fast one.

    `frozen=False` because `last_seen_id` advances on each
    `receive()`; `eq=True` plus an explicit `__hash__` is the
    dataclass-friendly way to put instances in a `set`. We hash
    on `id(self)` because the bus doesn't care about *which*
    Subscriber is registered — only that each call site operates
    on its own handle.
    """

    conversation_id: str
    _queue: deque[EventBase] = field(
        default_factory=lambda: deque(maxlen=DEFAULT_SUBSCRIBER_QUEUE_MAX),
    )
    _condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    last_seen_id: int = 0
    closed: bool = False

    def __hash__(self) -> int:
        return id(self)

    async def receive(self) -> EventBase | None:
        """Block until a new event arrives or the subscriber is closed.

        Returns `None` when the bus has closed this subscriber — the
        generator uses that as the loop terminator so the FastAPI
        `StreamingResponse` cleans up promptly.
        """
        async with self._condition:
            while not self._queue and not self.closed:
                await self._condition.wait()
            if self.closed and not self._queue:
                return None
            event = self._queue.popleft()
            self.last_seen_id = max(self.last_seen_id, event.id)
            return event

    async def push(self, event: EventBase) -> None:
        """Append `event` and wake the consumer.

        When the queue is full the oldest event is dropped silently —
        the consumer's reconnect path will replay it from the bus's
        per-conversation buffer if `last_seen_id` lags. We log at
        DEBUG because it's a hint, not an error.
        """
        async with self._condition:
            if len(self._queue) == self._queue.maxlen:
                logger.debug(
                    "sse subscriber queue full; dropping oldest event for conversation_id=%s",
                    self.conversation_id,
                )
            self._queue.append(event)
            self._condition.notify_all()

    async def close(self) -> None:
        """Mark the subscriber closed and wake any waiting `receive`."""
        async with self._condition:
            self.closed = True
            self._condition.notify_all()


class SseEventBus:
    """In-memory SSE pub/sub — T23 / #20.

    `subscribe(conversation_id, last_seen_id=0)` returns a fresh
    `Subscriber` plus the replay slice from the buffer the consumer
    missed (empty when `last_seen_id=0` or the buffer has aged out).

    `publish(event)` fans the event out to every subscriber on the
    conversation and appends it to the replay buffer.

    `unsubscribe(subscriber)` removes the subscriber from the channel
    and closes its queue so the consumer's `receive` returns `None`.

    The bus is process-local. There is no Mongo / Redis fallback in
    T23 — ADR-0010's "EventSource 自动重连" contract is satisfied by
    the in-memory buffer; the persistence story for a multi-replica
    deployment is a future ticket.
    """

    def __init__(
        self,
        *,
        replay_buffer_size: int = DEFAULT_REPLAY_BUFFER_SIZE,
        subscriber_queue_max: int = DEFAULT_SUBSCRIBER_QUEUE_MAX,
    ) -> None:
        self._channels: dict[str, _ConversationChannel] = {}
        self._lock = asyncio.Lock()
        self._replay_buffer_size = replay_buffer_size
        self._subscriber_queue_max = subscriber_queue_max

    # ------------------------------------------------------------------
    # Channel registry
    # ------------------------------------------------------------------

    async def _get_channel(self, conversation_id: str) -> _ConversationChannel:
        """Return (creating if needed) the channel for `conversation_id`.

        Lazy creation: the first `publish` or `subscribe` for a new
        id materialises a channel; idle channels are kept until
        process exit. Memory cost is ~one deque per conversation the
        process has ever seen, which is fine for the MVP per-process
        fan-out.
        """
        async with self._lock:
            channel = self._channels.get(conversation_id)
            if channel is None:
                channel = _ConversationChannel(
                    buffer=deque(maxlen=self._replay_buffer_size),
                )
                self._channels[conversation_id] = channel
            return channel

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, event: EventBase) -> None:
        """Fan `event` out to subscribers and append to the replay buffer.

        The event id is the caller's responsibility — the bus does
        not stamp it. Callers should use the helper factories in
        `app.realtime.events`, which take `event_id` as a required
        kwarg, and source it from the bus via `next_event_id(...)`.
        """
        channel = await self._get_channel(event.conversation_id)
        # Append to the buffer first so a subscriber that picks up
        # the event can ask "what's after this id?" and get a
        # consistent answer.
        channel.buffer.append(event)
        # Snapshot the subscriber list under the lock so a concurrent
        # `unsubscribe` doesn't mutate the iteration target.
        async with self._lock:
            subscribers = list(channel.subscribers)
        for sub in subscribers:
            await sub.push(event)

    async def next_event_id(self, conversation_id: str) -> int:
        """Allocate the next monotonically increasing id for `conversation_id`.

        Idempotency note: the id is allocated under the same lock
        that guards `_channels` insertion, so two concurrent
        publishers can't race to the same id even on a brand-new
        channel. Existing channel reads are unlocked (the deque is
        thread-safe for append/popleft under asyncio), so id
        allocation doesn't block consumers.
        """
        async with self._lock:
            channel = self._channels.get(conversation_id)
            if channel is None:
                channel = _ConversationChannel(
                    buffer=deque(maxlen=self._replay_buffer_size),
                )
                self._channels[conversation_id] = channel
            assigned = channel.next_event_id
            channel.next_event_id += 1
            return assigned

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        conversation_id: str,
        *,
        last_seen_id: int = 0,
    ) -> tuple[Subscriber, list[EventBase]]:
        """Register a subscriber and return its replay slice.

        The replay slice is the contiguous tail of buffered events
        with `id > last_seen_id` — empty when `last_seen_id` matches
        the high-water mark or the buffer has aged past it. A gap
        (request id larger than any buffered event) yields an empty
        list: the consumer will start receiving live events from
        now on, and the Frontend can `GET /conversations/{id}` to
        rebuild the missing context.

        Replay events are pre-loaded into the subscriber's queue
        so the async iterator (`iter_events`) can drain them
        immediately — the caller doesn't have to thread them back
        through `subscriber.push()` itself.
        """
        channel = await self._get_channel(conversation_id)
        replay = [event for event in channel.buffer if event.id > last_seen_id]
        subscriber = Subscriber(
            conversation_id=conversation_id,
            _queue=deque(maxlen=self._subscriber_queue_max),
            last_seen_id=last_seen_id,
        )
        # Pre-load the replay slice — drained by the SSE generator
        # before any live events arrive. Direct attribute writes are
        # fine because Subscriber is `@dataclass` (not `frozen=True`).
        # We bypass `Subscriber.push` here because that would
        # notify a condition we haven't yet handed to a consumer;
        # the bus has not returned the subscriber to the caller.
        for event in replay:
            subscriber._queue.append(event)
            if subscriber.last_seen_id < event.id:
                subscriber.last_seen_id = event.id
        async with self._lock:
            channel.subscribers.add(subscriber)
        return subscriber, replay

    async def unsubscribe(self, subscriber: Subscriber) -> None:
        """Remove `subscriber` from its channel and close its queue."""
        channel = await self._get_channel(subscriber.conversation_id)
        async with self._lock:
            channel.subscribers.discard(subscriber)
        await subscriber.close()

    # ------------------------------------------------------------------
    # Stats / test helpers
    # ------------------------------------------------------------------

    async def channel_stats(self, conversation_id: str) -> dict[str, Any]:
        """Return the live subscriber count + buffer length for tests / ops."""
        channel = await self._get_channel(conversation_id)
        return {
            "subscribers": len(channel.subscribers),
            "buffer_len": len(channel.buffer),
            "next_event_id": channel.next_event_id,
        }


# ---------------------------------------------------------------------------
# Async iterator helper — wraps a `Subscriber` so the SSE generator can
# `async for` over events without juggling the `receive()` return-value
# sentinel.
# ---------------------------------------------------------------------------


async def iter_events(subscriber: Subscriber) -> AsyncIterator[EventBase]:
    """Yield events from `subscriber` until it is closed.

    Translates the `None` sentinel from `Subscriber.receive()` into a
    clean loop terminator — the SSE generator catches the implicit
    StopAsyncIteration and the FastAPI StreamingResponse closes
    the underlying socket.
    """
    while True:
        event = await subscriber.receive()
        if event is None:
            return
        yield event


__all__ = [
    "DEFAULT_REPLAY_BUFFER_SIZE",
    "DEFAULT_SUBSCRIBER_QUEUE_MAX",
    "SseEventBus",
    "Subscriber",
    "iter_events",
]
