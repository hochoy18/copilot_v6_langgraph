import { describe, expect, it } from 'vitest'

import App from '@/App'
import { renderWithRouter } from '@/test-utils'

describe('App router', () => {
  it('redirects / to /chat', () => {
    const { getByTestId } = renderWithRouter(<App />, { initialEntries: ['/'] })
    expect(getByTestId('chat-page')).toBeInTheDocument()
  })

  it('renders the ChatPage at /chat', () => {
    const { getByTestId } = renderWithRouter(<App />, { initialEntries: ['/chat'] })
    expect(getByTestId('chat-page')).toBeInTheDocument()
  })

  it('renders the AdminPage at /admin', () => {
    const { getByTestId } = renderWithRouter(<App />, { initialEntries: ['/admin'] })
    expect(getByTestId('admin-page')).toBeInTheDocument()
  })
})
