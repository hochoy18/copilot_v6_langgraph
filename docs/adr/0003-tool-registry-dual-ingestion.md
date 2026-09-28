# ADR-0003: 双接入路径的 Tool 注册中心

## 状态
已接受

## 背景
系统需要让 LLM Agent 能调用企业内部 API。真实企业 API 资产是异构的:大部分现代 API 都有 OpenAPI/Swagger 文档,但仍存在有意义的"长尾"(legacy、内部、自研)没有。要求客户为成百上千个 API 手写 Tool schema 在运维上不可承受;但拒绝所有没 OpenAPI 的 API 又会让价值打折。

## 决策
Tool 注册中心从两条来源接收 Tool,在同一份内部 Tool schema 上汇聚:

1. **OpenAPI 导入(主)** — 粘贴 Swagger URL 或上传 spec 文件。注册中心解析后,每个 operation 派生一个 Tool,管理员在激活前可检查/修改名称、描述、风险等级。
2. **人工注册(兜底)** — 管理员手动填写名称、描述、JSON Schema 参数、HTTP 请求模板(method / URL / headers / body)、风险等级。

两条路径产出相同的内部记录。LLM 永远只看到内部记录。

## 后果
- 内部 Tool schema 是注册中心与运行时的契约,也是 LLM 唯一消费的形态。两条接入路径都是适配器。
- OpenAPI 解析必须对部分 / 不合规 spec 优雅降级(该 operation 退回人工,不可静默丢弃)。
- 导入后的管理员审核步骤不可省略:OpenAPI 描述面向开发者,通常不适用于 LLM Tool 描述。
- 注册中心保留原始导入产物(OpenAPI 文档或人工草稿)与活跃 Tool 的对应关系,以便上游 API 变更时重新同步。
