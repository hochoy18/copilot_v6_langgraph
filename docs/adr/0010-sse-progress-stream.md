# ADR-0010: SSE 推送协议

## 状态
已接受

## 背景
Plan 生成、Tool 执行、LLM 流式输出需要把进度实时推到前端。三种主流方案各有取舍:SSE 单向、HTTP/1.1 兼容、易实现;WebSocket 双向、可中断但复杂;轮询简单但延迟高且浪费带宽。

## 决策
采用 SSE (Server-Sent Events) 作为进度推送协议。后端在用户轮次开始后建立一个 SSE 连接,推送以下事件:

- `plan.generated` — Plan 已就绪,等待 HITL 确认
- `plan.modified` — 业务人员编辑了 Plan
- `tool.started` / `tool.finished` / `tool.failed` — 单个 Tool 的生命周期
- `llm.token` — LLM 流式输出(最终回答)
- `execution.completed` — 整个 Plan 执行完成
- `cost.warning` — 当前会话接近成本上限(详见 ADR-0025)

鉴权通过 SSE 连接建立时 Query Param 中的 Access Token 完成(EventSource API 不支持自定义 Header,只能用 Query Param)。SSE 通道本身不做鉴权,只看连接建立时 token 是否合法。

## 后果
- 单向足够,业务人员主动操作(批准 Plan / 编辑 / 取消)走普通 HTTP POST,不依赖 WebSocket 双向能力。
- SSE 连接建立后由后端持有,断线时前端自动重连(EventSource 原生)。
- 后端需要为每个活跃用户轮次维持一个 SSE 连接 — 单用户并发量低,资源可控。
- 如未来需要"业务人员中途取消正在执行的 Plan"等中断能力,可平滑升级到 WebSocket 而不破坏前端接口。
