---
title: Webhooks
---

# Webhooks

Webhook 让外部系统能够通知 Manor AI，也让 Manor AI 能够调用外部系统。

## 入站 Webhook {#inbound-webhooks}

入站 webhook 需要：

- 一个公开的 HTTPS URL。
- 提供商特定的密钥或签名设置。
- 在 Manor AI 中配置好的路由或集成。

## 出站 Webhook {#outbound-webhooks}

出站 webhook 在条件允许时应使用 HMAC 或提供商支持的签名机制。

## 安全 {#security}

- 定期轮换 webhook 密钥。
- 当提供商支持签名时，拒绝未签名的请求。
- 记录足够的元数据以便排查投递问题，但不要记录包含敏感内容的载荷。

## 故障排查 {#troubleshooting}

如果 webhook 没有到达：

1. 确认 `PUBLIC_BASE_URL`。
2. 检查提供商的投递日志。
3. 检查 API 日志。
4. 确认路由已启用且可访问。
