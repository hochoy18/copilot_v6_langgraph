import { Link, NavLink, Route, Routes } from 'react-router-dom'

import { ToolsTable } from '@/components/admin/ToolsTable'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

/**
 * Admin shell — T13 / #42 (and future admin tickets).
 *
 * Routes:
 * - `/admin`         — landing summary (placeholder for now).
 * - `/admin/tools`   — Tool Registry table (T13).
 *
 * Each future admin surface (OpenAPI import, audit log, alerts) will
 * land as a sibling route, keeping `AdminPage.tsx` as the chrome
 * (top nav, layout) only. Pages stay free to assume their own
 * layout needs without inheriting sibling structure.
 */
export function AdminPage(): React.ReactElement {
  return (
    <main
      data-testid="admin-page"
      className="mx-auto flex min-h-screen w-full max-w-6xl flex-col gap-6 p-8"
    >
      <header className="flex items-center gap-4 border-b pb-4">
        <h1 className="text-2xl font-semibold">Admin</h1>
        <nav className="ml-auto flex gap-2" aria-label="admin sections">
          <AdminNavLink to="/admin">总览</AdminNavLink>
          <AdminNavLink to="/admin/tools">Tool Registry</AdminNavLink>
        </nav>
      </header>
      <Routes>
        <Route index element={<AdminOverview />} />
        <Route path="tools" element={<ToolsTable />} />
      </Routes>
    </main>
  )
}

function AdminNavLink({
  to,
  children,
}: {
  to: string
  children: React.ReactNode
}): React.ReactElement {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) =>
        cn(
          'rounded-md px-3 py-1.5 text-sm transition-colors',
          isActive
            ? 'bg-primary text-primary-foreground'
            : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
        )
      }
    >
      {children}
    </NavLink>
  )
}

function AdminOverview(): React.ReactElement {
  return (
    <section className="flex flex-col items-start gap-4">
      <p className="max-w-prose text-muted-foreground">
        管理员后台入口。Tool Registry 已上线 — 后续将接入 OpenAPI 导入 / 审计日志 / 告警收件(ADR-0029)。
      </p>
      <Button asChild>
        <Link to="/admin/tools">打开 Tool Registry</Link>
      </Button>
    </section>
  )
}