---
sidebar_position: 7
title: Blueprints
---

# Blueprints

A blueprint is a portable, versioned snapshot of a workspace's configuration
— agents, skills, goals, workflows, scheduled jobs, governance policy, and
knowledge scaffolding — that can be exported from one workspace and installed
as a brand-new workspace elsewhere.

**An install is a copy, not a link.** The new workspace materializes its own
skills, goals, workflows, and knowledge groups. It creates Blueprint agents
unless you choose existing agents for its services; later edits to the
blueprint don't mutate installed workspaces (they surface as an optional
upgrade instead).

## What a Blueprint Contains

| Section | Contents |
| --- | --- |
| Manifest | Slug, title, summary, tags, category, author, changelog, optional usage guide |
| Contract | What the installing environment must supply: variables, channels, required tools/MCP servers |
| Embedded | Full skill and agent definitions (with tool bindings and starter memory), knowledge packs |
| Recipe | Operating model, subscriptions, scheduled jobs, workflows, goals, custom fields |
| Policy | Approval mode, explicit allow/ask/deny rules, budget and risk limits, and post-install checks |

Exports are sanitized: IDs, credentials, and runtime state are stripped, and
a recursive scan rejects any payload containing secret-shaped keys.
Active workspace workflow bindings and workspace-scoped workflow graphs are
included. Knowledge groups export as structure by default; operators can opt
in to selected Markdown or CSV starters, but only public, clean, non-PII
Knowledge text is eligible. Fixed Workflow document references must point to
included starters; installation maps their portable keys to new local
Documents. Missing or ambiguous dependencies are rejected, while runtime
output references remain templates. Browser-session requirements are limited
to providers, labels, or
session references found in the exported workspace configuration.

## Exporting and Installing

- **Export**: on a workspace page, **Export as Blueprint** creates a draft
  blueprint from the workspace's current configuration
  (`POST /api/v1/workspaces/{id}/export-blueprint`).
  Self-hosted exports download the portable payload as JSON. Import that JSON
  from Workspaces, supply declared variables and select your Entity's channel
  configurations, then run setup preflight before installing. Preflight does
  not create a Workspace; installation checks readiness and permissions again.
  The creator can add a usage guide that explains how to start and review work.
  To re-freeze an editable draft from the same workspace, use the same slug
  with **Replace an existing draft** (`replace_existing: true`). Published or
  pending-review payloads cannot be replaced.
- **Install**: `POST /api/v1/blueprints/{id}/install` creates a new
  workspace. Two modes: `simulate` (a sandboxed `[SIM]` workspace for a dry
  run — inspect the simulation report before going live) and `live`. Choose
  an approval preset (`automatic`, `confirm_changes`, `risk_based`, or
  `follow_default`) at install time. The default is `automatic`; explicit
  approval rules, Workflow review steps, permissions, budgets, risk limits,
  and lifecycle checks still apply. `follow_default` uses the current account
  default rather than freezing the author's preference.
  When the author provides a usage guide, it opens when the installer enters
  the new Workspace and stays available from its header. Workspaces without
  an authored guide do not show the guide entry.
  For each declared service, you can choose any active Agent you can access;
  the Blueprint's Agent is the default. The API accepts
  `agent_ids_by_service_key` (for example,
  `{"research": "<your-agent-id>"}`) on both install endpoints. Scheduled
  automations bind to that service's selected local Agent, not the author's
  Agent ID. Agent choices are local installation settings, not exported IDs.
- **Verify a Workspace copy**: tooling can call
  `POST /api/v1/workspaces/{id}/export-blueprint-payload` to obtain validated
  portable configuration without creating a draft. To install that exact
  export in the current Entity, pass `workspace_source: {workspace_id,
  export_options}` alongside `payload` to `POST /api/v1/blueprints/install-payload`.
  The server rechecks source access and lifecycle, then compares the complete
  payload with a fresh export. Edited or stale content returns HTTP 409 before
  a Workspace is created; export again to retry. Setup, permissions, and
  activation gates still apply. This creates an independent copy, not a
  published-source upgrade link.
- Channels and browser sessions are **never** auto-created — they become
  install to-dos the operator completes deliberately, since they involve
  credentials.
- **Promote**: a simulated workspace can be promoted to live after a
  preflight check confirms its required channels and sessions exist.
- **Share**: an owner can mint a revocable share link for an unpublished
  blueprint. Add tester email addresses when creating the link to limit
  preview and installation to signed-in accounts whose email is verified and
  matches the list. An empty list keeps the existing behavior: any signed-in
  user with the link can install the blueprint. Rotating the link invalidates
  the old URL; revoking it ends link access. Members of the owner's own entity
  retain their existing workspace and blueprint access.

Public practical examples remain importable examples, not default marketplace
listings. Curated commercial product packages are not part of the self-hosted
distribution. Catalog Agent/Skill, tool, browser and knowledge dependencies
must be resolved in the target installation; a JSON file does not copy their
permissions, credentials, execution grants or live consent.
Built-in Skill references resolve to the exact packaged source identity, not
just a matching slug. Customer delivery carries only selected configuration
and opted-in starters; recipient workspaces remain paused with
`provider_access: none` until configured locally.


## Versioning and Upgrades

Two version numbers, deliberately separate:

- **Format version** (`blueprint_version`, e.g. `1.1`) — the payload schema.
  Old formats are migrated automatically on load.
- **Content version** (semver, from `1.0.0`) — bumped on publish only when
  the content fingerprint actually changed. Installed workspaces compare
  against it and show an "update available" badge.

Upgrades are plan-then-apply with a restore point: items you never edited
since install are safely overwritten; anything you customized is kept as
yours. An Agent chosen at install is recorded as a local selection; if the
Blueprint later changes that service, review the choice before replacing it.
`POST /api/v1/workspaces/{id}/blueprint/revert` rolls back the last upgrade.

Source upgrades require a resolvable, published Blueprint. A raw JSON install
without such a source cannot upgrade by importing an arbitrary newer JSON;
draft/archived sources are not upgrade targets. Matching slugs alone do not
bind an install to another source. Historical installs without a recorded source
ID remain unbound; names do not establish provenance. Configuration restore points are not a
replacement for deployment backups.

## API Summary

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/blueprints`, `GET /{id}` | List (own + published + built-ins) and inspect |
| `PUT /{id}`, `DELETE /{id}` | Edit drafts, delete |
| `POST /{id}/install`, `POST /install-payload` | Install (simulate or live) |
| `POST /install-payload-preflight` | Check raw JSON variables and target dependencies without creating a Workspace |
| `POST/DELETE /{id}/share-token`, `GET /shared/{token}` | Sharing |
| `POST /{id}/favorite` | Favorites |
| `GET /api/v1/blueprints/governance-presets` | Preset definitions |
| `POST /api/v1/workspaces/{id}/export-blueprint` | Export |
| `POST /api/v1/workspaces/{id}/export-blueprint-payload` | Export validated portable configuration without creating a draft |
| `GET /api/v1/workspaces/{id}/simulation-report` | Dry-run report |
| `GET/POST /api/v1/workspaces/{id}/blueprint/upgrade`, `POST .../blueprint/revert` | Upgrade flow |

A hosted marketplace with review and paid listings exists on Manor's cloud
offering; self-hosted deployments exchange blueprints through exports and
share links.
