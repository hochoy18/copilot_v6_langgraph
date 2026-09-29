import '@testing-library/jest-dom/vitest'

/**
 * jsdom lacks the observers React Flow (@xyflow/react) measures nodes
 * with. No-op stubs are enough: node content renders regardless of the
 * measured dimensions, and no test asserts zoom/pan behaviour.
 */
class NoopObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

if (!('ResizeObserver' in globalThis)) {
  globalThis.ResizeObserver = NoopObserver as unknown as typeof ResizeObserver
}
if (!('IntersectionObserver' in globalThis)) {
  globalThis.IntersectionObserver = NoopObserver as unknown as typeof IntersectionObserver
}

/**
 * jsdom has no WHATWG `EventSource`. The chat pages subscribe to the
 * SSE progress stream via `useConversationStream` (T24 / #21); the
 * hook's `openSocket` would crash on `new EventSource(url)` and React
 * would surface the uncaught error against the component under test.
 *
 * Tests that exercise the SSE transport itself (`useEventSource.test.tsx`)
 * inject a `MockSocket` via the hook's `eventSourceFactory` seam. The
 * rest of the suite (chat / list / routing tests) just needs the
 * constructor to exist and the returned object to satisfy the
 * `EventSourceLike` shape — the connection never sends a single event
 * because no test asserts on stream payloads at this layer.
 */
class NoopEventSource implements EventSource {
  readonly CONNECTING = 0 as const
  readonly OPEN = 1 as const
  readonly CLOSED = 2 as const
  readyState: 0 | 1 | 2 = this.CONNECTING
  onopen: ((this: EventSource, ev: Event) => unknown) | null = null
  onerror: ((this: EventSource, ev: Event) => unknown) | null = null
  onmessage: ((this: EventSource, ev: MessageEvent) => unknown) | null = null
  url = ''
  withCredentials = false
  addEventListener(): void {}
  removeEventListener(): void {}
  dispatchEvent(): boolean {
    return true
  }
  close(): void {
    this.readyState = this.CLOSED
  }
}

if (typeof globalThis.EventSource === 'undefined') {
  globalThis.EventSource = NoopEventSource as unknown as typeof EventSource
}
