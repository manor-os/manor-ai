"""Workspace relationship ledger contract.

This application-level contract records CRM-style relationship facts and
interactions for customers, investors, partners, vendors, and other contacts.
Records are immutable events; the ``rows`` projection is derived from the
latest event for each identity.  Knowledge files remain evidence and are
referenced through ``evidence_refs`` rather than copied into the ledger.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import hashlib
import ipaddress
import json
import os
import re
import socket
import unicodedata
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
import tldextract

from packages.core.ai.runtime.file_actions import (
    runtime_entity_file_root,
    runtime_sync_entity_file_to_knowledge,
)
from packages.core.models.base import generate_ulid
from packages.core.services.ledger_evidence import EvidenceReferenceError, LedgerEvidenceRef, verify_evidence_refs
from packages.core.services.tool_cache_version import bump_tool_cache_version
from packages.core.services.workspace_artifacts import (
    ensure_workspace_artifact_directory,
    resolve_workspace_artifact_directory,
)
from packages.core.services.workspace_ledger import (
    LedgerStorage,
    WorkspaceLedgerError,
    ensure_ledger_location,
    ledger_key_fingerprint,
    ledger_now_iso,
    ledger_record_files,
    write_immutable_json,
)


RELATIONSHIP_LEDGER_CONTRACT_ID = "manor.relationship_ledger/v1"
RELATIONSHIP_LEDGER_EVENT_SCHEMA_VERSION = 2
RELATIONSHIP_LEDGER_EVENT_WRITE_SCHEMA_VERSION = 1
RELATIONSHIP_LEDGER_PROJECTION_SCHEMA_VERSION = 2
DEFAULT_RELATIONSHIP_LEDGER_DIRECTORY = "relationship-ledger"
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RelationshipLedgerError(ValueError):
    """Stable application error returned by the relationship-ledger boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RelationshipSubjectType(StrEnum):
    PERSON = "person"
    ORGANIZATION = "organization"
    CUSTOMER = "customer"
    INVESTOR = "investor"
    PARTNER = "partner"
    VENDOR = "vendor"
    PROSPECT = "prospect"
    LEAD = "lead"
    OTHER = "other"


class RelationshipContactKind(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    LINKEDIN = "linkedin"
    WEBSITE = "website"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    WHATSAPP = "whatsapp"
    YOUTUBE = "youtube"
    SKOOL = "skool"
    X = "x"
    THREADS = "threads"
    BLUESKY = "bluesky"
    TELEGRAM = "telegram"
    SIGNAL = "signal"
    WECHAT = "wechat"
    LINE = "line"
    DISCORD = "discord"
    SLACK = "slack"
    GITHUB = "github"
    OTHER = "other"


class RelationshipContactStatus(StrEnum):
    """Backward-compatible summary of the independent contact states."""

    ACTIVE = "active"
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    INVALID = "invalid"
    BOUNCED = "bounced"
    UNSUBSCRIBED = "unsubscribed"
    DO_NOT_CONTACT = "do_not_contact"


class RelationshipContactVerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"


class RelationshipContactDeliverabilityStatus(StrEnum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    INVALID = "invalid"
    BOUNCED = "bounced"


class RelationshipContactConsentStatus(StrEnum):
    UNKNOWN = "unknown"
    OPTED_IN = "opted_in"
    UNSUBSCRIBED = "unsubscribed"
    DO_NOT_CONTACT = "do_not_contact"


RelationshipContactClearField = Literal[
    "label",
    "is_primary",
    "status",
    "verification_status",
    "deliverability_status",
    "consent_status",
    "verified_at",
    "source_url",
    "metadata",
]


class RelationshipContactPoint(BaseModel):
    """One typed route patch for contacting or identifying a relationship subject."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: RelationshipContactKind
    value: NonEmptyText
    label: NonEmptyText | None = None
    is_primary: bool | None = None
    status: RelationshipContactStatus | None = None
    verification_status: RelationshipContactVerificationStatus | None = None
    deliverability_status: RelationshipContactDeliverabilityStatus | None = None
    consent_status: RelationshipContactConsentStatus | None = None
    verified_at: datetime | None = None
    source_url: NonEmptyText | None = None
    metadata: dict[str, Any] | None = None
    clear_fields: list[RelationshipContactClearField] = Field(default_factory=list)


class RelationshipContactReference(BaseModel):
    """Stable reference used to remove a contact point from the current view."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: RelationshipContactKind
    value: NonEmptyText


class RelationshipLedgerEvent(BaseModel):
    """Versioned immutable relationship event stored in a Workspace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    contract_id: Literal[RELATIONSHIP_LEDGER_CONTRACT_ID] = RELATIONSHIP_LEDGER_CONTRACT_ID
    # Keep writing v1 during the reader-first phase of the v2 rollout. Once all
    # readers accepting v2 are deployed, advance the write version separately.
    schema_version: Literal[1, 2] = RELATIONSHIP_LEDGER_EVENT_WRITE_SCHEMA_VERSION
    event_id: NonEmptyText
    workspace_id: NonEmptyText
    entity_id: NonEmptyText
    entry_type: Literal["event"] = "event"
    identity_key: NonEmptyText
    identity_fingerprint: NonEmptyText
    identity_aliases: list[NonEmptyText] = Field(default_factory=list)
    identity_aliases_removed: list[NonEmptyText] = Field(default_factory=list)
    contact_points: list[RelationshipContactPoint] = Field(default_factory=list)
    contact_points_removed: list[RelationshipContactReference] = Field(default_factory=list)
    subject_type: RelationshipSubjectType | None = None
    subject_type_explicit: bool | None = None
    display_name: NonEmptyText | None = None
    relationship_type: NonEmptyText | None = None
    status: NonEmptyText | None = None
    stage: NonEmptyText | None = None
    event: NonEmptyText
    occurred_at: datetime | None = None
    recorded_at: datetime
    idempotency_key: NonEmptyText
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[LedgerEvidenceRef] = Field(default_factory=list)
    source_task_id: str | None = None
    source_workflow_run_id: str | None = None


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RelationshipLedgerError("invalid_input", f"{field} is required")
    return text


def _safe_directory(value: str | None) -> str:
    raw = str(value or DEFAULT_RELATIONSHIP_LEDGER_DIRECTORY).strip().strip("/")
    if not raw or raw in {".", ".."} or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise RelationshipLedgerError("invalid_ledger_path", "Ledger directory must be Workspace-relative")
    return raw


def _normalized_identity_alias(value: str) -> str:
    """Normalize an alias without discarding identifier-significant punctuation."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return " ".join(normalized.split())


def _relationship_identity_fingerprint(value: str) -> str:
    """Hash an identity key while preserving identifier-significant punctuation."""

    normalized = _normalized_identity_alias(value)
    if not normalized:
        raise RelationshipLedgerError(
            "invalid_input",
            "identity_key must contain searchable text",
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _identity_aliases(
    values: list[str] | tuple[str, ...] | set[str] | None,
    *,
    field: str = "identity_aliases",
) -> list[str]:
    """Return stable, case-insensitively de-duplicated relationship aliases."""

    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        raise RelationshipLedgerError("invalid_input", f"{field} must be an array")

    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _required_text(value, field)
        normalized = _normalized_identity_alias(clean)
        if not normalized:
            raise RelationshipLedgerError(
                "invalid_input",
                f"{field} must contain searchable text",
            )
        if normalized in seen:
            continue
        seen.add(normalized)
        aliases.append(clean)
    return aliases


_PHONE_CONTACT_KINDS = {RelationshipContactKind.PHONE}
_HANDLE_CONTACT_KINDS = {
    RelationshipContactKind.LINKEDIN,
    RelationshipContactKind.TIKTOK,
    RelationshipContactKind.INSTAGRAM,
    RelationshipContactKind.FACEBOOK,
    RelationshipContactKind.YOUTUBE,
    RelationshipContactKind.SKOOL,
    RelationshipContactKind.X,
    RelationshipContactKind.THREADS,
    RelationshipContactKind.BLUESKY,
    RelationshipContactKind.TELEGRAM,
    RelationshipContactKind.SIGNAL,
    RelationshipContactKind.WECHAT,
    RelationshipContactKind.LINE,
    RelationshipContactKind.DISCORD,
    RelationshipContactKind.SLACK,
    RelationshipContactKind.GITHUB,
}

_SOCIAL_HOSTS = {
    RelationshipContactKind.LINKEDIN: {"linkedin.com", "www.linkedin.com"},
    RelationshipContactKind.TIKTOK: {"tiktok.com", "www.tiktok.com"},
    RelationshipContactKind.INSTAGRAM: {"instagram.com", "www.instagram.com"},
    RelationshipContactKind.FACEBOOK: {"facebook.com", "www.facebook.com", "m.facebook.com"},
    RelationshipContactKind.YOUTUBE: {"youtube.com", "www.youtube.com", "youtu.be"},
    RelationshipContactKind.SKOOL: {"skool.com", "www.skool.com"},
    RelationshipContactKind.X: {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
    RelationshipContactKind.THREADS: {"threads.net", "www.threads.net"},
    RelationshipContactKind.BLUESKY: {"bsky.app", "www.bsky.app"},
    RelationshipContactKind.TELEGRAM: {"t.me", "telegram.me"},
    RelationshipContactKind.SIGNAL: {"signal.me"},
    RelationshipContactKind.WECHAT: {"u.wechat.com", "weixin.qq.com"},
    RelationshipContactKind.LINE: {"line.me", "lin.ee"},
    RelationshipContactKind.DISCORD: {"discord.com", "discord.gg", "www.discord.com"},
    RelationshipContactKind.SLACK: {"slack.com", "www.slack.com"},
    RelationshipContactKind.GITHUB: {"github.com", "www.github.com"},
}

_EMAIL_LOCAL_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_DOMAIN_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_CONTACT_VALUE_LENGTH = 4096
_MAX_SOCIAL_CONTACT_VALUE_LENGTH = 2048
_MAX_SOCIAL_IDENTIFIER_LENGTH = 255
_YOUTUBE_PUBLIC_SUFFIX_EXTRACTOR = tldextract.TLDExtract(
    cache_dir=None,
    suffix_list_urls=(),
)


def _normalized_domain(value: str, *, field: str) -> str:
    raw = unicodedata.normalize("NFKC", value).strip().rstrip(".")
    try:
        domain = raw.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise RelationshipLedgerError("invalid_input", f"{field} has an invalid domain") from exc
    labels = domain.split(".")
    if not domain or any(not _DOMAIN_LABEL_PATTERN.fullmatch(label) for label in labels):
        raise RelationshipLedgerError("invalid_input", f"{field} has an invalid domain")
    return domain


def _normalized_email(value: str) -> str:
    if value.count("@") != 1 or any(character.isspace() for character in value):
        raise RelationshipLedgerError("invalid_input", "email contact points must be valid email addresses")
    local, domain = value.rsplit("@", 1)
    if (
        not local
        or len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not _EMAIL_LOCAL_PATTERN.fullmatch(local)
    ):
        raise RelationshipLedgerError("invalid_input", "email contact points must be valid email addresses")
    normalized = f"{local.casefold()}@{_normalized_domain(domain, field='email')}"
    if len(normalized) > 254:
        raise RelationshipLedgerError("invalid_input", "email contact points must be valid email addresses")
    return normalized


def _normalized_website(value: str) -> str:
    raw = unicodedata.normalize("NFKC", value).strip()
    try:
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        parsed.hostname
    except ValueError as exc:
        raise RelationshipLedgerError(
            "invalid_input",
            "website contact points must contain a valid host",
        ) from exc
    if parsed.scheme and parsed.scheme.casefold() not in {"http", "https"}:
        raise RelationshipLedgerError("invalid_input", "website contact points must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise RelationshipLedgerError("invalid_input", "website contact points cannot include credentials")
    host = str(parsed.hostname or "").strip().casefold().rstrip(".")
    if not host:
        raise RelationshipLedgerError("invalid_input", "website contact points must contain a valid host")
    if host.startswith("www."):
        host = host[4:]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            if re.fullmatch(r"[0-9.]+", host):
                raise RelationshipLedgerError(
                    "invalid_input",
                    "website contact points must contain a valid public host",
                )
            if "." not in host:
                raise RelationshipLedgerError(
                    "invalid_input",
                    "website contact points must contain a valid public host",
                )
            host = _normalized_domain(host, field="website")
            if host.rsplit(".", 1)[-1].isdigit():
                raise RelationshipLedgerError(
                    "invalid_input",
                    "website contact points must contain a valid public host",
                )
            reserved_suffixes = (
                ".example",
                ".home",
                ".internal",
                ".invalid",
                ".lan",
                ".local",
                ".localdomain",
                ".localhost",
                ".test",
            )
            if host in {
                "example",
                "home",
                "internal",
                "invalid",
                "lan",
                "local",
                "localdomain",
                "localhost",
                "test",
            } or host.endswith(reserved_suffixes):
                raise RelationshipLedgerError(
                    "invalid_input",
                    "website contact points must contain a valid public host",
                )
        else:
            if not address.is_global:
                raise RelationshipLedgerError(
                    "invalid_input",
                    "website contact points must contain a valid public host",
                )
            host = address.compressed
    else:
        if not address.is_global:
            raise RelationshipLedgerError(
                "invalid_input",
                "website contact points must contain a valid public host",
            )
        host = address.compressed
        if address.version == 6:
            host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as exc:
        raise RelationshipLedgerError("invalid_input", "website contact points have an invalid port") from exc
    scheme = parsed.scheme.casefold()
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443) or (not scheme and port in {80, 443}):
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit(("", netloc, path, query, "")).removeprefix("//")


def _matching_social_host(kind: RelationshipContactKind, host: str) -> str | None:
    try:
        normalized = _normalized_domain(host, field=f"{kind.value} contact")
    except RelationshipLedgerError:
        return None
    if kind == RelationshipContactKind.SLACK:
        if normalized.endswith(".slack.com") and normalized not in {"www.slack.com"}:
            return normalized
        return None
    hosts = _SOCIAL_HOSTS.get(kind, set())
    if any(normalized == base or normalized.endswith(f".{base}") for base in hosts):
        return normalized
    return None


def _invalid_social_profile(kind: RelationshipContactKind) -> RelationshipLedgerError:
    return RelationshipLedgerError(
        "invalid_input",
        f"{kind.value} contact points must be a profile URL or account identifier",
    )


def _youtube_handle_script_family(character: str) -> str | None:
    """Return the documented YouTube length-exception family for one character."""

    if unicodedata.category(character)[0] not in {"L", "M"}:
        return None
    name = unicodedata.name(character, "")
    if "HANGUL" in name:
        return "hangul"
    if "HIRAGANA" in name:
        return "hiragana"
    if "KATAKANA" in name:
        return "katakana"
    if "ETHIOPIC" in name:
        return "ethiopic"
    if "CJK" in name or "IDEOGRAPH" in name:
        return "han"
    if unicodedata.category(character).startswith("M"):
        return None
    return "other"


def _youtube_handle_length_bounds(handle: str) -> tuple[int, int] | None:
    families = {family for character in handle if (family := _youtube_handle_script_family(character)) is not None}
    if families and families <= {"han", "hangul"}:
        return 1, 10
    if families == {"ethiopic"}:
        return 2, 20
    if families and families <= {"han", "hiragana", "katakana"}:
        return 2, 20
    if not families or families == {"other"}:
        return 3, 30
    return None


def _youtube_handle_has_invalid_text_directions(handle: str) -> bool:
    directions = {
        unicodedata.bidirectional(character) for character in handle if unicodedata.category(character).startswith("L")
    }
    has_right_to_left_text = bool(directions & {"R", "AL"})
    if "L" in directions and has_right_to_left_text:
        return True
    if not has_right_to_left_text:
        return False
    first_digit = next((index for index, character in enumerate(handle) if character.isdecimal()), None)
    return first_digit is not None and any(not character.isdecimal() for character in handle[first_digit:])


def _youtube_handle_is_phone_like(handle: str, separators: set[str]) -> bool:
    compact = "".join(character for character in handle if character not in separators)
    return bool(compact) and all(character.isdecimal() for character in compact)


def _youtube_handle_is_url_like(handle: str) -> bool:
    if "." not in handle or any(character in handle for character in {"_", "·"}):
        return False
    try:
        domain = _normalized_domain(handle, field="youtube handle")
    except RelationshipLedgerError:
        return False
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    suffix = _YOUTUBE_PUBLIC_SUFFIX_EXTRACTOR(domain).suffix
    return bool(suffix) or labels[0] == "www"


_SOCIAL_RESERVED_HANDLES = {
    RelationshipContactKind.INSTAGRAM: {"explore", "p", "reel", "reels", "stories", "tv"},
    RelationshipContactKind.FACEBOOK: {"events", "groups", "marketplace", "photo", "posts", "reel", "watch"},
    RelationshipContactKind.X: {"compose", "explore", "home", "i", "messages", "notifications", "search", "settings"},
    RelationshipContactKind.TELEGRAM: {"addstickers", "joinchat", "s", "share"},
    RelationshipContactKind.GITHUB: {
        "about",
        "collections",
        "enterprise",
        "events",
        "explore",
        "features",
        "marketplace",
        "orgs",
        "settings",
        "sponsors",
        "topics",
    },
}


def _normalized_social_handle(kind: RelationshipContactKind, value: str) -> str:
    handle = unicodedata.normalize("NFKC", value).strip()
    if handle.startswith("@"):
        handle = handle[1:]
    handle = handle.casefold()
    if not handle or len(handle) > _MAX_SOCIAL_IDENTIFIER_LENGTH or handle.startswith("@"):
        raise _invalid_social_profile(kind)
    if kind == RelationshipContactKind.X:
        if not re.fullmatch(r"[a-z0-9_]{1,15}", handle, flags=re.ASCII):
            raise _invalid_social_profile(kind)
    elif kind in {RelationshipContactKind.INSTAGRAM, RelationshipContactKind.THREADS}:
        if len(handle) > 30 or not re.fullmatch(r"[a-z0-9._]+", handle, flags=re.ASCII):
            raise _invalid_social_profile(kind)
    elif kind == RelationshipContactKind.TIKTOK:
        if len(handle) > 24 or not re.fullmatch(r"[a-z0-9._]+", handle, flags=re.ASCII):
            raise _invalid_social_profile(kind)
    elif kind == RelationshipContactKind.FACEBOOK:
        if len(handle) > 50 or not re.fullmatch(r"[a-z0-9.]+", handle, flags=re.ASCII):
            raise _invalid_social_profile(kind)
    elif kind == RelationshipContactKind.GITHUB:
        if (
            len(handle) > 39
            or not re.fullmatch(r"[a-z0-9-]+", handle, flags=re.ASCII)
            or handle.startswith("-")
            or handle.endswith("-")
            or "--" in handle
        ):
            raise _invalid_social_profile(kind)
    elif kind == RelationshipContactKind.YOUTUBE:
        length_bounds = _youtube_handle_length_bounds(handle)
        separators = {".", "_", "-", "·"}
        if (
            length_bounds is None
            or len(handle) < length_bounds[0]
            or len(handle) > length_bounds[1]
            or handle[0] in separators
            or handle[-1] in separators
            or _youtube_handle_has_invalid_text_directions(handle)
            or _youtube_handle_is_phone_like(handle, separators)
            or _youtube_handle_is_url_like(handle)
            or any(
                not (unicodedata.category(character)[0] in {"L", "M", "N"} or character in separators)
                for character in handle
            )
        ):
            raise _invalid_social_profile(kind)
    elif kind == RelationshipContactKind.TELEGRAM:
        # Basic usernames require five characters, but short collectible
        # usernames are also valid public Telegram identifiers.
        if not re.fullmatch(r"[a-z0-9_]{1,32}", handle, flags=re.ASCII):
            raise _invalid_social_profile(kind)
    elif kind == RelationshipContactKind.SIGNAL:
        match = re.fullmatch(r"([a-z_][a-z0-9_]{2,31})\.([0-9]{2,9})", handle, flags=re.ASCII)
        if match is None:
            raise _invalid_social_profile(kind)
        discriminator = match.group(2)
        if discriminator == "00" or (len(discriminator) > 2 and discriminator.startswith("0")):
            raise _invalid_social_profile(kind)
    allowed_punctuation = {".", "_", "-", "·"} if kind == RelationshipContactKind.YOUTUBE else {".", "_", "-"}
    if (
        not any(unicodedata.category(character)[0] in {"L", "N"} for character in handle)
        or handle.startswith(".")
        or handle.endswith(".")
        or ".." in handle
        or any(
            not (unicodedata.category(character)[0] in {"L", "M", "N"} or character in allowed_punctuation)
            for character in handle
        )
    ):
        raise _invalid_social_profile(kind)
    if kind == RelationshipContactKind.BLUESKY:
        try:
            domain_handle = _normalized_domain(handle, field="bluesky handle")
        except RelationshipLedgerError as exc:
            raise _invalid_social_profile(kind) from exc
        if "." not in domain_handle:
            raise _invalid_social_profile(kind)
        handle = domain_handle
    if handle in _SOCIAL_RESERVED_HANDLES.get(kind, set()):
        raise _invalid_social_profile(kind)
    return handle


def _normalized_opaque_route_token(
    kind: RelationshipContactKind,
    value: str,
    *,
    percent_encoded: bool = False,
) -> str:
    token = value
    if percent_encoded:
        if re.search(r"%(?![0-9a-fA-F]{2})", token):
            raise _invalid_social_profile(kind)
        try:
            token = unquote(token, errors="strict")
        except UnicodeDecodeError as exc:
            raise _invalid_social_profile(kind) from exc
    token = unicodedata.normalize("NFKC", token)
    if len(token) > _MAX_SOCIAL_IDENTIFIER_LENGTH or not re.fullmatch(
        r"[A-Za-z0-9._~-]+",
        token,
        flags=re.ASCII,
    ):
        raise _invalid_social_profile(kind)
    return token


def _normalized_youtube_channel_id(kind: RelationshipContactKind, value: str) -> str:
    """Validate a YouTube Channel ID without changing its opaque casing."""

    channel_id = unicodedata.normalize("NFKC", value).strip()
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id, flags=re.ASCII):
        raise _invalid_social_profile(kind)
    return channel_id


def _normalized_youtube_legacy_name(kind: RelationshipContactKind, value: str) -> str:
    """Normalize legacy /c/ and /user/ names without handle-era limits."""

    name = unicodedata.normalize("NFKC", value).strip().casefold()
    if (
        not name
        or len(name) > _MAX_SOCIAL_IDENTIFIER_LENGTH
        or name.startswith(".")
        or name.endswith(".")
        or ".." in name
        or not any(unicodedata.category(character)[0] in {"L", "N"} for character in name)
        or any(
            not (unicodedata.category(character)[0] in {"L", "M", "N"} or character in {".", "_", "-"})
            for character in name
        )
    ):
        raise _invalid_social_profile(kind)
    return name


def _normalized_social_path_parts(
    kind: RelationshipContactKind,
    path: str,
) -> list[str]:
    parts: list[str] = []
    for raw_part in (part for part in path.strip("/").split("/") if part):
        if re.search(r"%(?![0-9a-fA-F]{2})", raw_part):
            raise _invalid_social_profile(kind)
        try:
            part = unicodedata.normalize("NFKC", unquote(raw_part, errors="strict"))
        except UnicodeDecodeError as exc:
            raise _invalid_social_profile(kind) from exc
        if (
            part in {".", ".."}
            or not part
            or len(part) > _MAX_SOCIAL_IDENTIFIER_LENGTH
            or "%" in part
            or "/" in part
            or "\\" in part
            or "?" in part
            or "#" in part
            or any(character in {'"', "'", "<", ">", "`"} for character in part)
            or any(character.isspace() or unicodedata.category(character).startswith("C") for character in part)
        ):
            raise _invalid_social_profile(kind)
        parts.append(part)
    return parts


def _normalized_social_contact(kind: RelationshipContactKind, value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if len(normalized) > _MAX_SOCIAL_CONTACT_VALUE_LENGTH:
        raise _invalid_social_profile(kind)
    url_candidate = normalized
    try:
        original = urlsplit(url_candidate)
    except ValueError as exc:
        raise _invalid_social_profile(kind) from exc
    if original.scheme and original.scheme.casefold() not in {"http", "https"}:
        raise _invalid_social_profile(kind)
    url_shaped_input = bool(
        original.scheme
        or original.netloc
        or original.query
        or original.fragment
        or "/" in normalized
        or "\\" in normalized
    )
    if "://" not in url_candidate and (
        "/" in url_candidate
        or any(url_candidate.casefold().startswith(host) for host in _SOCIAL_HOSTS.get(kind, set()))
    ):
        url_candidate = f"https://{url_candidate}"
    try:
        parsed = urlsplit(url_candidate)
        parsed.hostname
        parsed.port
    except ValueError as exc:
        raise _invalid_social_profile(kind) from exc
    if parsed.scheme and parsed.scheme.casefold() not in {"http", "https"}:
        raise _invalid_social_profile(kind)
    if parsed.username or parsed.password:
        raise _invalid_social_profile(kind)
    port = parsed.port
    if port is not None and not (
        (parsed.scheme.casefold() == "http" and port == 80) or (parsed.scheme.casefold() == "https" and port == 443)
    ):
        raise _invalid_social_profile(kind)
    host = _matching_social_host(kind, str(parsed.hostname or "").casefold())
    if host is not None:
        parts = _normalized_social_path_parts(kind, parsed.path)
        lowered = [part.casefold() for part in parts]
        if kind == RelationshipContactKind.LINKEDIN:
            if len(parts) == 2 and lowered[0] in {"in", "company", "school"}:
                return f"{lowered[0]}/{_normalized_social_handle(kind, parts[1])}"
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.TIKTOK:
            if len(parts) == 1 and parts[0].startswith("@"):
                return _normalized_social_handle(kind, parts[0])
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.INSTAGRAM:
            if len(parts) == 1:
                return _normalized_social_handle(kind, parts[0])
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.FACEBOOK and lowered == ["profile.php"]:
            query_items = parse_qsl(parsed.query, keep_blank_values=True)
            if len(query_items) == 1 and query_items[0][0] == "id":
                profile_id = query_items[0][1].strip()
                if re.fullmatch(r"[0-9]+", profile_id, flags=re.ASCII):
                    return f"id/{profile_id}"
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.FACEBOOK:
            if len(parts) == 1:
                return _normalized_social_handle(kind, parts[0])
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.YOUTUBE:
            if host == "youtu.be":
                raise _invalid_social_profile(kind)
            if len(parts) == 1 and parts[0].startswith("@"):
                return _normalized_social_handle(kind, parts[0])
            if len(parts) == 2 and lowered[0] == "channel":
                return f"channel/{_normalized_youtube_channel_id(kind, parts[1])}"
            if len(parts) == 2 and lowered[0] in {"c", "user"}:
                return f"{lowered[0]}/{_normalized_youtube_legacy_name(kind, parts[1])}"
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.SKOOL:
            if len(parts) == 1:
                return _normalized_social_handle(kind, parts[0])
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.X:
            if len(parts) == 1:
                return _normalized_social_handle(kind, parts[0])
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.THREADS:
            if len(parts) == 1 and parts[0].startswith("@"):
                return _normalized_social_handle(kind, parts[0])
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.BLUESKY:
            if len(parts) == 2 and lowered[0] == "profile":
                return _normalized_social_handle(kind, parts[1])
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.TELEGRAM:
            if len(parts) == 1:
                if parts[0].startswith("+"):
                    phone = _normalized_phone_contact(kind, parts[0])
                    return f"phone/{phone}"
                return _normalized_social_handle(kind, parts[0])
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.SIGNAL:
            if host not in {"signal.me", "www.signal.me"} or parts or parsed.query:
                raise _invalid_social_profile(kind)
            if parsed.fragment.startswith("p/"):
                return _normalized_phone_contact(kind, parsed.fragment.removeprefix("p/"))
            if parsed.fragment.startswith("eu/"):
                token = _normalized_opaque_route_token(
                    kind,
                    parsed.fragment.removeprefix("eu/"),
                    percent_encoded=True,
                )
                return f"url/signal.me#eu/{token}"
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.WECHAT:
            if host == "u.wechat.com" and len(parts) == 1 and not parsed.query and not parsed.fragment:
                token = _normalized_opaque_route_token(kind, parts[0])
                return f"url/{host}/{token}"
            if (
                host in {"weixin.qq.com", "www.weixin.qq.com"}
                and len(parts) == 2
                and lowered[0] == "r"
                and not parsed.query
                and not parsed.fragment
            ):
                token = _normalized_opaque_route_token(kind, parts[1])
                return f"url/{host}/r/{token}"
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.LINE:
            if host in {"lin.ee", "www.lin.ee"} and len(parts) == 1 and not parsed.query and not parsed.fragment:
                token = _normalized_opaque_route_token(kind, parts[0])
                return f"url/{host}/{token}"
            direct_profile = (len(parts) == 3 and lowered[:2] == ["ti", "p"]) or (
                len(parts) == 4 and lowered[:3] == ["r", "ti", "p"]
            )
            if host in {"line.me", "www.line.me"} and direct_profile and not parsed.query and not parsed.fragment:
                raw_profile = parts[-1]
                has_at_prefix = raw_profile.startswith("@")
                profile = _normalized_opaque_route_token(kind, raw_profile.removeprefix("@"))
                route_parts = [part.casefold() for part in parts[:-1]]
                route_parts.append(f"@{profile}" if has_at_prefix else profile)
                route = "/".join(route_parts)
                return f"url/{host}/{route}"
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.GITHUB:
            if len(parts) == 1:
                return _normalized_social_handle(kind, parts[0])
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.DISCORD:
            if host == "discord.com" and len(parts) == 2 and lowered[0] == "users":
                return f"user/{_normalized_social_handle(kind, parts[1])}"
            if host == "discord.gg" and len(parts) == 1:
                return f"invite/{_normalized_social_handle(kind, parts[0])}"
            raise _invalid_social_profile(kind)
        if kind == RelationshipContactKind.SLACK:
            if host.endswith(".slack.com") and not parts:
                return host.removesuffix(".slack.com")
            raise _invalid_social_profile(kind)
        raise _invalid_social_profile(kind)
    if url_shaped_input:
        raise _invalid_social_profile(kind)
    if kind == RelationshipContactKind.TELEGRAM and normalized.startswith("+"):
        phone = _normalized_phone_contact(kind, normalized)
        return f"phone/{phone}"
    handle = _normalized_social_handle(kind, normalized)
    if kind == RelationshipContactKind.LINKEDIN:
        return f"in/{handle}"
    return handle


def _normalized_phone_contact(kind: RelationshipContactKind, value: str) -> str:
    if not re.fullmatch(r"\+?[0-9\s().-]+", value):
        raise RelationshipLedgerError(
            "invalid_input",
            f"{kind.value} contact points must be valid phone numbers",
        )
    digits = "".join(character for character in value if character.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) < 7 or len(digits) > 15:
        raise RelationshipLedgerError(
            "invalid_input",
            f"{kind.value} contact points must contain 7 to 15 digits",
        )
    return digits


def _normalized_whatsapp_contact(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    url_candidate = normalized if "://" in normalized else f"https://{normalized}"
    try:
        parsed = urlsplit(url_candidate)
        parsed.hostname
        parsed.port
    except ValueError as exc:
        raise RelationshipLedgerError(
            "invalid_input",
            "whatsapp contact points must be a phone number or direct WhatsApp contact link",
        ) from exc
    if parsed.scheme.casefold() not in {"http", "https"} or parsed.username or parsed.password:
        raise RelationshipLedgerError(
            "invalid_input",
            "whatsapp contact points must be a phone number or direct WhatsApp contact link",
        )
    port = parsed.port
    if port is not None and not (
        (parsed.scheme.casefold() == "http" and port == 80) or (parsed.scheme.casefold() == "https" and port == 443)
    ):
        raise RelationshipLedgerError(
            "invalid_input",
            "whatsapp contact points must be a phone number or direct WhatsApp contact link",
        )
    host = str(parsed.hostname or "").casefold()
    if host in {"wa.me", "api.whatsapp.com", "whatsapp.com", "www.whatsapp.com"}:
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        number = ""
        if host == "wa.me" and len(parts) == 1:
            number = parts[0]
        elif parts and parts[-1].casefold() == "send":
            number = str(query.get("phone") or "")
        if number:
            return _normalized_phone_contact(RelationshipContactKind.WHATSAPP, number)
        raise RelationshipLedgerError(
            "invalid_input",
            "whatsapp contact points must be a phone number or direct WhatsApp contact link",
        )
    return _normalized_phone_contact(RelationshipContactKind.WHATSAPP, normalized)


def _normalized_contact_value(kind: RelationshipContactKind | str, value: str) -> str:
    contact_kind = RelationshipContactKind(str(getattr(kind, "value", kind)).strip().casefold())
    clean = _required_text(value, "contact point value")
    normalized = unicodedata.normalize("NFKC", clean).strip()
    if len(normalized) > _MAX_CONTACT_VALUE_LENGTH:
        raise RelationshipLedgerError(
            "invalid_input",
            f"{contact_kind.value} contact points must not exceed {_MAX_CONTACT_VALUE_LENGTH} characters",
        )
    if contact_kind == RelationshipContactKind.EMAIL:
        return _normalized_email(normalized)
    if contact_kind in _PHONE_CONTACT_KINDS:
        return _normalized_phone_contact(contact_kind, normalized)
    if contact_kind == RelationshipContactKind.WHATSAPP:
        return _normalized_whatsapp_contact(normalized)
    if contact_kind == RelationshipContactKind.SIGNAL and re.fullmatch(
        r"\+?[0-9\s().-]+",
        normalized,
    ):
        return _normalized_phone_contact(contact_kind, normalized)
    if contact_kind == RelationshipContactKind.WEBSITE:
        return _normalized_website(normalized)
    if contact_kind in _HANDLE_CONTACT_KINDS:
        return _normalized_social_contact(contact_kind, normalized)
    return _normalized_identity_alias(normalized)


def _contact_key(kind: RelationshipContactKind | str, value: str) -> str:
    contact_kind = RelationshipContactKind(str(getattr(kind, "value", kind)).strip().casefold())
    return f"{contact_kind.value}:{_normalized_contact_value(contact_kind, value)}"


def _projection_contact_key(kind: RelationshipContactKind | str, value: str) -> str:
    """Return a stable key while tolerating values committed by older writers."""

    return _projection_contact_identity(kind, value)[0]


def _projection_contact_identity(
    kind: RelationshipContactKind | str,
    value: str,
) -> tuple[str, bool]:
    """Return the projection key and whether the stored value passes current validation."""

    contact_kind = RelationshipContactKind(str(getattr(kind, "value", kind)).strip().casefold())
    try:
        return _contact_key(contact_kind, value), True
    except (RelationshipLedgerError, ValueError):
        legacy_value = _normalized_identity_alias(_required_text(value, "contact point value"))
        return f"{contact_kind.value}:{legacy_value}", False


_CONTACT_DEFAULTS: dict[str, Any] = {
    "label": None,
    "is_primary": False,
    "verification_status": RelationshipContactVerificationStatus.UNVERIFIED.value,
    "deliverability_status": RelationshipContactDeliverabilityStatus.UNKNOWN.value,
    "consent_status": RelationshipContactConsentStatus.UNKNOWN.value,
    "verified_at": None,
    "source_url": None,
    "metadata": {},
    "validation_status": "valid",
}


def _legacy_contact_status_patch(status: RelationshipContactStatus) -> dict[str, str]:
    if status == RelationshipContactStatus.VERIFIED:
        return {"verification_status": RelationshipContactVerificationStatus.VERIFIED.value}
    if status == RelationshipContactStatus.UNVERIFIED:
        return {"verification_status": RelationshipContactVerificationStatus.UNVERIFIED.value}
    if status == RelationshipContactStatus.ACTIVE:
        return {"deliverability_status": RelationshipContactDeliverabilityStatus.ACTIVE.value}
    if status == RelationshipContactStatus.INVALID:
        return {"deliverability_status": RelationshipContactDeliverabilityStatus.INVALID.value}
    if status == RelationshipContactStatus.BOUNCED:
        return {"deliverability_status": RelationshipContactDeliverabilityStatus.BOUNCED.value}
    if status == RelationshipContactStatus.UNSUBSCRIBED:
        return {"consent_status": RelationshipContactConsentStatus.UNSUBSCRIBED.value}
    return {"consent_status": RelationshipContactConsentStatus.DO_NOT_CONTACT.value}


def _contact_summary_status(point: dict[str, Any]) -> str:
    consent = str(point.get("consent_status") or "")
    deliverability = str(point.get("deliverability_status") or "")
    verification = str(point.get("verification_status") or "")
    if consent == RelationshipContactConsentStatus.DO_NOT_CONTACT.value:
        return RelationshipContactStatus.DO_NOT_CONTACT.value
    if consent == RelationshipContactConsentStatus.UNSUBSCRIBED.value:
        return RelationshipContactStatus.UNSUBSCRIBED.value
    if deliverability == RelationshipContactDeliverabilityStatus.BOUNCED.value:
        return RelationshipContactStatus.BOUNCED.value
    if deliverability == RelationshipContactDeliverabilityStatus.INVALID.value:
        return RelationshipContactStatus.INVALID.value
    if verification == RelationshipContactVerificationStatus.VERIFIED.value:
        return RelationshipContactStatus.VERIFIED.value
    if deliverability == RelationshipContactDeliverabilityStatus.ACTIVE.value:
        return RelationshipContactStatus.ACTIVE.value
    return RelationshipContactStatus.UNVERIFIED.value


def _project_contact_patch(
    existing: dict[str, Any] | None,
    point: RelationshipContactPoint,
    *,
    contact_key: str,
) -> dict[str, Any]:
    projected = {
        **_CONTACT_DEFAULTS,
        "metadata": {},
        **(existing or {}),
        "kind": point.kind.value,
        "value": point.value,
        "contact_key": contact_key,
    }
    for field in point.clear_fields:
        if field == "status":
            projected.update(
                {
                    "verification_status": RelationshipContactVerificationStatus.UNVERIFIED.value,
                    "deliverability_status": RelationshipContactDeliverabilityStatus.UNKNOWN.value,
                    "consent_status": RelationshipContactConsentStatus.UNKNOWN.value,
                }
            )
        elif field in _CONTACT_DEFAULTS:
            default = _CONTACT_DEFAULTS[field]
            projected[field] = dict(default) if isinstance(default, dict) else default
    if point.status is not None:
        projected.update(_legacy_contact_status_patch(point.status))
    patch = point.model_dump(
        mode="json",
        exclude={"kind", "value", "status", "clear_fields"},
        exclude_none=True,
    )
    projected.update(patch)
    projected["status"] = _contact_summary_status(projected)
    return projected


def _contact_points(
    values: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    field: str,
    model: type[RelationshipContactPoint] | type[RelationshipContactReference],
) -> list[RelationshipContactPoint] | list[RelationshipContactReference]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise RelationshipLedgerError("invalid_input", f"{field} must be an array")
    parsed = [model.model_validate(value) for value in values]
    if model is RelationshipContactPoint:
        for point in parsed:
            _contact_key(point.kind, point.value)
        return parsed
    seen: set[str] = set()
    result: list[RelationshipContactPoint] | list[RelationshipContactReference] = []
    for point in parsed:
        key = _projection_contact_key(point.kind, point.value)
        if key in seen:
            continue
        seen.add(key)
        result.append(point)
    return result


def _parse_timestamp(value: datetime | str | None, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RelationshipLedgerError("invalid_input", f"{field} must be an ISO timestamp") from exc


async def _ledger_location(
    *,
    entity_id: str,
    workspace_id: str,
    directory: str,
    create: bool = True,
) -> tuple[Any, str, str]:
    try:
        return await ensure_ledger_location(
            entity_id=entity_id,
            workspace_id=workspace_id,
            storage=LedgerStorage(directory=_safe_directory(directory)),
            directory_resolver=(
                ensure_workspace_artifact_directory if create else resolve_workspace_artifact_directory
            ),
            entity_root_resolver=runtime_entity_file_root,
            create=create,
        )
    except WorkspaceLedgerError as exc:
        raise RelationshipLedgerError(exc.code, str(exc)) from exc
    except RelationshipLedgerError:
        raise
    except ValueError as exc:
        raise RelationshipLedgerError("ledger_not_installed", str(exc)) from exc


async def _sync_ledger_projection(
    *,
    entity_id: str,
    workspace_id: str,
    abs_path: str,
    entity_root: str,
    agent_id: str | None,
    task_id: str | None,
    conversation_id: str | None,
    user_id: str | None,
) -> Any:
    await bump_tool_cache_version(entity_id, "ledgers")
    projection = await runtime_sync_entity_file_to_knowledge(
        entity_id=entity_id,
        abs_path=abs_path,
        entity_root=entity_root,
        source="ai_generated",
        created_by=agent_id or "relationship_ledger",
        force=True,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name="record_relationship_ledger",
    )
    if not bool(getattr(projection, "synced", False)):
        raise RelationshipLedgerError(
            "knowledge_sync_failed",
            "Relationship ledger record committed but Knowledge projection failed",
        )
    return projection


def _parse_event(path: str, *, workspace_id: str, entity_id: str) -> RelationshipLedgerEvent:
    try:
        with open(path, encoding="utf-8") as source:
            event = RelationshipLedgerEvent.model_validate(json.load(source))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RelationshipLedgerError(
            "ledger_schema_invalid",
            f"Relationship ledger record is invalid: {os.path.basename(path)}",
        ) from exc
    if event.workspace_id != workspace_id or event.entity_id != entity_id:
        raise RelationshipLedgerError(
            "ledger_scope_invalid",
            "Relationship ledger record belongs to another Workspace",
        )
    return event


def _current_rows(events: list[RelationshipLedgerEvent]) -> list[dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=lambda item: item.recorded_at):
        key = _relationship_identity_fingerprint(event.identity_key)
        row = current.setdefault(
            key,
            {
                "identity_key": event.identity_key,
                "identity_fingerprint": key,
                "identity_aliases": [],
                "contact_points": [],
                "contact_keys": [],
                "identity_conflicts": [],
                "workspace_id": event.workspace_id,
                "entity_id": event.entity_id,
                "subject_type": event.subject_type or RelationshipSubjectType.PERSON,
                "display_name": event.display_name,
                "relationship_type": event.relationship_type,
                "status": event.status,
                "stage": event.stage,
                "payload": {},
                "evidence_refs": [],
                "source_task_id": event.source_task_id,
                "source_workflow_run_id": event.source_workflow_run_id,
            },
        )
        for field in (
            "subject_type",
            "display_name",
            "relationship_type",
            "status",
            "stage",
            "source_task_id",
            "source_workflow_run_id",
        ):
            value = getattr(event, field)
            if value:
                row[field] = value
        aliases_by_key = {_normalized_identity_alias(value): value for value in row["identity_aliases"]}
        for alias in event.identity_aliases_removed:
            aliases_by_key.pop(_normalized_identity_alias(alias), None)
        for alias in event.identity_aliases:
            aliases_by_key.setdefault(_normalized_identity_alias(alias), alias)
        row["identity_aliases"] = list(aliases_by_key.values())

        contacts_by_key = {str(point["contact_key"]): point for point in row["contact_points"]}
        for point in event.contact_points_removed:
            contacts_by_key.pop(_projection_contact_key(point.kind, point.value), None)
        for point in event.contact_points:
            key, is_valid = _projection_contact_identity(point.kind, point.value)
            if point.is_primary and is_valid:
                for current_point in contacts_by_key.values():
                    if current_point["kind"] == point.kind.value:
                        current_point["is_primary"] = False
            projected = _project_contact_patch(
                contacts_by_key.get(key),
                point,
                contact_key=key,
            )
            projected["validation_status"] = "valid" if is_valid else "legacy_invalid"
            if not is_valid:
                projected["is_primary"] = False
                projected["verification_status"] = RelationshipContactVerificationStatus.UNVERIFIED.value
                projected["deliverability_status"] = RelationshipContactDeliverabilityStatus.INVALID.value
                projected["status"] = _contact_summary_status(projected)
            contacts_by_key[key] = projected
        row["contact_points"] = list(contacts_by_key.values())
        row["contact_keys"] = [
            key for key, point in contacts_by_key.items() if point.get("validation_status") == "valid"
        ]
        row["payload"].update(event.payload)
        if event.evidence_refs:
            row["evidence_refs"] = [ref.model_dump(mode="json") for ref in event.evidence_refs]
        row["event_id"] = event.event_id
        row["event"] = event.event
        row["recorded_at"] = event.recorded_at.isoformat()
        row["occurred_at"] = event.occurred_at.isoformat() if event.occurred_at else None
    return sorted(current.values(), key=lambda item: str(item.get("recorded_at") or ""), reverse=True)


def _identity_conflicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    owners: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        identity_key = str(row.get("identity_key") or "")
        for alias in row.get("identity_aliases") or []:
            owners.setdefault(("alias", _normalized_identity_alias(alias)), set()).add(identity_key)
        for contact_key in row.get("contact_keys") or []:
            owners.setdefault(("contact", str(contact_key)), set()).add(identity_key)

    conflicts = [
        {
            "identifier_type": identifier_type,
            "identifier_key": identifier_key,
            "identity_keys": sorted(identity_keys),
        }
        for (identifier_type, identifier_key), identity_keys in owners.items()
        if len(identity_keys) > 1
    ]
    conflicts.sort(key=lambda item: (item["identifier_type"], item["identifier_key"]))
    conflicts_by_identity: dict[str, list[dict[str, Any]]] = {}
    for conflict in conflicts:
        for identity_key in conflict["identity_keys"]:
            conflicts_by_identity.setdefault(identity_key, []).append(conflict)
    for row in rows:
        row["identity_conflicts"] = conflicts_by_identity.get(str(row.get("identity_key") or ""), [])
    return conflicts


async def read_relationship_ledger(
    *,
    entity_id: str,
    workspace_id: str,
    directory: str = DEFAULT_RELATIONSHIP_LEDGER_DIRECTORY,
    recent_limit: int | None = 100,
    create_location: bool = True,
) -> dict[str, Any]:
    """Read the current relationship projection and immutable interaction events."""

    _, ledger_root, _ = await _ledger_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory=directory,
        create=create_location,
    )
    events = [
        _parse_event(path, workspace_id=workspace_id, entity_id=entity_id)
        for path in ledger_record_files(ledger_root)
        if os.path.basename(os.path.dirname(path)) == "events"
    ]
    events.sort(key=lambda item: item.recorded_at)
    if recent_limit is None or int(recent_limit) <= 0:
        visible_events = events
    else:
        limit = max(1, min(int(recent_limit), 500))
        visible_events = events[-limit:]
    rows = _current_rows(events)
    identity_conflicts = _identity_conflicts(rows)
    return {
        "contract_id": RELATIONSHIP_LEDGER_CONTRACT_ID,
        "schema_version": RELATIONSHIP_LEDGER_PROJECTION_SCHEMA_VERSION,
        "entry_count": len(events),
        "relationship_count": len(rows),
        "identity_conflict_count": len(identity_conflicts),
        "identity_conflicts": identity_conflicts,
        "rows": rows,
        "entries": [event.model_dump(mode="json") for event in visible_events],
        "recent_entries": [event.model_dump(mode="json") for event in visible_events],
        "run_key": generate_ulid(),
    }


def _idempotency_payload(event: RelationshipLedgerEvent) -> dict[str, Any]:
    payload = event.model_dump(
        mode="json",
        exclude={"event_id", "identity_fingerprint", "recorded_at"},
    )
    for ref in payload.get("evidence_refs") or []:
        ref.pop("verified_at", None)
    return payload


def _idempotency_payloads_match(
    prior: RelationshipLedgerEvent,
    current: RelationshipLedgerEvent,
) -> bool:
    prior_payload = _idempotency_payload(prior)
    current_payload = _idempotency_payload(current)
    if prior_payload == current_payload:
        return True

    # A schema upgrade does not change the logical request represented by an
    # idempotency key. This must work in both directions while the reader-first
    # rollout still writes v1 and may encounter an already-written v2 event.
    prior_payload.pop("schema_version", None)
    current_payload.pop("schema_version", None)
    if prior_payload == current_payload:
        return True
    if prior.schema_version != 1:
        return False
    if prior.subject_type_explicit is not None:
        return False

    # Events written before subject_type_explicit existed treated omission as
    # person. Preserve replay compatibility only for those legacy records.
    prior_payload.pop("subject_type_explicit", None)
    current_payload.pop("subject_type_explicit", None)
    prior_payload["subject_type"] = str(prior_payload.get("subject_type") or RelationshipSubjectType.PERSON.value)
    current_payload["subject_type"] = str(current_payload.get("subject_type") or RelationshipSubjectType.PERSON.value)
    return prior_payload == current_payload


async def record_relationship_event(
    *,
    entity_id: str,
    workspace_id: str,
    identity_key: str,
    event: str,
    idempotency_key: str,
    identity_aliases: list[str] | tuple[str, ...] | set[str] | None = None,
    identity_aliases_removed: list[str] | tuple[str, ...] | set[str] | None = None,
    contact_points: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    contact_points_removed: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    subject_type: str | None = None,
    display_name: str | None = None,
    relationship_type: str | None = None,
    status: str | None = None,
    stage: str | None = None,
    occurred_at: datetime | str | None = None,
    payload: dict[str, Any] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    directory: str = DEFAULT_RELATIONSHIP_LEDGER_DIRECTORY,
    agent_id: str | None = None,
    task_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    workflow_run_id: str | None = None,
) -> dict[str, Any]:
    """Append one relationship event, idempotent by caller-provided key."""

    clean_entity = _required_text(entity_id, "entity_id")
    clean_workspace = _required_text(workspace_id, "workspace_id")
    clean_identity = _required_text(identity_key, "identity_key")
    clean_event = _required_text(event, "event")
    clean_idempotency = _required_text(idempotency_key, "idempotency_key")
    try:
        normalized_evidence_refs = await verify_evidence_refs(
            evidence_refs,
            entity_id=clean_entity,
            workspace_id=clean_workspace,
        )
        entry = RelationshipLedgerEvent(
            event_id=generate_ulid(),
            workspace_id=clean_workspace,
            entity_id=clean_entity,
            identity_key=clean_identity,
            identity_fingerprint=_relationship_identity_fingerprint(clean_identity),
            identity_aliases=_identity_aliases(identity_aliases),
            identity_aliases_removed=_identity_aliases(
                identity_aliases_removed,
                field="identity_aliases_removed",
            ),
            contact_points=_contact_points(
                contact_points,
                field="contact_points",
                model=RelationshipContactPoint,
            ),
            contact_points_removed=_contact_points(
                contact_points_removed,
                field="contact_points_removed",
                model=RelationshipContactReference,
            ),
            subject_type=(
                RelationshipSubjectType(str(subject_type).strip().casefold())
                if subject_type is not None and str(subject_type).strip()
                else None
            ),
            subject_type_explicit=bool(subject_type is not None and str(subject_type).strip()),
            display_name=display_name,
            relationship_type=relationship_type,
            status=status,
            stage=stage,
            event=clean_event,
            occurred_at=_parse_timestamp(occurred_at, "occurred_at"),
            recorded_at=datetime.fromisoformat(ledger_now_iso()),
            idempotency_key=clean_idempotency,
            payload=payload or {},
            evidence_refs=normalized_evidence_refs,
            source_task_id=task_id,
            source_workflow_run_id=workflow_run_id,
        )
    except EvidenceReferenceError as exc:
        raise RelationshipLedgerError(exc.code, str(exc)) from exc
    except WorkspaceLedgerError as exc:
        raise RelationshipLedgerError(exc.code, str(exc)) from exc
    except RelationshipLedgerError:
        raise
    except ValueError as exc:
        raise RelationshipLedgerError("ledger_schema_invalid", str(exc)) from exc

    directory_record, ledger_root, entity_root = await _ledger_location(
        entity_id=clean_entity,
        workspace_id=clean_workspace,
        directory=directory,
    )
    filename = f"relationship-{ledger_key_fingerprint(clean_idempotency)}.json"
    relative_name = f"events/{filename}"
    abs_path = os.path.join(ledger_root, relative_name)
    existing = await read_relationship_ledger(
        entity_id=clean_entity,
        workspace_id=clean_workspace,
        directory=directory,
        recent_limit=0,
    )
    for raw in existing["entries"]:
        if raw.get("idempotency_key") != clean_idempotency:
            continue
        prior = RelationshipLedgerEvent.model_validate(raw)
        if _idempotency_payloads_match(prior, entry):
            projection = None
            if os.path.isfile(abs_path):
                projection = await _sync_ledger_projection(
                    entity_id=clean_entity,
                    workspace_id=clean_workspace,
                    abs_path=abs_path,
                    entity_root=entity_root,
                    agent_id=agent_id,
                    task_id=task_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                )
            return {
                "ok": True,
                "idempotent": True,
                "event_id": prior.event_id,
                "status": prior.status,
                "document_id": getattr(projection, "document_id", None),
            }
        raise RelationshipLedgerError(
            "idempotency_conflict",
            "The idempotency_key already exists with different relationship data",
        )

    created = write_immutable_json(abs_path, entry.model_dump(mode="json"))
    if not created:
        existing_entry = _parse_event(abs_path, workspace_id=clean_workspace, entity_id=clean_entity)
        if existing_entry.idempotency_key == clean_idempotency:
            if not _idempotency_payloads_match(existing_entry, entry):
                raise RelationshipLedgerError(
                    "idempotency_conflict",
                    "The idempotency_key already exists with different relationship data",
                )
            projection = await _sync_ledger_projection(
                entity_id=clean_entity,
                workspace_id=clean_workspace,
                abs_path=abs_path,
                entity_root=entity_root,
                agent_id=agent_id,
                task_id=task_id,
                conversation_id=conversation_id,
                user_id=user_id,
            )
            return {
                "ok": True,
                "idempotent": True,
                "event_id": existing_entry.event_id,
                "status": existing_entry.status,
                "document_id": getattr(projection, "document_id", None),
            }
        raise RelationshipLedgerError("idempotency_conflict", "The relationship record path is already occupied")

    projection = await _sync_ledger_projection(
        entity_id=clean_entity,
        workspace_id=clean_workspace,
        abs_path=abs_path,
        entity_root=entity_root,
        agent_id=agent_id,
        task_id=task_id,
        conversation_id=conversation_id,
        user_id=user_id,
    )
    return {
        "ok": True,
        "idempotent": False,
        "event_id": entry.event_id,
        "status": entry.status,
        "path": f"{directory_record.storage_path}/{relative_name}",
        "display_path": f"{directory_record.display_path}/{relative_name}",
        "document_id": getattr(projection, "document_id", None),
    }


__all__ = [
    "DEFAULT_RELATIONSHIP_LEDGER_DIRECTORY",
    "RELATIONSHIP_LEDGER_CONTRACT_ID",
    "RELATIONSHIP_LEDGER_EVENT_SCHEMA_VERSION",
    "RELATIONSHIP_LEDGER_EVENT_WRITE_SCHEMA_VERSION",
    "RELATIONSHIP_LEDGER_PROJECTION_SCHEMA_VERSION",
    "RelationshipContactConsentStatus",
    "RelationshipContactDeliverabilityStatus",
    "RelationshipContactKind",
    "RelationshipContactPoint",
    "RelationshipContactReference",
    "RelationshipContactStatus",
    "RelationshipContactVerificationStatus",
    "RelationshipLedgerError",
    "RelationshipLedgerEvent",
    "RelationshipSubjectType",
    "read_relationship_ledger",
    "record_relationship_event",
]
