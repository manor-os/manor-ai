# Contributing

Thanks for improving Manor OS.

## Development Setup

```bash
cp .env.example .env
pip install ".[dev]"
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

Run focused checks before opening a PR:

```bash
make lint
make test
npm --prefix apps/web run build
```

## Pull Requests

- Keep changes scoped to one feature, fix, or release-boundary update.
- Add or update tests when behavior changes.
- Update README or docs when user-facing setup, routes, features, or deployment modes change.
- Link the issue or discussion that motivated the change when there is one.
- Do not include generated binaries, production logs, real customer data, or local `.env` files.

## Contribution License

Unless a separate contributor agreement applies, contributions intentionally
submitted to Manor AI are submitted under the
[`Manor Sustainable Use License 1.0`](LICENSE). Do not submit code, assets, or
documents unless you have the right to contribute them under that license.

## Issues

Please use the GitHub issue templates when possible. A strong issue includes:

- What you expected to happen.
- What actually happened.
- Reproduction steps or a minimal failing example.
- Environment details such as OS, Python, Node, Docker, browser, and deployment mode.
- Logs with secrets redacted.

## Self-Hosted Scope

Manor OS is a source-available self-hosted application. Contributions should be
usable by operators running the repository on their own infrastructure, with
configuration documented in `.env.example` or the README when setup changes.

Avoid adding dependencies on Manor AI hosted services unless the feature also
has a self-hosted path or degrades clearly when optional credentials are absent.
Local skills, agents, integrations, workflows, browser-runner automation,
documents, and knowledge features should remain operable in self-hosted mode.

## Secrets

Do not commit `.env` files, production credentials, customer data, logs, or
tokens. Use `.env.example` for placeholders only.

## Community

By participating, you agree to follow the project
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
