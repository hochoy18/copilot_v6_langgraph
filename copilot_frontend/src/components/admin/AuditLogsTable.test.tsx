import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { type ReactElement, type ReactNode } from 'react'

import { AuditLogsTable } from '@/components/admin/AuditLogsTable'
import type { AuditLog, AuditLogLifecycle } from '@/types/audit-log'

/**
 * Tests for the admin Audit Log table (T43 / #38).
 *
 * Acceptance criteria from the issue body:
 * - [ ] admin 过滤生效
 * - [ ] 点行展开详情
 * - [ ] 调档按钮可触发
 * - [ ] cursor 分页
 *
 * `mockFetch` arms `globalThis.fetch` with a sequence of queued
 * responses so each test exercises the real query-param contract
 * and the cursor-pagination handshake. The pattern mirrors
 * `ToolsTable.test.tsx` so the admin-suite stays uniform.
 *
 * TanStack Query owns server state (ADR-0029). The renderer wraps
 * the tree in a fresh `QueryClient` per test — `retry: 0` and
 * `staleTime: 0` keep the test free of caching surprises.
 */

function makeLog(overrides: Partial<AuditLog> = {}): AuditLog {
  return {
    id: 'log-1',
    actor_id: 'user-1',
    conversation_id: 'conv-1',
    turn_id: 'turn-1',
    plan_id: 'plan-1',
    plan_execution_id: 'plan-exec-1',
    tool_name: 'list_orders',
    tool_snapshot: {
      tool_id: 'tool-1',
      name: 'list_orders',
      description: 'List recent orders for the current merchant.',
      risk_level: 'read',
      parameters_schema: { type: 'object', properties: {} },
      http_method: 'GET',
      http_url_template: 'https://api.example.com/orders',
      http_headers: {},
      http_body_template: null,
    },
    parameters: { limit: 10 },
    response: { orders: [{ id: 'o-1' }] },
    status: 'succeeded',
    error: null,
    risk_level: 'read',
    retry_count: 0,
    occurred_at: '2026-09-28T07:00:00Z',
    lifecycle_status: 'active',
    cold_storage_ref: null,
    cold_archived_at: null,
    ...overrides,
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/**
 * Stub `globalThis.fetch` with a `vi.fn()` whose `.mock` is
 * populated by the caller. Each response is consumed in order so
 * the test can drive "initial load → user changes filter → second
 * load" sequences without rebuilding the mock between phases.
 */
function mockFetch(responses: ReadonlyArray<Response>): ReturnType<typeof vi.fn> {
  const fn = vi.fn()
  for (const response of responses) {
    fn.mockResolvedValueOnce(response)
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
  return render(<AuditLogsTable />, { wrapper: makeWrapper() })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('AuditLogsTable', () => {
  it('renders one row per audit log returned by the API', async () => {
    mockFetch([
      jsonResponse({
        logs: [makeLog({ id: 'a1' }), makeLog({ id: 'a2' })],
        next_cursor: null,
        has_more: false,
      }),
    ])
    renderTable()
    expect(await screen.findByTestId('audit-log-row-a1')).toBeInTheDocument()
    expect(screen.getByTestId('audit-log-row-a2')).toBeInTheDocument()
  })

  it('issues the initial GET /admin/audit-logs without filter params', async () => {
    const fetchMock = mockFetch([
      jsonResponse({ logs: [], next_cursor: null, has_more: false }),
    ])
    renderTable()
    await screen.findByTestId('empty-state')
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/admin/audit-logs?limit=50')
    expect((init?.method as string | undefined) ?? 'GET').toBe('GET')
  })

  it('sends the lifecycle filter to the API as ?lifecycle_status=', async () => {
    const fetchMock = mockFetch([
      jsonResponse({ logs: [], next_cursor: null, has_more: false }),
      jsonResponse({
        logs: [makeLog({ id: 'arc-1', lifecycle_status: 'archived' })],
        next_cursor: null,
        has_more: false,
      }),
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByTestId('empty-state')
    await user.selectOptions(screen.getByLabelText(/生命周期/), 'archived')
    await waitFor(() => {
      const lastCall = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
      expect(lastCall[0]).toContain('lifecycle_status=archived')
    })
    expect(await screen.findByTestId('audit-log-row-arc-1')).toBeInTheDocument()
  })

  it('sends the tool_name filter to the API as ?tool_name=', async () => {
    const fetchMock = mockFetch([
      jsonResponse({ logs: [], next_cursor: null, has_more: false }),
      jsonResponse({
        logs: [makeLog({ id: 't1', tool_name: 'purge_account' })],
        next_cursor: null,
        has_more: false,
      }),
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByTestId('empty-state')
    await user.type(screen.getByLabelText(/Tool 名称/), 'purge')
    await waitFor(() => {
      const lastCall = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
      expect(lastCall[0]).toContain('tool_name=purge')
    })
    expect(await screen.findByTestId('audit-log-row-t1')).toBeInTheDocument()
  })

  it('sends the actor_id filter to the API as ?actor_id=', async () => {
    const fetchMock = mockFetch([
      jsonResponse({ logs: [], next_cursor: null, has_more: false }),
      jsonResponse({
        logs: [makeLog({ id: 'a1', actor_id: 'user-42' })],
        next_cursor: null,
        has_more: false,
      }),
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByTestId('empty-state')
    await user.type(screen.getByLabelText(/actor_id/), 'user-42')
    await waitFor(() => {
      const lastCall = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
      expect(lastCall[0]).toContain('actor_id=user-42')
    })
  })

  it('sends the time-range filter as ?time_from= and ?time_to=', async () => {
    const fetchMock = mockFetch([
      jsonResponse({ logs: [], next_cursor: null, has_more: false }),
      jsonResponse({
        logs: [makeLog({ id: 't1' })],
        next_cursor: null,
        has_more: false,
      }),
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByTestId('empty-state')
    await user.type(screen.getByLabelText(/起始时间/), '2026-09-01T00:00')
    await user.type(screen.getByLabelText(/结束时间/), '2026-09-30T00:00')
    await waitFor(() => {
      const lastCall = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
      expect(lastCall[0]).toContain('time_from=2026-09-01T00%3A00')
      expect(lastCall[0]).toContain('time_to=2026-09-30T00%3A00')
    })
  })

  it('does not send empty / whitespace-only filter values', async () => {
    const fetchMock = mockFetch([
      jsonResponse({ logs: [], next_cursor: null, has_more: false }),
      jsonResponse({
        logs: [makeLog({ id: 'init' })],
        next_cursor: null,
        has_more: false,
      }),
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByTestId('empty-state')
    await user.type(screen.getByLabelText(/Tool 名称/), '   ')
    await waitFor(() => {
      const lastCall = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
      expect(lastCall[0]).not.toMatch(/[?&]tool_name=/)
    })
  })

  it('expands a row to show the detail panel on click', async () => {
    mockFetch([
      jsonResponse({
        logs: [makeLog({ id: 'exp-1', parameters: { foo: 'bar' } })],
        next_cursor: null,
        has_more: false,
      }),
    ])
    const user = userEvent.setup()
    renderTable()
    const row = await screen.findByTestId('audit-log-row-exp-1')
    await user.click(screen.getByTestId('audit-log-toggle-exp-1'))
    expect(
      await screen.findByTestId('audit-log-detail-exp-1'),
    ).toBeInTheDocument()
    expect(within(row).getByLabelText('收起详情')).toBeInTheDocument()
  })

  it('renders the row-detail payload (parameters / response / tool_snapshot) as JSON', async () => {
    mockFetch([
      jsonResponse({
        logs: [
          makeLog({
            id: 'json-1',
            parameters: { page: 2 },
            response: { total: 1 },
          }),
        ],
        next_cursor: null,
        has_more: false,
      }),
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByTestId('audit-log-row-json-1')
    await user.click(screen.getByTestId('audit-log-toggle-json-1'))
    const detail = await screen.findByTestId('audit-log-detail-json-1')
    expect(within(detail).getByTestId('parameters-json-1').textContent).toContain(
      '"page": 2',
    )
    expect(within(detail).getByTestId('response-json-1').textContent).toContain(
      '"total": 1',
    )
    expect(
      within(detail).getByTestId('tool-snapshot-json-1').textContent,
    ).toContain('list_orders')
  })

  it('renders the 调档 button only on archived rows', async () => {
    mockFetch([
      jsonResponse({
        logs: [
          makeLog({ id: 'arc', lifecycle_status: 'archived' as AuditLogLifecycle }),
          makeLog({ id: 'live', lifecycle_status: 'active' as AuditLogLifecycle }),
        ],
        next_cursor: null,
        has_more: false,
      }),
    ])
    renderTable()
    await screen.findByTestId('audit-log-row-arc')
    expect(screen.getByTestId('recall-arc')).toBeInTheDocument()
    expect(screen.queryByTestId('recall-live')).not.toBeInTheDocument()
  })

  it('triggers POST /admin/audit-logs/{id}/recall on 调档 click', async () => {
    mockFetch([
      jsonResponse({
        logs: [
          makeLog({ id: 'arc', lifecycle_status: 'archived' as AuditLogLifecycle }),
        ],
        next_cursor: null,
        has_more: false,
      }),
      jsonResponse(
        makeLog({ id: 'arc', lifecycle_status: 'recalled' as AuditLogLifecycle }),
      ),
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByTestId('audit-log-row-arc')
    await user.click(screen.getByTestId('recall-arc'))
    await waitFor(() => {
      const calls = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock
        .calls
      const recallCall = calls.find(([url]) =>
        String(url).includes('/admin/audit-logs/arc/recall'),
      )
      expect(recallCall).toBeDefined()
      const [, init] = recallCall!
      expect((init?.method as string | undefined) ?? 'GET').toBe('POST')
    })
  })

  it('updates the row lifecycle_status to recalled after a successful recall', async () => {
    mockFetch([
      jsonResponse({
        logs: [
          makeLog({ id: 'arc', lifecycle_status: 'archived' as AuditLogLifecycle }),
        ],
        next_cursor: null,
        has_more: false,
      }),
      jsonResponse(
        makeLog({ id: 'arc', lifecycle_status: 'recalled' as AuditLogLifecycle }),
      ),
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByTestId('audit-log-row-arc')
    await user.click(screen.getByTestId('recall-arc'))
    await waitFor(() => {
      const badge = screen.getByTestId('lifecycle-badge-arc')
      expect(badge.textContent).toBe('recalled')
    })
  })

  it('shows a recall error banner when the recall API fails', async () => {
    mockFetch([
      jsonResponse({
        logs: [
          makeLog({ id: 'arc', lifecycle_status: 'archived' as AuditLogLifecycle }),
        ],
        next_cursor: null,
        has_more: false,
      }),
      new Response(JSON.stringify({ code: 'internal_error' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      }),
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByTestId('audit-log-row-arc')
    await user.click(screen.getByTestId('recall-arc'))
    expect(await screen.findByTestId('recall-error')).toBeInTheDocument()
  })

  it('renders a Load more button when the API returns has_more=true', async () => {
    mockFetch([
      jsonResponse({
        logs: [makeLog({ id: 'p1' })],
        next_cursor: 'cursor-2',
        has_more: true,
      }),
    ])
    renderTable()
    await screen.findByTestId('audit-log-row-p1')
    expect(screen.getByTestId('load-more')).toBeInTheDocument()
  })

  it('sends the next page request with the cursor returned by the API', async () => {
    mockFetch([
      jsonResponse({
        logs: [makeLog({ id: 'p1' })],
        next_cursor: 'cursor-2',
        has_more: true,
      }),
      jsonResponse({
        logs: [makeLog({ id: 'p2' })],
        next_cursor: null,
        has_more: false,
      }),
    ])
    const user = userEvent.setup()
    renderTable()
    await screen.findByTestId('audit-log-row-p1')
    await user.click(screen.getByTestId('load-more'))
    await waitFor(() => {
      const calls = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock
        .calls
      const secondCall = calls[1]
      expect(secondCall[0]).toContain('cursor=cursor-2')
    })
    expect(await screen.findByTestId('audit-log-row-p2')).toBeInTheDocument()
    expect(screen.getByTestId('end-of-list')).toBeInTheDocument()
  })

  it('shows an empty-state message when the API returns no rows', async () => {
    mockFetch([jsonResponse({ logs: [], next_cursor: null, has_more: false })])
    renderTable()
    expect(await screen.findByTestId('empty-state')).toBeInTheDocument()
    expect(screen.getByText(/暂无审计日志/)).toBeInTheDocument()
  })

  it('surfaces an API error to the user with a retry button', async () => {
    globalThis.fetch = vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify({ code: 'internal_error' }), { status: 500 }),
    ) as unknown as typeof fetch
    renderTable()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/500/)).toBeInTheDocument()
  })
})
