import '@testing-library/jest-dom/vitest'

/**
 * jsdom lacks the observers React Flow (@xyflow/react) measures nodes
 * with. No-op stubs are enough: node content renders regardless of the
 * measured dimensions, and no test asserts zoom/pan behaviour.
 */
class NoopObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

if (!('ResizeObserver' in globalThis)) {
  globalThis.ResizeObserver = NoopObserver as unknown as typeof ResizeObserver
}
if (!('IntersectionObserver' in globalThis)) {
  globalThis.IntersectionObserver = NoopObserver as unknown as typeof IntersectionObserver
}
