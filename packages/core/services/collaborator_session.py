"""Scoped sessions are the only way a collaborator actor authenticates.

A provider never switches into the customer company. The provider's app
mints a short-lived ``collaborator_session`` token for one customer Workspace
(auth_service.create_collaborator_session_token) and the embedded frame uses
it in memory. Every entry point that resolves a collaborator actor (HTTP,
websocket, raw files) applies these rules:

* a collaborator actor must present a ``collaborator_session`` token, and a
  ``collaborator_session`` token must resolve to a collaborator actor;
* the token reaches only the Workspace it pins (``cs.workspace_id``), and
  only while the live grant it names still covers it: for ``client_trial``
  a live grant of the delivery ``cs.ref``, for ``grant`` the grant ``cs.ref``
  itself (``session_scope``);
* everything else the request (or a run it starts in-process) decides about
  the collaborator is narrowed to that Workspace too: deps pins the scope in
  the request context, and ``collaborator_scope.collaborator_live_workspaces``
  (Knowledge, resource and runtime checks) intersects with it.

A run that outlives the request (a queued Task, a scheduled job) does not
carry the pin. It runs in its own Workspace and the runtime gate admits a
collaborator's part in it only while a live grant covers that Workspace.
"""

from contextvars import ContextVar

from packages.core.services.auth_service import COLLABORATOR_SESSION_TYPE

COLLABORATION_ENDED = {
    "code": "collaboration_ended",
    "message": "Your access to this customer Workspace has ended",
}

# (entity_id, user_id, Workspaces) of the scoped session behind this request.
_PIN: ContextVar[tuple[str, str, frozenset[str]] | None] = ContextVar("collaborator_session_pin", default=None)


def is_collaborator_session(claims: dict | None) -> bool:
    return bool(claims) and claims.get("typ") == COLLABORATOR_SESSION_TYPE


def session_claims(claims: dict | None) -> tuple[str | None, str | None, str | None]:
    """(workspace_id, kind, ref) a scoped session names."""
    scope = (claims or {}).get("cs")
    if not isinstance(scope, dict):
        return None, None, None
    return tuple(str(scope[key]) if scope.get(key) else None for key in ("workspace_id", "kind", "ref"))


def session_scope(claims: dict | None, grants) -> frozenset[str]:
    """The Workspace this scoped session may reach now, given the person's live grants.

    Its pinned Workspace, while the grant it names is live there; otherwise
    nothing, which ends the session.
    """
    workspace_id, kind, ref = session_claims(claims)
    if not workspace_id or not ref:
        return frozenset()
    for grant in grants:
        if grant.workspace_id != workspace_id:
            continue
        if (kind == "client_trial" and grant.delivery_id == ref) or (kind == "grant" and grant.id == ref):
            return frozenset({workspace_id})
    return frozenset()


def collaborator_session_matches(claims: dict | None, *, collaborator: bool) -> bool:
    """Whether the token type fits the actor it resolved to."""
    return is_collaborator_session(claims) == bool(collaborator)


def require_collaborator_session(claims: dict | None, *, collaborator: bool) -> None:
    """HTTP form: a mismatch is an ended collaboration (401), never a fallback."""
    if not collaborator_session_matches(claims, collaborator=collaborator):
        from fastapi import HTTPException, status

        raise HTTPException(status.HTTP_401_UNAUTHORIZED, COLLABORATION_ENDED)


def pin_session(entity_id: str | None, user_id: str | None, workspace_ids) -> None:
    """Pin this request's collaborator scope; None clears it (any other actor)."""
    _PIN.set((str(entity_id), str(user_id), frozenset(workspace_ids)) if entity_id and user_id else None)


def session_pin(entity_id: str | None, user_id: str | None) -> frozenset[str] | None:
    """The scoped session's Workspaces for this person in this request, or None."""
    pin = _PIN.get()
    if pin is None or pin[0] != str(entity_id) or pin[1] != str(user_id):
        return None
    return pin[2]
