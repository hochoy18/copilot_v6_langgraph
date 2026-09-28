# SPEC — copilot_v6_langgraph MVP

> 本 SPEC 综合自 `CONTEXT.md`、ADR-0001 至 ADR-0032,以及 `docs/research/` 中的一手资料调研。所有术语以 `CONTEXT.md` 为准,所有架构决策以 ADR 为准。

---

## Problem Statement

业务人员(运营 / 销售 / 数据分析师等非技术用户)需要调用企业内部业务 API 完成日常工作,但**调用门槛高**:
- 通用大模型助手(GPT / Claude)无法触达企业内部 API
- RAG 问答只能读不能写,无法驱动业务动作
- RPA 流程僵硬,改一个参数要重做脚本
- iPaaS 仍是开发者工具,业务人员用不上

"企业内部 API 资产的可用性"是一个被现有方案都未触及的窄而深的空白。本系统填补这个空白:业务人员用一句自然语言(中文或英文)驱动企业已有业务 API,系统保证**安全**(凭证隔离、按用户授权、审计追溯)、**可控**(Plan 预览 + HITL 确认)、**可解释**(节点-边执行图 + 完整 trace)。

## Solution

部署一套 LangGraph 驱动的 Copilot:
- **业务人员侧**:登录(SSE 联邦)→ 输入自然语言 → 系统生成 Plan(节点-边 DAG,展示在右侧抽屉) → 业务人员 review/approve/edit → 系统按 Plan 执行 Tool → 流式返回最终回答 → 多轮继续
- **管理员侧**:Tool Registry 管理(OpenAPI 导入 / 人工注册)、审计日志查询、告警收件
- **底层基础设施**:Langfuse(Prompt 管理 + 可观测性)、MongoDB(业务真相)、Milvus(向量记忆)、SSE(实时进度推送)

单租户私有部署,Python + FastAPI 后端,React + TypeScript + React Flow 前端。

## User Stories

### 业务人员

1. 作为业务人员,我希望通过企业 SSO 登录系统,以便使用我的企业身份访问 Copilot
2. 作为业务人员,我希望登录后默认看到我的活跃会话列表,以便快速回到之前的工作
3. 作为业务人员,我希望在顶部三个 Tab(活跃 / 空闲 / 归档)中切换会话状态,以便找到过去的工作
4. 作为业务人员,我希望点击"新建会话"开一个空白画布,以便开始新任务
5. 作为业务人员,我希望在主区域下方输入自然语言指令(中文或英文),以便发起一个用户轮次
6. 作为业务人员,我希望系统在思考时显示"正在规划 Plan",以便我知道系统在工作
7. 作为业务人员,我希望 Planner 产出 Plan 后,从右侧滑出抽屉显示节点-边图,以便看清要做什么
8. 作为业务人员,我希望在 Plan 图中看到每个节点的 Tool 名、参数、风险等级,以便评估是否执行
9. 作为业务人员,我希望点 Plan 上的"批准"按钮,系统按 Plan 执行所有 Tool,以便快速推进
10. 作为业务人员,我希望点 Plan 上某个节点,弹出参数表单修改,以便微调 LLM 生成的参数
11. 作为业务人员,我希望点 Plan 上的"驳回",系统取消并允许我重新描述,以便换思路
12. 作为业务人员,我希望在执行中看到每个 Tool 节点的实时状态(pending / running / success / failed),以便追踪进度
13. 作为业务人员,我希望 read 类 Tool 自动跑、write / destructive 类 Tool 弹窗二次确认,以便安全
14. 作为业务人员,我希望在最终回答下方点 thumbs up / down + 可选 comment,以便给系统反馈
15. 作为业务人员,我希望同一个会话里可以接着说"再帮我看看上个月",以免重述上下文
16. 作为业务人员,我希望 15 分钟不操作后,会话自动转入空闲,数据保留,以便下次回来继续
17. 作为业务人员,我希望手动点"结束会话",立刻转空闲,以便整理会话列表
18. 作为业务人员,我希望会话空闲超过 30 天后转归档,我能重新激活继续,以便不丢历史
19. 作为业务人员,我希望 Access Token 快过期时自动续期,不必重复登录
20. 作为业务人员,我希望在输入"查上个月那个项目"时,系统能从长期记忆召回,以便跨多轮引用
21. 作为业务人员,我希望注入可疑指令时,系统拒绝并提示,以免被攻击
22. 作为业务人员,我希望在会话快达到成本上限时,看到提示,以免突然中断
23. 作为业务人员,我希望 Tool 返回超大结果时,系统自动压缩,核心信息不丢
24. 作为业务人员,我希望最终回答以流式方式逐字显示,以便快速感知进度
25. 作为业务人员,我希望 UI 默认中文,可切英文,以便按习惯使用

### 管理员

26. 作为管理员,我希望通过本地账号 + 密码登录,以便管理 Tool 与审计
27. 作为管理员,我希望在 Tool Registry 页看到一个表格,列出所有 Tool 的名称、描述、风险等级、状态,以便总览
28. 作为管理员,我希望过滤 Tool 状态(draft / active / disabled),以便聚焦
29. 作为管理员,我希望上传或粘贴 OpenAPI spec,系统展示所有派生的 Tool 预览,以便审核
30. 作为管理员,我希望 LLM 自动为每个 operation 生成 LLM-friendly 描述,以便减轻手写负担
31. 作为管理员,我希望逐个 review、修改、激活或弃用 preview 中的 Tool,以便控制上线
32. 作为管理员,我希望对没 OpenAPI 文档的 API 手动填写 Tool,以便覆盖长尾
33. 作为管理员,我希望修改 Tool 的风险等级、描述、状态,以便调整策略
34. 作为管理员,我希望凭证失效(401/403)时收到站内告警,以便及时轮换
35. 作为管理员,我希望注入检测器标 dangerous 时收到站内告警,以便排查用户
36. 作为管理员,我希望审计日志可按用户 / Tool / 时间 / 状态过滤,以便追溯
37. 作为管理员,我希望点审计日志行展开详情(请求 / 响应 / Plan 快照),以便还原现场
38. 作为管理员,我希望冷存的审计日志可手动调档(5 分钟内回到 MongoDB),以便紧急合规场景
39. 作为管理员,我希望点"查看 Langfuse"跳到 Langfuse 控制台,看到完整 trace 与 Prompt 版本,以便深度调优
40. 作为管理员,我希望管理员后台右上角可以登出,以便交接

### 系统 / 集成

41. 作为系统,我要把每次 LLM 调用的 prompt 与输出上报 Langfuse,以便追踪
42. 作为系统,我要把 Tool 调用按 risk_level 分级处理(read 自动重试 / write 不重试),以便安全 + 体验
43. 作为系统,我要在 Tool 调用前用 JSON Schema 严格校验参数,以免 LLM 幻觉打到上游 API
44. 作为系统,我要在 Tool 调用超时(默认 30s)时按风险等级处理,以免 Worker 被永久阻塞
45. 作为系统,我要在 Plan 生成时冻结 Tool 定义快照,以便审计可重现
46. 作为系统,我要对所有凭证加密存储,只在调用瞬间注入 Worker,以便凭证不外泄
47. 作为系统,我要按 `data_usage_opt_out=true` 走 no-train 路径,以便企业合规
48. 作为系统,我要按 Langfuse 上 Prompt 名 + 版本引用模板,代码仓库不存 Prompt 内容
49. 作为系统,我要在 Langfuse 不可用时降级(本地缓存 Prompt + 异步上报 trace),以免业务中断

## Implementation Decisions

### 模块边界(seams)

测试和实现的最高 seam 是 **HTTP API(SSE + REST)**,所有端点见 ADR-0031。前端不直接调用 Langfuse / MongoDB / Milvus,必须走后端。

后端模块分层:
- `auth/` — OIDC + 本地账号 + JWT 签发(ADR-0006 / ADR-0009)
- `tools/` — Tool Registry(ADR-0003 / ADR-0018 / ADR-0020 / ADR-0026)
- `conversations/` — 会话生命周期 + 多轮 + 长期记忆(ADR-0005 / ADR-0007 / ADR-0011 / ADR-0027)
- `planner/` — Planner / DAG 执行 / 错误恢复(ADR-0004 / ADR-0012 / ADR-0017 / ADR-0019 / ADR-0021)
- `safety/` — 注入检测 / 结果压缩 / 凭证隔离 / 数据 opt-out(ADR-0016 / ADR-0022 / ADR-0023 / ADR-0024)
- `observability/` — Langfuse 上报 / 成本监控 / 用户反馈(ADR-0025)
- `audit/` — 审计日志 + 冷存调档(ADR-0028)
- `admin/` — 管理员 API(用户 / 角色 / 通知)
- `frontend/` — React SPA,目录:`pages/`, `components/`, `stores/`, `hooks/`, `i18n/`

### 数据模型(MongoDB Collections)

- `users` — 用户记录(sso_id / local_id / roles / created_at)
- `roles` — 角色定义
- `tool_groups` — Tool 分组
- `tools` — Tool 定义(name / description / schema / risk_level / status / snapshots / credentials_ref)
- `credentials` — 加密凭证(每个 Tool 引用一个)
- `conversations` — 会话(owner_id / status / created_at / last_active_at / archived_at)
- `turns` — 用户轮次 + 助手响应(conversation_id / sequence / content / timestamp)
- `plans` — Plan 文档(conversation_id / turn_id / nodes / edges / tool_snapshots / status)
- `plan_executions` — 节点级执行轨迹(plan_id / node_id / status / request / response / retry_count)
- `audit_logs` — 审计条目(user_id / tool_id / plan_id / request / response / error_type / timestamp)
- `refresh_tokens` — Refresh Token 表(token_hash / user_id / created_at / revoked_at)

Milvus collections:
- `plan_history_vectors` — 历史 Plan 摘要向量(conversation_id / plan_id / text / vector)
- `tool_call_vectors` — 历史 Tool 调用摘要向量

### API 契约

完整端点见 ADR-0031。要点:
- 路径前缀 `/api/v1`,Bearer JWT 鉴权
- 错误响应统一格式:`{ code, message_zh, message_en?, details? }`
- 列表用 cursor 分页(细节在 `/to-tickets`)
- SSE 鉴权走 Query Param(ADR-0010),事件清单: `plan.generated` / `plan.modified` / `tool.started` / `tool.finished` / `tool.failed` / `llm.token` / `execution.completed` / `cost.warning`

### Planner 执行流(决策图)

```
User Turn 输入
    ↓
[注入检测] → dangerous 拒绝
    ↓ safe
[记忆窗口 + 长期记忆召回 + 当前 Plan 上下文] 拼成 prompt
    ↓
Planner LLM 调用(Langfuse prompt 名: planner)
    ↓
产出 Plan(节点-边 DAG)
    ↓
[Plan 预览] HITL checkpoint
    ↓ approve / edit / reject
[DAG 拓扑并行执行] (ADR-0012)
    ↓ 每个节点
[JSON Schema 校验] (ADR-0020)
    ↓ 通过
[注入检测 on Tool 返回] (ADR-0022)
    ↓ safe
[大小判断 + 超限压缩] (ADR-0023)
    ↓
[凭证注入 + 上游 API 调用](ADR-0002 + ADR-0024 + ADR-0026)
    ↓
[错误处理 by risk_level] (ADR-0017)
    ↓
[SSE 事件推前端]
    ↓
Plan 全部节点完成 → llm.token 流式回答 → execution.completed
```

### Langfuse Prompt 列表

- `planner` — Planner LLM 提示
- `tool-description-generator` — Tool 描述生成(ADR-0018)
- `injection-detector` — 注入检测(ADR-0022)
- `result-summarizer` — 结果压缩(ADR-0023)
- `feedback-analyzer`(V1.1)— 用户反馈分析
- `memory-summary`(V1.1)— 长期记忆摘要

### 测试 Decisions

- **测试 seam**:HTTP API(SSE + REST),不测内部实现细节
- **模块覆盖**:
  - `auth/` — 单元测试覆盖 JWT 签发 / Refresh 轮换 / OIDC code 交换;集成测试覆盖完整登录流程
  - `tools/` — 单元测试覆盖 JSON Schema 校验、超时处理、错误恢复分级;集成测试覆盖 OpenAPI 导入预览
  - `conversations/` — 集成测试覆盖多轮 / 长期记忆召回 / 归档重新激活
  - `planner/` — 用 Langfuse traces 回归测试(每次 Planner 跑都落 trace,人工对比关键 case)
  - `safety/` — 注入检测用 fixture 集(明确攻击用例 + 合法用例)
- **E2E**:用 Playwright 覆盖"业务人员一句话 → Plan 预览 → 执行 → 最终回答 → thumbs"完整链路
- **错误响应格式**:单元测试断言 `{ code, message_zh, message_en?, details? }` 结构
- **不做的事**:不测 LangChain / LangGraph 内部行为,不测 LLM 输出本身(只测结构化字段)

## Out of Scope(MVP 不做)

明确推迟到 V1.1+:
- **Webhook 推送**(飞书 / 钉钉 / Slack / 企业微信)— 管理员告警仅站内消息
- **MFA** for 管理员 — MVP 仅 username + password
- **Tool 列表聚合统计**(最近调用 / 成功率)— Tool 表只显示静态字段
- **Tool "试运行"**(管理员激活前调一次)— 走"激活 → 等真实调用"的路径
- **Mobile / responsive** — 桌面优先
- **多实例 SSE 路由** — 单实例足够
- **Plan reject 原因捕获** — V1.1
- **静态资源服务架构** — V1.1(SPA build 由 nginx 或 FastAPI 静态托管待定)
- **CORS 策略** — V1.1(SPA 与 FastAPI 同源部署,先不需要 CORS)
- **Langfuse A/B 测试灰度** — MVP 默认全量
- **Langfuse Evaluations(LLM-as-judge / 人工标注)** — V1.1
- **前端 mobile / 平板适配**

## Further Notes

### ADR-0031 中标注的"TBD"项(SPEC 后必须定型)

1. `PATCH /api/v1/conversations/{id}/plan` 请求 / 响应 schema(参数 diff 怎么传)— 在首个 Plan edit ticket 里定义
2. `POST /api/v1/admin/tools/import/openapi` 响应 schema(预览粒度)— 在 OpenAPI import ticket 里定义
3. 错误响应统一格式细节(`code` 命名空间)— 在 auth 第一个 ticket 里定义
4. 列表 cursor 字段约定 — 在 scaffold ticket 里定义

### 与研究文档的对应

`docs/research/2026-09-28-...` 引用了 LangGraph / LangChain / Langfuse / FastAPI / MongoDB / Milvus 的一手资料,涉及实现细节时(尤其是 LangGraph 的 checkpoint、interrupt、streaming 细节)优先参考该文档。

### 仓库当前状态

- `copilot_backend/` 空目录
- `copilot_frontend/` 空目录
- `docs/` 有 `adr/`(31 个 ADR)、`research/`(1 份调研)、`SPEC.md`(本文档)
- `CONTEXT.md` 在根目录
- `CLAUDE.md` 在根目录,指向 `docs/agents/`

### 下一步

`/to-spec` 完成后,`/to-tickets` 把 SPEC 切成可独立 implement 的 ticket,声明 blocking 边。`/implement` 每个 ticket 独立上下文。
