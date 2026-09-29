import { type ReactElement, type ReactNode } from 'react'
import { render, type RenderOptions } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { type Mock, vi } from 'vitest'

import type { AuthUser } from '@/stores/auth'
import { useAuthStore } from '@/stores/auth'

/**
 * Test renderer that wraps the tree in a fresh `QueryClient` +
 * `MemoryRouter`. Mirrors the production app providers in `main.tsx`
 * so components using `useQuery` / `useMutation` (T13 Tool
 * Registry, T11 Conversation List) find a client on mount.
 *
 * Each call gets a fresh `QueryClient` (no cache sharing across
 * tests) and `retry: 0` so a failing assertion isn't masked by
 * React Query's retry loop. Defaults to `/` which — per the App
 * router — redirects to /chat.
 */
export function renderWithRouter(
  ui: ReactElement,
  { initialEntries = ['/'], ...options }: RenderOptions & { initialEntries?: string[] } = {},
): ReturnType<typeof render> {
  function Wrapper({ children }: { children?: ReactNode }): ReactElement {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: 0 } },
    })
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={initialEntries}>{children}</MemoryRouter>
      </QueryClientProvider>
    )
  }
  return render(ui, { wrapper: Wrapper, ...options })
}

/**
 * Build a `Response` whose `Content-Type: application/json` matches
 * what `apiFetch` parses. Lives in test-utils so every auth /
 * conversation / tool test uses the same shape.
 */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/**
 * Stub `globalThis.fetch` with a `vi.fn()` that resolves to the
 * supplied responses in order. Returns the mock so the caller can
 * assert on `mock.calls`.
 */
export function mockFetch(responses: ReadonlyArray<Response>): Mock {
  const fn = vi.fn()
  for (const response of responses) {
    fn.mockResolvedValueOnce(response)
  }
  globalThis.fetch = fn as unknown as typeof fetch
  return fn
}

/**
 * Build a minimal `AuthUser` for tests. Defaults mirror the SSO
 * shape; `overrides` lets a specific test tweak one field without
 * restating the whole literal.
 */
export function makeAuthUser(overrides: Partial<AuthUser> = {}): AuthUser {
  return {
    id: 'u-test',
    email: 'user@example.com',
    display_name: 'Test User',
    source: 'sso',
    username: null,
    role_ids: [],
    ...overrides,
  }
}

/**
 * Reset the auth store to a known-empty baseline. Call this in
 * `beforeEach` so a previous test's tokens / user don't leak into
 * the next one.
 */
export function resetAuthStore(): void {
  useAuthStore.setState({
    accessToken: null,
    refreshToken: null,
    user: null,
    expiresAt: null,
    refreshFailed: false,
  })
}