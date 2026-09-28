/**
 * Wire types for the SSE progress stream — T24 / #21.
 *
 * Mirrors the canonical event registry emitted by the backend
 * `/api/v1/conversations/{id}/stream` endpoint (T23 / #20, ADR-0010).
 * Field names follow the Pydantic `EventBase` envelope 1:1 so a
 * `JSON.parse(event.data)` assigns directly without a mapping layer
 * — same convention as `types/plan.ts`.
 *
 * The envelope carries every event under the same shape; per-event
 * fields live under `payload` (see backend `app.realtime.events`).
 * The Frontend's `useEventSource` hook (T24) dispatches a tagged
 * union of these types through `useConversationStream` into the
 * Zustand stores, so each reducer can switch on `event` and trust
 * the payload type.
 */
import type { Plan } from '@/types/plan'

/** Canonical event names — ADR-0010 + T23 transport-only events. */
export type SseEventName =
  // Transport events (T23 only — not user-rendered by ADR-0010).
  | 'stream.opened'
  | 'stream.heartbeat'
  | 'auth.expired'
  // Plan lifecycle.
  | 'plan.generated'
  | 'plan.modified'
  // Tool execution lifecycle.
  | 'tool.started'
  | 'tool.finished'
  | 'tool.failed'
  // LLM streaming.
  | 'llm.token'
  // Plan-level terminal.
  | 'execution.completed'
  // Cost guardrail.
  | 'cost.warning'

/**
 * Shared envelope for every SSE event. Matches backend `EventBase`
 * (`app.realtime.events`). Field names are snake_case to match the
 * JSON the server emits; the Frontend never translates.
 */
export interface SseEventBase<TPayload extends Record<string, unknown>> {
  /** Monotonically increasing per-conversation event id. */
  id: number
  /** Event type — see `SseEventName`. */
  event: SseEventName
  /** Owning conversation's ObjectId. */
  conversation_id: string
  /** UTC wall-clock stamp the bus assigned on publish. */
  occurred_at: string
  /** Event-specific fields; the union below discriminates by `event`. */
  payload: TPayload
}

/** `stream.opened` — first event after handshake; carries the buffer's high-water mark. */
export interface StreamOpenedPayload extends Record<string, unknown> {
  high_water_mark: number
}

export type StreamOpenedEvent = SseEventBase<StreamOpenedPayload> & {
  event: 'stream.opened'
}

/** `stream.heartbeat` — typed keep-alive (T23 emits both a comment and a typed event). */
export interface HeartbeatPayload extends Record<string, unknown> {}

export type HeartbeatEvent = SseEventBase<HeartbeatPayload> & {
  event: 'stream.heartbeat'
}

/** `auth.expired` — access JWT expired; the connection is closing. */
export interface AuthExpiredPayload extends Record<string, unknown> {}

export type AuthExpiredEvent = SseEventBase<AuthExpiredPayload> & {
  event: 'auth.expired'
}

/**
 * `plan.generated` — Planner produced a Plan awaiting HITL.
 *
 * `plan` is the canonical Plan doc (T17 shape — `nodes` / `edges` /
 * `tool_snapshots` per ADR-0027). The Plan drawer's `usePlanDrawerStore`
 * hydrates from this event.
 */
export interface PlanGeneratedPayload extends Record<string, unknown> {
  plan: Plan
}

export type PlanGeneratedEvent = SseEventBase<PlanGeneratedPayload> & {
  event: 'plan.generated'
}

/** `plan.modified` — business user edited Plan parameters (ADR-0019). */
export interface PlanModifiedPayload extends Record<string, unknown> {
  plan: Plan
  edited_diff: Record<string, unknown>
}

export type PlanModifiedEvent = SseEventBase<PlanModifiedPayload> & {
  event: 'plan.modified'
}

/**
 * Per-node runtime status the Frontend derives from the
 * `tool.{started,finished,failed}` event stream. Mirrors the badge
 * vocabulary the Worker emits — see ADR-0017 for the recovery flow.
 */
export type ToolRuntimeStatus =
  | 'idle'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'cancelled'

/** `tool.started` — Worker picked up a Plan node (ADR-0012). */
export interface ToolStartedPayload extends Record<string, unknown> {
  node_id: string
  tool: string
}

export type ToolStartedEvent = SseEventBase<ToolStartedPayload> & {
  event: 'tool.started'
}

/** `tool.finished` — node terminated successfully (or was skipped). */
export type ToolFinishedOutcome = 'succeeded' | 'skipped' | 'cancelled'

export interface ToolFinishedPayload extends Record<string, unknown> {
  node_id: string
  tool: string
  status: ToolFinishedOutcome
  duration_ms: number
}

export type ToolFinishedEvent = SseEventBase<ToolFinishedPayload> & {
  event: 'tool.finished'
}

/**
 * `tool.failed` — node hit an unrecoverable error (ADR-0017).
 *
 * `error` mirrors the structured error envelope (code / message /
 * details) every backend error renders per ADR-0031. The Frontend
 * shows the message under the node badge and folds the code into the
 * audit-link copy for support.
 */
export interface ToolFailedError {
  code: string
  message_zh?: string
  message_en?: string
  details?: Record<string, unknown>
}

export interface ToolFailedPayload extends Record<string, unknown> {
  node_id: string
  tool: string
  error: ToolFailedError
}

export type ToolFailedEvent = SseEventBase<ToolFailedPayload> & {
  event: 'tool.failed'
}

/**
 * `llm.token` — incremental final-answer token (T22 / #19).
 *
 * The Frontend concatenates `payload.token` per `turn_id` to render
 * the streaming assistant reply. Multiple `turn_id`s can interleave
 * during branching conversations; the store keys the answer buffer
 * by `turn_id` so a new turn starts a fresh buffer.
 */
export interface LlmTokenPayload extends Record<string, unknown> {
  token: string
  turn_id: string
}

export type LlmTokenEvent = SseEventBase<LlmTokenPayload> & {
  event: 'llm.token'
}

/** `execution.completed` — Plan-level terminal (ADR-0004 / ADR-0017). */
export type ExecutionOutcome = 'succeeded' | 'failed' | 'aborted'

export interface ExecutionCompletedPayload extends Record<string, unknown> {
  plan_id: string
  status: ExecutionOutcome
}

export type ExecutionCompletedEvent = SseEventBase<ExecutionCompletedPayload> & {
  event: 'execution.completed'
}

/**
 * `cost.warning` — conversation approaching cost ceiling (ADR-0025).
 *
 * Soft signal: execution continues; the hard cap arrives via
 * `execution.completed(status="aborted")`. The banner UI is a cost
 * follow-up ticket (T36-adjacent), not T24 — the event type is kept
 * here so the wire contract mirrors `app.realtime.events` 1:1 and
 * the follow-up lands without a type change.
 */
export interface CostWarningPayload extends Record<string, unknown> {
  current_cost_usd: number
  ceiling_usd: number
}

export type CostWarningEvent = SseEventBase<CostWarningPayload> & {
  event: 'cost.warning'
}

/** Tagged union over every event the Frontend can render. */
export type SseEvent =
  | StreamOpenedEvent
  | HeartbeatEvent
  | AuthExpiredEvent
  | PlanGeneratedEvent
  | PlanModifiedEvent
  | ToolStartedEvent
  | ToolFinishedEvent
  | ToolFailedEvent
  | LlmTokenEvent
  | ExecutionCompletedEvent
  | CostWarningEvent
