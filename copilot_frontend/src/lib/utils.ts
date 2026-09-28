import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/**
 * Concatenate Tailwind class names with conflict resolution.
 * Used by shadcn/ui components for composing variant + consumer class overrides.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
