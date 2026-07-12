---
title: Webhooks
---

# Webhooks

Webhooks let external systems notify Manor OS and let Manor OS call external
systems.

## Inbound Webhooks

Inbound webhooks need:

- A public HTTPS URL.
- Provider-specific secret or signature settings.
- A route or integration configuration in Manor OS.

## Outbound Webhooks

Outbound webhooks should use HMAC or provider-supported signing when available.

## Security

- Rotate webhook secrets periodically.
- Reject unsigned requests when provider signing is available.
- Log enough metadata to debug delivery without logging secret payloads.

## Troubleshooting

If a webhook does not arrive:

1. Confirm `PUBLIC_BASE_URL`.
2. Check provider delivery logs.
3. Check API logs.
4. Confirm the route is enabled and reachable.
