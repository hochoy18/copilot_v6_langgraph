/**
 * Plan preview drawer — T19 / #17, T20 / #43, T24 / #21, T27 / #23.
 *
 * Right-side slide-out drawer rendering a Plan as a React Flow
 * node-edge graph (ADR-0029: "Plan 预览从右侧滑出为抽屉, 可全屏看图
 * 也可收起继续聊"). The three `mode`s map to the AC directly:
 *
 *   docked     — side panel over the chat, canvas + node info.
 *   fullscreen — same content, covers the viewport (看图).
 *   collapsed  — translated off-canvas, chat is unobstructed (收起).
 *
 * The aside stays mounted in every mode so the `transition-transform`
 * actually animates the slide-out; `aria-hidden` keeps the off-screen
 * copy from the a11y tree. Edge layout comes from `plan-graph.ts`
 * (pure + unit-tested); this file is wiring only.
 *
 * T20 / #43 (HITL approve / reject) layers two buttons on the header
 * when the Plan is `pending`. They hit the T20 backend endpoints
 * (`POST /conversations/{id}/plan/{approve,reject}`) and fold the
 * returned Plan back into the store so the status badge updates
 * without a refetch.
 *
 * T27 / #23 (Plan node parameter edit) mounts `NodeEditDialog` over
 * the drawer when `editingNodeId` is set; the entry button lives on
 * `NodeInfoPanel`'s header and calls `openEdit`. On a successful
 * PATCH, the dialog hands the post-edit Plan to `usePlanDrawerStore`
 * via `replacePlan` — the same single write point (approve / reject /
 * the SSE `plan.modified` event use.
 */
import '@xyflow/react/dist/style.css'

import {
  Background,
  BackgroundVariant,
  ReactFlow,
  type Edge,
  type Node,
  type NodeTypes,
} from '@xyflow/react'
import { Check, Maximize, Minimize, PanelRightClose, RefreshCw, WifiOff, X } from 'lucide-react'
import { useMemo, useState } from 'react'

import { NodeEditDialog } from '@/components/plan/NodeEditDialog'
import { NodeInfoPanel } from '@/components/plan/NodeInfoPanel'
import { PlanAnswerPane } from '@/components/plan/PlanAnswerPane'
import { PlanToolNode } from '@/components/plan/PlanToolNode'
import { Button } from '@/components/ui/button'
import {
  PLAN_TOOL_NODE_TYPE,
  buildFlowEdges,
  buildFlowNodes,
  type PlanNodeData,
} from '@/lib/plan-graph'
import {
  approvePlan,
  formatPlanDecisionError,
  rejectPlan,
} from '@/lib/conversations-api'
import {
  useConversationStreamStore,
  type StreamConnectionStatus,
} from '@/stores/conversation-stream'
import { usePlanDrawerStore } from '@/stores/plan-drawer'
import { cn } from '@/lib/utils'
import type { Plan } from '@/types/plan'

const NODE_TYPES: NodeTypes = {
  [PLAN_TOOL_NODE_TYPE]: PlanToolNode,
}

export function PlanDrawer(): React.ReactElement {
  const plan = usePlanDrawerStore((s) => s.plan)
  const mode = usePlanDrawerStore((s) => s.mode)
  const selectedNodeId = usePlanDrawerStore((s) => s.selectedNodeId)
  const editingNodeId = usePlanDrawerStore((s) => s.editingNodeId)
  const collapse = usePlanDrawerStore((s) => s.collapse)
  const toggleFullscreen = usePlanDrawerStore((s) => s.toggleFullscreen)
  const selectNode = usePlanDrawerStore((s) => s.selectNode)
  const replacePlan = usePlanDrawerStore((s) => s.replacePlan)
  const openEdit = usePlanDrawerStore((s) => s.openEdit)
  const closeEdit = usePlanDrawerStore((s) => s.closeEdit)

  const nodes = useMemo(
    () => (plan ? buildFlowNodes(plan) : []),
    [plan],
  )
  const edges = useMemo(
    () => (plan ? buildFlowEdges(plan) : []),
    [plan],
  )

  const collapsed = mode === 'collapsed'
  const fullscreen = mode === 'fullscreen'

  return (
    <aside
      data-testid="plan-drawer"
      data-mode={mode}
      aria-label="Plan 预览"
      aria-hidden={collapsed}
      className={cn(
        'fixed right-0 top-0 z-40 flex h-full flex-col border-l bg-card shadow-xl transition-transform duration-300 ease-in-out',
        fullscreen ? 'w-full' : 'max-w-md',
        collapsed ? 'translate-x-full' : 'translate-x-0',
      )}
    >
      <header className="flex shrink-0 items-center gap-2 border-b p-3">
        <h2 className="text-sm font-semibold">Plan 预览</h2>
        {plan && (
          <span
            data-testid="plan-status"
            data-status={plan.status}
            className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
          >
            {plan.status}
          </span>
        )}
        <ConnectionStatusPill />
        <div className="ml-auto flex items-center gap-1">
          {plan && plan.status === 'pending' && (
            <PlanDecisionButtons plan={plan} onDecided={replacePlan} />
          )}
          <Button
            variant="ghost"
            size="icon"
            aria-label={fullscreen ? '退出全屏' : '全屏'}
            onClick={toggleFullscreen}
          >
            {fullscreen ? <Minimize size={16} /> : <Maximize size={16} />}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label="收起"
            onClick={collapse}
          >
            <PanelRightClose size={16} />
          </Button>
        </div>
      </header>

      <div className="relative min-h-0 flex-1">
        {plan ? (
          <PlanCanvas
            plan={plan}
            nodes={nodes}
            edges={edges}
            onSelectNode={selectNode}
            onEditNode={openEdit}
          />
        ) : (
          <p className="p-4 text-sm text-muted-foreground">
            发送一条指令, Planner 生成的 Plan 会在这里以节点-边图展示。
          </p>
        )}
      </div>

      <PlanAnswerPane />

      {plan && selectedNodeId && (
        <NodeInfoPanel plan={plan} nodeId={selectedNodeId} onEdit={openEdit} />
      )}

      {/* T27 / #23: parameter-edit modal. Mounted alongside the
          drawer (not a React Portal) so its `z-50` overlay still
          stacks correctly above the drawer's `z-40`. Re-mounts on
          `editingNodeId` change so the form's initial state picks up
          the freshly-targeted node. */}
      {plan && editingNodeId && (
        <NodeEditDialog
          key={editingNodeId}
          plan={plan}
          nodeId={editingNodeId}
          open
          onClose={closeEdit}
          onSaved={replacePlan}
        />
      )}
    </aside>
  )
}

/**
 * The HITL approve / reject pair (T20 / #43, ADR-0004).
 *
 * Rendered only while the Plan is `pending` — once decided, the
 * buttons disappear (the header still shows the new `approved` /
 * `rejected` status badge). Each button calls the matching backend
 * endpoint, then folds the returned Plan back into the store so the
 * header re-renders against the new status. Errors surface inline
 * under the button row; the Plan stays `pending` on failure so the
 * user can retry.
 */
function PlanDecisionButtons({
  plan,
  onDecided,
}: {
  plan: Plan
  onDecided: (plan: Plan) => void
}): React.ReactElement {
  const [submitting, setSubmitting] = useState<'approve' | 'reject' | null>(
    null,
  )
  const [error, setError] = useState<string | null>(null)

  async function decide(action: 'approve' | 'reject'): Promise<void> {
    if (!plan.conversation_id) return
    setError(null)
    setSubmitting(action)
    try {
      const next =
        action === 'approve'
          ? await approvePlan(plan.conversation_id)
          : await rejectPlan(plan.conversation_id)
      onDecided(next)
    } catch (err) {
      setError(formatPlanDecisionError(err))
    } finally {
      setSubmitting(null)
    }
  }

  return (
    <div
      data-testid="plan-decision"
      className="flex items-center gap-1"
    >
      <Button
        type="button"
        variant="default"
        size="sm"
        data-testid="plan-approve"
        disabled={submitting !== null}
        onClick={() => void decide('approve')}
      >
        <Check size={14} />
        {submitting === 'approve' ? '批准中…' : '批准'}
      </Button>
      <Button
        type="button"
        variant="destructive"
        size="sm"
        data-testid="plan-reject"
        disabled={submitting !== null}
        onClick={() => void decide('reject')}
      >
        <X size={14} />
        {submitting === 'reject' ? '驳回中…' : '驳回'}
      </Button>
      {error && (
        <span
          role="alert"
          data-testid="plan-decision-error"
          className="ml-1 text-xs text-red-600"
        >
          {error}
        </span>
      )}
    </div>
  )
}

/**
 * One `<ReactFlow>` per Plan generation: `key` forces a remount so
 * the fresh layout + `fitView` re-run instead of fighting the stale
 * viewport of the previous Plan.
 */
function PlanCanvas({
  plan,
  nodes,
  edges,
  onSelectNode,
  onEditNode,
}: {
  plan: Plan
  nodes: Node<PlanNodeData>[]
  edges: Edge[]
  onSelectNode: (nodeId: string | null) => void
  /** T27 / #23 — double-clicking a node card opens the edit dialog. */
  onEditNode: (nodeId: string) => void
}): React.ReactElement {
  // Mirror ADR-0019's editable set client-side: a double-click on a
  // `succeeded` / `failed` / `executing` node would otherwise open a
  // dialog that immediately 409s on submit.
  const editable = plan.status === 'pending' || plan.status === 'modified'
  return (
    <ReactFlow
      key={plan.id}
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      fitView
      proOptions={{ hideAttribution: true }}
      onNodeClick={(_event, node) => onSelectNode(node.id)}
      onNodeDoubleClick={
        editable
          ? (_event, node) => onEditNode(node.id)
          : undefined
      }
    >
      <Background variant={BackgroundVariant.Dots} gap={16} size={1} />
    </ReactFlow>
  )
}

/**
 * Header pill that surfaces the SSE lifecycle — T24 / #21.
 *
 * `idle` / `open` / `closed` are deliberately invisible: "open" is
 * the happy path and a green pill on every screen would be noise.
 * The visible states are the transient ones (`connecting`,
 * `reconnecting`) and the terminal failure (`auth-failed`). The
 * hook keeps the status in `useConversationStreamStore`; this
 * component is just the visual, driven off one style map so the
 * pill chrome isn't duplicated per status (same pattern as
 * `RISK_STYLES` / `RUNTIME_STATUS_STYLES` in `plan-graph.ts`).
 */
const STATUS_PILLS: Partial<
  Record<StreamConnectionStatus, { label: string; className: string; spinner: boolean }>
> = {
  connecting: {
    label: '连接中…',
    className: 'bg-amber-100 text-amber-800',
    spinner: true,
  },
  reconnecting: {
    label: '重连中…',
    className: 'bg-amber-100 text-amber-800',
    spinner: true,
  },
  'auth-failed': {
    label: '登录已过期',
    className: 'bg-red-100 text-red-800',
    spinner: false,
  },
}

function ConnectionStatusPill(): React.ReactElement | null {
  const status = useConversationStreamStore((s) => s.connectionStatus)
  const pill = STATUS_PILLS[status]
  if (!pill) return null

  return (
    <span
      data-testid="stream-status"
      data-status={status}
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium',
        pill.className,
      )}
    >
      {pill.spinner ? (
        <RefreshCw size={12} className="animate-spin" aria-hidden="true" />
      ) : (
        <WifiOff size={12} aria-hidden="true" />
      )}
      {pill.label}
    </span>
  )
}
