/**
 * Conversation / Turn API client — T19 / #17, T20 / #43, T27 / #23.
 *
 * Thin facade over `apiFetch` for the chat write path
 * (`POST /api/v1/conversations`, `POST /api/v1/conversations/{id}/turns`,
 * ADR-0031) plus the HITL Plan approve / reject endpoints (T20 / #43,
 * ADR-0004) and the Plan-edit endpoint (T27 / #23, T26 / #44,
 * ADR-0019). The Turn response carries the generated Plan
 * synchronously — SSE (`plan.generated` events) lands with T23 / T24,
 * so T19's drawer hydrates straight from this call.
 *
 * Auth follows the rest of the app: `apiFetch` does not inject a bearer
 * yet (ADR-0032's in-memory Access Token store ships with the auth
 * ticket), same stance as `tools-api.ts`.
 */
import { ApiError, apiFetch } from '@/lib/api-client'
import type { Plan, PlanNode, TurnResponse } from '@/types/plan'

/** Lifecycle states — mirrors backend `ConversationStatus` (ADR-0011). */
export type ConversationStatus = 'active' | 'idle' | 'archived'

/** Wire shape of `POST /api/v1/conversations` (`ConversationResponse`). */
export interface ConversationResponse {
  id: string
  user_id: string
  title: string
  status: ConversationStatus
  last_activity_at: string
  created_at: string
  updated_at: string
}

/** Wire shape of `GET /api/v1/conversations` (`ConversationListResponse`). */
export interface ConversationListResponse {
  conversations: ConversationResponse[]
}

function jsonInit(method: 'POST', body: unknown, signal?: AbortSignal): RequestInit {
  const init: RequestInit = { method, body: JSON.stringify(body) }
  if (signal) init.signal = signal
  return init
}

/**
 * HITL approval — T20 / #43, ADR-0004.
 *
 * Flips the conversation's latest pending Plan to `approved`; the
 * Worker (T21) picks it up from there. The backend surfaces 409
 * (`plan_not_pending`) when the Plan is already decided, which the
 * caller can choose to render inline.
 */
export async function approvePlan(
  conversationId: string,
  signal?: AbortSignal,
): Promise<Plan> {
  const path = `/conversations/${encodeURIComponent(conversationId)}/plan/approve`
  return apiFetch<Plan>(path, jsonInit('POST', {}, signal))
}

/**
 * HITL rejection — T20 / #43, ADR-0004.
 *
 * Mirrors `approvePlan`; the Turn stays so the user can refine the
 * instruction and resubmit. Same `plan_not_pending` 409 contract.
 */
export async function rejectPlan(
  conversationId: string,
  signal?: AbortSignal,
): Promise<Plan> {
  const path = `/conversations/${encodeURIComponent(conversationId)}/plan/reject`
  return apiFetch<Plan>(path, jsonInit('POST', {}, signal))
}

/**
 * Render an `ApiError` from `approvePlan` / `rejectPlan` as a
 * user-facing Chinese sentence — T20 / #43, ADR-0031.
 *
 * Lives next to the calls that raise it so a future SSE hook (T24)
 * or worker-status pane (T21) that surfaces the same
 * `plan_not_pending` 409 can reuse the translation without
 * re-deriving the error envelope shape.
 *
 * Returns the backend's own `message_zh` for any 4xx/5xx (ADR-0031
 * guarantees it on every error response); falls back to a generic
 * sentence for transport failures (no body to draw from).
 */
export function formatPlanDecisionError(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body as { message_zh?: unknown } | null
    if (body && typeof body.message_zh === 'string') {
      return body.message_zh
    }
    return `请求失败 (HTTP ${err.status}), 请稍后重试。`
  }
  if (err instanceof TypeError) {
    return '无法连接后端服务, 请确认服务已启动。'
  }
  return '发生未知错误, 请重试。'
}

/**
 * HITL Plan edit — T27 / #23, T26 / #44, ADR-0019.
 *
 * PATCH /api/v1/conversations/{id}/plan. The wire shape is the full
 * edited `nodes` list (the repository enforces "same node-ids, same
 * `tool` per node", so any add / remove / repoint attempt surfaces as
 * a 400 `validation_error` upstream — the Frontend only mutates
 * `parameters` and `notes`). Returns the post-edit Plan with status
 * `modified`, which the dialog folds back into the drawer store via
 * `replacePlan`.
 *
 * Error envelope mirrors `approvePlan` / `rejectPlan`:
 * - 401 `auth_missing_token` — JWT gone (refresh chain dies).
 * - 404 `not_found` — wrong / cross-user / no pending Plan.
 * - 409 `plan_not_pending` — Plan already approved / rejected /
 *   executing; the user re-edits, the dialog keeps them on PATCH.
 * - 400 `validation_error` — node-ids or `tool` bindings drifted.
 */
export async function editPlan(
  conversationId: string,
  editedNodes: PlanNode[],
  signal?: AbortSignal,
): Promise<Plan> {
  const path = `/conversations/${encodeURIComponent(conversationId)}/plan`
  const init: RequestInit = {
    method: 'PATCH',
    body: JSON.stringify({ nodes: editedNodes }),
  }
  if (signal) init.signal = signal
  return apiFetch<Plan>(path, init)
}

/** Start a new conversation; the backend marks it `active` (ADR-0011). */
export async function createConversation(
  title = '',
  signal?: AbortSignal,
): Promise<ConversationResponse> {
  return apiFetch<ConversationResponse>('/conversations', jsonInit('POST', { title }, signal))
}

/**
 * List the signed-in user's conversations, optionally filtered by
 * lifecycle status — T11 / #41, ADR-0011.
 *
 * The three-tab Frontend surface (active / idle / archived) maps
 * 1:1 onto the `status` query param (`GET /api/v1/conversations`).
 * With `status=null` the backend returns every conversation the
 * user owns; we always pass an explicit value because the Frontend
 * never needs the "all statuses" view — that's what the tabs are
 * for.
 */
export async function fetchConversations(
  status: ConversationStatus,
  signal?: AbortSignal,
): Promise<ConversationResponse[]> {
  const path = `/conversations?status=${encodeURIComponent(status)}`
  const init: RequestInit = signal ? { signal } : {}
  const body = await apiFetch<ConversationListResponse>(path, init)
  return body.conversations
}

/**
 * Manually end a conversation — T11 / #41, ADR-0011.
 *
 * Transitions `active` / `idle` → `idle`. Re-archiving an
 * already-archived row is a backend no-op (the row stays archived),
 * but the caller shouldn't rely on that — the list view filters
 * out archived rows from the active tab, so this action surfaces
 * only for non-archived rows.
 */
export async function archiveConversation(
  conversationId: string,
  signal?: AbortSignal,
): Promise<ConversationResponse> {
  const path = `/conversations/${encodeURIComponent(conversationId)}/archive`
  return apiFetch<ConversationResponse>(path, jsonInit('POST', {}, signal))
}

/**
 * Submit one user Turn and run the Planner over it (T18 / #16).
 *
 * Returns the persisted turn plus the pending Plan (`null` when the
 * turn needed no Tool call or the Planner degraded — see `warnings`).
 */
export async function submitTurn(
  conversationId: string,
  content: string,
  signal?: AbortSignal,
): Promise<TurnResponse> {
  const path = `/conversations/${encodeURIComponent(conversationId)}/turns`
  return apiFetch<TurnResponse>(path, jsonInit('POST', { content }, signal))
}
