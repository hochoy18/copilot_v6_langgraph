import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor } from '@testing-library/react'

import App from '@/App'
import { jsonResponse, mockFetch, renderWithRouter, makeAuthUser } from '@/test-utils'
import { useAuthStore } from '@/stores/auth'

/**
 * AuthCallbackPage — T08 / #9 AC: "回调 /chat 显示用户名" +
 * "刷新仍登录".
 *
 * Three scenarios:
 *
 * 1. Happy path — IdP redirects with `?code=&state=`; the page POSTs
 *    both to the backend, folds the response into the auth store,
 *    and navigates to `/chat` (replacing the history entry so a
 *    Back click doesn't drag the user back through the IdP).
 * 2. Backend error — surface inline, leave the user on the page.
 * 3. Missing query params — bounce straight to `/auth/login`.
 *
 * We render the whole `App` (not just `AuthCallbackPage`) so the
 * `<Routes>` table actually has `/chat` and `/auth/login` mounted
 * when the navigate runs — `MemoryRouter` doesn't auto-add
 * routes, so a bare AuthCallbackPage would render an empty `<div />`
 * after the redirect.
 */

const callbackResponse = {
  access_token: 'jwt.callback',
  refresh_token: 'rt.callback',
  token_type: 'Bearer',
  expires_in: 900,
  user: makeAuthUser({
    id: 'u-callback',
    email: 'alice@example.com',
    display_name: 'Alice Liu',
  }),
}

beforeEach(() => {
  // Every test starts from a clean store — the callback writes into
  // it, and a previous run leaking state would mask bugs (e.g. a
  // stuck `user` from a happy-path test).
  useAuthStore.setState({
    accessToken: null,
    refreshToken: null,
    user: null,
    expiresAt: null,
    refreshFailed: false,
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('AuthCallbackPage', () => {
  it('exchanges code+state, stores the tokens, and navigates to /chat (回调 /chat 显示用户名)', async () => {
    const fetchMock = mockFetch([jsonResponse(callbackResponse, 200)])
    renderWithRouter(<App />, {
      initialEntries: ['/auth/callback?code=abc&state=xyz'],
    })

    // The page mounts with the "登录中…" placeholder while the
    // exchange is in flight.
    expect(screen.getByTestId('auth-callback-page')).toHaveTextContent(/登录中/)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/auth/sso/callback')
    expect((init as RequestInit).method).toBe('POST')
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      code: 'abc',
      state: 'xyz',
    })

    // The store now holds the user + tokens. `setTokens` stamps
    // `expiresAt` from `Date.now() + expires_in * 1000`; pin
    // membership only so a flaky clock doesn't flake the test.
    await waitFor(() => {
      const state = useAuthStore.getState()
      expect(state.accessToken).toBe('jwt.callback')
      expect(state.refreshToken).toBe('rt.callback')
      expect(state.user?.display_name).toBe('Alice Liu')
      expect(state.expiresAt).not.toBeNull()
    })

    // Routed to /chat. The chat shell mounts with the freshly-stored
    // user, and the header renders the display name.
    await waitFor(() => {
      expect(screen.getByTestId('chat-page')).toBeInTheDocument()
    })
    expect(screen.getByTestId('chat-username')).toHaveTextContent('Alice Liu')
    expect(screen.queryByTestId('auth-callback-page')).not.toBeInTheDocument()
  })

  it('surfaces an API failure inline (e.g. oidc_state_mismatch) without navigating', async () => {
    mockFetch([jsonResponse({ code: 'oidc_state_mismatch' }, 400)])
    renderWithRouter(<App />, {
      initialEntries: ['/auth/callback?code=abc&state=xyz'],
    })
    expect(await screen.findByRole('alert')).toHaveTextContent(/登录失败/)
    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    // No tokens land in the store on failure.
    expect(useAuthStore.getState().accessToken).toBeNull()
    expect(useAuthStore.getState().user).toBeNull()
  })

  it('bounces to /auth/login when the IdP omits code or state', () => {
    renderWithRouter(<App />, {
      initialEntries: ['/auth/callback?state=xyz'],
    })
    // `<Navigate replace />` flips the location; the LoginPage
    // (registered in App.tsx) renders in its place.
    expect(screen.getByTestId('login-page')).toBeInTheDocument()
  })
})