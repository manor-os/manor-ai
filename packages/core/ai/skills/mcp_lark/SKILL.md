---
name: mcp_lark
description: Operate the user's connected Lark account or legacy Lark/Feishu app. Use for reading chats and messages, sending or replying to text messages, reading and writing document content, and listing, creating or updating Bitable records.
version: 1.1.0
---

# Lark / Feishu Runtime Skill

Use the selected account through `mcp__lark__*`. New connections use international Lark user OAuth. Existing Lark and Feishu app accounts retain their platform-specific API domain.

## Connection and scope

An administrator configures the Lark App ID and App Secret in the deployment's OAuth client settings (Platform Admin integrations in Cloud); the user connects and grants consent in [Integrations](/integrations?provider=lark). OAuth tools act as that Lark user, including sending messages as the user. The account is bound to the configured international Lark app; changing that App ID requires reconnecting. Feishu app credentials cannot authorize international Lark OAuth. Never request or place secrets or access tokens in chat or tool arguments.

Legacy accounts with App ID and App Secret produce a tenant access token and act as the app bot. They access only resources available to that app. Do not silently switch an OAuth account to app identity or another connected account. Select an exact `integration_account_id` when several accounts could match the request.

User messaging requires the appropriate chat/message scopes, including `im:message.send_as_user` for sending, and access to the target chat. A legacy app must have its bot enabled and available in the chat. Full app messaging uses `im:chat:read`, `im:message` and `im:message.group_msg`; the group-message scope is needed to read history in groups associated with the bot. Document reads need `docx:document:readonly` or `docx:document`; creating documents and appending text need `docx:document`. Bitable needs the corresponding API permissions and access to the requested base. OAuth also needs `offline_access` for token refresh and basic user information for account identity. Authentication and permission failures should lead to specific setup guidance; do not keep retrying or switch to an unrelated app.

## Tools and recipes

- Find account-visible chats with `list_chats`, inspect one with `get_chat`, then read its messages with `list_messages`. Chat history includes thread roots; pass a discovered `thread_id` instead of `chat_id` to read the replies.
- Use `send_message` for a new text message and `reply_message` for a reply to an exact message ID. Resolve the intended recipient or chat before sending; user authorization and the shared runtime approval gate control the write.
- Read a Docx document with `get_document` for metadata and `get_document_content` for plain text. Follow `next_offset` with the returned `content_digest` when reading several chunks. If the document changes, restart at offset 0 without the old digest.
- Use `create_document` to create a document; an explicit target folder must be one created by the app when using its tenant identity. Use `append_document_text` to append the user's requested plain text paragraphs to a document; it adds content without replacing existing blocks. These writes require the document write scope and pass through the shared runtime approval gate.
- Inspect a Bitable base with `list_bitable_tables`, then `list_bitable_fields` to learn its field names and types. Read records with `list_bitable_records`; use its pagination and filter contract when selecting the requested records.
- Use `create_bitable_record` or `update_bitable_record` only for the user's requested fields and exact target table or record. Preserve other fields and confirm the returned `record_id`; read back the stored fields with `list_bitable_records` when verification is needed.

Large read results return `result_json` chunks. Continue with `result_pagination.next_offset` and the returned `result_digest`, keeping all query arguments and the provider `page_token` unchanged until every chunk is read. Join the chunks to recover the complete JSON, then advance to the provider's next page. A changed digest requires restarting at offset 0; do not combine chunks from different responses.

## Guardrails

- Discover actual IDs and field definitions; never invent chat IDs, document IDs, app tokens, table IDs or record IDs.
- Read operations may query all callable connected accounts when the user needs that scope. Send, reply, create and update operations require one selected account.
- Do not treat a timed-out write as proof of failure or automatically replay it. Check upstream state or report the uncertain result to avoid duplicate messages or records.
- Keep unrelated conversations and document contents out of the result. Return only the information the user requested.
- These tools provide account-scoped reads and outbound actions. They do not configure an inbound Manor chat channel or webhook.
