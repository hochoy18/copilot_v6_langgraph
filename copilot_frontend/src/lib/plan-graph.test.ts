import { describe, expect, it } from 'vitest'

import {
  buildFlowEdges,
  buildFlowNodes,
  layoutDepths,
  riskStyleFor,
  runtimeStatusStyleFor,
} from '@/lib/plan-graph'
import type { Plan, PlanNode, ToolSnapshot } from '@/types/plan'

/**
 * Pure mapping from a T17 Plan document to React Flow elements —
 * T19 / #17.
 *
 * This is the drawer's only non-trivial logic (graph layout +
 * risk-level styling), so it lives here as testable functions and
 * the React Flow components stay dumb renderers. The backend
 * guarantees snapshot binding + acyclicity (PlanBase validators),
 * but the renderer stays defensive: a dangling `tool` name must
 * surface as a `null` snapshot rather than a crash.
 */

function makeSnapshot(overrides: Partial<ToolSnapshot> = {}): ToolSnapshot {
  return {
    tool_id: 't1',
    name: 'list_customers',
    description: '查询客户列表',
    risk_level: 'read',
    parameters_schema: { type: 'object', properties: { region: { type: 'string' } } },
    http_method: 'GET',
    http_url_template: 'https://api.example.com/customers',
    http_headers: {},
    http_body_template: null,
    ...overrides,
  }
}

function makeNode(overrides: Partial<PlanNode> = {}): PlanNode {
  return {
    node_id: 'n1',
    tool: 'list_customers',
    parameters: { region: 'emea' },
    notes: '查 EMEA 客户',
    ...overrides,
  }
}

function makePlan(overrides: Partial<Plan> = {}): Plan {
  return {
    id: 'p1',
    conversation_id: 'c1',
    turn_id: 'u1',
    status: 'pending',
    nodes: [makeNode()],
    edges: [],
    tool_snapshots: [makeSnapshot()],
    edited_diff: null,
    created_at: '2026-09-28T07:00:00Z',
    updated_at: '2026-09-28T07:00:00Z',
    ...overrides,
  }
}

describe('layoutDepths', () => {
  it('places a single node at depth 0', () => {
    const plan = makePlan()
    expect(layoutDepths(plan)).toEqual({ n1: 0 })
  })

  it('pushes a dependent node one layer deeper along the edge', () => {
    const plan = makePlan({
      nodes: [makeNode(), makeNode({ node_id: 'n2', tool: 'create_order' })],
      edges: [{ source: 'n1', target: 'n2' }],
      tool_snapshots: [makeSnapshot(), makeSnapshot({ name: 'create_order' })],
    })
    expect(layoutDepths(plan)).toEqual({ n1: 0, n2: 1 })
  })

  it('uses the longest path when a node has multiple predecessors', () => {
    // n1 -> n3 and n1 -> n2 -> n3: n3 must land at depth 2, not 1.
    const plan = makePlan({
      nodes: [
        makeNode(),
        makeNode({ node_id: 'n2', tool: 'create_order' }),
        makeNode({ node_id: 'n3', tool: 'notify' }),
      ],
      edges: [
        { source: 'n1', target: 'n2' },
        { source: 'n1', target: 'n3' },
        { source: 'n2', target: 'n3' },
      ],
      tool_snapshots: [
        makeSnapshot(),
        makeSnapshot({ name: 'create_order' }),
        makeSnapshot({ name: 'notify' }),
      ],
    })
    expect(layoutDepths(plan)).toEqual({ n1: 0, n2: 1, n3: 2 })
  })
})

describe('buildFlowNodes', () => {
  it('maps the T18 single-node plan to one planTool node at the origin', () => {
    const nodes = buildFlowNodes(makePlan())
    expect(nodes).toHaveLength(1)
    expect(nodes[0].id).toBe('n1')
    expect(nodes[0].type).toBe('planTool')
    expect(nodes[0].position).toEqual({ x: 0, y: 0 })
    expect(nodes[0].data.node).toEqual(makeNode())
    expect(nodes[0].data.snapshot?.name).toBe('list_customers')
  })

  it('spreads parallel same-depth nodes vertically', () => {
    const plan = makePlan({
      nodes: [makeNode(), makeNode({ node_id: 'n2', tool: 'list_orders' })],
      tool_snapshots: [makeSnapshot(), makeSnapshot({ name: 'list_orders' })],
    })
    const [a, b] = buildFlowNodes(plan)
    expect(a.position.x).toBe(b.position.x)
    expect(a.position.y).not.toBe(b.position.y)
  })

  it('tolerates a node whose tool has no snapshot (defensive, null snapshot)', () => {
    const plan = makePlan({
      nodes: [makeNode({ tool: 'ghost_tool' })],
      tool_snapshots: [],
    })
    const [node] = buildFlowNodes(plan)
    expect(node.data.snapshot).toBeNull()
  })
})

describe('buildFlowEdges', () => {
  it('carries source/target through and keeps ids unique', () => {
    const plan = makePlan({
      nodes: [makeNode(), makeNode({ node_id: 'n2', tool: 'create_order' })],
      edges: [{ source: 'n1', target: 'n2' }],
      tool_snapshots: [makeSnapshot(), makeSnapshot({ name: 'create_order' })],
    })
    const flowEdges = buildFlowEdges(plan)
    expect(flowEdges).toHaveLength(1)
    expect(flowEdges[0].source).toBe('n1')
    expect(flowEdges[0].target).toBe('n2')
    expect(flowEdges[0].id).toBe('n1->n2')
  })

  it('returns an empty array for a single-node plan', () => {
    expect(buildFlowEdges(makePlan())).toEqual([])
  })
})

describe('riskStyleFor', () => {
  it('gives read / write / destructive distinct card + badge classes', () => {
    const read = riskStyleFor('read')
    const write = riskStyleFor('write')
    const destructive = riskStyleFor('destructive')
    const cards = new Set([read.card, write.card, destructive.card])
    expect(cards.size).toBe(3)
    expect(read.label).toBe('只读')
    expect(write.label).toBe('写入')
    expect(destructive.label).toBe('破坏性')
  })

  it('marks write / destructive as needing confirmation (ADR-0004)', () => {
    expect(riskStyleFor('read').hitl).toBe('自动执行')
    expect(riskStyleFor('write').hitl).toBe('需确认')
    expect(riskStyleFor('destructive').hitl).toBe('需确认')
  })
})

describe('runtimeStatusStyleFor (T24 / #21, T29 / #25)', () => {
  it('renders absent (never-seen) nodes as idle', () => {
    const idle = runtimeStatusStyleFor(undefined)
    expect(idle.label).toBe('等待执行')
    expect(idle.pulse).toBe(false)
    expect(idle.card).toBe('')
  })

  it('labels the live execution states with Chinese badge copy', () => {
    expect(runtimeStatusStyleFor('running').label).toBe('执行中')
    expect(runtimeStatusStyleFor('succeeded').label).toBe('成功')
    expect(runtimeStatusStyleFor('failed').label).toBe('失败')
    expect(runtimeStatusStyleFor('skipped').label).toBe('跳过')
    expect(runtimeStatusStyleFor('cancelled').label).toBe('已取消')
  })

  it('pulses only while running — the one state worth animating', () => {
    expect(runtimeStatusStyleFor('running').pulse).toBe(true)
    for (const settled of ['succeeded', 'failed', 'skipped', 'cancelled'] as const) {
      expect(runtimeStatusStyleFor(settled).pulse).toBe(false)
    }
  })

  it('card overlay is empty for idle / skipped / cancelled (no change to risk card)', () => {
    // T29 / #25: only live states decorate the card; quiet terminal
    // states leave the risk-level card alone.
    expect(runtimeStatusStyleFor('idle').card).toBe('')
    expect(runtimeStatusStyleFor('skipped').card).toBe('')
    expect(runtimeStatusStyleFor('cancelled').card).toBe('')
  })

  it('pulses the card border while running so parallel siblings are both visibly animated', () => {
    const overlay = runtimeStatusStyleFor('running').card
    // animate-pulse + a blue ring; the per-node spinner stays for the badge.
    expect(overlay).toMatch(/animate-pulse/)
    expect(overlay).toMatch(/ring-blue/)
  })

  it('rings the card emerald once the node succeeded', () => {
    const overlay = runtimeStatusStyleFor('succeeded').card
    expect(overlay).toMatch(/ring-emerald/)
    expect(overlay).not.toMatch(/animate-pulse/)
  })

  it('overrides the card with red on failure — the AC "失败节点标红" requirement', () => {
    const overlay = runtimeStatusStyleFor('failed').card
    // Red border + red fill override the risk-level emerald / amber
    // so a failed read-risk node can't masquerade as healthy. The
    // thick `ring-red` is the dominant signal — `write`-risk is
    // already `border-red-500 bg-red-50`, so without a separate ring
    // the failed overlay would be near-indistinguishable from idle.
    expect(overlay).toMatch(/border-red/)
    expect(overlay).toMatch(/bg-red/)
    expect(overlay).toMatch(/ring-red/)
    expect(overlay).toMatch(/text-red/)
    expect(overlay).not.toMatch(/animate-pulse/)
  })
})
