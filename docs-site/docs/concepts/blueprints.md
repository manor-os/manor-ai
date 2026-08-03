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
agents, skills, goals, workflows, and knowledge groups; later edits to the
blueprint don't mutate installed workspaces (they surface as an optional
upgrade instead).

## What a Blueprint Contains

| Section | Contents |
| --- | --- |
| Manifest | Slug, title, summary, tags, category, author, changelog |
| Contract | What the installing environment must supply: variables, channels, required tools/MCP servers |
| Embedded | Full skill and agent definitions (with tool bindings and starter memory), knowledge packs |
| Recipe | Operating model, subscriptions, scheduled jobs, workflows, goals, custom fields |
| Policy | Governance rules (never-allow / HITL-required / auto-approve, budget caps) and post-install checks |

Exports are sanitized: IDs, credentials, and runtime state are stripped, and
a recursive scan rejects any payload containing secret-shaped keys.

## Exporting and Installing

- **Export**: on a workspace page, **Export as Blueprint** creates a draft
  blueprint from the workspace's current configuration
  (`POST /api/v1/workspaces/{id}/export-blueprint`).
- **Install**: `POST /api/v1/blueprints/{id}/install` creates a new
  workspace. Two modes: `simulate` (a sandboxed `[SIM]` workspace for a dry
  run — inspect the simulation report before going live) and `live`. Choose
  a governance preset (`safe`, `standard`, `aggressive`) at install time.
- Channels and browser sessions are **never** auto-created — they become
  install to-dos the operator completes deliberately, since they involve
  credentials.
- **Promote**: a simulated workspace can be promoted to live after a
  preflight check confirms its required channels and sessions exist.
- **Share**: an owner can mint a share token; the link installs the
  blueprint for any authenticated user without publishing it.

Five built-in "solo company" blueprints ship with the platform as immutable
starting points.

## Versioning and Upgrades

Two version numbers, deliberately separate:

- **Format version** (`blueprint_version`, e.g. `1.1`) — the payload schema.
  Old formats are migrated automatically on load.
- **Content version** (semver, from `1.0.0`) — bumped on publish only when
  the content fingerprint actually changed. Installed workspaces compare
  against it and show an "update available" badge.

Upgrades are plan-then-apply with a restore point: items you never edited
since install are safely overwritten; anything you customized is kept as
yours. `POST /api/v1/workspaces/{id}/blueprint/revert` rolls back the last
upgrade.

## API Summary

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/blueprints`, `GET /{id}` | List (own + published + built-ins) and inspect |
| `PUT /{id}`, `DELETE /{id}` | Edit drafts, delete |
| `POST /{id}/install`, `POST /install-payload` | Install (simulate or live) |
| `POST/DELETE /{id}/share-token`, `GET /shared/{token}` | Sharing |
| `POST /{id}/favorite` | Favorites |
| `GET /api/v1/blueprints/governance-presets` | Preset definitions |
| `POST /api/v1/workspaces/{id}/export-blueprint` | Export |
| `GET /api/v1/workspaces/{id}/simulation-report` | Dry-run report |
| `GET/POST /api/v1/workspaces/{id}/blueprint/upgrade`, `POST .../blueprint/revert` | Upgrade flow |

A hosted marketplace with review and paid listings exists on Manor's cloud
offering; self-hosted deployments exchange blueprints through exports and
share links.
