import { useCallback, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api-client'
import {
  createTool,
  draftToCreateBody,
  importOpenAPI,
  setToolStatus,
} from '@/lib/openapi-import-api'
import type { ToolRiskLevel } from '@/types/tool'
import type {
  ImportOpenAPIResponse,
  ToolDraft,
} from '@/types/openapi-import'

/**
 * OpenAPI import preview — T15 / #13.
 *
 * Two-source UI (per ADR-0003 / ADR-0029):
 *
 * - **Upload file** — `<input type="file">` reads the text and POSTs
 *   it inline as `spec` (JSON) or `spec_yaml` (raw). The T14 backend
 *   handles the parse; this component never re-implements it.
 *
 * - **Paste URL** — textbox + "Fetch" button. The browser fetches
 *   the URL via `fetch()`, then the resulting text flows through the
 *   same `importOpenAPI` path. T14 explicitly defers remote-fetch
 *   from the backend (the field isn't on the wire), so the frontend
 *   owns URL fetching. Future move-to-backend can swap this seam
 *   without touching the rest of the component.
 *
 * Once a parse succeeds the component renders one row per draft.
 * Each row tracks its own local lifecycle (`idle` → `activating` →
 * `active` | `error`), keyed off the draft's `operation_ref` so the
 * row stays stable even if the admin edits the `name` field.
 *
 * Per-draft actions:
 * - **Activate** — `createTool` (lands in `draft` per ADR-0018)
 *   followed by `setToolStatus('active')`. The two-step pattern
 *   matches the canonical lifecycle; future tickets (T16 description
 *   generation, audit hook) sit between these two calls.
 * - **Discard** — local-state removal. Nothing is sent to the
 *   server, matching the "no implicit persistence" rule from
 *   ADR-0003.
 *
 * Server state for the preview itself is plain `useState` rather
 * than TanStack Query: the parsed drafts are scoped to this page
 * (the Registry table is the canonical source of truth for
 * persisted Tools), and the activate flow only needs per-row
 * status — neither needs cross-component caching.
 */

type SourceTab = 'file' | 'url'

type DraftStatus =
  | { kind: 'idle' }
  | { kind: 'activating' }
  | { kind: 'active'; toolId: string }
  | { kind: 'error'; message: string }

interface PreviewState {
  title: string | null
  version: string | null
  serverUrl: string | null
  sourceFormat: 'json' | 'yaml'
  drafts: ToolDraft[]
  statuses: Record<string, DraftStatus>
}

const INITIAL_STATE: PreviewState = {
  title: null,
  version: null,
  serverUrl: null,
  sourceFormat: 'json',
  drafts: [],
  statuses: {},
}

const RISK_OPTIONS: ReadonlyArray<{ value: ToolRiskLevel; label: string }> = [
  { value: 'read', label: 'read' },
  { value: 'write', label: 'write' },
  { value: 'destructive', label: 'destructive' },
]

export function OpenAPIImport(): React.ReactElement {
  const [tab, setTab] = useState<SourceTab>('file')
  const [urlInput, setUrlInput] = useState('')
  const [preview, setPreview] = useState<PreviewState>(INITIAL_STATE)
  const [parsing, setParsing] = useState(false)
  const [parseError, setParseError] = useState<string | null>(null)
  const fetchControllerRef = useRef<AbortController | null>(null)

  /**
   * Common path: take the parsed `ImportOpenAPIResponse` and shape
   * it into local state. Pulled out so both the file-upload path
   * and the URL-fetch path land the same way.
   */
  const applyPreview = useCallback((response: ImportOpenAPIResponse) => {
    setPreview({
      title: response.title,
      version: response.version,
      serverUrl: response.server_url,
      sourceFormat: response.source_format,
      drafts: response.drafts,
      statuses: Object.fromEntries(
        response.drafts.map((draft) => [draft.operation_ref, { kind: 'idle' }]),
      ),
    })
    setParseError(null)
  }, [])

  /**
   * Run `importOpenAPI` against `text`. Both source tabs funnel
   * through here so error handling and cancellation are uniform.
   *
   * The "request id" guard (`current === controller` after the await)
   * protects against a slow / mocked fetch that ignores the abort
   * signal: even if the response eventually resolves, we only apply
   * it if this call is still the live one. The signal itself is the
   * primary cancellation path for real `fetch`; this guard is the
   * belt-and-braces fallback for tests and servers that ignore
   * `AbortSignal`.
   */
  const runParse = useCallback(
    async (text: string) => {
      const controller = new AbortController()
      fetchControllerRef.current?.abort()
      fetchControllerRef.current = controller
      setParsing(true)
      setParseError(null)
      try {
        const response = await importOpenAPI({ text, signal: controller.signal })
        if (fetchControllerRef.current !== controller) return
        applyPreview(response)
      } catch (err) {
        if (fetchControllerRef.current !== controller) return
        if (err instanceof DOMException && err.name === 'AbortError') {
          return
        }
        setParseError(formatImportError(err))
        setPreview(INITIAL_STATE)
      } finally {
        if (fetchControllerRef.current === controller) {
          fetchControllerRef.current = null
          setParsing(false)
        }
      }
    },
    [applyPreview],
  )

  const handleFileChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0]
      if (!file) return
      // Read via FileReader so the same code path works in jsdom
      // (which lacks `Blob.text()`) and every browser. Falling back
      // to `file.text()` if FileReader is absent would silently break
      // tests; keep the seam explicit.
      const reader = new FileReader()
      reader.onload = () => {
        const text = typeof reader.result === 'string' ? reader.result : ''
        void runParse(text)
      }
      reader.readAsText(file)
      // Reset so picking the same file twice fires another change event.
      event.target.value = ''
    },
    [runParse],
  )

  const handleFetchUrl = useCallback(async () => {
    const trimmed = urlInput.trim()
    if (!trimmed) return
    const controller = new AbortController()
    fetchControllerRef.current?.abort()
    fetchControllerRef.current = controller
    setParsing(true)
    setParseError(null)
    try {
      const response = await fetch(trimmed, { signal: controller.signal })
      if (fetchControllerRef.current !== controller) return
      if (!response.ok) {
        throw new Error(
          `远程地址返回 ${response.status} ${response.statusText}。`,
        )
      }
      const text = await response.text()
      if (fetchControllerRef.current !== controller) return
      await runParse(text)
    } catch (err) {
      if (fetchControllerRef.current !== controller) return
      if (err instanceof DOMException && err.name === 'AbortError') {
        return
      }
      setParseError(formatImportError(err))
      setPreview(INITIAL_STATE)
    } finally {
      if (fetchControllerRef.current === controller) {
        fetchControllerRef.current = null
        setParsing(false)
      }
    }
  }, [runParse, urlInput])

  /**
   * Patch the editable fields on one draft. The preview list reuses
   * the same array reference but with the matching entry replaced,
   * so React keeps the row mounted (and its activation status) on
   * every keystroke.
   */
  const updateDraft = useCallback(
    (operationRef: string, patch: Partial<ToolDraft>) => {
      setPreview((prev) => ({
        ...prev,
        drafts: prev.drafts.map((draft) =>
          draft.operation_ref === operationRef ? { ...draft, ...patch } : draft,
        ),
      }))
    },
    [],
  )

  const discardDraft = useCallback((operationRef: string) => {
    setPreview((prev) => ({
      ...prev,
      drafts: prev.drafts.filter((d) => d.operation_ref !== operationRef),
      statuses: Object.fromEntries(
        Object.entries(prev.statuses).filter(([key]) => key !== operationRef),
      ),
    }))
  }, [])

  const resetAll = useCallback(() => {
    setPreview(INITIAL_STATE)
    setParseError(null)
  }, [])

  const activateDraft = useCallback(
    async (operationRef: string) => {
      const draft = preview.drafts.find((d) => d.operation_ref === operationRef)
      if (!draft) return
      setPreview((prev) => ({
        ...prev,
        statuses: { ...prev.statuses, [operationRef]: { kind: 'activating' } },
      }))
      try {
        const created = await createTool({ body: draftToCreateBody(draft) })
        await setToolStatus({ id: created.id, status: 'active' })
        setPreview((prev) => ({
          ...prev,
          statuses: {
            ...prev.statuses,
            [operationRef]: { kind: 'active', toolId: created.id },
          },
        }))
      } catch (err) {
        setPreview((prev) => ({
          ...prev,
          statuses: {
            ...prev.statuses,
            [operationRef]: { kind: 'error', message: formatImportError(err) },
          },
        }))
      }
    },
    [preview.drafts],
  )

  const hasPreview = preview.drafts.length > 0

  return (
    <section
      data-testid="openapi-import"
      className="flex flex-col gap-6"
    >
      <header className="flex flex-wrap items-end gap-4">
        <h2 className="mr-auto text-xl font-semibold">OpenAPI 导入</h2>
        {hasPreview ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={resetAll}
            data-testid="reset-preview"
          >
            重新导入
          </Button>
        ) : null}
      </header>

      <div
        role="tablist"
        aria-label="导入来源"
        className="flex gap-2 border-b"
      >
        <SourceTabButton
          active={tab === 'file'}
          onClick={() => setTab('file')}
          testId="tab-file"
        >
          上传文件
        </SourceTabButton>
        <SourceTabButton
          active={tab === 'url'}
          onClick={() => setTab('url')}
          testId="tab-url"
        >
          粘贴 URL
        </SourceTabButton>
      </div>

      {tab === 'file' ? (
        <div className="flex flex-col gap-2">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">OpenAPI 文件 (.json / .yaml / .yml)</span>
            <input
              type="file"
              accept=".json,.yaml,.yml,application/json,application/yaml,text/yaml"
              onChange={handleFileChange}
              disabled={parsing}
              data-testid="file-input"
              className="block w-full text-sm file:mr-3 file:rounded-md file:border-0 file:bg-primary file:px-3 file:py-1.5 file:text-primary-foreground"
            />
          </label>
          <p className="text-xs text-muted-foreground">
            支持 OpenAPI 3.x(JSON 或 YAML)。文件读取后以内联方式发送到后端,不离开浏览器以外的网络。
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">OpenAPI 文档 URL</span>
            <input
              type="url"
              placeholder="https://petstore3.example.com/openapi.json"
              value={urlInput}
              onChange={(e) => setUrlInput(e.target.value)}
              disabled={parsing}
              data-testid="url-input"
              className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            />
          </label>
          <div>
            <Button
              type="button"
              onClick={() => {
                void handleFetchUrl()
              }}
              disabled={parsing || !urlInput.trim()}
              data-testid="fetch-url"
            >
              {parsing ? '拉取中…' : '拉取并解析'}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            浏览器拉取文档后以内联方式发送到后端解析 — CORS 不通时可下载后改用"上传文件"。
          </p>
        </div>
      )}

      {parsing ? (
        <p
          data-testid="parsing-state"
          className="text-sm text-muted-foreground"
        >
          解析中…
        </p>
      ) : null}

      {parseError ? (
        <div
          role="alert"
          data-testid="parse-error"
          className="flex items-center rounded-md border border-destructive bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          <span>{parseError}</span>
        </div>
      ) : null}

      {hasPreview ? <PreviewPanel
        preview={preview}
        onUpdateDraft={updateDraft}
        onDiscard={discardDraft}
        onActivate={(operationRef) => {
          void activateDraft(operationRef)
        }}
      /> : null}
    </section>
  )
}

function SourceTabButton({
  active,
  onClick,
  children,
  testId,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
  testId: string
}): React.ReactElement {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      data-testid={testId}
      className={`-mb-px border-b-2 px-4 py-2 text-sm transition-colors ${
        active
          ? 'border-primary text-foreground'
          : 'border-transparent text-muted-foreground hover:text-foreground'
      }`}
    >
      {children}
    </button>
  )
}

interface PreviewPanelProps {
  preview: PreviewState
  onUpdateDraft: (operationRef: string, patch: Partial<ToolDraft>) => void
  onDiscard: (operationRef: string) => void
  onActivate: (operationRef: string) => void
}

function PreviewPanel({
  preview,
  onUpdateDraft,
  onDiscard,
  onActivate,
}: PreviewPanelProps): React.ReactElement {
  return (
    <div className="flex flex-col gap-4" data-testid="preview-panel">
      <PreviewHeader preview={preview} />
      <ul className="flex flex-col gap-3">
        {preview.drafts.map((draft) => (
          <DraftRow
            key={draft.operation_ref}
            draft={draft}
            status={preview.statuses[draft.operation_ref] ?? { kind: 'idle' }}
            onUpdate={onUpdateDraft}
            onDiscard={onDiscard}
            onActivate={onActivate}
          />
        ))}
      </ul>
    </div>
  )
}

function PreviewHeader({
  preview,
}: {
  preview: PreviewState
}): React.ReactElement {
  return (
    <div
      data-testid="preview-header"
      className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/30 px-4 py-3 text-sm"
    >
      <span className="font-semibold">{preview.title ?? '(未命名 API)'}</span>
      {preview.version ? (
        <span className="text-muted-foreground">v{preview.version}</span>
      ) : null}
      {preview.serverUrl ? (
        <span className="rounded bg-secondary px-2 py-0.5 font-mono text-xs text-secondary-foreground">
          {preview.serverUrl}
        </span>
      ) : null}
      <span className="ml-auto rounded bg-accent px-2 py-0.5 text-xs uppercase text-accent-foreground">
        {preview.sourceFormat}
      </span>
      <span
        data-testid="draft-count"
        className="text-xs text-muted-foreground"
      >
        共 {preview.drafts.length} 个 draft
      </span>
    </div>
  )
}

interface DraftRowProps {
  draft: ToolDraft
  status: DraftStatus
  onUpdate: (operationRef: string, patch: Partial<ToolDraft>) => void
  onDiscard: (operationRef: string) => void
  onActivate: (operationRef: string) => void
}

function DraftRow({
  draft,
  status,
  onUpdate,
  onDiscard,
  onActivate,
}: DraftRowProps): React.ReactElement {
  const disabled = status.kind === 'activating' || status.kind === 'active'
  return (
    <li
      data-testid={`draft-row-${draft.operation_ref}`}
      className="flex flex-col gap-3 rounded-md border p-4"
    >
      <div className="flex flex-wrap items-center gap-3">
        <span className="rounded bg-primary px-2 py-0.5 font-mono text-xs text-primary-foreground">
          {draft.http_method}
        </span>
        <span className="font-mono text-sm">{draft.http_url_template}</span>
        <span className="ml-auto text-xs text-muted-foreground">
          {draft.operation_ref}
        </span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-muted-foreground">名称 (Tool slug)</span>
          <input
            type="text"
            value={draft.name}
            disabled={disabled}
            onChange={(e) =>
              onUpdate(draft.operation_ref, { name: e.target.value })
            }
            data-testid={`draft-name-${draft.operation_ref}`}
            className="h-8 rounded-md border border-input bg-background px-2 font-mono text-sm disabled:opacity-60"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-muted-foreground">风险等级</span>
          <select
            value={draft.risk_level}
            disabled={disabled}
            onChange={(e) =>
              onUpdate(draft.operation_ref, {
                risk_level: e.target.value as ToolRiskLevel,
              })
            }
            data-testid={`draft-risk-${draft.operation_ref}`}
            className="h-8 rounded-md border border-input bg-background px-2 text-sm disabled:opacity-60"
          >
            {RISK_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="max-w-prose text-sm text-muted-foreground">
        {draft.description}
      </p>
      {draft.warnings.length > 0 ? (
        <div
          data-testid={`draft-warnings-${draft.operation_ref}`}
          className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-900 dark:text-amber-200"
        >
          <p className="font-semibold">{draft.warnings.length} 个 warning:</p>
          <ul className="mt-1 list-disc pl-5">
            {draft.warnings.map((warning, idx) => (
              <li key={idx}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          disabled={disabled}
          onClick={() => onActivate(draft.operation_ref)}
          data-testid={`draft-activate-${draft.operation_ref}`}
        >
          {status.kind === 'activating'
            ? '激活中…'
            : status.kind === 'active'
              ? '已激活'
              : '激活'}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled}
          onClick={() => onDiscard(draft.operation_ref)}
          data-testid={`draft-discard-${draft.operation_ref}`}
        >
          弃用
        </Button>
        <DraftStatus status={status} />
      </div>
    </li>
  )
}

function DraftStatus({ status }: { status: DraftStatus }): React.ReactElement | null {
  if (status.kind === 'idle') return null
  if (status.kind === 'activating') {
    return (
      <span
        data-testid="draft-status-activating"
        className="text-xs text-muted-foreground"
      >
        正在创建并激活…
      </span>
    )
  }
  if (status.kind === 'active') {
    return (
      <span
        data-testid="draft-status-active"
        className="text-xs text-primary"
      >
        已持久化 (id {status.toolId})
      </span>
    )
  }
  return (
    <span
      role="alert"
      data-testid="draft-status-error"
      className="text-xs text-destructive"
    >
      激活失败:{status.message}
    </span>
  )
}

/**
 * Render a one-line summary of any error thrown during parse /
 * fetch / activate. `ApiError` carries a status + parsed body;
 * other errors fall back to `err.message`. The goal is a message
 * the admin can act on, not a stack trace.
 */
function formatImportError(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body as { code?: string; message_en?: string } | null
    if (body?.message_en) return `${err.status}: ${body.message_en}`
    if (body?.code) return `${err.status} ${body.code}`
    return `${err.status} ${err.message}`
  }
  if (err instanceof Error) return err.message
  return '未知错误'
}