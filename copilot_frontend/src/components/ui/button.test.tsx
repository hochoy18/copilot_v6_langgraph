import { describe, expect, it } from 'vitest'

import { Button } from '@/components/ui/button'
import { renderWithRouter } from '@/test-utils'

/**
 * Acceptance: "shadcn/ui 组件可渲染".
 * Exercises a real shadcn/ui Button with Tailwind variant classes applied.
 */
describe('Button (shadcn/ui)', () => {
  it('renders with default variant', () => {
    const { getByRole } = renderWithRouter(<Button>点击</Button>)
    const btn = getByRole('button', { name: '点击' })
    expect(btn).toBeInTheDocument()
    expect(btn.className).toMatch(/bg-primary/)
  })
})
