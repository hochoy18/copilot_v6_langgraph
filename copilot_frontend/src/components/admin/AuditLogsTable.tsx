import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'
import { ChevronDown, ChevronRight } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api-client'
import {
  fetchAuditLogsPage,
  recallAuditLog,
} from '@/lib/audit-logs-api'
import type {
  AuditLog,
  AuditLogLifecycle,
} from '@/types/audit-log'
import type { ToolRiskLevel } from '@/types/tool'

/**
 * Admin Audit Log table — T43 / #38.
 *
 * Renders every `audit_logs` row the admin sees, with four filter
 * controls wired to the query-param contract `GET
 * /api/v1/admin/audit-logs` exposes (T42 / #37):
 *
 * - **Tool name search** — case-insensitive substring match against
 *   `tool_name`.
 * - **Actor id** — exact match against `actor_id` (ObjectId string).
 * - **Lifecycle filter** — `all` / `active` / `archived` /
 *   `recalled`. Drives the 调档-button visibility — only
 *   `archived` rows expose the cold-storage recall action
 *   (ADR-0028).
 * - **Time range** — `time_from` / `time_to` bound the
 *   `occurred_at` window. Inclusive `from`, exclusive `to` so a
 *   calendar-day query maps cleanly onto the backend's
 *   `$gte` / `$lt` semantics (T42 / #37 repository). The
 *   `datetime-local` values are normalized to UTC ISO-8601 by
 *   `audit-logs-api.ts ::toUtcIso` before hitting the wire.
 *
 * Cursor pagination uses TanStack Query's `useInfiniteQuery` —
 * ADR-0031 chose cursor for forward-only scrolling. The "Load
 * more" button surfaces only when the last page reports
 * `has_more: true`; the disabled state gives the admin the same
 * "no more rows" signal the server does.
 *
 * Row click expands a detail panel that surfaces the JSON-heavy
 * fields the table can't render (`parameters`, `response`,
 * `error`, `tool_snapshot`, cold-storage metadata). Per ADR-0028
 * the "调档" button on archived rows triggers `recallAuditLog`;
 * the panel optimistically updates the row's `lifecycle_status`
 * to `recalling` and rolls back on failure so the admin gets
 * instant feedback.
 *
 * Server state lives in TanStack Query (ADR-0029). `queryKey`
 * includes the filter tuple so a filter change invalidates the
 * previous page and starts a fresh window — `placeholderData`
 * keeps the prior rows visible until the first new page lands
 * so the table doesn't flash empty on every keystroke.
 */

const LIFECYCLE_OPTIONS: ReadonlyArray<{
  value: '' | AuditLogLifecycle
  label: string
}> = [
  { value: '', label: '全部' },
  { value: 'active', label: 'active (热存)' },
  { value: 'archived', label: 'archived (冷存)' },
  { value: 'recalled', label: 'recalled (已调档)' },
]

const LIFECYCLE_BADGE_CLASS: Record<AuditLogLifecycle, string> = {
  active: 'bg-primary text-primary-foreground',
  archived: 'bg-muted text-muted-foreground',
  recalled: 'bg-accent text-accent-foreground',
}

const RISK_BADGE_CLASS: Record<ToolRiskLevel, string> = {
  read: 'bg-secondary text-secondary-foreground',
  write: 'bg-accent text-accent-foreground',
  destructive: 'bg-destructive text-destructive-foreground',
}

/**
 * Stable string form of the filter tuple so the React Query cache
 * keys cleanly. Returning `readonly unknown[]` keeps the type
 * narrow for `queryKey`.
 */
function auditLogsQueryKey(filters: AuditLogsFilterState): readonly unknown[] {
  return [
    'admin',
    'audit-logs',
    filters.toolName.trim(),
    filters.actorId.trim(),
    filters.lifecycle,
    filters.timeFrom,
    filters.timeTo,
  ] as const
}

interface AuditLogsFilterState {
  toolName: string
  actorId: string
  lifecycle: '' | AuditLogLifecycle
  timeFrom: string
  timeTo: string
}

const EMPTY_FILTERS: AuditLogsFilterState = {
  toolName: '',
  actorId: '',
  lifecycle: '',
  timeFrom: '',
  timeTo: '',
}

export function AuditLogsTable(): React.ReactElement {
  const [filters, setFilters] = useState<AuditLogsFilterState>(EMPTY_FILTERS)
  // `committedFilters` is the debounced projection the React
  // Query cache keys off — typing in the search box mutates
  // `filters` on every keystroke, but `committedFilters` only
  // advances 250ms after the burst settles, so we issue one
  // fetch per typing run instead of one per character.
  const [committedFilters, setCommittedFilters] =
    useState<AuditLogsFilterState>(EMPTY_FILTERS)
  const queryClient = useQueryClient()

  useEffect(() => {
    const handle = setTimeout(
      () => setCommittedFilters(filters),
      250,
    )
    return () => clearTimeout(handle)
  }, [filters])

  const query = useInfiniteQuery({
    queryKey: auditLogsQueryKey(committedFilters),
    queryFn: ({ signal, pageParam }) =>
      fetchAuditLogsPage({
        tool_name: committedFilters.toolName.trim() || null,
        actor_id: committedFilters.actorId.trim() || null,
        lifecycle_status: committedFilters.lifecycle || null,
        time_from: committedFilters.timeFrom || null,
        time_to: committedFilters.timeTo || null,
        cursor: (pageParam as string | null) ?? null,
        signal,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  })

  const logs = useMemo<AuditLog[]>(
    () => query.data?.pages.flatMap((page) => page.logs) ?? [],
    [query.data],
  )

  const recallMutation = useMutation<AuditLog, ApiError, string>({
    mutationFn: (auditLogId) => recallAuditLog(auditLogId),
    onSuccess: (updated) => {
      // The list query owns the cache; patch the matching row in
      // place so the detail panel flips to `recalled` without a
      // full refetch. The `pages.flatMap` indirection means we
      // walk every page until we find the row's id.
      queryClient.setQueryData(
        auditLogsQueryKey(committedFilters),
        (prev: typeof query.data) => {
          if (!prev) return prev
          return {
            ...prev,
            pages: prev.pages.map((page) => ({
              ...page,
              logs: page.logs.map((log) => (log.id === updated.id ? updated : log)),
            })),
          }
        },
      )
    },
  })

  const updateFilter = useCallback(
    <K extends keyof AuditLogsFilterState>(
      key: K,
      value: AuditLogsFilterState[K],
    ) => {
      setFilters((prev) => ({ ...prev, [key]: value }))
    },
    [],
  )

  const resetFilters = useCallback(() => setFilters(EMPTY_FILTERS), [])

  const loading = query.isLoading
  const errorMessage = query.error
    ? query.error instanceof ApiError
      ? `加载审计日志失败 (status ${query.error.status})。`
      : '网络错误, 请稍后重试。'
    : null

  return (
    <section
      data-testid="audit-logs-table"
      className="flex flex-col gap-4"
    >
      <header className="flex flex-wrap items-end gap-4">
        <h2 className="mr-auto text-xl font-semibold">审计日志</h2>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => {
            void query.refetch()
          }}
          data-testid="refresh-audit-logs"
        >
          刷新
        </Button>
      </header>

      <div
        className="flex flex-wrap items-end gap-4 rounded-md border bg-muted/20 p-4"
        role="search"
        aria-label="审计日志过滤"
      >
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Tool 名称</span>
          <input
            type="search"
            placeholder="按 Tool 名称搜索"
            aria-label="Tool 名称"
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            value={filters.toolName}
            onChange={(e) => updateFilter('toolName', e.target.value)}
            data-testid="tool-name-filter"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">调用者 ID</span>
          <input
            type="search"
            placeholder="actor_id"
            aria-label="actor_id"
            className="h-9 rounded-md border border-input bg-background px-3 text-sm font-mono"
            value={filters.actorId}
            onChange={(e) => updateFilter('actorId', e.target.value)}
            data-testid="actor-filter"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">生命周期</span>
          <select
            aria-label="生命周期"
            className="h-9 rounded-md border border-input bg-background px-2 text-sm"
            value={filters.lifecycle}
            onChange={(e) =>
              updateFilter('lifecycle', e.target.value as '' | AuditLogLifecycle)
            }
            data-testid="lifecycle-filter"
          >
            {LIFECYCLE_OPTIONS.map((opt) => (
              <option key={opt.value || 'all'} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">起始时间</span>
          <input
            type="datetime-local"
            aria-label="起始时间"
            className="h-9 rounded-md border border-input bg-background px-2 text-sm"
            value={filters.timeFrom}
            onChange={(e) => updateFilter('timeFrom', e.target.value)}
            data-testid="time-from-filter"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">结束时间</span>
          <input
            type="datetime-local"
            aria-label="结束时间"
            className="h-9 rounded-md border border-input bg-background px-2 text-sm"
            value={filters.timeTo}
            onChange={(e) => updateFilter('timeTo', e.target.value)}
            data-testid="time-to-filter"
          />
        </label>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={resetFilters}
          data-testid="reset-filters"
        >
          重置过滤
        </Button>
      </div>

      {errorMessage ? (
        <div
          role="alert"
          data-testid="audit-logs-error"
          className="flex items-center rounded-md border border-destructive bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          <span>{errorMessage}</span>
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

      {recallMutation.isError ? (
        <div
          role="alert"
          data-testid="recall-error"
          className="flex items-center rounded-md border border-destructive bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          <span>调档失败: {recallMutation.error.message}</span>
          <button
            type="button"
            className="ml-auto underline"
            onClick={() => recallMutation.reset()}
          >
            关闭
          </button>
        </div>
      ) : null}

      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-left text-xs uppercase text-muted-foreground">
            <tr>
              <th className="w-8 px-2 py-2" aria-label="展开" />
              <th className="px-4 py-2 font-medium">发生时间</th>
              <th className="px-4 py-2 font-medium">Tool</th>
              <th className="px-4 py-2 font-medium">风险等级</th>
              <th className="px-4 py-2 font-medium">调用结果</th>
              <th className="px-4 py-2 font-medium">生命周期</th>
              <th className="px-4 py-2 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((log) => (
              <AuditLogRow
                key={log.id}
                log={log}
                recallPending={recallMutation.isPending && recallMutation.variables === log.id}
                onRecall={(id) => {
                  recallMutation.mutate(id)
                }}
              />
            ))}
            {!loading && logs.length === 0 ? (
              <tr>
                <td
                  colSpan={7}
                  className="px-4 py-8 text-center text-sm text-muted-foreground"
                  data-testid="empty-state"
                >
                  暂无审计日志,试试调整过滤条件。
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-end gap-3 text-sm">
        <span className="text-muted-foreground">
          已加载 {logs.length} 条
        </span>
        {query.hasNextPage ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={query.isFetchingNextPage}
            onClick={() => {
              void query.fetchNextPage()
            }}
            data-testid="load-more"
          >
            {query.isFetchingNextPage ? '加载中…' : '加载更多'}
          </Button>
        ) : logs.length > 0 ? (
          <span
            data-testid="end-of-list"
            className="text-xs text-muted-foreground"
          >
            已加载全部
          </span>
        ) : null}
      </div>
    </section>
  )
}

interface AuditLogRowProps {
  log: AuditLog
  recallPending: boolean
  onRecall: (id: string) => void
}

function AuditLogRow({
  log,
  recallPending,
  onRecall,
}: AuditLogRowProps): React.ReactElement {
  const [expanded, setExpanded] = useState(false)

  const toggle = useCallback(() => setExpanded((prev) => !prev), [])

  return (
    <>
      <tr
        data-testid={`audit-log-row-${log.id}`}
        className="border-t align-top hover:bg-muted/30"
      >
        <td className="px-2 py-2 align-middle">
          <button
            type="button"
            aria-label={expanded ? '收起详情' : '展开详情'}
            aria-expanded={expanded}
            aria-controls={`audit-log-detail-${log.id}`}
            onClick={toggle}
            className="rounded p-1 hover:bg-muted"
            data-testid={`audit-log-toggle-${log.id}`}
          >
            {expanded ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </button>
        </td>
        <td className="px-4 py-2 font-mono text-xs">
          {formatOccurredAt(log.occurred_at)}
        </td>
        <td className="px-4 py-2 font-mono text-xs">{log.tool_name}</td>
        <td className="px-4 py-2">
          <span
            className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${RISK_BADGE_CLASS[log.risk_level]}`}
            data-testid={`risk-badge-${log.id}`}
          >
            {log.risk_level}
          </span>
        </td>
        <td className="px-4 py-2">
          <span
            data-testid={`status-badge-${log.id}`}
            className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${callStatusClass(log.status)}`}
          >
            {log.status}
          </span>
        </td>
        <td className="px-4 py-2">
          <span
            data-testid={`lifecycle-badge-${log.id}`}
            className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${LIFECYCLE_BADGE_CLASS[log.lifecycle_status]}`}
          >
            {log.lifecycle_status}
          </span>
        </td>
        <td className="px-4 py-2">
          {log.lifecycle_status === 'archived' ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={recallPending}
              onClick={() => onRecall(log.id)}
              data-testid={`recall-${log.id}`}
            >
              {recallPending ? '调档中…' : '调档'}
            </Button>
          ) : null}
        </td>
      </tr>
      {expanded ? (
        <tr
          id={`audit-log-detail-${log.id}`}
          data-testid={`audit-log-detail-${log.id}`}
          className="border-t bg-muted/20"
        >
          <td colSpan={7} className="px-6 py-4">
            <AuditLogDetail log={log} />
          </td>
        </tr>
      ) : null}
    </>
  )
}

function AuditLogDetail({ log }: { log: AuditLog }): React.ReactElement {
  return (
    <div className="grid gap-3 text-xs sm:grid-cols-2">
      <DetailField label="audit log id" value={log.id} mono />
      <DetailField label="actor_id" value={log.actor_id} mono />
      <DetailField label="conversation_id" value={log.conversation_id} mono />
      <DetailField label="turn_id" value={log.turn_id} mono />
      <DetailField label="plan_id" value={log.plan_id} mono />
      <DetailField
        label="plan_execution_id"
        value={log.plan_execution_id ?? '—'}
        mono
      />
      <DetailField label="retry_count" value={String(log.retry_count)} />
      <DetailField
        label="cold_storage_ref"
        value={log.cold_storage_ref ?? '—'}
        mono
      />
      <DetailField
        label="cold_archived_at"
        value={log.cold_archived_at ? formatOccurredAt(log.cold_archived_at) : '—'}
      />
      <DetailBlock
        label="tool_snapshot"
        value={log.tool_snapshot}
        testId={`tool-snapshot-${log.id}`}
      />
      <DetailBlock
        label="parameters"
        value={log.parameters}
        testId={`parameters-${log.id}`}
      />
      <DetailBlock
        label="response"
        value={log.response}
        testId={`response-${log.id}`}
      />
      <DetailBlock
        label="error"
        value={log.error}
        testId={`error-${log.id}`}
      />
    </div>
  )
}

interface DetailFieldProps {
  label: string
  value: string
  mono?: boolean
}

function DetailField({ label, value, mono = false }: DetailFieldProps): React.ReactElement {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-muted-foreground">{label}</span>
      <span className={mono ? 'font-mono text-xs' : 'text-xs'}>{value}</span>
    </div>
  )
}

interface DetailBlockProps {
  label: string
  value: unknown
  testId?: string
}

function DetailBlock({
  label,
  value,
  testId,
}: DetailBlockProps): React.ReactElement {
  const formatted =
    value === null || value === undefined ? '—' : JSON.stringify(value, null, 2)
  return (
    <div className="flex flex-col gap-0.5 sm:col-span-2">
      <span className="text-muted-foreground">{label}</span>
      <pre
        data-testid={testId}
        className="max-h-48 overflow-auto rounded bg-background p-2 font-mono text-[11px] leading-snug"
      >
        {formatted}
      </pre>
    </div>
  )
}

/** CSS class for the per-call status badge. */
function callStatusClass(status: AuditLog['status']): string {
  if (status === 'succeeded') return 'bg-primary text-primary-foreground'
  if (status === 'failed') return 'bg-destructive text-destructive-foreground'
  if (status === 'skipped') return 'bg-secondary text-secondary-foreground'
  return 'bg-muted text-muted-foreground'
}

/**
 * ISO timestamp → compact `YYYY-MM-DD HH:mm:ss` for the table.
 * Falls back to the raw string when `Date` parsing fails so a
 * backend schema surprise can't blank the column.
 */
function formatOccurredAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
