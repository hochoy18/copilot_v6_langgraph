import { useCallback, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api-client'
import {
  createTool,
  draftToCreateBody,
  draftToRegenerateBody,
  importOpenAPI,
  regenerateDescription,
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
 *   matches the canonical lifecycle; the audit hook (T42) sits
 *   between these two calls. T16 / #14 description generation
 *   happens server-side *before* this flow sees the draft, so the
 *   row's textarea opens with the LLM rewrite (badge + original
 *   text for comparison) and the admin's edits are what persist.
 * - **Discard** — local-state removal. Nothing is sent to the
 *   server, matching the "no implicit persistence" rule from
 *   ADR-0003.
 *
 * T16 / #14 review surface: a row whose description was rewritten
 * by the LLM carries `description_generated` + `original_description`;
 * the row renders a 改写 badge, an editable textarea, and a collapsed
 * 原文 block. Import-level notices from the backend (`warnings` on
 * the response — LLM not configured, generation cap hit) render as a
 * banner above the list, per the no-silent-capping rule.
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
  | { kind: 'regenerating' }
  | { kind: 'regenerated'; warnings: string[] }
  | { kind: 'regenerating_error' }
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
  // T16 / #14 — import-level notices from the backend (LLM not
  // configured, generation cap hit). Distinct from per-draft warnings.
  importWarnings: string[]
}

const INITIAL_STATE: PreviewState = {
  title: null,
  version: null,
  serverUrl: null,
  sourceFormat: 'json',
  drafts: [],
  statuses: {},
  importWarnings: [],
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
      // `Array.isArray` guard: a pre-T16 backend (or a stale test
      // fixture) without the `warnings` key must not crash the parse.
      importWarnings: Array.isArray(response.warnings) ? response.warnings : [],
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

  /**
   * T16-followup / #51 — re-run the `tool-description-generator` Prompt
   * against one preview row. The endpoint is advisory (no row is
   * persisted) so the success path writes the rewrite back into local
   * draft state and toggles `description_generated` so the badge stays
   * accurate. The activate flow that ships the rewritten description to
   * Mongo is the same `createTool` path as before — the regenerate
   * button is purely a convenience for re-rolling the textarea.
   *
   * Spec body says "回填 textarea" — the success path touches
   * `description` and the `description_generated` flag (so the
   * "LLM 改写" badge reflects the rewrite), and shifts the previous
   * textarea value into `original_description` so the admin's side-by-
   * side review (T16 / #14) keeps a baseline against the latest LLM
   * pass, mirroring what the import-time batch does. Per-parameter
   * fields are intentionally left alone — the per-row endpoint
   * returns `{description, typical_use_cases, warnings}` only.
   *
   * The cap-skip case is handled implicitly: a row whose initial
   * `description_generated` was `false` because the import batch
   * skipped it (T16 / #14 cap) now flips to `true` after a successful
   * regen, mirroring what the batch path would have set.
   */
  const regenerateDraft = useCallback(
    async (operationRef: string) => {
      const draft = preview.drafts.find((d) => d.operation_ref === operationRef)
      if (!draft) return
      setPreview((prev) => ({
        ...prev,
        statuses: { ...prev.statuses, [operationRef]: { kind: 'regenerating' } },
      }))
      try {
        const result = await regenerateDescription({ body: draftToRegenerateBody(draft) })
        setPreview((prev) => ({
          ...prev,
          drafts: prev.drafts.map((d) =>
            d.operation_ref === operationRef
              ? {
                  ...d,
                  description: result.description,
                  description_generated: true,
                  // Mirror the import-time batch's review-baseline
                  // invariant: the textarea's previous value (raw text
                  // on a cap-skip, or the prior rewrite) becomes the
                  // `original_description` the admin compares against.
                  original_description: d.description,
                  warnings: [...d.warnings, ...result.warnings],
                }
              : d,
          ),
          statuses: {
            ...prev.statuses,
            [operationRef]: { kind: 'regenerated', warnings: result.warnings },
          },
        }))
      } catch (err) {
        setPreview((prev) => ({
          ...prev,
          statuses: {
            ...prev.statuses,
            [operationRef]: { kind: 'regenerating_error' },
          },
        }))
        // Surface the failure as a transient error in `parseError` so
        // the admin sees the underlying message. The draft's previous
        // description is left untouched.
        setParseError(formatImportError(err))
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
        onRegenerate={(operationRef) => {
          void regenerateDraft(operationRef)
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
  onRegenerate: (operationRef: string) => void
}

function PreviewPanel({
  preview,
  onUpdateDraft,
  onDiscard,
  onActivate,
  onRegenerate,
}: PreviewPanelProps): React.ReactElement {
  return (
    <div className="flex flex-col gap-4" data-testid="preview-panel">
      {preview.importWarnings.length > 0 ? (
        <div
          role="alert"
          data-testid="import-warnings"
          className="flex flex-col gap-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-900 dark:text-amber-200"
        >
          {preview.importWarnings.map((warning, idx) => (
            <p key={idx}>{warning}</p>
          ))}
        </div>
      ) : null}
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
            onRegenerate={onRegenerate}
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
  onRegenerate: (operationRef: string) => void
}

function DraftRow({
  draft,
  status,
  onUpdate,
  onDiscard,
  onActivate,
  onRegenerate,
}: DraftRowProps): React.ReactElement {
  // Activate + discard lock the row once they're in flight (the create
  // tool POST is already using the textarea / row state); the
  // regenerate button additionally locks on its own in-flight status so
  // a re-roll doesn't overlap itself.
  const busy =
    status.kind === 'activating' || status.kind === 'active' || status.kind === 'regenerating'
  const regenerateDisabled = status.kind === 'regenerating'
  const disabled = busy
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
      {/*
        T16 / #14 review surface. The description arrives as the LLM
        rewrite (badge + collapsed 原文) when generation succeeded,
        or the raw OpenAPI text otherwise. It is always editable —
        ADR-0018 makes admin review mandatory, and the edited value is
        what `draftToCreateBody` persists on activate.
      */}
      <div className="flex flex-col gap-1 text-xs">
        <span className="flex items-center gap-2 text-muted-foreground">
          <span>描述(LLM-friendly,激活前请审核)</span>
          {draft.description_generated ? (
            <span
              data-testid={`draft-description-generated-${draft.operation_ref}`}
              className="rounded bg-accent px-1.5 py-0.5 text-[11px] text-accent-foreground"
            >
              LLM 改写
            </span>
          ) : null}
        </span>
        <textarea
          value={draft.description}
          disabled={disabled}
          onChange={(e) =>
            onUpdate(draft.operation_ref, { description: e.target.value })
          }
          rows={3}
          data-testid={`draft-description-${draft.operation_ref}`}
          aria-label={`描述 ${draft.operation_ref}`}
          className="rounded-md border border-input bg-background px-2 py-1.5 text-sm disabled:opacity-60"
        />
        {draft.description_generated && draft.original_description ? (
          <details
            data-testid={`draft-original-description-${draft.operation_ref}`}
            className="text-xs text-muted-foreground"
          >
            <summary className="cursor-pointer select-none">
              查看 LLM 改写前的原始 OpenAPI 描述
            </summary>
            <p className="mt-1 max-w-prose whitespace-pre-wrap rounded bg-muted/40 p-2">
              {draft.original_description}
            </p>
          </details>
        ) : null}
      </div>
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
          variant="outline"
          disabled={regenerateDisabled}
          onClick={() => onRegenerate(draft.operation_ref)}
          data-testid={`draft-regenerate-${draft.operation_ref}`}
        >
          {status.kind === 'regenerating' ? '生成中…' : '重新生成'}
        </Button>
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
  if (status.kind === 'regenerating') {
    return (
      <span
        data-testid="draft-status-regenerating"
        className="text-xs text-muted-foreground"
      >
        重新生成中…
      </span>
    )
  }
  if (status.kind === 'regenerated') {
    return (
      <span
        data-testid="draft-status-regenerated"
        className="text-xs text-primary"
      >
        已重新生成描述{status.warnings.length > 0 ? ` (${status.warnings.length} 个 warning)` : ''}
      </span>
    )
  }
  if (status.kind === 'regenerating_error') {
    return (
      <span
        role="alert"
        data-testid="draft-status-regenerating-error"
        className="text-xs text-destructive"
      >
        重新生成失败(请查看顶部提示,描述保持原值)
      </span>
    )
  }
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