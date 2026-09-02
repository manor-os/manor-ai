---
name: mcp_twilio
description: Inspect connected Twilio numbers, send SMS, place an authorized Manor voice call, and read usage records through Twilio.
version: 1.0.0
---

# Twilio Runtime Skill

Use this skill for the credentials and phone numbers stored in the current Twilio Integration.

## Workflow

1. Use `list_phone_numbers` when the sender number or capabilities are unclear.
2. Before `send_sms`, confirm the E.164 recipient, sender when selectable, and exact message body.
3. Before `make_call`, confirm the recipient and that the user requested a live voice session with the bound Manor Agent.
4. Use `get_usage` for a bounded date range and report the returned units and dates.

## Guardrails

- Sending an SMS or placing a call is an external side effect and must follow approval policy.
- Never infer recipient consent or silently broaden a one-recipient request into a campaign.
- Do not expose Twilio credentials or phone-number secrets.
- After an ambiguous send/call response, inspect status before retrying to avoid duplicate contact.
