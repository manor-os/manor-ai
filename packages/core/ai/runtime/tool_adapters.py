"""Runtime-owned adapters for helpers that still live in tool modules."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
import warnings


async def runtime_duckduckgo_news_results(
    *,
    query: str,
    region: str,
    timelimit: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Return raw DuckDuckGo news results when its optional client exists."""

    try:
        from ddgs import DDGS
    except ImportError:
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=RuntimeWarning)
                from duckduckgo_search import DDGS  # type: ignore
        except ImportError:
            return []

    def _search() -> list[dict[str, Any]]:
        return list(
            DDGS().news(
                query or "news",
                region=region,
                safesearch="moderate",
                timelimit=timelimit,
                max_results=max_results,
            )
        )

    return await asyncio.to_thread(_search)


async def runtime_register_video_edit_artifact(
    *,
    abs_path: Path,
    entity_root: Path,
    entity_id: str,
    context: Any,
    artifact_role: str,
    generation: dict[str, Any],
) -> dict[str, Any]:
    """Project one video-editor output through the media artifact helpers."""

    import mimetypes

    from packages.core.services.generated_artifact_service import (
        bind_generated_artifact_to_workspace,
        register_generated_file_artifact,
    )

    rel_path = abs_path.relative_to(entity_root).as_posix()
    mime_type = mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"
    file_type = abs_path.suffix.lower().lstrip(".") or "file"
    document_id = await register_generated_file_artifact(
        entity_id=entity_id,
        user_id=context.user_id or "",
        filename=abs_path.name,
        rel_path=rel_path,
        file_size=abs_path.stat().st_size,
        file_type=file_type,
        mime_type=mime_type,
        workspace_id=context.workspace_id,
        task_id=context.task_id,
        agent_id=context.agent_id,
        conversation_id=context.conversation_id,
        tool_name="video_edit",
        artifact_role=artifact_role,
        generation=generation,
    )
    await bind_generated_artifact_to_workspace(
        entity_id=entity_id,
        document_id=document_id,
        workspace_id=context.workspace_id,
        task_id=context.task_id,
        agent_id=context.agent_id,
        conversation_id=context.conversation_id,
        user_id=context.user_id or "",
        tool_name="video_edit",
    )
    return {
        "document_id": document_id,
        "name": abs_path.name,
        "fs_path": rel_path,
        "url": f"/api/v1/fs/{entity_id}/{rel_path}",
        "mime_type": mime_type,
        "size_bytes": abs_path.stat().st_size,
    }


__all__ = [
    "runtime_duckduckgo_news_results",
    "runtime_register_video_edit_artifact",
]
