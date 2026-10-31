"""One rule for which avatar represents a person.

The team page, sharing and people pickers must agree: a user's own avatar
(an upload or their Google photo) unless it is the old generated placeholder,
otherwise the photo on their staff record, otherwise none — clients then draw
initials from the real name.

The placeholder was a DiceBear "initials" image seeded with an 8-character
hash of the name, so it showed random letters. Signup no longer stores it and
cloud data is cleared by migration 20261013_02; self-hosted data may still
hold it, so readers keep ignoring it.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from packages.core.models.staff import Staff
from packages.core.models.user import User

_GENERATED_PLACEHOLDER = re.compile(
    r"^https://api\.dicebear\.com/[^/]+/initials/svg\?seed=[0-9a-f]{8}"
    r"&backgroundColor=0f766e,14b8a6,0d9488,0ea5e9,6366f1,8b5cf6(&|$)"
)


def real_avatar_url(url: str | None) -> str | None:
    """The avatar URL, or None when absent or the generated placeholder."""
    value = (url or "").strip()
    if not value or _GENERATED_PLACEHOLDER.match(value):
        return None
    return value


def person_avatar_url(user_avatar_url: str | None, staff_avatar_url: str | None) -> str | None:
    return real_avatar_url(user_avatar_url) or real_avatar_url(staff_avatar_url)


def staff_avatar_column(entity_id: str):
    """Correlated scalar: the photo on ``User``'s staff record in this Entity."""
    return (
        select(Staff.avatar_url)
        .where(
            Staff.user_id == User.id,
            Staff.entity_id == entity_id,
            Staff.deleted_at.is_(None),
            Staff.avatar_url.is_not(None),
        )
        .order_by(Staff.created_at.desc())
        .limit(1)
        .correlate(User)
        .scalar_subquery()
    )
