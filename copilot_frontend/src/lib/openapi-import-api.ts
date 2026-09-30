/**
 * Admin OpenAPI import API client — T15 / #13.
 *
 * Four seams on top of `apiFetch`:
 *
 * 1. `importOpenAPI` — POST /admin/tools/import/openapi. Returns the
 *    preview drafts; the T14 backend never persists anything on this
 *    path (ADR-0003 / ADR-0018).
 *
 * 2. `createTool` — POST /admin/tools. Used by the "Activate" button
 *    to persist a selected draft as a `draft` row (the server pins
 *    `status='draft'` per ADR-0018; the admin reviews the description
 *    before flipping to `active`).
 *
 * 3. `setToolStatus` — PATCH /admin/tools/{id}. Used to promote the
 *    freshly-created row from `draft` to `active` immediately after
 *    the import. The two-step flow (create → activate) matches the
 *    ADR-0018 lifecycle. T16 / #14 description generation happens
 *    server-side at *preview* time (inside `importOpenAPI`), so the
 *    admin reviews the LLM rewrite before the row is ever persisted;
 *    the audit hook (T42) still sits between these two calls.
 *
 * 4. `regenerateDescription` — POST /admin/tools/descriptions/generate
 *    (T16-followup / #51). Per-row long-tail companion to the import-
 *    time batch: lets the admin re-run the generator against an
 *    existing preview row (skipped by the cap, or whose rewrite they
 *    don't like). The result is advisory; the preview writes it back
 *    into the textarea and activation still flows through
 *    `createTool` → `setToolStatus`.
 *
 * `detectSourceFormat` decides which arm of the discriminated union
 * the request body hits. JSON parses first because the OpenAPI JSON
 * dialect is a strict subset of YAML; if JSON parsing yields an
 * object, we know the source was intended as JSON. Otherwise the
 * text goes in as `spec_yaml` and the server's `parse_yaml` decodes
 * it (PyYAML handles both).
 */
import { apiFetch } from '@/lib/api-client'
import type { Tool, ToolStatus } from '@/types/tool'
import type {
  CreateToolRequestBody,
  ImportOpenAPIRequestBody,
  ImportOpenAPIResponse,
  RegenerateDescriptionRequestBody,
  RegenerateDescriptionResponse,
  ToolDraft,
} from '@/types/openapi-import'

export interface ImportOpenAPIParams {
  text: string
  signal?: AbortSignal
}

/**
 * Detect whether the source text should travel as `spec` (JSON
 * object) or `spec_yaml` (raw string).
 *
 * JSON-parsing first means a JSON-authored OpenAPI doc never round-
 * trips through YAML decoding, which avoids edge cases where PyYAML
 * silently permutes keys. `null` / arrays / primitives at the top
 * level fall through to YAML — they aren't valid OpenAPI documents
 * anyway, and the server's `parse_yaml` will surface the right
 * error.
 */
export function detectSourceFormat(text: string): ImportOpenAPIRequestBody {
  const trimmed = text.trim()
  if (trimmed.startsWith('{')) {
    try {
      const parsed = JSON.parse(trimmed)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return { spec: parsed as Record<string, unknown> }
      }
    } catch {
      // fall through to YAML
    }
  }
  return { spec_yaml: text }
}

/**
 * POST /api/v1/admin/tools/import/openapi — T14 / #12.
 *
 * Returns the preview drafts; the server does not persist anything
 * on this path (ADR-0003). The body shape is the discriminated
 * union from `detectSourceFormat`.
 */
export async function importOpenAPI(
  params: ImportOpenAPIParams,
): Promise<ImportOpenAPIResponse> {
  const body = detectSourceFormat(params.text)
  const init: RequestInit = params.signal
    ? { method: 'POST', body: JSON.stringify(body), signal: params.signal }
    : { method: 'POST', body: JSON.stringify(body) }
  return apiFetch<ImportOpenAPIResponse>('/admin/tools/import/openapi', init)
}

export interface CreateToolParams {
  body: CreateToolRequestBody
  signal?: AbortSignal
}

/**
 * POST /api/v1/admin/tools — T12 / #11.
 *
 * Always lands in `status='draft'` per ADR-0018; the server pins
 * `source='manual'` for this route. The "Activate" flow follows up
 * with `setToolStatus` to flip the new row to `active`.
 */
export async function createTool(params: CreateToolParams): Promise<Tool> {
  const init: RequestInit = params.signal
    ? { method: 'POST', body: JSON.stringify(params.body), signal: params.signal }
    : { method: 'POST', body: JSON.stringify(params.body) }
  return apiFetch<Tool>('/admin/tools', init)
}

export interface SetToolStatusParams {
  id: string
  status: ToolStatus
  signal?: AbortSignal
}

/**
 * PATCH /api/v1/admin/tools/{id} — T12 / #11, status-only.
 *
 * Mirrors the `only_status` branch in `admin_tools.patch_tool`: the
 * frontend sends `{"status": "active"}` and the server routes through
 * `svc.set_status` so the audit-log hook (T42) sees it as a discrete
 * lifecycle event rather than a generic diff.
 */
export async function setToolStatus(params: SetToolStatusParams): Promise<Tool> {
  const init: RequestInit = params.signal
    ? { method: 'PATCH', body: JSON.stringify({ status: params.status }), signal: params.signal }
    : { method: 'PATCH', body: JSON.stringify({ status: params.status }) }
  return apiFetch<Tool>(`/admin/tools/${encodeURIComponent(params.id)}`, init)
}

export interface RegenerateDescriptionParams {
  body: RegenerateDescriptionRequestBody
  signal?: AbortSignal
}

/**
 * POST /api/v1/admin/tools/descriptions/generate — T16-followup / #51.
 *
 * Re-runs the `tool-description-generator` Prompt against a single
 * preview row's metadata. The body carries the fields the Prompt
 * consumes (`name` / `operation_ref` / `http_method` /
 * `http_url_template` / `description` / `parameters_schema` /
 * `risk_level`) — see `GenerateDescriptionRequest` in
 * `app.api.admin_tools`.
 *
 * The endpoint does not persist anything; the preview writes the
 * returned `description` (plus the rewritten `parameters_schema`) back
 * into local row state, and the existing `createTool` path activates it
 * like any other draft. Failure surfaces as the standard
 * `llm_unavailable` envelope — the caller renders a one-line error
 * and leaves the previous description untouched.
 */
export async function regenerateDescription(
  params: RegenerateDescriptionParams,
): Promise<RegenerateDescriptionResponse> {
  const init: RequestInit = params.signal
    ? {
        method: 'POST',
        body: JSON.stringify(params.body),
        signal: params.signal,
      }
    : { method: 'POST', body: JSON.stringify(params.body) }
  return apiFetch<RegenerateDescriptionResponse>(
    '/admin/tools/descriptions/generate',
    init,
  )
}

/**
 * Shape the preview row into the body the regenerate endpoint wants.
 *
 * `description` is forwarded as the *current* textarea value (which
 * may be the LLM rewrite, the raw OpenAPI text, or the admin's first
 * edit) — that lets the admin re-run the generator against their own
 * pass without going back to the raw text. `operation_ref` is taken
 * verbatim from the row, matching what the import preview rendered.
 *
 * Pulled out of the component so the route stays declarative and
 * future "regen with override" affordances can layer on top without
 * touching `OpenAPIImport`. Mirrors the spec body's enumerated fields
 * (name / method / path / 原始描述 / 参数摘要); the preview row's
 * `risk_level` and `http_url_template` stay local to the UI — the
 * spec body didn't ask for them and the activate flow already has
 * them on hand.
 */
export function draftToRegenerateBody(
  draft: ToolDraft,
): RegenerateDescriptionRequestBody {
  return {
    name: draft.name,
    operation_ref: draft.operation_ref,
    http_method: draft.http_method,
    description: draft.description,
    parameters_schema: draft.parameters_schema,
  }
}

/**
 * Shape the "Activate" button needs from a draft to drive the two-
 * step create-then-activate flow. Pulled out so the component stays
 * declarative; the fields match `CreateToolRequestBody` 1:1.
 *
 * `source` is pinned to `'openapi'` so the persisted row carries
 * its ADR-0003 §21 provenance — the Registry must keep a pointer
 * from the live Tool back to the originating artefact so a future
 * re-sync can re-derive the row if the upstream API changes.
 */
export function draftToCreateBody(draft: ToolDraft): CreateToolRequestBody {
  return {
    name: draft.name,
    description: draft.description,
    risk_level: draft.risk_level,
    parameters_schema: draft.parameters_schema,
    http_method: draft.http_method,
    http_url_template: draft.http_url_template,
    http_headers: draft.http_headers,
    http_body_template: draft.http_body_template,
    source: 'openapi',
    source_ref: draft.source_ref,
    credentials_ref: draft.credentials_ref,
  }
}