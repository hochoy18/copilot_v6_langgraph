/**
 * Wire types for the Tool Registry — T13 / #42.
 *
 * Mirrors `app.api.admin_tools.ToolResponse` from the backend. The
 * literal unions pin the admin UI to the same vocabulary the Planner
 * uses (ADR-0004 / ADR-0018): a new `ToolStatus` or `ToolRiskLevel`
 * value is a backend schema change first, frontend type second.
 */

export type ToolStatus = 'draft' | 'active' | 'disabled'

export type ToolRiskLevel = 'read' | 'write' | 'destructive'

/**
 * Single Tool row, as returned by `GET /api/v1/admin/tools`. Field names
 * match `ToolResponse` 1:1 so `JSON.parse` of a backend payload assigns
 * directly without a mapping layer.
 */
export interface Tool {
  id: string
  name: string
  description: string
  risk_level: ToolRiskLevel
  status: ToolStatus
  parameters_schema: Record<string, unknown>
  http_method: string
  http_url_template: string
  http_headers: Record<string, string>
  http_body_template: Record<string, unknown> | null
  source: string
  source_ref: string | null
  credentials_ref: string | null
  created_at: string
  updated_at: string
}

/** Envelope of `GET /api/v1/admin/tools`. */
export interface ToolListResponse {
  tools: Tool[]
}