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
| WhatsApp Business | Meta Cloud API webhook | 通过 Meta Embedded Signup 连接客户自有 WABA 和业务号码。 |
| SMS / 语音（Twilio） | Webhook + Media Streams | 语音通话通过 websocket 将音频流式传输给实时语音 Agent（Deepgram STT + TTS——参见[配置](../configuration#channel-integrations)）。 |
| 微信公众号 | 固定回调地址 | 通过公众号 API 收发客服消息和模板消息。请在明文模式下配置，并使用 Integration 健康详情中显示的完整地址：`https://<公网域名>/api/v1/channels/wechat/callback?config_id=<ChannelConfig ID>`。 |
| 微信个人号 | `wechat-runner` 回调 | `wechat-runner` 服务桥接一个个人号形式的微信会话（在 `http://localhost:8801/qr.png` 扫码）。 |
| Facebook 主页 / Messenger | HMAC 验证的 webhook | 生产环境权限需要通过 Meta 应用审核。 |
| Slack | 通用回调 | 自动处理 Slack 的 `url_verification`。 |
| Discord | 通用回调 + 机器人 | 交互签名使用 `DISCORD_PUBLIC_KEY` 验证。 |
| 电子邮件 | SMTP/入站适配器 | 使用部署的电子邮件配置。 |
| 网页聊天 | 内置 | 可嵌入的公开聊天，无需外部提供商。 |

每个已连接的渠道都是你实体上的一份**渠道配置**，凭据以加密方式存储
（Vault）。发来消息的联系人会成为**渠道联系人**——按渠道划分的身份，
可选择由某个 Manor 用户认领并分配角色（`external`、`member`、`admin`），
该角色决定 Agent 在该会话中可以使用哪些工具。部分渠道允许联系人级路由；
WhatsApp Business 始终使用该业务号码唯一、精确、有效的 Agent Binding。

## WhatsApp Business 设置 {#whatsapp-business-setup}

Manor 只提供一个账号级 Embedded Signup 流程。客户拥有 Meta Business
Portfolio、WABA、业务号码、账单、模板及合规责任；Nango 保管客户授权的
delegated token。正常流程不要求手动填写 access token、App Secret、WABA ID、
phone ID 或 callback。

1. 打开**集成**，找到 **WhatsApp Business** 并点击**连接**。
2. 以 Business Portfolio 管理员身份完成 Meta Embedded Signup，选择或创建
   客户的 WABA 和可用 Cloud API 号码。
3. 执行**测试连接**。它只检查 OAuth、资产关系、号码注册、准确的 Manor
   Meta App 订阅和固定 callback。只有 Meta 要求注册恢复时才输入六位 PIN。
4. 打开 **Agent Channels**，使用现有 Agent Binding 控件，把该号码绑定到
   一个准确的 Agent 部署和 Workspace。
5. 从另一个 WhatsApp 账号给业务号码发消息，或打开
   `https://wa.me/<不带加号的 E164 号码>`；业务号码不能给自己发消息。

客户先发消息后会开启 Meta 的 24 小时客服窗口，窗口内可自由文本回复；
窗口外必须显式选择已审批模板，Manor 不会自动猜测或替换模板。

自托管运维需要创建 Facebook Login for Business v4 配置并选择
**Embedded Signup** 与 **Cloud API**，为 `whatsapp_business_management` 和
`whatsapp_business_messaging` 取得 Advanced Access，并提供公网 HTTPS callback。
staging 与 production 必须使用不同的 Meta App、Config ID、App Secret、verify
token、Nango provider config 和 callback host。production Nango 重定向地址必须
使用 `${NANGO_PUBLIC_URL}/oauth/callback`，不能复制 staging Nango host。

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
GET/POST /api/v1/channels/wechat/callback?config_id=...   # 公众号；明文模式
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
