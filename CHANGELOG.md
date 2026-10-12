# Changelog

Notable public release changes will be recorded here.

This project follows human-readable release notes for now. Versioned releases
are published from GitHub tags after the OSS export and docs checks pass.

## Unreleased

## [0.1.1]

- Enforced Entity-owned BYOK for the OpenAI-compatible LLM gateway, voice,
  and media entry points. Self-hosted model calls do not use platform-managed
  provider keys or Cloud credit billing.
- Enabled stable Flows in OSS production while retaining explicit operator
  overrides. Experimental Task engines remain opt-in.
- Added portable Blueprint JSON preflight/import and simulation promotion;
  tightened upgrade provenance and starter Knowledge ownership checks.
- Preserved scoped permissions, execution budgets, approvals, and Entity
  isolation while keeping hosted catalogs and commercial packages outside
  the public distribution.
- Strengthened public release checks for clean installation, production
  builds, version metadata, and upgrades from published public migrations.
- Updated self-hosted model-key, governance, storage, backup, and upgrade
  documentation in English and Chinese.
- Updated the Compose MinIO image and health check. Back up existing data and
  review storage permissions and volume compatibility before upgrading;
  see the [upgrade guide](docs-site/docs/operations/upgrade-release.md) and
  [backup guide](docs-site/docs/operations/backup-restore.md).
- Added an OpenAI-compatible LLM gateway (`POST /api/v1/llm/chat/completions`,
  `GET /api/v1/llm/credit`) for downstream applications. Callers authenticate
  with a Manor access token or registered OAuth client credentials and an
  authorized Entity; OSS uses that Entity's configured model credentials.
- Adopted the complete Sustainable Use License 1.0 text used by n8n's
  fair-code model, replacing Manor AI's earlier abbreviated custom wording.
- Added a contributor license agreement so future contributions can be
  distributed under both fair-code and commercial licensing arrangements.
- Prepared the public Manor AI repository for source-available distribution.
- Added public docs site publishing support.
- Added release-ready README, roadmap, security, contribution, and community
  guidance.
- Kept the self-hosted Docker Compose path as the primary evaluation flow.
