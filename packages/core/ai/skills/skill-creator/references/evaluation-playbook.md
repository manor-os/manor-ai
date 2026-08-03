# Manor Skill Evaluation Playbook

Use this playbook when a user asks to test a Skill or when a deterministic, multi-step, or side-effect-sensitive Skill needs evidence before handoff.

## Select representative cases

Choose two realistic execution prompts:

1. a common request the Skill should handle;
2. an edge case involving missing, empty, malformed, or ambiguous input.

When discovery behavior matters, add a third, separate near-miss that should not select the Skill. Treat it as a discovery case rather than ordinary forced execution.

Use concrete prompts with realistic context. Avoid trivial examples that would pass without the Skill.

## Define observable success

For each case, identify a small set of observable expectations:

- required output fields or structure;
- correct use of a declared tool or reference;
- handling of missing information;
- absence of unauthorized external effects;
- compliance with an explicit format or quality gate.

Do not use verbosity, token count, or stylistic similarity as a proxy for correctness.

## Run safely

- Use `invoke_skill` for the common and edge execution prompts.
- Test a discovery near-miss through a runtime selection harness when one is available. Direct `invoke_skill` bypasses selection and cannot prove whether a Skill would activate automatically.
- If no selection harness is available, inspect the catalog description against the near-miss and report that automatic selection was not execution-tested. Use forced invocation only when separately testing how the Skill behaves after explicit selection.
- Prefer read-only or dry-run inputs.
- Do not send messages, publish content, create charges, modify external records, or expose data solely for testing without explicit authorization.
- Record tool errors and incomplete outputs as evidence rather than hiding them.

## Evaluate and improve

Compare the observed result with the expectations. Separate:

- instruction problems;
- missing or unnecessary tools;
- absent reference material;
- unsafe defaults;
- limitations of the connected data or runtime.

Apply the smallest general improvement through `update_skill`. Do not overfit the Skill to exact wording or values from one example.

Rerun only the affected cases plus one unchanged common case to detect regressions. Stop when the agreed success criteria are met or further changes no longer produce meaningful improvement.

## Human judgment

For subjective output—voice, visual quality, persuasion, taste, or creative direction—show the result to the user and ask for focused feedback. Convert that feedback into a general principle before updating the Skill.

## Handoff

Summarize:

- cases run;
- expectations passed or failed;
- changes made;
- external actions intentionally not tested;
- remaining assumptions.
