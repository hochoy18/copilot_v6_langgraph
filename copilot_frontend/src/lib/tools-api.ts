/**
 * Admin Tool Registry API client — T13 / #42.
 *
 * Thin facade over `apiFetch` that maps the URL query-param contract
 * the backend exposes (`status`, `risk_level`, `q`, `limit`). Empty
 * strings and `null` filter values are stripped so the URL never
 * carries `?status=` (which the backend would treat as "filter on the
 * empty string" — fine, but pointless noise in the network panel).
 *
 * Defaults `limit` to the backend's `MAX_LIST_LIMIT` (200) so the
 * Registry UI satisfies the T13 acceptance criterion "渲染所有 Tool"
 * — without it, the default 50 silently truncates the view once a
 * tenant exceeds the cap.
 */
import { apiFetch } from '@/lib/api-client'
import type { Tool, ToolListResponse, ToolRiskLevel, ToolStatus } from '@/types/tool'

export interface FetchToolsParams {
  status?: ToolStatus | null
  risk_level?: ToolRiskLevel | null
  q?: string
  limit?: number
  signal?: AbortSignal
}

/**
 * List Tools with optional status / risk-level / free-text filters.
 * Mirrors `GET /api/v1/admin/tools` per ADR-0031.
 */
export async function fetchTools(params: FetchToolsParams = {}): Promise<Tool[]> {
  const search = new URLSearchParams()
  if (params.status) search.set('status', params.status)
  if (params.risk_level) search.set('risk_level', params.risk_level)
  const trimmed = params.q?.trim()
  if (trimmed) search.set('q', trimmed)
  const limit = params.limit ?? 200
  search.set('limit', String(limit))
  const path = `/admin/tools?${search.toString()}`
  const init: RequestInit = params.signal ? { signal: params.signal } : {}
  const body = await apiFetch<ToolListResponse>(path, init)
  return body.tools
}