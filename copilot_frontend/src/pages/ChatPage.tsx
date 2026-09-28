import { Network, Wrench } from 'lucide-react'
import { useEffect, useState } from 'react'

import { PlanDrawer } from '@/components/plan/PlanDrawer'
import { Button } from '@/components/ui/button'
import { useConversationStream } from '@/hooks/useConversationStream'
import { ApiError } from '@/lib/api-client'
import { createConversation, submitTurn } from '@/lib/conversations-api'
import { useConversationStreamStore } from '@/stores/conversation-stream'
import { usePlanDrawerStore } from '@/stores/plan-drawer'
import { cn } from '@/lib/utils'
import type { Turn } from '@/types/plan'

/** Mirrors `CreateTurnRequest.content` (`Field(max_length=4000)`) on the
 *  backend — keep both ends in lockstep so the wire shape is honoured
 *  before the request goes out. */
const MAX_INSTRUCTION_LENGTH = 4000

/** Right-side gutter that mirrors the docked drawer's `max-w-md` (~28rem). */
const DRAWER_DOCKED_PADDING = 'pr-[28rem]'

/**
 * Business-user chat surface — T19 / #17 slice.
 *
 * Per ADR-0029 the drawer lives to the right of the chat and slides
 * in when a Plan is ready. T19's job is exactly the first two links
 * of that chain: an instruction bar that hits the T18 turn endpoint
 * and pipes the returned Plan into the React Flow drawer
 * (`输入指令 → 抽屉滑出`). The transcript below is deliberately
 * minimal — conversation list (T11 / #41), SSE streaming (T23/T24),
 * HITL approve/reject (T20 / #43) and Plan editing (T26/T27) land
 * as siblings, each widening this page rather than replacing it.
 *
 * One conversation per page lifetime is created lazily on the first
 * send (`POST /conversations`, then `POST /turns` reuses its id —
 * the T18 endpoint requires a live conversation). Turns are kept in
 * local state because the persisted transcript hydrates with the
 * conversation-list ticket, not here.
 */
export function ChatPage(): React.ReactElement {
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<Turn[]>([])
  const [notices, setNotices] = useState<string[]>([])
  const [input, setInput] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const hasPlan = usePlanDrawerStore((s) => s.plan !== null)
  const drawerMode = usePlanDrawerStore((s) => s.mode)
  const showPlan = usePlanDrawerStore((s) => s.showPlan)
  const reopen = usePlanDrawerStore((s) => s.reopen)

  // Declare the store sync *before* the stream hook so effects run
  // in that order: the store's `conversationId` + buffers reset
  // first, then the socket opens. (React runs effects in declaration
  // order, so textual order here is the actual mount order.)
  useEffect(() => {
    const store = useConversationStreamStore.getState()
    if (conversationId) {
      store.startConversation(conversationId)
    } else {
      store.reset()
    }
  }, [conversationId])

  // SSE stream subscription (T24 / #21). Opens when a conversation
  // id is known; tears down on unmount or conversation switch. The
  // hook drives `useConversationStreamStore`, which `PlanToolNode`
  // and `PlanAnswerPane` select from.
  useConversationStream(conversationId)

  // When the drawer is docked, the chat main column must reserve the
  // right gutter the fixed-position drawer occupies; otherwise the
  // transcript runs under the drawer (the AC's "抽屉滑出" leaves the
  // chat unobstructed).
  const docked = drawerMode === 'docked'

  async function send(): Promise<void> {
    const content = input.trim()
    if (!content || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      let convId = conversationId
      if (!convId) {
        const conversation = await createConversation()
        convId = conversation.id
        setConversationId(convId)
      }
      const response = await submitTurn(convId, content)
      setMessages((prev) => [...prev, response.turn])
      if (response.warnings.length > 0) {
        setNotices((prev) => [...prev, ...response.warnings])
      }
      if (response.plan) showPlan(response.plan)
      setInput('')
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div data-testid="chat-page" className="flex min-h-screen">
      <main
        className={cn(
          'mx-auto flex h-screen w-full max-w-3xl flex-col gap-4 p-6 transition-[padding] duration-300',
          docked && DRAWER_DOCKED_PADDING,
        )}
      >
        <header className="flex items-center gap-2 border-b pb-4">
          <h1 className="text-xl font-semibold">Copilot Chat</h1>
          {hasPlan && drawerMode === 'collapsed' && (
            <Button
              variant="outline"
              size="sm"
              className="ml-auto gap-1"
              onClick={reopen}
            >
              <Wrench size={14} />
              查看 Plan
            </Button>
          )}
        </header>

        <section
          aria-label="对话记录"
          className="flex-1 space-y-3 overflow-y-auto"
        >
          {messages.length === 0 && notices.length === 0 && !error && (
            <p className="pt-8 text-center text-sm text-muted-foreground">
              用一句自然语言描述你要做的事, 例如「查一下 EMEA 的客户」。
            </p>
          )}
          {messages.map((turn) => (
            <div
              key={turn.id}
              data-testid={`turn-${turn.role}`}
              className="ml-auto max-w-[80%] rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground"
            >
              {turn.content}
            </div>
          ))}
          {notices.map((notice, index) => (
            <p
              // Warnings are append-only per turn, so index is stable
              // enough — the text itself carries no identity.
              key={`notice-${index}`}
              data-testid="turn-warning"
              className="mr-auto max-w-[80%] rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900"
            >
              {notice}
            </p>
          ))}
          {error && (
            <p
              role="alert"
              className="mr-auto flex max-w-[80%] items-center gap-2 rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-900"
            >
              <Network size={14} className="shrink-0" />
              {error}
            </p>
          )}
        </section>

        <form
          className="flex items-center gap-2 border-t pt-4"
          onSubmit={(event) => {
            event.preventDefault()
            void send()
          }}
        >
          <input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="输入指令…"
            aria-label="指令输入"
            maxLength={MAX_INSTRUCTION_LENGTH}
            className="h-10 flex-1 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <Button type="submit" disabled={submitting || input.trim() === ''}>
            {submitting ? '生成中…' : '发送'}
          </Button>
        </form>
      </main>

      <PlanDrawer />
    </div>
  )
}

function formatApiError(err: unknown): string {
  if (err instanceof ApiError) {
    return `请求失败 (HTTP ${err.status}), 请稍后重试。`
  }
  if (err instanceof TypeError) {
    return '无法连接后端服务, 请确认服务已启动。'
  }
  return '发生未知错误, 请重试。'
}
