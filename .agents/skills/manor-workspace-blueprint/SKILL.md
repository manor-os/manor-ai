---
name: manor-workspace-blueprint
description: Use when creating, exporting, hand-authoring, installing, upgrading, reverting, reviewing, or debugging any Manor Workspace Blueprint, especially when Workspace behavior differs from its Blueprint or when workflows, tasks, automations, artifacts, approvals, or runtime requirements are involved.
---

# Manor Workspace Blueprint

**REQUIRED BACKGROUND:** Use `manor-workspace-architecture` first. Map the
requested Blueprint change to Workspace runtime nodes and their cross-node
contracts before reading or changing `export_workspace()` or installation
logic. Blueprint is the WS-14 portability boundary, not the Workspace
architecture itself.

Treat a Blueprint as a portable projection of a real Workspace configuration,
not as a second workflow implementation. The authoritative lifecycle is:

```text
Workspace created/configured in Manor
  -> export_workspace()
  -> validated Blueprint payload
  -> install_blueprint()
  -> installed Workspace
  -> export again for semantic round-trip comparison
```

## Non-negotiable rules

1. Do not hand-edit a Blueprint as the primary way to design Workspace behavior.
   Configure a real Workspace first, export it, then make only deliberate
   portable edits.
2. Never claim install/export parity from `validate_payload()` alone. That
   validator checks shape and safety, not runtime equivalence.
3. Treat Workflow, Proposal-generated Task behavior, and Automation as separate contracts.
   Include all three in the review and in the round-trip evidence.
4. Distinguish portable configuration from runtime state. Do not export or
   promise portability for Task instances, task logs, workflow runs, agent
   executions, conversations, messages, leases, credentials, measurements, or
   generated artifacts unless the payload explicitly defines a safe portable
   representation.
5. Any non-empty payload section that the installer does not materialize is a
   release blocker. Either implement its installer/exporter mapping or mark it
   explicitly excluded and keep it empty.
6. Natural-language fields (`summary`, descriptions, prompts, operating-loop
   copy, acceptance text, and `not_included`) explain the structured contract;
   they must not introduce steps, tools, approvals, or deliverables absent from
   that contract.

## Required workflow

1. Apply `manor-workspace-architecture`, read its architecture and node test
   checklist, then read [runtime-contract.md](references/runtime-contract.md) and inspect the
   current exporter, installer, payload validator, and upgrade code.
2. Define the intended Workspace behavior as a structured inventory:
   operating model, agents/subscriptions, skills/knowledge, workflows, tasks,
   automations, goals/stats, governance, channels/sessions, and deliverables.
3. Create or update the real Workspace configuration. Do not start with a
   free-form Blueprint JSON unless the task is explicitly a migration.
4. Export with `export_workspace()` and run the portable payload validator.
   For HTTP tooling, use `POST /api/v1/workspaces/{id}/export-blueprint-payload`
   with the portable export options. It returns validated configuration without
   creating a Marketplace draft. Reserve `POST .../export-blueprint` for
   intentional Marketplace authoring or tests of that persistence contract.
5. Inspect the exported payload for source-row IDs, secrets, runtime state, and
   unsupported sections. Fail closed on any of them.
6. Install the payload into an isolated Workspace in both simulation and live
   modes when both modes are in scope. Record install todos and materialized
   IDs; an install with blocking todos is not equivalent.
   For same-account verification through `POST /api/v1/blueprints/install-payload`,
   pass `workspace_source: {workspace_id, export_options}` alongside `payload`.
   The server rechecks source access and exports the current configuration under
   lifecycle/authorization locks; the complete payload must still match. A stale
   or edited export returns 409 before creating a Workspace. Cloud does not
   require a raw-import subscription for this verified source; ordinary Workspace
   capacity and setup gates still apply. Arbitrary JSON imports retain their
   existing subscription gate, and Marketplace installs retain their identity
   and purchase checks.
7. Export the installed Workspace again. Normalize only generated identifiers,
   install timestamps, sandbox/provenance fields, workspace-specific job
   suffixes, and generated local Skill aliases backed by the same exact external
   source identity. Embedded and legacy slug-only bindings remain exact. Compare the runtime-projected sections, including Workflow graphs,
   Automation triggers, artifact contracts, and approval scopes. Verify
   one-shot install declarations
   (`contract.variables`, unresolved channel/session requirements,
   `post_install_checks`, and `expected_baseline`) from install preview/todos and
   check results; they are not reconstructable from the installed Workspace.
8. Run the deterministic checker:

   ```bash
   ./.venv/bin/python .agents/skills/manor-workspace-blueprint/scripts/check_blueprint.py path/to/blueprint.json
   ```

   Compare source and installed exports with the semantic round-trip checker:

   ```bash
   ./.venv/bin/python .agents/skills/manor-workspace-blueprint/scripts/compare_roundtrip.py \
     source.json installed.json
   ```

9. Verify each affected parity rule with existing regression coverage; add or
   extend tests only for a coverage gap, following the repository
   [testing policy](../../testing.md). At minimum run the relevant
   `tests/test_blueprint_v11_e2e.py`, exporter/installer tests, and
   `git diff --check`.
10. Only after the runtime projection round-trip and the separate one-shot
    install checks pass may the Blueprint be published, seeded as built-in
    content, or used as an upgrade source.

## Verification resource lifecycle

Use [verify_roundtrip.py](scripts/verify_roundtrip.py) for HTTP export, source-verified
payload install, and re-export; see `--help`. It preserves the supplied source Workspace,
records its newly installed Workspace in a run directory, and attempts cleanup
on both success and failure. Retain the run directory until cleanup succeeds;
retry pending cleanup with the same API origin and account. A lost install
response is an uncertain outcome, not permission to install again or discover
deletion targets by name. Resolve the exact receipt before declaring cleanup
complete. Install todos and one-shot declarations still need their separate
evidence; semantic comparison alone is not full acceptance.

For variable personalization, the verifier uses the repository's canonical
installer resolver, including supplied values, defaults and JSON types. Run it
with the repository Python environment. It saves the resolved comparison input
as `expected-payload.json` while sending the original export to source-verified
installation; real configuration differences still fail comparison.

When explicitly testing persisted Marketplace export, durably record every newly
created Blueprint and Workspace ID as it is returned. In `finally`, delete only
those Blueprint IDs through `DELETE /api/v1/blueprints/{id}`, then delete the
temporary Workspaces through their existing DELETE API. Keep failed IDs for
independent retry. Refreezing a pre-existing draft does not make it test-owned.
Published or purchased assets require their normal lifecycle; never bypass its
guards to force cleanup.

Workspace DELETE is soft deletion; the recovery window and normal purge remain
in force. Blueprint source provenance is not lifecycle ownership: deleting a
Workspace must not cascade to independent authored/published Blueprints, and
same-title or same-slug searches must not select cleanup targets. Verification
journals belong outside portable payloads and Workspace configuration.

## Failure policy

Stop and report the exact object and boundary when any of these occur:

- exported content cannot be installed;
- installed content cannot be exported back;
- a Workflow step or dependency changes;
- an entity-level Task category, SLA, or SLA-linked escalation policy is placed
  in a Workspace Blueprint;
- an Automation target resolves to a different Workflow/Agent;
- an explicitly portable configuration is dropped;
- an artifact/output contract changes;
- approval or governance scope becomes broader or narrower;
- human-readable Blueprint copy disagrees with the executable contract.

Do not repair these by adding another conflicting prompt. Fix the shared
exporter/installer contract or remove the unsupported declaration.
