import { Link, NavLink, Route, Routes } from 'react-router-dom'

import { OpenAPIImport } from '@/components/admin/OpenAPIImport'
import { ToolsTable } from '@/components/admin/ToolsTable'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

/**
 * Admin shell — T13 / #42 + T15 / #13 (and future admin tickets).
 *
 * Routes:
 * - `/admin`               — landing summary.
 * - `/admin/tools`         — Tool Registry table (T13).
 * - `/admin/tools/import`  — OpenAPI import preview (T15).
 *
 * Each future admin surface (audit log, alerts) lands as a sibling
 * route, keeping `AdminPage.tsx` as the chrome (top nav, layout)
 * only. Pages stay free to assume their own layout needs without
 * inheriting sibling structure.
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
          <AdminNavLink to="/admin/tools/import">OpenAPI 导入</AdminNavLink>
        </nav>
      </header>
      <Routes>
        <Route index element={<AdminOverview />} />
        <Route path="tools" element={<ToolsTable />} />
        <Route path="tools/import" element={<OpenAPIImport />} />
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
        管理员后台入口。Tool Registry 与 OpenAPI 导入已上线 — 后续将接入审计日志 / 告警收件(ADR-0029)。
      </p>
      <Button asChild>
        <Link to="/admin/tools">打开 Tool Registry</Link>
      </Button>
      <Button asChild variant="outline">
        <Link to="/admin/tools/import">从 OpenAPI spec 导入 Tool</Link>
      </Button>
    </section>
  )
}