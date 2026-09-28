# ADR-0025: Langfuse 可观测性

## 状态
已接受

## 背景
ADR-0013 已把 Prompt 管理放在 Langfuse 上(版本化、A/B、回放)。但 Langfuse 的能力不止于此 — 完整 trace、token / cost 监控、延迟指标、用户反馈闭环都是企业级 Agent 必须的运营能力。LLM 调用失败 / 慢 / 贵 / 答非所问,没有可观测性就无从调优。

## 决策
后端所有关键路径都通过 Langfuse SDK 上报 trace 与指标,扩到三层:

### 1. Tracing

- **Trace 范围** — 每个用户轮次(从收到 User Turn 到返回最终响应或失败)。
- **Span 嵌套** — Planner LLM 调用、Tool 调用、注入检测 LLM 调用、压缩 LLM 调用、Milvus 召回 — 每个都是 trace 内的一个 span。
- **关联字段** — `trace_id`、`session_id`、`user_id`、`conversation_id`、`plan_id`,在 Langfuse 上可按任意维度回溯。

### 2. 指标采集

- 每次 LLM 调用的 token 用量(prompt / completion / total)
- 每次调用的延迟
- 每次 Tool 调用的状态码 / 错误类型(尤其 `credential_invalid` 单独标记)
- 估算 cost(Langfuse 按 model 自动计算)
- Langfuse 上管理员可按 session / user / Tool / Prompt 维度聚合

### 3. 成本上限(可配置)

- **每会话阈值** — 默认 100k tokens(可按部署调整)。超过 → SSE 推一条 `cost.warning` 事件给前端,业务人员看到提示。
- **硬上限** — 默认 5 倍软阈值(即 500k tokens)。超过 → 强制中断当前会话,业务人员需开新会话继续。
- **每用户每日上限** — 默认 1M tokens(可调)。超过 → 锁定用户 24 小时,管理员可手动解锁。
- **所有阈值通过部署配置**,不在代码里硬编码。

### 4. 用户反馈

- 业务人员对 Copilot 的**最终回答**可 thumbs up / down + 可选 comment(中间步骤不评)。
- 反馈关联到对应 trace / span,Langfuse 收集用于后续评估与调优。
- 反馈进审计日志。

### 5. Prompt 与 Trace 关联

- 每次 LLM 调用的 trace 自动标注 Prompt 名 + 版本。
- Langfuse 上从 trace 可一键跳到 Prompt 配置页。
- A/B 测试:同一 Prompt 名下多个版本可灰度(后续 V1.1,MVP 默认全量)。

## 后果
- 所有 LLM / Tool 调用可观测、可回放、可分析。
- 调优有数据支撑(token 哪里贵、哪里慢、哪个 Tool 失败率高、哪个 Prompt 版本好)。
- 用户反馈形成持续优化闭环。
- 成本失控有预警 + 硬阻断,不会因一个长会话把整个部署的 token 预算烧光。
- Langfuse 不可用时降级:trace / 指标采集失败不影响业务(本地缓存最近 trace,Langfuse 恢复后异步上报);但失去实时观测能力,需告警运维。
- 进一步可接 Langfuse Evaluations(LLM-as-judge、人工标注)做质量评估 — 这是 V1.1+ 的事,MVP 不必。
