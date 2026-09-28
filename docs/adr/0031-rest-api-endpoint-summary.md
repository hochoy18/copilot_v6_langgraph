# ADR-0031: 后端 REST API 端点总览

## 状态
已接受

## 背景
前端 ADR-0029 列了业务/管理 UI 需要调用的接口,后端 28 个 ADR 散见各能力的接口约定,但没有一份集中的"端点清单"。SPEC 阶段会细化每个端点的请求/响应 schema,本 ADR 给出**端点清单 + 每个端点的来源 ADR**,前端开发可对照查找。新增端点必须在本 ADR 追加行,不收口在散落 ADRs。

## 决策

### 通用约定

- 路径前缀:`/api/v1`
- 鉴权:除 `/auth/*` 公开外,均需 Bearer JWT
- 业务人员(`role ∈ user_roles`):访问 `/api/v1/conversations/*`、`/api/v1/feedback/*`
- 管理员(`role = admin`):访问 `/api/v1/admin/*`
- 错误响应统一格式:`{ code: string, message_zh: string, message_en?: string, details?: object }`(细节 SPEC 阶段定型)

### 业务人员侧

| 端点 | 方法 | 用途 | 来源 ADR |
|---|---|---|---|
| `/api/v1/auth/login` | GET | 发起 OIDC 重定向 | ADR-0006 / ADR-0009 |
| `/api/v1/auth/callback` | GET | OIDC 回调,签发 token | ADR-0006 / ADR-0009 |
| `/api/v1/auth/refresh` | POST | 用 Refresh Token 续期 | ADR-0009 |
| `/api/v1/auth/logout` | POST | 吊销 Refresh Token | ADR-0009 |
| `/api/v1/conversations` | GET | 列出当前用户的会话(active / idle / archived,query 参数过滤) | ADR-0011 |
| `/api/v1/conversations` | POST | 创建新会话 | ADR-0005 |
| `/api/v1/conversations/{id}` | GET | 拉取会话详情(含历史 Plan / 消息) | ADR-0005 / ADR-0027 |
| `/api/v1/conversations/{id}/turns` | POST | 发起用户轮次(开 SSE) | ADR-0005 |
| `/api/v1/conversations/{id}/plan` | PATCH | 修改 Plan 参数(详见 ADR-0019) | ADR-0019 |
| `/api/v1/conversations/{id}/plan/approve` | POST | 批准 Plan | ADR-0004 |
| `/api/v1/conversations/{id}/plan/reject` | POST | 驳回 Plan | ADR-0004 |
| `/api/v1/conversations/{id}/feedback` | POST | thumbs up/down + comment | ADR-0025 |
| `/api/v1/conversations/{id}/archive` | POST | 手动结束会话(转入 idle) | ADR-0011 |
| `/api/v1/conversations/{id}/reactivate` | POST | 重新激活 idle/archived 会话 | ADR-0011 |
| `/api/v1/conversations/{id}/stream` | GET (SSE) | 订阅会话实时事件 | ADR-0010 |

### 管理员侧

| 端点 | 方法 | 用途 | 来源 ADR |
|---|---|---|---|
| `/api/v1/admin/auth/login` | POST | 管理员本地账号登录 | ADR-0006 |
| `/api/v1/admin/tools` | GET | Tool 列表(可过滤、可搜索) | ADR-0003 / ADR-0018 |
| `/api/v1/admin/tools` | POST | 手动注册 Tool(默认 `draft`) | ADR-0003 / ADR-0018 |
| `/api/v1/admin/tools/{id}` | GET / PATCH | 拉取 / 修改 Tool(描述、风险等级、状态) | ADR-0003 / ADR-0018 |
| `/api/v1/admin/tools/import/openapi` | POST | 上传 / 粘贴 OpenAPI spec,返回预览(每个 operation 派生 draft Tool) | ADR-0003 / ADR-0018 |
| `/api/v1/admin/tools/import/confirm` | POST | 确认 preview 中的若干 operation 激活 | ADR-0018 |
| `/api/v1/admin/audit-logs` | GET | 审计日志查询(可过滤) | ADR-0028 |
| `/api/v1/admin/audit-logs/{id}/recall` | POST | 触发冷存调档 | ADR-0028 |
| `/api/v1/admin/users` | GET / POST | 用户列表 / 创建 | ADR-0006 |
| `/api/v1/admin/users/{id}/roles` | PUT | 分配角色 | ADR-0006 / ADR-0002 |

### 运维与可观测性

| 端点 | 方法 | 用途 | 来源 ADR |
|---|---|---|---|
| `/healthz` | GET | 进程与依赖探针(MongoDB / Milvus / Langfuse 健康聚合,公开、不需 JWT,K8s probe 友好) | T03 / ADR-0031 |

> `/healthz` 故意不在 `/api/v1` 前缀下,也不走 Bearer JWT — 部署探针 / 负载均衡需在不携带凭据时拿到响应,见 `app/api/health.py` 的设计说明。新增任何"运维类"端点(如 `/readyz`、`/metrics`)也应在本节追加并放在 `/api/v1` 之外。

### 系统配置(前端可读)

| 端点 | 方法 | 用途 | 来源 ADR |
|---|---|---|---|
| `/api/v1/config` | GET | 前端可读配置(Langfuse URL、版本号等) | ADR-0025 / ADR-0029 |

### 待 SPEC 阶段细化的端点 / 细节

- `Plan 修改 PATCH` 的请求 / 响应 schema(参数 diff 怎么传,详见 ADR-0019)
- `Tool 列表`聚合统计("最近调用 / 成功率",详见 ADR-0029)— MVP 是否提供、TODO 标记
- `OpenAPI 预览`的请求 / 响应 schema(返回什么粒度的 operation 列表)
- 错误响应统一格式(代码 + 双语消息)的细节
- 列表分页规范(limit / cursor / offset)— 选用 cursor,前端无限滚动友好
- 是否需要 `/api/v1/admin/notifications` 列表接口(站内消息)

## 后果

- 前端开发对照本 ADR 即可知道调哪个端点。
- SPEC 阶段把"待 SPEC 阶段细化的端点"逐一定型,SPEC 完成后本 ADR 末段清空。
- 新增端点必须在本 ADR 中追加行,不能散落 ADRs 不收口。
- 错误响应统一带 code,前端 i18n 时不用解析中文自然语言。
- MVP 不提供 Tool 列表聚合统计,前端 Tool 表只显示静态字段(无"最近调用 / 成功率"列)。
