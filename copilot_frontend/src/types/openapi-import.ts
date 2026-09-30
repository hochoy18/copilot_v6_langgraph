/**
 * Wire types for the OpenAPI import preview — T15 / #13 + T16 / #14.
 *
 * Mirrors `app.api.admin_tools.ImportOpenAPIRequest`,
 * `app.api.admin_tools.ImportOpenAPIResponse`, and
 * `app.api.admin_tools.ToolDraftResponse` from the backend (T14 / #12).
 *
 * Drafts arrive in `status='draft'` per ADR-0018. T16 / #14 adds LLM
 * description generation at preview time: `description` carries the
 * rewrite (with 典型用例 hints), `original_description` keeps the raw
 * OpenAPI text for side-by-side review, and `description_generated`
 * distinguishes the two states. The admin UI edits per-draft fields
 * (name, description, risk_level) before activating. The preview
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
 *
 * T16 / #14: `description` may be the LLM rewrite — a `true`
 * `description_generated` pairs it with the raw OpenAPI text in
 * `original_description` for side-by-side review. Both are inert
 * (`false` / `null`) when generation was skipped or failed, and the
 * admin can edit `description` either way before activating.
 */
export interface ToolDraft {
  operation_ref: string
  name: string
  description: string
  original_description: string | null
  description_generated: boolean
  risk_level: ToolRiskLevel
  status: ToolStatus
  parameters_schema: Record<string, unknown>
  original_parameters_schema: Record<string, unknown> | null
  parameters_schema_generated: boolean
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
 *
 * `warnings` is the T16 / #14 import-level notice list (LLM not
 * configured, the per-import generation cap hit). Per-operation
 * issues stay on each draft's own `warnings`.
 */
export interface ImportOpenAPIResponse {
  drafts: ToolDraft[]
  title: string | null
  version: string | null
  server_url: string | null
  source_format: 'json' | 'yaml'
  warnings: string[]
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

/**
 * Body of `POST /api/v1/admin/tools/descriptions/generate` —
 * T16-followup / #51.
 *
 * Long-tail companion to the import-time batch generation. The
 * spec body of #51 enumerates "name / method / path / 原始描述 /
 * 参数摘要"; the body shape here is exactly that. The preview UI
 * forwards the row's existing fields; the backend reuses the same
 * `tool-description-generator` Prompt.
 *
 * `description` is the text the LLM rewrites — usually the raw
 * OpenAPI summary, but an admin who has already typed their own
 * pass can also re-run the generator against their draft. `risk_level`
 * and `http_url_template` are deliberately omitted: the spec didn't
 * ask, and the preview row already carries them locally for the
 * activate flow.
 */
export interface RegenerateDescriptionRequestBody {
  name: string
  operation_ref: string
  http_method: string
  description: string
  parameters_schema: Record<string, unknown>
}

/**
 * Wire shape of `POST /api/v1/admin/tools/descriptions/generate`.
 *
 * Spec-pinned to `{description, typical_use_cases, warnings}` — the
 * per-row endpoint is description-only; per-parameter notes stay on
 * the import-preview batch (T16-followup / #50). The preview UI
 * writes `description` back into the textarea, renders `warnings`
 * next to the row, and shows the 典型用例 section as part of the
 * rewritten text.
 */
export interface RegenerateDescriptionResponse {
  description: string
  typical_use_cases: string[]
  warnings: string[]
}