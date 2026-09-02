---
name: relationship-ledger
description: Read, query, and record people, organizations, contact routes, relationship state, interactions, follow-ups, consent, and evidence in the active Workspace Relationship Ledger. Use whenever a task relies on CRM-style relationship data or must update it after a verified action.
---

# Relationship Ledger

Use the Relationship Ledger as the Workspace's durable relationship database.
The AI and connected channel tools perform the operational work; the Ledger
stores verified identities, contact routes, current state, immutable events,
evidence, and planned next actions.

## Choose the read path

- Use `query_ledger` with `ledger="relationship_ledger"` for targeted filters,
  counts, grouping, pagination, or current-versus-event queries.
- Use `read_relationship_ledger` when the complete current projection, recent
  history, identity conflicts, or exact contact-point state is required.
- Use `visualize_workspace_ledgers` only when a visual summary materially helps
  the user. It is not a substitute for a pre-action lookup.

Before an external action involving a person or organization, reconcile the
stable identity, aliases, normalized contact points, prior interactions,
conflicts, deliverability, consent, and relationship-level contact policy. Do
not treat a missing search result as proof that no prior record exists when the
identity or contact value is uncertain.

## Store data in the right place

Use top-level event fields for durable relationship state:

- `identity_key`: one stable Workspace-local key; never use a mutable display
  name as the identity.
- `identity_aliases`: non-contact external IDs or prior stable keys for the same
  subject.
- `subject_type`, `display_name`, `relationship_type`, `stage`, and `status`:
  the current relationship projection.
- `event`, `occurred_at`, and `idempotency_key`: the immutable fact being
  appended and its deduplication key.

Use `contact_points` for email, phone, website, LinkedIn, TikTok, Instagram,
Facebook, WhatsApp, YouTube, Skool, X, Threads, Bluesky, Telegram, Signal,
WeChat, LINE, Discord, Slack, GitHub, and other contact routes. Do not duplicate
contact values inside `identity_aliases` or `payload`.

For contact-point state, prefer the independent fields:

- `verification_status`: whether the value has been verified.
- `deliverability_status`: whether the route is deliverable, including bounce
  state.
- `consent_status`: whether communication is allowed, unsubscribed, or do not
  contact.

Use `payload` for facts not represented by typed fields. Reuse stable
snake_case names already established by the Workspace or existing records.
For common relationship planning, prefer:

- `channel`, `direction`, `message_subject`, `message_label`
- `intent_level`, `reply_text`, `last_reply_at`
- `next_action`, `next_action_at`, `next_action_owner_ref`
- `follow_up_count`, `meeting`
- `contact_policy`

Do not add a domain schema name to each row. The installed Workspace may add
domain attributes, but it must preserve existing names instead of creating
synonyms for the same fact.

The current projection shallow-merges `payload`. Treat every nested payload
object as a complete replacement: read its current value first, preserve fields
that remain valid, and write the complete updated object.

## Contact policy semantics

Keep contact-route restrictions separate from subject-wide restrictions:

- A bounce updates only that contact point's `deliverability_status`.
- An unsubscribe or route-specific prohibition updates that contact point's
  `consent_status`.
- A confirmed request not to contact the person or organization through any
  route records a complete `payload.contact_policy` object with
  `suppression_scope: "all_channels"`, plus `reason`, `recorded_at`, and a
  source or evidence reference.

When `suppression_scope` is `all_channels`, do not propose or perform outreach
through another contact point. Conflicting identities or policies require
resolution before action; conflict reporting is not permission to guess.

## Event conventions

Prefer stable event names so future AI runs can query the same history:

- `relationship_created`, `contact_point_added`, `contact_point_updated`
- `first_contact_sent`, `follow_up_sent`, `reply_received`
- `meeting_scheduled`, `meeting_rescheduled`, `meeting_completed`
- `bounce_received`, `unsubscribe_received`, `contact_declined`
- `stage_changed`, `status_changed`, `note_recorded`

Use a deterministic `idempotency_key` for the same external fact. Include
`evidence_refs` for messages, source pages, documents, receipts, or other
authoritative records. Preserve the exact external text in `payload` only when
needed; do not rewrite an actual reply into a stronger intent claim.

## Write discipline

Use `record_relationship_ledger` only for a confirmed fact or an explicitly
planned next action. Do not record a message as sent, a meeting as booked, or a
reply as received before the external system confirms it. After a verified
external action, append the corresponding event promptly so the next AI run
sees the current state.

Never invent contact details, consent, audience facts, relationship stage,
intent, commitments, or outcomes. If a required fact cannot be verified, keep
it unknown and record the evidence gap only when that gap is operationally
useful.
