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

    # --- Docker sandbox defaults ---
    SANDBOX_IMAGE: str = os.getenv("SANDBOX_IMAGE", "sandbox-skill:latest")
    SANDBOX_NETWORK: str = os.getenv("SANDBOX_NETWORK", "bridge")
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
    IDLE_TIMEOUT_SECONDS: int = int(os.getenv("SANDBOX_IDLE_TIMEOUT", "3600"))
    MAX_SANDBOXES: int = int(os.getenv("SANDBOX_MAX_SANDBOXES", "20"))

    # --- Skill files ---
    MAX_FILE_READ_SIZE: int = int(os.getenv("SANDBOX_MAX_FILE_READ_SIZE", "65536"))
    MAX_SKILL_FILES: int = int(os.getenv("SANDBOX_MAX_SKILL_FILES", "50"))

    # --- Skills base directory (where skill dirs live on the host) ---
    SKILLS_BASE_DIR: str = os.getenv("SANDBOX_SKILLS_BASE_DIR", str(Path.home() / ".skills"))


config = Config()
