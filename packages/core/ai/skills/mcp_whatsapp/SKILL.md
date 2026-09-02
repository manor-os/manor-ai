---
name: mcp_whatsapp
description: Read and manage the connected WhatsApp Business profile, templates, and authorized messages through the WhatsApp MCP.
version: 1.0.0
---

# WhatsApp Business Runtime Skill

Use this skill for the WhatsApp Business Account and phone number stored in the current Integration.

## Workflow

1. Use `get_phone_number` or `list_phone_numbers` to verify the sending identity.
2. Use `send_text` only inside Meta's customer-service window; use an approved `send_template` to initiate a conversation.
3. For media sends, confirm the recipient, public HTTPS asset URL, media type, and caption.
4. Read templates before creating or deleting one, and report Meta's returned review/status information.

## Guardrails

- Sending messages, changing the public business profile, creating templates, and deleting templates are external writes.
- Confirm the exact recipient and content; never turn a single-message request into bulk outreach.
- `delete_message_template` is destructive and requires explicit approval.
- Do not expose access tokens, phone-number IDs, or Business Account secrets.
- After an ambiguous write, inspect the relevant state before retrying.
