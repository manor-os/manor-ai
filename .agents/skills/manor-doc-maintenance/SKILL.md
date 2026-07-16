---
name: manor-doc-maintenance
description: Use when Manor README, docs-site, public screenshots/videos, release-ready wording, quickstart, configuration docs, roadmap, changelog, API docs, or public self-hosted documentation need audit or updates after product, OSS, CI, or setup changes.
---

# Manor Doc Maintenance

Use this skill to keep public Manor AI docs accurate, concise, and self-hosted focused.

## Public docs contract

- README is the public landing page. Keep it high-signal: hero/video, why Manor, quickstart, OSS stack, extension, operations, community.
- Detailed screenshots belong in `docs-site/docs/**`, not as a README screenshot wall.
- Do not reintroduce README sections named `Development Setup`, `Checks`, or `API Surface`.
- Use factual claims only. No fake stars, unsupported hosted-service claims, or license mismatch.

## Update workflow

1. Inspect changed product/setup files and decide which docs are user-facing.
2. Update the nearest public doc:
   - setup/env/model keys → `docs-site/docs/configuration.md`, `quickstart.md`, or `docker-compose.md`
   - architecture/runtime → `docs-site/docs/architecture.md` or concept docs
   - agent/task/HITL behavior → `docs-site/docs/concepts/**`
   - public landing narrative/media → `README.md` and `docs-site/static/img|video`
3. Add new public media under `docs-site/static/img` or `docs-site/static/video`.
4. If a public asset is required, verify it is tracked and referenced from the public README or docs-site pages that use it.
5. Keep copy operator-centered: self-hosted, BYOK, local data ownership, scoped tools, approvals, auditability.

## Required checks

Use targeted checks for doc-only changes:

```bash
git diff --check
```

If docs-site rendering or asset paths changed, run:

```bash
npm --prefix docs-site ci
npm --prefix docs-site run build
```

Remove ignored build artifacts such as `docs-site/build` and `docs-site/.docusaurus` after verification unless the user asks to keep them. Do not delete `docs-site/node_modules` just because a docs build ran.

## Final review

Confirm:

- links are public and current,
- images/videos render from GitHub/docs-site context,
- no sensitive screenshot data is visible,
- README remains concise,
- New public docs/media are tracked and covered by the relevant docs or build checks.
