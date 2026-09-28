/**
 * Plan preview drawer — T19 / #17.
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
 * (pure + unit-tested); this file is wiring only. HITL approve/reject
 * buttons land with T20 (#43) — until then the drawer is preview-only.
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
import { Maximize, Minimize, PanelRightClose } from 'lucide-react'
import { useMemo } from 'react'

import { NodeInfoPanel } from '@/components/plan/NodeInfoPanel'
import { PlanToolNode } from '@/components/plan/PlanToolNode'
import { Button } from '@/components/ui/button'
import {
  PLAN_TOOL_NODE_TYPE,
  buildFlowEdges,
  buildFlowNodes,
  type PlanNodeData,
} from '@/lib/plan-graph'
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
  const collapse = usePlanDrawerStore((s) => s.collapse)
  const toggleFullscreen = usePlanDrawerStore((s) => s.toggleFullscreen)
  const selectNode = usePlanDrawerStore((s) => s.selectNode)

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
          <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
            {plan.status}
          </span>
        )}
        <div className="ml-auto flex items-center gap-1">
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
          />
        ) : (
          <p className="p-4 text-sm text-muted-foreground">
            发送一条指令, Planner 生成的 Plan 会在这里以节点-边图展示。
          </p>
        )}
      </div>

      {plan && selectedNodeId && (
        <NodeInfoPanel plan={plan} nodeId={selectedNodeId} />
      )}
    </aside>
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
}: {
  plan: Plan
  nodes: Node<PlanNodeData>[]
  edges: Edge[]
  onSelectNode: (nodeId: string | null) => void
}): React.ReactElement {
  return (
    <ReactFlow
      key={plan.id}
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      fitView
      proOptions={{ hideAttribution: true }}
      onNodeClick={(_event, node) => onSelectNode(node.id)}
    >
      <Background variant={BackgroundVariant.Dots} gap={16} size={1} />
    </ReactFlow>
  )
}
