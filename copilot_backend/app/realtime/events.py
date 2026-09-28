"""SSE event type registry — T23 / #20.

The wire-level shape of every event the `/conversations/{id}/stream`
endpoint can emit. The event names follow ADR-0010's canonical list
and are the only contract the Frontend EventSource consumer needs to
recognise; payloads are typed via Pydantic so router / Worker code
constructs events through the same model the wire serialises to.

Event identity
--------------

Every event carries a monotonically increasing `id` per conversation
(issued by `SseEventBus`). The id is what `Last-Event-ID` carries on
reconnect (EventSource native reconnect, per the WHATWG spec) — the
bus keeps a bounded replay buffer so a late reconnect can resume
from the last seen id without missing events emitted in the gap.

Event names
-----------

ADR-0010 names the lifecycle events the Frontend must render. We add
two transport-only events that the Frontend doesn't render but the
spec / resilience story leans on:

* `stream.opened` — sent once on connect, carries the latest event
  id the buffer has seen. Lets the Frontend catch up to the
  last-seen id synchronously.
* `stream.heartbeat` — comment-only keep-alive (no `event:` line;
  the SSE encoding emits a `: heartbeat\\n\\n` line). SSE proxies
  idle-out connections in 30–60s without traffic, so the heartbeats
  keep the channel alive during long quiet stretches between
  Worker node transitions.

Why a Literal for the event name
---------------------------------

A `Literal[...]` of the canonical event names is the cheapest way to
catch a typo at the construction site: `EventType("plan.genereated")`
fails at import / test time, not in production after the Frontend
silently ignores the misspelled event for a sprint. Adding a new
event is a single Literal member + payload model — the type checker
flags every site that switches over `event_name`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Canonical event names — ADR-0010.
#
# `cost_warning` is the ADR-0025 cost-cap signal; `auth_expired` is the
# T23 transport-level event that lets the Frontend render "session
# ended — please re-authenticate" before the connection drops.
EventName = Literal[
    # Transport events (T23 only — not user-rendered by ADR-0010).
    "stream.opened",
    "stream.heartbeat",
    "auth.expired",
    # Plan lifecycle.
    "plan.generated",
    "plan.modified",
    # Tool execution lifecycle.
    "tool.started",
    "tool.finished",
    "tool.failed",
    # LLM streaming.
    "llm.token",
    # Plan-level terminal.
    "execution.completed",
    # Cost guardrail.
    "cost.warning",
]


# ---------------------------------------------------------------------------
# Payload models — every event carries the same envelope on the wire so the
# Frontend can write one `event.data` parser. Per-event fields land under
# `payload` rather than at the top level to keep `data` flat and forward-
# compatible (adding a new event doesn't add fields that old consumers
# trip over).
# ---------------------------------------------------------------------------


class EventBase(BaseModel):
    """Shared fields across every SSE event.

    `id` is the per-conversation monotonically increasing event id; the
    Frontend echoes it back as `Last-Event-ID` on reconnect. `occurred_at`
    is the wall-clock stamp the bus assigns on publish — the
    Frontend's `useEventSource` (T24) renders this in the chat panel.
    `conversation_id` is duplicated here so a multi-conversation consumer
    doesn't have to track which channel emitted the event.
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(description="Monotonically increasing per-conversation event id.")
    event: EventName = Field(description="Event type — see `EventName`.")
    conversation_id: str = Field(description="ObjectId of the owning conversation.")
    occurred_at: datetime = Field(description="UTC wall-clock stamp.")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Event-specific data; see per-event factories below.",
    )


# ---------------------------------------------------------------------------
# Factory helpers — one per event name so the construction site (Worker,
# Planner, cost watchdog, etc.) reads as a typed function call rather than
# a `dict(...)` blob. Each returns the canonical `EventBase` shape.
# ---------------------------------------------------------------------------


def _new_event(
    *,
    event_id: int,
    event_name: EventName,
    conversation_id: str,
    payload: dict[str, Any],
    now: datetime,
) -> EventBase:
    """Stamp shared fields and return the typed envelope."""
    return EventBase(
        id=event_id,
        event=event_name,
        conversation_id=conversation_id,
        occurred_at=now,
        payload=payload,
    )


def plan_generated(
    *,
    event_id: int,
    conversation_id: str,
    plan: dict[str, Any],
    now: datetime,
) -> EventBase:
    """`plan.generated` — Planner produced a Plan awaiting HITL.

    `plan` is the canonical Plan doc (T17 shape — `nodes` / `edges` /
    `tool_snapshots` per ADR-0027). The Frontend's React Flow drawer
    (T19) hydrates from this event.
    """
    return _new_event(
        event_id=event_id,
        event_name="plan.generated",
        conversation_id=conversation_id,
        payload={"plan": plan},
        now=now,
    )


def plan_modified(
    *,
    event_id: int,
    conversation_id: str,
    plan: dict[str, Any],
    diff: dict[str, Any],
    now: datetime,
) -> EventBase:
    """`plan.modified` — business user edited Plan parameters (ADR-0019)."""
    return _new_event(
        event_id=event_id,
        event_name="plan.modified",
        conversation_id=conversation_id,
        payload={"plan": plan, "edited_diff": diff},
        now=now,
    )


def tool_started(
    *,
    event_id: int,
    conversation_id: str,
    node_id: str,
    tool_name: str,
    now: datetime,
) -> EventBase:
    """`tool.started` — Worker picked up a Plan node (ADR-0012)."""
    return _new_event(
        event_id=event_id,
        event_name="tool.started",
        conversation_id=conversation_id,
        payload={"node_id": node_id, "tool": tool_name},
        now=now,
    )


def tool_finished(
    *,
    event_id: int,
    conversation_id: str,
    node_id: str,
    tool_name: str,
    status: Literal["succeeded", "skipped", "cancelled"],
    duration_ms: int,
    now: datetime,
) -> EventBase:
    """`tool.finished` — node terminated successfully (or was skipped)."""
    return _new_event(
        event_id=event_id,
        event_name="tool.finished",
        conversation_id=conversation_id,
        payload={
            "node_id": node_id,
            "tool": tool_name,
            "status": status,
            "duration_ms": duration_ms,
        },
        now=now,
    )


def tool_failed(
    *,
    event_id: int,
    conversation_id: str,
    node_id: str,
    tool_name: str,
    error: dict[str, Any],
    now: datetime,
) -> EventBase:
    """`tool.failed` — node hit an unrecoverable error (ADR-0017).

    `error` is the structured error envelope (code / message /
    details). The Frontend's React Flow drawer renders it under the
    node badge.
    """
    return _new_event(
        event_id=event_id,
        event_name="tool.failed",
        conversation_id=conversation_id,
        payload={"node_id": node_id, "tool": tool_name, "error": error},
        now=now,
    )


def llm_token(
    *,
    event_id: int,
    conversation_id: str,
    token: str,
    turn_id: str,
    now: datetime,
) -> EventBase:
    """`llm.token` — incremental final-answer token (T22 / #19).

    The Frontend concatenates `payload.token` per `turn_id` to render
    the streaming assistant reply.
    """
    return _new_event(
        event_id=event_id,
        event_name="llm.token",
        conversation_id=conversation_id,
        payload={"token": token, "turn_id": turn_id},
        now=now,
    )


def execution_completed(
    *,
    event_id: int,
    conversation_id: str,
    plan_id: str,
    status: Literal["succeeded", "failed", "aborted"],
    now: datetime,
) -> EventBase:
    """`execution.completed` — Plan-level terminal (per ADR-0004 / ADR-0017)."""
    return _new_event(
        event_id=event_id,
        event_name="execution.completed",
        conversation_id=conversation_id,
        payload={"plan_id": plan_id, "status": status},
        now=now,
    )


def cost_warning(
    *,
    event_id: int,
    conversation_id: str,
    current_cost_usd: float,
    ceiling_usd: float,
    now: datetime,
) -> EventBase:
    """`cost.warning` — conversation approaching cost ceiling (ADR-0025).

    Emitted as a soft signal — the Frontend renders a banner but
    execution continues. ADR-0025 also defines a hard cap that would
    land via `execution.completed(status="aborted")`; that's owned by
    the cost watchdog, not the SSE layer.
    """
    return _new_event(
        event_id=event_id,
        event_name="cost.warning",
        conversation_id=conversation_id,
        payload={"current_cost_usd": current_cost_usd, "ceiling_usd": ceiling_usd},
        now=now,
    )


def auth_expired(
    *,
    event_id: int,
    conversation_id: str,
    now: datetime,
) -> EventBase:
    """`auth.expired` — access token expired; connection closing.

    Transport-level event (T23 only): the bus emits this once before
    tearing the stream down so the Frontend can render a "session
    ended" banner and trigger a `/auth/refresh` instead of guessing
    why the connection closed.
    """
    return _new_event(
        event_id=event_id,
        event_name="auth.expired",
        conversation_id=conversation_id,
        payload={},
        now=now,
    )


def stream_opened(
    *,
    event_id: int,
    conversation_id: str,
    now: datetime,
) -> EventBase:
    """`stream.opened` — first event after the SSE handshake.

    Lets a late-joining consumer learn `id` (the high-water mark of
    the replay buffer) without parsing the heartbeat comments.
    """
    return _new_event(
        event_id=event_id,
        event_name="stream.opened",
        conversation_id=conversation_id,
        payload={"high_water_mark": event_id},
        now=now,
    )


def heartbeat(
    *,
    event_id: int,
    conversation_id: str,
    now: datetime,
) -> EventBase:
    """`stream.heartbeat` — typed keep-alive emitted on the bus.

    The SSE encoding also includes a `: heartbeat\\n\\n` comment
    line for proxy-compatibility, but the typed event advances the
    `Last-Event-ID` cursor so a reconnecting consumer that catches
    up via the query param never silently drops to the buffer
    head.
    """
    return _new_event(
        event_id=event_id,
        event_name="stream.heartbeat",
        conversation_id=conversation_id,
        payload={},
        now=now,
    )


def serialize_event(event: EventBase) -> str:
    """Render `event` as an SSE-encoded frame.

    The encoding follows WHATWG §Server-Sent Events: `id:` line,
    `event:` line, one or more `data:` lines, blank-line terminator.
    JSON is the data shape because the Frontend (T24) renders via
    `JSON.parse(event.data)` — keeping the data on one `data:` line
    matches the spec's "a single line per data" recommendation
    without forcing multi-line encoding edge cases.
    """
    import json

    data = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.event}\ndata: {data}\n\n"


__all__ = [
    "EventBase",
    "EventName",
    "auth_expired",
    "cost_warning",
    "execution_completed",
    "heartbeat",
    "llm_token",
    "plan_generated",
    "plan_modified",
    "serialize_event",
    "stream_opened",
    "tool_failed",
    "tool_finished",
    "tool_started",
]
