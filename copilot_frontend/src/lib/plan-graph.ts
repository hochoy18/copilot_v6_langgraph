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
import type { ToolRuntimeStatus } from '@/types/sse'

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

/**
 * Per-node runtime status pulled off the SSE stream (T24 / #21, T29 / #25).
 * Reused by `PlanToolNode` so the node badge flips "执行中 / 成功 /
 * 失败" as the Worker reports progress without the consumer
 * knowing which event triggered the flip.
 *
 * T29 / #25 added `card` — the card-level overlay (ring + pulse for
 * live states, red border/fill/ring + dark-red text on failure).
 * Folding it into the same table keeps a single source of truth:
 * an earlier draft kept `card` in a parallel table and required
 * two lookups to stay exhaustive in lockstep (Shotgun Surgery).
 */
export interface RuntimeStatusStyle {
  /** Tailwind classes for the small status pill below the title. */
  pill: string
  /** Short Chinese label. */
  label: string
  /** Optional pulse animation — set on `running`. */
  pulse: boolean
  /**
   * Card-level overlay classes, layered on top of the risk-level
   * card by `PlanToolNode`. `''` means "leave the risk card alone";
   * `failed` deliberately overrides the risk colour so a failed
   * `read`-risk node can't masquerade as healthy at a glance.
   */
  card: string
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

// ---------------------------------------------------------------------------
// Runtime status — T24 / #21, T29 / #25.
//
// A node has a runtime status (running / succeeded / failed) that
// the SSE stream flips independently of the frozen risk level.
// `idle` is the absent case — when no `tool.*` event has fired
// yet — so the lookup table stays exhaustive without a `null`
// branch at every call site.
//
// T29 added the `card` field (the third sibling on each row) so
// parallel siblings all pulse at once and a failed sibling flips
// the whole card red. The `failed` overlay is intentionally
// dramatic — `border-red-600 bg-red-100 ring-2 ring-red-500
// ring-offset-1 text-red-900` — because `write`-risk is already
// `border-red-500 bg-red-50` and the AC's "失败节点标红" requires
// a clearly-different surface on every risk level.
// ---------------------------------------------------------------------------

const RUNTIME_STATUS_STYLES = {
  idle: {
    pill: 'bg-muted text-muted-foreground',
    label: '等待执行',
    pulse: false,
    // Absent / no event yet — leave the risk card alone.
    card: '',
  },
  running: {
    pill: 'bg-blue-100 text-blue-800',
    label: '执行中',
    pulse: true,
    // Pulse the whole card, not just the spinner icon — two
    // parallel siblings in `running` would otherwise look static
    // side by side.
    card: 'ring-2 ring-blue-500 ring-offset-1 animate-pulse',
  },
  succeeded: {
    pill: 'bg-emerald-100 text-emerald-800',
    label: '成功',
    pulse: false,
    // Settled-green ring so the green pill isn't the only cue.
    card: 'ring-2 ring-emerald-500 ring-offset-1',
  },
  failed: {
    pill: 'bg-red-100 text-red-800',
    label: '失败',
    pulse: false,
    // Hard red override — the AC's literal "标红". Beats the
    // risk-level emerald / amber / destructive-red so a failed
    // read-risk or write-risk node can't masquerade as healthy at
    // a glance. The thick red ring is the dominant signal; the
    // `text-red-900` lifts the body off the red-100 fill so the
    // description stays legible.
    card: 'border-red-600 bg-red-100 ring-2 ring-red-500 ring-offset-1 text-red-900',
  },
  skipped: {
    pill: 'bg-zinc-100 text-zinc-700',
    label: '跳过',
    pulse: false,
    // `skipped` / `cancelled` are quiet terminal states; the pill
    // is enough signal and the risk colour stays legible.
    card: '',
  },
  cancelled: {
    pill: 'bg-zinc-100 text-zinc-700',
    label: '已取消',
    pulse: false,
    card: '',
  },
} as const satisfies Record<ToolRuntimeStatus, RuntimeStatusStyle>

/**
 * Look up the runtime-status style. `undefined` = the store has no
 * entry for the node yet, which renders as `idle`. The `satisfies`
 * clause keeps this table exhaustive against `ToolRuntimeStatus`.
 */
export function runtimeStatusStyleFor(
  status: ToolRuntimeStatus | undefined,
): RuntimeStatusStyle {
  return RUNTIME_STATUS_STYLES[status ?? 'idle']
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
