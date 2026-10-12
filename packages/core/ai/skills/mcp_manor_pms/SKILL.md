---
name: mcp_manor_pms
description: Use a connected PMS management account for available units, leads, property tours, applications, inspections, tenant messages, notices, leases, maintenance, accounting and short-stay operations.
version: 1.1.0
---

# Manor PMS

Use `mcp__manor_pms__*` with the user's connected management account. Load the current catalog's exact schemas before calling; legacy `pms_*` names are not executable tools. These management tools are not available to anonymous visitors or resident Webchat. Resident maintenance uses its separate verified self-service tools and browser confirmation.

## Connect and select the account

Direct the user to [PMS connection settings](/integrations?provider=manor_pms) if no usable account exists. New connections use OAuth with the current Manor user and Entity. Existing management-token connections remain supported. Never request tokens in chat or put them into tasks, prompts, Knowledge or Blueprints. Portal accounts cannot use management tools. Connecting or reconnecting never replaces an action's approval requirement.

Select the intended `integration_account_id` when several accounts are connected. The runtime leases credentials for the current actor; PMS derives its entity scope from the signed token. Do not supply entity IDs, user IDs or authorization overrides as tool arguments. Read tools may use the runtime's permitted account aggregation; writes target a single account.

## Use the catalog

- Properties and people: property, unit, tenant, owner, vendor and property-owner records, plus management-user lookup for supported assignments.
- Leasing and marketing: lead, showing, application, recorded screening result, nurture sequence and review operations.
- Long-term operations: leases, maintenance requests, payments, charges, expenses, vendor bills, bank accounts and transactions, reconciliation, budgets and reports.
- Resident operations: property inspections and walkthroughs, tenant communication messages, and tenant notice / notification records.
- Short stays: reservation, guest, availability calendar, pricing, policy, ancillary, cleaning, damage, consent, tax, channel, resources and resource bookings.
- Cleaning record tools use `cleaning_task_id` for the PMS record, and `cleaning_assign_task` uses `assigned_to_user_id` for the PMS assignee. Neither is the Manor runtime's own Task or actor identity.

For a known PMS account, search its operations with `search_tools` using `mode: "provider"`, `provider_key: "manor_pms"`, and a capability query. If semantic search returns no provider or no useful operation, retry this provider-scoped search before concluding anything about feature support. A search result is a small ranked subset, not the whole catalog. Load this Skill and the exact schema when a module is unfamiliar.

| Business request | PMS operations |
| --- | --- |
| Rental applications / 租赁申请 | `list_applications`, `get_application` |
| Property inspection or unit walkthrough / 巡检、验房 | `list_inspections`, `get_inspection` |
| Tenant messages or communication records / 住户消息、沟通记录 | `list_messages`, `get_message` |
| Tenant notices or notification records / 住户通知 | `list_notices`, `get_notice` |

Channel sync logs describe calendar or distribution synchronization, not tenant notices. Report an empty list only after the relevant list operation succeeds. If discovery fails, say that discovery failed; never claim the module or all records are absent.

## Available units, tours and maintenance

For current vacancies, use `list_units` with `available_only: true`, or `list_available_units` for an existing property. Read the returned unit details before quoting rent or features. Current vacancy does not guarantee availability for future dates. `get_availability_calendar` describes short-stay reservations, not long-term lease availability.

For a property tour, resolve the property, unit when applicable, and existing lead. Create a lead only with the user's supplied details and authorization. Collect the requested date and time, then use `create_showing` after the normal write approval. PMS records a showing schedule but has no tour slot or conflict-checking engine; do not claim a time was validated or a confirmation email was sent. Read back the showing to verify its ID and stored schedule.

For maintenance, resolve the property, unit and tenant from the selected account, collect the issue description, category and urgency, and call `create_maintenance_request` after approval. Read the created request before confirming its status. Updates and assignment must target that exact request; do not invent a deadline, vendor acceptance or completed repair.

Messages and notices are persisted records, not proof of email/SMS delivery or legal service. Recording a screening result does not request a provider report or authorize a tenancy decision. Nurture delivery depends on a configured worker and provider. Keep these limits clear in the result.

Resolve existing resource IDs with read tools before writing. Use only data returned by the selected account; do not invent IDs or infer that two accounts share a property or guest. Follow the user's authorized scope and existing runtime approval policy for writes, including reservation state changes, payments, damage claims and public review replies.

## Interpret results

Report success only when PMS returns a successful result. Preserve useful business errors without exposing credentials. On authentication failure, direct the user to reconnect in Integrations and test the connection. Connection testing verifies authentication and the selected identity; it does not read business records or execute a write.

A generic 403 proves only that the action was denied. Do not infer the account's role, name a missing permission, or invent an administrator fix without evidence. Resolve tool names from the actual catalog: property tours use `create_showing`, not invented `create_tour` or `schedule_tour` operations. A failed search does not prove the PMS lacks a feature.

Writes are not retried automatically. If a timeout leaves the outcome uncertain, query the relevant record before attempting the same write again. Do not report an unverified write as completed.
