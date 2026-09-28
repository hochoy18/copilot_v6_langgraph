# ADR-0033: LLM 集成落地偏差 — Prompt 获取、bootstrap 兜底、opt-out 校验时机

## 状态
已接受(收编 T16 / #14 实现时与 ADR-0013 / ADR-0016 的三处文字冲突;不取代两条 ADR 的整体决策,只修订实施细节)

## 背景
T16 是仓库第一次真实接入 LLM 链路(`tool-description-generator` Prompt 改写 Tool 描述)。落地时,ADR-0013 / ADR-0016 的三处表述与工程现实冲突:

1. ADR-0013 说"后端通过 Langfuse SDK 拉取 Prompt 模板",但 Langfuse Python SDK v3 捆绑 OpenTelemetry 全家桶,其价值在 T40(全链路 trace)才兑现;MVP 只需要一个 `GET /api/public/v2/prompts/{name}` 只读接口,且 SDK 为同步 API,在 async 请求路径中会阻塞事件循环。ADR-0015 的依赖注释(`copilot_backend/pyproject.toml` httpx 条目)已把 httpx 定性为"Langfuse 流量的传输层"。
2. ADR-0013 说"代码仓库不留 Prompt 内容",降级策略是"本地缓存的最近版本 Prompt"。但"缓存"预设了至少成功拉取过一次;全新部署首启动恰逢 Langfuse 不可达时,没有任何缓存,功能完全不可用。
3. ADR-0016 要求"启动校验:data_usage_opt_out=true 且 Provider 不支持 no-train → 启动失败"。但 `app/main.py` 的既定契约是依赖不可达也允许降级启动(healthz 报 503 交给部署探针),且大量开发/测试部署根本不配置 LLM——boot 期硬失败会把它们全打死。另外"Provider 是否支持 no-train"无法自动探测(OpenAI ZDR 是合同/端点级,国产模型是控制台级),只能由部署方声明。

## 决策
以下三项作为 MVP 实施细节,修订 ADR-0013 / ADR-0016 的对应文字:

1. **Prompt 拉取走 Langfuse 公共 REST API(httpx),不走 SDK。** 实现见 `app/llm/prompts.py`。T40 引入 SDK 做 trace 上报时,本 Provider 可无缝改为委托 SDK;调用方(`get_prompt(name)`)不感知。
2. **允许代码内嵌"bootstrap 兜底模板",作为降级阶梯的地板。** 阶梯固定为:Langfuse 新鲜拉取(TTL 内)→ 进程内最近成功副本 → 代码内嵌 bootstrap。约束:bootstrap 只是可用性地板、不是 canonical 资产;Langfuse 上的同名 Prompt 一经建立即生效并在后续拉取中覆盖;`source` 字段随每次解析记录(`langfuse` / `cache` / `bootstrap`)供 trace / 调试回放。这是对"仓库不留 Prompt 内容"的有意收窄:**不留的是运营资产,留的是不可用兜底**。
3. **ADR-0016 的校验时机从"启动失败"改为"首次使用时拒绝"。** `app/llm/provider.py` 的 `build_chat_model` 在每次构造 ChatModel 时校验;Provider 能力由部署方通过 `llm_provider_supports_no_train` 显式声明(默认 true);`llm_data_usage_opt_out=true` 且声明 false → 构造抛 `LLMConfigurationError`,生成路径降级为管理员可见的警告,绝不静默调用可训练端点。启动期由 lifespan 记一条 warning 日志,不 fail boot——与仓库降级启动契约一致,同时保留 ADR-0016"明确报错"的语义。

随附实施参数(可逆,不视为 ADR 级决策,记录备查):单次 OpenAPI 导入预览最多对前 32 个 operation 自动生成描述、并发 5;超限与跳过项在响应级 `warnings` 明示(ADR-0003 不静默丢弃),逐条补生成由 follow-up 工单跟进。

## 后果
- 三条偏离从"代码注释里的私刑"变成有记录的决议,后续 review / 审计不再反复报同一冲突。
- T40 接入 Langfuse SDK 时,应回头评估是否把 Prompt 拉取也切回 SDK,并更新本 ADR(或以新 ADR 取代第 1 条)。
- bootstrap 模板与 Langfuse 上的正式 Prompt 存在漂移风险:管理员在 Langfuse 建好同名 Prompt 前,生成质量由代码内模板决定——上线 checklist 应包含"在 Langfuse 创建 `tool-description-generator`"一步。
- opt-out 的保障强度从"进程级不变量"降为"调用级不变量":任何构造 ChatModel 的代码路径都会被迫过校验,但绕过工厂直接 new SDK 对象的代码不会被拦——依赖 code review 与 `app.llm.provider` 作为唯一入口来兜底。
