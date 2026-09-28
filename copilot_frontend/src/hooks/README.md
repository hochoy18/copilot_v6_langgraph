# Hooks

Custom React hooks live here.

- `useEventSource.ts` — the SSE transport per ADR-0029: EventSource wrapper
  handling named-event dispatch, exponential-backoff reconnect, and the
  `auth.expired` → `/auth/refresh` token swap (T24 / #21).
- `useConversationStream.ts` — the ADR-0029 "事件分发到 Zustand store" adapter:
  folds `useEventSource` events into `useConversationStreamStore` (live
  progress) and `usePlanDrawerStore` (Plan content — issue #53 pins the drawer
  store as the single Plan write point). Chat pages call this one hook; nobody
  opens an EventSource directly.
