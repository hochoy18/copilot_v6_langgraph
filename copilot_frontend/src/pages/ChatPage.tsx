import { Route, Routes } from 'react-router-dom'

import { ConversationList } from '@/components/chat/ConversationList'
import { ConversationView } from '@/components/chat/ConversationView'

/**
 * Business-user chat shell router — T11 / #41.
 *
 * Routes:
 * - `/chat`                  — three-tab conversation list (active / idle / archived).
 * - `/chat/:conversationId`  — single conversation chat surface (T19 drawer + chat input).
 *
 * The list owns the "new conversation" entry point; the view is
 * reached only via the list (or a deep link). Both surfaces share
 * the global auth + SSE stores via the providers mounted in
 * `main.tsx`, so route switches are seamless.
 */
export function ChatPage(): React.ReactElement {
  return (
    <div data-testid="chat-page" className="flex min-h-screen">
      <Routes>
        <Route index element={<ConversationList />} />
        <Route path=":conversationId" element={<ConversationView />} />
      </Routes>
    </div>
  )
}
