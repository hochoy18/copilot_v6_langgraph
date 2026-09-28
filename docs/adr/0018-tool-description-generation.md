# ADR-0018: Tool 描述生成 — LLM 生成 + 管理员必须复核

## 状态
已接受

## 背景
LLM 选 Tool 靠的是 Tool 描述(语义匹配)。OpenAPI 描述面向开发者("GET /users/{id} — Returns a user by ID"),LLM 用不上("查用户信息"类指令可能匹配不到)。但人工为每个 Tool 重写 LLM-friendly 描述在 Tool 数量多时不可持续。

## 决策
Tool 描述生成采用"LLM 生成 + 管理员必须复核":

1. **导入 OpenAPI 时** — 自动调用 Langfuse 上的 `tool-description-generator` Prompt,基于 OpenAPI operation 生成 LLM-friendly 的描述、参数说明、典型用例。
2. **LLM 生成结果存为 `draft` 状态** — 不直接激活。
3. **管理员在 Langfuse 或后台 review** — 可改写、可补充边界情况、可调整风险等级。
4. **review 通过后** — Tool 状态切为 `active`,LLM Planner 可见可用。
5. **管理员主动下架** — Tool 状态切为 `disabled`,Planner 不可见(配置已存在,但不可调用)。

## 后果
- Tool 描述质量由"LLM 自动 + 管理员兜底"两层保证,运营负担适中。
- `tool-description-generator` Prompt 是关键资产,放在 Langfuse 上,管理员可调优。
- 人工注册的 Tool 描述本身就是管理员写的,无需生成步骤,直接进入 review 流程。
- LLM 生成 Tool 描述的调用本身也走 Langfuse + `data_usage_opt_out=true`(详见 ADR-0016),不外泄企业 API 元数据。
- Tool 状态:`draft` / `active` / `disabled` 三态,详见 CONTEXT.md。
