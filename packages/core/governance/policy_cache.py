"""Revisioned, transaction-fenced cache for Workspace governance policies."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import uuid4

from packages.core.cache import Cache, _get_redis

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GovernancePolicySnapshot:
    workspace_id: str
    revision: int
    policy: dict


@dataclass(frozen=True)
class GovernancePolicyMutationFence:
    workspace_id: str
    token: str
    active: bool


class GovernancePolicyCacheUnavailable(RuntimeError):
    """The policy cache could not establish a mutation fence."""

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        super().__init__(
            f"Governance policy cache unavailable for workspace {workspace_id}"
        )


class GovernancePolicyCache(ABC):
    @abstractmethod
    async def get_current(self, workspace_id: str) -> GovernancePolicySnapshot | None:
        """Return the current revisioned snapshot, or miss safely."""

    @abstractmethod
    async def store_if_current(self, snapshot: GovernancePolicySnapshot) -> bool:
        """Store a snapshot unless a mutation fence or newer revision wins."""

    @abstractmethod
    async def begin_mutation(self, workspace_id: str) -> GovernancePolicyMutationFence:
        """Fence readers before a database mutation is flushed."""

    @abstractmethod
    async def commit_mutation(
        self,
        fence: GovernancePolicyMutationFence,
        snapshot: GovernancePolicySnapshot,
    ) -> None:
        """Publish the committed revision and release this mutation's fence."""

    @abstractmethod
    async def rollback_mutation(self, fence: GovernancePolicyMutationFence) -> None:
        """Release this mutation's fence without publishing uncommitted data."""


class RedisGovernancePolicyCache(GovernancePolicyCache):
    """Redis cache whose TTL is cleanup-only, never the consistency boundary.

    A mutation first installs a shared fence and removes the revision head.
    Readers then fall back to PostgreSQL until the owning transaction commits.
    Lua scripts make head/body publication monotonic and prevent an older
    concurrent reader from restoring a stale head after a newer commit.
    """

    _ENTRY_TTL_SECONDS = 60 * 60
    _KEY_PREFIX = f"{Cache.PREFIX}governance:policy:"

    _GET_CURRENT_SCRIPT = """
        if redis.call('exists', KEYS[1]) == 1 then return nil end
        local revision = redis.call('get', KEYS[2])
        if not revision then return nil end
        return redis.call('get', ARGV[1] .. revision)
    """
    _STORE_SCRIPT = """
        if redis.call('exists', KEYS[1]) == 1 then return 0 end
        local current = redis.call('get', KEYS[2])
        if current and tonumber(current) > tonumber(ARGV[1]) then return 0 end
        redis.call('set', ARGV[2] .. ARGV[1], ARGV[3], 'EX', ARGV[4])
        redis.call('set', KEYS[2], ARGV[1], 'EX', ARGV[4])
        return 1
    """
    _BEGIN_SCRIPT = """
        redis.call('set', KEYS[1], ARGV[1])
        redis.call('del', KEYS[2])
        return 1
    """
    _COMMIT_SCRIPT = """
        redis.call('set', ARGV[2] .. ARGV[1], ARGV[3], 'EX', ARGV[4])
        local current = redis.call('get', KEYS[2])
        if (not current) or tonumber(current) <= tonumber(ARGV[1]) then
            redis.call('set', KEYS[2], ARGV[1], 'EX', ARGV[4])
        end
        if redis.call('get', KEYS[1]) == ARGV[5] then
            redis.call('del', KEYS[1])
        end
        return 1
    """
    _ROLLBACK_SCRIPT = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            redis.call('del', KEYS[1])
            return 1
        end
        return 0
    """

    @classmethod
    def _fence_key(cls, workspace_id: str) -> str:
        return f"{cls._KEY_PREFIX}{workspace_id}:mutating"

    @classmethod
    def _head_key(cls, workspace_id: str) -> str:
        return f"{cls._KEY_PREFIX}{workspace_id}:head"

    @classmethod
    def _body_prefix(cls, workspace_id: str) -> str:
        return f"{cls._KEY_PREFIX}{workspace_id}:revision:"

    async def get_current(self, workspace_id: str) -> GovernancePolicySnapshot | None:
        redis = await _get_redis()
        if redis is None:
            return None
        try:
            raw = await redis.eval(
                self._GET_CURRENT_SCRIPT,
                2,
                self._fence_key(workspace_id),
                self._head_key(workspace_id),
                self._body_prefix(workspace_id),
            )
            if not raw:
                return None
            payload = json.loads(raw)
            if (
                not isinstance(payload, dict)
                or payload.get("workspace_id") != workspace_id
                or not isinstance(payload.get("policy"), dict)
            ):
                return None
            return GovernancePolicySnapshot(
                workspace_id=workspace_id,
                revision=int(payload.get("revision") or 0),
                policy=dict(payload["policy"]),
            )
        except Exception as exc:
            logger.debug("Governance policy cache read failed: %s", exc)
            return None

    async def store_if_current(self, snapshot: GovernancePolicySnapshot) -> bool:
        redis = await _get_redis()
        if redis is None:
            return False
        payload = json.dumps(
            {
                "workspace_id": snapshot.workspace_id,
                "revision": snapshot.revision,
                "policy": snapshot.policy,
            },
            separators=(",", ":"),
        )
        try:
            stored = await redis.eval(
                self._STORE_SCRIPT,
                2,
                self._fence_key(snapshot.workspace_id),
                self._head_key(snapshot.workspace_id),
                str(snapshot.revision),
                self._body_prefix(snapshot.workspace_id),
                payload,
                str(self._ENTRY_TTL_SECONDS),
            )
            return bool(stored)
        except Exception as exc:
            logger.debug("Governance policy cache store failed: %s", exc)
            return False

    async def begin_mutation(self, workspace_id: str) -> GovernancePolicyMutationFence:
        token = uuid4().hex
        # A Redis client can outlive the pytest/Celery event loop that created
        # it, or lose its connection while the process remains healthy.  The
        # cache layer normally replaces clients on loop changes, but a failed
        # EVAL response still needs one fresh-client retry before we declare
        # the governance mutation unavailable.  Retrying this idempotent
        # fence with the same token is safe even if the first response was
        # lost after Redis applied the script.
        for attempt in range(2):
            redis = await _get_redis()
            if redis is None:
                return GovernancePolicyMutationFence(workspace_id, token, False)
            try:
                await redis.eval(
                    self._BEGIN_SCRIPT,
                    2,
                    self._fence_key(workspace_id),
                    self._head_key(workspace_id),
                    token,
                )
                return GovernancePolicyMutationFence(workspace_id, token, True)
            except Exception as exc:
                logger.debug("Governance policy cache fence failed: %s", exc)
                if attempt == 0:
                    try:
                        await redis.aclose()
                    except Exception:
                        pass
                    try:
                        from packages.core import cache as cache_module

                        if getattr(cache_module, "_redis", None) is redis:
                            cache_module._redis = None
                            cache_module._redis_loop = None
                    except Exception:
                        pass
        return GovernancePolicyMutationFence(workspace_id, token, False)

    async def commit_mutation(
        self,
        fence: GovernancePolicyMutationFence,
        snapshot: GovernancePolicySnapshot,
    ) -> None:
        redis = await _get_redis()
        if redis is None:
            return
        payload = json.dumps(
            {
                "workspace_id": snapshot.workspace_id,
                "revision": snapshot.revision,
                "policy": snapshot.policy,
            },
            separators=(",", ":"),
        )
        try:
            await redis.eval(
                self._COMMIT_SCRIPT,
                2,
                self._fence_key(fence.workspace_id),
                self._head_key(fence.workspace_id),
                str(snapshot.revision),
                self._body_prefix(snapshot.workspace_id),
                payload,
                str(self._ENTRY_TTL_SECONDS),
                fence.token,
            )
        except Exception as exc:
            logger.debug("Governance policy cache commit failed: %s", exc)

    async def rollback_mutation(self, fence: GovernancePolicyMutationFence) -> None:
        if not fence.active:
            return
        redis = await _get_redis()
        if redis is None:
            return
        try:
            await redis.eval(
                self._ROLLBACK_SCRIPT,
                1,
                self._fence_key(fence.workspace_id),
                fence.token,
            )
        except Exception as exc:
            logger.debug("Governance policy cache rollback failed: %s", exc)


class GovernancePolicyCacheFactory:
    """Composition root kept replaceable for tests and deployments."""

    _default: GovernancePolicyCache | None = None

    @classmethod
    def create_default(cls) -> GovernancePolicyCache:
        if cls._default is None:
            cls._default = RedisGovernancePolicyCache()
        return cls._default
