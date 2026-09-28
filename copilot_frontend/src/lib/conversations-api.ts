/**
 * Conversation / Turn API client — T19 / #17.
 *
 * Thin facade over `apiFetch` for the chat write path
 * (`POST /api/v1/conversations`, `POST /api/v1/conversations/{id}/turns`,
 * ADR-0031). The Turn response carries the generated Plan synchronously —
 * SSE (`plan.generated` events) lands with T23 / T24, so T19's drawer
 * hydrates straight from this call.
 *
 * Auth follows the rest of the app: `apiFetch` does not inject a bearer
 * yet (ADR-0032's in-memory Access Token store ships with the auth
 * ticket), same stance as `tools-api.ts`.
 */
import { apiFetch } from '@/lib/api-client'
import type { TurnResponse } from '@/types/plan'

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
