---
name: skill-creator
description: Create, update, review, and evaluate reusable Manor Skills. Use when the user wants to turn a workflow into a Skill, build a new Skill, improve an existing Skill, test whether a Skill works, refine its triggering behavior, or organize instructions, scripts, and references for progressive disclosure.
---

# Skill Creator

## Objective

Turn a user's repeatable workflow into a focused Manor Skill that activates for the right requests, follows a reliable process, uses only appropriate tools, and produces a clearly defined result.

Treat Skill creation as product design, not prompt expansion. Preserve the user's intent while removing accidental complexity.

## Choose the operating mode

- **Create**: design and save a new Skill.
- **Update**: improve an existing Skill without changing its identity unless the user explicitly requests a rename.
- **Evaluate**: test a Skill on representative requests and identify actionable improvements.
- **Explain**: help the user understand Skill structure without creating or changing anything.

Do not create a second Skill when the user intends to update an existing one.

## Required workflow

1. **Capture intent from context.** Extract the desired outcome, likely trigger situations, inputs, tools or data sources, output format, constraints, and examples already present in the conversation.
2. **Resolve material ambiguity.** If missing information would substantially change the Skill, call `draft_skill` and relay only its most important questions. Do not ask again for facts the user already supplied.
3. **Read the creation contract.** Before creating or deeply reviewing a Skill, read `references/generation-contract.md`. Use it to prepare a compact creation brief for `create_skill` or an improvement brief for `update_skill`.
4. **Plan progressive disclosure.** Keep the core workflow in the main instructions. Put detailed domain material, schemas, policies, or long examples in references. Request scripts only for deterministic or repeatedly reimplemented work.
5. **Create or update through Manor tools.**
   - For a new Skill, call `create_skill` once with a clear name, a short descriptive category, and a description containing the complete creation brief. Categories are open labels, not a fixed taxonomy.
   - For an existing Skill, call `get_skill_details`, preserve its name and slug, then call `update_skill` with a precise change description.
6. **Inspect the saved result.** Call `get_skill_details` after creation or update. Check discovery description, workflow clarity, tool consistency, input assumptions, output contract, progressive disclosure, and safety boundaries.
7. **Evaluate when useful.** For deterministic workflows or when the user asks for testing, read `references/evaluation-playbook.md`. Invoke the created Skill with safe execution cases. Evaluate discovery near-misses through a selection harness when one is available; otherwise inspect the catalog description and explicitly report that automatic selection was not execution-tested. Never perform an external side effect merely to test a Skill without the user's explicit authorization.
8. **Iterate on evidence.** Use `update_skill` only when inspection, execution evidence, or user feedback identifies a concrete improvement. Avoid rewriting a Skill merely to make it longer.
9. **Report the result.** State what was created or changed, its slug or ID, its expected triggers, resources, tools, validation performed, and any remaining assumptions.

## Creation brief

Before calling `create_skill`, express the design in the description using this structure:

```text
Outcome:
Trigger situations:
Near-misses that should not trigger:
Inputs and defaults:
Required tools or data sources:
Workflow and failure handling:
Output contract:
Reusable scripts or references:
Safety and approval constraints:
Representative examples:
```

Omit sections that genuinely do not apply. Do not pad the brief with generic advice.

## Design principles

- Keep the Skill as short as the task allows and as specific as reliability requires.
- Put all discovery and trigger guidance in the description because the body is loaded only after selection.
- Describe triggers semantically and include meaningful near-misses; do not build literal keyword classifiers.
- Explain why important constraints exist so the executing model can generalize beyond examples.
- Match instruction strictness to task risk: flexible guidance for judgment-heavy work, explicit ordered steps and scripts for fragile operations.
- Prefer one-level references directly linked from the main instructions.
- Avoid duplicating the same guidance between the main instructions and references.
- Use only tools that exist in Manor and are necessary for the workflow.
- Never add hidden behavior, unrelated data collection, unauthorized access, or surprising external actions.

## Tool use

- `draft_skill`: obtain up to three material clarifying questions before creation.
- `create_skill`: generate and persist one new Skill from the completed creation brief.
- `get_skill_details`: inspect a visible Skill before updating it and verify the saved result.
- `update_skill`: apply an evidence-backed change to an entity-owned Skill.
- `invoke_skill`: run a created Skill on a representative safe example. Never invoke `skill-creator` recursively.
- `list_skills`: locate an existing Skill when the user provides a name rather than an ID.
- `read_file` and `list_files`: read this Skill's packaged references on demand.

## Evaluation boundary

Evaluation is proportional to risk and verifiability:

- Use a common request and an edge case for deterministic or multi-step execution testing.
- Add a third, separate near-miss case when discovery behavior matters.
- Do not claim that direct `invoke_skill` execution proves automatic Skill selection.
- Compare observable outcomes, not verbosity.
- Ask for human feedback when quality is subjective.
- Stop iterating when the Skill satisfies the agreed success criteria or further changes do not produce meaningful improvement.

## Completion format

Return a compact handoff:

```markdown
Skill: <display name> (`<slug-or-id>`)
Mode: created | updated | evaluated | explained
Designed for: <trigger summary>
Resources: <scripts/references or none>
Validation: <inspection and test summary>
Notes: <remaining assumptions or none>
```
