import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import App from '@/App'
import './index.css'

const rootEl = document.getElementById('root')
if (!rootEl) {
  throw new Error('Root element #root is missing from index.html')
}

/**
 * App-wide React Query client. The Tool Registry (T13) is the first
 * consumer; future admin endpoints (audit logs, users) will share
 * this instance so cache lifecycle is centralised. Defaults stay on
 * `staleTime: 0` so a `refetch()` after an admin mutation picks up the
 * new server state immediately.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 0,
      retry: 0,
    },
  },
})

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)