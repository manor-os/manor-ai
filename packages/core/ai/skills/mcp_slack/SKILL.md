---
name: mcp_slack
description: Explain the current Slack Integration status without claiming unavailable Slack MCP actions can execute.
version: 1.0.0
---

# Slack Integration Runtime Skill

Slack is present in the Integration catalog, but this build does not expose an executable Slack MCP tool surface.

## Behavior

- If the user asks to connect Slack, direct them to the Integration setup flow when it is available to their account.
- If the user asks to read or write Slack data and no `mcp__slack__*` tool is discoverable, state that the operation is unavailable in the current runtime.
- Never substitute browser automation, another messaging provider, or invented Slack results without an explicit user request and an available authorized capability.
- Never ask the user to paste Slack tokens into chat.

This Skill is catalog guidance only. It must not claim that a message, reaction, channel read, or thread action occurred.
