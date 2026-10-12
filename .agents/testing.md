# Testing policy

Apply this policy when changing code, tests, CI, or test guidance. Protect
meaningful behavior with maintainable evidence. Test count, new test files,
and coverage percentages are not independent delivery goals.

This is the single source for Manor's test authoring and verification policy.
Skills and domain checklists reference it; they should not duplicate it.
Generic advice to add tests, including TDD workflows, does not require a new
test for every edit, helper, or implementation step. Apply this repository's
proportional verification and retention rules when using those workflows.
Domain contracts still determine which risks must be covered. Existing CI and
release gates remain required until explicitly changed in a reviewed change.

The delivery target is useful, reliable regression detection at a reasonable
maintenance cost. Neither a fixed unit/integration/E2E ratio nor a net reduction
in test count is a target. Tests that frequently break after behavior-preserving
edits need review; frequent failure alone does not establish flakiness.

## Keep the coding loop lightweight

During implementation, freely run focused checks and create temporary probes
outside automatic discovery. Test authoring does not require separate approval,
a report per iteration, or a full-suite run after every edit. Fixes may start
with a regression test and keep it when it protects a real defect.

Before delivery, review retention once. If permanent tests, parametrization,
shared fixtures, or selection markers changed, check the affected collected
cases and their CI lanes. Reuse the final test invocation's selection and
results; do not install two environments or collect the whole repository twice
for every PR. Report only measured lanes and mark others `not measured`.
Compare added/removed node IDs only when needed for a test refactor or growth
investigation, using a comparable lock/runtime baseline. Production-only edits
with unchanged test selection do not need a growth inventory. Required behavior
and release checks still apply.

This document governs authoring and review. CI does not read it to filter
tests or impose a numeric cap; automatic discovery still applies. Do not
claim an enforced growth limit from policy compliance alone.

## Decide whether a new test is needed

Before adding a test, identify the behavior or failure it protects and search
for existing coverage. Prefer extending a relevant test or fixture. Create a
new file only for a coherent responsibility that does not fit an existing suite.

| Change | Expected evidence |
| --- | --- |
| New behavior or a reproducible bug | Reuse coverage that detects the relevant failure; fill any gap with the smallest sufficient behavior test. For a bug, demonstrate failure before the fix when practical. |
| Refactor with unchanged behavior | Run existing relevant tests; add coverage only for an identified gap. |
| Copy, documentation, styling, formatting, or mechanical edits with no contract change | No new automated test by default. Use the applicable diff, build, lint, link, or rendered UI check. |
| Authorization, tenant isolation, approval, billing, data integrity, concurrency, or recovery | Prove the affected success and failure boundaries, including bypass, duplicate execution, rollback, or resume where relevant. Reuse existing coverage when sufficient. |
| Intentional contract change | Update the affected expectations and retire superseded assertions in the same change. Preserve unrelated guarantees. |

For each added test, be able to explain what plausible defect would make it
fail. A short PR explanation is enough; do not create a separate report or
new documentation file for every test. No new test is a valid outcome when
existing coverage or proportional manual verification is sufficient.

A permanent test needs an identifiable failure mode, a gap in existing
coverage, an assertion on behavior or an intentional static contract, and
repeatable setup appropriate to its lane. Prefer a few representative cases
over a Cartesian product. Different tenants, providers, entry surfaces, and
deployment modes may protect different risks despite similar test code.

## Choose the layer for Manor's risk

| Risk | Primary evidence |
| --- | --- |
| Parsing, normalization, policy decisions, schema validation | Direct calls with representative inputs and boundary cases; no service startup. |
| API/worker routing, ownership, approval, credits, persistence, retry/resume | Focused integration through the affected entry and real local persistence where the invariant depends on transactions, queries, or constraints. Mock the external provider, not the gate being checked. |
| Browser editing, input, focus, rendering, navigation | A component or focused browser test using user-visible controls and results. A callable JS helper test covers pure frontend logic. |
| Packaging, forbidden dependencies, OSS/Cloud or Blueprint export | Structured static checks or an executable package/export check. Source inspection is valid when source structure is the actual boundary. |
| Live LLM/provider quality, real media or deployed workflows | An explicitly selected evaluation/integration lane with prerequisites, bounded cost, and meaningful result criteria. Mocked responses do not prove model quality or live integration. |

Use the cheapest layer that can detect the defect, with an additional layer
only for a distinct risk. Manor's persistence and orchestration contracts need
integration coverage; do not replace them with helper mocks to meet a pyramid
ratio. Browser smoke tests can be appropriate critical gates: smoke describes
selection and feedback speed, not a synonym for unit tests.

## Separate implementation checks from regression coverage

Agents may use focused checks to reproduce a bug, inspect unfamiliar behavior,
or test an implementation hypothesis. Writing such a check during development
does not automatically make it a permanent regression test.

- Before implementation, identify the behavior and missing coverage. Plan the
  smallest useful regression coverage; do not plan a new test file for each
  development step or expand cases merely to demonstrate progress.
- Keep disposable diagnostic scripts and experiments in a task-owned temporary
  directory outside automatic test discovery. Do not add a permanent scratch
  suite, package script, or CI job just to run a one-off probe.
- Before delivery, review every test and test helper created in the task:

  | Disposition | Criterion |
  | --- | --- |
  | Keep | Detects a plausible regression in intended behavior, fills a distinct coverage gap, and is stable enough for its declared test lane. |
  | Consolidate | Covers the same risk as another test; preserve the useful case in the existing suite or parameterized test. |
  | Remove | A disposable probe, assertion about an abandoned implementation, or duplicate with no additional protection. |

- A TDD test that captures the real bug or new contract normally stays, possibly
  consolidated. Do not remove unique behavioral or safety coverage just because
  the feature now works. Repair a valuable but brittle test instead of labeling
  it temporary. Never delete a failing test merely to finish the task.
- Remove the task's disposable probes and orphaned helpers before delivery;
  preserve pre-existing and other contributors' work. Run the relevant retained
  suite after consolidation. Temporary checks alone do not establish that the
  committed tests pass.
- Briefly report which coverage was retained or consolidated and whether the
  task's temporary checks were cleaned up. Group by behavior; no per-test report
  or numeric quota is needed.

## Test behavior and stable contracts

- Assert public inputs, outputs, persisted effects, user interactions, or
  externally observable failures. Avoid locking private function names,
  variable names, JSX layout, class order, or incidental call sequences.
- Do not add source-regex tests for runtime behavior or extract source snippets
  into a VM merely to assert the current implementation. Use a callable module,
  component test, or focused integration test as appropriate.
- Static checks remain appropriate for actual static contracts: forbidden
  dependencies, package contents, configuration, schema, or export boundaries.
  Prefer parsing structured data. Exact text belongs in a test only when that
  text itself is the required contract; explain that requirement.
- For skills/prompts, retain packaging, nonempty bodies, loadable references,
  tool/schema compatibility, trust boundaries, and deliberate routing or
  approval contracts. Do not snapshot whole prompts, prose inventories, or
  section titles by default. Prompt wording checks do not prove enforcement:
  authorization, consent, and charges need runtime behavior tests.
- Keep normal AI regressions deterministic by mocking the provider boundary
  and checking real request construction, response parsing, tool dispatch, and
  persisted outcomes. Do not assert exact prose from a live model. A mock
  returning its own expected value is not evidence of real integration.
- Reuse an existing provider fake or fixture before creating another. A new
  scripted helper should serve a demonstrated repeated need; do not build a
  general testing framework for one change.
- Give each behavior a primary test layer. Additional layers must cover a
  distinct risk, such as persistence or an alternative authorization entry.
  Parameterize meaningful equivalence classes and boundaries; avoid expanding
  every combination without a distinct failure mode.
- Keep readable, focused fixtures. Do not build a generic test framework or
  refactor production code solely to satisfy a brittle assertion.

## Keep tests isolated and deterministic

- Tests must work individually and alongside related tests, without relying on
  collection order, another test's data, or a developer's running application.
- Restore environment variables, caches, monkeypatches, module replacements,
  and background tasks through scoped fixtures and teardown. Keep imports free
  of network calls and mutable global test setup; marker selection still
  collects and imports test modules.
- Give parallel database, Redis, and filesystem tests isolated resources or
  namespaces. Cleanup must target test-owned resources, never a shared live
  database or keyspace. Scope service fixtures to tests that need them; avoid
  making every unit test initialize the application or clear Redis.
  An existing broad cleanup fixture must be narrowed by tracing its actual
  consumers and verifying mixed/order-dependent runs, not by simply removing
  cleanup or excluding tests marked `unit`.
- The standalone `db_session` fixture rolls back pending work, not rows a test
  already committed. For a global recovery/sweep test, establish its baseline
  in the disposable test database before creating the target records, then
  assert their persisted transitions and idempotence. Do not assume the shard
  database contains only the current test's rows or relax exact counts to hide
  a missing baseline.
- When a test uses the root API `client`, shared database preparation cleans
  and seeds once before either `client` or `db_session` exposes resources.
  Dependent fixtures may then commit their own seed data without a later client
  reset erasing it. API and direct database sessions remain independent; a
  locally overridden `client` does not opt into this cleanup automatically.
- Control time and randomness when they affect the result. Await an observable
  condition with a bounded timeout instead of adding arbitrary sleeps. Keep
  machine-specific timing thresholds in dedicated performance checks.
  Keep collected node IDs stable: use fixed parameter data or explicit IDs
  instead of embedding the current time, random values, or temporary paths.
  Shard weights and cross-run evidence must identify the same behavior.
- Mock external providers at their boundary for deterministic tests. Mark live
  network, Docker, manual, slow, and media dependencies truthfully using the
  existing markers in [pyproject.toml](../pyproject.toml).

The shared `client` and `db_session` fixtures use real bcrypt with a low default
work factor to keep ordinary business tests fast. Their scoped patch affects
all default bcrypt salts, including worker/OAuth secrets, and preserves explicit
rounds. Hash-policy or cost-sensitive tests must use `real_password_hashing`;
the registration regression verifies the persisted production work factor and
real correct/incorrect login behavior. Do not replace password verification,
authorization, or persistence with constant success stubs for speed.

## Select verification by impact

Use the checked-out [workflow](../.github/workflows/ci.yml),
[Makefile](../Makefile), and package scripts for actual commands and required
environment. When they differ, reproduce the failing job's exact selection
and environment; a smaller local suite is not proof that CI passes.

Use `make test-setup` to validate the lock and install the Python 3.12 test
environment. `scripts/run_python_tests.py` owns Python lane expressions;
Make and CI call it. Pass explicit test paths for focused work. Optional
`--report-dir` saves selected IDs, JUnit and safe environment identity from
that invocation, including its exit status. Make's deterministic test targets
use the locked Python 3.12 environment on every invocation. Collection alone
is not a pass; distinguish selected, executed, skipped, and tests requiring
an explicit service/provider lane.

For sharded runs, inspect the reported duration-weight coverage and actual
shard times before increasing parallelism. Missing weights are advisory and
must not block new tests. Refresh `.test_durations` from successful executed
reports with `python scripts/update_test_durations.py <report-dir> ...`,
providing every shard of each included lane from the same CI run. The tool
checks runtime/lock/run identity, selection, outcomes, and phase durations;
it preserves unmeasured entries. An unsharded focused report updates only its
measured keys and is not evidence for a complete lane. Review the weight diff;
do not let parallel CI jobs race to commit cache updates. Compare full workflow
wall time and cumulative job time as well as the slowest test job: balancing
alone reduces waiting, not the amount of test work.

Review prerequisite-related skips, including collection skips, in the affected
suites before claiming coverage. Required deterministic checks belong in a lane
that installs their dependencies; mark only the cases that need those tools.
Do not turn them into manual checks just to avoid provisioning. The required
media lane rejects skips in selected cases; public regression applies the same
rule to its selected media cases. These strict runs also reject collection
skips, whose lane membership cannot be established. Legitimate platform-specific
alternatives can remain explicit runtime skips outside the protected selection.

Set `TEST_REDIS_URL` only to a disposable test service when one is required;
an inherited application `REDIS_URL` is not test-resource authorization.

Keep test layer (`unit`, `integration`, `e2e`), prerequisites (`network`,
`docker`, `media_integration`, etc.), and CI membership (`oss_smoke`,
`oss_regression`, `cloud`) distinct. A marker does not itself create a job or
an exclusion. When adding/moving tests, confirm their collected node IDs in
the intended selection and confirm that its environment provides prerequisites.
Do not use broad module markers when they misclassify mixed cases.


Live API/runner requirements need explicit selection and provision; their
marker names alone do not exclude them. Keep feedback gates selective and
retain broader regression at the existing release/promotion boundary.

- Start with the affected behavior and its callers or crossed domain contracts.
  Existing architecture checklists select risks and checks; they are not a
  requirement to create another test file for every change.
- Expand verification for shared models, authorization, runtime services,
  fixtures, imports, dependencies, migrations, or unclear impact. A changed-file
  list alone does not establish the affected test set.
- Do not rerun the full suite after every edit. After relevant checks pass,
  broaden or repeat only for a required gate, a new change, a failure, or a
  concrete unresolved risk. For isolation fixes, verify the previously failing
  combination or order as well as the individual test.
- New tests must be discovered by the appropriate existing test command and
  accurately classified. Use `oss_smoke` only for fast, stable checks of critical
  behavior with provisioned dependencies and a stated coverage gap. Consider
  incremental setup/runtime cost, not merely whether a case passes locally.
- Heavy or live scenarios belong in an explicit integration, scheduled, or
  manual lane with documented prerequisites. Do not add a new always-running
  CI job or move tests out of a required gate without explaining the risk,
  runtime cost, and coverage that remains.

## Handle failures and retire obsolete tests

Classify the first actionable failure before changing code or expectations:

| Failure | Action |
| --- | --- |
| Product regression | Fix the behavior and retain a meaningful regression check. |
| Obsolete assertion | Confirm the intended contract, then update or remove the old expectation. |
| Test defect or order dependence | Repair fixtures, isolation, or synchronization; verify the failing combination. |
| Environment or dependency problem | Repair or declare the prerequisite and reproduce the CI environment. |

Never change an expectation solely to make a failure disappear. A successful
rerun is not a fix, and increasing retries, timeouts, or shard counts requires
evidence of the underlying problem.

For intermittent failures, capture the SHA, environment, selected node IDs,
shard/order, and relevant logs. Reproduce the failing combination, including
fixture setup/teardown. A deterministic outdated assertion is not a flake.
Use bounded condition/assertion polling for eventual state; distinguish that
from rerunning an entire failing test or job until it passes.

Temporary quarantine is reserved for a diagnosed test defect. Record the exact
tests, evidence, owner, repair deadline, and a separate execution path that
continues to report failures. Do not silently skip tests or broadly apply
`continue-on-error`. Authorization, billing, and data-integrity gates must
retain equivalent protection before any quarantine.

When touching an area, consolidate duplicate tests and remove superseded
assertions within the change's scope. Explain what coverage remains. Do not
delete tests to meet a count target or preserve obsolete product behavior just
because an old test expects it.

For bulk cleanup, name the retained coverage for each group and collect/run
the affected suites. Similar source, long files, old tests, or tests that read
files are not sufficient reasons to delete. Remove orphaned helpers and stale
test IDs from runnable manifests and duration records where used (such as
`.test_durations`); preserve historical
validation records. Parameterization reduces repetition, not the number or
cost of executed cases; shadowed definitions were never separate pytest cases.

## Improve suite health with evidence

Before changing global fixtures, shards, timeouts, or lane membership, inspect
collection cost and setup/call/teardown durations (for example with pytest
`--durations=20`). Use recent comparable CI runs to assess time to feedback,
first-run success, failure categories, and recurring offenders. Historical
`.test_durations` values are shard weights, not a fresh performance benchmark.
Treat that file as a cache: stale entries and new tests without recorded
weights must not break a feature PR. Validate its format and finite timings;
refresh weights from comparable successful executions when imbalance is
measured. Do not require it to mirror the current test inventory.

Use the runner's existing phase timings, outcomes, shard identity, and slowest
tests to locate increasing cost before expanding CI. Keep service setup lazy
and test resources isolated; test growth must not introduce a network cleanup
round trip for every unrelated test. Give jobs a bounded timeout and diagnose
the cause before raising it. Report collection-only or prerequisite-only
validation as such, never as successful test execution.


Prioritize repeated setup, state leaks, and brittle assertions before adding
infrastructure or raising limits. Keep changes within existing tooling unless
measurements justify more. No new reporting service, mandatory per-test form,
or blanket time/coverage quota is required by this policy.

Reuse read-only source or export inputs within a test process when consumers
share the same configuration. Keep mutation, cold-cache, and fresh release
validation paths independent. A process-local fixture does not share work
across CI shards, and cached inputs do not replace execution evidence. Measure
the affected selection before and after a cost change; do not infer whole-CI
speedups from a single module or treat total setup time as database cleanup
time.

## Report verification

In the PR or completion note, briefly state:

- the behavior or risk covered, and why tests were added, reused, or unnecessary;
- the selected lane/prerequisites and meaningful extra cost when coverage grows;
- commands actually run and their results;
- relevant checks not run, their reason, and remaining limitations.

Do not claim full regression, real-provider, browser, or CI success from narrower
or mocked checks. Documentation-only policy changes need link and diff checks,
not new tests that assert the wording of this policy.

## Basis

These references inform the policy; Manor's contracts and checked-in gates
determine its application. Do not import numeric ratios or another project's
quarantine automation as requirements.

- [Google: change-detector tests](https://testing.googleblog.com/2015/01/testing-on-toilet-change-detector-tests.html)
- [GitLab: testing levels](https://docs.gitlab.com/development/testing_guide/testing_levels/) and [testing AI features](https://docs.gitlab.com/development/testing_guide/testing_ai_features/)
- [pytest: flaky tests](https://docs.pytest.org/en/stable/explanation/flaky.html)
- [Playwright: best practices](https://playwright.dev/docs/best-practices)
