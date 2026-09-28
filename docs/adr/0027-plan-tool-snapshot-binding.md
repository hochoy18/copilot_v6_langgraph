# ADR-0027: Plan-Tool 版本绑定 — Plan 持有 Tool 定义快照

## 状态
已接受

## 背景
Plan 在执行过程中(可能跨数秒到数分钟),管理员可能修改 Tool 定义(参数变了、风险等级变了、描述变了)。如果 Plan 实时引用 Tool 最新定义,执行中语义会漂移,审计 / 复盘无法还原"当时的 Plan 实际调的是什么"。

## 决策
Plan 生成时,冻结其引用的所有 Tool 定义快照:

1. **快照内容** — Tool 名称、描述、JSON Schema 参数、`risk_level`、HTTP 请求模板。
2. **快照位置** — Plan 文档嵌入(`plan.tool_snapshots: [...]`);不另外存表,随 Plan 一起进 MongoDB。
3. **执行依据** — Tool Worker 按快照里的 schema 校验参数(详见 ADR-0020),按快照里的 risk_level 决定 HITL(详见 ADR-0004),按快照里的 HTTP 模板发请求。
4. **Tool 定义更新影响** — 已在执行的 Plan 不变;新生成的 Plan 才用最新 Tool 定义。
5. **审计关联** — 审计日志记录 Plan ID + Tool 快照内容,可还原"那次执行调的是什么版本的 Tool"。

## 后果
- Plan 可重现:从 MongoDB 拉出 Plan 文档,就能完整还原当时调的是什么 Tool、什么参数、什么风险等级。
- 审计可追溯:合规审计时,审计员能看清"那一刻"的状态,而不是"现在的 Tool 是什么样子"。
- Plan 文档体积略大(嵌入了 Tool 快照) — Tool 定义本身不大,可接受。
- Tool 定义更新对未执行的 Plan 没有自动影响(用户主动发起新 Plan 才会用新版),符合"显式优于隐式"原则。
