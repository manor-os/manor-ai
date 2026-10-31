"""
Sandbox Service Configuration.

All settings can be overridden via environment variables with the SANDBOX_ prefix.
"""

import os
from pathlib import Path


class Config:
    # --- Server ---
    HOST: str = os.getenv("SANDBOX_HOST", "0.0.0.0")
    PORT: int = int(os.getenv("SANDBOX_PORT", "8000"))
    DEBUG: bool = os.getenv("SANDBOX_DEBUG", "false").lower() == "true"
    API_TOKEN: str = os.getenv("SANDBOX_API_TOKEN", "")

    # --- Docker sandbox defaults ---
    SANDBOX_IMAGE: str = os.getenv("SANDBOX_IMAGE", "sandbox-skill:latest")
    SANDBOX_NETWORK: str = os.getenv("SANDBOX_NETWORK", "bridge")
    SANDBOX_ALLOWED_NETWORKS: list[str] = [
        network.strip()
        for network in os.getenv("SANDBOX_ALLOWED_NETWORKS", SANDBOX_NETWORK).split(",")
        if network.strip()
    ]
    SANDBOX_DNS_SERVERS: list[str] = [
        server.strip()
        for server in os.getenv("SANDBOX_DNS_SERVERS", "").split(",")
        if server.strip()
    ]
    # One general sandbox serves all of a user's work, up to 4 commands at
    # once (256 MiB and 32 pids each, see DockerSandbox.max_concurrent_executions):
    # 1 GiB / 512 pids. On a 4 GB / 2 vCPU runner that is ~7 users at the
    # sibling memory admission's 2x overcommit (8 GiB - 256 MiB budget), on a
    # 2 GB / 1 vCPU staging runner 3. CPU stays capped at one core per user.
    SANDBOX_MEMORY: str = os.getenv("SANDBOX_MEMORY", "1g")
    SANDBOX_CPUS: float = float(os.getenv("SANDBOX_CPUS", "1.0"))
    SANDBOX_PIDS_LIMIT: int = int(os.getenv("SANDBOX_PIDS_LIMIT", "512"))
    SANDBOX_READ_ONLY_ROOT: bool = os.getenv("SANDBOX_READ_ONLY_ROOT", "true").lower() == "true"
    SANDBOX_CONTAINER_PREFIX: str = os.getenv("SANDBOX_CONTAINER_PREFIX", "skill-sbx-")
    SANDBOX_SERVICE_SCOPE: str = os.getenv("SANDBOX_SERVICE_SCOPE", "1")
    SANDBOX_WORKDIR: str = os.getenv("SANDBOX_WORKDIR", "/skill")
    # Writable tmpfs sizes. tmpfs pages count against the container's memory
    # limit, so together they stay well below it (256+256+64+64+16+32+1 MiB
    # ~ 690 MiB of a 1 GiB sandbox at worst; processes keep the rest):
    #   /skills    -- every loaded Skill, side by side (SANDBOX_WORKDIR_TMPFS_SIZE)
    #   /workspace -- the work areas of the user's conversations and tasks
    #   /tmp, /var/tmp, /home/sandbox -- scratch, caches, office profiles
    SANDBOX_WORKDIR_TMPFS_SIZE: str = os.getenv("SANDBOX_WORKDIR_TMPFS_SIZE", "256m")
    SANDBOX_WORKSPACE_TMPFS_SIZE: str = os.getenv("SANDBOX_WORKSPACE_TMPFS_SIZE", "256m")
    SANDBOX_TMP_TMPFS_SIZE: str = os.getenv("SANDBOX_TMP_TMPFS_SIZE", "64m")
    SANDBOX_HOME_TMPFS_SIZE: str = os.getenv("SANDBOX_HOME_TMPFS_SIZE", "64m")
    # Commands one sandbox runs at once (all of a user's work shares it);
    # each container further bounds it by pids_limit/32 and memory/256MiB.
    MAX_CONCURRENT_EXECUTIONS: int = int(os.getenv("SANDBOX_MAX_CONCURRENT_EXECUTIONS", "4"))
    # Commands that may wait for a slot in one sandbox (fair FIFO); beyond
    # that a request is answered busy at once.
    MAX_QUEUED_EXECUTIONS: int = int(os.getenv("SANDBOX_MAX_QUEUED_EXECUTIONS", "64"))
    # A user's sandbox may never sit idle long enough for the idle reaper, so
    # it is recycled after this age once nothing runs or waits in it (its
    # files are ephemeral; durable results go to the user's files). 0 = never.
    MAX_LIFETIME_SECONDS: int = int(os.getenv("SANDBOX_MAX_LIFETIME_SECONDS", str(24 * 3600)))
    # How often the runner's disk watchdog measures a sandbox with work in
    # it (sandbox.disk_quota: one policy for every disk; an idle sandbox is
    # measured once a minute). 0 disables the disk watchdog.
    SANDBOX_DISK_WATCHDOG_INTERVAL_SECONDS: float = float(
        os.getenv("SANDBOX_DISK_WATCHDOG_INTERVAL_SECONDS", "3")
    )
    # Disk-backed install dir every sandbox gets (docker_backend.SANDBOX_LOCAL_DIR):
    # past this many MB the disk watchdog stops the commands writing into it and
    # recycles the container once it is idle and still over (0 = no cap).
    SANDBOX_LOCAL_DISK_LIMIT_MB: int = int(os.getenv("SANDBOX_LOCAL_DISK_LIMIT_MB", "4096"))

    # --- Timeouts (seconds) ---
    INSTALL_TIMEOUT: int = int(os.getenv("SANDBOX_INSTALL_TIMEOUT", "300"))  # 5 min for npm/pip installs
    EXEC_TIMEOUT: int = int(os.getenv("SANDBOX_EXEC_TIMEOUT", "120"))

    # --- Lifecycle ---
    # On SIGTERM the service keeps serving existing sandboxes (exec, file
    # reads, status polls) and refuses new ones until in-flight work settles,
    # then exits. Must stay below the orchestrator's stop grace period
    # (compose stop_grace_period,
    # ``docker stop -t``); 0 restores the old stop-immediately behaviour.
    DRAIN_TIMEOUT_SECONDS: float = float(os.getenv("SANDBOX_DRAIN_TIMEOUT_SECONDS", "240"))
    # Drain ends once nothing has been in flight for this long, so a client's
    # follow-up call (exec -> read the output -> destroy) is not cut off.
    DRAIN_QUIET_SECONDS: float = float(os.getenv("SANDBOX_DRAIN_QUIET_SECONDS", "5"))
    # /health and /ready serve a cached image probe; it is refreshed off the
    # event loop on this interval so a probe never waits on the Docker CLI.
    RUNTIME_PROBE_INTERVAL_SECONDS: float = float(
        os.getenv("SANDBOX_RUNTIME_PROBE_INTERVAL_SECONDS", "30")
    )
    IDLE_TIMEOUT_SECONDS: int = int(os.getenv("SANDBOX_IDLE_TIMEOUT", "600"))
    MAX_SANDBOXES: int = int(os.getenv("SANDBOX_MAX_SANDBOXES", "5"))
    INSTANCE_MAX_EXECUTING: int = int(os.getenv("SANDBOX_INSTANCE_MAX_EXECUTING", "0"))
    # Background executions not yet finished, runner-wide (running + queued).
    MAX_PENDING_EXECUTIONS: int = int(
        os.getenv("SANDBOX_MAX_PENDING_EXECUTIONS", "256")
    )
    MAX_EXECUTION_HISTORY: int = int(
        os.getenv("SANDBOX_MAX_EXECUTION_HISTORY", "200")
    )
    # Absolute path prefixes that files/tail may read (append-only logs).
    TAIL_ALLOWED_PREFIXES: tuple[str, ...] = tuple(
        p for p in os.getenv("SANDBOX_TAIL_ALLOWED_PREFIXES", "/work/.agent/").split(",") if p
    )
    EXECUTION_HISTORY_TTL_SECONDS: int = int(
        os.getenv("SANDBOX_EXECUTION_HISTORY_TTL_SECONDS", "3600")
    )
    # Side-channel execs (the platform's own short bookkeeping scripts, e.g.
    # bash's working-copy staging) run next to the command slots like file
    # operations do; this bounds each one so the side channel stays short.
    SIDE_CHANNEL_MAX_TIMEOUT_SECONDS: int = int(
        os.getenv("SANDBOX_SIDE_CHANNEL_MAX_TIMEOUT_SECONDS", "120")
    )

    # --- Memory admission (see sandbox/capacity.py) ---
    # A container is admitted only when its memory limit fits:
    #   budget        = host_memory * MEMORY_OVERCOMMIT - MEMORY_RESERVE
    #   max_container = host_memory - MEMORY_RESERVE (a request above it is clamped)
    # host_memory is the Docker daemon's MemTotal unless SANDBOX_HOST_MEMORY
    # is set. MAX_SANDBOXES stays an additional count cap.
    HOST_MEMORY: str = os.getenv("SANDBOX_HOST_MEMORY", "")
    HOST_CPUS: float = float(os.getenv("SANDBOX_HOST_CPUS", "0") or 0)
    MEMORY_OVERCOMMIT: float = float(os.getenv("SANDBOX_MEMORY_OVERCOMMIT", "2.0"))
    MEMORY_RESERVE: str = os.getenv("SANDBOX_MEMORY_RESERVE", "256m")
    # Absolute budget; replaces the formula above when set.
    MEMORY_BUDGET: str = os.getenv("SANDBOX_MEMORY_BUDGET", "")
    # Per-container ceiling; defaults to host_memory - MEMORY_RESERVE.
    MAX_CONTAINER_MEMORY: str = os.getenv("SANDBOX_MAX_CONTAINER_MEMORY", "")

    # --- Bounds on caller config overrides ---
    # Only memory, cpus, pids_limit, workdir_tmpfs_size, network (within
    # SANDBOX_ALLOWED_NETWORKS) and the two timeouts may be overridden; every
    # other ContainerConfig field is runner-owned and rejected with 400.
    OVERRIDE_MAX_MEMORY: str = os.getenv("SANDBOX_OVERRIDE_MAX_MEMORY", "8g")
    OVERRIDE_MAX_CPUS: float = float(os.getenv("SANDBOX_OVERRIDE_MAX_CPUS", "8"))
    OVERRIDE_MAX_PIDS: int = int(os.getenv("SANDBOX_OVERRIDE_MAX_PIDS", "1024"))
    OVERRIDE_MAX_WORKDIR_TMPFS: str = os.getenv("SANDBOX_OVERRIDE_MAX_WORKDIR_TMPFS", "4g")
    OVERRIDE_MAX_EXEC_TIMEOUT: int = int(os.getenv("SANDBOX_OVERRIDE_MAX_EXEC_TIMEOUT", "1800"))
    OVERRIDE_MAX_INSTALL_TIMEOUT: int = int(
        os.getenv("SANDBOX_OVERRIDE_MAX_INSTALL_TIMEOUT", "900")
    )

    # --- Optional Redis-backed cluster concurrency gates ---
    REDIS_URL: str = os.getenv("SANDBOX_REDIS_URL", os.getenv("REDIS_URL", ""))
    GLOBAL_MAX_ACTIVE: int = int(os.getenv("SANDBOX_GLOBAL_MAX_ACTIVE", "0"))
    GLOBAL_MAX_EXECUTING: int = int(os.getenv("SANDBOX_GLOBAL_MAX_EXECUTING", "0"))
    GATE_TTL_SECONDS: int = int(os.getenv("SANDBOX_GATE_TTL_SECONDS", "900"))
    GATE_WAIT_TIMEOUT_SECONDS: float = float(os.getenv("SANDBOX_GATE_WAIT_TIMEOUT_SECONDS", "0"))
    GATE_OPERATION_TIMEOUT_SECONDS: float = float(
        os.getenv("SANDBOX_GATE_OPERATION_TIMEOUT_SECONDS", "5")
    )

    # --- Skill files ---
    MAX_FILE_READ_SIZE: int = int(os.getenv("SANDBOX_MAX_FILE_READ_SIZE", "65536"))
    MAX_SKILL_FILES: int = int(os.getenv("SANDBOX_MAX_SKILL_FILES", "50"))

    # --- Skills base directory (where skill dirs live on the host) ---
    SKILLS_BASE_DIR: str = os.getenv("SANDBOX_SKILLS_BASE_DIR", str(Path.home() / ".skills"))


config = Config()
