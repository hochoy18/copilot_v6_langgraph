import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import App from '@/App'
import { jsonResponse, makeAuthUser, mockFetch, renderWithRouter } from '@/test-utils'
import { useAuthStore } from '@/stores/auth'
import { usePlanDrawerStore } from '@/stores/plan-drawer'
import type { Plan, TurnResponse } from '@/types/plan'

/**
 * ConversationView ↔ drawer integration — T19 / #17 AC:
 * - [ ] 输入指令抽屉滑出
 *
 * The view lives at `/chat/:conversationId`; the conversation id
 * is taken from the URL (T11 / #41 wired it). T11's list view owns
 * the "new conversation" entry point — so this view assumes an
 * existing conversation id and never lazily creates one. `fetch`
 * is mocked with queued responses so the component drives its real
 * API contract (`POST /conversations/{id}/turns`).
 */

const plan: Plan = {
  id: 'p1',
  conversation_id: 'c1',
  turn_id: 'u1',
  status: 'pending',
  nodes: [
    {
      node_id: 'n1',
      tool: 'list_customers',
      parameters: { region: 'emea' },
      notes: '',
    },
  ],
  edges: [],
  tool_snapshots: [
    {
      tool_id: 't1',
      name: 'list_customers',
      description: '查询客户列表',
      risk_level: 'read',
      parameters_schema: {},
      http_method: 'GET',
      http_url_template: 'https://api.example.com/customers',
      http_headers: {},
      http_body_template: null,
    },
  ],
  edited_diff: null,
  created_at: '2026-09-28T07:00:00Z',
  updated_at: '2026-09-28T07:00:00Z',
}

function turnResponse(overrides: Partial<TurnResponse> = {}): TurnResponse {
  return {
    turn: {
      id: 'u1',
      conversation_id: 'c1',
      role: 'user',
      content: '查一下 EMEA 客户',
      plan_id: plan.id,
      created_at: '2026-09-28T07:00:00Z',
    },
    plan,
    warnings: [],
    ...overrides,
  }
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

function renderView(): ReturnType<typeof renderWithRouter> {
  // Render via the whole `<App />` so the outer `/chat/*` route
  // provides the parent path that ChatPage's inner `<Routes>`
  // resolves against — `useParams` needs the `:conversationId`
  // segment to bind.
  return renderWithRouter(<App />, { initialEntries: ['/chat/c1'] })
}

async function submitInstruction(text: string): Promise<void> {
  const user = userEvent.setup()
  await user.type(screen.getByPlaceholderText(/输入指令/), text)
  await user.click(screen.getByRole('button', { name: '发送' }))
}

describe('ConversationView', () => {
  it('submits a turn against the URL-bound conversation id', async () => {
    const fetchMock = mockFetch([jsonResponse(turnResponse(), 201)])
    renderView()
    await submitInstruction('查一下 EMEA 客户')

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/conversations/c1/turns')
    const turnBody = JSON.parse(
      (fetchMock.mock.calls[0][1] as RequestInit).body as string,
    )
    expect(turnBody.content).toBe('查一下 EMEA 客户')
  })

  it('slides the plan drawer out with the generated node (输入指令抽屉滑出)', async () => {
    mockFetch([jsonResponse(turnResponse(), 201)])
    renderView()
    await submitInstruction('查一下 EMEA 客户')

    const drawer = await screen.findByTestId('plan-drawer')
    expect(drawer).toHaveAttribute('data-mode', 'docked')
    expect(within(drawer).getByTestId('plan-node-n1')).toHaveTextContent('list_customers')
  })

  it('keeps the drawer collapsed and surfaces warnings when no Plan was produced', async () => {
    mockFetch([
      jsonResponse(
        turnResponse({ plan: null, warnings: ['未配置 LLM, 已跳过 Plan 生成。'] }),
        201,
      ),
    ])
    renderView()
    await submitInstruction('你好')

    expect(await screen.findByText(/未配置 LLM/)).toBeInTheDocument()
    expect(screen.getByTestId('plan-drawer')).toHaveAttribute('data-mode', 'collapsed')
  })

  it('offers a reopen control after the drawer is collapsed (可收起继续聊)', async () => {
    mockFetch([jsonResponse(turnResponse(), 201)])
    renderView()
    await submitInstruction('查一下 EMEA 客户')
    const drawer = await screen.findByTestId('plan-drawer')

    const user = userEvent.setup()
    await user.click(within(drawer).getByRole('button', { name: '收起' }))
    expect(drawer).toHaveAttribute('data-mode', 'collapsed')

    await user.click(screen.getByRole('button', { name: /查看 Plan/ }))
    expect(drawer).toHaveAttribute('data-mode', 'docked')
  })

  it('renders the user message in the transcript and clears the input', async () => {
    mockFetch([jsonResponse(turnResponse(), 201)])
    renderView()
    await submitInstruction('查一下 EMEA 客户')

    expect(await screen.findByText('查一下 EMEA 客户')).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/输入指令/)).toHaveValue('')
  })

  it('surfaces an API failure without touching the drawer', async () => {
    mockFetch([jsonResponse({ code: 'internal_error' }, 500)])
    renderView()
    await submitInstruction('查一下 EMEA 客户')

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByTestId('plan-drawer')).toHaveAttribute('data-mode', 'collapsed')
  })

  it('shows the authenticated user\'s display_name in the header (回调 /chat 显示用户名)', () => {
    useAuthStore.setState({
      accessToken: 'jwt',
      refreshToken: 'rt',
      expiresAt: Date.now() + 900_000,
      refreshFailed: false,
      user: makeAuthUser({
        id: 'u1',
        email: 'alice@example.com',
        display_name: 'Alice Liu',
      }),
    })
    renderView()
    expect(screen.getByTestId('chat-username')).toHaveTextContent('Alice Liu')
  })

  it('falls back to email when display_name is empty', () => {
    useAuthStore.setState({
      accessToken: 'jwt',
      refreshToken: 'rt',
      expiresAt: Date.now() + 900_000,
      refreshFailed: false,
      user: makeAuthUser({
        id: 'u1',
        email: 'alice@example.com',
        display_name: '',
      }),
    })
    renderView()
    expect(screen.getByTestId('chat-username')).toHaveTextContent('alice@example.com')
  })

  it('hides the username slot when no user is signed in', () => {
    renderView()
    expect(screen.queryByTestId('chat-username')).not.toBeInTheDocument()
  })
})
