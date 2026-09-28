# ADR-0015: 后端技术栈

## 状态
已接受

## 背景
后端选型在多轮决策中已定型,但分散在多份文档里,没有单一权威源。新成员入职、第三方审计、客户合规问询时,都需要一处能回答"你们用什么"的清单。

## 决策
后端技术栈如下:

| 层级 | 选型 |
|---|---|
| 语言 | Python 3.11+ |
| Web 框架 | FastAPI |
| 业务数据库 | MongoDB(单租户实例,详见 ADR-0001) |
| 向量数据库 | Milvus |
| Agent 框架 | LangGraph + LangChain |
| LLM 抽象 | LangChain ChatModel(默认 OpenAI 兼容,详见 ADR-0014) |
| Prompt 管理 / Tracing | Langfuse(详见 ADR-0013) |
| 认证 | OIDC / SAML(业务人员)+ 本地账号(管理员)(详见 ADR-0006) |
| Token 模型 | 短期 JWT + Refresh Token(详见 ADR-0009) |
| 实时推送 | SSE(详见 ADR-0010) |
| 部署 | 单租户私有部署(详见 ADR-0001) |

## 后果
- 技术栈集中决策点在此 ADR,变更需走新 ADR 流程。
- 第三方 SDK 兼容性以 LangChain / FastAPI 生态为准。
- 不在本表内的技术(如 PostgreSQL / Redis / Kafka)引入需新 ADR。
