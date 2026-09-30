/**
 * Admin Audit Log API client — T43 / #38.
 *
 * Thin facade over `apiFetch` for the two endpoints ADR-0031
 * assigns to the audit-log surface:
 *
 * - `GET    /api/v1/admin/audit-logs`               — filterable list
 *                                                     with cursor
 *                                                     pagination.
 * - `POST   /api/v1/admin/audit-logs/{id}/recall`   — trigger
 *                                                     cold-storage
 *                                                     hydration
 *                                                     (ADR-0028).
 *
 * Filter values map 1:1 onto the query-param contract the backend
 * will expose (T42 / #37 lays the matching repository; the route
 * follows). Empty / `null` filters are stripped so the URL never
 * carries `?actor_id=` noise, mirroring `tools-api.ts`.
 */
import { apiFetch } from '@/lib/api-client'
import type {
  AuditLog,
  AuditLogLifecycle,
  AuditLogListResponse,
} from '@/types/audit-log'

export interface FetchAuditLogsParams {
  tool_name?: string | null
  actor_id?: string | null
  lifecycle_status?: AuditLogLifecycle | null
  time_from?: string | null
  time_to?: string | null
  cursor?: string | null
  limit?: number
  signal?: AbortSignal
}

const DEFAULT_LIMIT = 50

/**
 * Build the query string for the audit-log list endpoint.
 * Pulled out so `fetchAuditLogs` and the test can both inspect the
 * exact URL contract — `URLSearchParams.toString()`'s encoding is
 * the source of truth for which keys reach the wire.
 */
function buildAuditLogsSearch(params: FetchAuditLogsParams): string {
  const search = new URLSearchParams()
  const toolName = params.tool_name?.trim()
  if (toolName) search.set('tool_name', toolName)
  const actorId = params.actor_id?.trim()
  if (actorId) search.set('actor_id', actorId)
  if (params.lifecycle_status) {
    search.set('lifecycle_status', params.lifecycle_status)
  }
  if (params.time_from) search.set('time_from', params.time_from)
  if (params.time_to) search.set('time_to', params.time_to)
  if (params.cursor) search.set('cursor', params.cursor)
  const limit = params.limit ?? DEFAULT_LIMIT
  search.set('limit', String(limit))
  return search.toString()
}

/**
 * One page of audit logs. The caller is responsible for chaining
 * `cursor` through subsequent pages — `useInfiniteQuery` in the
 * page does this for the Frontend.
 */
export async function fetchAuditLogsPage(
  params: FetchAuditLogsParams = {},
): Promise<AuditLogListResponse> {
  const query = buildAuditLogsSearch(params)
  const path = `/admin/audit-logs?${query}`
  const init: RequestInit = params.signal ? { signal: params.signal } : {}
  return apiFetch<AuditLogListResponse>(path, init)
}

/**
 * Trigger cold-storage hydration for one archived audit row.
 *
 * The backend (T42) is responsible for the 5-minute SLO; the
 * frontend only kicks the call. Returns the row in its post-recall
 * state — typically `lifecycle_status === 'recalled'` once the
 * cold-storage hydration completes, though the backend may also
 * report `recalling` mid-flight on a future ticket.
 */
export async function recallAuditLog(
  auditLogId: string,
  signal?: AbortSignal,
): Promise<AuditLog> {
  const path = `/admin/audit-logs/${encodeURIComponent(auditLogId)}/recall`
  const init: RequestInit = { method: 'POST' }
  if (signal) init.signal = signal
  return apiFetch<AuditLog>(path, init)
}
