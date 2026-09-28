# 项目上下文 — copilot_v6_langgraph

本系统是一个基于 LangGraph 的 Copilot,让业务人员通过一句自然语言指令(中文或英文)安全地调用企业内部业务 API。本文件是术语表,不是规范文档,不含实现细节。

## 术语

### 业务人员 (Business User)
发起指令的企业内非技术用户。技术画像(运营 / 销售 / 数据分析师等)在后续确认中定型。

### 会话 (Conversation)
业务人员与 Copilot 之间的多轮对话单元。一个会话包含一个或多个用户轮次和系统响应。持久化,状态跨轮次保留。

#### 活跃会话 (Active Conversation)
处于"活跃"状态的会话:最近 15 分钟内有用户轮次。完整读写能力。参见 ADR-0011。

#### 空闲会话 (Idle Conversation)
超过 15 分钟无活动的会话。前端默认隐藏(可在"历史会话"标签下找回);用户可手动激活或主动"结束会话"。数据完整保留。参见 ADR-0011。

#### 归档会话 (Archived Conversation)
软结束超过 30 天的会话。只读存储,可被用户重新激活(创建新会话引用旧数据)。参见 ADR-0011。

### 用户轮次 (User Turn)
会话内业务人员发出的一条自然语言消息。是触发 Plan 生成与 Tool 执行的最小工作单元。

### Plan
位于用户轮次与 Tool 执行之间的结构化中间产物:由 Planner 生成的 Tool 调用有向图,带数据依赖边。任何 Tool 被调用前,Plan 都先展示给业务人员做 HITL 预览。

### Planner
负责把用户轮次翻译为 Plan 的子系统。具体实现可以是 LLM、规则、或两者混合 — 不属于本术语表的关注点。

### Plan 预览 (Plan Preview)
HITL 检查点。业务人员在此看到 Plan 的节点-边图,批准、修改或驳回。参见 ADR-0004。

### 风险等级 (Risk Level)
每个 Tool 的分类标签(`read` / `write` / `destructive`),用于驱动单次调用的 HITL 决策。语义:`read` 自动执行;`write` 与 `destructive` 需人工确认。参见 ADR-0004。

### API 资产 (API Asset)
企业自有的后端 API,是 Copilot 可调用的对象。来源:从 OpenAPI/Swagger 文档导入,或人工注册。参见 ADR-0003。

### Tool
API 资产在 Agent 运行时的视图:一个可调用对象,包含名称、自然语言描述、JSON Schema 参数、`risk_level`,以及后端 Credential 的引用。LLM 看到并选择的是 Tool;底层 API 资产的凭证不出现在 LLM 上下文中。

### Tool 生命周期
Tool 在 Tool Registry 中的状态:
- `draft` — LLM 已生成或人工已起草,等待管理员 review,Planner 不可见
- `active` — 管理员已激活,Planner 可见可调用
- `disabled` — 管理员主动下架,Planner 不可见(配置仍保留,便于再激活)

参见 ADR-0018。

### 工具组 (Tool Group)
一组 Tool 的命名集合,用于批量授权。管理员可将整组 Tool 一次性分配给角色,避免逐个授权。Tool 可以属于多个组。参见 ADR-0002。

### Tool 注册中心 (Tool Registry)
管理 Tool 目录的子系统。两条接入路径(OpenAPI 导入与人工注册)在同一份内部 Tool schema 上汇聚。参见 ADR-0003。

### 凭证 (Credential)
用于向 Tool 上游 API 进行身份认证的密钥(API Key、OAuth Token、mTLS 证书等)。在后端加密存储,只在执行 Tool 调用的 Worker 内部注入。绝不进入 LLM prompt,也不到达前端。参见 ADR-0002。

### 审计日志 (Audit Log)
只增不改的记录:谁、何时、为何调用了哪个 Tool,参数和返回结果各是什么。是"这件事为什么发生"的最终事实来源。保留策略:1 年热存(MongoDB 可查)+ 3 年冷存(归档存储,可调档),详见 ADR-0028。参见 ADR-0002。

### 身份源 (Identity Source)
业务人员身份的权威来源。MVP 阶段来自企业 SSO (OIDC/SAML) 联邦登录;后台管理员另设本地账号。运行时统一映射为内部 User 记录。参见 ADR-0006。

### IdP (Identity Provider)
企业 SSO 提供方(Azure AD / Okta / Keycloak / Authing 等)。通过 OIDC 或 SAML 与本系统对接。

### 用户组 (User Group)
IdP 通过 OIDC claim 返回的用户分组(如 `groups: ["finance", "sales"]`)。作为业务人员与内部 Role 之间的映射输入。IdP 侧的分组策略不在本系统控制范围内。参见 ADR-0006。

### 角色 (Role)
权限授予的目标。Tool(或 Tool 组)被分配给角色,业务人员通过角色获得 Tool 的调用权。

### 记忆窗口 (Memory Window)
每轮 Planner 拿到的"近期 K 轮"上下文(原文、Tool 调用结果、Plan 快照)。K 是可配置参数,默认 K=5。原文进 LLM,信号无损。参见 ADR-0007。

### 长期记忆 (Long-term Memory)
可被语义召回的历史 Plan / Tool 调用记录,用于跨轮 / 跨会话的远距离引用(如"上周那个项目")。MVP 阶段以原始 Plan 文本 + 描述字段入库,后续可演进为 LLM 摘要。参见 ADR-0007。

### Access Token
JWT,15 分钟有效。载荷包含用户 ID、来源(sso/local)、角色列表。后续请求鉴权主凭证。参见 ADR-0009。

### Refresh Token
不透明随机串,7 天有效,可吊销。用于在 Access Token 过期后无感续期,每次刷新轮换。参见 ADR-0009。

### Langfuse
LLM Prompt 管理与可观测性平台。所有 Planner Prompt、Memory 摘要 Prompt、tool-description-generator、injection-detector、result-summarizer 等集中托管于 Langfuse;每次 LLM 调用的输入 Prompt 与输出 Response 一并上报 Langfuse,形成 trace。参见 ADR-0013 / ADR-0025。

### LLM Provider
LLM 服务的具体提供方(OpenAI / DeepSeek / 豆包 / 自托管 vLLM 等)。通过 LangChain ChatModel 抽象,默认 OpenAI 兼容协议;不支持 Anthropic。参见 ADR-0014。
