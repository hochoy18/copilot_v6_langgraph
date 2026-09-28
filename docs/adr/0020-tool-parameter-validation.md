# ADR-0020: Tool 参数校验 — 后端严格 schema 校验

## 状态
已接受

## 背景
LLM 生成的 Tool 调用参数可能幻觉(类型错、必填缺失、enum 越界)。如果直接调用上游 API,可能:收到 4xx 浪费时间、构造出危险请求、写入脏数据。靠 Prompt 强调不够 — LLM 幻觉不可避免。

## 决策
Tool 调用前,后端用 Tool 注册时存的标准 JSON Schema 严格校验 LLM 生成的参数:

1. **校验时机** — Tool Worker 接到参数后,调用上游 API 之前。
2. **校验依据** — Tool 注册时存的标准 JSON Schema(类型、必填、enum、format)。
3. **校验失败处理** — 不调用上游 API,返回结构化错误给 Planner:
   - 错误类型:`schema_violation`
   - 错误内容:哪个字段、违反了哪条规则
   - Planner 据此重生成参数(或根据 ADR-0017 决定停下 HITL)
4. **校验通过** — 才调用上游 API,Audit 记录"校验通过"。

## 后果
- LLM 幻觉不直接打到上游 API,系统鲁棒。
- 上游 API 收到的请求一定 schema 合规,4xx 错误率显著下降。
- 后端需要一份稳健的 JSON Schema 校验实现(Python 推荐 `jsonschema` 库)。
- Tool 注册时必须提供完整、准确的 JSON Schema — 这是管理员的责任。
- 对没声明 schema 的 Tool,后端拒绝注册(强制 schema 完整性)。
