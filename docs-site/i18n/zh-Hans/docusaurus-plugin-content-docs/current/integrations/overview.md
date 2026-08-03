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

提供商需要一个稳定的 HTTPS 回调 URL。请将 `PUBLIC_BASE_URL` 设置为
你的部署对外可访问的 URL。

## 本地测试 {#local-testing}

进行本地 webhook 测试时，请使用隧道工具，并在测试期间临时更新
`PUBLIC_BASE_URL`。
