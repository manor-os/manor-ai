---
name: manor-coding-guardrails
description: Use when writing, reviewing, or refactoring Manor code to avoid overcomplication, make surgical changes, surface assumptions, and define verifiable success criteria.
license: MIT
---

# Manor Coding Guardrails

Adapted from [Karpathy Guidelines](https://github.com/multica-ai/andrej-karpathy-skills/blob/main/skills/karpathy-guidelines/SKILL.md) (MIT). Preserve these core directives when working in Manor.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

Before choosing tests or verification scope, read the repository
[testing policy](../../testing.md). It governs when to add, reuse, or retire tests.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- Do not select a "safe", "simple", or reversible default when interpretations
  change persisted state, user-visible behavior, lifecycle, or external effects.
  Stop and request a decision.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports, variables, and functions that your changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" -> "Add or extend coverage for meaningful invalid-input boundaries."
- "Fix the bug" -> "Reproduce it with an existing or focused new test, then verify the fix."
- "Refactor X" -> "Run existing relevant tests; add tests only for identified coverage gaps."

Low-risk copy, styling, documentation, and mechanical changes do not require
new automated tests by default. Use the proportional checks in the testing policy.
Before delivery, apply its keep/consolidate/remove review to tests created during
implementation. Development probes and intermediate assertions are not
automatically permanent suite additions.

For multi-step tasks, state a brief plan:
```
1. [Step] -> verify: [check]
2. [Step] -> verify: [check]
3. [Step] -> verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## Manor Boundary Rule

These rules do not permit a Manor shortcut around a shared contract. When a
change crosses Workspace, Task, Plan, Workflow, approval, credits, Knowledge,
artifact, lifecycle, or Blueprint boundaries, **REQUIRED SUB-SKILL:** use
`$manor-workspace-architecture` before editing. Keep all entry surfaces on the
same authorization, approval, credit, lifecycle, and durable-artifact path.

Use another specialized Manor skill only when its trigger directly matches the
request. It adds domain-specific context; it never replaces the four core
guardrails above.
