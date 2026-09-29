import { Navigate, Route, Routes } from 'react-router-dom'

import { AuthCallbackPage } from '@/pages/AuthCallbackPage'
import { AdminPage } from '@/pages/AdminPage'
import { ChatPage } from '@/pages/ChatPage'
import { LoginPage } from '@/pages/LoginPage'
import { useAuthRefresh } from '@/hooks/useAuthRefresh'

/**
 * SPA root: owns the React Router route table + the silent-token-
 * rotation hook (T08 / #9, ADR-0009).
 *
 * Routes added by T08:
 * - `/auth/login`     — entry point; user clicks 登录, page redirects to IdP.
 * - `/auth/callback`  — IdP redirects back; page exchanges code for tokens.
 *
 * `/` and `/chat` predate T08. `useAuthRefresh` mounts at the root
 * so the timer fires regardless of which page the user is on —
 * chat-shell or admin shell, it doesn't matter; both consume
 * `useAuthStore` and would feel the same reload error if the timer
 * didn't.
 *
 * Subsequent tickets will add `/chat/:conversationId` and admin
 * sub-routes (see ADR-0029).
 */
function App(): React.ReactElement {
  useAuthRefresh()
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/chat" replace />} />
      <Route path="/auth/login" element={<LoginPage />} />
      <Route path="/auth/callback" element={<AuthCallbackPage />} />
      <Route path="/chat/*" element={<ChatPage />} />
      <Route path="/admin/*" element={<AdminPage />} />
    </Routes>
  )
}

export default App