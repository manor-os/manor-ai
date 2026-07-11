"""
Sandbox SDK — Python client for the Sandbox Service.

Quick start:
    from packages.core.services.sandbox_sdk import SandboxClient

    async with SandboxClient("http://sandbox-service:8000") as client:
        sandbox = await client.create_from_files("my-skill", files={...})
        result = await client.exec(sandbox.sandbox_id, "python run.py")
        print(result.stdout)
"""

from .client import SandboxClient
from .exceptions import (
    SandboxConnectionError,
    SandboxError,
    SandboxNotFoundError,
    SandboxRuntimeError,
    SandboxSecurityError,
)
from .models import (
    ContainerConfig,
    CreateSandboxResult,
    ExecResult,
    FileReadBase64Result,
    FileReadResult,
    FileWriteResult,
    LoadSkillResult,
    SandboxInfo,
    SkillContext,
    SkillManifest,
    SkillRunResult,
    SkillRunStepResult,
)

__all__ = [
    # Client
    "SandboxClient",
    # Exceptions
    "SandboxError",
    "SandboxNotFoundError",
    "SandboxSecurityError",
    "SandboxRuntimeError",
    "SandboxConnectionError",
    # Models
    "ContainerConfig",
    "CreateSandboxResult",
    "ExecResult",
    "FileReadResult",
    "FileReadBase64Result",
    "FileWriteResult",
    "LoadSkillResult",
    "SandboxInfo",
    "SkillContext",
    "SkillManifest",
    "SkillRunResult",
    "SkillRunStepResult",
]
