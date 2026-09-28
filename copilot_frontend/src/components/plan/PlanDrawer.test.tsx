import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { act } from 'react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PlanDrawer } from '@/components/plan/PlanDrawer'
import { usePlanDrawerStore } from '@/stores/plan-drawer'
import type { Plan, PlanNode, ToolSnapshot } from '@/types/plan'

/**
 * Drawer UI tests — T19 / #17 + T20 / #43 acceptance criteria:
 * - [ ] 显示单节点 + 节点信息
 * - [ ] 可收起 / 全屏
 * - [ ] 节点按风险等级区分样式
 * - [ ] 批准触发 API, 状态 approved  (T20)
 * - [ ] 驳回触发 API, 状态 rejected  (T20)
 * - [ ] 按钮在 Plan 渲染后可用    (T20)
 *
 * React Flow mounts in jsdom against the no-op observer stubs from
 * `test-setup.ts`; we assert rendered DOM (node cards, badges, info
 * panel, `data-mode`), never canvas internals. The T20 approve /
 * reject API is mocked at the module boundary so the drawer can be
 * exercised without a running backend.
 */

function makeSnapshot(overrides: Partial<ToolSnapshot> = {}): ToolSnapshot {
  return {
    tool_id: 't1',
    name: 'list_customers',
    description: '查询客户列表',
    risk_level: 'read',
    parameters_schema: { type: 'object' },
    http_method: 'GET',
    http_url_template: 'https://api.example.com/customers',
    http_headers: {},
    http_body_template: null,
    ...overrides,
  }
}

function makePlan(overrides: Partial<Plan> = {}): Plan {
  return {
    id: 'p1',
    conversation_id: 'c1',
    turn_id: 'u1',
    status: 'pending',
    nodes: [
      {
        node_id: 'n1',
        tool: 'list_customers',
        parameters: { region: 'emea' },
        notes: '查 EMEA 客户',
      } satisfies PlanNode,
    ],
    edges: [],
    tool_snapshots: [makeSnapshot()],
    edited_diff: null,
    created_at: '2026-09-28T07:00:00Z',
    updated_at: '2026-09-28T07:00:00Z',
    ...overrides,
  }
}

beforeEach(() => {
  usePlanDrawerStore.setState({
    plan: null,
    mode: 'collapsed',
    selectedNodeId: null,
  })
})

afterEach(() => {
  cleanup()
})

describe('PlanDrawer', () => {
  it('renders the single Plan node card with tool name and risk badge', () => {
    usePlanDrawerStore.getState().showPlan(makePlan())
    render(<PlanDrawer />)

    const card = screen.getByTestId('plan-node-n1')
    expect(card).toHaveTextContent('list_customers')
    expect(card).toHaveTextContent('只读')
    expect(card).toHaveTextContent('自动执行')
    expect(card).toHaveTextContent('查询客户列表')
  })

  it('preselects the single node so its info panel is on screen (节点信息)', () => {
    usePlanDrawerStore.getState().showPlan(makePlan())
    render(<PlanDrawer />)

    const panel = screen.getByTestId('node-info-panel')
    expect(panel).toHaveTextContent('list_customers')
    expect(panel).toHaveTextContent('GET https://api.example.com/customers')
    expect(panel).toHaveTextContent('查 EMEA 客户')
    expect(screen.getByTestId('node-info-parameters')).toHaveTextContent('"region": "emea"')
  })

  it('starts hidden (collapsed) and shows the placeholder when no Plan exists', () => {
    render(<PlanDrawer />)
    expect(screen.getByTestId('plan-drawer')).toHaveAttribute('data-mode', 'collapsed')
    expect(screen.getByTestId('plan-drawer')).toHaveClass('translate-x-full')
    expect(screen.getByText(/发送一条指令/)).toBeInTheDocument()
  })

  it('collapse button slides the drawer away; a Plan stays loaded behind it', () => {
    usePlanDrawerStore.getState().showPlan(makePlan())
    render(<PlanDrawer />)
    expect(screen.getByTestId('plan-drawer')).toHaveClass('translate-x-0')

    fireEvent.click(screen.getByRole('button', { name: '收起' }))
    expect(screen.getByTestId('plan-drawer')).toHaveClass('translate-x-full')
    expect(screen.getByTestId('plan-drawer')).toHaveAttribute('aria-hidden', 'true')
    expect(usePlanDrawerStore.getState().plan).not.toBeNull()
  })

  it('fullscreen button toggles docked <-> fullscreen (可收起 / 全屏)', async () => {
    usePlanDrawerStore.getState().showPlan(makePlan())
    render(<PlanDrawer />)
    const user = userEvent.setup()

    const drawer = screen.getByTestId('plan-drawer')
    expect(drawer).toHaveAttribute('data-mode', 'docked')

    await user.click(screen.getByRole('button', { name: '全屏' }))
    expect(drawer).toHaveAttribute('data-mode', 'fullscreen')
    expect(drawer).toHaveClass('w-full')
    expect(screen.queryByRole('button', { name: '全屏' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '退出全屏' }))
    expect(drawer).toHaveAttribute('data-mode', 'docked')
  })

  it('styles write vs destructive nodes differently (风险等级区分样式)', () => {
    const plan = makePlan({
      id: 'p-risk',
      nodes: [
        { node_id: 'n1', tool: 'write_tool', parameters: {}, notes: '' },
        { node_id: 'n2', tool: 'danger_tool', parameters: {}, notes: '' },
      ],
      tool_snapshots: [
        makeSnapshot({ name: 'write_tool', risk_level: 'write' }),
        makeSnapshot({ name: 'danger_tool', risk_level: 'destructive' }),
      ],
    })
    usePlanDrawerStore.getState().showPlan(plan)
    render(<PlanDrawer />)

    const write = screen.getByTestId('plan-node-n1')
    const destructive = screen.getByTestId('plan-node-n2')
    expect(write).toHaveClass('border-amber-400')
    expect(write).toHaveTextContent('写入')
    expect(destructive).toHaveClass('border-red-500')
    expect(destructive).toHaveTextContent('破坏性')
    expect(destructive).not.toHaveClass('border-amber-400')
  })

  it('selects a clicked node and swaps the info panel to it', () => {
    const plan = makePlan({
      id: 'p-two',
      nodes: [
        { node_id: 'n1', tool: 'list_customers', parameters: { region: 'emea' }, notes: '' },
        { node_id: 'n2', tool: 'get_order', parameters: { order_id: 42 }, notes: '拉取订单' },
      ],
      tool_snapshots: [
        makeSnapshot(),
        makeSnapshot({ name: 'get_order', description: '按 ID 查询订单' }),
      ],
    })
    usePlanDrawerStore.getState().showPlan(plan)
    render(<PlanDrawer />)

    // showPlan preselects n1; clicking n2 must move the panel.
    expect(screen.getByTestId('node-info-panel')).toHaveTextContent('list_customers')
    fireEvent.click(screen.getByTestId('plan-node-n2'))
    expect(screen.getByTestId('node-info-panel')).toHaveTextContent('get_order')
    expect(screen.getByTestId('node-info-parameters')).toHaveTextContent('"order_id": 42')
  })
})

/**
 * HITL approve / reject — T20 / #43.
 *
 * Mirrors the `mockFetch` pattern from `ChatPage.test.tsx`: queue
 * canned `Response` objects on `globalThis.fetch` so the drawer's
 * real `apiFetch` path can hit the approve / reject endpoints
 * without spinning up a backend. The drawer reads `plan.id` and
 * `plan.conversation_id` off the seeded Plan, so the path strings
 * stay stable.
 */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function mockFetch(responses: ReadonlyArray<Response>): ReturnType<typeof vi.fn> {
  const fn = vi.fn()
  for (const response of responses) {
    fn.mockResolvedValueOnce(response)
  }
  globalThis.fetch = fn as unknown as typeof fetch
  return fn
}

describe('PlanDrawer HITL (T20 / #43)', () => {
  it('renders the approve / reject buttons only after a pending Plan is loaded (按钮在 Plan 渲染后可用)', () => {
    // No plan yet — buttons are hidden.
    render(<PlanDrawer />)
    expect(screen.queryByTestId('plan-decision')).not.toBeInTheDocument()

    // After `showPlan`, the button row is on screen.
    act(() => {
      usePlanDrawerStore.getState().showPlan(makePlan())
    })
    expect(screen.getByTestId('plan-decision')).toBeInTheDocument()
    expect(screen.getByTestId('plan-approve')).toBeInTheDocument()
    expect(screen.getByTestId('plan-reject')).toBeInTheDocument()
  })

  it('hides the buttons once the Plan is no longer pending (e.g. approved)', () => {
    const approved = makePlan({ status: 'approved' })
    usePlanDrawerStore.getState().showPlan(approved)
    render(<PlanDrawer />)
    expect(screen.queryByTestId('plan-decision')).not.toBeInTheDocument()
    expect(screen.getByTestId('plan-status')).toHaveAttribute('data-status', 'approved')
  })

  it('approve triggers the API and updates Plan status to approved', async () => {
    const fetchMock = mockFetch([
      jsonResponse({ ...makePlan(), status: 'approved' }),
    ])
    usePlanDrawerStore.getState().showPlan(makePlan())
    render(<PlanDrawer />)

    const user = userEvent.setup()
    await user.click(screen.getByTestId('plan-approve'))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/conversations/c1/plan/approve')
    expect(init.method).toBe('POST')

    await waitFor(() =>
      expect(screen.getByTestId('plan-status')).toHaveAttribute('data-status', 'approved'),
    )
    // Buttons gone — the Plan is no longer pending.
    expect(screen.queryByTestId('plan-decision')).not.toBeInTheDocument()
  })

  it('reject triggers the API and updates Plan status to rejected', async () => {
    const fetchMock = mockFetch([
      jsonResponse({ ...makePlan(), status: 'rejected' }),
    ])
    usePlanDrawerStore.getState().showPlan(makePlan())
    render(<PlanDrawer />)

    const user = userEvent.setup()
    await user.click(screen.getByTestId('plan-reject'))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/conversations/c1/plan/reject')
    expect(init.method).toBe('POST')

    await waitFor(() =>
      expect(screen.getByTestId('plan-status')).toHaveAttribute('data-status', 'rejected'),
    )
  })

  it('surfaces the backend 409 message inline and keeps the Plan pending on conflict', async () => {
    mockFetch([
      jsonResponse({ code: 'plan_not_pending', message_zh: 'Plan 已不可变更' }, 409),
    ])
    usePlanDrawerStore.getState().showPlan(makePlan())
    render(<PlanDrawer />)

    const user = userEvent.setup()
    await user.click(screen.getByTestId('plan-approve'))

    const error = await screen.findByTestId('plan-decision-error')
    expect(error).toHaveTextContent('Plan 已不可变更')
    // The Plan status badge stays `pending` so the user can retry
    // (the canonical "decide failed, keep going" UX).
    expect(screen.getByTestId('plan-status')).toHaveAttribute('data-status', 'pending')
    expect(screen.getByTestId('plan-decision')).toBeInTheDocument()
  })
})
