from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from packages.core.ai.runtime.channel_tools import RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME
from packages.core.ai.runtime.chrome_routing import detect_chrome_local_browser_route
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.skill_routing import (
    external_platform_action_intent,
    external_platform_draft_intent,
    local_coding_cli_intent,
)

RuntimePromptGuidanceKind = Literal[
    "local_coding_cli",
    "external_integration",
    "external_platform_draft",
    "code_artifact",
    "workspace_artifact",
    "workspace_task_update",
    "workspace_agent_mode",
]


def runtime_file_editor_live_edit_guidance(editor_context: Mapping[str, Any] | None) -> str:
    """Render patch-only live edit instructions for the file-editor surface."""

    context = dict(editor_context or {})
    document_name = (
        str(
            context.get("documentName")
            or context.get("document_name")
            or context.get("path")
            or context.get("sourcePath")
            or context.get("source_path")
            or "the active file"
        ).strip()
        or "the active file"
    )
    editor_type = str(context.get("editorType") or context.get("editor_type") or "editor").strip()
    file_type = str(
        context.get("fileType")
        or context.get("file_type")
        or context.get("mimeType")
        or context.get("mime_type")
        or ""
    ).strip().lower()
    supports_image_generation = bool(
        context.get("supportsImageGeneration")
        or context.get("supports_image_generation")
    )

    lines = [
        "## File Editor Live Edit Runtime",
        f"You are editing `{document_name}` in the active {editor_type or 'editor'}.",
        "The active document content is provided by a Runtime context block between "
        "`<manor-current-document>` tags when the editor can supply it.",
        "Do not ask which document to edit. Treat the mounted current editor file as "
        "the only writable target for this turn.",
        "",
        "Patch protocol:",
        "- Make direct edits in this response. If placement or wording is underspecified, "
        "choose a reasonable default from the current document and mention the assumption "
        "outside patch blocks.",
        "- For file/text/code/editor-state edits, output at least one complete "
        "`<manor-live-patch>` block before explanation. Do not answer with only prose.",
        "- The patch protocol is strict. Use exactly `<manor-live-patch>` and "
        "`</manor-live-patch>`; do not use variants such as `<manor live patch>`.",
        "- Never stream or return the complete replacement file unless the whole file is "
        "the exact `find` value for a necessary full-file replacement.",
        "- Each patch block contains a JSON array and no Markdown fence or commentary. "
        "Use exactly this schema:",
        "<manor-live-patch>",
        '[{"op":"replace","find":"exact current text","replace":"new text"}]',
        "</manor-live-patch>",
        "- Supported `op` enum values are exactly `replace`, `delete`, "
        "`insert_before`, `insert_after`, `prepend`, and `append`.",
        "- `insert_before` and `insert_after` require `find` plus `text`; never use "
        "`replace` for insert operations.",
        "- Do not write human-readable operation names such as `insert after`; use the "
        "exact enum value `insert_after`.",
        "- Use exact `find` text copied from the current document. If the exact text is "
        "long, replace a stable enclosing section.",
        "- For replace/delete, set `all:true` only when every occurrence should change.",
        "- If multiple edits depend on each other, emit them in order inside the same "
        "patch array or as several patch blocks.",
        "- Each patch block must leave the active document valid for its editor format.",
        "- The app hides patch tags from chat and applies each complete patch block to "
        "the editor for user review.",
    ]
    if supports_image_generation:
        lines.extend([
            "",
            "Image-generation protocol:",
            "- The current rendered image may be attached to this chat turn as a hidden "
            "image file.",
            "- For semantic bitmap changes such as replacing embedded text, redrawing a "
            "subject, changing style, or regenerating from the current image, use "
            "`generate_image` or `generate_file` with `kind:\"image\"` and the attached "
            "image reference.",
            "- Preserve composition, subject identity, style, framing, and aspect ratio "
            "unless the user explicitly asks to change them.",
            "- Set `save_to_knowledge:false` for temporary editor previews.",
            "- Do not return raw image bytes or base64 inside patch blocks.",
        ])
    lines.extend(_runtime_file_editor_format_guidance(file_type, editor_type))
    lines.append(
        "Ask a clarification only when the requested edit is impossible to represent "
        "in the active editor."
    )
    return "\n".join(lines)


def runtime_voice_session_guidance() -> str:
    """Render no-tools realtime voice guidance for the voice surface."""

    return (
        "You are in a live voice conversation. "
        "This realtime session has no Manor tool bridge; answer from the visible "
        "conversation context and do not promise tool actions. "
        "Keep responses concise and conversational - avoid long lists, code blocks, "
        "or markdown formatting. Match the user's language automatically."
    )


_CHANNEL_LANGUAGE_NAMES = {
    "en": "English",
    "zh": "Chinese",
    "es": "Spanish",
    "de": "German",
}


def _runtime_channel_language_instruction(language: str | None) -> str:
    normalized = str(language or "en").strip().lower().replace("_", "-").split("-", 1)[0]
    language_name = _CHANNEL_LANGUAGE_NAMES.get(normalized, "English")
    return (
        f"Reply to the visitor in {language_name}. This is the customer-facing "
        "language configured for this channel, independent of any operator UI "
        "language or Manor account language."
    )


def _runtime_file_editor_format_guidance(file_type: str, editor_type: str) -> list[str]:
    key = f"{file_type} {editor_type}".lower()
    if "diagram" in key:
        return [
            "",
            "Diagram editor-state requirements:",
            "- Patch the current JSON document with exact find/replace operations.",
            "- Keep the final EditableDiagramDocument complete and valid.",
            '- Required top-level fields: `version:"editable_diagram_v1"`, `id`, '
            "`title`, `canvas`, `theme`, and `elements`.",
            '- Canvas shape: `{ width, height, unit:"px", originX, originY }`.',
            '- Element kinds include `shape`, `text`, and `connector`; preserve '
            "editability by using text fields on shapes instead of drawing labels as "
            "image content.",
            "- Supported shape values include rect, roundRect, ellipse, diamond, "
            "triangle, hexagon, parallelogram, trapezoid, cylinder, document, "
            "rightArrow, and downArrow.",
            "- For whole-diagram changes, patch the `elements` array with a clear new "
            "editable diagram.",
        ]
    if "pdf" in key:
        return [
            "",
            "PDF editor-state requirements:",
            "- Do not return raw PDF bytes.",
            '- Patch the current JSON overlay and keep this shape valid: '
            '`{ "format": "manor-pdf-overlay-v1", "annotations": [...] }`.',
            "- The PDF editor applies `annotations` as editable overlays on the "
            "currently open PDF.",
            "- Keep existing annotations unless the user asks to remove or replace them.",
            "- Coordinates are normalized from the top-left of each page: page is "
            "1-based, and x/y/width/height are numbers from 0 to 1.",
            '- Supported annotation kinds include `text`, `highlight`, `whiteout`, '
            "and `draw`.",
            "- Use `pageText` hints in the current document JSON to position edits near "
            "matching labels or text.",
            "- For table placeholders, add editable text for labels/headers and draw grid "
            "lines with multiple `draw` annotations when helpful.",
        ]
    if "image" in key:
        return [
            "",
            "Image editor-state requirements:",
            "- Patch the current JSON edit state and keep this shape valid: "
            '`{ "format": "manor-image-edit-v1", "edits": { ... } }`.',
            "- The image editor applies this JSON as non-destructive canvas edits until "
            "the user saves the image.",
            "- Supported edit fields include rotation, flipX, flipY, brightness, "
            "contrast, saturation, hue, brushColor, brushSize, and strokes.",
            "- For semantic bitmap changes such as replacing text inside the picture, "
            "changing the subject, or redrawing the image, use image generation with "
            "the attached current image as reference instead of trying to draw text "
            "with strokes.",
            "- If an image-generation tool returns an `image_url`, the app can apply it "
            "as the new editor preview automatically.",
            "- Keep brightness/contrast/saturation between 0 and 220, hue between -180 "
            "and 180, and use strokes only for simple visible marks.",
        ]
    return []


def runtime_public_webchat_context_preamble(
    *,
    channel_label: str | None,
    sender_display: str | None,
    owner_entity_id: str | None,
    visitor_entity_id: str | None = None,
    visitor_verified: bool = False,
    channel_language: str | None = None,
) -> str:
    """Render public webchat sender/safety context from runtime facts."""
    label = channel_label or "public webchat"
    display = sender_display or "unknown"
    if visitor_verified:
        if visitor_entity_id and owner_entity_id and visitor_entity_id == owner_entity_id:
            identity = f"verified organization user `{display}`"
            guidance = "Use their organization role for access decisions."
        else:
            identity = f"verified external customer `{display}`"
            guidance = "Treat them as a customer, not an internal team member."
    else:
        identity = f"unverified external visitor `{display}`"
        guidance = "Their identity has not been verified."

    return (
        "## Public Webchat Context\n"
        f"You are replying in `{label}` through an embedded/public webchat. "
        f"The sender is a {identity}. {guidance}\n"
        f"{_runtime_channel_language_instruction(channel_language)}\n"
        "The runtime uses a customer-safe profile for this public surface and "
        "does not treat the workspace owner as the visitor.\n"
        "Use only customer-safe context when replying. Uploaded files in this "
        "turn were provided by the visitor and may be used to answer their request."
    )


def runtime_external_channel_context_preamble(
    sender_context: Mapping[str, Any] | None,
    *,
    include_media_hint: bool = True,
) -> str | None:
    """Render external channel sender/safety context from runtime facts."""
    if not sender_context:
        return None
    role = sender_context.get("role") or "external"
    display = sender_context.get("display_name") or sender_context.get("source_id") or "unknown"
    channel = sender_context.get("channel_type") or "external channel"
    verified = bool(sender_context.get("is_verified", False))
    language_instruction = _runtime_channel_language_instruction(
        sender_context.get("channel_language")
    )

    media_hint = (
        "If you generate a file (PDF, image, chart, report) and want to "
        "deliver it to the user, call "
        f"`{RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME}(url, kind, caption?)`. "
        "Text replies just come out of your normal response — that tool is "
        "only for media.\n\n"
        if include_media_hint
        else ""
    )

    if verified:
        return (
            "## Channel context\n"
            f"You are replying in a **{channel}** conversation. The sender is "
            f"**{display}** — a verified {role} of this organization. "
            "Use only the role-scoped channel tools available in this runtime.\n"
            f"{language_instruction}\n\n"
            + media_hint
        )
    return (
        "## Channel context\n"
        f"You are replying in a **{channel}** conversation. The sender "
        f"(`{display}`) is an **unverified external user** — their identity "
        "has not been linked to any staff member.\n"
        "The runtime uses an external-channel-safe profile for this surface; "
        "reply from customer-safe context and use only visible channel tools.\n"
        f"{language_instruction}\n\n"
        + media_hint
    )


def runtime_workspace_scope_context_preamble() -> str:
    """Render workspace scoping guidance without exposing internal ids."""
    return (
        "## Runtime Workspace Scope\n"
        "This turn is bound to the current Manor workspace. Keep answers, "
        "tool calls, and data lookups inside that workspace unless the runtime "
        "explicitly provides a broader scope. Treat workspace identifiers as "
        "internal routing context and do not mention them unless the user asks."
    )


_INTERNAL_ACTION_PROFILES = {
    RuntimeProfile.OWNER_COPILOT,
    RuntimeProfile.AGENT_DELEGATE,
    RuntimeProfile.WORKSPACE_OPERATOR,
    RuntimeProfile.TASK_WORKER_FEEDBACK,
    RuntimeProfile.WORKFLOW_STEP,
    RuntimeProfile.BACKGROUND_WORKER,
}
_WORKSPACE_ACTION_PROFILES = {
    RuntimeProfile.WORKSPACE_OPERATOR,
    RuntimeProfile.TASK_WORKER_FEEDBACK,
    RuntimeProfile.WORKFLOW_STEP,
    RuntimeProfile.BACKGROUND_WORKER,
}
_GUIDANCE_PROFILE_ALLOWLIST: dict[RuntimePromptGuidanceKind, set[RuntimeProfile]] = {
    "local_coding_cli": _INTERNAL_ACTION_PROFILES,
    "external_integration": _INTERNAL_ACTION_PROFILES,
    "external_platform_draft": _INTERNAL_ACTION_PROFILES,
    "code_artifact": _INTERNAL_ACTION_PROFILES,
    "workspace_artifact": _WORKSPACE_ACTION_PROFILES,
    "workspace_task_update": _WORKSPACE_ACTION_PROFILES,
    "workspace_agent_mode": {RuntimeProfile.WORKSPACE_OPERATOR},
}


def runtime_allows_prompt_guidance(
    envelope,
    guidance: RuntimePromptGuidanceKind,
) -> bool:
    """Return whether a hard-coded prompt hint is valid for this runtime."""
    if envelope is None:
        return True
    allowed = _GUIDANCE_PROFILE_ALLOWLIST.get(guidance)
    if not allowed:
        return False
    profile = getattr(envelope, "profile", None)
    if isinstance(profile, str):
        try:
            profile = RuntimeProfile(profile)
        except ValueError:
            return False
    return profile in allowed


_WORKSPACE_ARTIFACT_TERMS = (
    "文件", "文档", "附件", "下载", "交付物", "产物", "资料包", "压缩包",
    "pdf", "docx", "word", "ppt", "pptx", "slides", "deck", "xlsx", "excel",
    "csv", "表格", "图片", "图像", "配图", "视频", "音频", "海报", "封面",
    "产品图", "参考图", "对标图", "改图", "修图", "设计图", "图纸", "效果图", "渲染图", "样图", "三视图", "多视角",
    "尺寸标注", "标注图", "草图", "cad", "solidworks", "image", "visual",
    "product image", "reference image", "image edit", "drawing", "render", "mockup", "sketch", "diagram", "file", "artifact",
    "attachment", "download", "document", "spreadsheet", "video", "audio",
    "代码", "源码", "网站", "网页", "页面", "前端", "html", "css", "javascript", "typescript", "react", "vue", "code", "website", "web app",
)
_WORKSPACE_ARTIFACT_CREATE_TERMS = (
    "生成", "创建", "制作", "绘制", "导出", "输出", "产出", "保存", "打包",
    "做成", "整理成", "给我", "调整", "修改", "编辑", "优化", "融合", "合成",
    "参考", "结合", "换", "去掉", "增加", "generate", "create", "make", "draw", "render",
    "produce", "export", "save", "package", "edit", "modify", "adjust", "refine",
    "improve", "combine", "merge", "写", "编写", "开发", "实现", "build",
)
_WORKSPACE_ARTIFACT_LOOKUP_TERMS = (
    "哪里", "在哪", "查看", "看", "找到", "打开", "下载", "发给我",
    "where", "view", "find", "open", "download",
)
_WORKSPACE_ARTIFACT_EXISTING_TERMS = (
    "上次", "之前", "刚才", "已经", "已有", "现有", "之前生成", "上次生成",
    "previous", "existing", "already", "earlier", "last time",
)

_CODE_ARTIFACT_TERMS = (
    "代码", "源码", "网站", "网页", "页面", "前端", "html", "css", "javascript",
    "js", "typescript", "ts", "react", "vue", "svelte", "landing page",
    "code", "website", "web app", "frontend",
)
_CODE_ARTIFACT_CREATE_TERMS = (
    "写", "编写", "生成", "创建", "制作", "开发", "实现", "做", "打包",
    "write", "generate", "create", "make", "build", "implement",
)

_WORKSPACE_IN_FLIGHT_TASK_UPDATE_TERMS = (
    "中途追加", "追加角色", "追加要求", "追加任务", "新增角色", "新增流程",
    "衔接现有", "现有运行任务", "正在运行", "继续运行", "不中断", "不重置",
    "不推翻", "原有任务", "原有流程", "原有要求", "后续审核", "后续评估",
    "回传", "闭环流程", "协作链路", "下游", "上游", "承接",
    "in-flight", "in flight", "running task", "current task", "existing task",
    "continue running", "do not interrupt", "do not reset", "append requirement",
    "append requirements", "add role", "add roles", "downstream", "handoff",
)


@dataclass(frozen=True)
class RuntimeWorkspaceArtifactIntent:
    is_artifact: bool = False
    is_creation_request: bool = False
    is_existing_lookup: bool = False


def runtime_workspace_artifact_intent(text: str | None) -> bool:
    return runtime_workspace_artifact_intent_details(text).is_artifact


def runtime_workspace_artifact_intent_details(
    text: str | None,
) -> RuntimeWorkspaceArtifactIntent:
    if not text:
        return RuntimeWorkspaceArtifactIntent()
    lowered = text.lower()
    has_artifact_term = any(term in lowered for term in _WORKSPACE_ARTIFACT_TERMS)
    if not has_artifact_term:
        return RuntimeWorkspaceArtifactIntent()
    has_creation_term = any(term in lowered for term in _WORKSPACE_ARTIFACT_CREATE_TERMS)
    has_lookup_term = any(term in lowered for term in _WORKSPACE_ARTIFACT_LOOKUP_TERMS)
    has_existing_term = any(term in lowered for term in _WORKSPACE_ARTIFACT_EXISTING_TERMS)
    is_artifact = has_creation_term or has_lookup_term
    is_existing_lookup = has_lookup_term and (not has_creation_term or has_existing_term)
    is_creation_request = has_creation_term and not is_existing_lookup
    return RuntimeWorkspaceArtifactIntent(
        is_artifact=is_artifact,
        is_creation_request=is_creation_request,
        is_existing_lookup=is_existing_lookup,
    )


def runtime_code_artifact_intent(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(term in lowered for term in _CODE_ARTIFACT_TERMS) and any(
        term in lowered for term in _CODE_ARTIFACT_CREATE_TERMS
    )


def runtime_workspace_in_flight_task_update_intent(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(term in lowered for term in _WORKSPACE_IN_FLIGHT_TASK_UPDATE_TERMS)


def _tool_name_set(tool_names: Iterable[str] | None) -> set[str]:
    return {str(name) for name in (tool_names or ()) if str(name or "").strip()}


def runtime_response_language_guidance(active_user_message: str | None) -> str | None:
    text = active_user_message or ""
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    if cjk_count <= 0:
        return None
    return (
        "## Response Language\n"
        "- The latest user message is in Chinese. All user-visible narration, progress updates, "
        "SSE `text_delta` content, and final summaries must be Chinese.\n"
        "- Do not emit English operational narration such as \"Now let me...\", "
        "\"Good progress\", \"The issue is...\", or \"The PPT built successfully\".\n"
        "- Tool commands, filenames, JSON keys, API names, and code identifiers may remain in their required language."
    )


def runtime_output_discipline_guidance() -> str:
    """User-visible answer discipline.

    Multi-round chat turns (search → delegate → draft) tend to make the model
    stack several restated lead-ins before the actual deliverable — e.g. "I
    found a better angle", "You're right, the last version was too…", "Here's
    the rewrite" — so a single reply reads as the same answer "repeated many
    times". This rule keeps the reply to one clean version.
    """
    return (
        "## Output discipline\n"
        "- Deliver ONE clean answer. Lead with the result itself (the draft, the "
        "deliverable, the direct answer) — not with narration about how you got there.\n"
        "- Do NOT restate the task, and do NOT prefix the answer with process "
        "narration such as \"I found a better angle\", \"You're right, the last "
        "version was too…\", \"Here's the rewrite\", \"我找到了…\", \"下面是重写版\", "
        "or \"Let me…\". Any such framing belongs in at most one short sentence, if at all.\n"
        "- Never include more than one version of the same answer in a single reply. "
        "If you reconsidered your approach mid-turn, show only the final version — "
        "do not stack earlier drafts or repeated lead-ins above it."
    )


def runtime_available_tools_section(tools: Iterable[dict] | None) -> str | None:
    items = list(tools or ())
    if not items:
        return None
    lines = ["## Available Tools"]
    for tool in items:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name", "?")
        desc = fn.get("description", "")
        if desc:
            first_sentence = desc.split(".")[0].strip()
            if len(first_sentence) > 120:
                first_sentence = first_sentence[:117] + "..."
            lines.append(f"- **{name}**: {first_sentence}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)


def runtime_tool_usage_guidance(
    *,
    tool_names: Iterable[str] | None,
    has_tools: bool,
    active_user_message: str | None = None,
) -> str | None:
    if not has_tools:
        return None
    loaded_tools = _tool_name_set(tool_names)
    chrome_local_route = detect_chrome_local_browser_route(active_user_message)
    chrome_hint = (
        "- The latest request explicitly asks to operate the user's local "
        "Chrome browser. Treat this as a Chrome skill task, not a direct MCP "
        "tool-discovery task. When the `chrome` skill is listed in Available "
        "Skills, call `invoke_skill(skill=\"chrome\", input=<latest user "
        "request>)` as the primary route; if the `invoke_skill` schema is "
        "deferred, load `invoke_skill` with `search_tools`; do not load Chrome MCP tools directly from the parent chat. The Chrome skill owns "
        "status/open/list-tabs/read_page/click_element/fill_or_select/"
        "computer/key/scroll through the Runtime Harness. If the Chrome skill is not "
        "visible in this turn, stop and explain that the Chrome runtime skill "
        "or setup is unavailable. Do not use `web_search`, `web_fetch`, or "
        "`browse_web` as a substitute for Chrome.\n"
        if chrome_local_route and {"invoke_skill", "search_tools"}.intersection(loaded_tools)
        else ""
    )
    rendered_web_hint = (
        "- For JavaScript-rendered websites or SPA shells, use `browse_web`; "
        "if it is not loaded yet, call `search_tools` for `browse_web` first. "
        "`web_fetch` only reads static HTTP content.\n"
        if not chrome_local_route
        and ("search_tools" in loaded_tools or "browse_web" in loaded_tools)
        else ""
    )
    return (
        "## Tool Usage\n"
        "- Before the first tool call in a tool-using turn, emit one short, natural progress sentence "
        "that tells the user what you are about to check or do. This sentence must be normal visible "
        "assistant text, not a tool argument or final summary.\n"
        "- During long multi-step tool work, add brief progress text only when the intent changes "
        "meaningfully; do not narrate every low-level click, page read, or file read.\n"
        "- On tool error, explain the issue; chain tool calls when needed.\n"
        "- Treat the latest user message as the active intent — don't "
        "resume earlier tasks unless asked.\n"
        "- Match response length to scope: short question → short answer.\n"
        f"{chrome_hint}"
        f"{rendered_web_hint}"
        "- Route code/scripts/large content through generate_file(kind='code'), "
        "write_file, or bash, not inline chat text.\n"
        "- For LARGE files, prefer edit_file (targeted find/replace) over "
        "rewriting the whole file with write_file, and build big files in "
        "sections (write once, then append) — a single oversized write_file "
        "can exceed the model output limit and get truncated, failing the step.\n"
        "- For exact Knowledge file content, use document details `fs_path` "
        "with read_file."
    )


def runtime_workspace_agent_mode_guidance(
    *,
    envelope,
    workspace_id: str | None,
) -> str | None:
    if not workspace_id:
        return None
    if not runtime_allows_prompt_guidance(envelope, "workspace_agent_mode"):
        return None
    return (
        "## Workspace Agent Mode\n"
        "You are Manor AI operating as the Workspace Agent for the active workspace. "
        "Treat workspace chat as an operational control surface, not just Q&A.\n"
        "- Interpret the latest user message as one of: answer, new task, task update, "
        "goal/strategy request, workspace rule/guardrail change, knowledge request, "
        "approval reply, or suggestion.\n"
        "- Before answering or acting on workspace state, call `workspace_agent` "
        "with `action='search'` for the relevant category unless the answer is "
        "purely conversational.\n"
        "- If the Workspace Context includes Open Workspace HITL Requests, first "
        "decide whether the latest user message semantically answers one of those "
        "requests. When it does, call `workspace_resolve_hitl` with the matching "
        "`message_id` or `hitl_id` and action. When it does not, continue the "
        "normal workspace conversation without resolving HITL.\n"
        "- For concrete one-off work, call `workspace_agent` with "
        "`action='create_task'` and include task-only instructions, required "
        "references, and task rules in `params`. Set `params.start=true` when "
        "the user asks you to do/prepare/run the work now; leave it false only "
        "when they explicitly ask to create a todo/task for later.\n"
        "- When the user asks you to use an existing workspace service or a "
        "service-bound agent capability now, call `workspace_agent` with "
        "`action='delegate_service'`. Pass `params.service_key` (or "
        "`agent_subscription_id`) from the Workspace Context Agents/services "
        "list and `params.prompt`. If the service key is not visible, call "
        "`workspace_search(category='agents')` first. The delegated service "
        "agent must use its own tool/MCP scope; do not claim the master agent "
        "has the service's MCP tools directly.\n"
        "- For extra requirements on an existing task, call `workspace_agent` "
        "with `action='update_task_runtime'`; do not leave durable task "
        "requirements only in chat text.\n"
        "- If the latest message only appends roles, review stages, or downstream "
        "workflow to an existing/running task, treat those as task-local runtime "
        "instructions, not workspace-wide rules or immediate new tasks.\n"
        "- If the user asks to add, enable, connect, load, or scope an external "
        "platform/integration for the workspace, do not call that platform tool "
        "immediately. Create an operation draft that binds the platform as a "
        "`capability_binding.upsert` with `capability_type='mcp'`, "
        "`integration_key`; include `allowed_tools` only when the user wants to "
        "narrow that server's actions. Add any "
        "requested guardrail/rule patches. Apply only after user confirmation; "
        "if credentials are missing, tell the user which Integration setting or "
        "runtime worker must be connected.\n"
        "- Never use action labels such as `publish` as `capability_type`; use "
        "`capability`, `mcp`, `tool`, `skill`, or `action` depending on the "
        "binding transport.\n"
        "- For persistent workspace-wide behavior changes, use the operation "
        "draft flow: call `workspace_operation` (or `workspace_agent` with "
        "`action='operation'`) to create/patch/validate/preview a draft, then "
        "apply only after the user explicitly confirms. Simple rule additions "
        "may use `workspace_agent` with `action='add_rule'`, which also goes "
        "through the operation draft runtime. "
        "If it is unclear whether a rule is task-only or workspace-wide, ask one "
        "short clarification before changing policy.\n"
        "- For planning, reprioritization, or goal-driven next steps, call "
        "`workspace_agent` with `action='request_strategist_review'`. Do not "
        "trigger strategist review merely because the user appended task-local "
        "requirements unless they explicitly ask to replan or create follow-up tasks now.\n"
        "- For document-dependent work, use workspace Knowledge/document references "
        "and include required refs or a knowledge query on the task. In strict "
        "knowledge mode, cite or defer rather than inventing.\n"
        "- External publishing/sending and user-visible file mutations are governed "
        "by the runtime policy gate. Call the intended tool once and respect the "
        "returned approval workflow; never bypass or silently weaken guardrails."
    )


def runtime_local_coding_cli_routing_guidance(
    *,
    envelope,
    active_user_message: str | None,
    tool_names: Iterable[str] | None,
    manual_skill_selected: bool = False,
) -> str | None:
    if not runtime_allows_prompt_guidance(envelope, "local_coding_cli"):
        return None
    if manual_skill_selected or not local_coding_cli_intent(active_user_message):
        return None
    loaded_tools = _tool_name_set(tool_names)
    if not {"search_tools", "invoke_skill"}.intersection(loaded_tools):
        return None
    return (
        "## Local Coding CLI Routing\n"
        "- The latest user message asks to use a local coding CLI or modify/review "
        "files in a local project directory. Treat this as a local coding task, "
        "not a browser, social media, public web, Knowledge, generic bash, or "
        "`generate_file` workflow.\n"
        "- When the `local-coding-operations` skill is listed in Available "
        "Skills, call `invoke_skill(skill=\"local_coding_operations\", "
        "input=<latest user request>)` as the primary route. That skill owns "
        "provider choice, path confirmation, check_path, run/review, session "
        "continuation, and async dispatch behavior.\n"
        "- Only fall back to direct MCP discovery with `search_tools` when the "
        "`local-coding-operations` skill is not available in this turn. In that "
        "fallback, start with `search_tools` for "
        "`select:mcp__codex_cli__check_path,mcp__codex_cli__run,mcp__claude_code__check_path,mcp__claude_code__run`, "
        "then call `check_path` only when there is no active confirmed target "
        "and the user is asking to work in an existing local project/path. For "
        "new scratch coding work without a user-specified path, call `run` "
        "without `cwd`; the runtime will create and reuse a Manor scratch "
        "workspace for this conversation.\n"
        "- Do not call `browse_web`, `take_screenshot`, `bash`, or `sandbox_exec` as the "
        "primary route for this request."
    )


def runtime_external_integration_routing_guidance(
    *,
    envelope,
    active_user_message: str | None,
    tool_names: Iterable[str] | None,
    workspace_id: str | None = None,
) -> str | None:
    if not runtime_allows_prompt_guidance(envelope, "external_integration"):
        return None
    if not external_platform_action_intent(active_user_message):
        return None
    loaded_tools = _tool_name_set(tool_names)
    if "search_tools" not in loaded_tools:
        return None
    workspace_scope = ""
    if workspace_id:
        workspace_scope = (
            "- In Workspace chats, `search_tools` only reveals platform tools "
            "that are already in the workspace runtime surface. If the target "
            "platform is not returned or loaded, do not treat the platform name "
            "as authorization and do not invent a tool call. If the user is "
            "asking to add/enable/connect/load that platform for future workspace "
            "work, create a `workspace_operation` draft with "
            "`capability_binding.upsert` (`capability_type='mcp'`, "
            "`integration_key`; add `allowed_tools` only to narrow scope) and apply only after "
            "the user confirms. For this request, prefer the platform MCP "
            "server when one is available.\n"
        )
    return (
        "## External Platform Routing\n"
        "- The latest user message appears to ask for an action on a named "
        "external platform or Integration. Treat this "
        "as an integration task, not a generic writing task.\n"
        f"{workspace_scope}"
        "- First call `search_tools` with the platform and action keywords, "
        "then use the provider-specific MCP tool if it is available.\n"
        "- Do not ask the user to name internal tool chains; hide orchestration "
        "behind the natural request and follow the selected skill/tool's own "
        "rules for platform-specific constraints.\n"
        "- Do not invoke blog/article/content-writing skills as the primary "
        "route for publishing, sending, commenting, liking, or posting to an "
        "external platform. Use a writing skill only when the user explicitly "
        "asks for a blog/article draft or a named style skill.\n"
        "- For publish/send tools that expose a confirmation argument, pass "
        "`confirm: true` only after the required draft/assets are ready and "
        "then rely on the Manor runtime approval gate for user-visible actions.\n"
        "- If required content or media is missing, prepare the missing draft "
        "or asset, then continue to the integration tool. If no ready "
        "integration exists, say exactly what needs to be connected."
    )


def runtime_external_platform_draft_guidance(
    *,
    envelope,
    active_user_message: str | None,
    tool_names: Iterable[str] | None,
) -> str | None:
    if not runtime_allows_prompt_guidance(envelope, "external_platform_draft"):
        return None
    if not external_platform_draft_intent(active_user_message):
        return None
    loaded_tools = _tool_name_set(tool_names)
    if not {"write_file", "generate_file", "search_tools", "workspace_agent"}.intersection(loaded_tools):
        return None
    return (
        "## External Platform Draft\n"
        "- The latest user message appears to ask for platform-specific copy "
        "or visuals, but not to publish yet. Draft the requested content using "
        "the tools already available in this turn.\n"
        "- If `write_file` or `generate_file` is not loaded yet, call "
        "`search_tools` to load the needed file/media generation tool.\n"
        "- When the user later asks to post to a social platform, call "
        "`search_tools` for the platform MCP integration when one exists. "
        "Keep platform-specific browser logic out of the global prompt."
    )


def runtime_code_artifact_routing_guidance(
    *,
    envelope,
    active_user_message: str | None,
    tool_names: Iterable[str] | None,
    manual_skill_selected: bool = False,
) -> str | None:
    if not runtime_allows_prompt_guidance(envelope, "code_artifact"):
        return None
    if manual_skill_selected or not runtime_code_artifact_intent(active_user_message):
        return None
    loaded_tools = _tool_name_set(tool_names)
    if not {"generate_file", "search_tools"}.intersection(loaded_tools):
        return None
    route = (
        "Call `generate_file(kind='code')`"
        if "generate_file" in loaded_tools
        else "Call `search_tools` to load `generate_file`, then call `generate_file(kind='code')`"
    )
    return (
        "## Code Artifact Routing\n"
        "- The latest user message asks to create code, a website, web page, or frontend app. "
        f"{route}; do not create separate document/TXT files for source code.\n"
        "- Put the project in one bundle folder with `name`, `params.entry`, and "
        "`params.files=[{path, content}, ...]`. Preserve real extensions such as "
        "`index.html`, `styles.css`, `app.js`, `data.json`, `main.tsx`; never rename "
        "code files to `.txt`.\n"
        "- If media assets are needed, generate those first, then reference returned "
        "`/api/v1/fs/...` URLs from the code bundle."
    )


def runtime_workspace_artifact_routing_guidance(
    *,
    envelope,
    active_user_message: str | None,
    tool_names: Iterable[str] | None,
    workspace_id: str | None,
    manual_skill_selected: bool = False,
) -> str | None:
    if not runtime_allows_prompt_guidance(envelope, "workspace_artifact"):
        return None
    if manual_skill_selected:
        return None
    if not workspace_id or not runtime_workspace_artifact_intent(active_user_message):
        return None
    loaded_tools = _tool_name_set(tool_names)
    if not {"generate_file", "search_tools", "workspace_agent", "workspace_search"}.intersection(loaded_tools):
        return None
    artifact_intent = runtime_workspace_artifact_intent_details(active_user_message)
    is_creation_request = artifact_intent.is_creation_request

    generation_route = (
        "first check Available Skills for a matching specialist workflow and "
        "call `invoke_skill` when one is listed; otherwise call `generate_file` "
        "with the matching `kind` (for example `code`, `image`, `pdf`, "
        "`document`, `presentation`, `spreadsheet`, or `video`). Specialist "
        "`generate_file` kinds are compatibility fallbacks when no matching "
        "skill is available or selected"
        if "generate_file" in loaded_tools
        else (
            "first check Available Skills for a matching specialist workflow "
            "and call `invoke_skill` when one is listed; otherwise call "
            "`search_tools` to load `generate_file`, then call it with the "
            "matching `kind` (for example `code`, `image`, `pdf`, `document`, "
            "`presentation`, `spreadsheet`, or `video`)"
        )
    )
    lookup_or_creation_rule = (
        "- This is an artifact creation request. Do not call `manor`, "
        "`list_workspace_artifacts`, `list_documents`, `search_documents`, or "
        "`workspace_search(category='artifacts')` before generation unless the "
        "user explicitly asks to find/reuse an existing file that is not already "
        f"attached. Instead, {generation_route}.\n"
        if is_creation_request
        else (
            "- This is an artifact lookup request: call "
            "`workspace_search(category='artifacts')` when available, or search "
            "workspace documents/artifacts before answering. If no file evidence "
            "exists, say that no artifact is currently recorded.\n"
        )
    )
    return (
        "## Workspace Artifact Routing\n"
        "- The latest workspace message appears to involve a user-visible artifact: a file, "
        "document, media asset, export, attachment, or downloadable deliverable.\n"
        f"{lookup_or_creation_rule}"
        f"- For creation requests, {generation_route}. Save/report the returned artifact "
        "reference, such as `image_url`, `document_url`, `file_url`, `fs_path`, or `files`.\n"
        "- For websites/apps/code projects, use `generate_file(kind='code')` with "
        "`params.files=[{path, content}, ...]` so `index.html`, `styles.css`, `app.js`, "
        "and assets stay in one bundle; do not save CSS/JS as `.txt` files.\n"
        "- To include generated images, video, or audio in code, create those media assets "
        "first with `generate_file(kind='image'|'video'|'audio')`, then reference the returned "
        "`/api/v1/fs/...` URLs from the code bundle.\n"
        "- If attached or Knowledge images are referenced in the message, use their "
        "`[Image: ... -> /api/v1/fs/...]` / `[Image: ... → /api/v1/fs/...]` and "
        "`[Image from KB: ... -> /api/v1/fs/...]` / `[Image from KB: ... → /api/v1/fs/...]` "
        "URLs as `reference_urls` or `input_image_urls` when generating/editing images, "
        "or as video `first_frame_url`, `last_frame_url`, and `reference_urls` for video.\n"
        "- In Workspace chats, keep generated deliverables under the Workspace's own "
        "Knowledge folder; do not present entity-root folders like `images/` or `videos/` "
        "as the organizing location.\n"
        "- Do not claim an artifact exists unless the workspace state or a tool result has "
        "a concrete file/media/document reference.\n"
        "- If the user asks for a domain-native specialist file that Manor cannot produce "
        "with current tools, explain that specific limitation and offer the closest supported "
        "artifact format only when it fits the request."
    )


def runtime_workspace_in_flight_task_update_guidance(
    *,
    envelope,
    active_user_message: str | None,
    tool_names: Iterable[str] | None,
    workspace_id: str | None,
) -> str | None:
    if not runtime_allows_prompt_guidance(envelope, "workspace_task_update"):
        return None
    if not workspace_id or not runtime_workspace_in_flight_task_update_intent(active_user_message):
        return None
    loaded_tools = _tool_name_set(tool_names)
    if not {"workspace_update_task_runtime", "workspace_agent"}.intersection(loaded_tools):
        return None
    update_route = (
        "call `workspace_update_task_runtime` with `replace=false`"
        if "workspace_update_task_runtime" in loaded_tools
        else "call `workspace_agent` with `action='update_task_runtime'` and append-only params"
    )
    if "workspace_search" in loaded_tools:
        search_route = "call `workspace_search(category='tasks', status='in_progress')` first"
    elif "workspace_agent" in loaded_tools:
        search_route = "call `workspace_agent` with `action='search'` for running/in-progress tasks first"
    else:
        search_route = "use the visible active task context; if no task is visible, ask which task to update"
    return (
        "## Workspace In-Flight Task Update Routing\n"
        "- The latest workspace message appears to add requirements, roles, review stages, "
        "or workflow steps to work that is already running. Treat this as an update to "
        "an existing task, not a standalone answer.\n"
        "- This section overrides the generic workspace routing for this turn: first persist "
        "the task update; do not turn the role/stage text into workspace-wide policy or a "
        "new batch of tasks.\n"
        "- Keep the current work running. Do not cancel, restart, reset, replace, or "
        "contradict the original task unless the user explicitly asks for that.\n"
        "- Interpret added 'roles' as task-local review stages or workflow checkpoints. "
        "Do not create durable agents/services, `workspace_add_rule` rules, or governance "
        "policies from these role descriptions unless the user explicitly asks to configure "
        "the Workspace globally.\n"
        "- If this chat is already bound to an active `task_id`, "
        f"{update_route}; include the user's new instructions, role chain, sequencing, "
        "and preservation rules in `runtime_instructions` / task rules.\n"
        "- If no `task_id` is visible, "
        f"{search_route}. If exactly one active task is found or one task clearly matches "
        "the user's wording, update that task. If multiple active tasks are plausible, "
        "ask one short clarification instead of guessing.\n"
        "- Persist these changes append-only as a downstream or post-processing workflow. "
        "For example: current producer task continues, then validation/review/market/QA "
        "roles run in the requested order, and their findings may feed back into a final revision.\n"
        "- If the user mentions future reports, documents, checks, or final deliverables, record "
        "them as expected downstream outputs. Do not generate those artifacts immediately unless "
        "the user explicitly asks to run that stage now.\n"
        "- Do not call `workspace_create_task`, `workspace_request_strategist_review`, or "
        "`workspace_agent` actions `create_task`, `request_strategist_review`, or `add_rule` "
        "for this message unless the user explicitly asks to create new tasks, replan the whole "
        "workspace, or add persistent workspace-wide policy.\n"
        "- After the tool call succeeds, briefly confirm which task was updated and that "
        "the original running work was not interrupted."
    )
