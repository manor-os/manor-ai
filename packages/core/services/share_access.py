"""Audience verification for externally shared resources."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from packages.core.config import get_settings
from packages.core.models.permission import Share


OTP_TTL_SECONDS = 10 * 60
ACCESS_TTL_SECONDS = 60 * 60
VIEW_SESSION_TTL_SECONDS = 60 * 60
MAX_OTP_ATTEMPTS = 5
MAX_CONCURRENT_OTP_CHALLENGES = 5


class ShareAccessError(ValueError):
    pass


@dataclass(frozen=True)
class ShareOtpChallenge:
    email: str
    code: str
    challenge_id: str


def _secret() -> bytes:
    return get_settings().JWT_SECRET_KEY.encode("utf-8")


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(value: str) -> str:
    return _b64_encode(hmac.new(_secret(), value.encode("ascii"), hashlib.sha256).digest())


def normalize_share_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ShareAccessError("A valid email address is required")
    return email


def share_requires_verification(share: Share) -> bool:
    return bool(getattr(share, "require_otp", False)) or str(share.audience or "anonymous") != "anonymous"


def audience_allows_email(share: Share, email: str) -> bool:
    audience = str(share.audience or "anonymous").strip().lower()
    if audience == "anonymous":
        return True
    if audience.startswith("email:"):
        return hmac.compare_digest(email, audience[6:])
    if audience.startswith("domain:"):
        domain = audience[7:].lstrip("@")
        return email.rsplit("@", 1)[-1] == domain
    return False


def _active_otp_challenges(value: Any, *, now: int) -> list[dict[str, Any]]:
    """Normalize legacy single challenges and retain only active proofs."""
    if isinstance(value, dict):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = []
    return [
        dict(challenge)
        for challenge in candidates
        if isinstance(challenge, dict)
        and challenge.get("proof")
        and int(challenge.get("exp") or 0) >= now
    ]


def create_otp_challenge_record(
    share: Share,
    email_value: str,
) -> ShareOtpChallenge:
    """Create one identifiable challenge so failed delivery can remove it."""

    email = normalize_share_email(email_value)
    if not audience_allows_email(share, email):
        # Do not reveal whether an address is the configured recipient.
        raise ShareAccessError("This email address cannot access the share")
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge_id = secrets.token_urlsafe(12)
    expires_at = int(time.time()) + OTP_TTL_SECONDS
    key = hashlib.sha256(email.encode("utf-8")).hexdigest()
    proof = hmac.new(
        _secret(),
        f"{share.id}:{email}:{code}:{expires_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    metadata = dict(getattr(share, "metadata_", {}) or {})
    challenges = dict(metadata.get("otp_challenges") or {})
    # Bound stale challenge growth on long-lived links.
    now = int(time.time())
    current = _active_otp_challenges(challenges.get(key), now=now)
    current.append({
        "id": challenge_id,
        "proof": proof,
        "exp": expires_at,
        "attempts": 0,
    })
    challenges[key] = current[-MAX_CONCURRENT_OTP_CHALLENGES:]
    metadata["otp_challenges"] = {
        challenge_key: active
        for challenge_key, challenge in challenges.items()
        if (active := _active_otp_challenges(challenge, now=now))
    }
    share.metadata_ = metadata
    return ShareOtpChallenge(
        email=email,
        code=code,
        challenge_id=challenge_id,
    )


def create_otp_challenge(share: Share, email_value: str) -> tuple[str, str]:
    """Compatibility wrapper for callers that do not need delivery cleanup."""

    challenge = create_otp_challenge_record(share, email_value)
    return challenge.email, challenge.code


def discard_otp_challenge(
    share: Share,
    email_value: str,
    challenge_id: str,
) -> bool:
    """Remove exactly one challenge after a determinate delivery failure."""

    email = normalize_share_email(email_value)
    key = hashlib.sha256(email.encode("utf-8")).hexdigest()
    metadata = dict(getattr(share, "metadata_", {}) or {})
    challenges = dict(metadata.get("otp_challenges") or {})
    current = _active_otp_challenges(challenges.get(key), now=int(time.time()))
    remaining = [
        challenge
        for challenge in current
        if str(challenge.get("id") or "") != str(challenge_id)
    ]
    if len(remaining) == len(current):
        return False
    if remaining:
        challenges[key] = remaining
    else:
        challenges.pop(key, None)
    metadata["otp_challenges"] = challenges
    share.metadata_ = metadata
    return True


def verify_otp_challenge(share: Share, email_value: str, code_value: str) -> str:
    email = normalize_share_email(email_value)
    code = str(code_value or "").strip()
    key = hashlib.sha256(email.encode("utf-8")).hexdigest()
    metadata = dict(getattr(share, "metadata_", {}) or {})
    challenges = dict(metadata.get("otp_challenges") or {})
    now = int(time.time())
    active = _active_otp_challenges(challenges.get(key), now=now)
    if not active:
        challenges.pop(key, None)
        metadata["otp_challenges"] = challenges
        share.metadata_ = metadata
        raise ShareAccessError("Verification code is invalid or expired")

    matched = False
    for challenge in active:
        expected = hmac.new(
            _secret(),
            f"{share.id}:{email}:{code}:{int(challenge['exp'])}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        matched = hmac.compare_digest(
            expected,
            str(challenge.get("proof") or ""),
        ) or matched
    if not matched:
        remaining = []
        for challenge in active:
            attempts = int(challenge.get("attempts") or 0) + 1
            if attempts < MAX_OTP_ATTEMPTS:
                challenge["attempts"] = attempts
                remaining.append(challenge)
        if remaining:
            challenges[key] = remaining
        else:
            challenges.pop(key, None)
        metadata["otp_challenges"] = challenges
        share.metadata_ = metadata
        raise ShareAccessError("Verification code is invalid or expired")

    challenges.pop(key, None)
    metadata["otp_challenges"] = challenges
    share.metadata_ = metadata
    return create_share_access_token(share=share, email=email)


def create_share_access_token(*, share: Share, email: str) -> str:
    now = int(time.time())
    share_exp = int(share.expires_at.timestamp()) if share.expires_at else now + ACCESS_TTL_SECONDS
    payload: dict[str, Any] = {
        "share_id": share.id,
        "email": email,
        "exp": min(share_exp, now + ACCESS_TTL_SECONDS),
    }
    encoded = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{encoded}.{_sign(encoded)}"


def verify_share_access(share: Share, access_token: str | None) -> str | None:
    if not share_requires_verification(share):
        return None
    try:
        encoded, signature = str(access_token or "").split(".", 1)
        if not hmac.compare_digest(signature, _sign(encoded)):
            raise ValueError
        payload = json.loads(_b64_decode(encoded).decode("utf-8"))
        email = normalize_share_email(payload.get("email"))
        if str(payload.get("share_id") or "") != share.id:
            raise ValueError
        if int(payload.get("exp") or 0) < int(time.time()):
            raise ValueError
        if not audience_allows_email(share, email):
            raise ValueError
        return email
    except Exception as exc:
        raise ShareAccessError("Email verification is required") from exc


def create_share_view_session(*, share: Share, use_count: int) -> str:
    """Bind public preview subresources to one already-counted share view."""
    now = int(time.time())
    share_exp = int(share.expires_at.timestamp()) if share.expires_at else now + VIEW_SESSION_TTL_SECONDS
    payload: dict[str, Any] = {
        "share_id": share.id,
        "use_count": use_count,
        "exp": min(share_exp, now + VIEW_SESSION_TTL_SECONDS),
    }
    encoded = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{encoded}.{_sign(encoded)}"


def verify_share_view_session(share: Share, session_token: str | None) -> bool:
    try:
        encoded, signature = str(session_token or "").split(".", 1)
        if not hmac.compare_digest(signature, _sign(encoded)):
            return False
        payload = json.loads(_b64_decode(encoded).decode("utf-8"))
        if str(payload.get("share_id") or "") != share.id:
            return False
        if int(payload.get("exp") or 0) < int(time.time()):
            return False
        use_count = int(payload.get("use_count") or 0)
        return 0 < use_count <= int(share.use_count or 0)
    except Exception:
        return False
