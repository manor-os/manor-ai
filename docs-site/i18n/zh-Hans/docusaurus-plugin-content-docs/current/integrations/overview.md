---
title: 集成概览
---

# 集成概览

Manor AI 通过提供商凭据、webhook、OAuth 以及可选的 Nango，
支持自托管的集成能力。

## 集成类型 {#integration-types}

- 用于入站和出站事件的 webhook。
- 基于 OAuth 的提供商连接。
- 基于 API 密钥的工具。
- 针对 Nango 所支持的提供商，使用由 Nango 承载的 SaaS 连接器。

## 凭据 {#credentials}

请通过应用设置或由密钥后端支撑的集成流程来存储凭据。切勿将提供商凭据
提交到源代码仓库。

自托管集成使用部署者自有账户。可选供应商未配置时显示配置要求，不会回退到
Manor 凭据。商业额度放行不影响 Entity 权限和工具审批；安装或交付蓝图不会
复制凭据，也不会自动给接收方 provider access。

### 国际版 Lark OAuth {#international-lark-oauth}

管理员为部署注册国际版 Lark OAuth 应用并配置其凭据；每位用户仍需单独连接并授权
自己的 Lark 账号。重新连接必须使用相同账号和应用。Lark 凭据及已连接账号的令牌
不会通过蓝图迁移。历史飞书应用和机器人连接与此国际版 Lark OAuth 流程保持独立。
回调 URL 和所需权限见[配置](../configuration#international-lark-oauth)。

### 模型 Gateway

`POST /api/v1/llm/chat/completions` 支持普通和流式请求，使用目标 Entity 配置的
BYOK 模型；请求模型必须与配置匹配。用户 access token 保留当前选中的 Entity
及有效成员权限。机器客户端复用启动 bootstrap：在部署环境设置
`MANOR_OAUTH_CLIENT_<SLUG>_SECRET`、`_REDIRECT_URIS` 和 `_RESTRICTED_ENTITY_ID`，
请求携带 `X-Manor-Client-Id`、`X-Manor-Client-Secret` 与匹配的
`X-Manor-Entity-Id`。`X-Manor-User-Id` 可省略；提供时该用户必须为目标 Entity 的
有效成员。Entity 请求头本身不是授权证明。

`GET /api/v1/llm/credit` 只检查商业 credits 闸口；OSS 放行不代表供应商已准备好，
也不取消执行预算。

## 公开 URL {#public-urls}

第一方 OAuth 提供商需要基于 `APP_URL` 的稳定回调 URL：
`{APP_URL}/api/v1/integrations/oauth/{server_key}/callback`。入站 webhook
提供商则需要把 `PUBLIC_BASE_URL` 设置为外部可以访问的 API URL。

## 本地测试 {#local-testing}

进行本地 webhook 测试时，请使用隧道工具，并在测试期间临时更新
`PUBLIC_BASE_URL`。
