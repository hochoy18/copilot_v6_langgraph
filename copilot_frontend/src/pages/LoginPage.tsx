/**
 * LoginPage — T08 / #9, ADR-0029 §路由与角色, ADR-0009 §SSO 登录路径.
 *
 * The whole page is a single button. Clicking it asks the backend
 * for an IdP authorization URL (with PKCE / state / nonce already
 * minted server-side), then does a full-document navigation so the
 * browser hands the user off to the IdP. The IdP sends the user
 * back to `/auth/callback` with `code` + `state` on the query
 * string; `AuthCallbackPage` does the second half of the dance.
 *
 * Why a full-document navigation (not `react-router` `navigate`)
 * -----------------------------------------------------------
 *
 * `window.location.assign(authorization_url)` is the standard OIDC
 * pattern: the user leaves the SPA's origin entirely (the IdP may
 * live on a different host). Router-pushing the URL would replace
 * the SPA's history entry with a non-SPA URL, breaking the back
 * button. `assign` is the right tool — it adds a new history entry
 * and lets the browser manage the cross-origin hop.
 */
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api-client'
import { startSsoLogin } from '@/lib/auth-api'

/**
 * Surface a network / API failure as a Chinese sentence the user
 * can act on. Lives here (not in `auth-api.ts`) because the only
 * caller that renders this string is this page; future admin /
 * programmatic callers can branch on `ApiError.status` directly.
 */
function formatStartError(err: unknown): string {
  if (err instanceof ApiError) {
    return `无法发起登录 (HTTP ${err.status}), 请稍后重试。`
  }
  if (err instanceof TypeError) {
    return '无法连接后端服务, 请确认服务已启动。'
  }
  return '发生未知错误, 请重试。'
}

export function LoginPage(): React.ReactElement {
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  /**
   * Fetch the IdP URL, then navigate. Splitting fetch and navigate
   * means a backend failure renders inline instead of leaving the
   * user on a blank page mid-redirect.
   */
  async function handleLogin(): Promise<void> {
    if (loading) return
    setLoading(true)
    setError(null)
    try {
      const { authorization_url } = await startSsoLogin()
      window.location.assign(authorization_url)
    } catch (err) {
      setError(formatStartError(err))
      setLoading(false)
    }
  }

  return (
    <main
      data-testid="login-page"
      className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-6 p-8"
    >
      <header className="space-y-2 text-center">
        <h1 className="text-2xl font-semibold">Copilot Chat</h1>
        <p className="text-sm text-muted-foreground">
          请使用企业账号登录后继续。
        </p>
      </header>
      <Button
        data-testid="login-button"
        onClick={() => {
          void handleLogin()
        }}
        disabled={loading}
        size="lg"
      >
        {loading ? '跳转中…' : '登录'}
      </Button>
      {error && (
        <p
          role="alert"
          className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-900"
        >
          {error}
        </p>
      )}
    </main>
  )
}