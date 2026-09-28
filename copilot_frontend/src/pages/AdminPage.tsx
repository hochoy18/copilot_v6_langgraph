import { Button } from '@/components/ui/button'

/**
 * Placeholder admin surface.
 *
 * The admin shell will host the Tool Registry table, OpenAPI importer, audit
 * log viewer, and the in-app alert inbox (see ADR-0029). This page only
 * anchors /admin in the router.
 */
export function AdminPage(): React.ReactElement {
  return (
    <main
      data-testid="admin-page"
      className="flex min-h-screen flex-col items-center justify-center gap-4 p-8"
    >
      <h1 className="text-3xl font-semibold">Admin</h1>
      <p className="max-w-prose text-center text-muted-foreground">
        管理员后台占位页。Tool Registry / OpenAPI 导入 / 审计日志 / 告警收件将在后续 ticket 接入。
      </p>
      <Button variant="secondary">登录</Button>
    </main>
  )
}
