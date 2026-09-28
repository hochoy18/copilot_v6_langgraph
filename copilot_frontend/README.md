# copilot-frontend

Zhipo Copilot 单页应用。技术栈:React 18 + TypeScript (strict) + Vite + Tailwind + shadcn/ui + React Router v6。

设计依据:[`docs/SPEC.md`](../../docs/SPEC.md) 和 [`docs/adr/0029-frontend-architecture.md`](../../docs/adr/0029-frontend-architecture.md)。

## 目录结构

```
src/
├── App.tsx                  # 路由根(/ → /chat, /chat/*, /admin/*)
├── main.tsx                 # 入口,挂载 BrowserRouter
├── index.css                # Tailwind base + shadcn CSS 变量(浅色)
├── components/ui/           # shadcn/ui 组件源码
├── pages/                   # 路由级页面组件(占位)
├── lib/utils.ts             # cn() helper
├── stores/                  # Zustand stores(空占位,见 README)
├── hooks/                   # 自定义 React hooks(空占位,见 README)
├── i18n/                    # react-i18next 资源(空占位,见 README)
├── test-setup.ts            # vitest 全局 setup(@testing-library/jest-dom)
└── test-utils.tsx           # MemoryRouter 包裹的测试渲染器
```

## 已落地 / 待补

按 [`SPEC.md`](../../docs/SPEC.md) 和 ADR-0029 推进。本 ticket (#3) 只交付"骨架":

| 关注点 | 状态 |
| --- | --- |
| React 18 + Vite + TS strict | ✅ |
| Tailwind + shadcn/ui (Button) | ✅ |
| `/chat`、`/admin` 占位路由 | ✅ |
| Vitest + Testing Library | ✅ (路由可见性 seam) |
| `react-i18next` 接入 | ⏳ i18n ticket(US-25, ADR-0029) |
| `@xyflow/react` Plan 预览 | ⏳ Plan-preview ticket (T19) |
| `/auth/login`、`/auth/callback` | ⏳ 鉴权 ticket(ADR-0029 line 31) |
| Zustand store + `QueryClientProvider` | ⏳ SSE hook / 数据层 ticket |
| 暗色主题 | 🚫 MVP 范围外 |

## 命令

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 启动 Vite dev server,默认 `http://localhost:5173` |
| `npm run build` | `tsc -b` (strict) + `vite build` |
| `npm run typecheck` | 仅跑 TypeScript 类型检查,不输出产物 |
| `npm run test` | 跑 vitest 单次运行 |
| `npm run test:watch` | vitest 监听模式 |
| `npm run lint` | oxlint 静态检查 |
| `npm run preview` | 预览生产构建产物 |

## 测试 seam

默认按 ADR-0029 走 HTTP API seam,本项目内的 Vitest + Testing Library 单元测试主要锚定路由可见性与组件可渲染性(占位页阶段)。后续 ticket 接入真实业务后再加 Playwright E2E。
