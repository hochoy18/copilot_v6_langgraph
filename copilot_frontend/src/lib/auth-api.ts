/**
 * SSO login API — T08 / #9, ADR-0009 / ADR-0031 / ADR-0032.
 *
 * Two endpoints, two call sites:
 *
 * - `startSsoLogin()`    → `GET  /api/v1/auth/sso/login`
 *   The chat shell calls this when the user clicks 登录, then does a
 *   full-document navigation to the returned `authorization_url` so
 *   the browser hands the user off to the IdP. The backend has
 *   already minted the PKCE bundle + state + nonce; the client never
 *   sees them.
 *
 * - `completeSsoLogin()` → `POST /api/v1/auth/sso/callback`
 *   The `/auth/callback` page calls this with the `code` + `state`
 *   the IdP put on the URL, folds the response into the auth store,
 *   and navigates to `/chat`.
 *
 * The refresh path lives in `stores/auth.ts` (`refreshAccessToken`)
 * because every consumer of the store needs to mutate it, not just
 * the auth shell.
 */
import { ApiError, apiFetch } from '@/lib/api-client'
import type { AuthUser } from '@/stores/auth'

/**
 * Wire shape of `GET /api/v1/auth/sso/login` — the IdP authorization
 * URL plus the opaque `state` the backend echoes back to itself on
 * the callback (CSRF guard, RFC 6749 §10.12).
 */
export interface SsoLoginStartResponse {
  authorization_url: string
  state: string
}

/**
 * Wire shape of `POST /api/v1/auth/sso/callback`. Mirrors the
 * refresh endpoint so a single parser feeds both call sites
 * (ADR-0009 §"Refresh Token" — the spec calls for the same envelope
 * for token issuance and rotation). The auth store also consumes
 * the same shape from `/auth/refresh` (T08b / #49).
 */
export interface SsoLoginCompleteResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  user: AuthUser
}

/**
 * Begin the OIDC code+PKCE flow.
 *
 * The backend mints `state` + PKCE + nonce and builds the IdP
 * authorization URL with `response_type=code`, `code_challenge`, and
 * the configured `redirect_uri` already set. The client only has to
 * navigate to `authorization_url`.
 */
export async function startSsoLogin(): Promise<SsoLoginStartResponse> {
  return apiFetch<SsoLoginStartResponse>('/auth/sso/login')
}

/**
 * Finish the OIDC code+PKCE flow.
 *
 * Posts the `code` the IdP put on the redirect URL alongside the
 * `state` we returned at start-login. On success the backend has
 * verified the `id_token`, upserted the local `users` row, minted
 * the access JWT, and issued a fresh refresh token. The returned
 * bundle is what `useAuthStore.setTokens` consumes.
 */
export async function completeSsoLogin(
  code: string,
  state: string,
): Promise<SsoLoginCompleteResponse> {
  return apiFetch<SsoLoginCompleteResponse>('/auth/sso/callback', {
    method: 'POST',
    body: JSON.stringify({ code, state }),
  })
}

/**
 * Render an `ApiError` from the SSO surface as a Chinese sentence
 * the user can act on. `prefix` describes the call that failed
 * ("登录失败", "无法发起登录") so the same helper drives the
 * login + callback pages without each page re-deriving the error
 * envelope shape.
 *
 * Returns the backend's `message_zh` when present (ADR-0031
 * guarantees it on every error response); falls back to a generic
 * sentence for transport failures (no body to draw from).
 */
export function formatAuthApiError(err: unknown, prefix: string): string {
  if (err instanceof ApiError) {
    const body = err.body as { message_zh?: unknown } | null
    if (body && typeof body.message_zh === 'string') {
      return body.message_zh
    }
    return `${prefix} (HTTP ${err.status}), 请稍后重试。`
  }
  if (err instanceof TypeError) {
    return '无法连接后端服务, 请确认服务已启动。'
  }
  return '发生未知错误, 请重试。'
}