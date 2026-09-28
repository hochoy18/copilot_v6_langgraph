/**
 * Wire types for the OpenAPI import preview — T15 / #13.
 *
 * Mirrors `app.api.admin_tools.ImportOpenAPIRequest`,
 * `app.api.admin_tools.ImportOpenAPIResponse`, and
 * `app.api.admin_tools.ToolDraftResponse` from the backend (T14 / #12).
 *
 * Drafts arrive in `status='draft'` per ADR-0018; the admin UI edits
 * per-draft fields (name, risk_level) before activating. The preview
 * is **not persisted** until the admin hits "Activate" on each row,
 * which routes through `POST /admin/tools` (T12) + `PATCH /admin/tools/{id}`
 * (T12) — the existing CRUD endpoints.
 */

import type { ToolRiskLevel, ToolStatus } from '@/types/tool'

export type { ToolRiskLevel, ToolStatus }

/**
 * Body of `POST /api/v1/admin/tools/import/openapi`.
 *
 * Exactly one of `spec` / `spec_yaml` is set. The frontend picks
 * `spec` when the source text parses as a JSON object; otherwise it
 * posts the raw text as `spec_yaml` and lets the parser's
 * `parse_yaml` decode it (PyYAML is fine with both).
 */
export type ImportOpenAPIRequestBody =
  | { spec: Record<string, unknown>; spec_yaml?: never }
  | { spec_yaml: string; spec?: never }

/**
 * Wire shape of a single OpenAPI-derived draft Tool.
 *
 * Mirrors `ToolDraftResponse` 1:1. `operation_ref` is the human-
 * readable label for the preview list (`GET /pets/{id}`); the rest
 * of the fields are the same shape the manual-registration form
 * produces (T12). `warnings` carries per-draft issues so the admin
 * can fix and retry before activating.
 */
export interface ToolDraft {
  operation_ref: string
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
  warnings: string[]
}

/**
 * Wire shape of `POST /api/v1/admin/tools/import/openapi`.
 *
 * `source_format` is what the parser consumed (`json` vs `yaml`).
 * The UI surfaces it as a badge so the admin knows what they sent.
 */
export interface ImportOpenAPIResponse {
  drafts: ToolDraft[]
  title: string | null
  version: string | null
  server_url: string | null
  source_format: 'json' | 'yaml'
}

/**
 * Body of `POST /api/v1/admin/tools`.
 *
 * Mirrors `app.api.admin_tools.CreateToolRequest`. The frontend
 * defaults `source` to `'openapi'` when activating an import draft
 * so the persisted row keeps its provenance (ADR-0003 §21). Manual
 * registration forms can pass `'manual'` explicitly or omit it to
 * inherit the server default.
 */
export interface CreateToolRequestBody {
  name: string
  description: string
  risk_level: ToolRiskLevel
  parameters_schema: Record<string, unknown>
  http_method: string
  http_url_template: string
  http_headers: Record<string, string>
  http_body_template: Record<string, unknown> | null
  source?: 'openapi' | 'manual'
  source_ref?: string | null
  credentials_ref?: string | null
}