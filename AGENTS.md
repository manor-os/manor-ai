# Repository instructions

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
