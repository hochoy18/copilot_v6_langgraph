/**
 * `planTool` custom node — T19 / #17.
 *
 * Renders one Plan invocation: Tool slug, frozen description, and the
 * risk-level badge that satisfies the AC "节点按风险等级区分样式"
 * (card + badge classes come from `riskStyleFor`, keyed off the
 * ADR-0027 snapshot so the colour reflects the risk *at Plan
 * generation time*, not the live Tool row).
 *
 * Clicking a node selects it and drives `NodeInfoPanel` — parameter
 * *editing* is T26/T27 (#23/#24); this card is read-only preview.
 */
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'

import { PLAN_TOOL_NODE_TYPE, riskStyleFor, type PlanNodeData } from '@/lib/plan-graph'
import { cn } from '@/lib/utils'

/** Narrowed node type so `data` is `PlanNodeData`, not `unknown`. */
type PlanToolFlowNode = Node<PlanNodeData, typeof PLAN_TOOL_NODE_TYPE>

export function PlanToolNode({
  data,
  selected,
}: NodeProps<PlanToolFlowNode>): React.ReactElement {
  const { node, snapshot } = data
  const style = snapshot ? riskStyleFor(snapshot.risk_level) : null

  return (
    <div
      data-testid={`plan-node-${node.node_id}`}
      className={cn(
        'w-64 rounded-lg border-2 bg-card p-3 shadow-sm',
        style ? style.card : 'border-dashed border-muted-foreground/50 bg-muted',
        selected && 'ring-2 ring-ring ring-offset-1',
      )}
    >
      {/* Edge anchors; T18 single-node Plans never draw them, T25 will. */}
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />

      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-sm font-semibold" title={node.tool}>
          {node.tool}
        </span>
        <span
          className={cn(
            'shrink-0 rounded-full px-2 py-0.5 text-xs font-medium',
            style ? style.badge : 'bg-muted text-muted-foreground',
          )}
        >
          {style ? style.label : '未知'}
        </span>
      </div>

      <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
        {snapshot?.description || '快照缺失:该节点引用的 Tool 未冻结定义'}
      </p>

      {style && (
        <p className="mt-1 text-xs font-medium text-muted-foreground">{style.hitl}</p>
      )}
    </div>
  )
}
