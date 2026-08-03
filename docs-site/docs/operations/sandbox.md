---
title: Sandbox
---

# Sandbox

The sandbox service isolates code execution from the API and worker
containers. When an agent runs a skill script or a shell command, the sandbox
service launches a dedicated child Docker container for the run, applies
resource limits, and returns the output — so untrusted code never executes
inside the application processes.

## Responsibilities

- Execute skill scripts and shell workloads in per-run child containers.
- Enforce memory, CPU, process-count, and timeout limits per run.
- Provide a constrained working directory and (by default) a read-only root
  filesystem.
- Keep runtime dependencies (interpreters, package installs) away from the
  API image.

## Services

| Service | Role |
| --- | --- |
| `sandbox` | HTTP service that manages isolated execution containers. |
| `sandbox-skill-image` | Builds `sandbox-skill:latest`, the base image child containers run. Building it inside Compose means `docker compose up --build -d` needs no separate prebuild step. |

## Configuration

The API and worker reach the sandbox through:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SANDBOX_SERVICE_URL` | `http://sandbox:8000` | Endpoint for sandbox calls. |
| `SHELL_SANDBOX_ENABLED` | `true` | Allows shell-backed agent tools. Set `false` to disable shell execution entirely. |

The sandbox service itself is tuned with `SANDBOX_*` variables on the
`sandbox` container:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SANDBOX_IMAGE` | `sandbox-skill:latest` | Image for child containers. |
| `SANDBOX_NETWORK` | `bridge` | Docker network child containers join. |
| `SANDBOX_DNS_SERVERS` | unset | Optional DNS override for child containers. |
| `SANDBOX_MEMORY` | `512m` | Memory limit per run. |
| `SANDBOX_CPUS` | `1.0` | CPU limit per run. |
| `SANDBOX_PIDS_LIMIT` | `256` | Max processes per run. |
| `SANDBOX_READ_ONLY_ROOT` | `true` | Mount the child container root read-only. |
| `SANDBOX_WORKDIR` | `/skill` | Writable working directory inside the child. |
| `SANDBOX_INSTALL_TIMEOUT` | `300` | Seconds allowed for npm/pip installs. |

## Operational Guidance

- Only expose the sandbox endpoint inside the Docker network; nothing outside
  the stack should reach it.
- Treat shell execution as a sensitive capability: pair it with agent tool
  scope and [HITL governance](../concepts/hitl-governance.md) so shell-using
  agents run under approval policies.
- Child containers are named with the `skill-sbx-` prefix; if a run is killed
  uncleanly you can list and remove leftovers with
  `docker ps -a --filter name=skill-sbx-`.
- Raise `SANDBOX_MEMORY` / `SANDBOX_CPUS` if legitimate skills hit limits;
  the defaults favor protecting the host.
