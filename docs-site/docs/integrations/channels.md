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
| WhatsApp Business | Meta Cloud API webhook | Customer-owned WABA and business number through Meta Embedded Signup. |
| SMS / Voice (Twilio) | Webhooks + Media Streams | Voice calls use native OpenAI Realtime over a WebSocket. Configure the Entity's **Primary AI** BYOK in Account → Models — see [Configuration](../configuration#live-voice-calls). |
| WeChat Official Account | Fixed callback URL | Customer-service and template messages through the WeChat OA API. Configure the callback in plain-text mode with the complete URL shown on the Integration health card: `https://<public-host>/api/v1/channels/wechat/callback?config_id=<ChannelConfig ID>`. |
| WeChat Personal | `wechat-runner` callback | The `wechat-runner` service bridges a personal-style WeChat session (scan the QR at `http://localhost:8801/qr.png`). |
| Facebook Pages / Messenger | HMAC-verified webhook | Requires Meta app review for production scopes. |
| Slack | Generic callback | Handles Slack's `url_verification` automatically. |
| Discord | Generic callback + bot | Interaction signatures verified with `DISCORD_PUBLIC_KEY`. |
| Email | SMTP/inbound adapter | Uses the deployment's email configuration. |
| Web chat | Built-in | Embeddable public chat, no external provider needed. |

Each connected channel is a **channel config** on your entity, with
credentials stored encrypted (Vault). Contacts who message in become
**channel contacts** — per-channel identities that can optionally be claimed
by a Manor user and assigned a role (`external`, `member`, `admin`), which
governs what tools the agent may use in that conversation. Some channels allow
contact-level routing; WhatsApp Business always uses the one exact active
Agent Binding for its connected number.

Browser voice and Twilio voice use the same Entity BYOK model policy as Chat.
Configure the speech providers as well as the model; a platform-only key does
not make self-hosted voice healthy. Without SMTP, outbound email is unavailable
but local documents, signatures and collaboration remain usable. Health/setup
checks do not grant channel or tool permissions.

## WhatsApp Business Setup

Manor uses one account-only Embedded Signup flow. The customer owns the Meta
Business Portfolio, WABA, number, billing, templates, and policy compliance;
Nango holds the delegated customer token. The normal setup requires no manual
access token, App Secret, WABA ID, phone ID, or callback field.

1. Open **Integrations**, find **WhatsApp Business**, and select **Connect**.
2. Complete Meta Embedded Signup as a Business Portfolio administrator and
   select or create the customer WABA and eligible Cloud API number.
3. Run **Test connection**. This checks OAuth, assets, phone registration, the
   exact Manor Meta App subscription, and the fixed callback. A six-digit PIN
   is requested only when Meta requires registration recovery.
4. Open **Agent Channels** and use the existing Agent Binding control to map
   the connected number to one exact Agent deployment and Workspace.
5. Send a message from a different WhatsApp account to the business number,
   or open `https://wa.me/<E164-number-without-plus>`. The business number must
   not message itself.

A customer-initiated message opens Meta's 24-hour customer-service window for
free-form replies. Outside that window Manor requires an explicit approved
template and never silently substitutes one.

Self-hosted operators need a Facebook Login for Business v4 configuration with
**Embedded Signup** and **Cloud API**, Advanced Access for
`whatsapp_business_management` and `whatsapp_business_messaging`, and a public
HTTPS callback. Staging and production must use separate Meta Apps, Config IDs,
App Secrets, verify tokens, Nango provider configs, and callback hosts. The
production Nango redirect is `${NANGO_PUBLIC_URL}/oauth/callback`; never copy a
staging Nango host into production.

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
GET/POST /api/v1/channels/wechat/callback?config_id=...   # Official Account; plain-text mode
GET/POST /api/v1/channels/facebook/webhook
POST /api/v1/channels/{channel_type}/callback?config_id=...   # generic: email inbound, WeChat Personal runner
```

All of them require `PUBLIC_BASE_URL` to be reachable by the provider —
HTTPS in production. Signature verification (Meta HMAC, Discord Ed25519,
Twilio signatures) rejects unsigned traffic. The generic callback accepts a
post only when the channel proves the sender, so a WeChat Personal runner
needs `WECHAT_RUNNER_BEARER_TOKEN` and a Facebook Page needs its app secret;
channels with their own endpoint above get `410` there. A signature mismatch fails
silently from the provider's point of view, so double-check app secrets when
messages don't arrive.

### Signed email inbound

The email channel accepts inbound mail only from a forwarder (an IMAP poller
or an inbound-parse service) that signs each POST to
`/api/v1/channels/email/callback?config_id=...`. The connection owner reads
the callback URL and signing secret from
`GET /api/v1/integrations/{integration_id}/email-inbound` (the first call
creates the secret) and can replace it with `POST .../email-inbound/rotate`.
Each request carries:

```text
X-Manor-Timestamp: <unix seconds>
X-Manor-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>." + raw body)>
```

Requests without a valid signature, or with a timestamp more than five minutes
from the server clock, are rejected with `403` before anything is logged or
dispatched.

Manor accepts each `message_id` once per channel. Retrying a message that was
already received, or replaying a signed request inside the five-minute window,
returns `200` with `{"ok": true, "noop": true, "reason": "duplicate_event"}`
and does not start a second reply, so a forwarder can retry safely.

## Internal Direct Messages

Separate from external channels, Manor has team direct messages
(**Messages** in the sidebar): human-to-human threads between users of the
same entity, backed by `GET/POST /api/v1/messages` and `/api/v1/messages/threads`.
