---
title: 消息渠道
---

# 消息渠道

渠道将外部会话——WhatsApp 上的客户、团队的 Slack、一个收件箱——
连接到 Agent 运行时。入站消息会被路由到正确的工作区和 Agent，可以创建
任务，回复则通过同一渠道发回。

## 支持的渠道 {#supported-channels}

| 渠道 | 传输方式 | 说明 |
| --- | --- | --- |
| Telegram | Webhook **或**长轮询 | `TELEGRAM_MODE=webhook\|polling\|auto`；`auto` 在 `PUBLIC_BASE_URL` 为 HTTPS 时使用 webhook，否则使用轮询——因此在没有公网 URL 的笔记本上 Telegram 也能工作。 |
| WhatsApp | Meta Cloud API webhook | Meta 验证挑战 + 消息 webhook。 |
| SMS / 语音（Twilio） | Webhook + Media Streams | 语音通话通过 websocket 将音频流式传输给实时语音 Agent（Deepgram STT + TTS——参见[配置](../configuration#channel-integrations)）。 |
| 微信 | 公众号回调，或 `wechat` Compose profile | `wechat-runner` 服务桥接一个个人号形式的微信会话（在 `http://localhost:8801/qr.png` 扫码）。 |
| Facebook 主页 / Messenger | HMAC 验证的 webhook | 生产环境权限需要通过 Meta 应用审核。 |
| Slack | 通用回调 | 自动处理 Slack 的 `url_verification`。 |
| Discord | 通用回调 + 机器人 | 交互签名使用 `DISCORD_PUBLIC_KEY` 验证。 |
| 电子邮件 | SMTP/入站适配器 | 使用部署的电子邮件配置。 |
| 网页聊天 | 内置 | 可嵌入的公开聊天，无需外部提供商。 |

每个已连接的渠道都是你实体上的一份**渠道配置**，凭据以加密方式存储
（Vault）。发来消息的联系人会成为**渠道联系人**——按渠道划分的身份，
可选择由某个 Manor 用户认领并分配角色（`external`、`member`、`admin`），
该角色决定 Agent 在该会话中可以使用哪些工具。联系人也可以被固定到
特定的 Agent。

## 路由到工作区 {#routing-into-workspaces}

在工作区的**渠道**标签页中，绑定一个渠道并将其映射到合适的 Agent
（例如：预约收件箱 → 礼宾 Agent）。此后入站消息会在该工作区中创建
会话和任务，Agent 则通过该渠道回复。

## 配对码 {#pairing-codes}

对于用户需要将外部身份关联到 Manor 账户的聊天应用（例如为团队服务的
Telegram 机器人），Manor 使用短配对码：

1. 运维人员在 Manor 中生成一个配对码（`POST /api/v1/channel-pairings`）。
2. 用户将配对码发送给机器人。
3. 机器人兑换该配对码（`POST /api/v1/channel-pairings/redeem`），
   外部身份即与该 Manor 用户完成关联。

配对码一次性有效，几分钟后过期，并且可以绑定到特定工作区。

## Webhook URL {#webhook-urls}

入站 webhook 位于 `/api/v1/channels/` 之下：

```text
POST /api/v1/channels/telegram/webhook/{bot_token_hash}
GET/POST /api/v1/channels/whatsapp/webhook
POST /api/v1/channels/twilio/sms | /twilio/voice | /twilio/status
GET/POST /api/v1/channels/wechat/callback
GET/POST /api/v1/channels/facebook/webhook
POST /api/v1/channels/{channel_type}/callback?config_id=...   # generic (Slack, Discord, …)
```

所有这些端点都要求 `PUBLIC_BASE_URL` 能被提供商访问——生产环境必须是
HTTPS。签名验证（Meta HMAC、Discord Ed25519、Twilio 签名）会拒绝未签名的
流量；从提供商的角度看，签名不匹配会静默失败，所以当消息迟迟不到时，
请务必仔细检查应用密钥。

## 内部私信 {#internal-direct-messages}

与外部渠道分开，Manor 还提供团队私信（侧边栏中的**消息**）：同一实体
用户之间的人对人会话线程，由 `GET/POST /api/v1/messages` 和
`/api/v1/messages/threads` 支持。
