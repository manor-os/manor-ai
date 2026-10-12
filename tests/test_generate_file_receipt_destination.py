"""The public generate_file receipt and the inner write share one destination.

Staging 2026-10-06 (global owner chat, no Workspace/Task): converting an
uploaded PNG with ``generate_file kind=image source=...`` minted a runtime
receipt for ``IMG_9973_converted.png`` while the handler wrote
``images/IMG_9973_converted.png``. The inner guard denied the write with
``authorization_receipt_mismatch`` and the model told the user it was a
permission problem they could "allow".

These tests drive the real Runtime boundary (classifier -> receipt -> handler
-> materializer -> guard) against a real database and filesystem.
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image

from packages.core.ai.runtime import (
    AIRuntimeRequest,
    ChatSurface,
    RuntimeResolver,
    runtime_execute_registered_tool,
)
from packages.core.ai.runtime.authorization_receipts import (
    runtime_current_tool_authorization_receipt,
)
from packages.core.ai.tools.generate_file import tool as generate_file_tool
from packages.core.ai.tools.generate_file.schema import GENERATE_FILE_SCHEMA
from packages.core.ai.tools.generate_file.tool import _generate_file_handler
from packages.core.contracts.audio_generation import GenerateFileKind
from packages.core.contracts.file_engine import (
    DELIMITED_FILE_TYPES,
    FILE_CONVERSION_TARGETS,
    JSON_FILE_TYPES,
    OPERATION_GENERATION_TYPES,
)
from packages.core.governance.approval_policy_service import (
    account_defaults,
    initialize_personal_approval,
)
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.services import ai_file_permissions
from tests.test_chat_attachment_read_access import attachment_fs  # noqa: F401
from tests.test_office_operation_generation import office_operations


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 8), (240, 20, 40)).save(output, format="PNG")
    return output.getvalue()


@dataclass
class _Context:
    surface: ChatSurface
    entity_id: str
    user_id: str | None
    conversation_id: str | None
    root: Path
    workspace_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    guard_calls: list[dict] = field(default_factory=list)

    def envelope(self):
        return RuntimeResolver().resolve_trace_envelope(
            AIRuntimeRequest(
                surface=self.surface,
                entity_id=self.entity_id,
                user_id=self.user_id,
                agent_id=self.agent_id,
                workspace_id=self.workspace_id,
                task_id=self.task_id,
                conversation_id=self.conversation_id,
            ),
            tool_schemas=[{"type": "function", "function": {"name": "generate_file"}}],
            allowed_tool_names={"generate_file"},
        )

    async def call(self, arguments: dict) -> dict:
        result = await runtime_execute_registered_tool(
            tool_name="generate_file",
            arguments={
                **arguments,
                "_runtime_run_id_from_context": generate_ulid(),
                "_runtime_tool_call_id_from_context": "call_1",
            },
            handler_resolver=lambda _name: _generate_file_handler,
            entity_id=self.entity_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            workspace_id=self.workspace_id,
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            runtime_envelope=self.envelope(),
        )
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            payload = {"raw": str(result)}
        return payload if isinstance(payload, dict) else {"raw": payload}


async def _seed(db, root, context_name: str) -> _Context:
    entity_id, user_id, conversation_id = (generate_ulid() for _ in range(3))
    db.add(Entity(id=entity_id, name="Receipt test", settings={"approval_defaults": account_defaults()}))
    db.add(User(id=user_id, entity_id=entity_id, email=f"{user_id}@test.com",
                password_hash="unused", role="owner", status="active"))
    db.add(UserMembership(user_id=user_id, entity_id=entity_id, role="owner",
                          status="active", is_primary=True))
    workspace_id = None
    if context_name in {"workspace_chat", "task"}:
        workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Receipt WS", operating_model={})
        db.add(workspace)
        await db.flush()
        from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder

        await ensure_workspace_artifact_folder(db, workspace)
        workspace_id = workspace.id
    db.add(Conversation(id=conversation_id, entity_id=entity_id, user_id=user_id,
                        workspace_id=workspace_id,
                        scope="workspace_main" if workspace_id else "channel", status="active"))
    await db.flush()
    task_id = None
    if context_name == "task":
        task_id = generate_ulid()
        db.add(Task(id=task_id, entity_id=entity_id, workspace_id=workspace_id,
                    conversation_id=conversation_id, creator_id=user_id, title="Receipt task",
                    status="in_progress", task_type="agent", details={}))
    await initialize_personal_approval(db, user_id=user_id, entity_id=entity_id)
    await db.commit()
    from packages.core.services.entity_fs import provision_entity_filesystem

    provision_entity_filesystem(entity_id)
    surface = {
        "global_owner_chat": ChatSurface.GLOBAL_OWNER_CHAT,
        "agent_dm": ChatSurface.AGENT_DM,
        "workspace_chat": ChatSurface.WORKSPACE_CHAT,
        "task": ChatSurface.SCHEDULED_AGENT_RUN,
    }[context_name]
    from packages.core.constants.agents import MANOR_AGENT_ID

    return _Context(
        surface=surface,
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=None if context_name == "task" else conversation_id,
        root=root / entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=MANOR_AGENT_ID if context_name in {"agent_dm", "task"} else None,
    )


_REAL_INNER_GUARD = ai_file_permissions.guard_ai_file_mutation


def _spy_inner_guard(monkeypatch, context: _Context) -> None:
    real_guard = _REAL_INNER_GUARD

    async def spy(**kwargs):
        receipt = runtime_current_tool_authorization_receipt()
        result = await real_guard(**kwargs)
        context.guard_calls.append({
            "paths": list(kwargs["paths"]),
            "receipt_action_key": receipt.action_key if receipt else None,
            "receipt_resources": receipt.resource_ids if receipt else None,
            "receipt_base": receipt.workspace_artifact_base_dir if receipt else None,
            "result": json.loads(result) if result else None,
        })
        return result

    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_mutation", spy)


# ---------------------------------------------------------------- incident


@pytest.mark.asyncio
async def test_global_owner_chat_image_conversion_writes_what_the_receipt_authorized(
    db_session, attachment_fs, monkeypatch,  # noqa: F811
):
    context = await _seed(db_session, attachment_fs, "global_owner_chat")
    data = _png()
    path = f"uploads/chat/{hashlib.sha256(data).hexdigest()[:16]}.png"
    db_session.add(Message(conversation_id=context.conversation_id, role="user", author_kind="user",
                           attachments=[{"kind": "chat_upload", "path": path}]))
    await db_session.commit()
    (context.root / path).parent.mkdir(parents=True, exist_ok=True)
    (context.root / path).write_bytes(data)
    _spy_inner_guard(monkeypatch, context)

    payload = await context.call({
        "kind": "image",
        "name": "IMG_9973_converted.png",
        "source": {"path": path, "expected_sha256": hashlib.sha256(data).hexdigest()},
        "storage_scope": "task",
    })

    assert payload.get("created") and payload.get("converted"), payload
    [guard] = context.guard_calls
    assert guard["result"] is None, guard
    assert guard["receipt_action_key"] == "workspace.file.create"
    # No handler-local bucket: global chat has no execution archive, so the
    # receipt and the write both name the Knowledge-root file.
    assert guard["receipt_resources"] == ("IMG_9973_converted.png",)
    assert guard["paths"] == ["IMG_9973_converted.png"]
    with Image.open(context.root / "IMG_9973_converted.png") as image:
        assert image.format == "PNG" and image.size == (12, 8)


@pytest.mark.asyncio
async def test_a_receipt_mismatch_surfaces_as_an_internal_error_not_a_permission_question(
    db_session, attachment_fs, monkeypatch,  # noqa: F811
):
    """Reintroduce the incident's handler-local prefix and check what is shown."""
    from packages.core.ai.runtime.output_policy import (
        PUBLIC_TOOL_FAILURE_MESSAGE,
        runtime_public_tool_result,
    )
    from packages.core.ai.runtime.tool_error_contract import model_tool_error_result

    context = await _seed(db_session, attachment_fs, "global_owner_chat")
    data = _png()
    (context.root / "photo.png").write_bytes(data)

    async def readable(**_kwargs):
        return None  # source read ACLs are covered elsewhere

    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_guard_file_read_access", readable)
    real_apply = generate_file_tool._apply_authorized_destination

    def drifted(handler_kwargs, arguments):
        real_apply(handler_kwargs, arguments)
        handler_kwargs["name"] = f"images/{handler_kwargs['name']}"

    monkeypatch.setattr(generate_file_tool, "_apply_authorized_destination", drifted)
    payload = await context.call({
        "kind": "image", "name": "copy.png", "file_type": "png",
        "source": {"path": "photo.png", "expected_sha256": hashlib.sha256(data).hexdigest()},
    })

    assert payload["mode"] == "authorization_receipt_mismatch", payload
    assert payload["error"] == "internal_authorization_error"
    assert payload["internal_error"] is True and payload["requires_human"] is False
    assert payload["retryable"] is False
    assert payload["authorized"] == {"action_key": "workspace.file.create", "paths": ["copy.png"]}
    assert payload["attempted"] == {"action_key": "workspace.file.create", "paths": ["images/copy.png"]}
    assert not (context.root / "images" / "copy.png").exists()

    model_view = json.loads(model_tool_error_result("generate_file", json.dumps(payload), max_chars=4000))
    assert model_view["error"] == "internal_authorization_error"
    assert model_view["message"].startswith("Internal Manor error")
    assert "not a permission decision" in model_view["message"]
    # The user never sees a permission prompt or an "ask the owner" hint.
    assert runtime_public_tool_result(payload) == PUBLIC_TOOL_FAILURE_MESSAGE


# --------------------------------------------------------------------- code


@pytest.mark.asyncio
async def test_nested_code_content_writes_inside_its_canonical_receipt_bundle(
    db_session, attachment_fs, monkeypatch,  # noqa: F811
):
    context = await _seed(db_session, attachment_fs, "task")
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "packages.core.ai.runtime.approval_service.guard_runtime_tool_action",
        AsyncMock(return_value=None),
    )
    _spy_inner_guard(monkeypatch, context)

    payload = await context.call({
        "kind": "code",
        "name": "nested-receipt/output.md",
        "content": "# Receipt content",
        "storage_scope": "task",
    })

    assert payload.get("created") is True, payload
    assert payload["files"] and len(payload["files"]) == 1, payload
    [guard] = context.guard_calls
    assert guard["result"] is None, guard
    assert guard["receipt_action_key"] == "workspace.file.create"
    [receipt_resource] = guard["receipt_resources"]
    assert payload["bundle_path"] == f"{guard['receipt_base']}/{receipt_resource}"
    [written] = guard["paths"]
    assert written == payload["files"][0]["path"]
    assert written.startswith(f"{payload['bundle_path']}/")
    assert (context.root / written).read_text() == "# Receipt content"


# ------------------------------------------------------------------ matrix


_KINDS = tuple(kind for kind in GenerateFileKind if kind is not GenerateFileKind.SEARCH)
_STORAGE_SCOPES = tuple(
    GENERATE_FILE_SCHEMA["function"]["parameters"]["properties"]["storage_scope"]["enum"]
)
# One representative source per conversion target: the destination depends on
# the target type and kind, never on which source format produced it.
_CONVERSIONS = {
    target: next(source for source, targets in FILE_CONVERSION_TARGETS.items() if target in targets)
    for target in sorted({target for targets in FILE_CONVERSION_TARGETS.values() for target in targets})
}
_PROMPT_ONLY_HANDLERS = (
    "handle_pdf", "handle_word_document", "handle_presentation",
    "handle_spreadsheet", "handle_image", "handle_video", "handle_audio",
)


def _routes(sources: dict[str, dict]) -> list[tuple[str, str, dict]]:
    """(route, natural extension, arguments) for every deterministic generate_file route."""

    routes: list[tuple[str, str, dict]] = [("content", "md", {"content": "# Report"})]
    for file_type in sorted(JSON_FILE_TYPES | DELIMITED_FILE_TYPES | {"md"}):
        content = "a,b\n1,2" if file_type in DELIMITED_FILE_TYPES else (
            '{"type": "diagram", "elements": []}' if "diagram" in file_type else "{}" if file_type == "json" else "# Report"
        )
        routes.append(("content", file_type, {"content": content, "file_type": file_type}))
    routes.append(("prompt", "diagram.json", {"prompt": "Start -> Review -> Done"}))
    routes.append(("files", "html", {"files": [{"path": "index.html", "content": "<p>hi</p>"}]}))
    for extension in sorted(OPERATION_GENERATION_TYPES):
        routes.append(("operations", extension, {"operations": office_operations(extension), "file_type": extension}))
        routes.append(("operations", extension, {"operations": office_operations(extension)}))
    for target, source in _CONVERSIONS.items():
        routes.append(("source", target, {"source": sources[source], "file_type": target}))
    return routes


def _names(extension: str, case: int) -> list[str | None]:
    # Unnamed, bare, bare with extension (the incident's shape), nested.
    return [None, f"c{case}-out", f"c{case}-out.{extension}", f"nested-{case}/c{case}-out.{extension}"]


def _matrix_contexts() -> list[tuple[str, str]]:
    cells = []
    for context_name in ("global_owner_chat", "agent_dm", "workspace_chat", "task"):
        for storage_scope in _STORAGE_SCOPES:
            if storage_scope == "workspace" and context_name in {"global_owner_chat", "agent_dm"}:
                continue  # rejected before any authorization or write
            cells.append((context_name, storage_scope))
    return cells


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.parametrize("context_name,storage_scope", _matrix_contexts())
async def test_every_generate_file_write_lands_inside_its_receipt(
    db_session, attachment_fs, monkeypatch, context_name, storage_scope,  # noqa: F811
):
    """kind x route x name x storage_scope x context: receipt paths == written paths."""

    context = await _seed(db_session, attachment_fs, context_name)
    sources: dict[str, dict] = {}
    for source in set(_CONVERSIONS.values()):
        data = _png() if source == "png" else f"synthetic {source}".encode()
        (context.root / f"source.{source}").write_bytes(data)
        sources[source] = {"path": f"source.{source}", "expected_sha256": hashlib.sha256(data).hexdigest()}

    async def readable(**_kwargs):
        return None  # source/template read ACLs are covered elsewhere

    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_guard_file_read_access", readable)

    async def prompt_only(**_kwargs):
        return json.dumps({"error": "prompt_only_route"})

    for handler in _PROMPT_ONLY_HANDLERS:
        monkeypatch.setattr(generate_file_tool, handler, prompt_only)

    from unittest.mock import AsyncMock

    # Governance/HITL is not under test: the receipt is minted from the real
    # classification either way, and the receipt comparison runs after it.
    monkeypatch.setattr(
        "packages.core.ai.runtime.approval_service.guard_runtime_tool_action",
        AsyncMock(return_value=None),
    )

    # The inner guard evaluates the exact comparison guard_ai_file_mutation
    # makes, then blocks, so no case writes and every case stays independent.
    observed: list[dict] = []
    probe_hits: list[int] = []

    async def evaluating_guard(**kwargs):
        receipt = runtime_current_tool_authorization_receipt()
        if receipt is None:  # bare-handler probe
            probe_hits.append(1)
            return json.dumps({"error": "probe_blocked"})
        visible = ai_file_permissions.canonical_visible_user_paths(kwargs["entity_id"], kwargs["paths"])
        authorized, action_key = ai_file_permissions.receipt_authorizes_file_mutation(
            receipt,
            entity_id=kwargs["entity_id"],
            user_id=kwargs.get("user_id"),
            workspace_id=kwargs.get("workspace_id") or context.workspace_id,
            conversation_id=kwargs.get("conversation_id"),
            task_id=kwargs.get("task_id"),
            action=str(getattr(kwargs["action"], "value", kwargs["action"])),
            visible_paths=visible,
        )
        observed.append({"authorized": authorized, "paths": visible, "action_key": action_key,
                         "receipt_action_key": receipt.action_key,
                         "receipt_resources": receipt.resource_ids,
                         "receipt_base": receipt.workspace_artifact_base_dir})
        return json.dumps({"error": "evaluated"})

    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_mutation", evaluating_guard)

    # Phase 1 (cheap, code-derived): which kind x route x name cases reach a
    # file mutation at all? The bare handler runs without a receipt.
    reaching: list[tuple[str, str, dict]] = []
    case = 0
    for kind in _KINDS:
        for route, extension, route_arguments in _routes(sources):
            for name in _names(extension, case):
                case += 1
                arguments = {"kind": kind.value, "storage_scope": storage_scope, **route_arguments}
                if name is not None:
                    arguments["name"] = name
                before = len(probe_hits)
                await _generate_file_handler(
                    context.entity_id, user_id=context.user_id or "",
                    conversation_id=context.conversation_id or "",
                    workspace_id=context.workspace_id, task_id=context.task_id,
                    **arguments,
                )
                if len(probe_hits) > before:
                    reaching.append((kind.value, route, arguments))

    # Phase 2: every reaching case through the real Runtime boundary
    # (classifier -> receipt -> handler -> materializer -> inner guard).
    mismatches: list[dict] = []
    covered: dict[str, set[str]] = {}
    for kind, route, arguments in reaching:
        before = len(observed)
        payload = await context.call(arguments)
        calls = observed[before:]
        if not calls:
            mismatches.append({"kind": kind, "route": route, "arguments": arguments, "no_guard": payload})
        for guard in calls:
            if not guard["authorized"]:
                mismatches.append({"kind": kind, "route": route, "name": arguments.get("name"),
                                   "file_type": arguments.get("file_type"), **guard})
            else:
                covered.setdefault(route, set()).add(kind)

    assert not mismatches, json.dumps(mismatches[:5], indent=1, default=str)
    # The matrix must actually exercise every deterministic route.
    assert set(covered) == {"content", "prompt", "files", "operations", "source"}, covered
    assert {"document", "pdf", "image"} <= covered["source"], covered
    assert {"document", "word_document", "presentation", "spreadsheet"} <= covered["operations"], covered
