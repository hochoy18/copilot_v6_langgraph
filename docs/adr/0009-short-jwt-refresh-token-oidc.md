# ADR-0009: 短期 JWT + Refresh Token + OIDC code+PKCE

## 状态
已接受

## 背景
认证要解决三件事:(a) 业务人员通过 IdP 联邦登录,(b) 后续请求携带凭证,(c) 凭证过期不影响用户体验。Session 方案需要后端共享存储;透传 IdP token 依赖 IdP token TTL 与本系统期望生命周期吻合;JWT 方案无状态、易水平扩展,但需要明确 Refresh 机制与吊销策略。

## 决策
业务人员与本地管理员采用同一种 token 模型:

1. **登录路径(SSO)** — 标准 OIDC Authorization Code + PKCE 流程。IdP 返回 `id_token` 与 `code`,后端用 `code` 换 `id_token` + `access_token`,验证 `id_token` 签名/issuer/audience 后,签发本系统 token。
2. **Access Token** — JWT,15 分钟有效。载荷包含用户 ID、来源(sso/local)、角色列表。签名密钥保管在后端,不经过前端。
3. **Refresh Token** — 不透明随机串(后端存 MongoDB,可吊销),7 天有效。每次刷新轮换,旧 token 立即失效。
4. **登出** — 后端把 Refresh Token 标记吊销;Access Token 因短期特性自然过期。

## 后果
- 鉴权中间件只看 Access Token 签名与 exp,不查 DB,延迟低。
- Refresh 路径每次查 MongoDB 校验 + 轮换,可即时吊销(管理员强制下线场景)。
- IdP 切换不影响前端,只影响后端 OIDC 适配层。
- Access Token 短生命周期意味着即使泄露,影响窗口 ≤15 分钟。
- 前端在 Access Token 临近过期时(剩余 ≤2 分钟)主动用 Refresh Token 换取新 Access Token,业务人员无感知;用户活跃会话中无需重新登录。
- 跨服务调用(若有)需在网关层做 token 转换,不在业务层处理。
