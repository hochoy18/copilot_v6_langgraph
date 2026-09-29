/**
 * Auth token store — T08 / #9 + T08b / #49, ADR-0032.
 *
 * Owns the in-memory Access Token + persisted Refresh Token + the
 * canonical `User` shape that the SSO callback and refresh responses
 * both carry (ADR-0009). The SSE hook reads the token off the store
 * on every reconnect; the `/chat` header reads the user for the
 * "显示用户名" acceptance line; the auto-refresh hook reads `expiresAt`
 * to decide when to rotate.
 *
 * Storage policy (ADR-0032):
 * - Access Token → in-memory only (refresh page = re-auth).
 * - Refresh Token → localStorage (7-day TTL, accepted XSS risk).
 * - User → in-memory only; `display_name`/`email` are the chat-shell
 *   identity. Not persisted: a page reload still has the refresh
 *   token, so a quick `/auth/refresh` rehydrates the user from the
 *   backend response (refresh responses carry the same `user` shape).
 *
 * Why a Zustand store (not React Context)
 * ---------------------------------------
 *
 * The SSE hook reads the token off the store on every reconnect; a
 * Context would force the hook to live inside a Provider tree,
 * coupling the socket lifecycle to the React tree. Zustand's
 * standalone store lets the hook live at any level — including
 * `src/hooks/useConversationStream.ts`.
 */
import { create } from 'zustand'

import { apiFetch, ApiError } from '@/lib/api-client'
import type { SsoLoginCompleteResponse } from '@/lib/auth-api'

/**
 * The subset of `User` the front-end needs to render the chat shell
 * (T08 AC: "回调 /chat 显示用户名") and decide where the user can go.
 *
 * Mirrors `app.db.schemas.User` (`UserBase + id + role_ids`); the
 * backend strips `password_hash` before serialising (`User.from_db`),
 * so the wire is safe to land in `localStorage` if a future ticket
 * wants persistence — for T08 we keep it memory-only.
 */
export interface AuthUser {
  id: string
  email: string
  display_name: string
  source: 'sso' | 'local'
  /** `local_username` for admins, `null` for SSO users (mirrors the backend). */
  username: string | null
  role_ids: string[]
}

/** Re-exported so consumer-side code keeps importing from one place. */
export type { SsoLoginCompleteResponse }

/**
 * Wall-clock instant (ms since epoch) when the Access Token stops
 * being trustworthy. Refresh window per ADR-0009: when the remaining
 * lifetime is ≤ 2 minutes, the auto-refresh hook (`useAuthRefresh`)
 * calls `/auth/refresh`. `null` when no token is held.
 */
const REFRESH_TOKEN_STORAGE_KEY = 'copilot.refresh_token'

interface AuthState {
  /** In-memory Access Token (ADR-0032). */
  accessToken: string | null
  /** Refresh Token persisted to localStorage (ADR-0032). */
  refreshToken: string | null
  /** Canonical user from the SSO callback / refresh response. */
  user: AuthUser | null
  /** Wall-clock ms when the Access Token expires (ADR-0009). */
  expiresAt: number | null
  /** Set on `/auth/refresh` failure — drives the re-login banner. */
  refreshFailed: boolean
  /**
   * Replace every credential in one shot — called by T08's SSO
   * callback handler and by `refreshAccessToken`. Computes
   * `expiresAt` from `Date.now() + expires_in * 1000` so the
   * auto-refresh hook never has to read time itself.
   */
  setTokens(
    accessToken: string,
    refreshToken: string,
    user: AuthUser,
    expiresIn: number,
  ): void
  /** Wipe every credential — drives the re-login redirect. */
  clearTokens(): void
  /** Mark the most recent refresh as failed (drives re-login). */
  markRefreshFailed(): void
}

/**
 * Resolve the browser's localStorage, or `null` outside a real
 * browser. The `in` check (not a property read) keeps Node's
 * experimental `localStorage` global from emitting warnings in the
 * vitest/jsdom environment, where jsdom hasn't installed it.
 */
function browserStorage(): Storage | null {
  if (typeof window === 'undefined') return null
  if (!('localStorage' in window)) return null
  return window.localStorage
}

/** Read the persisted Refresh Token, if any. Safe in SSR / non-browser. */
function readPersistedRefreshToken(): string | null {
  const storage = browserStorage()
  if (!storage) return null
  try {
    return storage.getItem(REFRESH_TOKEN_STORAGE_KEY)
  } catch {
    // localStorage can throw in private-mode sandboxes; treat as
    // "no refresh token" rather than crashing the store.
    return null
  }
}

/** Write the Refresh Token. Same swallowing semantics as the read. */
function writePersistedRefreshToken(value: string | null): void {
  const storage = browserStorage()
  if (!storage) return
  try {
    if (value === null) storage.removeItem(REFRESH_TOKEN_STORAGE_KEY)
    else storage.setItem(REFRESH_TOKEN_STORAGE_KEY, value)
  } catch {
    // best-effort: private-mode failures degrade to in-memory only
  }
}

export const useAuthStore = create<AuthState>((set) => ({
  accessToken: null,
  refreshToken: readPersistedRefreshToken(),
  user: null,
  expiresAt: null,
  refreshFailed: false,

  setTokens: (accessToken, refreshToken, user, expiresIn) => {
    writePersistedRefreshToken(refreshToken)
    set({
      accessToken,
      refreshToken,
      user,
      expiresAt: Date.now() + expiresIn * 1000,
      refreshFailed: false,
    })
  },

  clearTokens: () => {
    writePersistedRefreshToken(null)
    set({
      accessToken: null,
      refreshToken: null,
      user: null,
      expiresAt: null,
      refreshFailed: true,
    })
  },

  markRefreshFailed: () => set({ refreshFailed: true }),
}))

/**
 * Attempt a `/auth/refresh` round-trip and fold the new credentials
 * into the store. Returns the new token on success, `null` on
 * failure — the caller (the SSE hook) treats every `null` as
 * terminal for the current stream: a 401/404 means the refresh
 * chain is dead (revoked / replayed → backend burns the whole
 * family per T07, so a retry could never succeed), and a transient
 * 5xx / network blip can't be retried *here* either because the
 * now-past Access Token would just fail T23's on-connect `exp`
 * check. `refreshFailed` + the wiped token drive the re-login UX
 * instead (ADR-0009).
 */
export async function refreshAccessToken(): Promise<string | null> {
  const refresh = useAuthStore.getState().refreshToken
  if (!refresh) {
    useAuthStore.getState().markRefreshFailed()
    return null
  }
  try {
    // `apiFetch` sets `Content-Type: application/json` from the body.
    // `SsoLoginCompleteResponse` is shared with the SSO callback by
    // design — ADR-0009 calls for the same envelope on issuance
    // and rotation, so one parser feeds both call sites.
    const response = await apiFetch<SsoLoginCompleteResponse>('/auth/refresh', {
      method: 'POST',
      body: JSON.stringify({ refresh_token: refresh }),
    })
    useAuthStore.getState().setTokens(
      response.access_token,
      response.refresh_token,
      response.user,
      response.expires_in,
    )
    return response.access_token
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 404)) {
      useAuthStore.getState().clearTokens()
      return null
    }
    // Network / 5xx: keep the credentials (the chain may be fine),
    // flag the failure. The SSE hook stops for this session —
    // re-opening with the now-past token would only 401 at T23's
    // on-connect `exp` guard — so the banner guides a re-login.
    useAuthStore.getState().markRefreshFailed()
    return null
  }
}