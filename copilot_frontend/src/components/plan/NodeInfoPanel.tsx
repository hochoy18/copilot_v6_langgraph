/**
 * Node info panel — T19 / #17.
 *
 * The "节点信息" half of the AC: everything the business user needs
 * to judge the pending node before HITL (ADR-0004) — Tool
 * description, frozen risk level + its HITL consequence, endpoint,
 * the Planner's chosen parameters, and the Planner's notes. All
 * fields resolve from the Plan's own `tool_snapshots` (ADR-0027),
 * so the panel shows what *will* execute, not what the live Tool
 * row says today. Editing lands with T26 (#44) — this is a read
 * view.
 */
import { riskStyleFor, snapshotFor } from '@/lib/plan-graph'
import { cn } from '@/lib/utils'
import type { Plan } from '@/types/plan'

export function NodeInfoPanel({
  plan,
  nodeId,
}: {
  plan: Plan
  nodeId: string
}): React.ReactElement | null {
  const node = plan.nodes.find((n) => n.node_id === nodeId)
  if (!node) return null
  const snapshot = snapshotFor(plan, node.tool)
  const style = snapshot ? riskStyleFor(snapshot.risk_level) : null

  return (
    <section
      data-testid="node-info-panel"
      className="shrink-0 space-y-3 border-t bg-card p-4 text-sm"
    >
      <header className="flex items-center justify-between gap-2">
        <h3 className="font-mono font-semibold">{node.tool}</h3>
        {style && (
          <div className="flex shrink-0 items-center gap-2">
            <span
              className={cn(
                'rounded-full px-2 py-0.5 text-xs font-medium',
                style.badge,
              )}
            >
              {style.label}
            </span>
            <span className="text-xs text-muted-foreground">{style.hitl}</span>
          </div>
        )}
      </header>

      {snapshot && (
        <p className="text-muted-foreground">{snapshot.description}</p>
      )}

      <dl className="space-y-2">
        <div>
          <dt className="text-xs font-medium text-muted-foreground">调用端点</dt>
          <dd className="font-mono text-xs">
            {snapshot
              ? `${snapshot.http_method} ${snapshot.http_url_template}`
              : '—'}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-muted-foreground">参数</dt>
          <dd
            data-testid="node-info-parameters"
            className="mt-1 max-h-40 overflow-auto rounded-md bg-muted p-2 font-mono text-xs"
          >
            {JSON.stringify(node.parameters, null, 2)}
          </dd>
        </div>
        {node.notes && (
          <div>
            <dt className="text-xs font-medium text-muted-foreground">Planner 备注</dt>
            <dd>{node.notes}</dd>
          </div>
        )}
      </dl>
    </section>
  )
}
