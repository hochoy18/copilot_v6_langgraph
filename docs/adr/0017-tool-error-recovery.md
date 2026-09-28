# ADR-0017: Tool 错误恢复 — 按风险等级分级

## 状态
已接受

## 背景
每个 Tool 调用都可能失败:网络超时、5xx、4xx 业务错误、参数缺失等。一刀切"全停"会让系统很难用(用户被迫处理网络抖动);一刀切"全重试"在 write/destructive 场景会重复副作用(重复扣款、重复发邮件)。

## 决策
按 Tool 的 risk_level 与错误类型分级处理:

1. **read 类 Tool**
   - 网络错误 / 超时 / 5xx:自动重试,最多 2 次(共 3 次尝试),指数退避(初始 1s 翻倍,上限 8s)
   - 4xx 业务错误(参数错、权限错):不重试,立即停下 HITL
2. **write / destructive 类 Tool**
   - 任意失败:立即停下 HITL,**不自动重试**
   - 业务人员决定:重试 / 跳过此节点 / 中止整个 Plan
3. **Plan 状态可恢复** — 出错时,Planner 拿到错误信息后可决定:重做此节点、改 Plan、放弃。LangGraph 的 checkpoint 机制支持此模式。

## 后果
- read 类对网络抖动鲁棒,体验好。
- write/destructive 不重复副作用,安全。
- 错误信息要进审计日志(含 request、response、error、retry count)。
- Planner 在节点失败后如何应对(重做 / 改 Plan / 放弃)是 Planner 行为的一部分,具体策略由 Prompt 决定,在 Langfuse 上可调。
