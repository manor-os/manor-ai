# Roadmap

This roadmap describes the public self-hosted direction for Manor AI. It is not
a promise of dates; it is a guide for contributors and operators who want to
understand where the project is going.

## Now

- Keep the Docker Compose path reliable for local evaluation and small team
  deployments.
- Improve docs for BYOK model setup, HITL approvals, workspace knowledge, and
  backup/restore.
- Stabilize the OSS release export so the public repository stays clean,
  reproducible, and free of private cloud-only surfaces.
- Tighten tests around agent tool permissions, workflow execution, and
  self-hosted integrations.

## Next

- Publish a stable OpenAPI artifact with each public release.
- Add more guided examples for building agents, approval policies, and
  integration workflows.
- Improve first-run onboarding for self-hosted operators.
- Expand operational docs for upgrades, storage, model providers, and
  production hardening.
- Add more screenshots and short walkthroughs to the docs site.

## Later

- Add optional deployment templates for common infrastructure targets.
- Improve observability dashboards for workers, queues, tool calls, and agent
  runs.
- Broaden connector coverage while keeping user-provided credentials and
  self-hosted operation as the default assumption.
- Continue separating hosted-service conveniences from the public self-hosted
  runtime.

## Contributing To The Roadmap

Open a GitHub Discussion or feature request with:

- The operator or end-user problem.
- The expected self-hosted behavior.
- The configuration or credentials required.
- Any security, data ownership, or HITL implications.
