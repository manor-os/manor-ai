---
name: manor-release
description: Use when preparing, cutting, promoting, tagging, or validating a Manor public OSS release, beta/stable release, changelog entry, release notes, docs publish, or post-release announcement.
---

# Manor Release

Use this skill to coordinate a public Manor release while keeping release notes, docs, and checks self-hosted focused.

## Release model

- Public releases are published from GitHub tags after CI and docs checks pass.
- `CHANGELOG.md` records notable public release changes.
- `ROADMAP.md` stays forward-looking and non-committal on dates.
- `.github/workflows/release.yml`, `.github/workflows/deploy-docs.yml`, and tags are release triggers; inspect current workflows before assuming behavior.

## Release preparation

1. Determine release type: beta/candidate, stable tag, or promote existing candidate.
2. Inspect changes since the last relevant tag:

```bash
git tag --sort=-creatordate | head
git log --oneline <last-tag>..HEAD
```

3. Update `CHANGELOG.md` with user-facing OSS changes only.
4. Confirm README/docs-site describe current self-hosted setup, model keys, governance, and operations.
5. Confirm public media and docs render in GitHub and the docs site.
6. Confirm docs publishing behavior in `.github/workflows/deploy-docs.yml` if the release should update the public docs site.

## Required validation before tagging

```bash
.venv/bin/python -m pytest tests/ -m "oss_smoke or oss_regression" -q --tb=short -p no:warnings
git diff --check
```

Add targeted checks based on touched areas:

- Python runtime: relevant pytest selection.
- frontend: `npm --prefix apps/web run build`.
- docs-site: `npm --prefix docs-site run build`.

## Tag safety

- Do not create or push tags unless the user explicitly asks.
- If the release workflow fails, use `manor-ci-triage` before retrying.

## Release notes

Keep notes operator-focused:

- self-hosted setup changes,
- agent/task/HITL behavior,
- security or config changes,
- migration/backup implications,
- known limitations.

Avoid implementation details that do not affect public self-hosted operators.
