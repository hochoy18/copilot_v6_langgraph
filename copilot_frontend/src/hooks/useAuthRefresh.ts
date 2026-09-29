/**
 * `useAuthRefresh` — T08 / #9, ADR-0009 §"Access Token" / AC §3-4.
 *
 * Drives the silent-rotation path: while the user is signed in, the
 * Access Token's lifetime is short (15 min). Whenever the remaining
 * window drops to ≤ 2 minutes we want a fresh JWT landing in the
 * store *before* any consumer notices — otherwise the SSE hook
 * (T24) hits the auth-loss branch on its next connect attempt and
 * the chat shell flashes a re-login banner.
 *
 * The hook is intentionally tiny: a `setTimeout` per `expiresAt`
 * change. Mounting it once at the SPA root (`App.tsx`) is enough.
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
 * Cap on the delay before refreshing: a token whose remaining
 * lifetime is *shorter* than the lead (e.g. a freshly-issued JWT with
 * a 30-second TTL in tests) should still rotate immediately, so we
 * clamp at 0. We use Math.max rather than a `?? 0` to keep the type
 * narrow.
 */
function clampRefreshDelay(ms: number): number {
  return Math.max(0, ms)
}

/**
 * Mount once at the SPA root. Schedules the next refresh, awaits
 * it, then schedules the next one off the fresh `expiresAt`. The
 * effect re-runs whenever `expiresAt` lands in the store (login,
 * refresh) or `accessToken` is wiped (logout / refresh failure),
 * so the loop keeps itself current without any global state.
 */
export function useAuthRefresh(): void {
  const accessToken = useAuthStore((s) => s.accessToken)
  const expiresAt = useAuthStore((s) => s.expiresAt)

  useEffect(() => {
    // The refresh-access-token seam (`stores/auth.ts`) handles both
    // the no-token branch and the failed-rotation branch — when
    // either wipes `accessToken`, this effect re-runs and the
    // `accessToken === null || expiresAt === null` guard short-
    // circuits below.
    if (accessToken === null || expiresAt === null) return

    const delay = clampRefreshDelay(expiresAt - Date.now() - REFRESH_LEAD_MS)

    let timer: ReturnType<typeof setTimeout> | null = null
    let cancelled = false

    timer = setTimeout(() => {
      // The user logged out between the timer being scheduled and
      // it firing — `refreshAccessToken` would early-return with
      // `null` anyway, but skipping the call saves a needless
      // round-trip and avoids the `refreshFailed` flip.
      if (cancelled) return
      void refreshAccessToken()
    }, delay)

    return () => {
      cancelled = true
      if (timer !== null) clearTimeout(timer)
    }
  }, [accessToken, expiresAt])
}