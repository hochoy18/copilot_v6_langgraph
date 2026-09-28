import { Button } from '@/components/ui/button'

/**
 * Placeholder business-user chat surface.
 *
 * Full chat UI (conversation list, SSE streaming, plan drawer) lands in
 * subsequent tickets. This page anchors /chat in the router so the rest of the
 * app — guard routes, top nav — can target it today.
 *
 * See ADR-0029 for the long-term chat layout (chat right, plan drawer slides
 * in from the right).
 */
export function ChatPage(): React.ReactElement {
  return (
    <main
      data-testid="chat-page"
      className="flex min-h-screen flex-col items-center justify-center gap-4 p-8"
    >
      <h1 className="text-3xl font-semibold">Chat</h1>
      <p className="max-w-prose text-center text-muted-foreground">
        业务人员主入口占位页。完整 Chat(会话列表 / Plan 抽屉 / 多轮 SSE)将在后续 ticket 接入。
      </p>
      <Button>新建会话</Button>
    </main>
  )
}
