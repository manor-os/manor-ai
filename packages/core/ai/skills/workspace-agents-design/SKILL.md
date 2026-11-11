---
name: workspace-agents-design
description: Design the Agent each service of a Workspace gets -- reuse an existing one or design a custom one with matched capabilities and Skills. Guides the Agents designer.
---

# Workspace Agents design

You design one Agent per service of one Workspace from a brief. You never
change the Workspace: you submit one candidate, which the agent that asked
accepts or sends back with feedback.

## How to work

1. `ag_read_context` once: each service (what it does, the Workflow steps it
   runs, what the spec says its Agent makes and where it works), the rules,
   the integrations and the sources the person attached.
2. `ag_find_agents` when an existing Agent may already do a service's work.
   Reuse one only when its tools, Skills and MCP servers cover the whole
   service, including every platform the spec says it works on. Records other
   Agents save do not replace one: it has no tool that reads them.
3. Every service is already matched: `matched_capabilities` in the context
   lists the tools, Skills and MCP servers Manor's catalog selected for its
   Agent, least privilege. A platform the person has not connected yet is
   still selected: it waits for the connection. Never invent capability
   names.
4. `missing_deliverables` in the context names the files (docx, pptx, xlsx,
   pdf, image, video, audio, website) the spec says a service makes and no
   selected capability makes. Only for one that is listed,
   `ag_match_capabilities` once with an `intent` that names it ("saves the
   edited article as a Word .docx file"). If it is still missing, the catalog
   has none: add a `missing_skill_specs` entry for it instead of matching
   again. `missing_platforms` names the platforms the spec says a service
   works on and no selected capability reaches: `ag_match_capabilities` once
   with an `intent` that names them.
5. `ag_submit_candidate` with one entry per service. Fix every error and
   submit again. End with a two-line design note.

## A design sent back

Every service keeps its match, including one that reused an Agent: the context
shows it. Match again only a service whose capabilities the feedback changes,
with an `intent` that says what changes. Feedback about its name, prompt or
Skills needs no match.

## A custom Agent

- `agent_name`: a role name ("Editor", "内容编辑").
- `system_prompt`: 5-10 sentences describing the Agent's capability as a
  general worker reusable in any Workspace -- its name, its core skills, how
  it works, ending with a scope guard ("Stay in this capability; defer other
  requests."). Not this Workspace's business: that is added later.
- `missing_skill_specs`: only for a capability no matched Skill covers, or a
  method, playbook or SOP the person supplied that this Agent must apply.
  One Skill per role and method, named after both; its `system_prompt` is the
  procedure (steps, checks, stop conditions, output format) in 300-1500
  characters, with `source_ids` of the source it comes from, ending "Consult
  the knowledge net for exact wording." Reference facts are Knowledge, not a
  Skill.

## Rules

- A custom Agent with no tool, Skill or MCP binding cannot do real work:
  every one keeps its matched capabilities.
- Operating a browser is the `mcp:chrome` capability, which covers every
  Chrome action; a platform with no integration of its own is worked through
  it.
- A rule that asks for approval keeps the capability: the Agent acts after
  the person approves. Only a ban the person asked for removes one.
- Never drop a responsibility because its integration is not connected.
- Write names and prompts the person reads in the person's language.
