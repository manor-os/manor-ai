"""Tool descriptors and per-call context for the Manor MCP endpoint."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from starlette.requests import Request

from apps.api.mcp.manor_api import ManorApi


class ToolError(Exception):
    """A failure the model should read and act on (returned with ``isError``)."""


@dataclass(frozen=True)
class ToolImage:
    """A tool result that is an image (MCP image content) with a short caption."""

    data: str  # base64
    mime_type: str
    text: str = ""


@dataclass
class ToolContext:
    api: ManorApi
    claims: dict
    request: Request

    @property
    def user_id(self) -> str:
        return str(self.claims["sub"])

    @property
    def entity_id(self) -> str:
        return str(self.claims["entity_id"])

    def transfer_url(self, transfer_id: str) -> str:
        from apps.api.routers.oauth_metadata import public_origin

        return f"{public_origin(self.request)}/api/v1/mcp/transfers/{transfer_id}"


Handler = Callable[[ToolContext, dict], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    handler: Handler
    properties: dict = field(default_factory=dict)
    required: tuple[str, ...] = ()
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    # OAuth scopes the caller's token must carry (besides "mcp"); none for
    # Workspace-building tools.
    scopes: tuple[str, ...] = ()
    # Whether this caller has the tool at all (e.g. a feature flag). A tool it
    # lacks is neither listed nor callable, as if it did not exist.
    available: Callable[["ToolContext"], Awaitable[bool]] | None = None

    def descriptor(self) -> dict:
        schema: dict[str, Any] = {"type": "object", "properties": self.properties}
        if self.required:
            schema["required"] = list(self.required)
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent,
                "openWorldHint": self.open_world,
            },
        }


def string(description: str, **extra: Any) -> dict:
    return {"type": "string", "description": description, **extra}


def boolean(description: str, default: bool | None = None) -> dict:
    schema: dict[str, Any] = {"type": "boolean", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def obj(description: str, **extra: Any) -> dict:
    return {"type": "object", "description": description, "additionalProperties": True, **extra}


def require(args: dict, name: str) -> Any:
    value = args.get(name)
    if value in (None, ""):
        raise ToolError(f"'{name}' is required")
    return value
