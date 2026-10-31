---
name: deliver-workspace
description: Use when handing a finished Manor Workspace to a customer — promoting a simulation Workspace to live, delivering a trial copy to a client contact, or checking a client's delivery and acceptance status.
---

# Deliver a Workspace

Deliver only a Workspace whose Blueprint round trip passed (`blueprint-as-code`)
and whose workflows ran cleanly.

## Going live in your own team

1. `promote_workspace` with `preflight_only: true` and read each unmet
   requirement (missing integrations, channels, setup inputs).
2. Resolve them with the user, then `promote_workspace`. Use `force: true` only
   when the user explicitly accepts the listed gaps.

## Delivering to a customer's team

1. `list_clients` to find the client and the contact who will receive it. If
   they aren't there, ask the user to add them in Manor.
2. Confirm with the user before sending: the Workspace, the contact's email,
   the phase, and any Knowledge documents included. Delivery emails the
   contact an invitation.
   Include every fixed Workflow file dependency in `knowledge_document_ids`.
   For a mutable file, use `configure_knowledge_starter` to declare reviewed
   initial text separately from its current data. Export copies that template,
   never the current runtime rows; the source file remains unchanged. Do not
   infer initial content from an arbitrary runtime file or change its safety
   classification to make it exportable. Verify the installed copy's file
   references and initial content before delivering.
3. `deliver_to_client` with `phase: "trial"` (default). The customer receives a
   paused copy to try; nothing runs in their team until they accept. Use
   `phase: "delivered"` only for a final handover the user asked for.
4. `list_client_deliveries` to follow invitation, acceptance and maintenance
   requests.

## Shipping a fix to a delivered Workspace

Each delivery is a copy. To ship a fix, change the source Workspace, re-run the
release loop, then `publish_delivery_update` for each delivery that should get
it (find them with `list_client_deliveries`). Pass `release_notes` saying what
changed. The customer's Workspace then shows an update, which someone there
(or you, in the customer trial) reviews and applies through the Blueprint
upgrade dialog; anything the customer edited stays theirs unless they choose
the new version. A pending invitation installs the new release when accepted.
Don't edit the customer's copy unless you have been granted maintainer access
to it.

## Upgrading an installed Workspace

When you can manage the installed Workspace (your own, or the customer's in a
trial with maintainer access), use the same review the upgrade dialog shows:

1. `preview_workspace_upgrade`: latest by default, or `target_version` for any
   listed release, including an older one. Read `version_change` (the semver
   step and why), `changelog` and `active_runs`; an open or paused run blocks
   applying.
2. Show the user each item whose action is `update`, `keep_yours` or `missing`
   and get their choice. `keep_yours` means both sides changed it; never pick
   `use_blueprint` for those without asking.
3. `apply_workspace_upgrade` with the preview's two fingerprints and one
   `configuration_resolutions` entry per item from step 2. If it reports the
   plan changed, preview again.
4. `revert_workspace_upgrade` undoes the most recent upgrade while nothing has
   changed since.
