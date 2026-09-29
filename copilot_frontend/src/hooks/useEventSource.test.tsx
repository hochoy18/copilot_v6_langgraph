import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  buildStreamUrl,
  useEventSource,
  type EventSourceLike,
} from '@/hooks/useEventSource'
import { useAuthStore } from '@/stores/auth'
import type { SseEvent, SseEventName } from '@/types/sse'

/**
 * `useEventSource` — T24 / #21 acceptance criteria:
 * - [x] 断线自动重连       (backoff + cursor tests)
 * - [x] Token 过期自动换新  (auth.expired → refresh → token-restart)
 *
 * The transport is exercised against a `MockSocket` factory —
 * jsdom has no `EventSource`, and the hook deliberately takes one
 * as an injectable seam. Events are delivered as the browser would
 * deliver them: per-name `MessageEvent`s carrying the `data:` JSON
 * and the frame's `id:` as `lastEventId` (T23's
 * `serialize_event` always stamps both, so the mock does too).
 */

class MockSocket implements EventSourceLike {
  onopen: ((ev: Event) => unknown) | null = null
  onerror: ((ev: Event) => unknown) | null = null
  closeCount = 0
  /**
   * WHATWG readyState — tests flip this to 2 (CLOSED) before
   * `emitError` to simulate a server-rejected connect (non-200).
   * `close()` deliberately does NOT touch it: the hook reads the
   * state *before* closing, and leaving it settable keeps both
   * flavours of error testable.
   */
  readyState = 1
  /** Captured by the test factory to assert the connect URL. */
  url = ''

  private listeners = new Map<string, Array<(ev: MessageEvent) => unknown>>()

  addEventListener(
    type: string,
    listener: (this: EventSourceLike, ev: MessageEvent) => unknown,
  ): void {
    const existing = this.listeners.get(type) ?? []
    existing.push(listener)
    this.listeners.set(type, existing)
  }

  close(): void {
    this.closeCount += 1
  }

  emitOpen(): void {
    this.onopen?.call(this, new Event('open'))
  }

  /** Dispatch one named event the way the browser would. */
  emitEvent(
    name: SseEventName,
    id: number,
    payload: Record<string, unknown> = {},
  ): void {
    const envelope = {
      id,
      event: name,
      conversation_id: 'c1',
      occurred_at: '2026-09-29T07:00:00Z',
      payload,
    }
    const ev = {
      data: JSON.stringify(envelope),
      lastEventId: String(id),
    } as unknown as MessageEvent
    for (const listener of this.listeners.get(name) ?? []) {
      listener.call(this, ev)
    }
  }

  emitError(): void {
    this.onerror?.call(this, new Event('error'))
  }
}

let sockets: MockSocket[]

function mockSocketFactory(url: string): MockSocket {
  const socket = new MockSocket()
  socket.url = url
  sockets.push(socket)
  return socket
}

beforeEach(() => {
  sockets = []
  useAuthStore.setState({ accessToken: 'jwt-0', refreshToken: 'rt-0', refreshFailed: false })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

function setup(
  overrides: {
    initialBackoffMs?: number
    maxBackoffMs?: number
    enabled?: boolean
    onEvent?: (event: SseEvent) => void
    onAuthFailed?: () => void
  } = {},
) {
  const onEvent = overrides.onEvent ?? vi.fn()
  const onOpen = vi.fn()
  const onClose = vi.fn()
  const onReconnect = vi.fn()
  const onAuthFailed = overrides.onAuthFailed ?? vi.fn()
  const result = renderHook(
    ({ id }: { id: string | null }) =>
      useEventSource(id, {
        onEvent,
        onOpen,
        onClose,
        onReconnect,
        onAuthFailed,
        eventSourceFactory: mockSocketFactory,
        initialBackoffMs: overrides.initialBackoffMs ?? 100,
        maxBackoffMs: overrides.maxBackoffMs ?? 100,
        enabled: overrides.enabled ?? true,
      }),
    { initialProps: { id: 'c1' } },
  )
  return { result, onEvent, onOpen, onClose, onReconnect, onAuthFailed }
}

describe('buildStreamUrl', () => {
  it('attaches the access token as a query param (EventSource cannot set headers)', () => {
    expect(buildStreamUrl('c1', 'jwt.abc', 0)).toBe(
      '/api/v1/conversations/c1/stream?token=jwt.abc',
    )
  })

  it('appends the replay cursor once events have been seen', () => {
    expect(buildStreamUrl('c1', 'jwt.abc', 42)).toContain('last_event_id=42')
  })

  it('URL-encodes the conversation id', () => {
    expect(buildStreamUrl('a/b', 't', 0)).toContain('/conversations/a%2Fb/stream')
  })
})

describe('useEventSource connect', () => {
  it('opens the stream with the access token in the query param (T23 auth)', () => {
    setup()
    expect(sockets).toHaveLength(1)
    expect(sockets[0].url).toBe('/api/v1/conversations/c1/stream?token=jwt-0')
  })

  it('stays dormant without a conversation id or when disabled', () => {
    const { rerender } = renderHook(
      ({ id, enabled }: { id: string | null; enabled: boolean }) =>
        useEventSource(id, {
          onEvent: vi.fn(),
          enabled,
          eventSourceFactory: mockSocketFactory,
        }),
      { initialProps: { id: null as string | null, enabled: false } },
    )
    expect(sockets).toHaveLength(0)
    rerender({ id: 'c1', enabled: false })
    expect(sockets).toHaveLength(0)
    rerender({ id: 'c1', enabled: true })
    expect(sockets).toHaveLength(1)
  })

  it('surfaces auth-failed instead of opening when no token is in memory (ADR-0032)', () => {
    useAuthStore.setState({ accessToken: null })
    const { onAuthFailed } = setup()
    expect(sockets).toHaveLength(0)
    expect(onAuthFailed).toHaveBeenCalledTimes(1)
  })

  it('opens as soon as a token lands after mount (T08 login completes late)', () => {
    useAuthStore.setState({ accessToken: null })
    const { onAuthFailed } = setup()
    expect(sockets).toHaveLength(0)
    expect(onAuthFailed).toHaveBeenCalledTimes(1)

    // T08 will call `setTokens` after the OIDC callback; the hook
    // must react without any consumer-side plumbing.
    act(() => {
      useAuthStore.getState().setTokens(
        'jwt-late',
        'rt-late',
        {
          id: 'u1',
          email: 'late@example.com',
          display_name: 'Late Login',
          source: 'sso',
          username: null,
          role_ids: [],
        },
        900,
      )
    })
    expect(sockets).toHaveLength(1)
    expect(sockets[0].url).toContain('token=jwt-late')
  })

  it('dispatches named business events and skips transport-only ones', () => {
    const { onEvent, onOpen } = setup()
    const socket = sockets[0]

    socket.emitOpen()
    expect(onOpen).toHaveBeenCalledTimes(1)

    socket.emitEvent('stream.opened', 1, { high_water_mark: 1 })
    socket.emitEvent('stream.heartbeat', 2)
    socket.emitEvent('llm.token', 3, { token: '你', turn_id: 'u1' })

    expect(onEvent).toHaveBeenCalledTimes(1)
    const event = (onEvent as ReturnType<typeof vi.fn>).mock.calls[0][0] as SseEvent
    expect(event.event).toBe('llm.token')
    expect(event.payload.token).toBe('你')
  })

  it('drops frames whose data payload is not JSON without killing the stream', () => {
    const { onEvent } = setup()
    const socket = sockets[0]
    // Simulate a corrupted `data:` line arriving intact through the
    // browser layer (defensive: JSON.parse throw must not propagate).
    const badEv = { data: '{not json', lastEventId: '4' } as unknown as MessageEvent
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const listeners = (socket as any).listeners.get('llm.token') as Array<
      (ev: MessageEvent) => unknown
    >
    act(() => {
      for (const l of listeners) l.call(socket, badEv)
    })
    expect(onEvent).not.toHaveBeenCalled()

    // The stream keeps working for the next good frame.
    act(() => {
      socket.emitEvent('llm.token', 5, { token: 'ok', turn_id: 'u1' })
    })
    expect(onEvent).toHaveBeenCalledTimes(1)
  })

  it('calls onClose on unmount and closes the socket', () => {
    const { result, onClose } = setup()
    act(() => {
      result.unmount()
    })
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(sockets[0].closeCount).toBe(1)
  })
})

describe('useEventSource reconnect (断线自动重连)', () => {
  it('schedules a backoff reconnect after a socket error and resumes from the cursor', async () => {
    vi.useFakeTimers()
    const { onReconnect } = setup({ initialBackoffMs: 100, maxBackoffMs: 3_200 })
    const socket = sockets[0]

    socket.emitEvent('tool.started', 5, { node_id: 'n1', tool: 'search' })
    socket.emitError()

    // Hook opts out of the native auto-reconnect by closing the socket.
    expect(socket.closeCount).toBe(1)
    expect(onReconnect).toHaveBeenCalledWith(1, 100)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })
    expect(sockets).toHaveLength(2)
    // The replay cursor travelled with the reconnect (T23 buffer).
    expect(sockets[1].url).toContain('token=jwt-0')
    expect(sockets[1].url).toContain('last_event_id=5')
  })

  it('doubles the delay on consecutive failures and resets on a successful open', async () => {
    vi.useFakeTimers()
    const { onReconnect } = setup({ initialBackoffMs: 100, maxBackoffMs: 3_200 })

    sockets[0].emitError()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })
    expect(sockets).toHaveLength(2)

    sockets[1].emitError()
    expect(onReconnect).toHaveBeenLastCalledWith(2, 200)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })

    // A clean open resets the ladder — the next failure is 100ms again.
    sockets[2].emitOpen()
    sockets[2].emitError()
    expect(onReconnect).toHaveBeenLastCalledWith(1, 100)
    expect(sockets).toHaveLength(3)
  })

  it('caps the backoff at maxBackoffMs', async () => {
    vi.useFakeTimers()
    setup({ initialBackoffMs: 1_000, maxBackoffMs: 4_000 })

    for (const [, delay] of [
      [1, 1_000],
      [2, 2_000],
      [3, 4_000],
      [4, 4_000],
    ] as const) {
      sockets[sockets.length - 1].emitError()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(delay)
      })
    }
    // 4 failures → 4 sockets beyond the first, every delay ≤ the cap.
    expect(sockets).toHaveLength(5)
  })
})

describe('useEventSource token refresh (Token 过期自动换新)', () => {
  function refreshResponse(accessToken: string, refreshToken: string): Response {
    return new Response(
      JSON.stringify({
        access_token: accessToken,
        refresh_token: refreshToken,
        token_type: 'Bearer',
        expires_in: 900,
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )
  }

  it('refreshes the access token on auth.expired and reopens with the new one', async () => {
    const fetchMock = vi.fn().mockResolvedValue(refreshResponse('jwt-1', 'rt-1'))
    globalThis.fetch = fetchMock as unknown as typeof fetch

    const { onEvent, onAuthFailed } = setup()
    act(() => {
      sockets[0].emitEvent('auth.expired', 9)
    })
    await act(async () => {
      await Promise.resolve()
    })

    // One refresh call against the documented wire shape.
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/auth/refresh')
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({
      refresh_token: 'rt-0',
    })

    // The rotated token restarts the connect cycle: old socket torn
    // down, fresh one opens with the *new* token and the replay
    // cursor — the expired JWT never reconnects.
    expect(sockets).toHaveLength(2)
    expect(sockets[0].closeCount).toBeGreaterThanOrEqual(1)
    expect(sockets[1].url).toContain('token=jwt-1')
    expect(sockets[1].url).toContain('last_event_id=9')
    expect(onAuthFailed).not.toHaveBeenCalled()
    // auth.expired is transport-only; it never reaches onEvent.
    expect(onEvent).not.toHaveBeenCalled()
    expect(useAuthStore.getState().accessToken).toBe('jwt-1')
  })

  it('stops reconnecting when the refresh is rejected (revoked chain)', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ code: 'refresh_token_revoked' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    ) as unknown as typeof fetch

    const { onAuthFailed } = setup()
    act(() => {
      sockets[0].emitEvent('auth.expired', 9)
    })
    await act(async () => {
      await Promise.resolve()
    })

    // The wiped token restarts the effect once; the no-token branch
    // signals exactly one `auth-failed` and never re-opens.
    expect(onAuthFailed).toHaveBeenCalledTimes(1)
    expect(sockets).toHaveLength(1)
    expect(useAuthStore.getState().accessToken).toBeNull()
    expect(useAuthStore.getState().refreshToken).toBeNull()
  })

  it('reopens through the backoff ladder on a transient (5xx) refresh failure', async () => {
    vi.useFakeTimers()
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError('network down')) as unknown as typeof fetch

    const { onAuthFailed } = setup()
    act(() => {
      sockets[0].emitEvent('auth.expired', 4)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    // The chain may be fine — keep backing off with the current
    // token instead of giving up or wiping credentials.
    expect(onAuthFailed).not.toHaveBeenCalled()
    expect(useAuthStore.getState().accessToken).toBe('jwt-0')
    expect(useAuthStore.getState().refreshFailed).toBe(true)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })
    expect(sockets).toHaveLength(2)
    expect(sockets[1].url).toContain('token=jwt-0')
  })

  it('treats a rejected connect (readyState CLOSED) as auth loss and refreshes', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          access_token: 'jwt-1',
          refresh_token: 'rt-1',
          token_type: 'Bearer',
          expires_in: 900,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    globalThis.fetch = fetchMock as unknown as typeof fetch

    // Simulates "page resumed from sleep": the JWT is already past
    // `exp`, so T23 answers the connect itself with 401 — no
    // `auth.expired` event is ever emitted; only the socket error
    // signals it.
    setup()
    sockets[0].readyState = 2
    act(() => {
      sockets[0].emitError()
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(sockets).toHaveLength(2)
    expect(sockets[1].url).toContain('token=jwt-1')
  })

  it('caps consecutive rejected-connect refreshes, then falls back to backoff', async () => {
    vi.useFakeTimers()
    let seq = 0
    const fetchMock = vi.fn().mockImplementation(() => {
      seq += 1
      return Promise.resolve(
        new Response(
          JSON.stringify({
            access_token: `jwt-${seq}`,
            refresh_token: `rt-${seq}`,
            token_type: 'Bearer',
            expires_in: 900,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
    })
    globalThis.fetch = fetchMock as unknown as typeof fetch

    setup()
    // Four rejections, each "immediately" after the token-driven
    // restart — a backend that refreshes fine but never lets the
    // stream connect (e.g. conversation deleted → 404).
    for (let i = 0; i < 4; i += 1) {
      const socket = sockets[sockets.length - 1]
      socket.readyState = 2
      act(() => {
        socket.emitError()
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(200)
      })
    }

    // Refresh runs until the cap (3), then stops: the fourth
    // rejection rides the plain backoff ladder with the last token.
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(sockets.length).toBeGreaterThanOrEqual(4)
    const last = sockets[sockets.length - 1]
    expect(last.url).toContain('token=jwt-3')
    expect(last.url).not.toContain('token=jwt-4')
  })

  it('a socket error during an in-flight refresh does not double-schedule', async () => {
    vi.useFakeTimers()
    let resolveRefresh: (value: Response) => void = () => {}
    globalThis.fetch = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveRefresh = resolve
        }),
    ) as unknown as typeof fetch

    setup()
    act(() => {
      sockets[0].emitEvent('auth.expired', 3)
    })
    // The server closes right after auth.expired → error fires while
    // the refresh round-trip is pending. Must not open a stale socket.
    act(() => {
      sockets[0].emitError()
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(sockets).toHaveLength(1)

    resolveRefresh(refreshResponse('jwt-1', 'rt-1'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(sockets).toHaveLength(2)
    expect(sockets[1].url).toContain('token=jwt-1')
  })
})
