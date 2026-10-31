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
| `SANDBOX_EXEC_QUEUE_WAIT_SECONDS` | `600` | How long a command may wait for a free slot of the user's sandbox before it is answered "not started". A durable chat run parks meanwhile (`waiting_resource` / `sandbox_exec_slot`) and resumes when its command starts. |

The sandbox service itself is tuned with `SANDBOX_*` variables on the
`sandbox` container:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SANDBOX_IMAGE` | `sandbox-skill:latest` | Image for child containers. |
| `SANDBOX_NETWORK` | `bridge` | Docker network child containers join. |
| `SANDBOX_DNS_SERVERS` | unset | Optional DNS override for child containers. |
| `SANDBOX_MEMORY` | `1g` | Memory limit of a sandbox (one general sandbox per user, shared by all of the user's work). |
| `SANDBOX_CPUS` | `1.0` | CPU limit of a sandbox. |
| `SANDBOX_PIDS_LIMIT` | `512` | Max processes (tasks) of a sandbox. |
| `SANDBOX_MAX_SANDBOXES` | `5` | Hard cap on live child containers. |
| `SANDBOX_MEMORY_OVERCOMMIT` | `2.0` | Memory admission: a container is created only while the sum of container memory limits stays within host memory × this factor − `SANDBOX_MEMORY_RESERVE`. |
| `SANDBOX_MEMORY_RESERVE` | `256m` | Memory kept for Docker, the sandbox service and the OS. |
| `SANDBOX_MAX_CONTAINER_MEMORY` | host memory − reserve | Per-container ceiling; a larger request is clamped to it (the create response carries a warning). |
| `SANDBOX_OVERRIDE_MAX_MEMORY` / `_CPUS` / `_PIDS` / `_WORKDIR_TMPFS` | `8g` / `8` / `1024` / `4g` | Upper bounds on what a caller may request. Only memory, CPUs, PIDs, workdir size, network (within `SANDBOX_ALLOWED_NETWORKS`) and timeouts can be overridden; image, mounts and security settings cannot. |
| `SANDBOX_OVERRIDE_MAX_EXEC_TIMEOUT` / `_INSTALL_TIMEOUT` | `1800` / `900` | Longest command and install timeout a caller may request. External-agent pool runners set the exec cap to `3600`. |
| `SANDBOX_MAX_CONCURRENT_EXECUTIONS` | `4` | Commands one sandbox runs at once; further bounded by memory/256 MiB and pids/32. |
| `SANDBOX_MAX_QUEUED_EXECUTIONS` | `64` | Commands that may wait for a slot in one sandbox (fair FIFO across conversations and tasks). |
| `SANDBOX_MAX_LIFETIME_SECONDS` | `86400` | A sandbox older than this is recycled once nothing runs or waits in it (`0` never). Its files are temporary. |
| `SANDBOX_DISK_WATCHDOG_INTERVAL_SECONDS` | `3` | How often the runner measures the disks of a sandbox with work in it, per work area (`0` disables the disk watchdog). |
| `SANDBOX_WORKDIR_TMPFS_SIZE` / `SANDBOX_WORKSPACE_TMPFS_SIZE` | `256m` / `256m` | Size of `/skills` (loaded Skills, side by side) and `/workspace` (one area per conversation or task). |
| `SANDBOX_TMP_TMPFS_SIZE` / `SANDBOX_HOME_TMPFS_SIZE` | `64m` / `64m` | Size of `/tmp`, `/var/tmp` and the sandbox user's home. All tmpfs count against `SANDBOX_MEMORY`. |
| `SANDBOX_READ_ONLY_ROOT` | `true` | Mount the child container root read-only. |
| `SANDBOX_WORKDIR` | `/skill` | Writable working directory inside the child. |
| `SANDBOX_INSTALL_TIMEOUT` | `300` | Seconds allowed for npm/pip installs. |


## Operational Guidance

The public native browser and Code Projects execution paths use self-hosted
sandbox capabilities. The private Chrome extension/CLI runtime is not required
or included. Missing sandbox configuration should be resolved for the dependent
tool, not by disabling unrelated Workspace features. Commercial quota bypasses
do not bypass sandbox resource limits, approval or tool scopes.

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
- One command cannot burn out a shared sandbox: a command over the memory
  limit is killed alone and reported as out of memory, a runaway process tree
  is stopped as a whole when the sandbox nears its pids limit, and when
  `/workspace` is nearly full (92%) the commands of the work area holding the
  most of it are stopped (`disk_killed`) and that area's new commands are
  refused as not started until it frees space (cleanup commands such as `rm`
  still run); every other command keeps running, and a full disk never
  recycles the shared sandbox. Commands beyond the slot limit wait their turn
  instead of failing.
