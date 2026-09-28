# ADR-0013: Prompt 管理走 Langfuse

## 状态
已接受

## 背景
产品需要精细控制 LLM 行为,而 Prompt 是行为控制的主载体。把 Prompt 硬编码在代码里,改一次要走部署,无法回放历史调用时实际用的 Prompt 与变量。集中托管在 Langfuse 可以做到:版本化、A/B、追踪每次调用的实际 Prompt 与输出、按会话回放。

## 决策
所有 LLM 调用的 Prompt 集中托管于 Langfuse:

- Planner Prompt、Memory 摘要 Prompt、Tool 选择 Prompt 等模板统一存放 Langfuse,代码仓库不留 Prompt 内容。
- 后端通过 Langfuse SDK 拉取 Prompt 模板,运行时注入变量。
- 每次 LLM 调用的输入 Prompt 与输出 Response 一并上报 Langfuse,形成完整 trace。
- Prompt 版本由 Langfuse 管理,代码仓库只引用 Prompt 名称(不引用版本号 — 默认拿 Langfuse 上的活跃版本)。

## 后果
- Prompt 改完无需发版,Langfuse 上线即生效。
- 每条 LLM 调用可回放(看到 Prompt 版本、变量、返回),调试与审计双赢。
- Langfuse 是关键依赖。Langfuse 不可用时,后端降级策略:MVP 默认用本地缓存的最近版本 Prompt(Langfuse 恢复后自动重新拉取并刷新缓存)。
- Prompt 编辑权限由 Langfuse 控制台管理(本系统不重复实现);后台管理员通过 Langfuse 修改 Prompt。
