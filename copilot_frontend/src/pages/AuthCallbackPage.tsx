/**
 * AuthCallbackPage — T08 / #9, ADR-0009 §SSO 登录路径.
 *
 * The IdP redirects the browser here with `?code=&state=` on the
 * URL. The page POSTs both to `/api/v1/auth/sso/callback`, the
 * backend verifies the `id_token` + PKCE + state, and returns the
 * access + refresh bundle. We fold it into the auth store and
 * route the user to `/chat`.
 *
 * Why `replace` (not `push`) on the success navigate?
 * ---------------------------------------------------
 *
 * The IdP bounce appends `/auth/callback?code=…&state=…` to the
 * history. `replace` swaps that entry for `/chat` so a Back click
 * after landing on the chat doesn't drag the user back through the
 * IdP — they would re-render the callback, fail the
 * one-shot `code` exchange (RFC 6749 §10.5: IdPs reject code reuse),
 * and end up back on the login page.
 */
import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { formatAuthApiError, completeSsoLogin } from '@/lib/auth-api'
import { useAuthStore } from '@/stores/auth'

/** Where to land after the callback succeeds. Matches the AC's "回调 /chat 显示用户名". */
const POST_LOGIN_PATH = '/chat'

export function AuthCallbackPage(): React.ReactElement {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)

  /**
   * Read `code` + `state` off the URL once on mount. Reading inside
   * the effect (rather than at render time) avoids re-triggering the
   * exchange on a re-render — the callback is one-shot and the
   * backend rejects code reuse (RFC 6749 §10.5).
   */
  const code = searchParams.get('code')
  const state = searchParams.get('state')

  useEffect(() => {
    // The IdP is required by OIDC to send both `code` and `state`;
    // anything else is a misconfigured upstream or a hand-crafted URL
    // — bounce the user back to /auth/login rather than sit on a
    // blank page. We branch *inside* the effect (rather than at the
    // render boundary above) so the hook-order rule is preserved.
    if (code === null || state === null) {
      navigate('/auth/login', { replace: true })
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const result = await completeSsoLogin(code, state)
        if (cancelled) return
        useAuthStore.getState().setTokens(
          result.access_token,
          result.refresh_token,
          result.user,
          result.expires_in,
        )
        navigate(POST_LOGIN_PATH, { replace: true })
      } catch (err) {
        if (cancelled) return
        setError(formatAuthApiError(err, '登录失败'))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [code, state, navigate])

  if (error !== null) {
    return (
      <main
        data-testid="auth-callback-page"
        className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-6 p-8"
      >
        <h1 className="text-xl font-semibold">登录失败</h1>
        <p
          role="alert"
          className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-900"
        >
          {error}
        </p>
      </main>
    )
  }

  return (
    <main
      data-testid="auth-callback-page"
      className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-6 p-8"
    >
      <p className="text-sm text-muted-foreground">登录中…</p>
    </main>
  )
}