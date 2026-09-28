/**
 * Wire types for Plans / Turns — T19 / #17.
 *
 * Mirrors the backend `app.db.schemas` T17 shape (`nodes` / `edges` /
 * `tool_snapshots`, ADR-0027) plus the `TurnResponse` envelope of
 * `POST /api/v1/conversations/{id}/turns` (T18 / ADR-0031). Field
 * names match the Pydantic models 1:1 so a parsed response assigns
 * directly without a mapping layer — same convention as
 * `types/tool.ts`.
 */
import type { ToolRiskLevel } from '@/types/tool'

export type { ToolRiskLevel }

/** Plan lifecycle (ADR-0004 / ADR-0019); mirrors backend `PlanStatus`. */
export type PlanStatus =
  | 'pending'
  | 'approved'
  | 'modified'
  | 'rejected'
  | 'executing'
  | 'succeeded'
  | 'failed'
  | 'aborted'

/**
 * Frozen Tool definition embedded in a Plan (ADR-0027). Mirrors
 * backend `ToolSnapshot` minus runtime-only noise — the React Flow
 * renderer reads `description` / `risk_level` / `http_*` straight off
 * this for the node card and the node info panel.
 */
export interface ToolSnapshot {
  tool_id: string | null
  name: string
  description: string
  risk_level: ToolRiskLevel
  parameters_schema: Record<string, unknown>
  http_method: string
  http_url_template: string
  http_headers: Record<string, string>
  http_body_template: Record<string, unknown> | null
}

/** One Tool invocation (backend `PlanNode`). */
export interface PlanNode {
  node_id: string
  tool: string
  parameters: Record<string, unknown>
  notes: string
}

/** Dependency pair; `{source, target}` is React Flow's own edge shape. */
export interface PlanEdge {
  source: string
  target: string
}

/** Persisted Plan document as returned by the API (`plan.model_dump`). */
export interface Plan {
  id: string
  conversation_id: string
  turn_id: string
  status: PlanStatus
  nodes: PlanNode[]
  edges: PlanEdge[]
  tool_snapshots: ToolSnapshot[]
  edited_diff: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

/** One chat turn row (backend `Turn.model_dump(mode="json")`). */
export interface Turn {
  id: string
  conversation_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  plan_id: string | null
  created_at: string
}

/** Envelope of `POST /api/v1/conversations/{id}/turns` (T18 / #16). */
export interface TurnResponse {
  turn: Turn
  /** The pending Plan awaiting HITL preview, or null (smalltalk / degraded). */
  plan: Plan | null
  /** Non-fatal notes explaining a missing / partial Plan. */
  warnings: string[]
}
