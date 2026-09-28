# ADR-0032: 前端 Token 存储策略

## 状态
已接受

## 背景
后端 ADR-0009 定义 Access Token(JWT, 15 min) + Refresh Token(不透明, 7 day)。前端要把它们存在某处,以便后续请求 / SSE 订阅能取用。考虑到 ADR-0010 的 SSE 鉴权走 Query Param,token 必须可被 JS 访问,因此不能放 httpOnly cookie。

## 决策

1. **Access Token 存内存**(Zustand store,刷新页面即丢)
2. **Refresh Token 存 localStorage**(可承受 XSS 风险:7 天长寿,泄露后会被后端轮换机制发现并吊销)
3. **SSE 连接**:从内存中读 Access Token,放进 Query Param(详见 ADR-0010)
4. **页面刷新**:Access Token 丢失,前端用 Refresh Token 调 `/api/v1/auth/refresh` 换新 Access Token,无感续期
5. **关闭 tab**:Access Token 丢,Refresh Token 仍在 localStorage,下次开 tab 自动恢复

## 后果

- SSE 鉴权能拿到 token(从内存直接读)
- 7 天 Refresh Token 是 XSS 攻击的"金矿",需要前端严格 CSP + 输入消毒 + 严格 Langfuse 注入检测(详见 ADR-0022)
- 多个 tab 同时打开会各自维护一份内存中的 Access Token,过期时各自刷新;后端 Refresh Token 轮换机制保证不会乱
- 如果未来想完全免疫 XSS:Refresh Token 也搬到 httpOnly cookie + BFF 层,后端需要调整(不在 MVP 范围)
- 当前方案是"够用就好",不是"最安全",但匹配 MVP 的简洁原则
