# ADR-0021: 多轮上下文 Token 处理 — 依赖 LLM Context Window

## 状态
已接受

## 背景
多轮会话越长,上下文越长。三种典型策略:硬上限截断、超限压缩、不设限。前两种控制成本但打断体验;第三种让模型自己管理。考虑主流 LLM 已普遍支持 100k+ context window,且 LangChain 自动按模型 context window 截断 message 列表,业务实践上"不设限"通常足够。

## 决策
不设应用层硬上限,依赖 LLM Context Window:

- Planner 每次调用,把记忆窗口(K=5,见 ADR-0007)+ 长期记忆召回 + Plan 上下文 + 当前 User Turn 一起交给 LLM。
- 不做应用层截断、压缩、拒绝。
- LangChain 自动按模型 context window 截断 message 列表(超出会从最早的开始丢)。
- 上线后通过 Langfuse 监控每次调用的实际 token 用量,设告警阈值。

## 后果
- 简单:应用层不维护截断/压缩逻辑。
- 长会话体验连贯,不强制用户拆会话。
- 极端长会话(几百轮)成本不可控 — 通过 Langfuse 监控 + 告警阈值发现异常会话。
- LLM context window 不足时,LangChain 自动截断可能导致早期上下文丢失 — 这是 LangChain 的默认行为,可接受。
- 如未来发现成本失控,可在本 ADR 上叠加"软上限 + 压缩"策略,业务代码不需重写。

## 边界澄清
本 ADR 的"不设应用层硬上限"指**单次 Planner LLM 调用的 context window 上限**(由 LLM 自身 + LangChain 管理)。与 ADR-0025 的**会话级 token 累计成本上限**是不同维度,两者不冲突:
- ADR-0021:管"一次 LLM 调用能塞多少 token"(context 维度)
- ADR-0025:管"一个会话累计消耗多少 token"(成本维度)
