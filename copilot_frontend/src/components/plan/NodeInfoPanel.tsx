/**
 * Node info panel — T19 / #17, T27 / #23.
 *
 * The "节点信息" half of the AC: everything the business user needs
 * to judge the pending node before HITL (ADR-0004) — Tool
 * description, frozen risk level + its HITL consequence, endpoint,
 * the Planner's chosen parameters, and the Planner's notes. All
 * fields resolve from the Plan's own `tool_snapshots` (ADR-0027),
 * so the panel shows what *will* execute, not what the live Tool
 * row says today.
 *
 * T27 / #23 layers a single "编辑参数" button on the header that
 * opens the parameter-edit dialog (handled by `PlanDrawer`). The
 * button only renders when the Plan is editable (`pending` /
 * `modified` per ADR-0019 + the backend's `PlanNotPendingError`
 * 409 envelope), so a user staring at a `succeeded` / `failed`
 * Plan can't open the dialog into a guaranteed-conflict path.
 */
import { Pencil } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { riskStyleFor, snapshotFor } from '@/lib/plan-graph'
import { cn } from '@/lib/utils'
import type { Plan } from '@/types/plan'

export function NodeInfoPanel({
  plan,
  nodeId,
  onEdit,
}: {
  plan: Plan
  nodeId: string
  /** T27 / #23 — open the parameter-edit dialog against this node. */
  onEdit(nodeId: string): void
}): React.ReactElement | null {
  const node = plan.nodes.find((n) => n.node_id === nodeId)
  if (!node) return null
  const snapshot = snapshotFor(plan, node.tool)
  const style = snapshot ? riskStyleFor(snapshot.risk_level) : null
  const editable = plan.status === 'pending' || plan.status === 'modified'

  return (
    <section
      data-testid="node-info-panel"
      className="shrink-0 space-y-3 border-t bg-card p-4 text-sm"
    >
      <header className="flex items-center justify-between gap-2">
        <h3 className="font-mono font-semibold">{node.tool}</h3>
        <div className="flex shrink-0 items-center gap-2">
          {style && (
            <>
              <span
                className={cn(
                  'rounded-full px-2 py-0.5 text-xs font-medium',
                  style.badge,
                )}
              >
                {style.label}
              </span>
              <span className="text-xs text-muted-foreground">{style.hitl}</span>
            </>
          )}
          {editable && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              data-testid="node-edit-button"
              onClick={() => onEdit(nodeId)}
              aria-label="编辑参数"
            >
              <Pencil size={12} aria-hidden="true" />
              编辑参数
            </Button>
          )}
        </div>
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
