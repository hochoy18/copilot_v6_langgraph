/**
 * `useEventSource` — T24 / #21, ADR-0010 / ADR-0029.
 *
 * The single subscription path for the chat progress stream. Per
 * ADR-0029 ("组件订阅 store 里的 plan / tool 状态, 不用各自维护
 * EventSource"), components don't own their own `EventSource`;
 * `useConversationStream` wires this transport hook to the Zustand
 * stores. Responsibilities split like so:
 *
 * - **Transport** (this file) — open the socket, dispatch named
 *   events, reconnect with exponential backoff, handle
 *   `auth.expired` by calling `/auth/refresh` and re-opening with
 *   the new token.
 * - **State** (`useConversationStream` → `useConversationStreamStore`
 *   / `usePlanDrawerStore`) — fold parsed events into the per-node
 *   status map, the streaming answer buffer, and the drawer's Plan.
 * - **Render** (`PlanDrawer` / `PlanToolNode` / `PlanAnswerPane`) —
 *   read from the stores.
 *
 * Why per-name `addEventListener` (not `onmessage` + own parser)
 * --------------------------------------------------------------
 *
 * T23 stamps every event with an `event:` line
 * (`app.realtime.events.serialize_event`). The WHATWG spec routes
 * *named* events only to listeners registered under that name —
 * `onmessage` sees just the unnamed ones, which T23 never emits.
 * The browser also parses the frame (id / data lines) natively and
 * surfaces the cursor on `MessageEvent.lastEventId`, so a
 * hand-rolled frame parser would duplicate (and drift from) the
 * platform.
 *
 * Reconnect policy
 * ----------------
 *
 * The WHATWG `EventSource` auto-reconnects on socket close with no
 * backoff — during a server flap that hammers the endpoint. We opt
 * out of the native loop (`close()` in `onerror`) and drive our own
 * timer, so the reconnect always carries `?last_event_id=` (T23's
 * query fallback — a *fresh* EventSource never sends the header
 * itself). Backoff starts at 1s, doubles to a 30s ceiling, and
 * resets on a successful open.
 *
 * `onerror` splits by WHATWG's own distinction: a *stream end*
 * (readyState CONNECTING — the server will replay on reconnect)
 * takes the plain backoff path; a *rejected connect* (readyState
 * CLOSED — non-200 response, e.g. the JWT already past `exp` when
 * the page resumed from sleep) can never succeed with the same
 * credentials, so it enters the auth-loss path (refresh + restart)
 * instead of retrying a guaranteed 401. Rejections without a
 * single successful open between them are capped so a backend that
 * answers `/auth/refresh` fine but keeps rejecting `/stream` (a
 * deleted conversation, say) degrades to the backoff ladder rather
 * than a refresh busy-loop.
 *
 * Token lifecycle
 * ---------------
 *
 * The hook subscribes to the auth store's Access Token, so two
 * flows need no manual plumbing: (1) a login that completes *after*
 * this component mounted (T08) — the token landing re-triggers the
 * effect and opens the socket; (2) `auth.expired` → `/auth/refresh`
 * — the rotated token re-triggers the same restart, and the
 * per-conversation cursor map resumes from the last seen id. A
 * terminal refresh failure (revoked chain) stops the socket and
 * fires `onAuthFailed`; the chat layer drives the re-login UI.
 * (ADR-0032.)
 *
 * Test seam
 * ---------
 *
 * jsdom has no `EventSource`; the hook accepts an
 * `eventSourceFactory` option and `useEventSource.test.tsx` injects
 * a mock that dispatches named-message events. The default factory
 * is the global constructor, so production code never touches the
 * seam.
 */
import { useEffect, useRef } from 'react'

import { refreshAccessToken, useAuthStore } from '@/stores/auth'
import type { SseEvent, SseEventName } from '@/types/sse'

/**
 * Runtime registry of event names to attach listeners for. A
 * `Record<SseEventName, …>` is exhaustive by construction: adding a
 * new event to the backend registry (`app.realtime.events`) makes
 * this map — and therefore the listener fan-out — a compile error
 * until it's updated, mirroring the backend's own `Literal[...]`
 * typo guard.
 */
const SSE_EVENT_REGISTRY: Record<SseEventName, true> = {
  'stream.opened': true,
  'stream.heartbeat': true,
  'auth.expired': true,
  'plan.generated': true,
  'plan.modified': true,
  'tool.started': true,
  'tool.finished': true,
  'tool.failed': true,
  'llm.token': true,
  'execution.completed': true,
  'cost.warning': true,
}
const SSE_EVENT_NAMES = Object.keys(SSE_EVENT_REGISTRY) as SseEventName[]

/** Discriminated callbacks — event dispatch plus lifecycle hooks. */
export interface EventSourceHandlers {
  /** Called for every business event (never transport-only ones). */
  onEvent(event: SseEvent): void
  /** Called when the socket has opened (transport-level). */
  onOpen?(): void
  /** Called when the socket has been torn down (intentionally or by the server). */
  onClose?(): void
  /**
   * Called when the hook is about to schedule a reconnect. Lets the
   * consumer surface a "reconnecting…" indicator without having to
   * poll the hook's internal status.
   */
  onReconnect?(attempt: number, delayMs: number): void
  /**
   * Called when `/auth/refresh` failed terminally and the hook is
   * giving up. The auth store's `refreshFailed` flag is also
   * flipped, so most consumers can simply read that.
   */
  onAuthFailed?(): void
}

export interface UseEventSourceOptions extends EventSourceHandlers {
  /**
   * Override the `EventSource` factory. The default is the global
   * `EventSource` constructor; tests inject a mock that dispatches
   * canned named events.
   */
  eventSourceFactory?: EventSourceFactory
  /** First reconnect delay in ms; doubles on every failure up to `maxBackoffMs`. */
  initialBackoffMs?: number
  /** Cap on the exponential backoff window. */
  maxBackoffMs?: number
  /** When false, the hook is dormant — useful for parent-gated usage. */
  enabled?: boolean
}

export type EventSourceFactory = (url: string) => EventSourceLike

/**
 * Narrowed subset of the WHATWG `EventSource` interface this hook
 * actually consumes. Lets the test factory substitute an object
 * without inheriting every EventSource member (which jsdom doesn't
 * implement).
 */
export interface EventSourceLike {
  onopen: ((this: EventSourceLike, ev: Event) => unknown) | null
  onerror: ((this: EventSourceLike, ev: Event) => unknown) | null
  /**
   * WHATWG `readyState`: 0 CONNECTING / 1 OPEN / 2 CLOSED. The hook
   * only reads it in `onerror` to tell "transient stream end"
   * (browser would retry) from "server rejected the connect"
   * (browser gave up — retrying the same credentials is pointless,
   * see the reconnect-policy note above).
   */
  readonly readyState: number
  addEventListener(
    type: string,
    listener: (this: EventSourceLike, ev: MessageEvent) => unknown,
  ): void
  /** Native `EventSource.close()`. */
  close(): void
}

/** WHATWG `EventSource.CLOSED` — the "fail the connection" state. */
const READY_STATE_CLOSED = 2

/**
 * Max consecutive rejected connects (no successful open between
 * them) that still trigger a `/auth/refresh` attempt. Past the cap
 * the hook falls back to plain backoff, bounding the load when the
 * rejection isn't actually about auth (deleted conversation etc.).
 */
const MAX_AUTH_LOSS_ATTEMPTS = 3

const DEFAULT_INITIAL_BACKOFF_MS = 1_000
const DEFAULT_MAX_BACKOFF_MS = 30_000

/**
 * Build the SSE URL the hook connects to. The access token travels
 * in `?token=` because `EventSource` cannot set custom headers
 * (T23 / #20, ADR-0010). The `last_event_id` query param is the
 * replay cursor for *manual* reconnects — the standard
 * `Last-Event-ID` header only accompanies the browser's native
 * reconnect, which this hook deliberately disables.
 */
export function buildStreamUrl(
  conversationId: string,
  token: string,
  lastEventId: number,
): string {
  const base = `/api/v1/conversations/${encodeURIComponent(conversationId)}/stream`
  const params = new URLSearchParams({ token })
  if (lastEventId > 0) params.set('last_event_id', String(lastEventId))
  return `${base}?${params.toString()}`
}

/**
 * Subscribe to one conversation's SSE stream for the lifetime of the
 * component. Re-opens on transient failure *and* on access-token
 * changes (login completing after mount, `auth.expired` refresh),
 * refreshing the token automatically on `auth.expired` and giving
 * up only when `/auth/refresh` returns a terminal failure.
 */
export function useEventSource(
  conversationId: string | null,
  options: UseEventSourceOptions,
): void {
  const {
    onEvent,
    onOpen,
    onClose,
    onReconnect,
    onAuthFailed,
    eventSourceFactory,
    initialBackoffMs = DEFAULT_INITIAL_BACKOFF_MS,
    maxBackoffMs = DEFAULT_MAX_BACKOFF_MS,
    enabled = true,
  } = options

  // Hold the callbacks in refs so re-renders with new function
  // identities don't tear down + re-open the socket. The hook is
  // already expected to be cheap to subscribe; recreating the
  // EventSource on every parent render would flood the server.
  const handlerRef = useRef<EventSourceHandlers>({
    onEvent,
    onOpen,
    onClose,
    onReconnect,
    onAuthFailed,
  })
  handlerRef.current = { onEvent, onOpen, onClose, onReconnect, onAuthFailed }

  const factoryRef = useRef<EventSourceFactory | undefined>(eventSourceFactory)
  factoryRef.current = eventSourceFactory

  const optionsRef = useRef({
    initialBackoffMs,
    maxBackoffMs,
  })
  optionsRef.current = { initialBackoffMs, maxBackoffMs }

  // Reactive token read: a login that lands *after* this component
  // mounted (or the `auth.expired` refresh swapping the JWT)
  // re-triggers the effect below and re-opens the socket with the
  // fresh credentials. The value itself is read from the store at
  // connect time — subscribing here is only about restart timing.
  const accessToken = useAuthStore((s) => s.accessToken)

  // Replay cursor survives socket restarts (backoff reconnect,
  // token-driven restart) but is scoped per conversation — a
  // conversation switch must not resume from the old channel's ids.
  const cursorsRef = useRef(new Map<string, number>())

  // Consecutive rejected connects across token-rotation restarts.
  // Reset on the first successful open; the cap turns a non-auth
  // rejection (deleted conversation) into plain backoff instead of
  // a refresh busy-loop.
  const rejectionsRef = useRef(0)

  useEffect(() => {
    if (!enabled || !conversationId) return
    // Narrowed snapshot of the guarded id — TS can't keep the
    // parameter's narrowing inside the nested functions below.
    const conversation = conversationId

    let socket: EventSourceLike | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    // Seed the cursor from the persisted per-conversation value.
    let lastEventId = cursorsRef.current.get(conversation) ?? 0
    let attempt = 0
    let authRefreshInFlight = false
    let disposed = false

    const rememberCursor = (id: number): void => {
      lastEventId = Math.max(lastEventId, id)
      cursorsRef.current.set(conversation, lastEventId)
    }

    // Wrap the global EventSource so its native `EventSource`
    // type narrows to our `EventSourceLike` interface. The cast is
    // safe because `EventSource` implements every member
    // `EventSourceLike` declares.
    const factory = (): EventSourceFactory =>
      factoryRef.current ??
      ((url: string) => new EventSource(url) as unknown as EventSourceLike)

    function clearReconnect(): void {
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
    }

    function scheduleReconnect(): void {
      if (disposed) return
      attempt += 1
      const base = optionsRef.current.initialBackoffMs
      const cap = optionsRef.current.maxBackoffMs
      const delay = Math.min(cap, base * 2 ** (attempt - 1))
      handlerRef.current.onReconnect?.(attempt, delay)
      clearReconnect()
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null
        openSocket()
      }, delay)
    }

    function openSocket(): void {
      if (disposed) return
      const token = useAuthStore.getState().accessToken
      if (!token) {
        // No credentials → surface the auth-failed signal and stop;
        // a token landing later (T08 login) re-triggers this effect
        // through the `accessToken` subscription.
        handlerRef.current.onAuthFailed?.()
        return
      }

      const url = buildStreamUrl(conversation, token, lastEventId)
      const next = factory()(url)
      socket = next

      next.onopen = () => {
        attempt = 0
        rejectionsRef.current = 0
        handlerRef.current.onOpen?.()
      }
      next.onerror = () => {
        // WHATWG fires `error` for both a transient stream end (the
        // native loop would retry instantly — we take over with our
        // own backoff) and a rejected connect (readyState CLOSED:
        // the browser gave up for good; with non-200 responses the
        // native spec does NOT retry). A rejection can never
        // succeed on the same credentials — T23's connect-time
        // 401 means the JWT is past `exp` (page resumed from
        // sleep) — so route it through the auth-loss path instead
        // of pointlessly backing off against a guaranteed 401.
        if (authRefreshInFlight) return
        const rejected = next.readyState === READY_STATE_CLOSED
        next.close()
        socket = null
        if (rejected) {
          if (rejectionsRef.current < MAX_AUTH_LOSS_ATTEMPTS) {
            rejectionsRef.current += 1
            void handleAuthExpired()
            return
          }
          // Past the cap the rejection isn't behaving like expiry
          // (refresh keeps answering fine) — degrade to the plain
          // backoff ladder so we never busy-loop `/auth/refresh`.
        }
        scheduleReconnect()
      }

      // WHATWG routes *named* events only to per-name listeners —
      // every T23 event carries an `event:` line, so `onmessage`
      // alone would see nothing. One listener per contract member.
      for (const name of SSE_EVENT_NAMES) {
        next.addEventListener(name, (ev) => handleNamedEvent(name, ev))
      }
    }

    function handleNamedEvent(name: SseEventName, ev: MessageEvent): void {
      // The browser surfaced the frame's `id:` line as
      // `lastEventId`; advance the cursor *before* dispatching so a
      // throwing handler still resumes from the right spot.
      const id = Number(ev.lastEventId)
      if (Number.isFinite(id)) rememberCursor(id)

      if (name === 'stream.opened' || name === 'stream.heartbeat') {
        // Transport-only (T23): the cursor above is all the hook
        // needs from them, so they never reach `onEvent`.
        return
      }

      let event: SseEvent
      try {
        event = JSON.parse(ev.data) as SseEvent
      } catch {
        // A malformed `data:` payload should never poison the whole
        // stream — drop the frame and let the next one continue.
        return
      }

      if (name === 'auth.expired') {
        void handleAuthExpired()
        return
      }
      handlerRef.current.onEvent(event)
    }

    async function handleAuthExpired(): Promise<void> {
      if (authRefreshInFlight) return
      authRefreshInFlight = true
      try {
        const newToken = await refreshAccessToken()
        if (!newToken) {
          // Terminal: the refresh chain is dead (revoked / rotated
          // token replay / refresh itself rejected). Tear down this
          // socket and stop.
          socket?.close()
          socket = null
          if (useAuthStore.getState().accessToken === null) {
            // The auth store wiped the token (revoked / replayed
            // chain) → this effect restarts and the no-token branch
            // in `openSocket` emits `onAuthFailed` exactly once.
            return
          }
          // Only `refreshFailed` was marked (transient 5xx / network
          // blip while refreshing; the chain itself is fine). Keep
          // the backoff ladder alive: when the backend recovers,
          // either the connect succeeds (token still valid) or the
          // rejection re-enters this path for another refresh.
          scheduleReconnect()
          return
        }
        // Success: `setTokens` updated the store's `accessToken`,
        // which re-triggers this effect — the restart re-opens with
        // the fresh JWT and the persisted cursor. Nothing to do
        // here except let the cleanup run.
      } finally {
        authRefreshInFlight = false
      }
    }

    openSocket()

    return () => {
      disposed = true
      clearReconnect()
      socket?.close()
      socket = null
      handlerRef.current.onClose?.()
    }
    // `conversationId` / `enabled` / `accessToken` are the connect-
    // cycle inputs; callbacks and backoff knobs live in refs so a
    // parent re-render with fresh closures never thrashes the
    // socket.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, enabled, accessToken])
}
