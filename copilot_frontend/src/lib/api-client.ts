/**
 * Minimal fetch wrapper for the admin Tool Registry — T13 / #42.
 *
 * Centralises the `/api/v1` prefix and JSON content-type so callers
 * don't repeat boilerplate. Lives next to `tools-api.ts`; future
 * admin endpoints (audit, users) will sit alongside it.
 *
 * The wrapper deliberately does not own auth: T09 / #10 puts the
 * `Authorization` header on the underlying fetch. When the admin auth
 * ticket (T09 follow-ups) lands, the bearer-injection belongs here so
 * every admin endpoint picks it up.
 */

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `API request failed with status ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

/**
 * Issue a JSON request to `/api/v1/*` and parse the response.
 *
 * Throws `ApiError` on non-2xx so React components can render a
 * single error path. A bare `fetch` failure (network down) propagates
 * as the native `TypeError` — distinguishable by `err.name`.
 */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const url = path.startsWith('/api/') ? path : `/api/v1${path}`
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(url, { ...init, headers })
  const text = await response.text()
  const body: unknown = text ? safeJsonParse(text) : null
  if (!response.ok) {
    throw new ApiError(response.status, body)
  }
  return body as T
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}