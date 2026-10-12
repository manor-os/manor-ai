"""International Lark user OAuth contracts (legacy app accounts stay separate)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

LARK_OAUTH_SCOPES = (
    "offline_access contact:user.base:readonly im:chat:read im:message "
    "im:message.send_as_user im:message.group_msg:get_as_user "
    "im:message.p2p_msg:get_as_user docx:document bitable:app"
)
LARK_USER_INFO_URL = "https://open.larksuite.com/open-apis/authen/v1/user_info"


def lark_token_response_is_valid(data: Any, *, scopes: str) -> bool:
    """A successful v2 grant has a single-use refresh token and explicit expiry."""
    if not isinstance(data, dict) or data.get("code") not in (0, "0"):
        return False
    for key in ("access_token", "refresh_token"):
        token = data.get(key)
        if (
            not isinstance(token, str) or not token or not token.isascii()
            or any(char.isspace() for char in token)
        ):
            return False
    expires_in = data.get("expires_in")
    return bool(
        isinstance(expires_in, int) and not isinstance(expires_in, bool)
        and expires_in > 0
        and str(data.get("token_type") or "").lower() == "bearer"
        and set(scopes.split()).issubset(str(data.get("scope") or "").split())
    )


def lark_account_matches_config(account: Any, config: Any) -> bool:
    """Never send a saved grant to a different Lark app or to Feishu."""
    from packages.core.services.provider_keys import canonical_provider_key

    profile = account.profile if isinstance(account.profile, dict) else {}
    app_id = profile.get("app_id")
    open_id = profile.get("open_id")
    return bool(
        config and canonical_provider_key(account.provider) == "lark"
        and profile.get("platform") == "lark"
        and isinstance(app_id, str) and app_id and app_id == config.client_id
        and isinstance(open_id, str) and open_id
        and account.provider_user_id == f"lark:{app_id}:user:{open_id}"
    )


async def lark_oauth_account_matches(db: AsyncSession, account: Any) -> bool:
    from packages.core.services.oauth_provider_config import resolve_oauth_config

    return lark_account_matches_config(account, await resolve_oauth_config(db, "lark"))
