import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { LoginPage } from '@/pages/LoginPage'
import { renderWithRouter } from '@/test-utils'

/**
 * LoginPage — T08 / #9 AC: "点登录跳 IdP".
 *
 * The page is a single button that triggers
 * `startSsoLogin()` + `window.location.assign`. The mock covers the
 * fetch side and the navigation side separately so we can verify
 * both halves without coupling them.
 */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function mockFetch(responses: ReadonlyArray<Response>): ReturnType<typeof vi.fn> {
  const fn = vi.fn()
  for (const response of responses) {
    fn.mockResolvedValueOnce(response)
  }
  globalThis.fetch = fn as unknown as typeof fetch
  return fn
}

/**
 * jsdom's `window.location` is read-only by default; we replace it
 * with a mutable proxy that records `assign` calls.
 */
function mockLocation(): { assign: ReturnType<typeof vi.fn>; restore: () => void } {
  const original = window.location
  // Cast through `unknown` — jsdom's Location has more members than
  // our tests use, but the only one we actually read or assign is
  // `assign`, so the narrow surface is fine.
  const stub = { assign: vi.fn() } as unknown as Location
  Object.defineProperty(window, 'location', {
    configurable: true,
    get: () => stub,
    set: () => {},
  })
  return {
    assign: stub.assign as unknown as ReturnType<typeof vi.fn>,
    restore: () => {
      Object.defineProperty(window, 'location', {
        configurable: true,
        get: () => original,
        set: () => {},
      })
    },
  }
}

let locationStub: ReturnType<typeof mockLocation>

beforeEach(() => {
  locationStub = mockLocation()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  locationStub.restore()
})

describe('LoginPage', () => {
  it('fetches the IdP URL and navigates the user to it on click (点登录跳 IdP)', async () => {
    const fetchMock = mockFetch([
      jsonResponse({
        authorization_url: 'https://idp.example.com/authorize?...',
        state: 'csrf',
      }),
    ])
    renderWithRouter(<LoginPage />)
    const user = userEvent.setup()
    await user.click(screen.getByTestId('login-button'))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/auth/sso/login')
    expect(locationStub.assign).toHaveBeenCalledTimes(1)
    expect(locationStub.assign).toHaveBeenCalledWith(
      'https://idp.example.com/authorize?...',
    )
  })

  it('surfaces an API failure inline and does not navigate', async () => {
    mockFetch([jsonResponse({ code: 'oidc_discovery_failed' }, 503)])
    renderWithRouter(<LoginPage />)
    const user = userEvent.setup()
    await user.click(screen.getByTestId('login-button'))

    expect(await screen.findByRole('alert')).toHaveTextContent(/无法发起登录/)
    expect(locationStub.assign).not.toHaveBeenCalled()
  })

  it('surfaces a network failure inline (no body to parse)', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))
    globalThis.fetch = fetchMock as unknown as typeof fetch
    renderWithRouter(<LoginPage />)
    const user = userEvent.setup()
    await user.click(screen.getByTestId('login-button'))

    expect(await screen.findByRole('alert')).toHaveTextContent(/无法连接后端服务/)
    expect(locationStub.assign).not.toHaveBeenCalled()
  })
})