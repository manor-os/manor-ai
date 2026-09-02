"""Public Robinhood Trading MCP endpoints and client registration payload.

Source: https://agent.robinhood.com/.well-known/oauth-authorization-server/mcp/trading
Registration creates an application identifier, never an account grant.
"""
from urllib.parse import urlsplit


ROBINHOOD_MCP_ENDPOINT = "https://agent.robinhood.com/mcp/trading"
ROBINHOOD_REGISTRATION_ENDPOINT = "https://agent.robinhood.com/oauth/trading/register"
ROBINHOOD_CALLBACK_PATH = "/api/v1/integrations/oauth/robinhood/callback"


def robinhood_registration_payload(app_url: str) -> dict:
    """Build a public PKCE client for the deployment's existing callback."""
    base = app_url.strip().rstrip("/")
    parsed = urlsplit(base)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path
        or not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback))
    ):
        raise ValueError("APP_URL must be an HTTPS origin or an HTTP loopback origin")
    return {
        "client_name": "Manor AI",
        "redirect_uris": [f"{base}{ROBINHOOD_CALLBACK_PATH}"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "internal",
    }
