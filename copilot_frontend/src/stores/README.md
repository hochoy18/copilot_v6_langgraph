# Stores

Zustand stores live here (client state per ADR-0029; server state belongs to
TanStack Query, not these).

- `auth.ts` — Access Token (in-memory) + Refresh Token (localStorage) per
  ADR-0032, plus `refreshAccessToken` for the SSE hook's `auth.expired` path
  (T24 / #21 seam; T08 fills in the login side).
- `plan-drawer.ts` — the single Plan write point (issue #53 handoff): drawer
  mode, selected node, and every Plan status fold — HITL decide (T20) and SSE
  `plan.generated` / `plan.modified` / `execution.completed` (T24).
- `conversation-stream.ts` — live per-conversation progress: node runtime
  badges, the `llm.token` typewriter buffer, and the connection lifecycle
  pill's status (T24 / #21).
