# Hooks

Custom React hooks live here.

Per ADR-0029, the first concrete hook will be `useEventSource` — a thin
EventSource wrapper that handles reconnect + token-refresh + Zustand dispatch.
It lands with the SSE-hook ticket.
