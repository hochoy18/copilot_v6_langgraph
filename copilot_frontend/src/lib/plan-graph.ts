/**
 * Plan document → React Flow elements — T19 / #17.
 *
 * The drawer's one piece of real logic: turn a T17 Plan
 * (`nodes` / `edges` / `tool_snapshots`, ADR-0027) into positioned
 * React Flow nodes + edges, and centralise the risk-level styling
 * the AC calls "节点按风险等级区分样式".
 *
 * Kept pure (no React, no xyflow runtime import — types only) so
 * the whole DAG renderer is testable without a DOM. The backend
 * `PlanBase` validator already guarantees unique ids, snapshot
 * binding and acyclicity, so the longest-path layering here can
 * trust its input; the one defensive branch left is a dangling
 * `node.tool`, which renders as a `null` snapshot instead of
 * crashing (a Plan fetched from an older deployment may drift).
 *
 * Layout: layers run left→right by dependency depth (longest path
 * from any root), nodes within a layer stack top→bottom. T18 emits
 * single-node Plans, so the common case is one node at the origin;
 * the layering exists so T25 (#22) multi-node Plans render without
 * a layout-library dependency (dagre is the obvious upgrade path
 * once branch fan-out makes columns ugly).
 */
import type { Edge, Node } from '@xyflow/react'

import type { Plan, PlanNode, ToolRiskLevel, ToolSnapshot } from '@/types/plan'

/** Custom node type key registered on the React Flow canvas. */
export const PLAN_TOOL_NODE_TYPE = 'planTool'

/** Horizontal gap between dependency layers (px, React Flow units). */
const X_SPACING = 320
/** Vertical gap between same-layer nodes (px); card is ~150 tall. */
const Y_SPACING = 220

/**
 * Data payload for a `planTool` node: the invocation plus its frozen
 * definition (ADR-0027). `snapshot` is `null` only for a dangling
 * `node.tool` reference — see module docstring.
 */
export interface PlanNodeData extends Record<string, unknown> {
  node: PlanNode
  snapshot: ToolSnapshot | null
}

/** Visual identity for one risk level: card + badge classes and labels. */
export interface RiskStyle {
  /** Tailwind classes for the node card (border + background). */
  card: string
  /** Tailwind classes for the risk badge (background + text). */
  badge: string
  /** Short Chinese label on the badge. */
  label: string
  /** HITL consequence per ADR-0004, shown next to the risk badge. */
  hitl: string
}

const RISK_STYLES: Record<ToolRiskLevel, RiskStyle> = {
  // read → auto-executed: calm green, no confirmation friction.
  read: {
    card: 'border-emerald-400 bg-emerald-50',
    badge: 'bg-emerald-100 text-emerald-800',
    label: '只读',
    hitl: '自动执行',
  },
  // write → needs confirmation: warning amber.
  write: {
    card: 'border-amber-400 bg-amber-50',
    badge: 'bg-amber-100 text-amber-800',
    label: '写入',
    hitl: '需确认',
  },
  // destructive → needs confirmation: alarm red.
  destructive: {
    card: 'border-red-500 bg-red-50',
    badge: 'bg-red-100 text-red-800',
    label: '破坏性',
    hitl: '需确认',
  },
}

export function riskStyleFor(level: ToolRiskLevel): RiskStyle {
  return RISK_STYLES[level]
}

/**
 * Longest-path depth per `node_id` (0 = root). Kahn order so any
 * DAG — not just chains — gets a stable layering regardless of the
 * order `plan.nodes` arrives in.
 */
export function layoutDepths(plan: Plan): Record<string, number> {
  const depth: Record<string, number> = {}
  for (const node of plan.nodes) depth[node.node_id] = 0

  const indegree: Record<string, number> = {}
  const outgoing: Record<string, string[]> = {}
  for (const node of plan.nodes) {
    indegree[node.node_id] = 0
    outgoing[node.node_id] = []
  }
  for (const edge of plan.edges) {
    indegree[edge.target] += 1
    outgoing[edge.source].push(edge.target)
  }

  const queue = plan.nodes
    .filter((node) => indegree[node.node_id] === 0)
    .map((node) => node.node_id)
  while (queue.length > 0) {
    const current = queue.shift() as string
    for (const next of outgoing[current]) {
      if (depth[current] + 1 > depth[next]) depth[next] = depth[current] + 1
      indegree[next] -= 1
      if (indegree[next] === 0) queue.push(next)
    }
  }
  return depth
}

/** Resolve a node's frozen Tool definition; `null` when dangling. */
export function snapshotFor(plan: Plan, toolName: string): ToolSnapshot | null {
  return plan.tool_snapshots.find((snap) => snap.name === toolName) ?? null
}

export function buildFlowNodes(plan: Plan): Node<PlanNodeData>[] {
  const depths = layoutDepths(plan)
  // Index within each depth layer, in `plan.nodes` order → row.
  const rowIndex: Record<number, number> = {}
  return plan.nodes.map((node) => {
    const depth = depths[node.node_id] ?? 0
    const row = rowIndex[depth] ?? 0
    rowIndex[depth] = row + 1
    return {
      id: node.node_id,
      type: PLAN_TOOL_NODE_TYPE,
      position: { x: depth * X_SPACING, y: row * Y_SPACING },
      data: { node, snapshot: snapshotFor(plan, node.tool) } satisfies PlanNodeData,
    }
  })
}

export function buildFlowEdges(plan: Plan): Edge[] {
  return plan.edges.map((edge) => ({
    id: `${edge.source}->${edge.target}`,
    source: edge.source,
    target: edge.target,
    animated: false,
  }))
}
