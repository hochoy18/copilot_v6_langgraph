/**
 * `useAuthRefresh` — T08 / #9, ADR-0009 §"Access Token" / AC §3-4.
 *
 * Drives the silent-rotation path. Two trigger windows:
 *
 * 1. **Rehydrate on mount** — the Refresh Token is persisted to
 *    `localStorage` (ADR-0032) but the Access Token is in-memory
 *    only, so a hard reload lands with `accessToken === null` and
 *    `refreshToken !== null`. The hook fires `refreshAccessToken`
 *    once in that gap; the call succeeds → `setTokens` populates
 *    `accessToken` + `expiresAt` → the timer branch below takes
 *    over; the call fails (revoked / replayed chain) →
 *    `clearTokens` runs → the user lands on the re-login UI instead
 *    of looking half-authenticated.
 *
 * 2. **Schedule on `expiresAt`** — once an Access Token is in
 *    memory, the hook sets a `setTimeout` for `(expiresAt - now) -
 *    REFRESH_LEAD_MS`, clamped at 0 so a token whose remaining
 *    lifetime is already inside the lead rotates on the next tick.
 *
 * Why not poll?
 * --------------
 *
 * A 2-minute polling tick would be cheap (the browser only fires the
 * timer when the tab is foregrounded) but it'd race the SSE hook's
 * own refresh path on `auth.expired` and double the round-trips on a
 * sleepy machine. A deadline-trimmed timer fires exactly when it's
 * needed — never sooner, never later.
 *
 * Why subtract the refresh margin from the delay?
 * ------------------------------------------------
 *
 * ADR-0009 calls out the "≤ 2 min" window explicitly. Naïvely
 * scheduling the refresh for `expiresAt - now` lets the next request
 * race the rotation. We schedule for
 * `(expiresAt - now) - REFRESH_LEAD_MS`, clamping to 0 so a token
 * that's already past the lead still rotates immediately.
 */
import { useEffect } from 'react'

import { refreshAccessToken, useAuthStore } from '@/stores/auth'

/**
 * Refresh the Access Token when ≤ this many milliseconds remain on
 * its lifetime. Matches the ADR-0009 / T08 AC: "Token 剩 ≤2 min
 * 自动续期".
 */
const REFRESH_LEAD_MS = 2 * 60 * 1000

/**
 * Mount once at the SPA root. The effect re-runs whenever the
 * store's access / refresh / expiry slots change, so the loop keeps
 * itself current without any global state.
 */
export function useAuthRefresh(): void {
  const accessToken = useAuthStore((s) => s.accessToken)
  const refreshToken = useAuthStore((s) => s.refreshToken)
  const expiresAt = useAuthStore((s) => s.expiresAt)

  useEffect(() => {
    // Rehydrate path — persisted Refresh Token with no in-memory
    // Access Token. A login completing after mount follows the
    // second branch on the next render because `setTokens` flips
    // both fields at once.
    if (accessToken === null && refreshToken !== null) {
      void refreshAccessToken()
      return
    }
    // No credentials at all — nothing to rotate.
    if (accessToken === null || expiresAt === null) return

    const delay = Math.max(0, expiresAt - Date.now() - REFRESH_LEAD_MS)

    const timer = setTimeout(() => {
      // The user logged out between scheduling and firing —
      // `refreshAccessToken` would early-return with `null` anyway,
      // but `clearTimeout` in the cleanup already guarantees the
      // callback is dead before `setTokens` flipped `accessToken`
      // back to `null`.
      void refreshAccessToken()
    }, delay)

    return () => {
      clearTimeout(timer)
    }
  }, [accessToken, refreshToken, expiresAt])
}