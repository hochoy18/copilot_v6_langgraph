import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { type ReactElement, type ReactNode } from 'react'

import { ToolsTable } from '@/components/admin/ToolsTable'
import type { Tool } from '@/types/tool'

/**
 * Tests for the Tool Registry table (T13 / #42).
 *
 * Acceptance criteria from the issue body:
 * - [ ] 表格渲染所有 Tool
 * - [ ] 状态过滤生效
 * - [ ] 风险等级过滤生效
 * - [ ] 名称搜索生效
 *
 * `mockFetch` arms `globalThis.fetch` with a sequence of queued
 * responses so the component exercises its real query-param
 * contract. Each test inspects the *last* fetch call to assert the
 * URL picked up the latest filter change.
 *
 * TanStack Query owns server state (ADR-0029), so the renderer
 * wraps the tree in a fresh `QueryClient` per test — `retry: 0`,
 * `staleTime: 0` to keep the test free of caching surprises.
 */

function makeTool(overrides: Partial<Tool> = {}): Tool {
  return {
    id: 'tool-1',
    name: 'list_orders',
    description: 'List recent orders for the current merchant.',
    risk_level: 'read',
    status: 'active',
    parameters_schema: {},
    http_method: 'GET',
    http_url_template: 'https://api.example.com/orders',
    http_headers: {},
    http_body_template: null,
    source: 'manual',
    source_ref: null,
    credentials_ref: null,
    created_at: '2026-09-28T07:00:00Z',
    updated_at: '2026-09-28T07:00:00Z',
    ...overrides,
  }
}

/**
 * Replace `globalThis.fetch` with a `vi.fn` whose `.mock` is populated
 * by the caller. Each response is consumed in order so the test can
 * drive "initial load → user changes filter → second load" sequences
 * without rebuilding the mock between phases.
 */
function mockFetch(responses: ReadonlyArray<unknown>): ReturnType<typeof vi.fn> {
  const fn = vi.fn()
  for (const body of responses) {
    fn.mockResolvedValueOnce(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  }
  globalThis.fetch = fn as unknown as typeof fetch
  return fn
}

function makeWrapper(): ({ children }: { children?: ReactNode }) => ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: 0, staleTime: 0 } },
  })
  return function Wrapper({ children }: { children?: ReactNode }): ReactElement {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

function renderTable(): ReturnType<typeof render> {
  return render(<ToolsTable />, { wrapper: makeWrapper() })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ToolsTable', () => {
  it('renders one row per Tool returned by the API', async () => {
    mockFetch([{ tools: [makeTool({ id: 't1' }), makeTool({ id: 't2' })] }])
    renderTable()
    const descriptions = await screen.findAllByText(/List recent orders/)
    expect(descriptions).toHaveLength(2)
  })

  it('issues an initial GET /admin/tools?limit=200 without filter params', async () => {
    const fetchMock = mockFetch([{ tools: [] }])
    renderTable()
    await screen.findByTestId('empty-state')
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/admin/tools?limit=200')
    expect((init?.method as string | undefined) ?? 'GET').toBe('GET')
  })

  it('renders a status filter that hits the API with ?status=', async () => {
    const fetchMock = mockFetch([
      // Initial load
      { tools: [makeTool({ id: 'init' })] },
      // After the user picks `draft`
      { tools: [makeTool({ id: 'd1', name: 'pending_tool', status: 'draft' })] },
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByText(/List recent orders/)
    await user.selectOptions(screen.getByLabelText(/状态/), 'draft')
    await waitFor(() => {
      const lastCall = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
      expect(lastCall[0]).toContain('status=draft')
    })
    expect(await screen.findByText('pending_tool')).toBeInTheDocument()
  })

  it('renders a risk_level filter that hits the API with ?risk_level=', async () => {
    const fetchMock = mockFetch([
      { tools: [makeTool({ id: 'init' })] },
      { tools: [makeTool({ id: 'd1', name: 'purge_account', risk_level: 'destructive' })] },
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByText(/List recent orders/)
    await user.selectOptions(screen.getByLabelText(/风险等级/), 'destructive')
    await waitFor(() => {
      const lastCall = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
      expect(lastCall[0]).toContain('risk_level=destructive')
    })
    expect(await screen.findByText('purge_account')).toBeInTheDocument()
  })

  it('renders a name search box that hits the API with ?q=', async () => {
    const fetchMock = mockFetch([
      { tools: [makeTool({ id: 'init' })] },
      { tools: [makeTool({ id: 'm1', name: 'report_revenue' })] },
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByText(/List recent orders/)
    await user.type(screen.getByPlaceholderText(/按名称搜索/), 'report')
    await waitFor(() => {
      const lastCall = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
      expect(lastCall[0]).toContain('q=report')
    })
    expect(await screen.findByText('report_revenue')).toBeInTheDocument()
  })

  it('does not send empty / whitespace-only q in the URL', async () => {
    const fetchMock = mockFetch([
      { tools: [makeTool({ id: 'init' })] },
      { tools: [makeTool({ id: 'm1' })] },
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByText(/List recent orders/)
    await user.type(screen.getByPlaceholderText(/按名称搜索/), '   ')
    await waitFor(() => {
      const lastCall = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
      // `q=` must not appear; `limit=200` is fine.
      expect(lastCall[0]).not.toMatch(/[?&]q=/)
    })
  })

  it('shows an empty-state message when the API returns no rows', async () => {
    mockFetch([{ tools: [] }])
    renderTable()
    expect(await screen.findByTestId('empty-state')).toBeInTheDocument()
    expect(screen.getByText(/暂无 Tool/)).toBeInTheDocument()
  })

  it('renders status and risk_level as readable badges', async () => {
    mockFetch([
      { tools: [makeTool({ id: 'a1', status: 'active', risk_level: 'destructive' })] },
    ])
    renderTable()
    const row = await screen.findByTestId('tool-row-a1')
    expect(within(row).getByText('active')).toBeInTheDocument()
    expect(within(row).getByText('destructive')).toBeInTheDocument()
  })

  it('surfaces an API error to the user', async () => {
    globalThis.fetch = vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify({ code: 'internal_error' }), { status: 500 }),
    ) as unknown as typeof fetch
    renderTable()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/500/)).toBeInTheDocument()
  })

  it('retry button refetches when the initial load failed', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ code: 'internal_error' }), { status: 500 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ tools: [makeTool({ id: 'r1' })] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const user = userEvent.setup()
    renderTable()
    const alert = await screen.findByRole('alert')
    expect(alert).toBeInTheDocument()
    expect(fetchMock.mock.calls).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2))
    expect(await screen.findByText(/List recent orders/)).toBeInTheDocument()
  })

  it('debounces the search box so a burst of keystrokes fires one fetch', async () => {
    const fetchMock = mockFetch([
      { tools: [makeTool({ id: 'init' })] },
      { tools: [makeTool({ id: 'm1', name: 'report_revenue' })] },
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByText(/List recent orders/)
    await user.type(screen.getByPlaceholderText(/按名称搜索/), 'report')
    await waitFor(() => {
      const lastCall = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
      expect(lastCall[0]).toContain('q=report')
    })
    // The initial mount + one debounced fetch = exactly two calls.
    expect(fetchMock.mock.calls).toHaveLength(2)
  })
})