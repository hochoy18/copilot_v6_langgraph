/**
 * Wire types for the admin Audit Log UI — T43 / #38.
 *
 * Mirrors `app.db.schemas.AuditLog` from the backend (T06 / #7 +
 * T42 / #37). The admin surface cares about three extra fields
 * beyond the canonical row:
 *
 * - `parameters` / `response` / `error` — the live call envelope,
 *   rendered as JSON in the row-expansion panel.
 * - `tool_snapshot` — frozen Tool definition at call time
 *   (ADR-0027), so the admin sees "which version of `list_orders`
 *   ran on 2026-04-12" without joining the live `tools` table.
 * - `lifecycle_status` / `cold_storage_ref` / `cold_archived_at`
 *   — ADR-0028 retention metadata. The "调档" button only renders
 *   when `lifecycle_status === 'archived'`.
 *
 * Names match `AuditLog` 1:1 so the JSON payload assigns directly
 * without a mapping layer; any backend field rename is a typecheck
 * failure on this file before the page breaks.
 */
import type { ToolRiskLevel } from '@/types/tool'

/** Per-call outcome — mirrors the backend's `status` literal. */
export type AuditLogCallStatus = 'running' | 'succeeded' | 'failed' | 'skipped'

/**
 * ADR-0028 retention lifecycle — `active` is the hot tier,
 * `archived` is in cold storage (调档-eligible), `recalled` has
 * been hydrated back to hot.
 */
export type AuditLogLifecycle = 'active' | 'archived' | 'recalled'

/**
 * Frozen Tool definition per ADR-0027. The admin sees this in the
 * row-expansion panel so a "which version?" question never has to
 * join the live `tools` collection.
 *
 * Mirrors `app.db.schemas.ToolSnapshot` 1:1 — `tool_id` is the
 * pointer to the live row (nullable for hand-built / older
 * snapshots), and the `http_*` fields are the request template as
 * it stood at freeze time.
 */
export interface AuditLogToolSnapshot {
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

export interface AuditLog {
  id: string
  actor_id: string
  conversation_id: string
  turn_id: string
  plan_id: string
  plan_execution_id: string | null
  tool_name: string
  tool_snapshot: AuditLogToolSnapshot
  parameters: Record<string, unknown>
  response: Record<string, unknown> | null
  status: AuditLogCallStatus
  error: Record<string, unknown> | null
  risk_level: ToolRiskLevel
  retry_count: number
  occurred_at: string
  lifecycle_status: AuditLogLifecycle
  cold_storage_ref: string | null
  cold_archived_at: string | null
}

/**
 * Envelope of `GET /api/v1/admin/audit-logs`. Cursor pagination
 * fields (`next_cursor`, `has_more`) match the convention noted
 * in ADR-0031 ("列表分页规范 … 选用 cursor, 前端无限滚动友好").
 * `next_cursor` is the canonical end-of-list signal the UI reads
 * (via `getNextPageParam`); `has_more` mirrors it server-side so a
 * consumer can check either without decoding the opaque cursor.
 */
export interface AuditLogListResponse {
  logs: AuditLog[]
  next_cursor: string | null
  has_more: boolean
}
