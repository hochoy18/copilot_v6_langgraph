# ADR-0029: 前端架构

## 状态
已接受

## 背景
后端架构与 28 个 ADR 已定型,前端需要明确技术栈、路由、状态管理、UI 模式,才能开始 `/to-spec` 与实现。原始需求:前端"对后端好兼容,能渲染节点-边的流程图"。多轮 grilling 后,前端关键决策已收敛。

## 决策

### 技术栈

| 层级 | 选型 |
|---|---|
| 框架 | React 18 + TypeScript |
| 构建 | Vite |
| UI 组件 | shadcn/ui(Tailwind + Radix UI) |
| 流程图 | React Flow (xyflow) |
| 路由 | React Router v6 |
| 客户端状态 | Zustand |
| 服务端状态 | TanStack Query (React Query) |
| i18n | react-i18next |
| SSE | 原生 EventSource + 自封装 `useEventSource` hook |
| 鉴权 | OIDC code+PKCE(详见 ADR-0006、ADR-0009) |
| HTTP | 原生 fetch(配 TanStack Query) |

### 路由与角色

- 单 SPA 应用,业务人员 `/chat/*`、管理员 `/admin/*`。
- 同一用户可同时拥有业务人员 + 管理员角色,通过 JWT claim 决定路由可达性。
- `/auth/login` 发起 OIDC 重定向,`/auth/callback` 接 token 后跳回原页面。

### 业务人员侧 UI

- **Chat 布局** — Chat 主区在右,Plan 预览从右侧滑出为抽屉(drawer),可全屏看图也可收起继续聊(详见 ADR-0004、ADR-0019)。
- **Plan 编辑** — 点击 Plan 节点弹出参数表单,改完保存,直接进入执行阶段(详见 ADR-0019)。
- **会话列表** — 主页面顶部三个 Tab:活跃 / 空闲 / 归档。归档会话点开可重新激活(详见 ADR-0011)。
- **多轮** — Chat 主区下方输入框,SSE 流式显示回答。
- **用户反馈** — 最终回答下方 thumbs up / down + 可选 comment(详见 ADR-0025)。

### 管理员侧 UI(`/admin/*`)

- **Tool Registry** — 表格视图,列:Tool 名 / 描述 / 风险等级 / 状态(draft/active/disabled)。可过滤、可搜索(详见 ADR-0003)。MVP 不提供"最近调用 / 成功率"等聚合统计(详见 ADR-0031)。
- **OpenAPI 导入** — 上传 / 粘贴 spec → 预览所有 operation → LLM 自动生成描述(进度可见)→ 逐个 review / 激活 / 弃用(详见 ADR-0018)。
- **审计日志** — 可按用户 / Tool / 时间 / 状态过滤的表格,点行展开详情(请求 / 响应 / Plan 快照),支持冷存调档(详见 ADR-0028)。
- **告警收件** — 站内消息列表(详见下方"通知";Webhook 推送本期不做,留待 V1.1)。
- **Langfuse 跳转** — 跳到独立部署的 Langfuse 控制台查看 trace / 调 Prompt。

### 鉴权流程

- 业务人员走 OIDC 重定向,标准 code+PKCE(详见 ADR-0006、ADR-0009)。
- 管理员本地账号 MVP 走 username + password;MFA 留待后续追加(详见 ADR-0006)。
- 前端在 Access Token 临近过期(剩余 ≤2 分钟)主动用 Refresh Token 续期(详见 ADR-0009)。
- Token 存储策略见 ADR-0032。

### SSE 消费

- 封装 `useEventSource` hook:内部处理连接、重连(token 过期后自动换取再重连)、事件分发到 Zustand store。
- 组件订阅 store 里的 plan / tool 状态,不用各自维护 EventSource。
- 事件类型见 ADR-0010。

### 通知

- 管理员告警(凭证失效、注入检测 suspicious / dangerous、成本超阈值):仅站内消息(后台红色角标)。
- 普通事件(导入完成、用户反馈阈值触发):仅站内消息。
- Webhook 推送(飞书 / 钉钉 / Slack / 企业微信):本期不做,留待 V1.1。

### UI 语言

- UI 文本默认中文,右上角语言切换器可切英文(i18next + react-i18next)。
- LLM 输入语言策略由 Langfuse 上的 Prompt 作者决定,前端不参与(详见 ADR-0013、ADR-0014)。

## 后果

- 技术栈选定后,前端实施不再有架构层不确定性。
- 业务人员 + 管理员同一应用,但路由分层清晰。
- 全部依赖开源组件,无商业绑定。
- shadcn/ui 是组件源码复制进项目而非 npm 包,可深度定制。
- i18n 框架成本极低,只在初期多写一份 en 翻译。
- 前端不直接调 Langfuse API(避免暴露 Langfuse 凭证),跳过去即可(URL 由后端配置接口下发,详见 ADR-0031)。
- 多实例部署下 SSE 连接归属由后端决定,前端不感知(详见 ADR-0010)。
- 自封装 SSE hook 是关键基础设施,需要在首个 ticket 实现时打好基础。
- 前端与后端的 REST API 契约见 ADR-0031;Token 存储策略见 ADR-0032。
