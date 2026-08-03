---
title: Message Channels
---

# Message Channels

Channels connect outside conversations — customers on WhatsApp, a team
Slack, an inbox — to the agent runtime. Inbound messages are routed to the
right workspace and agent, can create tasks, and replies go back out on the
same channel.

## Supported Channels

| Channel | Transport | Notes |
| --- | --- | --- |
| Telegram | Webhook **or** long-polling | `TELEGRAM_MODE=webhook\|polling\|auto`; `auto` uses webhooks when `PUBLIC_BASE_URL` is HTTPS, otherwise polls — so Telegram works on a laptop with no public URL. |
| WhatsApp | Meta Cloud API webhook | Meta challenge + message webhooks. |
| SMS / Voice (Twilio) | Webhooks + Media Streams | Voice calls stream audio over a websocket to a live voice agent (Deepgram STT + TTS — see [Configuration](../configuration#channel-integrations)). |
| WeChat | Official Account callback, or the `wechat` Compose profile | The `wechat-runner` service bridges a personal-style WeChat session (scan the QR at `http://localhost:8801/qr.png`). |
| Facebook Pages / Messenger | HMAC-verified webhook | Requires Meta app review for production scopes. |
| Slack | Generic callback | Handles Slack's `url_verification` automatically. |
| Discord | Generic callback + bot | Interaction signatures verified with `DISCORD_PUBLIC_KEY`. |
| Email | SMTP/inbound adapter | Uses the deployment's email configuration. |
| Web chat | Built-in | Embeddable public chat, no external provider needed. |

Each connected channel is a **channel config** on your entity, with
credentials stored encrypted (Vault). Contacts who message in become
**channel contacts** — per-channel identities that can optionally be claimed
by a Manor user and assigned a role (`external`, `member`, `admin`), which
governs what tools the agent may use in that conversation. A contact can also
be pinned to a specific agent.

## Routing Into Workspaces

In a workspace's **Channels** tab, attach a channel and map it to the right
agent (for example: bookings inbox → concierge agent). Inbound messages then
create conversations and tasks in that workspace, and the agent replies on
the channel.

## Pairing Codes

For chat apps where users must link their external identity to their Manor
account (for example a Telegram bot serving your team), Manor uses short
pairing codes:

1. An operator mints a code in Manor (`POST /api/v1/channel-pairings`).
2. The user sends the code to the bot.
3. The bot redeems it (`POST /api/v1/channel-pairings/redeem`) and the
   external identity is linked to the Manor user.

Codes are single-use, expire after minutes, and can be workspace-bound.

## Webhook URLs

Inbound webhooks live under `/api/v1/channels/`:

```text
POST /api/v1/channels/telegram/webhook/{bot_token_hash}
GET/POST /api/v1/channels/whatsapp/webhook
POST /api/v1/channels/twilio/sms | /twilio/voice | /twilio/status
GET/POST /api/v1/channels/wechat/callback
GET/POST /api/v1/channels/facebook/webhook
POST /api/v1/channels/{channel_type}/callback?config_id=...   # generic (Slack, Discord, …)
```

All of them require `PUBLIC_BASE_URL` to be reachable by the provider —
HTTPS in production. Signature verification (Meta HMAC, Discord Ed25519,
Twilio signatures) rejects unsigned traffic; a signature mismatch fails
silently from the provider's point of view, so double-check app secrets when
messages don't arrive.

## Internal Direct Messages

Separate from external channels, Manor has team direct messages
(**Messages** in the sidebar): human-to-human threads between users of the
same entity, backed by `GET/POST /api/v1/messages` and `/api/v1/messages/threads`.
