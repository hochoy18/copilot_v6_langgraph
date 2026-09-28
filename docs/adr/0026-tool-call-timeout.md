# ADR-0026: Tool 调用超时 — 可配置,默认 30s

## 状态
已接受

## 背景
每个 Tool 调用都可能因上游 API 卡顿而长时间不返回。Tool Worker 死等会占住 SSE 连接、阻塞 Plan 执行、拖垮系统。

## 决策
Tool 可配置 `timeout_seconds`(默认 30s):

1. **Tool 注册时** — 管理员可设置 per-Tool timeout,覆盖全局默认值。
2. **调用时** — Tool Worker 在 `timeout_seconds` 内未收到响应 → 视为超时。
3. **超时处理** — 按 ADR-0017 错误恢复策略:
   - read 类 Tool:重试 2 次(指数退避),仍超时则停下 HITL
   - write / destructive 类 Tool:立即停下 HITL,**不重试**
4. **超时错误类型** — `timeout`,与 `schema_violation`、`credential_invalid` 并列。

## 后果
- Tool Worker 不会因上游卡顿而被永久阻塞。
- 长耗时 API(如报表生成)需管理员显式配置更长 timeout,系统不会"自作主张"地等。
- 超时配置错误会导致体验差(短了误中断),上线初期监控告警要关注超时频率。
