/**
 * Node edit dialog — T27 / #23 acceptance criteria:
 *  - [x] 点节点弹表单
 *  - [x] 改 hello→world 批准后 Tool 用新参数
 *  - [x] 返回 world
 *
 * The dialog hydrates from a Plan + nodeId passed in by PlanDrawer
 * (T19 wires the "编辑参数" entry button — see `NodeInfoPanel`).
 * Form state is local (`useState` of two strings — JSON for
 * parameters + plain text for notes) so the dialog's render path
 * stays decoupled from the drawer store and the tests can pin the
 * "edit one Plan, save, see Plan swap" cycle in isolation.
 *
 * The submit path goes through `editPlan` (T26 / #44, PATCH
 * /conversations/{id}/plan) and on success the parent folds the
 * response back into the store via `onSaved`. Errors render inline
 * — both the client-side JSON parse and the backend's
 * `validation_error` / `plan_not_pending` envelopes.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { NodeEditDialog } from '@/components/plan/NodeEditDialog'
import { jsonResponse, mockFetch } from '@/test-utils'
import { usePlanDrawerStore } from '@/stores/plan-drawer'
import type { Plan, ToolSnapshot } from '@/types/plan'

function makeSnapshot(overrides: Partial<ToolSnapshot> = {}): ToolSnapshot {
  return {
    tool_id: 't-echo',
    name: 'echo',
    description: 'Echo back a string.',
    risk_level: 'read',
    parameters_schema: { type: 'object', properties: { text: { type: 'string' } } },
    http_method: 'POST',
    http_url_template: 'https://api.example.com/echo',
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
        tool: 'echo',
        parameters: { text: 'hello' },
        notes: '回显 hello',
      },
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
    editingNodeId: null,
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('NodeEditDialog', () => {
  it('prefills the form from the Plan node (parameters JSON + notes)', () => {
    const plan = makePlan()
    render(
      <NodeEditDialog
        plan={plan}
        nodeId="n1"
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    )

    expect(screen.getByTestId('node-edit-parameters')).toHaveValue(
      '{\n  "text": "hello"\n}',
    )
    expect(screen.getByTestId('node-edit-notes')).toHaveValue('回显 hello')
    // The frozen Tool slug is shown so the user knows what they're editing.
    expect(screen.getByTestId('node-edit-tool')).toHaveTextContent('echo')
  })

  it('changing hello → world submits a PATCH with the FULL nodes list and folds the modified Plan back', async () => {
    const fetchMock = mockFetch([
      jsonResponse(
        makePlan({
          status: 'modified',
          nodes: [
            {
              node_id: 'n1',
              tool: 'echo',
              parameters: { text: 'world' },
              notes: '回显 hello',
            },
          ],
        }),
      ),
    ])
    const plan = makePlan()
    const onSaved = vi.fn()
    const onClose = vi.fn()
    render(
      <NodeEditDialog plan={plan} nodeId="n1" open onClose={onClose} onSaved={onSaved} />,
    )

    const user = userEvent.setup()
    const params = screen.getByTestId('node-edit-parameters') as HTMLTextAreaElement
    fireEvent.change(params, { target: { value: '{"text":"world"}' } })
    await user.click(screen.getByTestId('node-edit-submit'))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/conversations/c1/plan')
    expect(init.method).toBe('PATCH')
    // T26 / ADR-0019 contract: every node must be in the PATCH body
    // (the backend's `record_edit` runs a set-equality check and
    // would 400 a partial list). The single-node Plan in this test
    // trivially keeps `n1`; the multi-node test below pins the
    // sibling pass-through.
    expect(JSON.parse(init.body as string)).toEqual({
      nodes: [
        {
          node_id: 'n1',
          tool: 'echo',
          parameters: { text: 'world' },
          notes: '回显 hello',
        },
      ],
    })

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    const savedPlan = onSaved.mock.calls[0][0] as Plan
    expect(savedPlan.status).toBe('modified')
    expect(savedPlan.nodes[0].parameters).toEqual({ text: 'world' })
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('passes un-edited sibling nodes through unchanged in the PATCH body (multi-node Plan)', async () => {
    const fetchMock = mockFetch([
      jsonResponse(makePlan({ id: 'p-multi', status: 'modified' })),
    ])
    const plan = makePlan({
      id: 'p-multi',
      nodes: [
        { node_id: 'n1', tool: 'echo', parameters: { text: 'hello' }, notes: 'A' },
        { node_id: 'n2', tool: 'log_event', parameters: { tag: 'audit' }, notes: 'B' },
        { node_id: 'n3', tool: 'noop', parameters: {}, notes: '' },
      ],
      tool_snapshots: [
        ...[
          {
            tool_id: 't1',
            name: 'echo',
            description: '',
            risk_level: 'read' as const,
            parameters_schema: {},
            http_method: 'POST',
            http_url_template: 'https://api/echo',
            http_headers: {},
            http_body_template: null,
          },
        ],
        {
          tool_id: 't2',
          name: 'log_event',
          description: '',
          risk_level: 'read' as const,
          parameters_schema: {},
          http_method: 'POST',
          http_url_template: 'https://api/log',
          http_headers: {},
          http_body_template: null,
        },
        {
          tool_id: 't3',
          name: 'noop',
          description: '',
          risk_level: 'read' as const,
          parameters_schema: {},
          http_method: 'POST',
          http_url_template: 'https://api/noop',
          http_headers: {},
          http_body_template: null,
        },
      ],
    })
    render(
      <NodeEditDialog
        plan={plan}
        nodeId="n2"
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    )

    const user = userEvent.setup()
    const params = screen.getByTestId('node-edit-parameters') as HTMLTextAreaElement
    fireEvent.change(params, { target: { value: '{"tag":"audit-final"}' } })
    await user.click(screen.getByTestId('node-edit-submit'))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const body = JSON.parse(init.body as string) as { nodes: Array<{ node_id: string; parameters: Record<string, unknown> }> }
    expect(body.nodes).toHaveLength(3)
    expect(body.nodes.find((n) => n.node_id === 'n2')?.parameters).toEqual({
      tag: 'audit-final',
    })
    expect(body.nodes.find((n) => n.node_id === 'n1')?.parameters).toEqual({
      text: 'hello',
    })
    expect(body.nodes.find((n) => n.node_id === 'n3')?.parameters).toEqual({})
  })

  it('blocks submit with an inline error when parameters is not valid JSON', async () => {
    const fetchMock = mockFetch([])
    const plan = makePlan()
    render(
      <NodeEditDialog
        plan={plan}
        nodeId="n1"
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    )

    const user = userEvent.setup()
    const params = screen.getByTestId('node-edit-parameters') as HTMLTextAreaElement
    fireEvent.change(params, { target: { value: '{not-json' } })
    await user.click(screen.getByTestId('node-edit-submit'))

    const error = await screen.findByTestId('node-edit-error')
    expect(error).toHaveTextContent(/JSON/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('blocks submit when parameters is not a JSON object (e.g. a bare string)', async () => {
    const fetchMock = mockFetch([])
    const plan = makePlan()
    render(
      <NodeEditDialog
        plan={plan}
        nodeId="n1"
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    )

    const user = userEvent.setup()
    const params = screen.getByTestId('node-edit-parameters') as HTMLTextAreaElement
    fireEvent.change(params, { target: { value: '"just a string"' } })
    await user.click(screen.getByTestId('node-edit-submit'))

    expect(await screen.findByTestId('node-edit-error')).toHaveTextContent(/对象/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('surfaces backend 409 message_zh and keeps the dialog open on conflict', async () => {
    mockFetch([
      jsonResponse({ code: 'plan_not_pending', message_zh: 'Plan 已不可变更' }, 409),
    ])
    const plan = makePlan()
    const onSaved = vi.fn()
    render(
      <NodeEditDialog plan={plan} nodeId="n1" open onClose={vi.fn()} onSaved={onSaved} />,
    )

    const user = userEvent.setup()
    await user.click(screen.getByTestId('node-edit-submit'))

    expect(await screen.findByTestId('node-edit-error')).toHaveTextContent(
      'Plan 已不可变更',
    )
    expect(onSaved).not.toHaveBeenCalled()
    // Dialog stays open so the user can retry after the Plan un-sticks.
    expect(screen.getByTestId('node-edit-dialog')).toBeInTheDocument()
  })

  it('renders nothing when the node is missing from the Plan', () => {
    const plan = makePlan()
    const { container } = render(
      <NodeEditDialog
        plan={plan}
        nodeId="n-does-not-exist"
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('does not render when closed', () => {
    const plan = makePlan()
    render(
      <NodeEditDialog
        plan={plan}
        nodeId="n1"
        open={false}
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    )
    expect(screen.queryByTestId('node-edit-dialog')).not.toBeInTheDocument()
  })
})