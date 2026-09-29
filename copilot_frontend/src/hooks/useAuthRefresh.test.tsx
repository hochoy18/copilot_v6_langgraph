import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useAuthRefresh } from '@/hooks/useAuthRefresh'
import { useAuthStore } from '@/stores/auth'

/**
 * `useAuthRefresh` — T08 / #9 AC: "Token 剩 ≤2 min 自动续期".
 *
 * Three properties drive the hook:
 *
 * 1. With `expiresAt` set such that `expiresAt - now > REFRESH_LEAD_MS`,
 *    the hook waits the difference minus the lead.
 * 2. When the remaining window drops to ≤ the lead, the timer fires
 *    and `refreshAccessToken` is called.
 * 3. When `accessToken` becomes null (logout / refresh failure), the
 *    hook is dormant — no timer is scheduled, no fetch is fired.
 *
 * `vi.useFakeTimers()` lets us step the clock deterministically.
 * `refreshAccessToken` calls `apiFetch`; stubbing `globalThis.fetch`
 * keeps the test focused on the timing decision without dragging in
 * the full API surface.
 */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  // Start every test from a clean store so the previous test's
  // tokens / expiresAt don't carry over.
  useAuthStore.setState({
    accessToken: null,
    refreshToken: null,
    user: null,
    expiresAt: null,
    refreshFailed: false,
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useAuthRefresh', () => {
  it('stays dormant when there is no access token', () => {
    const fetchMock = vi.fn()
    globalThis.fetch = fetchMock
    renderHook(() => useAuthRefresh())
    vi.advanceTimersByTime(10 * 60 * 1000)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('schedules the refresh for (remaining - 2min) when the lead is comfortably far', () => {
    const fetchMock = vi.fn()
    globalThis.fetch = fetchMock

    // 15-minute token, well outside the 2-minute lead.
    act(() => {
      useAuthStore.getState().setTokens(
        'jwt',
        'rt',
        {
          id: 'u1',
          email: 'u@example.com',
          display_name: 'User',
          source: 'sso',
          username: null,
          role_ids: [],
        },
        15 * 60,
      )
    })
    renderHook(() => useAuthRefresh())

    // 12 minutes pass — still inside the lead, no refresh yet.
    vi.advanceTimersByTime(12 * 60 * 1000)
    expect(fetchMock).not.toHaveBeenCalled()

    // One more minute — total elapsed 13 min, remaining 2 min, lead
    // boundary crossed. The hook fires immediately.
    vi.advanceTimersByTime(60 * 1000)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('rotates immediately when the remaining lifetime is already inside the lead', () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        access_token: 'jwt.next',
        refresh_token: 'rt.next',
        token_type: 'Bearer',
        expires_in: 900,
        user: {
          id: 'u1',
          email: 'u@example.com',
          display_name: 'User',
          source: 'sso',
          username: null,
          role_ids: [],
        },
      }),
    )
    globalThis.fetch = fetchMock

    act(() => {
      useAuthStore.getState().setTokens(
        'jwt',
        'rt',
        {
          id: 'u1',
          email: 'u@example.com',
          display_name: 'User',
          source: 'sso',
          username: null,
          role_ids: [],
        },
        // 60 s — already inside the 2-minute lead. `clampRefreshDelay`
        // floors the delay at 0 so the timer fires on the next tick.
        60,
      )
    })
    renderHook(() => useAuthRefresh())
    vi.advanceTimersByTime(0)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('clears its timer when the access token is wiped mid-flight', () => {
    const fetchMock = vi.fn()
    globalThis.fetch = fetchMock

    act(() => {
      useAuthStore.getState().setTokens(
        'jwt',
        'rt',
        {
          id: 'u1',
          email: 'u@example.com',
          display_name: 'User',
          source: 'sso',
          username: null,
          role_ids: [],
        },
        15 * 60,
      )
    })
    renderHook(() => useAuthRefresh())

    // Wipe the token (logout / refresh-failure branch). The hook
    // re-runs with `accessToken === null` and skips the timer.
    act(() => {
      useAuthStore.setState({
        accessToken: null,
        refreshToken: null,
        expiresAt: null,
        user: null,
      })
    })
    vi.advanceTimersByTime(20 * 60 * 1000)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})