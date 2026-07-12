---
title: Sandbox
---

# Sandbox

The sandbox service isolates code execution and file-producing tool runs from
the API and worker containers.

## Responsibilities

- Execute trusted tool workloads in a separate service boundary.
- Provide a constrained filesystem bridge.
- Scan or validate generated artifacts before returning them.
- Keep runtime dependencies away from the API image when possible.

## Services

| Service | Role |
| --- | --- |
| `sandbox` | HTTP service for isolated execution. |
| `sandbox-skill-image` | Image used for skill execution environments. |

## Configuration

| Variable | Purpose |
| --- | --- |
| `SANDBOX_SERVICE_URL` | API/worker endpoint for sandbox calls. |
| `SHELL_SANDBOX_ENABLED` | Enables shell-backed tools when set to true. |

## Operational Guidance

Only expose sandbox endpoints inside the Docker network. Treat shell execution
as a sensitive capability and pair it with agent tool scope and HITL policy.
