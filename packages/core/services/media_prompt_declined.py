"""A media provider refusing the description itself is the person's to fix.

Music, picture and video providers run their own content checks: Google
Lyria answers ``Input blocked: The prompt could not be processed``, OpenAI
``content_policy_violation``, others a safety-system message. Retrying the
same description fails the same way, and substituting stock music or a
library picture hides the problem — the person has to describe it
differently. Such failures become ``prompt_declined`` with a sentence that
says so, so a Workflow can send the work back to the person and a chat can
ask for a new description.
"""
from __future__ import annotations

import json
import re
from typing import Any

PROMPT_DECLINED = "prompt_declined"

# The providers' own wording for "your request's content was refused". A
# generic "blocked" or "rejected" is not enough: quota, auth and network
# failures say that too and are not the person's to fix.
_DECLINED = re.compile(
    r"input blocked"
    r"|prompt could not be processed"
    r"|content[ _-]?policy"
    r"|safety (?:system|filter|settings|reasons)"
    r"|responsible ai"
    r"|prohibited[ _]content"
    r"|blocked by (?:our )?(?:safety|moderation)"
    r"|moderation[ _]blocked"
    r"|flagged by (?:our )?(?:moderation|safety)"
    r"|violat\w* (?:our|the) (?:usage|content|safety) polic",
    re.IGNORECASE,
)

_SERVICE = {"music": "music", "audio": "audio", "image": "picture", "video": "video"}


def provider_declined_prompt(text: Any) -> bool:
    """Whether a provider error says the request's content was refused."""
    return bool(text) and bool(_DECLINED.search(str(text)))


def declined_message(kind: str, provider_text: Any) -> str:
    """The sentence a person acts on, keeping the provider's own words."""
    service = _SERVICE.get(kind, "generation")
    detail = " ".join(str(provider_text or "").split())[:300]
    return (
        f"The {service} service declined this description under its content policy"
        f"{f' ({detail})' if detail else ''}. Describe it differently — for example leave out real "
        "people's or artists' names and lyrics or text you did not write — and try again."
    )


def mark_declined_result(result: Any, kind: str) -> Any:
    """A tool's JSON error result, typed ``prompt_declined`` when the content was refused."""
    if not isinstance(result, str):
        return result
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return result
    if not isinstance(payload, dict) or payload.get("code") == PROMPT_DECLINED:
        return result
    text = payload.get("error") or payload.get("message") or payload.get("reason")
    if not isinstance(text, str) or not provider_declined_prompt(text):
        return result
    return json.dumps(
        {**payload, "code": PROMPT_DECLINED, "error": declined_message(kind, text), "retryable": False},
        ensure_ascii=False,
        default=str,
    )
