# Repository instructions

## Workspace architecture boundary

Implement business scenarios through the existing Workspace architecture.
Keep scenario-specific orchestration, prompts, schemas, cadence, and platform
choices in Blueprint, Workflow, and Skill configuration. Reuse the shared
Workflow, WorkflowProject, Scheduler, Knowledge, Stats, approval, billing,
lifecycle, and notification contracts before introducing another capability.

Before adding runtime code, identify the existing public capability and the
specific missing contract. Extend that shared capability only when configuration
cannot express the required behavior. Verify the extension with a second,
unrelated scenario. Do not add business-specific backend branches, database
tables, APIs, UI tabs, or approval bypasses (for example, JR Music-only runtime
services). Renaming a scenario-specific implementation does not make it a shared
capability; its inputs, policy, state, and presentation must be configurable.

## Manor MCP and plugin stay in sync with Workspace tools

The hosted MCP endpoint (`apps/api/mcp/`, `apps/api/routers/mcp.py`) and the
Claude Code / Codex plugin (`plugins/manor/`) expose Workspace building to
external coding agents. Each MCP tool is a thin adapter that calls a Manor REST
route in-process as the signed-in user.

When a change adds, renames, removes, or changes the request/response contract
or behaviour of a Workspace-building capability, update the MCP surface in the
same change. Workspace-building capabilities include:

- REST routes for Workspaces (including operation drafts and the app view),
  Blueprints (export, validate, install, upgrade, promote), Workflows, Skills,
  Knowledge documents, and client delivery.
- The in-product agent tools that build Workspaces (`workspace_agent_tools`,
  `workspace_arch_tools`, `workflow_tools`, `skill_tools`, Workspace actions in
  `manor_tool`).

Update in the same change:

1. The tool adapter in `apps/api/mcp/tools_*.py`: its route, arguments, input
   schema, description, and read-only/destructive annotations.
2. The plugin skills in `plugins/manor/skills/` and the tool table in
   `plugins/manor/README.md` when the workflow or tool set changes.
3. The `version` in both `plugins/manor/plugin.json` and
   `plugins/manor/.claude-plugin/plugin.json` when tools are added, removed, or
   change arguments.

A new Workspace-building capability that in-product agents can use should also
get an MCP tool. If it deliberately stays internal, say why in the PR.
`tests/test_mcp_endpoint.py` checks every MCP tool against the live route table
and request-body models; keep it passing and add a sample call for every new
tool.

## File edits

Use `apply_patch` for manual code edits. Do not create or edit files with `cat`,
heredocs, or other shell write tricks. Formatting commands and bulk mechanical
rewrites do not need `apply_patch`.

## Testing

Before changing code, tests, CI, or test guidance, read
[.agents/testing.md](.agents/testing.md). It is the repository's test policy;
apply it when deciding whether to add tests and what verification to run.

Development probes are not automatically permanent regression tests. Before
delivery, apply the policy's keep/consolidate/remove review to tests created in
the task. Generic TDD workflows must follow this repository's proportional
verification and retention rules.

Keep iteration lightweight: run focused checks while coding, then perform
test retention and applicable CI impact review once before delivery. No
separate approval is needed to write or run ordinary development tests.

A change that renames or removes a function, or changes a signature, field,
or behavior, must update the tests and test doubles that reference it in the
same change. Search for them (`git grep -n <name> tests/`) instead of waiting
for CI to report them.
