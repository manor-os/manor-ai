"""Every question Manor asks a person carries one recommended answer.

A card (``messages.pending_action``, a ``meta.hitl_requests`` entry, or an
Agent ``request_input`` question) offers ``options``: the wire choices its
resolve path understands. This module adds the shared contract on top:

* ``recommendation`` — ``{"option", "reason", "reason_code"?, "reason_params"?}``.
  ``option`` is exactly one of ``options`` (one recommendation per question,
  by construction), ``reason`` is one plain sentence a person reads under the
  buttons. ``reason_code`` names a shared sentence so a client can show it in
  the viewer's language; ``reason`` is always the readable fallback.
* ``options`` are reordered so the recommended one is first. Order never
  changes meaning: resolve paths read the choice value, not its position.
* ``option_details`` — optional ``{value: {"label", "payload"?, "scope"?}}`` for
  options whose button needs words of its own (a proposal combination, the
  scope an "Always allow" covers). ``payload`` is sent back with the choice.
* ``batch_key`` — optional; open cards sharing it are one kind of question
  and may be presented together ("approve all / review each").
* ``allow_other`` — the person may always answer in their own words instead
  ("Something else…"); a card opts out only when it has nowhere to send text.

Producers that know better (an Agent's question, a Strategist proposal, the
authorization policy) set the recommendation themselves; every other card gets
the default derived here from what its options mean, so no question reaches a
person without one. A producer-supplied recommendation that breaks the
contract is a producer bug and raises at write time.
"""
from __future__ import annotations

import re
from typing import Any

RECOMMENDATION_KEY = "recommendation"
OPTION_DETAILS_KEY = "option_details"
ALLOW_OTHER_KEY = "allow_other"

#: A reason is one sentence under the buttons, not an essay.
MAX_REASON_CHARS = 280


class RecommendationError(ValueError):
    """A producer supplied a recommendation that breaks the contract."""


#: Shared sentences by code. Clients localize the code; the text is the
#: readable fallback stored with the card.
REASONS: dict[str, str] = {
    "approve_once": "This is the next step of the work you asked for.",
    "irreversible_once": "This can't be undone, so approve just this one.",
    "repeated_always": (
        "You've approved this {count} times and never declined; you can turn it off anytime."
    ),
    "proposal_all": "Doing all of them together moves the work forward fastest.",
    "review_ready": "It is ready to use; ask for changes if anything looks off.",
    "run_flow": "The inputs are filled in, so running it continues the work.",
    "answer_needed": "The work continues as soon as you answer.",
    "reconnect": "Reconnecting lets the work continue; you sign in on the provider's own page.",
    "connect_pending": "Connecting {provider} starts the work waiting for it; you sign in on the provider's own page.",
    "retry": "Trying again continues from where it stopped.",
    "avoid_duplicate": "Confirming it already happened avoids doing the same thing twice.",
    "review_first": "Look at the result before deciding what to do next.",
    "most_likely": "This is the most likely answer.",
    "best_match": "It matches your request most closely.",
}

#: What each well-known choice means, best first. The first option of a card
#: found here is its default recommendation. Values not listed fall back to the
#: card's first option.
_DEFAULT_PRIORITY: tuple[tuple[str, str], ...] = (
    ("approve_all", "proposal_all"),
    ("confirm_applied", "avoid_duplicate"),
    ("approve", "approve_once"),
    ("accept", "approve_once"),
    ("confirm", "approve_once"),
    ("run", "run_flow"),
    ("provide_answers", "answer_needed"),
    ("respond", "answer_needed"),
    ("submit", "answer_needed"),
    ("sign_in", "reconnect"),
    ("reconnect", "reconnect"),
    ("retry", "retry"),
    ("retry_now", "retry"),
    ("resume", "retry"),
    ("review", "review_first"),
)
_REVIEW_KINDS = frozenset({"task_approval", "workspace_operation_review"})


def reason_text(code: str, **params: Any) -> str:
    template = REASONS[code]
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template


def recommendation(
    option: str,
    *,
    reason: str | None = None,
    reason_code: str | None = None,
    reason_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a recommendation; a shared ``reason_code`` supplies the text."""
    if reason is None:
        if reason_code is None:
            raise RecommendationError("a recommendation needs a reason")
        reason = reason_text(reason_code, **(reason_params or {}))
    value: dict[str, Any] = {"option": str(option), "reason": str(reason).strip()}
    if reason_code:
        value["reason_code"] = reason_code
    if reason_params:
        value["reason_params"] = dict(reason_params)
    return value


def _options(action: dict[str, Any]) -> list[str]:
    raw = action.get("options")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item.strip()]


def validate_recommendation(action: dict[str, Any]) -> None:
    """Raise when ``action`` breaks the recommended-option contract."""
    options = _options(action)
    rec = action.get(RECOMMENDATION_KEY)
    if rec is None:
        if options:
            raise RecommendationError("a question with options needs a recommended option")
        return
    if not isinstance(rec, dict):
        raise RecommendationError("recommendation must be an object")
    option = rec.get("option")
    if not isinstance(option, str) or not option.strip():
        raise RecommendationError("recommendation.option is required")
    if options and option not in options:
        raise RecommendationError(f"recommended option {option!r} is not one of the options")
    reason = rec.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise RecommendationError("recommendation.reason is required")
    if len(reason) > MAX_REASON_CHARS or "\n" in reason.strip():
        raise RecommendationError("recommendation.reason must be one short sentence")
    details = action.get(OPTION_DETAILS_KEY)
    if details is not None:
        if not isinstance(details, dict):
            raise RecommendationError("option_details must map option values to details")
        unknown = [key for key in details if key not in options]
        if unknown:
            raise RecommendationError(f"option_details names unknown options: {unknown}")


def default_recommendation(action: dict[str, Any]) -> dict[str, Any] | None:
    """The recommendation a card gets when its producer did not choose one."""
    options = _options(action)
    if not options:
        return None
    normalized = {option.strip().lower(): option for option in options}
    for value, code in _DEFAULT_PRIORITY:
        if value in normalized:
            if value == "approve" and str(action.get("kind") or "") in _REVIEW_KINDS:
                code = "review_ready"
            if value == "approve" and str(action.get("hitl_type") or "") == "review":
                code = "review_ready"
            return recommendation(normalized[value], reason_code=code)
    return recommendation(options[0], reason_code="most_likely")


def ensure_recommendation(action: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``action`` carrying a valid recommendation, recommended first.

    Never mutates its argument. A card without options (free-text input) is
    returned as is unless its producer suggested an answer.
    """
    if not isinstance(action, dict):
        return action
    result = dict(action)
    if result.get(RECOMMENDATION_KEY) is None:
        default = default_recommendation(result)
        if default is not None:
            result[RECOMMENDATION_KEY] = default
    validate_recommendation(result)
    rec = result.get(RECOMMENDATION_KEY)
    options = _options(result)
    if isinstance(rec, dict) and rec.get("option") in options:
        chosen = rec["option"]
        result["options"] = [chosen, *[option for option in options if option != chosen]]
    if options:
        result.setdefault(ALLOW_OTHER_KEY, True)
    return result


def project_recommendation(action: dict[str, Any] | None) -> dict[str, Any] | None:
    """Read-side projection for stored cards: never fails a read.

    Cards written before this contract (or by a path that bypasses the write
    chokepoints) get their default; a stored card that cannot be repaired is
    returned unchanged rather than breaking the chat history.
    """
    if not isinstance(action, dict):
        return action
    prompt = action.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        cleaned = strip_internal_rule_mentions(prompt).strip()
        if cleaned != prompt:
            # Older cards titled themselves with a rule id; show the action.
            action = {**action, "prompt": cleaned or readable_action(action.get("action"))}
    try:
        return ensure_recommendation(action)
    except RecommendationError:
        repaired = {key: value for key, value in action.items() if key != RECOMMENDATION_KEY}
        try:
            return ensure_recommendation(repaired)
        except RecommendationError:
            return action


#: Card types that ask for permission. Every one is shown as the
#: authorization card (``"approval"``), never as a free-text question.
AUTHORIZATION_CARD_TYPES = frozenset({"approval", "authorize"})
_INTERNAL_KEY = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


def is_internal_key(text: Any) -> bool:
    """A policy, rule or action key (``approval.mode``), not words for people."""
    return bool(_INTERNAL_KEY.match(str(text or "").strip()))


def readable_action(action_key: Any) -> str:
    """Words for an action key: ``workspace.agent.capabilities.modify`` →
    "Modify workspace agent capabilities"."""
    parts = [part.replace("_", " ") for part in str(action_key or "").split(".") if part]
    if len(parts) < 2:
        return "Approve this action"
    verb, subject = parts[-1], " ".join(parts[:-1])
    return f"{verb[:1].upper()}{verb[1:]} {subject}"


def user_facing_reason(text: Any) -> str:
    """A reason fit to show a person: empty when it is an internal key."""
    value = " ".join(str(text or "").split())
    return "" if is_internal_key(value) else value


_RULE_LINE = re.compile(r"(?im)^[ \t]*rule:[ \t]*`?[a-z0-9_.:-]+`?[ \t]*\n?")
_RULE_PARENTHETICAL = re.compile(r"[ \t]*\((?:governance )?rule:?[ \t]*`?[a-z0-9_.:-]+`?\)", re.IGNORECASE)


def is_internal_key_only_message(message: Any) -> bool:
    """A stored message whose whole text is an internal key ("approval.mode")
    and that carries nothing else to show: hidden from history."""
    if getattr(message, "role", None) == "user" or getattr(message, "pending_action", None):
        return False
    meta = getattr(message, "meta", None)
    if isinstance(meta, dict) and (meta.get("hitl_requests") or meta.get("attachments")):
        return False
    return is_internal_key(str(getattr(message, "content", "") or "").strip())


def strip_internal_rule_mentions(text: Any) -> str:
    """Stored message text without the internal rule ids older cards carried
    ("Rule: `approval.mode`", "(rule: `approval.mode`)"); a text that is only
    an internal key reads as nothing."""
    value = str(text or "")
    if is_internal_key(value.strip()):
        return ""
    return _RULE_PARENTHETICAL.sub("", _RULE_LINE.sub("", value))


def _project_authorization_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if str(entry.get("type") or "") not in AUTHORIZATION_CARD_TYPES:
        return entry
    item = dict(entry)
    item["type"] = "approval"
    prompt = str(item.get("prompt") or "").strip()
    if not prompt or is_internal_key(prompt):
        item["prompt"] = readable_action(item.get("action")) if is_internal_key(item.get("action")) else (
            str(item.get("action") or "").strip() or "Approve this action"
        )
    return item


def project_hitl_entries(entries: list[dict] | None) -> list[dict] | None:
    """Apply the read-side projection to ``meta.hitl_requests`` entries.

    Every authorization is the authorization card with words for a title:
    an ``authorize`` entry is an ``approval`` card, and an internal key
    (``approval.mode``) is never shown as its title.
    """
    if not isinstance(entries, list):
        return entries
    return [
        project_recommendation(_project_authorization_entry(entry)) if isinstance(entry, dict) else entry
        for entry in entries
    ]


#: A question answered by replying offers at most this many options
#: (plus "Something else…").
MAX_REPLY_OPTIONS = 3


def reply_question(
    question: Any, options: Any, recommended: Any, reason: Any,
) -> dict[str, Any]:
    """A question the person answers by replying, its recommended answer first.

    ``answer_as: "message"``: picking an option sends it as the person's next
    message, the path a typed answer takes. A recommended answer missing from
    ``options`` becomes the first option; at most ``MAX_REPLY_OPTIONS`` stay.
    """
    text = " ".join(str(question or "").split())
    answer = " ".join(str(recommended or "").split())[:200]
    why = " ".join(str(reason or "").split())[:MAX_REASON_CHARS]
    if not text or not answer or not why:
        raise RecommendationError("a question needs its text, a recommended answer and a reason")
    choices: list[str] = [answer]
    for item in options if isinstance(options, (list, tuple)) else []:
        value = " ".join(str(item or "").split())[:200]
        if value and value not in choices:
            choices.append(value)
    return ensure_recommendation({
        "question": text[:1000],
        "options": choices[:MAX_REPLY_OPTIONS],
        "recommendation": recommendation(answer, reason=why),
        "answer_as": "message",
    })


_CHANNEL_LABELS = {
    "approve": "Approve", "reject": "Reject", "confirm": "Confirm", "cancel": "Cancel",
    "retry": "Try again", "run": "Run", "sign_in": "Sign in", "skip": "Skip",
}
_CHANNEL_SYNONYMS = {
    "approve": ["yes", "ok", "y"],
    "reject": ["no", "deny", "n"],
}


def channel_actions(action: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Native channel buttons (Telegram, Slack, numbered text) for a card.

    The card's own options in its order — recommended first — so a channel
    reply resolves exactly what the in-app buttons would. The recommended
    button says so, since channels cannot colour it.
    """
    card = project_recommendation(action) or {}
    details = card.get(OPTION_DETAILS_KEY) if isinstance(card.get(OPTION_DETAILS_KEY), dict) else {}
    rec = card.get(RECOMMENDATION_KEY) if isinstance(card.get(RECOMMENDATION_KEY), dict) else {}
    buttons: list[dict[str, Any]] = []
    for option in _options(card):
        detail = details.get(option) if isinstance(details.get(option), dict) else {}
        label = str(detail.get("label") or _CHANNEL_LABELS.get(option) or option.replace("_", " ").capitalize())
        if option == rec.get("option"):
            label = f"{label} · Recommended"
        buttons.append({"key": option, "label": label, "synonyms": list(_CHANNEL_SYNONYMS.get(option, []))})
    return buttons
