---
title: Skills and Tools
---

# Skills and Tools

Tools are executable capabilities. Skills are instruction packages that teach an
agent when and how to use capabilities.

## Tools

Tools can read files, call APIs, search, manage documents, run code, interact
with MCP servers, or perform other bounded actions.

Tool access is governed by:

- Agent configuration.
- Runtime policy.
- Workspace permissions.
- HITL approval rules.

## Skills

A skill usually contains a `SKILL.md` file and optional assets or scripts. The
runtime loads skill instructions only when the skill is relevant, keeping agent
context focused.

Good skills:

- Name the use case clearly.
- Explain required inputs.
- Describe tool usage boundaries.
- Include examples for common workflows.
- Avoid broad permissions when narrower tools are available.

## Browser-Backed Tools

Some integrations do not expose complete APIs. Manor OS keeps browser-backed
automation in the browser-runner sidecar, so the API and worker do not need to
host Chromium directly.

See [Browser Runner](../operations/browser-runner.md).
