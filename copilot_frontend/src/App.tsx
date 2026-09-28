import { Navigate, Route, Routes } from 'react-router-dom'

import { AdminPage } from '@/pages/AdminPage'
import { ChatPage } from '@/pages/ChatPage'

/**
 * SPA root: owns the React Router route table. Subsequent tickets will add
 * /chat/:conversationId, /admin/tools, /admin/audit, etc. (see ADR-0029).
 *
 * The `/` route redirects to /chat so a fresh load lands on the business-user
 * surface.
 */
function App(): React.ReactElement {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/chat" replace />} />
      <Route path="/chat/*" element={<ChatPage />} />
      <Route path="/admin/*" element={<AdminPage />} />
    </Routes>
  )
}

export default App
