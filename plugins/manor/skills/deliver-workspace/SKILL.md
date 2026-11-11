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
   requests. Omit `client_id` to see every client's live deliveries of the
   Workspaces you manage, one page at a time: while `has_more` is true, call it
   again with `offset` set to `next_offset`.

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

The update can also come from somewhere other than the source Workspace — a
Blueprint your team merged in git, or a Workspace tested on staging (another
Manor). Export it as a `.manor` package (or `blueprint.json`), send it with
`create_upload`, then `publish_delivery_update` with that `upload_id`:

1. Build it from the delivery's current release (install that release where you
   develop), so Workflows, Skills and Agents keep their identities; a file that
   shares none of them with the delivery is refused.
2. Call with `dry_run: true` first and show the user the version, its step
   (patch / minor / major), the reasons and the changed components.
3. Send it for real with `release_notes`. A package's files are written into the
   customer's team, so it needs an accepted delivery; a payload without files
   can also update a pending invitation.

## Shipping your own Workspace to another environment

A Workspace's configuration ships as one self-contained Blueprint file (a
`.manor` package carries its own files). The same file installs and upgrades
the same way locally, on staging and in production, so never rebuild a
Workspace by hand in another environment and never ask a user to install a new
copy to get a change.

1. Build and test the change locally, then `export_blueprint` it as a `.manor`
   package. Its manifest names the package (`manifest.package.name`); keep that
   name so every environment recognises the file as the same package.
2. First time in an environment: `install_blueprint` from the file. The install
   records the file as release 1.0.0 of that team's package.
3. Every later change: `create_upload` the new file, then
   `publish_workspace_release` on the installed Workspace with `dry_run: true`,
   show the user the version step and changed components, and publish it with
   `release_notes`. Then upgrade it as below.
4. A Workspace that has no package yet (built by hand, or installed before
   packages) takes `adopt: true` once. Check the dry run's `adoption.matched`
   list: a component listed under `unmatched` will be installed as a new one by
   the upgrade, so ask before adopting when an existing Flow should have matched.

A Workspace installed from a Marketplace listing or a client delivery updates
from there; `publish_workspace_release` refuses it.

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

A reviewed upgrade refreshes the installed usage guide too. Undo
restores the previous guide and refuses to overwrite a later guide edit.

When the user just wants the update applied, `apply_workspace_upgrade` with
`recommended: true` does it in one step: it takes every change of yours and
keeps every edit the Workspace made itself. A 409 `upgrade_needs_review` says
what needs a person first (`active_runs`, `setup` or `variables`).

After `publish_delivery_update`, the customer's Workspace chat shows an update
card that applies the release in one click, and its owner gets a notification.
During a trial, the web send dialog can apply it to the trial Workspace at once.
