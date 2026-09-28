# ADR-0008: MongoDB 与 Milvus 的存储分工

## 状态
已接受

## 背景
后端选型已定:Python + FastAPI + MongoDB + Milvus。两个存储各有强项,但职责不清会导致两边写入重复、读路径绕弯、向量与文档一致性难维护。

## 决策
按"业务真相 / 向量记忆"二八开分工:

**MongoDB 存(业务真相)**
- 用户、角色、Tool 注册、Tool 注册中心元数据
- 会话、用户轮次、消息历史、Plan 快照、执行轨迹、审计日志
- 配置、租户级设置

**Milvus 存(向量记忆)**
- 历史 Plan 的向量表示(用于跨轮 / 跨会话语义召回)
- 历史 Tool 调用的向量摘要(同上)
- Tool 描述的**向量**(原文存 MongoDB;向量可选,MVP 阶段先用 MongoDB 文本匹配,Tool 数量撑不住时再启用向量召回)

**MongoDB 永远是真相源,Milvus 是派生索引。** Milvus 重建不影响业务,业务数据丢失则影响。

## 后果
- 写顺序:先写 MongoDB,再写 Milvus(写入失败时 MongoDB 仍是真相)。
- 删除/更新时同步删除/更新 Milvus 中的向量。
- Tool 选择路径:MVP 阶段 MongoDB 文本匹配足够,MongoDB 不够时(几千个 Tool)切到 Milvus 向量召回。
- 审计日志只入 MongoDB,不入 Milvus。
