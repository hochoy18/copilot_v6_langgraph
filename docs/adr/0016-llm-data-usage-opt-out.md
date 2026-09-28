# ADR-0016: LLM 数据使用 — Provider 配置可控

## 状态
已接受

## 背景
企业客户常禁止 LLM 提供方用其输入训练或保留日志。不同 Provider 的协议不同:OpenAI 提供 zero-retention endpoint、Azure OpenAI 私有部署默认 no-train、国产模型厂商有各自的脱敏模式。强制某一种会限制 Provider 选型;完全不管又让企业合规过不去。

## 决策
LLM Provider 配置带 `data_usage_opt_out` 标志:

- **开启(true)** — 调用走 no-train 路径(OpenAI zero-retention endpoint / Azure OpenAI 私有部署 / 国产模型的脱敏模式)。
- **关闭(false)** — 调用走默认路径(各 Provider 标准 API,可能用输入训练)。
- **默认** — 企业部署默认 `true`,启用 opt-out。
- **启动校验** — 若 `data_usage_opt_out=true` 但所选 Provider 不支持 no-train 路径,启动失败并明确报错。

## 后果
- 企业部署默认安全(默认 opt-out,数据不外泄)。
- Provider 必须支持 no-train 路径才能在 `data_usage_opt_out=true` 模式下启用 — 选 Provider 时需校验能力。
- 不支持 no-train 的 Provider 仍可接入,但客户需明确接受数据可能被使用。
- Prompt 中可能含敏感数据(用户输入 / Tool 返回)— `data_usage_opt_out=true` 是这些数据不外泄的最终防线。
