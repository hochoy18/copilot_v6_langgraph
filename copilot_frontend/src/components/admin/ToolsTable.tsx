import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { ApiError } from '@/lib/api-client'
import { fetchTools } from '@/lib/tools-api'
import type { Tool, ToolRiskLevel, ToolStatus } from '@/types/tool'

/**
 * Tool Registry table — T13 / #42.
 *
 * Renders every Tool the admin sees, with three orthogonal filter
 * controls wired directly into the query-param contract from
 * `GET /api/v1/admin/tools` (T12 / #11):
 *
 * - **Status filter** — `draft` / `active` / `disabled`. Maps onto
 *   the lifecycle in ADR-0018.
 * - **Risk-level filter** — `read` / `write` / `destructive`. Maps
 *   onto the HITL tiers in ADR-0004.
 * - **Name search** — case-insensitive substring against `name` /
 *   `description`. Backend already implements this; the Frontend just
 *   forwards `q`.
 *
 * Server state lives in TanStack Query (ADR-0029 mandates it).
 * `queryKey` is the (status, risk_level, q) tuple so a filter change
 * hits the cache first; the debounce on `q` keeps a keystroke burst
 * from issuing a fetch per character.
 */

const STATUS_OPTIONS: ReadonlyArray<{ value: '' | ToolStatus; label: string }> = [
  { value: '', label: '全部状态' },
  { value: 'draft', label: 'draft (待审核)' },
  { value: 'active', label: 'active (已激活)' },
  { value: 'disabled', label: 'disabled (已下架)' },
]

const RISK_OPTIONS: ReadonlyArray<{ value: '' | ToolRiskLevel; label: string }> = [
  { value: '', label: '全部风险等级' },
  { value: 'read', label: 'read' },
  { value: 'write', label: 'write' },
  { value: 'destructive', label: 'destructive' },
]

const STATUS_BADGE_CLASS: Record<ToolStatus, string> = {
  draft: 'bg-muted text-muted-foreground',
  active: 'bg-primary text-primary-foreground',
  disabled: 'bg-destructive text-destructive-foreground',
}

const RISK_BADGE_CLASS: Record<ToolRiskLevel, string> = {
  read: 'bg-secondary text-secondary-foreground',
  write: 'bg-accent text-accent-foreground',
  destructive: 'bg-destructive text-destructive-foreground',
}

/**
 * Tools keyed by the (status, risk_level, q) filter tuple. Stable
 * string form so the cache hits when the user toggles back to a
 * previous combination.
 */
function toolsQueryKey(
  status: '' | ToolStatus,
  riskLevel: '' | ToolRiskLevel,
  q: string,
): readonly unknown[] {
  return ['admin', 'tools', status, riskLevel, q] as const
}

export function ToolsTable(): React.ReactElement {
  const [statusFilter, setStatusFilter] = useState<'' | ToolStatus>('')
  const [riskFilter, setRiskFilter] = useState<'' | ToolRiskLevel>('')
  const [searchInput, setSearchInput] = useState('')
  const [committedSearch, setCommittedSearch] = useState('')

  // Debounce the search box so every keystroke doesn't issue a fetch.
  // 250ms is short enough to feel instant and long enough to coalesce
  // a burst of typing into one round trip.
  useEffect(() => {
    const handle = setTimeout(() => setCommittedSearch(searchInput.trim()), 250)
    return () => clearTimeout(handle)
  }, [searchInput])

  const query = useQuery<Tool[], ApiError>({
    queryKey: toolsQueryKey(statusFilter, riskFilter, committedSearch),
    queryFn: ({ signal }) =>
      fetchTools({
        status: statusFilter || null,
        risk_level: riskFilter || null,
        q: committedSearch,
        signal,
      }),
  })

  const tools = query.data ?? []
  const loading = query.isLoading
  const error = query.error
    ? query.error instanceof ApiError
      ? `${query.error.status}`
      : 'network'
    : null

  return (
    <section
      data-testid="tools-table"
      className="flex flex-col gap-4"
    >
      <header className="flex flex-wrap items-end gap-4">
        <h2 className="mr-auto text-xl font-semibold">Tool Registry</h2>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">状态</span>
          <select
            aria-label="状态"
            className="h-9 rounded-md border border-input bg-background px-2 text-sm"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as '' | ToolStatus)}
            data-testid="status-filter"
          >
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value || 'all'} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">风险等级</span>
          <select
            aria-label="风险等级"
            className="h-9 rounded-md border border-input bg-background px-2 text-sm"
            value={riskFilter}
            onChange={(e) => setRiskFilter(e.target.value as '' | ToolRiskLevel)}
            data-testid="risk-filter"
          >
            {RISK_OPTIONS.map((opt) => (
              <option key={opt.value || 'all'} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">搜索</span>
          <input
            type="search"
            placeholder="按名称搜索"
            aria-label="按名称搜索"
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            data-testid="search-input"
          />
        </label>
      </header>

      {error ? (
        <div
          role="alert"
          data-testid="tools-error"
          className="flex items-center rounded-md border border-destructive bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          <span>加载 Tool 列表失败 (status {error})。</span>
          <button
            type="button"
            className="ml-auto underline"
            onClick={() => {
              void query.refetch()
            }}
          >
            重试
          </button>
        </div>
      ) : null}

      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-left text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-4 py-2 font-medium">名称</th>
              <th className="px-4 py-2 font-medium">描述</th>
              <th className="px-4 py-2 font-medium">风险等级</th>
              <th className="px-4 py-2 font-medium">状态</th>
            </tr>
          </thead>
          <tbody>
            {tools.map((tool) => (
              <tr
                key={tool.id}
                data-testid={`tool-row-${tool.id}`}
                className="border-t align-top"
              >
                <td className="px-4 py-2 font-mono text-xs">{tool.name}</td>
                <td className="max-w-md px-4 py-2 text-sm text-muted-foreground">
                  {tool.description}
                </td>
                <td className="px-4 py-2">
                  <span
                    className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${RISK_BADGE_CLASS[tool.risk_level]}`}
                    data-testid={`risk-badge-${tool.id}`}
                  >
                    {tool.risk_level}
                  </span>
                </td>
                <td className="px-4 py-2">
                  <span
                    className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${STATUS_BADGE_CLASS[tool.status]}`}
                    data-testid={`status-badge-${tool.id}`}
                  >
                    {tool.status}
                  </span>
                </td>
              </tr>
            ))}
            {!loading && tools.length === 0 ? (
              <tr>
                <td
                  colSpan={4}
                  className="px-4 py-8 text-center text-sm text-muted-foreground"
                  data-testid="empty-state"
                >
                  暂无 Tool,试试调整过滤条件。
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </section>
  )
}