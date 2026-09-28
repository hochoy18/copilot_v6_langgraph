/**
 * Plan drawer store — T19 / #17 (Zustand per ADR-0029).
 *
 * Client state for the right-side Plan preview drawer: which Plan is
 * on screen, whether it is docked / fullscreen / collapsed, and the
 * currently selected node (drives the node info panel). ADR-0029's
 * layout rule — "Plan 预览从右侧滑出为抽屉, 可全屏看图也可收起继续
 * 聊" — is exactly the `mode` union below.
 *
 * The SSE-hook ticket (T24 / #21) will add Plan *status* updates to
 * this store's `plan`; T19 hydrates it synchronously from the
 * `POST /turns` response (T18), so the store only owns view state.
 */
import { create } from 'zustand'

import type { Plan } from '@/types/plan'

export type PlanDrawerMode = 'collapsed' | 'docked' | 'fullscreen'

interface PlanDrawerState {
  /** The Plan currently previewed, or `null` when none was generated yet. */
  plan: Plan | null
  mode: PlanDrawerMode
  /** `node_id` whose details the info panel shows; `null` = none. */
  selectedNodeId: string | null
  /** Slide the drawer out with a fresh Plan (AC: 输入指令抽屉滑出). */
  showPlan(plan: Plan): void
  collapse(): void
  /** Re-open a collapsed drawer without changing its Plan. */
  reopen(): void
  toggleFullscreen(): void
  selectNode(nodeId: string | null): void
}

export const usePlanDrawerStore = create<PlanDrawerState>((set) => ({
  plan: null,
  mode: 'collapsed',
  selectedNodeId: null,

  showPlan: (plan) =>
    set({
      plan,
      mode: 'docked',
      // T18 emits single-node Plans — preselect so 节点信息 is on
      // screen immediately (AC: 显示单节点 + 节点信息). With a future
      // multi-node Plan this keeps the first node's info visible.
      selectedNodeId: plan.nodes[0]?.node_id ?? null,
    }),

  collapse: () => set({ mode: 'collapsed' }),
  reopen: () => set((state) => (state.plan ? { mode: 'docked' } : state)),
  toggleFullscreen: () =>
    set((state) => ({
      mode: state.mode === 'fullscreen' ? 'docked' : 'fullscreen',
    })),
  selectNode: (nodeId) => set({ selectedNodeId: nodeId }),
}))
