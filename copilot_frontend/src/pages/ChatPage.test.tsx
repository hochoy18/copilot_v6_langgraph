import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen } from '@testing-library/react'

import App from '@/App'
import { jsonResponse, mockFetch, renderWithRouter } from '@/test-utils'
import { useAuthStore } from '@/stores/auth'
import { usePlanDrawerStore } from '@/stores/plan-drawer'

/**
 * ChatPage routing — T11 / #41.
 *
 * ChatPage is a thin router that picks between the list view
 * (`/chat`) and the single-conversation view (`/chat/:id`). These
 * tests pin the route split; the views themselves have their own
 * test files (`ConversationList.test.tsx`, `ConversationView.test.tsx`).
 *
 * Renders `<App />` so the outer `/chat/*` route supplies the
 * parent path ChatPage's inner `<Routes>` resolves against.
 */

const activeConversation = {
  id: 'c1',
  user_id: 'usr1',
  title: '查客户',
  status: 'active',
  last_activity_at: '2026-09-28T07:00:00Z',
  created_at: '2026-09-28T07:00:00Z',
  updated_at: '2026-09-28T07:00:00Z',
}

beforeEach(() => {
  usePlanDrawerStore.setState({ plan: null, mode: 'collapsed', selectedNodeId: null })
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

describe('ChatPage routing', () => {
  it('renders the conversation list at /chat (no id)', async () => {
    mockFetch([jsonResponse({ conversations: [activeConversation] })])
    renderWithRouter(<App />, { initialEntries: ['/chat'] })

    expect(await screen.findByTestId('conversation-list')).toBeInTheDocument()
    expect(screen.queryByTestId('conversation-view')).not.toBeInTheDocument()
  })

  it('renders the single-conversation view at /chat/:id', async () => {
    renderWithRouter(<App />, { initialEntries: ['/chat/c1'] })

    expect(await screen.findByTestId('conversation-view')).toBeInTheDocument()
    expect(screen.queryByTestId('conversation-list')).not.toBeInTheDocument()
    expect(screen.getByTestId('back-to-list')).toHaveAttribute('href', '/chat')
  })
})
