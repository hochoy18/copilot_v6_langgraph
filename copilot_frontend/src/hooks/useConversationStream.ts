/**
 * `useConversationStream` — T24 / #21.
 *
 * Thin adapter between the transport-only `useEventSource` hook
 * and the two Zustand stores (ADR-0029's "事件分发到 Zustand
 * store"). Chat surfaces call `useConversationStream(id)` once and
 * read the stores; nobody else opens an EventSource.
 *
 * Store split follows issue #53's handoff contract:
 *
 * - Plan *content* is written only through `usePlanDrawerStore`
 *   (`showPlan` on `plan.generated`, `replacePlan` on
 *   `plan.modified`, `markExecutionOutcome` on
 *   `execution.completed`) — the drawer keeps being the single
 *   Plan write point.
 * - Live *progress* (node badges, typewriter answer, connection
 *   pill) lands in `useConversationStreamStore`.
 */
import { useEventSource } from '@/hooks/useEventSource'
import { useConversationStreamStore } from '@/stores/conversation-stream'
import { usePlanDrawerStore } from '@/stores/plan-drawer'

/**
 * Open the SSE stream for `conversationId` (or stay dormant if
 * `null`). The hook keeps the stores in sync until the component
 * unmounts.
 */
export function useConversationStream(conversationId: string | null): void {
  useEventSource(conversationId, {
    onOpen: () => {
      useConversationStreamStore.getState().setConnectionStatus('open')
    },
    onClose: () => {
      useConversationStreamStore
        .getState()
        .setConnectionStatus('closed')
    },
    onReconnect: () => {
      useConversationStreamStore
        .getState()
        .setConnectionStatus('reconnecting')
    },
    onAuthFailed: () => {
      useConversationStreamStore
        .getState()
        .setConnectionStatus('auth-failed')
    },
    onEvent: (event) => {
      const stream = useConversationStreamStore.getState()
      const drawer = usePlanDrawerStore.getState()
      switch (event.event) {
        case 'plan.generated':
          // Hydrate the drawer (same path as T19's synchronous Turn
          // response — idempotent if both land) and wipe stale node
          // badges for the new execution.
          drawer.showPlan(event.payload.plan)
          stream.beginPlan()
          return
        case 'plan.modified':
          drawer.replacePlan(event.payload.plan)
          return
        case 'tool.started':
          stream.markNodeRunning(event.payload.node_id)
          return
        case 'tool.finished':
          stream.markNodeFinished(event.payload.node_id, event.payload.status)
          return
        case 'tool.failed':
          stream.markNodeFailed(event.payload.node_id)
          return
        case 'llm.token':
          stream.appendAnswerToken(event.payload.turn_id, event.payload.token)
          return
        case 'execution.completed':
          stream.finishActiveAnswer()
          drawer.markExecutionOutcome(event.payload.plan_id, event.payload.status)
          return
        case 'cost.warning':
          // ADR-0025's soft signal — the banner UI is a cost
          // follow-up ticket, not T24. Consumed-but-unrendered is
          // intentional: dropping it silently here (rather than in
          // the transport) keeps the decision visible at the fold.
          return
        default:
          // `stream.opened` / `stream.heartbeat` / `auth.expired`
          // are filtered inside `useEventSource` and never reach
          // this dispatcher.
          return
      }
    },
    // The hook only runs while a conversation is open; ChatPage
    // hands `null` until the user has actually submitted a turn.
    enabled: conversationId !== null,
  })
}
