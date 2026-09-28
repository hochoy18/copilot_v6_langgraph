/**
 * Conversation / Turn API client — T19 / #17, T20 / #43.
 *
 * Thin facade over `apiFetch` for the chat write path
 * (`POST /api/v1/conversations`, `POST /api/v1/conversations/{id}/turns`,
 * ADR-0031) plus the HITL Plan approve / reject endpoints (T20 / #43,
 * ADR-0004). The Turn response carries the generated Plan
 * synchronously — SSE (`plan.generated` events) lands with T23 / T24,
 * so T19's drawer hydrates straight from this call.
 *
 * Auth follows the rest of the app: `apiFetch` does not inject a bearer
 * yet (ADR-0032's in-memory Access Token store ships with the auth
 * ticket), same stance as `tools-api.ts`.
 */
import { ApiError, apiFetch } from '@/lib/api-client'
import type { Plan, TurnResponse } from '@/types/plan'

/** Wire shape of `POST /api/v1/conversations` (`ConversationResponse`). */
export interface ConversationResponse {
  id: string
  user_id: string
  title: string
  status: 'active' | 'idle' | 'archived'
  last_activity_at: string
  created_at: string
  updated_at: string
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

/** Start a new conversation; the backend marks it `active` (ADR-0011). */
export async function createConversation(
  title = '',
  signal?: AbortSignal,
): Promise<ConversationResponse> {
  return apiFetch<ConversationResponse>('/conversations', jsonInit('POST', { title }, signal))
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
