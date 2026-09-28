# ADR-0014: LLM Provider 抽象 — LangChain ChatModel

## 状态
已接受

## 背景
企业客户对 LLM 提供方有不同偏好:有的只接受国产模型(合规),有的用自托管 vLLM,有的用 OpenAI。直接绑定某个 SDK 会让 swap 变成业务代码改动,且不同客户的私有部署模型接口各异(都"自称"OpenAI 兼容但细节不一)。

## 决策
采用 LangChain 的 `BaseChatModel` 抽象作为 LLM Provider 层:

- 业务代码只依赖 LangChain 的 `BaseChatModel` 接口,不直接依赖任何特定 SDK。
- 默认 Provider:OpenAI 兼容协议(可指向 OpenAI / DeepSeek / 豆包 / 自托管 vLLM 等任意 OpenAI 兼容端点)。
- 切换 Provider 仅需修改运行时配置(base_url + api_key + model_name),不改业务代码。
- 不支持 Anthropic(用户明确排除)。

## 后果
- 新 Provider 接入只需在 LangChain 体系内提供对应 `BaseChatModel` 实现,业务侧零改动。
- 流式输出、Tool calling、token 计数等能力统一通过 LangChain 抽象。
- LangChain 抽象本身有 bug 时,需要升级 LangChain 版本(由 LangChain 维护)。
- 多模型路由(MVP 所有 LLM 调用走同一 Provider 配置;不同任务用不同模型)是后续优化项,当前不做。
