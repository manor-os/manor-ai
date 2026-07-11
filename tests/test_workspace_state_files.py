from datetime import datetime, timezone
from types import SimpleNamespace

from packages.core.services.workspace_state_files import WorkspaceFileEntry, _render_files_md


def test_render_files_md_exposes_document_id_and_origin() -> None:
    workspace = SimpleNamespace(id="ws_1", name="Launch Workspace")
    files = [
        WorkspaceFileEntry(
            key="doc_1",
            name="campaign-hero.png",
            description="Final campaign hero image",
            location="assets/campaign-hero.png",
            document_id="doc_1",
            source="replicate, role=final",
            task_id="task_1",
            task_title="Generate campaign visuals",
            updated_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
        )
    ]

    rendered = _render_files_md(
        workspace,
        now=datetime(2026, 6, 25, tzinfo=timezone.utc),
        files=files,
    )

    assert "| Name | Document ID | What | Location | Origin | Updated |" in rendered
    assert "doc_1" in rendered
    assert "assets/campaign-hero.png" in rendered
    assert "task=Generate campaign visuals" in rendered
