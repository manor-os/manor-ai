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

For changes that affect the public release boundary, also run:

```bash
make oss-check
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

## Public Release Boundary

The private source tree may contain SaaS/operator code that is stripped from
the public tree. If you add cloud-only files, update `.ossexclude` in the same
change. If OSS runtime code imports a helper, keep that helper public and gate
only the private behavior behind `DEPLOYMENT_MODE=cloud`.

Hosted Skill Marketplace, Agent Marketplace, and Apps Marketplace catalogs,
imports, subscriptions, ratings, reviews, and rankings are Cloud-only.
Custom/local skills and agents remain public self-hosted core features.

Use [`docs/OSS_CLOUD_FEATURE_BOUNDARY.md`](docs/OSS_CLOUD_FEATURE_BOUNDARY.md)
to classify new work as OSS, Cloud-only, or shared-core-cloud-gated.

## Secrets

Do not commit `.env` files, production credentials, customer data, logs, or
tokens. Use `.env.example` for placeholders only.

## Community

By participating, you agree to follow the project
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
