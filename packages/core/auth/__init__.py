"""Manor permission-v1 authorization.

Public surface (call from routers / services):

    from packages.core.auth import authorize, require, Actor, Resource

    decision = await authorize(db, actor, action, resource)
    if not decision.allow:
        raise HTTPException(403, decision.reason)

See docs/PERMISSIONS_DESIGN_ZH.md for the design.
"""
from packages.core.auth.authz import (
    Actor,
    AgentActor,
    Decision,
    Resource,
    ShareTokenActor,
    UserActor,
    WorkerActor,
    authorize,
    make_actor,
    require,
)

__all__ = [
    "Actor",
    "AgentActor",
    "Decision",
    "Resource",
    "ShareTokenActor",
    "UserActor",
    "WorkerActor",
    "authorize",
    "make_actor",
    "require",
]
