import { type ReactElement, type ReactNode } from 'react'
import { render, type RenderOptions } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

/**
 * Test renderer that wraps the tree in a MemoryRouter with an initial entry,
 * so route components can be exercised at a specific path.
 *
 * Defaults to `/` which — per the App router — redirects to /chat.
 */
export function renderWithRouter(
  ui: ReactElement,
  { initialEntries = ['/'], ...options }: RenderOptions & { initialEntries?: string[] } = {},
): ReturnType<typeof render> {
  function Wrapper({ children }: { children?: ReactNode }): ReactElement {
    return (
      <MemoryRouter initialEntries={initialEntries}>{children}</MemoryRouter>
    )
  }
  return render(ui, { wrapper: Wrapper, ...options })
}
