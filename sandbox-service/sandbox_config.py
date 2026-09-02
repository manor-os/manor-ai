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
    SANDBOX_MEMORY: str = os.getenv("SANDBOX_MEMORY", "512m")
    SANDBOX_CPUS: float = float(os.getenv("SANDBOX_CPUS", "1.0"))
    SANDBOX_PIDS_LIMIT: int = int(os.getenv("SANDBOX_PIDS_LIMIT", "256"))
    SANDBOX_READ_ONLY_ROOT: bool = os.getenv("SANDBOX_READ_ONLY_ROOT", "true").lower() == "true"
    SANDBOX_CONTAINER_PREFIX: str = os.getenv("SANDBOX_CONTAINER_PREFIX", "skill-sbx-")
    SANDBOX_WORKDIR: str = os.getenv("SANDBOX_WORKDIR", "/skill")

    # --- Timeouts (seconds) ---
    INSTALL_TIMEOUT: int = int(os.getenv("SANDBOX_INSTALL_TIMEOUT", "300"))  # 5 min for npm/pip installs
    EXEC_TIMEOUT: int = int(os.getenv("SANDBOX_EXEC_TIMEOUT", "120"))

    # --- Lifecycle ---
    IDLE_TIMEOUT_SECONDS: int = int(os.getenv("SANDBOX_IDLE_TIMEOUT", "600"))
    MAX_SANDBOXES: int = int(os.getenv("SANDBOX_MAX_SANDBOXES", "5"))
    INSTANCE_MAX_EXECUTING: int = int(os.getenv("SANDBOX_INSTANCE_MAX_EXECUTING", "0"))
    MAX_PENDING_EXECUTIONS: int = int(
        os.getenv("SANDBOX_MAX_PENDING_EXECUTIONS", "20")
    )
    MAX_EXECUTION_HISTORY: int = int(
        os.getenv("SANDBOX_MAX_EXECUTION_HISTORY", "200")
    )
    EXECUTION_HISTORY_TTL_SECONDS: int = int(
        os.getenv("SANDBOX_EXECUTION_HISTORY_TTL_SECONDS", "3600")
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
