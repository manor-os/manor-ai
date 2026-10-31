"""Short-lived tokens for the in-sandbox ``manor`` CLI.

One token is minted for one bash command, delivered to that command only
through the runner's per-exec env, and revoked when the command returns. It
carries the exact identity of the tool call that minted it (entity, user,
agent, conversation, Workspace, task, run, sandbox); the server never trusts
anything the CLI sends about who it is.

Only the SHA-256 of a token is stored or used as a key, so the cache never
holds a usable token and nothing here can log one. Revocation deletes the
record; expiry is enforced both by the cache TTL and by ``expires_at``.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any

TOKEN_PREFIX = "mcli_"
_TOKEN_KEY_PREFIX = "sandbox-cli:token:"
_OPS_KEY_PREFIX = "sandbox-cli:ops:"
# A queued operation outlives its token by this much so the bash tool can
# still drain it after revoking the token at the end of the command.
_OPS_GRACE_SECONDS = 300
MAX_QUEUED_OPERATIONS = 100
MIN_TOKEN_TTL_SECONDS = 30
MAX_TOKEN_TTL_SECONDS = 900


class SandboxCliTokenError(RuntimeError):
    """A token could not be minted or an operation could not be queued."""


@dataclass(frozen=True)
class SandboxCliClaims:
    """Identity of the tool call a CLI token was minted for."""

    entity_id: str
    sandbox_id: str
    exec_id: str
    expires_at: float
    user_id: str | None = None
    agent_id: str | None = None
    conversation_id: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    runtime_run_id: str | None = None
    surface: str | None = None

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_record(cls, record: Any) -> "SandboxCliClaims | None":
        if not isinstance(record, dict):
            return None
        try:
            entity_id = str(record.get("entity_id") or "").strip()
            sandbox_id = str(record.get("sandbox_id") or "").strip()
            exec_id = str(record.get("exec_id") or "").strip()
            expires_at = float(record.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            return None
        if not entity_id or not sandbox_id or not exec_id or expires_at <= 0:
            return None

        def optional(name: str) -> str | None:
            return str(record.get(name) or "").strip() or None

        return cls(
            entity_id=entity_id,
            sandbox_id=sandbox_id,
            exec_id=exec_id,
            expires_at=expires_at,
            user_id=optional("user_id"),
            agent_id=optional("agent_id"),
            conversation_id=optional("conversation_id"),
            workspace_id=optional("workspace_id"),
            task_id=optional("task_id"),
            runtime_run_id=optional("runtime_run_id"),
            surface=optional("surface"),
        )

    def tool_context_kwargs(self) -> dict[str, Any]:
        """Trusted runtime kwargs for the file tools this CLI request reuses.

        The identity keys are always present (even when ``None``) so a handler
        treats the call as runtime-injected and never falls back to a
        model-supplied ``user_id``.
        """

        kwargs: dict[str, Any] = {
            "_user_id_from_context": self.user_id,
            "_agent_id_from_context": self.agent_id,
        }
        if self.workspace_id:
            kwargs["workspace_id"] = self.workspace_id
        if self.conversation_id:
            kwargs["conversation_id"] = self.conversation_id
        if self.task_id:
            kwargs["task_id"] = self.task_id
        return kwargs


def token_digest(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _token_key(token: str) -> str:
    return f"{_TOKEN_KEY_PREFIX}{token_digest(token)}"


def _ops_count_key(exec_id: str) -> str:
    return f"{_OPS_KEY_PREFIX}{exec_id}:count"


def _ops_item_key(exec_id: str, index: int) -> str:
    return f"{_OPS_KEY_PREFIX}{exec_id}:{index}"


async def mint_sandbox_cli_token(
    *,
    entity_id: str,
    sandbox_id: str,
    ttl_seconds: int,
    user_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    runtime_run_id: str | None = None,
    surface: str | None = None,
) -> tuple[str, SandboxCliClaims]:
    """Mint a token for one command. Fails closed when it cannot be stored."""

    from packages.core.ai.runtime.profiles import runtime_surface_is_internal
    from packages.core.cache import cache

    if not str(entity_id or "").strip() or not str(sandbox_id or "").strip():
        raise SandboxCliTokenError("A CLI token needs an entity and a sandbox.")
    if not runtime_surface_is_internal(surface):
        raise SandboxCliTokenError("External principals cannot use the sandbox CLI.")
    ttl = max(MIN_TOKEN_TTL_SECONDS, min(int(ttl_seconds), MAX_TOKEN_TTL_SECONDS))
    claims = SandboxCliClaims(
        entity_id=str(entity_id).strip(),
        sandbox_id=str(sandbox_id).strip(),
        exec_id=secrets.token_hex(16),
        expires_at=time.time() + ttl,
        user_id=str(user_id or "").strip() or None,
        agent_id=str(agent_id or "").strip() or None,
        conversation_id=str(conversation_id or "").strip() or None,
        workspace_id=str(workspace_id or "").strip() or None,
        task_id=str(task_id or "").strip() or None,
        runtime_run_id=str(runtime_run_id or "").strip() or None,
        surface=str(surface or "").strip() or None,
    )
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    if not await cache.set(_token_key(token), claims.to_record(), ttl=ttl):
        raise SandboxCliTokenError("The sandbox CLI token store is unavailable.")
    return token, claims


async def resolve_sandbox_cli_token(token: str | None) -> SandboxCliClaims | None:
    """Return the claims of a live token, or None (unknown, revoked, expired)."""

    from packages.core.cache import cache

    raw = str(token or "").strip()
    if not raw.startswith(TOKEN_PREFIX) or len(raw) < len(TOKEN_PREFIX) + 32:
        return None
    claims = SandboxCliClaims.from_record(await cache.get(_token_key(raw)))
    if claims is None or claims.expires_at <= time.time():
        return None
    return claims


async def revoke_sandbox_cli_token(token: str | None) -> None:
    from packages.core.cache import cache

    raw = str(token or "").strip()
    if raw:
        await cache.delete(_token_key(raw))


async def revoke_sandbox_cli_token_digest(digest: str | None) -> None:
    """Revoke a token known only by its digest (``token_digest``).

    A bash command that parked while it waited for a slot keeps only the
    digest on its ticket -- never the token -- and revokes it once collected.
    """

    from packages.core.cache import cache

    raw = str(digest or "").strip().lower()
    if len(raw) == 64 and all(char in "0123456789abcdef" for char in raw):
        await cache.delete(f"{_TOKEN_KEY_PREFIX}{raw}")


async def queue_sandbox_cli_operation(claims: SandboxCliClaims, operation: dict[str, Any]) -> int:
    """Record one file operation for the bash tool to apply after the command."""

    from packages.core.cache import cache

    ttl = max(1, int(claims.expires_at - time.time())) + _OPS_GRACE_SECONDS
    index = await cache.incr(_ops_count_key(claims.exec_id), ttl=ttl)
    if index is None:
        raise SandboxCliTokenError("The sandbox CLI operation queue is unavailable.")
    if index > MAX_QUEUED_OPERATIONS:
        raise SandboxCliTokenError(
            f"At most {MAX_QUEUED_OPERATIONS} file operations can be queued by one command."
        )
    if not await cache.set(_ops_item_key(claims.exec_id, index), dict(operation), ttl=ttl):
        raise SandboxCliTokenError("The sandbox CLI operation queue is unavailable.")
    return index


async def drain_sandbox_cli_operations(exec_id: str) -> list[dict[str, Any]]:
    """Return and delete the operations one command queued, in order."""

    from packages.core.cache import cache

    count_key = _ops_count_key(exec_id)
    try:
        count = int(await cache.get(count_key) or 0)
    except (TypeError, ValueError):
        count = 0
    count = min(count, MAX_QUEUED_OPERATIONS)
    if count <= 0:
        return []
    keys = [_ops_item_key(exec_id, index) for index in range(1, count + 1)]
    values = await cache.get_many(keys)
    for key in [*keys, count_key]:
        await cache.delete(key)
    missing = [key for key, value in zip(keys, values) if not isinstance(value, dict)]
    if missing:
        raise SandboxCliTokenError(
            "Some file operations queued by this command could not be read back."
        )
    return [dict(value) for value in values]
