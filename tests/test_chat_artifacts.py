import json
from types import SimpleNamespace

import pytest

from packages.core.services.chat_artifacts import chat_attachments_from_tool_results


def test_chat_attachments_from_generated_media_tool_results():
    tool_results = [
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "created": True,
                    "kind": "image",
                    "name": "hero.png",
                    "document_id": "doc_img",
                    "result_url": "/api/v1/fs/ent/images/hero.png",
                    "mime_type": "image/png",
                }
            ),
        },
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "created": True,
                    "kind": "video",
                    "name": "intro.mp4",
                    "document_id": "doc_vid",
                    "result_url": "/api/v1/fs/ent/videos/intro.mp4",
                    "mime_type": "video/mp4",
                }
            ),
        },
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "created": True,
                    "kind": "pdf",
                    "document": {
                        "document_id": "doc_pdf",
                        "name": "brief.pdf",
                        "fs_path": "documents/brief.pdf",
                        "file_type": "pdf",
                        "mime_type": "application/pdf",
                    },
                }
            ),
        },
    ]

    assert chat_attachments_from_tool_results(tool_results) == [
        {
            "name": "hero.png",
            "document_id": "doc_img",
            "type": "knowledge",
            "open_url": "/viewer/doc_img",
            "markdown_link": "[hero.png](/viewer/doc_img)",
            "fs_path": "images/hero.png",
            "fileType": "png",
            "mimeType": "image/png",
            "previewUrl": "/api/v1/fs/ent/images/hero.png",
        },
        {
            "name": "intro.mp4",
            "document_id": "doc_vid",
            "type": "knowledge",
            "open_url": "/viewer/doc_vid",
            "markdown_link": "[intro.mp4](/viewer/doc_vid)",
            "fs_path": "videos/intro.mp4",
            "fileType": "mp4",
            "mimeType": "video/mp4",
            "previewUrl": "/api/v1/fs/ent/videos/intro.mp4",
        },
        {
            "name": "brief.pdf",
            "document_id": "doc_pdf",
            "type": "knowledge",
            "open_url": "/viewer/doc_pdf",
            "markdown_link": "[brief.pdf](/viewer/doc_pdf)",
            "fs_path": "documents/brief.pdf",
            "fileType": "pdf",
            "mimeType": "application/pdf",
            "previewUrl": "/api/v1/fs/ent/documents/brief.pdf",
        },
    ]


def test_runtime_document_payload_keeps_exact_id_through_chat_artifacts():
    from packages.core.ai.runtime.document_actions import runtime_document_to_dict

    document = runtime_document_to_dict(
        SimpleNamespace(
            id="doc_exact",
            name="report.md",
            file_type="md",
            file_size=42,
        )
    )

    attachments = chat_attachments_from_tool_results([
        {
            "name": "generate_document_file",
            "raw_result": json.dumps({"created": True, "document": document}),
        }
    ])

    assert attachments == [
        {
            "name": "report.md",
            "document_id": "doc_exact",
            "type": "knowledge",
            "open_url": "/viewer/doc_exact",
            "markdown_link": "[report.md](/viewer/doc_exact)",
            "fileType": "md",
            "mimeType": "text/markdown",
        }
    ]

def test_chat_attachments_from_generated_code_bundle_files():
    tool_results = [
        {
            "name": "generate_file",
            "raw_result": {
                "created": True,
                "kind": "code",
                "files": [
                    {
                        "path": "code/site/index.html",
                        "url": "/api/v1/fs/ent/code/site/index.html",
                        "document_id": "doc_html",
                    },
                    {
                        "path": "code/site/styles.css",
                        "url": "/api/v1/fs/ent/code/site/styles.css",
                        "document_id": "doc_css",
                    },
                ],
            },
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == [
        {
            "name": "index.html",
            "document_id": "doc_html",
            "type": "knowledge",
            "open_url": "/viewer/doc_html",
            "markdown_link": "[index.html](/viewer/doc_html)",
            "fs_path": "code/site/index.html",
            "fileType": "html",
            "mimeType": "text/html",
            "previewUrl": "/api/v1/fs/ent/code/site/index.html",
        },
        {
            "name": "styles.css",
            "document_id": "doc_css",
            "type": "knowledge",
            "open_url": "/viewer/doc_css",
            "markdown_link": "[styles.css](/viewer/doc_css)",
            "fs_path": "code/site/styles.css",
            "fileType": "css",
            "mimeType": "text/css",
            "previewUrl": "/api/v1/fs/ent/code/site/styles.css",
        },
    ]


def test_chat_attachments_ignore_input_upload_references():
    tool_results = [
        {
            "name": "read_file",
            "raw_result": {
                "path": "uploads/chat/source.png",
                "url": "/api/v1/fs/ent/uploads/chat/source.png",
                "name": "source.png",
            },
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == []


def test_sandbox_save_result_is_not_chat_attachment_by_default():
    tool_results = [
        {
            "name": "sandbox_save_result",
            "raw_result": json.dumps(
                {
                    "saved": True,
                    "saved_to_knowledge": True,
                    "document_id": "doc_txt",
                    "name": "file.txt",
                    "fs_path": "file.txt",
                    "result_url": "/api/v1/fs/ent/file.txt",
                    "mime_type": "text/plain",
                }
            ),
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == []


def test_sandbox_save_result_can_opt_into_final_chat_attachment():
    tool_results = [
        {
            "name": "sandbox_save_result",
            "raw_result": json.dumps(
                {
                    "saved": True,
                    "saved_to_knowledge": True,
                    "display_as_artifact": True,
                    "artifact_role": "final",
                    "document_id": "doc_pdf",
                    "name": "final-report.pdf",
                    "fs_path": "reports/final-report.pdf",
                    "result_url": "/api/v1/fs/ent/reports/final-report.pdf",
                    "mime_type": "application/pdf",
                }
            ),
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == [
        {
            "name": "final-report.pdf",
            "document_id": "doc_pdf",
            "type": "knowledge",
            "open_url": "/viewer/doc_pdf",
            "markdown_link": "[final-report.pdf](/viewer/doc_pdf)",
            "fs_path": "reports/final-report.pdf",
            "fileType": "pdf",
            "mimeType": "application/pdf",
            "previewUrl": "/api/v1/fs/ent/reports/final-report.pdf",
        }
    ]


def test_composite_sandbox_save_result_is_not_chat_attachment_by_default():
    tool_results = [
        {
            "name": "sandbox",
            "arguments": {
                "action": "save_result",
                "params": {"sandbox_id": "sb_123", "path": "file.txt"},
            },
            "raw_result": {
                "saved": True,
                "saved_to_knowledge": True,
                "document_id": "doc_txt",
                "name": "file.txt",
                "fs_path": "file.txt",
                "result_url": "/api/v1/fs/ent/file.txt",
                "mime_type": "text/plain",
            },
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == []


def test_composite_sandbox_save_result_can_opt_into_final_chat_attachment():
    tool_results = [
        {
            "name": "sandbox",
            "arguments": {
                "action": "save_result",
                "params": {"sandbox_id": "sb_123", "path": "final-report.pdf"},
            },
            "raw_result": {
                "saved": True,
                "saved_to_knowledge": True,
                "display_as_artifact": True,
                "artifact_role": "final",
                "document_id": "doc_pdf",
                "name": "final-report.pdf",
                "fs_path": "reports/final-report.pdf",
                "result_url": "/api/v1/fs/ent/reports/final-report.pdf",
                "mime_type": "application/pdf",
            },
        }
    ]

    attachments = chat_attachments_from_tool_results(tool_results)

    assert len(attachments) == 1
    assert attachments[0]["document_id"] == "doc_pdf"
    assert attachments[0]["open_url"] == "/viewer/doc_pdf"


def test_chat_attachments_open_document_id_without_filesystem_reference():
    tool_results = [
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "created": True,
                    "kind": "pdf",
                    "document_id": "doc_pdf",
                    "name": "draft.pdf",
                    "mime_type": "application/pdf",
                }
            ),
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == [
        {
            "name": "draft.pdf",
            "document_id": "doc_pdf",
            "type": "knowledge",
            "open_url": "/viewer/doc_pdf",
            "markdown_link": "[draft.pdf](/viewer/doc_pdf)",
            "fileType": "pdf",
            "mimeType": "application/pdf",
        }
    ]


def test_chat_attachments_do_not_infer_document_ids_from_alias_fields():
    tool_results = []
    for key in ("documentId", "doc_id", "id"):
        tool_results.append({
            "name": "generate_file",
            "raw_result": {
                "created": True,
                "kind": "pdf",
                "name": f"{key}.pdf",
                key: f"doc_{key}",
                "mime_type": "application/pdf",
            },
        })

    assert chat_attachments_from_tool_results(tool_results) == []


@pytest.mark.asyncio
async def test_conversation_history_resolves_only_canonical_document_id():
    from packages.core.services.conversation_history import (
        _resolve_message_attachment_refs,
    )

    class NoDocumentLookup:
        async def execute(self, _statement):
            raise AssertionError("legacy attachment id must not trigger a Document lookup")

    refs = await _resolve_message_attachment_refs(
        NoDocumentLookup(),
        SimpleNamespace(entity_id="entity"),
        SimpleNamespace(attachments=[{"id": "doc_alias", "name": "legacy.pdf"}]),
    )

    assert refs == [{"id": "doc_alias", "name": "legacy.pdf"}]


def test_chat_attachments_open_external_url_without_filesystem_reference():
    tool_results = [
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "created": True,
                    "kind": "image",
                    "name": "remote.png",
                    "result_url": "https://cdn.example.com/remote.png",
                    "mime_type": "image/png",
                }
            ),
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == [
        {
            "name": "remote.png",
            "type": "file",
            "open_url": "https://cdn.example.com/remote.png",
            "markdown_link": "[remote.png](https://cdn.example.com/remote.png)",
            "fileType": "png",
            "mimeType": "image/png",
            "previewUrl": "https://cdn.example.com/remote.png",
        }
    ]


def test_chat_attachments_ignore_non_terminal_generated_files():
    tool_results = [
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "status": "processing",
                    "created": True,
                    "kind": "video",
                    "name": "clip.mp4",
                    "document_id": "doc_video",
                    "fs_path": "videos/clip.mp4",
                    "result_url": "/api/v1/fs/ent/videos/clip.mp4",
                    "mime_type": "video/mp4",
                }
            ),
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == []


@pytest.mark.asyncio
async def test_generated_media_attachment_carries_its_knowledge_document_id():
    from packages.core.services.chat_artifacts import chat_turn_attachments

    def image_result(name: str) -> dict:
        fs_path = f"Workspaces/_by_id/ws/{name}"
        open_url = f"/api/v1/fs/ent/{fs_path}"
        return {
            "name": "generate_file",
            "raw_result": json.dumps({
                "image_url": open_url,
                "saved_to_knowledge": True,
                "fs_path": fs_path,
                "file": {
                    "name": name, "fs_path": fs_path, "open_url": open_url,
                    "markdown_link": f"[{name}]({open_url})",
                },
            }),
        }

    class Documents:
        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: [("Workspaces/_by_id/ws/poster.png", "doc_poster")])

    attachments = await chat_turn_attachments(
        [image_result("poster.png"), image_result("scratch.png")], entity_id="ent", db=Documents(),
    )

    assert attachments == [
        {
            "name": "poster.png",
            "type": "knowledge",
            "open_url": "/viewer/doc_poster",
            "markdown_link": "[poster.png](/viewer/doc_poster)",
            "document_id": "doc_poster",
            "fs_path": "Workspaces/_by_id/ws/poster.png",
            "fileType": "png",
            "mimeType": "image/png",
            "previewUrl": "/api/v1/fs/ent/Workspaces/_by_id/ws/poster.png",
        },
        {
            "name": "scratch.png",
            "type": "knowledge",
            "open_url": "/api/v1/fs/ent/Workspaces/_by_id/ws/scratch.png",
            "markdown_link": "[scratch.png](/api/v1/fs/ent/Workspaces/_by_id/ws/scratch.png)",
            "fs_path": "Workspaces/_by_id/ws/scratch.png",
            "fileType": "png",
            "mimeType": "image/png",
            "previewUrl": "/api/v1/fs/ent/Workspaces/_by_id/ws/scratch.png",
        },
    ]


def _publish_result(site_id: str, revision: int, *, name: str = "Acme", published: bool = True) -> str:
    return json.dumps({
        "published": published,
        "url": f"https://{site_id}.sites.example.com",
        "name": name,
        "site_id": site_id,
        "slug": site_id,
        "revision": revision,
    })


def test_published_sites_become_one_site_attachment_each_latest_revision_wins():
    tool_results = [
        {"name": "publish_site", "raw_result": _publish_result("acme", 1)},
        # The same site through the call_tool gateway (e.g. after approval).
        {
            "name": "call_tool",
            "arguments": {"name": "publish_site", "arguments": {"name": "Acme v2"}},
            "raw_result": _publish_result("acme", 2, name="Acme v2"),
        },
        {"name": "publish_site", "raw_result": json.dumps({"published": False, "error": "x"})},
        {"name": "publish_site", "raw_result": _publish_result("other", 1, name="Other")},
    ]

    assert chat_attachments_from_tool_results(tool_results) == [
        {"type": "site", "name": "Acme v2", "url": "https://acme.sites.example.com", "site_id": "acme", "revision": 2},
        {"type": "site", "name": "Other", "url": "https://other.sites.example.com", "site_id": "other", "revision": 1},
    ]
