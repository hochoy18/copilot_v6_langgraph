/**
 * Plan drawer store — T19 / #17, T20 / #43, T27 / #23 (Zustand per ADR-0029).
 *
 * Client state for the right-side Plan preview drawer: which Plan is
 * on screen, whether it is docked / fullscreen / collapsed, the
 * currently selected node (drives the node info panel), and the
 * node currently being edited in the popup dialog (T27). ADR-0029's
 * layout rule — "Plan 预览从右侧滑出为抽屉, 可全屏看图也可收起继续
 * 聊" — is exactly the `mode` union below.
 *
 * T20 / #43 / T27 / #23 converge on `replacePlan`: after a successful
 * HITL approve / reject (T20) or Plan-edit PATCH (T27), the Plan
 * returned by the backend is folded back into the store so the
 * header status badge + button set reflect the new lifecycle without
 * a refetch. The issue #53 handoff comment pins this store as the
 * canonical Plan swap site — T24 / #21 keeps that promise for the
 * SSE path too (`plan.modified` lands via `replacePlan` in
 * `useConversationStream`).
 */
import { create } from 'zustand'

import type { Plan, PlanStatus } from '@/types/plan'

export type PlanDrawerMode = 'collapsed' | 'docked' | 'fullscreen'

interface PlanDrawerState {
  /** The Plan currently previewed, or `null` when none was generated yet. */
  plan: Plan | null
  mode: PlanDrawerMode
  /** `node_id` whose details the info panel shows; `null` = none. */
  selectedNodeId: string | null
  /**
   * `node_id` whose parameters are being edited in the popup
   * (T27 / #23, ADR-0019). The dialog is mounted but hidden when
   * `null`; setting it opens the dialog against the matching node.
   */
  editingNodeId: string | null
  /** Slide the drawer out with a fresh Plan (AC: 输入指令抽屉滑出). */
  showPlan(plan: Plan): void
  collapse(): void
  /** Re-open a collapsed drawer without changing its Plan. */
  reopen(): void
  toggleFullscreen(): void
  selectNode(nodeId: string | null): void
  /** Open the parameter-edit dialog for one node (T27 / #23). */
  openEdit(nodeId: string): void
  /** Close the parameter-edit dialog without saving. */
  closeEdit(): void
  /**
   * Swap in a Plan returned by the backend (T20 / #43 approve /
   * reject, T27 / #23 edit, T24 / #21 SSE `plan.modified` —
   * issue #53 pins this store as the Plan's single write point).
   * The header badge re-renders against the new status; the
   * approve / reject buttons disappear because the Plan is no
   * longer `pending`.
   */
  replacePlan(plan: Plan): void
  /**
   * Fold an SSE `execution.completed` outcome onto the live Plan
   * (T24 / #21). The Worker (T21) flips the persisted Plan's status
   * server-side; the event carries only `plan_id` + outcome, so the
   * badge updates from the in-store copy. A stale `plan_id` (the
   * drawer already moved on to a newer Plan) is ignored.
   */
  markExecutionOutcome(
    planId: string,
    outcome: Extract<PlanStatus, 'succeeded' | 'failed' | 'aborted'>,
  ): void
}

export const usePlanDrawerStore = create<PlanDrawerState>((set) => ({
  plan: null,
  mode: 'collapsed',
  selectedNodeId: null,
  editingNodeId: null,

  showPlan: (plan) =>
    set({
      plan,
      mode: 'docked',
      // T18 emits single-node Plans — preselect so 节点信息 is on
      // screen immediately (AC: 显示单节点 + 节点信息). With a future
      // multi-node Plan this keeps the first node's info visible.
      // T27 wipes any open edit so a fresh Plan never inherits a
      // half-finished dialog from the previous one.
      selectedNodeId: plan.nodes[0]?.node_id ?? null,
      editingNodeId: null,
    }),

  collapse: () => set({ mode: 'collapsed' }),
  reopen: () => set((state) => (state.plan ? { mode: 'docked' } : state)),
  toggleFullscreen: () =>
    set((state) => ({
      mode: state.mode === 'fullscreen' ? 'docked' : 'fullscreen',
    })),
  selectNode: (nodeId) => set({ selectedNodeId: nodeId }),
  openEdit: (nodeId) => set({ editingNodeId: nodeId }),
  closeEdit: () => set({ editingNodeId: null }),
  replacePlan: (plan) => set({ plan }),

  markExecutionOutcome: (planId, outcome) =>
    set((state) =>
      state.plan && state.plan.id === planId
        ? { plan: { ...state.plan, status: outcome } }
        : state,
    ),
}))
