import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import App from '@/App'
import { jsonResponse, mockFetch, renderWithRouter } from '@/test-utils'
import { useAuthStore } from '@/stores/auth'
import type { ConversationResponse, ConversationStatus } from '@/lib/conversations-api'

/**
 * ConversationList — T11 / #41 acceptance tests.
 *
 * Pinning the four ACs from #41 plus the supporting plumbing
 * (loading state, error retry, URL navigation):
 * - [ ] 三 Tab 切换正常
 * - [ ] 新建会话出现 active
 * - [ ] 归档会话出现 idle
 * - [ ] 点进会话详情
 *
 * Renders the whole `<App />` (not just `<ChatPage />`) so the
 * outer `/chat/*` route in App.tsx provides the parent path that
 * ChatPage's inner `<Routes>` resolves against. Bare `<ChatPage />`
 * mounted at `/chat` would match the `:conversationId` route with
 * `conversationId = "chat"` — that's not what the test wants.
 */

function makeConversation(overrides: Partial<ConversationResponse> = {}): ConversationResponse {
  return {
    id: 'c1',
    user_id: 'usr1',
    title: '查客户',
    status: 'active',
    last_activity_at: '2026-09-28T07:00:00Z',
    created_at: '2026-09-28T07:00:00Z',
    updated_at: '2026-09-28T07:00:00Z',
    ...overrides,
  }
}

beforeEach(() => {
  useAuthStore.setState({
    accessToken: null,
    refreshToken: null,
    user: null,
    expiresAt: null,
    refreshFailed: false,
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function renderList(): ReturnType<typeof renderWithRouter> {
  return renderWithRouter(<App />, { initialEntries: ['/chat'] })
}

describe('ConversationList', () => {
  it('renders the three status tabs with active selected by default', async () => {
    mockFetch([jsonResponse({ conversations: [makeConversation()] })])
    renderList()

    const tablist = await screen.findByRole('tablist', { name: '会话状态' })
    const tabs = within(tablist).getAllByRole('tab')
    expect(tabs).toHaveLength(3)
    expect(tabs.map((t) => t.textContent)).toEqual(['活跃', '空闲', '归档'])
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true')
  })

  it('fetches the active tab on mount and renders each row (三 Tab 切换正常)', async () => {
    mockFetch([jsonResponse({ conversations: [makeConversation()] })])
    renderList()

    expect(await screen.findByTestId('conversation-row-c1')).toBeInTheDocument()
    expect(screen.getByTestId('tab-active')).toHaveAttribute('aria-selected', 'true')
  })

  it('switches tabs by re-querying with the new status', async () => {
    const idleConversation = makeConversation({
      id: 'c2',
      status: 'idle',
      title: '昨天的会话',
    })
    const fetchMock = mockFetch([
      jsonResponse({ conversations: [makeConversation()] }),
      jsonResponse({ conversations: [idleConversation] }),
    ])
    renderList()

    await screen.findByTestId('conversation-row-c1')
    await userEvent.setup().click(screen.getByTestId('tab-idle'))

    await screen.findByTestId('conversation-row-c2')
    expect(screen.getByTestId('tab-idle')).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByTestId('conversation-row-c1')).not.toBeInTheDocument()
    expect(fetchMock.mock.calls[1][0]).toBe(
      '/api/v1/conversations?status=idle',
    )
  })

  it('shows the empty state copy for a tab with no rows', async () => {
    mockFetch([jsonResponse({ conversations: [] })])
    renderList()

    expect(await screen.findByTestId('empty-state')).toHaveTextContent('暂无活跃会话')
  })

  it('creates a new conversation and navigates to /chat/:id (新建会话出现 active)', async () => {
    const created = makeConversation({ id: 'c-new', title: '' })
    const fetchMock = mockFetch([
      jsonResponse({ conversations: [] }),
      jsonResponse(created, 201),
    ])
    renderList()

    await screen.findByTestId('conversation-list')
    await userEvent.setup().click(screen.getByTestId('new-conversation'))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    // First call: initial active-tab list fetch (returns empty).
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/conversations?status=active')
    // Second call: POST /conversations (the create).
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/conversations')
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe('POST')
    // Navigation lands on the view at /chat/:id.
    expect(await screen.findByTestId('conversation-view')).toBeInTheDocument()
  })

  it('navigates to the detail page when a row is clicked (点进会话详情)', async () => {
    mockFetch([jsonResponse({ conversations: [makeConversation({ id: 'c-row' })] })])
    renderList()

    const row = await screen.findByTestId('open-c-row')
    await userEvent.setup().click(row)

    expect(await screen.findByTestId('conversation-view')).toBeInTheDocument()
  })

  it('archives an active conversation and removes the row optimistically (归档会话出现 idle)', async () => {
    const active = makeConversation({ id: 'c-active', title: '活跃会话' })
    const archived = { ...active, status: 'idle' as ConversationStatus }
    const fetchMock = mockFetch([
      jsonResponse({ conversations: [active] }),
      jsonResponse(archived),
    ])
    renderList()

    expect(await screen.findByTestId('conversation-row-c-active')).toBeInTheDocument()
    await userEvent.setup().click(screen.getByTestId('archive-c-active'))

    // Optimistic remove: the row leaves the active tab without
    // waiting for the refetch to settle.
    await waitFor(() =>
      expect(screen.queryByTestId('conversation-row-c-active')).not.toBeInTheDocument(),
    )
    // The archive POST went out.
    const archiveCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        url === '/api/v1/conversations/c-active/archive' &&
        (init as RequestInit | undefined)?.method === 'POST',
    )
    expect(archiveCall).toBeDefined()
  })

  it('does not show the archive control on archived rows', async () => {
    const archived = makeConversation({
      id: 'c-old',
      status: 'archived',
      title: '旧会话',
    })
    const fetchMock = mockFetch([
      jsonResponse({ conversations: [] }), // initial active tab (empty)
      jsonResponse({ conversations: [archived] }), // archived tab after switch
    ])
    renderList()

    await userEvent.setup().click(screen.getByTestId('tab-archived'))
    expect(await screen.findByTestId('conversation-row-c-old')).toBeInTheDocument()
    expect(screen.queryByTestId('archive-c-old')).not.toBeInTheDocument()
    expect(fetchMock.mock.calls[1][0]).toBe(
      '/api/v1/conversations?status=archived',
    )
  })

  it('renders a loading row while the initial fetch is pending', () => {
    mockFetch([])
    renderList()
    expect(screen.getByTestId('conversations-loading')).toBeInTheDocument()
  })

  it('surfaces an API error with a retry control', async () => {
    mockFetch([jsonResponse({ code: 'internal_error' }, 500)])
    renderList()

    const alert = await screen.findByTestId('conversations-error')
    expect(alert).toHaveTextContent('HTTP 500')
    expect(within(alert).getByRole('button', { name: /重试/ })).toBeInTheDocument()
  })
})
