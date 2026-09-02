"""Public, declarative Webchat side panels. Never project raw Workspace config."""
from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator


def _public_url(value: str) -> str:
    if not value:
        return value
    if any(ord(char) < 33 for char in value) or "\\" in value:
        raise ValueError("Use a public http:// or https:// URL without whitespace")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid public URL")
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Use a public http:// or https:// URL without credentials") from exc
    return value


PublicURL = Annotated[str, Field(max_length=2048), AfterValidator(_public_url)]
Title = Annotated[str, Field(max_length=160)]


def _public_field_label(value: str) -> str:
    if value.strip() != value or any(ord(char) < 32 for char in value):
        raise ValueError("Public field labels cannot contain leading whitespace or control characters")
    normalized = "".join(char for char in value.lower() if char.isalnum())
    if any(part in normalized for part in (
        "password", "passwd", "secret", "token", "apikey", "creditcard", "cvv", "cvc",
    )):
        raise ValueError("Public forms cannot collect credentials or payment secrets")
    return value


PublicFieldLabel = Annotated[
    str,
    Field(min_length=1, max_length=80),
    AfterValidator(_public_field_label),
]

_PUBLIC_ACTION_RESERVED_FIELD_LABELS = {
    "fields",
    "trigger",
    "webchat_channel_config_id",
    "webchat_module_id",
    "webchat_session_id",
    "webchat_submission_id",
}


class PageValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModuleBase(PageValue):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    side: Literal["left", "right"]


class BrandModule(ModuleBase):
    type: Literal["brand"]
    name: Title = ""
    logo_url: PublicURL = ""
    website: PublicURL = ""


class TextModule(ModuleBase):
    type: Literal["text"]
    title: Title = ""
    body: str = Field(default="", max_length=4000)


class ImageModule(ModuleBase):
    type: Literal["image"]
    title: Title = ""
    url: PublicURL = ""
    caption: str = Field(default="", max_length=500)


class ListItem(PageValue):
    title: Title = ""
    description: str = Field(default="", max_length=1000)


class ListModule(ModuleBase):
    type: Literal["list"]
    title: Title = ""
    items: list[ListItem] = Field(default_factory=list, max_length=12)


class LinkItem(PageValue):
    label: Title = ""
    url: PublicURL = ""


class LinksModule(ModuleBase):
    type: Literal["links"]
    title: Title = ""
    items: list[LinkItem] = Field(default_factory=list, max_length=12)


class FAQItem(PageValue):
    question: Title = ""
    answer: str = Field(default="", max_length=2000)


class FAQModule(ModuleBase):
    type: Literal["faq"]
    title: Title = ""
    items: list[FAQItem] = Field(default_factory=list, max_length=12)


class FormModule(ModuleBase):
    type: Literal["form"]
    title: Title = ""
    fields: list[PublicFieldLabel] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def unique_fields(self) -> FormModule:
        if len({field.casefold() for field in self.fields}) != len(self.fields):
            raise ValueError("Public form field labels must be unique")
        return self


class ResolvedWorkspaceContent(PageValue):
    name: Title = ""
    body: str = Field(default="", max_length=4000)
    image_url: PublicURL = ""
    items: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=8)


class WorkspaceContentModule(ModuleBase):
    type: Literal["workspace_content"]
    title: Title = ""
    source: Literal["profile", "document"] = "profile"
    resource_id: str = Field(default="", max_length=80, pattern=r"^[A-Za-z0-9_-]*$")
    resolved: ResolvedWorkspaceContent | None = None


class WorkspaceActionModule(ModuleBase):
    type: Literal["workspace_action"]
    title: Title = ""
    description: str = Field(default="", max_length=1000)
    binding_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    fields: list[PublicFieldLabel] = Field(default_factory=list, max_length=6)
    submit_label: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def unique_fields(self) -> WorkspaceActionModule:
        normalized_fields = {field.casefold() for field in self.fields}
        if len(normalized_fields) != len(self.fields):
            raise ValueError("Public action field labels must be unique")
        if normalized_fields & _PUBLIC_ACTION_RESERVED_FIELD_LABELS:
            raise ValueError("Public action field labels cannot use reserved workflow names")
        return self


WebchatModule = Annotated[
    BrandModule | TextModule | ImageModule | ListModule | LinksModule | FAQModule | FormModule
    | WorkspaceContentModule | WorkspaceActionModule,
    Field(discriminator="type"),
]


class WebchatPage(PageValue):
    version: Literal[1] = 1
    modules: list[WebchatModule] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def unique_module_ids(self) -> WebchatPage:
        if len({module.id for module in self.modules}) != len(self.modules):
            raise ValueError("Module IDs must be unique")
        return self


def public_webchat_page(value: object) -> WebchatPage | None:
    """Invalid legacy/provider configuration must not break or broaden public chat."""
    if value is None:
        return None
    try:
        return WebchatPage.model_validate(value)
    except ValidationError:
        return None
