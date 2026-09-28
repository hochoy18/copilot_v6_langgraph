/**
 * Auth token store — T24 / #21 (preliminary seam), ADR-0032.
 *
 * T08 / #9 (OIDC frontend flow) hasn't landed yet, so production
 * values here start null and the refresh helper is a thin POST to
 * the already-shipped T08b backend (`/auth/refresh`, #49). The
 * contract is what T24 needs — an in-memory Access Token the SSE
 * hook subscribes to, plus a `refreshAccessToken` the hook awaits
 * on `auth.expired`. T08 fills in the `setTokens` call after the
 * OIDC callback without touching the consumer-side hook.
 *
 * Storage policy (ADR-0032):
 * - Access Token → in-memory only (refresh page = re-auth).
 * - Refresh Token → localStorage (7-day TTL, accepted XSS risk).
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

/**
 * Wire shape of `POST /api/v1/auth/refresh` (T08b / #49).
 *
 * Mirrors backend `LoginCompleteResult`: same envelope as the
 * login response so the Frontend uses one parser for both. T08
 * will move this into a shared `lib/auth-api.ts` once that ticket
 * lands; until then T24 owns the seam.
 */
interface RefreshResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

interface AuthState {
  /** In-memory Access Token (ADR-0032). */
  accessToken: string | null
  /** Refresh Token persisted to localStorage (ADR-0032). */
  refreshToken: string | null
  /** Set on `/auth/refresh` failure — drives the re-login banner. */
  refreshFailed: boolean
  /** Replace both tokens (called by T08 login + `refreshAccessToken`). */
  setTokens(accessToken: string, refreshToken: string): void
  /** Wipe every credential — drives the re-login redirect. */
  clearTokens(): void
  /** Mark the most recent refresh as failed (drives re-login). */
  markRefreshFailed(): void
}

const REFRESH_TOKEN_STORAGE_KEY = 'copilot.refresh_token'

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
  refreshFailed: false,

  setTokens: (accessToken, refreshToken) => {
    writePersistedRefreshToken(refreshToken)
    set({ accessToken, refreshToken, refreshFailed: false })
  },

  clearTokens: () => {
    writePersistedRefreshToken(null)
    set({ accessToken: null, refreshToken: null, refreshFailed: true })
  },

  markRefreshFailed: () => set({ refreshFailed: true }),
}))

/**
 * Attempt a `/auth/refresh` round-trip and fold the new Access Token
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
    const response = await apiFetch<RefreshResponse>('/auth/refresh', {
      method: 'POST',
      body: JSON.stringify({ refresh_token: refresh }),
    })
    useAuthStore.getState().setTokens(
      response.access_token,
      response.refresh_token,
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
