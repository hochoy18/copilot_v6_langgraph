/**
 * Business-user conversation list — T11 / #41, ADR-0011.
 *
 * Three-tab surface (active / idle / archived) sitting at `/chat`.
 * Maps 1:1 onto the `status` query param of
 * `GET /api/v1/conversations` (T10 / #40). The chat view itself
 * lives at `/chat/:conversationId` (`ConversationView`).
 *
 * Acceptance criteria from #41:
 * - [ ] 三 Tab 切换正常
 * - [ ] 新建会话出现 active
 * - [ ] 归档会话出现 idle
 * - [ ] 点进会话详情
 *
 * Server state lives in TanStack Query (ADR-0029). Each tab owns
 * its own query so a tab switch doesn't refetch the other two; the
 * `invalidateQueries` calls on create / archive keep the tabs in
 * sync without a hard reload.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Archive, MessageSquarePlus, RefreshCw } from 'lucide-react'
import { useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api-client'
import {
  archiveConversation,
  createConversation,
  fetchConversations,
} from '@/lib/conversations-api'
import type {
  ConversationResponse,
  ConversationStatus,
} from '@/lib/conversations-api'
import { useAuthStore } from '@/stores/auth'
import { cn } from '@/lib/utils'

const TABS: ReadonlyArray<{ value: ConversationStatus; label: string; empty: string }> = [
  { value: 'active', label: '活跃', empty: '暂无活跃会话, 点击「新建会话」开始。' },
  { value: 'idle', label: '空闲', empty: '没有空闲会话。' },
  { value: 'archived', label: '归档', empty: '没有归档会话。' },
]

function conversationsQueryKey(status: ConversationStatus): readonly unknown[] {
  return ['conversations', status] as const
}

export function ConversationList(): React.ReactElement {
  const [activeTab, setActiveTab] = useState<ConversationStatus>('active')
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  // Header identity slot — mirrors T08 / #9's "回调 /chat 显示用户名"
  // AC: the AuthCallbackPage flow lands on /chat (now the list view),
  // and the freshly-stored user must be visible right away. Falls
  // back to email so a missing display_name still renders.
  const user = useAuthStore((s) => s.user)

  const listQuery = useQuery<ConversationResponse[], ApiError>({
    queryKey: conversationsQueryKey(activeTab),
    queryFn: ({ signal }) => fetchConversations(activeTab, signal),
  })

  const createMut = useMutation<ConversationResponse, ApiError, void>({
    mutationFn: () => createConversation(),
    onSuccess: (conversation) => {
      // The user is now navigating into the conversation view; the
      // list cache stays as-is and React Query refetches on the
      // next visit (staleTime: 0) so the new row appears.
      navigate(`/chat/${conversation.id}`)
    },
  })

  const archiveMut = useMutation<ConversationResponse, ApiError, string>({
    mutationFn: (id) => archiveConversation(id),
    onSuccess: (_conversation, id) => {
      // Archive transitions active/idle → idle. Refetch both source
      // tabs so the row leaves one and (potentially) joins the
      // other. The active-tab query is invalidated regardless of
      // the source because the source row is unknown here.
      void queryClient.invalidateQueries({ queryKey: conversationsQueryKey('active') })
      void queryClient.invalidateQueries({ queryKey: conversationsQueryKey('idle') })
      // Optimistic remove the row from whichever tab the user is
      // looking at; rollback is left to React Query's standard
      // refetch on the next interaction.
      queryClient.setQueryData<ConversationResponse[]>(
        conversationsQueryKey(activeTab),
        (prev) => (prev ?? []).filter((c) => c.id !== id),
      )
    },
  })

  const conversations = listQuery.data ?? []
  const loading = listQuery.isLoading
  const error = listQuery.error
  const creating = createMut.isPending
  const archivingId = archiveMut.isPending ? archiveMut.variables : null

  function renderError(): React.ReactElement | null {
    if (!error) return null
    const code =
      error instanceof ApiError ? `HTTP ${error.status}` : 'network'
    return (
      <div
        role="alert"
        data-testid="conversations-error"
        className="flex items-center rounded-md border border-destructive bg-destructive/10 px-4 py-3 text-sm text-destructive"
      >
        <span>加载会话列表失败 ({code})。</span>
        <button
          type="button"
          className="ml-auto underline"
          onClick={() => {
            void listQuery.refetch()
          }}
        >
          <RefreshCw size={14} className="mr-1 inline" />
          重试
        </button>
      </div>
    )
  }

  return (
    <section
      data-testid="conversation-list"
      className="flex w-full max-w-3xl flex-col gap-4 p-6"
    >
      <header className="flex items-center gap-3 border-b pb-4">
        <h1 className="text-xl font-semibold">会话</h1>
        {user !== null && (
          <span
            data-testid="chat-username"
            className="ml-auto text-sm text-muted-foreground"
          >
            {user.display_name || user.email}
          </span>
        )}
        <Button
          className="gap-1"
          size="sm"
          onClick={() => createMut.mutate()}
          disabled={creating}
          data-testid="new-conversation"
        >
          <MessageSquarePlus size={14} />
          {creating ? '新建中…' : '新建会话'}
        </Button>
      </header>

      <nav
        role="tablist"
        aria-label="会话状态"
        className="flex gap-1 border-b"
      >
        {TABS.map((tab) => (
          <button
            key={tab.value}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.value}
            data-testid={`tab-${tab.value}`}
            className={cn(
              'px-3 py-2 text-sm transition-colors',
              activeTab === tab.value
                ? 'border-b-2 border-primary font-medium text-foreground'
                : 'text-muted-foreground hover:text-foreground',
            )}
            onClick={() => setActiveTab(tab.value)}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      {renderError()}

      <ul
        aria-label={TABS.find((t) => t.value === activeTab)?.label}
        data-testid="conversation-rows"
        className="divide-y rounded-md border"
      >
        {loading ? (
          <li
            data-testid="conversations-loading"
            className="px-4 py-8 text-center text-sm text-muted-foreground"
          >
            加载中…
          </li>
        ) : conversations.length === 0 ? (
          <li
            data-testid="empty-state"
            className="px-4 py-8 text-center text-sm text-muted-foreground"
          >
            {TABS.find((t) => t.value === activeTab)?.empty}
          </li>
        ) : (
          conversations.map((conversation) => (
            <ConversationRow
              key={conversation.id}
              conversation={conversation}
              archiving={archivingId === conversation.id}
              onOpen={() => navigate(`/chat/${conversation.id}`)}
              onArchive={() => archiveMut.mutate(conversation.id)}
            />
          ))
        )}
      </ul>
    </section>
  )
}

function ConversationRow({
  conversation,
  archiving,
  onOpen,
  onArchive,
}: {
  conversation: ConversationResponse
  archiving: boolean
  onOpen: () => void
  onArchive: () => void
}): React.ReactElement {
  return (
    <li
      data-testid={`conversation-row-${conversation.id}`}
      className="flex items-center gap-3 px-4 py-3"
    >
      <button
        type="button"
        className="flex flex-1 flex-col items-start gap-0.5 text-left"
        onClick={onOpen}
        data-testid={`open-${conversation.id}`}
      >
        <span className="text-sm font-medium">
          {conversation.title || `会话 ${conversation.id.slice(-6)}`}
        </span>
        <span className="text-xs text-muted-foreground">
          最近活动: {formatTimestamp(conversation.last_activity_at)}
        </span>
      </button>
      {conversation.status !== 'archived' && (
        <Button
          variant="outline"
          size="sm"
          className="gap-1"
          onClick={onArchive}
          disabled={archiving}
          data-testid={`archive-${conversation.id}`}
        >
          <Archive size={14} />
          {archiving ? '结束中…' : '结束会话'}
        </Button>
      )}
    </li>
  )
}

/**
 * ISO-8601 timestamp → locale-friendly short form. `Date` parsing
 * on a non-ISO string would throw; the backend always serialises
 * `datetime.isoformat()`, so `new Date(...)` is safe.
 */
function formatTimestamp(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
