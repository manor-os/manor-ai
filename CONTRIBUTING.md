# Contributing

Thanks for improving Manor AI.

Manor AI is a source-available, self-hosted AI workspace runtime. Contributions
should help operators run the repository on their own infrastructure with clear
configuration, predictable upgrades, and user-controlled credentials.

## Development Setup

```bash
cp .env.example .env
uv sync --locked --extra dev --python 3.12
cd apps/web && npm ci && cd ../..
./scripts/dev.sh infra
./scripts/dev.sh init
```

Start the app:

```bash
./scripts/dev.sh api
./scripts/dev.sh web
```

## Checks

Follow the [testing policy](.agents/testing.md). Start with the behavior being
changed and its existing coverage. Run the relevant files while iterating:

```bash
uv run --locked --extra dev --python 3.12 python scripts/run_python_tests.py smoke tests/test_ci_first_phase.py
```

Choose the appropriate lane and test paths for your change; this example checks
the CI contract only. Use `make test-smoke` for the complete smoke gate and
`make test-regression` for general regression. Run the applicable build or lint
checks for frontend changes. These broader checks are not required after every
edit.

Docs changes should also build locally:

```bash
cd docs-site
npm ci
npm run build
```

## Pull Requests

- Keep changes scoped to one feature, fix, or release-boundary update.
- Reuse existing coverage and fill meaningful behavior gaps. Before delivery,
  review new tests once: keep distinct regressions, consolidate duplicates,
  and remove disposable probes. Copy, styling, and mechanical changes normally
  need the relevant build, diff, or UI check without a new permanent test.
- Update README, docs, or `.env.example` when setup, configuration, routes, or
  deployment behavior changes.
- Link the issue or discussion that motivated the change when there is one.
- Do not include generated binaries, production logs, real customer data, or
  local `.env` files.

## Good First Areas

- Documentation that makes self-hosted setup clearer.
- Tests around agent tools, HITL approvals, integrations, and upgrade paths.
- Small UI improvements that preserve the existing workspace patterns.
- Connectors that work with user-provided credentials and degrade clearly when
  optional services are absent.

## Contribution License

Contributions intentionally submitted to Manor AI are subject to the
[Contributor License Agreement](CONTRIBUTOR_LICENSE_AGREEMENT.md) and are made
available to users under the [Sustainable Use License 1.0](LICENSE). By opening
a pull request, you agree to that contributor license agreement and confirm
that you have the right to contribute the submitted code, assets, and documents
under those terms. Maintainers may request separate written acceptance before
merging a contribution.

## Issues

Please use the GitHub issue templates when possible. A strong issue includes:

- What you expected to happen.
- What actually happened.
- Reproduction steps or a minimal failing example.
- Environment details such as OS, Python, Node, Docker, browser, and deployment
  mode.
- Logs with secrets redacted.

## Self-Hosted Scope

Contributions should remain usable by operators running Manor AI on their own
infrastructure. Avoid adding dependencies on hosted Manor AI services unless the
feature also has a self-hosted path or degrades clearly when optional
credentials are absent.

Local skills, agents, integrations, workflows, browser automation, documents,
and knowledge features should remain operable in self-hosted mode.

## Secrets

Do not commit `.env` files, production credentials, customer data, logs, or
tokens. Use `.env.example` for placeholders only.

## Community

By participating, you agree to follow the project
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
