"""Shared contracts for user-visible file mutations.

Keep operation names and approval identity in one place so every file tool
uses the same authorization vocabulary and one-time approvals cannot be
replayed with different content.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping


class FileMutationAction(str, Enum):
    """Canonical actions understood by file authorization and HITL."""

    WRITE = "write"
    EDIT = "edit"
    DELETE = "delete"
    CREATE_DOCUMENT = "create_document"
    CREATE_CODE_BUNDLE = "create_code_bundle"
    UPLOAD_DOCUMENT = "upload_document"
    SAVE_FILE = "save_file"
    SHELL_MODIFY = "shell_modify"

    @classmethod
    def normalize(cls, value: FileMutationAction | str | None) -> FileMutationAction:
        raw = str(getattr(value, "value", value) or "").strip().lower()
        raw = raw.replace("-", "_").replace(" ", "_")
        aliases = {
            "append": cls.EDIT,
            "modify": cls.EDIT,
            "overwrite": cls.EDIT,
            "remove": cls.DELETE,
            "update": cls.EDIT,
        }
        if raw in aliases:
            return aliases[raw]
        try:
            return cls(raw)
        except ValueError as exc:
            raise ValueError(f"Unsupported file mutation action: {raw or '<empty>'}") from exc


class FileResourceAction(str, Enum):
    """Canonical non-mutating actions used by file resource ACLs."""

    READ = "read"


class FilePermissionMode(str, Enum):
    """Canonical user preference values for AI file mutations."""

    APPROVAL = "approval"
    ALWAYS_APPROVE = "always_approve"
    DENY = "deny"


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return {
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class FileApprovalOperation:
    """The exact mutation authorized by a one-time approval."""

    tool_name: str
    action: FileMutationAction
    paths: tuple[str, ...]
    payload_fingerprint: str

    def to_record(self) -> dict[str, Any]:
        return {
            "tool": self.tool_name,
            "action": self.action.value,
            "paths": list(self.paths),
            "payload_fingerprint": self.payload_fingerprint,
        }

    def matches(self, record: Mapping[str, Any]) -> bool:
        return (
            record.get("tool") == self.tool_name
            and record.get("action") == self.action.value
            and record.get("paths") == list(self.paths)
            and record.get("payload_fingerprint") == self.payload_fingerprint
        )


class FileApprovalOperationFactory:
    """Create deterministic identities for exact file mutations."""

    @staticmethod
    def payload_fingerprint(payload: Any) -> str:
        canonical = json.dumps(
            _canonical_json_value(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        tool_name: str,
        action: FileMutationAction | str,
        paths: Iterable[str],
        payload: Any,
    ) -> FileApprovalOperation:
        return FileApprovalOperation(
            tool_name=str(tool_name or "").strip(),
            action=FileMutationAction.normalize(action),
            paths=tuple(str(path) for path in paths),
            payload_fingerprint=cls.payload_fingerprint(payload),
        )
