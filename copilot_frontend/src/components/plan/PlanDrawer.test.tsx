import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { PlanDrawer } from '@/components/plan/PlanDrawer'
import { usePlanDrawerStore } from '@/stores/plan-drawer'
import type { Plan, PlanNode, ToolSnapshot } from '@/types/plan'

/**
 * Drawer UI tests — T19 / #17 acceptance criteria:
 * - [ ] 显示单节点 + 节点信息
 * - [ ] 可收起 / 全屏
 * - [ ] 节点按风险等级区分样式
 *
 * React Flow mounts in jsdom against the no-op observer stubs from
 * `test-setup.ts`; we assert rendered DOM (node cards, badges, info
 * panel, `data-mode`), never canvas internals.
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
