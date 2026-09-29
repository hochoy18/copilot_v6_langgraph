import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/lib/api-client'
import { completeSsoLogin, startSsoLogin } from '@/lib/auth-api'
import { jsonResponse, mockFetch } from '@/test-utils'

/**
 * `auth-api` — T08 / #9. The two SSO call sites:
 *
 * - `startSsoLogin`    → `GET  /api/v1/auth/sso/login`
 * - `completeSsoLogin` → `POST /api/v1/auth/sso/callback`
 *
 * `fetch` is stubbed per test so we exercise the real `apiFetch`
 * envelope (URL prefix, JSON content-type, error wrapping).
 */

afterEach(() => {
  vi.restoreAllMocks()
})

describe('startSsoLogin', () => {
  it('GETs /api/v1/auth/sso/login and returns the IdP URL + state', async () => {
    const fetchMock = mockFetch([
      jsonResponse({
        authorization_url: 'https://idp.example.com/authorize?...',
        state: 'csrf-token',
      }),
    ])
    const result = await startSsoLogin()
    expect(result).toEqual({
      authorization_url: 'https://idp.example.com/authorize?...',
      state: 'csrf-token',
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/auth/sso/login')
    // `apiFetch` doesn't set a method — fetch's default is GET, so
    // the absence of `method` *is* the GET.
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBeUndefined()
  })

  it('wraps a non-2xx in ApiError so LoginPage can render an alert', async () => {
    mockFetch([
      jsonResponse({ code: 'oidc_discovery_failed' }, 503),
    ])
    await expect(startSsoLogin()).rejects.toBeInstanceOf(ApiError)
  })
})

describe('completeSsoLogin', () => {
  it('POSTs {code, state} to /api/v1/auth/sso/callback and returns the token bundle', async () => {
    const fetchMock = mockFetch([
      jsonResponse({
        access_token: 'jwt.new',
        refresh_token: 'rt.new',
        token_type: 'Bearer',
        expires_in: 900,
        user: {
          id: 'u1',
          email: 'alice@example.com',
          display_name: 'Alice',
          source: 'sso',
          username: null,
          role_ids: [],
        },
      }),
    ])
    const result = await completeSsoLogin('auth-code', 'csrf-token')
    expect(result.access_token).toBe('jwt.new')
    expect(result.user.display_name).toBe('Alice')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/auth/sso/callback')
    expect((init as RequestInit).method).toBe('POST')
    const body = JSON.parse((init as RequestInit).body as string)
    expect(body).toEqual({ code: 'auth-code', state: 'csrf-token' })
  })

  it('propagates an OIDC state mismatch as an ApiError', async () => {
    mockFetch([
      jsonResponse({ code: 'oidc_state_mismatch' }, 400),
    ])
    await expect(completeSsoLogin('code', 'bad')).rejects.toBeInstanceOf(ApiError)
  })
})