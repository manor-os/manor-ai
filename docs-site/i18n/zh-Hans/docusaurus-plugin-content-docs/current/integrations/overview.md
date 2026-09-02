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

## 公开 URL {#public-urls}

第一方 OAuth 提供商需要基于 `APP_URL` 的稳定回调 URL：
`{APP_URL}/api/v1/integrations/oauth/{server_key}/callback`。入站 webhook
提供商则需要把 `PUBLIC_BASE_URL` 设置为外部可以访问的 API URL。

## 本地测试 {#local-testing}

进行本地 webhook 测试时，请使用隧道工具，并在测试期间临时更新
`PUBLIC_BASE_URL`。
