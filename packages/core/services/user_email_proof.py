"""Current-address verification evidence; account activation alone is not proof."""

from datetime import datetime, timezone

from packages.core.models.user import User


def email_is_verified(user: User) -> bool:
    return bool(
        user.status == "active"
        and user.deleted_at is None
        and user.email
        and getattr(user, "verified_email", None) == user.email
        and isinstance(getattr(user, "email_verified_at", None), datetime)
    )


def mark_email_verified(user: User, email: str) -> None:
    """Call only after a consumed OTP or explicit verified provider claim."""
    if email and user.email == email and user.status == "active" and user.deleted_at is None:
        user.verified_email = email
        user.email_verified_at = datetime.now(timezone.utc)


def provider_verified_claim(value: object) -> bool:
    # Apple returns the string; Google normally returns JSON boolean true.
    # In particular, missing values, 1, and "false" are not evidence.
    return value is True or value == "true"
