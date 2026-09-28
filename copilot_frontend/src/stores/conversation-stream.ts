/**
 * Per-conversation stream store — T24 / #21, ADR-0010.
 *
 * Sibling of `plan-drawer.ts`: the drawer owns "what Plan is on
 * screen", this store owns "what's live". `useConversationStream`
 * folds the SSE hook's events into three pieces of state:
 *
 * - `nodeStatuses` — per-`node_id` runtime status map keyed off
 *   `tool.{started,finished,failed}`. The `PlanToolNode` reads it
 *   so the node badge flips "执行中 / 成功 / 失败" as the Worker
 *   reports progress (AC: 节点实时切状态).
 * - `streamingAnswer` — typewriter buffer for the latest `turn_id`.
 *   Each `llm.token` event appends one chunk; the `PlanAnswerPane`
 *   renders the cumulative string (AC: 回答逐字流出).
 * - `connectionStatus` — the hook's lifecycle signal, surfaced as
 *   the drawer's "重连中…" / "登录已过期" pill.
 *
 * Plan *content* deliberately does NOT live here: issue #53's
 * handoff pins `usePlanDrawerStore.plan` as the single write point
 * for Plan state, so `plan.generated` / `plan.modified` go straight
 * to the drawer store; this file only reacts to the *transition*
 * (a new Plan wipes stale node statuses via `beginPlan`).
 */
import { create } from 'zustand'

import type { ToolRuntimeStatus } from '@/types/sse'

/** Streaming answer buffer — T22 / #19 produces the tokens. */
export interface StreamingAnswer {
  /** Turn whose tokens are accumulating. */
  turnId: string
  /** Cumulative content; the pane renders this verbatim. */
  text: string
  /** True once `execution.completed` for the Plan's turn arrives. */
  done: boolean
}

/** Hook lifecycle signal (mirrors `useEventSource`'s callback set). */
export type StreamConnectionStatus =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'closed'
  | 'auth-failed'

interface ConversationStreamState {
  /** Conversation this store is hydrated for; `null` when dormant. */
  conversationId: string | null
  /** Per-node runtime status map (`tool.*` events). */
  nodeStatuses: Record<string, ToolRuntimeStatus>
  /** Streaming answer buffer (latest `turn_id` only). */
  streamingAnswer: StreamingAnswer | null
  connectionStatus: StreamConnectionStatus

  /** Drop into a fresh conversation; clears every buffer. */
  startConversation(conversationId: string): void
  /** Wipe every buffer (call on conversation switch / unmount). */
  reset(): void

  /** `plan.generated` — a new execution begins; clear stale badges. */
  beginPlan(): void
  /** `tool.started` — flip the node to `running`. */
  markNodeRunning(nodeId: string): void
  /** `tool.finished` — node terminated cleanly (succeeded / skipped / cancelled). */
  markNodeFinished(
    nodeId: string,
    status: Extract<ToolRuntimeStatus, 'succeeded' | 'skipped' | 'cancelled'>,
  ): void
  /** `tool.failed` — node hit an unrecoverable error. */
  markNodeFailed(nodeId: string): void
  /** `llm.token` — append one chunk to the active `turn_id`'s buffer. */
  appendAnswerToken(turnId: string, token: string): void
  /**
   * `execution.completed` — freeze whichever answer buffer is
   * currently active. No `turn_id` is threaded through because the
   * event carries `plan_id` instead; the buffer is single-slot per
   * conversation by design (ADR-0005's turns are sequential).
   */
  finishActiveAnswer(): void
  /** Update the lifecycle signal (called from the SSE adapter). */
  setConnectionStatus(status: StreamConnectionStatus): void
}

/** The shared "no live buffers" state (`reset` / `startConversation`). */
const EMPTY_BUFFERS = {
  nodeStatuses: {},
  streamingAnswer: null,
} as const

export const useConversationStreamStore = create<ConversationStreamState>(
  (set) => ({
    conversationId: null,
    nodeStatuses: {},
    streamingAnswer: null,
    connectionStatus: 'idle',

    startConversation: (conversationId) =>
      set({
        conversationId,
        ...EMPTY_BUFFERS,
        connectionStatus: 'connecting',
      }),

    reset: () => set({ conversationId: null, ...EMPTY_BUFFERS, connectionStatus: 'idle' }),

    beginPlan: () => set({ nodeStatuses: {} }),

    markNodeRunning: (nodeId) =>
      set((state) => ({
        nodeStatuses: { ...state.nodeStatuses, [nodeId]: 'running' },
      })),

    markNodeFinished: (nodeId, status) =>
      set((state) => ({
        nodeStatuses: { ...state.nodeStatuses, [nodeId]: status },
      })),

    markNodeFailed: (nodeId) =>
      set((state) => ({
        nodeStatuses: { ...state.nodeStatuses, [nodeId]: 'failed' },
      })),

    appendAnswerToken: (turnId, token) =>
      set((state) => {
        const current = state.streamingAnswer
        if (current && current.turnId === turnId) {
          return {
            streamingAnswer: { ...current, text: current.text + token },
          }
        }
        // A *different* `turn_id` replaces the buffer: the drawer
        // pane shows only the newest answer. Turn `u1`'s completed
        // text is already persisted on the Turn row (T10), so
        // nothing is lost — the buffer is a live-progress artifact.
        return { streamingAnswer: { turnId, text: token, done: false } }
      }),

    finishActiveAnswer: () =>
      set((state) => {
        const current = state.streamingAnswer
        if (!current) return state
        return { streamingAnswer: { ...current, done: true } }
      }),

    setConnectionStatus: (connectionStatus) => set({ connectionStatus }),
  }),
)
