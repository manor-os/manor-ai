"""PendingAction — generic "the agent needs the user to do something" payload.

Reuses the existing ``messages.pending_action`` JSONB column that
Strategist proposals + ai_tasks retries already write to. Unlike
those — which are domain-specific to the workspace review loop —
this module covers the **MCP-tool-level** cases:

  * Tool hits a login wall      → ``NeedsLogin``
  * Tool can't fill a form field → ``NeedsInput``
  * Tool wants to do a destructive thing → ``NeedsConfirmation``

The shape is intentionally free-form JSON (matches Strategist's
existing convention). Each kind defines its own keys; the only
universal fields are ``kind`` + ``options``.

Producer / consumer split
─────────────────────────
**Producers** are MCP tool wrappers. They embed a PendingAction in
the standard MCP envelope as the optional ``_pending_action`` key:

    {
      "content": [{"type": "text", "text": "..."}],
      "isError": False,
      "_pending_action": {"kind": "needs_login", ...}
    }

The underscore prefix marks this as Manor-internal — the MCP spec
ignores it, the LLM (which only reads ``content``) ignores it,
existing tools that don't set it just don't.

**Consumers** are the two places that dispatch MCP tools:

  1. ``packages.core.ai.tools.mcp_builtin._build_handler`` (regular
     chat — LLM tool calls). Sees ``_pending_action`` → posts a chat
     message with that payload + tells the LLM the user is being
     asked.

  2. ``packages.core.plans.executor`` (Plan DAG steps). Sees
     ``_pending_action`` → marks step ``waiting_human`` + posts a
     chat message with the payload. Plan resumes when the user
     resolves it via the resolve endpoint.

Both consumers are unchanged today (Phase 3a = producer side only).
The contract is documented here so they can be wired in a separate
PR without retro-fitting all the producers.

Resolution
──────────
The frontend renders buttons keyed on ``options``. When the user
picks one, it POSTs to a single resolve endpoint (TODO) carrying the
``message_id`` + chosen option + any free-form payload (e.g. answers
to questions). The kind-specific handler decides how to resume:

  * needs_login        → spawn /login_session, capture, retry tool
  * needs_input        → re-call tool with answers in args
  * needs_confirmation → re-call tool with confirm=True
"""
from __future__ import annotations

import secrets
from typing import Any, ClassVar, Dict, List, Literal, Optional

from packages.core.constants.pending_actions import PendingActionKind


# The kind vocabulary is ``PendingActionKind`` (packages/core/constants/
# pending_actions.py) — one enum for every card kind in the repo, not just the
# three this module produces.
#
# These module-level ``KIND_*`` names are kept as aliases onto its members.
# They are what the dispatcher, the resolve endpoint and the producer tests
# already import, and because the enum is ``str``-based an alias is the member
# itself: ``KIND_NEEDS_LOGIN == "needs_login"`` and ``KIND_NEEDS_LOGIN.value``
# both hold, so no caller had to change. Prefer ``PendingActionKind`` in new
# code; the aliases exist so this refactor stayed a type change and not a
# rewrite of the four consumers.
KIND_NEEDS_LOGIN = PendingActionKind.NEEDS_LOGIN
KIND_NEEDS_INPUT = PendingActionKind.NEEDS_INPUT
KIND_NEEDS_CONFIRMATION = PendingActionKind.NEEDS_CONFIRMATION

# The free-form text-input card the chat notifier builds when a lease pauses
# without a structured payload. Not tool-produced, but it lands in the same
# ``pending_action`` column and is answered through the same endpoint.
KIND_HUMAN_INPUT = PendingActionKind.HUMAN_INPUT

# Kinds produced elsewhere in the codebase, re-exported here so consumers that
# route on the whole union have one import.
KIND_APPROVE_PROPOSALS = PendingActionKind.APPROVE_PROPOSALS
KIND_RETRY_STRATEGIST_REVIEW = PendingActionKind.RETRY_STRATEGIST_REVIEW
KIND_GOVERNANCE_APPROVAL = PendingActionKind.GOVERNANCE_APPROVAL


#: Mid-execution ("path C") HITL pauses whose unified HitlRequest the chat
#: resolve endpoint knows how to close.
#:
#: ⚠ This is the ONE definition shared by both ends of that lifecycle:
#:   * mint  — ``dispatcher.service.lease_needs_human`` mints only for kinds
#:             in this set;
#:   * close — ``apps.api.routers.workspace_chat.resolve_chat_action`` decides
#:             requests only for cards of these kinds.
#: They must stay equal. Minting a kind the endpoint cannot close strands the
#: row PENDING forever, and it is a closed loop rather than a leak that some
#: sweep later collects: the step stays waiting_human, so the plan never
#: reaches a terminal state, so ``resolve_origin_requests`` — whose only
#: production caller is the plan's terminal path — never runs.
#:
#: Adding a kind here means first giving its resolve branch a grant/deny
#: verdict (see ``_lease_decision``); otherwise you reopen the same hole from
#: the other side.
#:
#: Members are ``.value`` (plain ``str``), not enum members: the thing tested
#: against this set is a raw word off a JSONB blob, and a set of plain strings
#: is the one spelling that cannot depend on how a mixin enum hashes.
LEASE_HITL_CLOSEABLE_KINDS: frozenset[str] = frozenset({
    KIND_HUMAN_INPUT.value,
    KIND_NEEDS_INPUT.value,
    KIND_NEEDS_CONFIRMATION.value,
    KIND_NEEDS_LOGIN.value,
})


# Envelope key. Underscore prefix → Manor-internal, ignored by MCP
# spec consumers and by LLMs (which only read ``content``).
_ENVELOPE_KEY = "_pending_action"


def _new_resume_token() -> str:
    """Short opaque token the resolve endpoint uses to find the
    original tool invocation. Frontends echo it back verbatim."""
    return secrets.token_urlsafe(8)


class PendingAction:
    """Builder for the JSONB payload that goes onto messages.pending_action.

    Use the kind-specific constructors (``needs_login`` / ``needs_input``
    / ``needs_confirmation``) instead of instantiating directly — they
    enforce the per-kind required fields.
    """

    # Maps kind → required keys (other than 'kind' / 'options'). Used by
    # the from_dict roundtrip to validate before consumers act on it.
    _REQUIRED_BY_KIND: ClassVar[Dict[str, List[str]]] = {
        KIND_NEEDS_LOGIN.value: ["login_url"],
        KIND_NEEDS_INPUT.value: ["questions"],
        KIND_NEEDS_CONFIRMATION.value: ["action_summary"],
    }

    def __init__(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        if not payload.get("kind"):
            raise ValueError("payload missing 'kind'")
        kind = payload["kind"]
        for required in self._REQUIRED_BY_KIND.get(kind, []):
            if payload.get(required) in (None, "", []):
                raise ValueError(f"{kind!r} payload missing required field {required!r}")
        self._payload = dict(payload)
        if "options" not in self._payload:
            self._payload["options"] = self._default_options(kind)

    # ── Constructors for the three MCP-tool-level kinds ──

    @classmethod
    def needs_login(
        cls,
        *,
        login_url: str,
        title: Optional[str] = None,
        integration_hint: Optional[str] = None,
        provider_key: Optional[str] = None,
        resume_token: Optional[str] = None,
        partial_data: Optional[Any] = None,
    ) -> "PendingAction":
        """Tool hit a login / SSO / CAPTCHA wall.

        Frontend: render a "Sign in to {hint}" button. Click → spawn
        /login_session start at ``login_url`` → on capture, retry the
        original tool call with the new credentials (use ``resume_token``
        to look up what to retry).

        Args:
            login_url: where to send the user's browser. Required.
            title: human-readable header (default derived from
                integration_hint or login_url's host).
            integration_hint: e.g. ``"stripe.com"``; helps the
                Integrations page pre-select the provider on capture.
            provider_key: explicit MCPServer.server_key to associate
                captured cookies with. Wins over integration_hint when
                both are set.
            resume_token: opaque token the resolve endpoint uses to
                find the original invocation. Auto-generated if omitted.
            partial_data: anything the tool managed to extract before
                hitting the wall. Surfaced to the user (and kept in the
                resume context) so re-running starts cheaper.
        """
        if not login_url:
            raise ValueError("login_url required")
        host = _host_of(login_url)
        return cls({
            "kind": KIND_NEEDS_LOGIN.value,
            "title": title or (
                f"Sign in to {integration_hint or host}"
                if (integration_hint or host)
                else "Sign in required"
            ),
            "login_url": login_url,
            "integration_hint": integration_hint,
            "provider_key": provider_key,
            "resume_token": resume_token or _new_resume_token(),
            "partial_data": partial_data,
            "options": ["sign_in", "skip"],
        })

    @classmethod
    def needs_input(
        cls,
        *,
        questions: List[Dict[str, Any]],
        title: Optional[str] = None,
        context_summary: Optional[str] = None,
        resume_token: Optional[str] = None,
    ) -> "PendingAction":
        """Tool can't fill one or more required fields.

        ``questions`` is a list of ``{label, type, options?, required?}``
        — same shape ``easy_apply`` already returns in
        ``blocking_questions``. Frontend renders a form; resolve
        endpoint feeds the answers back into the tool's params dict.

        Args:
            questions: list of question dicts. Required, non-empty.
            title: human-readable header.
            context_summary: 1-line summary of what we were trying to
                do (e.g. ``"Applying to Coinbase Senior Backend"``).
            resume_token: opaque token for the resolve endpoint.
        """
        if not questions:
            raise ValueError("questions required and non-empty")
        return cls({
            "kind": KIND_NEEDS_INPUT.value,
            "title": title or "I need a few answers",
            "context_summary": context_summary,
            "questions": questions,
            "resume_token": resume_token or _new_resume_token(),
            "options": ["provide_answers", "skip"],
        })

    @classmethod
    def needs_confirmation(
        cls,
        *,
        action_summary: str,
        title: Optional[str] = None,
        impact: Optional[str] = None,
        resume_token: Optional[str] = None,
    ) -> "PendingAction":
        """Tool wants to do a destructive thing (Submit / Send / Buy /
        Pay) and needs explicit user OK.

        Resolve endpoint re-calls the tool with ``confirm=True``.

        Args:
            action_summary: short imperative description (e.g.
                ``"Submit Coinbase Senior Backend application"``).
                Required.
            title: human-readable header.
            impact: optional one-liner about consequences (e.g.
                ``"This sends $42 to the merchant."``).
            resume_token: opaque token for the resolve endpoint.
        """
        if not action_summary:
            raise ValueError("action_summary required")
        return cls({
            "kind": KIND_NEEDS_CONFIRMATION.value,
            "title": title or "Please confirm",
            "action_summary": action_summary,
            "impact": impact,
            "resume_token": resume_token or _new_resume_token(),
            "options": ["confirm", "cancel"],
        })

    # ── (De)serialization ──

    def to_dict(self) -> Dict[str, Any]:
        """JSONB-ready payload for ``messages.pending_action``."""
        return dict(self._payload)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PendingAction":
        """Round-trip from a stored JSONB blob. Validates required
        fields per kind."""
        return cls(payload)

    @property
    def kind(self) -> str:
        return self._payload["kind"]

    # ── MCP envelope helpers ──

    def attach_to_envelope(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """Mutate the standard MCP envelope to carry this PendingAction
        under ``_pending_action``. Returns the same envelope so callers
        can chain."""
        envelope[_ENVELOPE_KEY] = self.to_dict()
        return envelope

    @staticmethod
    def from_envelope(envelope: Dict[str, Any]) -> Optional["PendingAction"]:
        """Read a PendingAction off a tool result envelope, if present.
        Returns None when the envelope has no ``_pending_action``."""
        if not isinstance(envelope, dict):
            return None
        raw = envelope.get(_ENVELOPE_KEY)
        if not isinstance(raw, dict):
            return None
        try:
            return PendingAction.from_dict(raw)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _default_options(kind: str) -> List[str]:
        return {
            KIND_NEEDS_LOGIN.value: ["sign_in", "skip"],
            KIND_NEEDS_INPUT.value: ["provide_answers", "skip"],
            KIND_NEEDS_CONFIRMATION.value: ["confirm", "cancel"],
        }.get(kind, [])


def _host_of(url: str) -> Optional[str]:
    """Best-effort hostname extract for default-title generation. We
    keep this private to avoid pulling urllib into hot paths just for
    title fallbacks."""
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return (urlparse(url).hostname or "").lower() or None
    except Exception:  # noqa: BLE001
        return None
