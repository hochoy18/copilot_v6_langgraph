/**
 * Streaming answer pane — T24 / #21, ADR-0010.
 *
 * Renders the cumulative text the LLM streams back as `llm.token`
 * events arrive. AC: "回答逐字流出". The pane sits below the
 * React Flow canvas so the user can watch the final answer render
 * while the Plan graph above shows which Tool produced it.
 *
 * The pane reads from `useConversationStreamStore.streamingAnswer`
 * — a buffer keyed by `turn_id`. When `done` flips true the
 * typewriter caret hides and the pane becomes a static paragraph.
 * While the stream is still open the caret pulses via Tailwind's
 * `animate-pulse`; no JS animation timers needed.
 *
 * The pane stays mounted with `aria-live="polite"` so screen readers
 * announce new tokens without forcing the user to re-focus. The
 * content area is `whitespace-pre-wrap` so multi-line answers (e.g.
 * tables / lists the LLM emits) render correctly.
 */
import { useEffect, useRef } from 'react'

import { useConversationStreamStore } from '@/stores/conversation-stream'

export function PlanAnswerPane(): React.ReactElement | null {
  const answer = useConversationStreamStore((s) => s.streamingAnswer)
  const containerRef = useRef<HTMLDivElement | null>(null)

  // Pin the scroll position to the bottom as new tokens arrive so
  // long answers don't drift up past the visible area. The user can
  // still scroll up to read history; the effect re-runs on every
  // text change but only writes `scrollTop` when already near the
  // bottom (heuristic: skip if the user scrolled more than 32px up).
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    if (distanceFromBottom < 32) {
      el.scrollTop = el.scrollHeight
    }
  }, [answer?.text])

  if (!answer) return null

  return (
    <section
      data-testid="plan-answer-pane"
      data-done={answer.done ? 'true' : 'false'}
      className="shrink-0 border-t bg-background p-3 text-sm"
      aria-live="polite"
    >
      <header className="mb-1 flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          最终回答
        </h3>
        {!answer.done && (
          <span
            data-testid="plan-answer-caret"
            className="h-2 w-2 animate-pulse rounded-full bg-blue-500"
            aria-hidden="true"
          />
        )}
      </header>
      <div
        ref={containerRef}
        data-testid="plan-answer-text"
        className="max-h-48 overflow-y-auto whitespace-pre-wrap break-words"
      >
        {answer.text}
      </div>
    </section>
  )
}
